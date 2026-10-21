"""PowerSourceDetector: AC detection, debounce, resume grace and battery profile enforcement."""
import threading
import time
from pathlib import Path

import pytest

from conftest import build_fake_sysfs, read, wait_for

PP = "sys/firmware/acpi/platform_profile"


def _det(psd, mgr, **kw):
    return psd.PowerSourceDetector(mgr, **kw)


def _set_ac(root, online, name="ACAD"):
    Path(root, f"sys/class/power_supply/{name}/online").write_text(f"{online}\n")


def profile(root):
    return read(root, PP).strip()


# ------------------------------------------------------------- detection
@pytest.mark.parametrize("name", ["AC", "ACAD", "ADP1", "AC0"])
def test_ac_detection_paths(root, damx, psd, name):
    build_fake_sysfs(root, ac_name=name, ac_online="1")
    d = _det(psd, damx.DAMXManager())
    assert d._is_ac_connected() is True
    _set_ac(root, 0, name)
    assert d._is_ac_connected() is False


@pytest.mark.parametrize("name", ["AC", "ACAD", "ADP1"])
def test_ac_detection_legacy_name_without_type(root, damx, psd, name):
    build_fake_sysfs(root, ac_name=name, ac_online="1", ac_type=None)
    d = _det(psd, damx.DAMXManager())
    assert d._is_ac_connected() is True


def test_ac_detection_any_mains_or_usb_supply(root, damx, psd, fake_sp):
    build_fake_sysfs(root, ac_name="ACAD", ac_online="0")
    bat = Path(root, "sys/class/power_supply/BAT1")
    bat.mkdir()
    (bat / "type").write_text("Battery\n")
    (bat / "online").write_text("1\n")  # batteries are ignored
    d = _det(psd, damx.DAMXManager())
    assert d._is_ac_connected() is False
    usb = Path(root, "sys/class/power_supply/ucsi-source-psy-USBC000:001")
    usb.mkdir()
    (usb / "type").write_text("USB\n")
    (usb / "online").write_text("1\n")
    assert d._is_ac_connected() is True
    assert fake_sp.calls == []  # sysfs worked: no processes spawned


def test_no_power_supply_falls_back_to_tools(root, damx, psd, fake_sp):
    build_fake_sysfs(root, ac_name=None)
    d = _det(psd, damx.DAMXManager())
    fake_sp.stdout = "Adapter 0: on-line\n"
    assert d._is_ac_connected() is True
    assert [c[0] for c in fake_sp.calls] == ["upower", "acpi"]
    # Result is cached: no new processes on the next 5-second check
    assert d._is_ac_connected() is True
    assert len(fake_sp.calls) == 2


def test_failing_fallback_method_is_disabled(root, damx, psd, fake_sp):
    build_fake_sysfs(root, ac_name=None)
    d = _det(psd, damx.DAMXManager(), fallback_min_interval=0)
    fake_sp.fail = True
    for _ in range(10):
        assert d._is_ac_connected() is None
    assert len(fake_sp.calls) == 2 * d.MAX_METHOD_FAILURES


def test_upower_output_parsing(root, damx, psd, fake_sp):
    """Real `upower` output pads values ('online:              yes')."""
    build_fake_sysfs(root, ac_name=None)
    d = _det(psd, damx.DAMXManager())
    fake_sp.stdout = ("  native-path:          ACAD\n  power supply:         yes\n"
                      "  line-power\n    warning-level:       none\n    online:              yes\n")
    assert d._check_using_upower() is True
    fake_sp.stdout = "  line-power\n    online:              no\n"
    assert d._check_using_upower() is False
    fake_sp.stdout = "Daemon:\n  on-battery:          yes\n"
    assert d._check_using_upower() is False
    fake_sp.stdout = ""
    assert d._check_using_upower() is None


# ------------------------------------------------------------- battery policy
def test_battery_switches_performance_to_balanced(root, damx, psd):
    build_fake_sysfs(root, profile="performance", ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert profile(root) == "balanced"


def test_ac_does_not_touch_profile(root, damx, psd):
    build_fake_sysfs(root, profile="performance", ac_online="1")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert read(root, PP) == "performance\n"


def test_only_acts_on_transition(root, damx, psd):
    build_fake_sysfs(root, profile="performance", ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source(now=0)
    # User re-selects performance while still on battery: no re-enforcement
    Path(root, PP).write_text("performance\n")
    d.check_power_source(now=5)
    assert read(root, PP) == "performance\n"


@pytest.mark.parametrize("current", ["low-power", "balanced"])
def test_battery_friendly_profile_kept(root, damx, psd, current):
    """Kernel name is 'low-power' (not 'power-saver'); it must not be replaced."""
    build_fake_sysfs(root, profile=current, ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert profile(root) == current


def test_battery_fallback_never_picks_quiet(root, damx, psd):
    """linuwu_sense rejects QUIET on battery (-EOPNOTSUPP)."""
    build_fake_sysfs(root, profile="performance", choices="low-power quiet performance", ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert profile(root) == "low-power"


def test_battery_quiet_profile_is_replaced(root, damx, psd):
    build_fake_sysfs(root, profile="quiet", choices="quiet balanced performance", ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert profile(root) == "balanced"


def test_battery_no_friendly_profile_available(root, damx, psd):
    build_fake_sysfs(root, profile="performance", choices="quiet performance", ac_online="0")
    d = _det(psd, damx.DAMXManager())
    d.check_power_source()
    assert profile(root) == "performance"


def test_battery_falls_back_to_low_power_when_balanced_rejected(root, damx, psd, monkeypatch):
    build_fake_sysfs(root, profile="performance", ac_online="0")
    mgr = damx.DAMXManager()
    real = mgr.set_thermal_profile
    tried = []

    def kernel(p):
        tried.append(p)
        return False if p == "balanced" else real(p)
    monkeypatch.setattr(mgr, "set_thermal_profile", kernel)
    _det(psd, mgr).check_power_source()
    assert tried == ["balanced", "low-power"]
    assert profile(root) == "low-power"


# ------------------------------------------------------------- debounce / resume
def _started_on_ac(root, damx, psd, **kw):
    build_fake_sysfs(root, profile="performance", ac_online="1")
    d = _det(psd, damx.DAMXManager(), **kw)
    d._sleep_offset = lambda: 0.0
    d.check_power_source(now=0)
    assert d.current_source is True
    return d


def test_unplug_acted_on_only_after_debounce(root, damx, psd):
    d = _started_on_ac(root, damx, psd)
    _set_ac(root, 0)
    for t in (5, 10):
        d.check_power_source(now=t)
        assert profile(root) == "performance", f"acted after {t - 5}s"
    d.check_power_source(now=15)  # 3 consecutive checks spanning 10 s
    assert profile(root) == "balanced"
    assert d.current_source is False


def test_flapping_adapter_never_triggers_switch(root, damx, psd):
    """GitHub #229: under GPU load with the limiter on, AC flaps briefly."""
    d = _started_on_ac(root, damx, psd)
    for i in range(1, 40):
        _set_ac(root, i % 2)
        d.check_power_source(now=i * 5)
    assert profile(root) == "performance"
    assert d.current_source is True


def test_short_dropout_does_not_switch(root, damx, psd):
    d = _started_on_ac(root, damx, psd)
    _set_ac(root, 0)
    d.check_power_source(now=5)
    d.check_power_source(now=10)
    _set_ac(root, 1)
    d.check_power_source(now=15)
    _set_ac(root, 0)
    d.check_power_source(now=20)
    d.check_power_source(now=25)
    assert profile(root) == "performance"  # candidate restarted at 20
    d.check_power_source(now=30)
    assert profile(root) == "balanced"


def test_changes_ignored_during_resume_grace(root, damx, psd):
    """GitHub #253: spurious AC/battery transitions right after resume from S3."""
    d = _started_on_ac(root, damx, psd, resume_grace_seconds=30)
    _set_ac(root, 0)
    d._sleep_offset = lambda: 3600.0  # suspended for an hour
    for t in (5, 10, 15, 20, 25, 30, 35):
        d.check_power_source(now=t)
        assert profile(root) == "performance", f"acted during grace at t={t}"
    # grace ends at 35; debounce then needs 3 checks / 10 s
    d.check_power_source(now=40)
    d.check_power_source(now=45)
    assert profile(root) == "balanced"


def test_sleep_offset_monotonic_vs_boottime(root, damx, psd):
    build_fake_sysfs(root)
    d = _det(psd, damx.DAMXManager())
    assert d._sleep_offset() > -1.0
    assert d._detect_resume(0) is False  # first sample only records the offset


# ------------------------------------------------------------- thread lifecycle
def test_monitor_thread_stops(root, damx, psd, monkeypatch):
    build_fake_sysfs(root)
    d = _det(psd, damx.DAMXManager(), check_interval=0.01)
    calls = []
    monkeypatch.setattr(d, "_is_ac_connected", lambda: calls.append(1) or True)
    d.start_monitoring()
    d.start_monitoring()  # idempotent
    assert wait_for(lambda: len(calls) >= 3)
    d.stop_monitoring()
    assert not d.is_running()
    n = len(calls)
    time.sleep(0.1)
    assert len(calls) == n, "checks continued after stop_monitoring()"


def test_stop_while_check_in_progress(root, damx, psd, monkeypatch):
    build_fake_sysfs(root)
    d = _det(psd, damx.DAMXManager(), check_interval=0.01)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(2)
        return True
    monkeypatch.setattr(d, "_is_ac_connected", slow)
    d.start_monitoring()
    assert entered.wait(2)
    t = threading.Thread(target=d.stop_monitoring)
    t.start()
    release.set()
    t.join(3)
    assert not d.is_running()


def test_monitor_survives_exceptions(root, damx, psd, monkeypatch):
    """An exception in a check or in the handler must not kill monitoring."""
    build_fake_sysfs(root, ac_online="0")
    d = _det(psd, damx.DAMXManager(), check_interval=0.01)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) % 2:
            raise RuntimeError("transient")
        return False

    def boom(_):
        raise RuntimeError("handler failure")
    monkeypatch.setattr(d, "_is_ac_connected", flaky)
    monkeypatch.setattr(d, "_handle_power_change", boom)
    d.start_monitoring()
    try:
        assert wait_for(lambda: len(calls) >= 6)
        assert d.is_running()
    finally:
        d.stop_monitoring()
