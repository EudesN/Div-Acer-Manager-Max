"""Settings persistence: state file, saving on user changes, restore on start/AC change/resume."""
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from conftest import BASE_REL, build_fake_sysfs, read

NITRO = BASE_REL + "/nitro_sense"
FAN = NITRO + "/fan_speed"
PP = "sys/firmware/acpi/platform_profile"
STATE = "var/lib/damx/state.json"


def write_state(root, pers, **over):
    state = pers.default_state()
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value
    p = Path(root, STATE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))
    return state


def profile(root):
    return read(root, PP).strip()


# ------------------------------------------------------------- state store
def test_defaults_without_file(root, damx, pers, fc):
    build_fake_sysfs(root)
    m = damx.DAMXManager()
    s = m.state.snapshot()
    assert s == {
        "version": 1, "restore_on_start": True,
        "ac": {"thermal_profile": None, "fan_mode": None, "fan_manual": None},
        "battery": {"thermal_profile": None, "fan_mode": None, "fan_manual": None},
        "curve": fc.default_curve(),
        "settings": {"battery_limiter": None, "usb_charging": None, "backlight_timeout": None},
        "ppd_sync": {
            "enabled": True,
            "to_acer": {
                "ac": {"power-saver": "quiet", "balanced": "balanced", "performance": "balanced-performance"},
                "battery": {"power-saver": "low-power", "balanced": "balanced", "performance": "balanced"},
            },
            "to_ppd": {"low-power": "power-saver", "quiet": "power-saver", "balanced": "balanced",
                       "balanced-performance": "performance", "performance": "performance"},
        },
        "gpu_power_limits": {"default_limit_w": None, "max_limit_w": None},
    }
    assert not Path(root, STATE).exists()  # nothing written until something changes


def test_save_is_atomic_with_permissions(root, pers, monkeypatch):
    path = str(Path(root, STATE))
    st = pers.StateStore(path)
    assert st.update_source("ac", thermal_profile="balanced")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o755
    assert json.loads(Path(path).read_text())["ac"]["thermal_profile"] == "balanced"

    def boom(*a):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "replace", boom)
    assert st.update_source("ac", thermal_profile="low-power") is False
    assert json.loads(Path(path).read_text())["ac"]["thermal_profile"] == "balanced"
    assert os.listdir(os.path.dirname(path)) == ["state.json"]


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"\xff\xfe", b""])
def test_corrupt_state_file_backed_up(root, pers, content):
    p = Path(root, STATE)
    p.parent.mkdir(parents=True)
    p.write_bytes(content)
    st = pers.StateStore(str(p))
    assert st.snapshot() == pers.default_state()
    assert Path(str(p) + ".bak").read_bytes() == content
    assert not p.exists()


def test_invalid_fields_sanitised(root, pers, fc):
    p = Path(root, STATE)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "restore_on_start": "yes",
        "ac": {"thermal_profile": 5, "fan_mode": "turbo", "fan_manual": [1, 200]},
        "battery": {"thermal_profile": "low-power", "fan_mode": "manual", "fan_manual": [20, 30]},
        "curve": {"cpu": [[40, 0], [50, 10]], "gpu": [], "hysteresis": 4, "interval": 2},
        "settings": {"battery_limiter": "true", "usb_charging": 20, "backlight_timeout": False},
    }))
    s = pers.StateStore(str(p)).snapshot()
    assert s["restore_on_start"] is True
    assert s["ac"] == {"thermal_profile": None, "fan_mode": None, "fan_manual": None}
    assert s["battery"] == {"thermal_profile": "low-power", "fan_mode": "manual", "fan_manual": [20, 30]}
    assert s["curve"] == fc.default_curve()
    assert s["settings"] == {"battery_limiter": None, "usb_charging": 20, "backlight_timeout": False}


# ------------------------------------------------------------- saving user changes
@pytest.fixture
def srv(nitro, damx):
    s = damx.DaemonServer(nitro)
    yield s
    nitro.curve_engine.stop()


def saved(root):
    return json.loads(Path(root, STATE).read_text())


def test_user_changes_saved_under_current_source(srv, root):
    pc = srv.process_command
    assert pc("set_thermal_profile", {"profile": "performance"})["success"]
    assert pc("set_fan_mode", {"mode": "manual", "cpu": 30, "gpu": 40})["success"]
    assert pc("set_battery_limiter", {"enabled": True})["success"]
    assert pc("set_usb_charging", {"level": 20})["success"]
    assert pc("set_backlight_timeout", {"enabled": False})["success"]
    assert pc("set_battery_calibration", {"enabled": True})["success"]  # not persisted
    s = saved(root)
    assert s["ac"] == {"thermal_profile": "performance", "fan_mode": "manual", "fan_manual": [30, 40]}
    assert s["battery"]["thermal_profile"] is None
    assert s["settings"] == {"battery_limiter": True, "usb_charging": 20, "backlight_timeout": False}


def test_changes_on_battery_saved_under_battery(root, damx):
    build_fake_sysfs(root, ac_online="0")
    srv = damx.DaemonServer(damx.DAMXManager())
    assert srv.process_command("set_thermal_profile", {"profile": "low-power"})["success"]
    assert srv.process_command("set_fan_mode", {"mode": "max"})["success"]
    s = saved(root)
    assert s["battery"]["thermal_profile"] == "low-power" and s["battery"]["fan_mode"] == "max"
    assert s["ac"]["thermal_profile"] is None


def test_failed_change_not_saved(srv, root):
    assert srv.process_command("set_thermal_profile", {"profile": "turbo"})["success"] is False
    assert not Path(root, STATE).exists()


def test_persistence_commands(srv, root, nitro):
    pc = srv.process_command
    r = pc("get_persistence", {})["data"]
    assert r["restore_on_start"] is True and r["power_source"] == "ac"
    assert r["state"] == nitro.state.snapshot()
    assert pc("set_persistence", {"restore_on_start": False}) == {
        "success": True, "data": {"restore_on_start": False}}
    assert saved(root)["restore_on_start"] is False
    assert pc("set_persistence", {"restore_on_start": "no"})["success"] is False
    assert pc("set_persistence", {})["error"] == "missing required parameter 'restore_on_start'"
    settings = pc("get_all_settings", {})["data"]
    assert settings["restore_on_start"] is False and settings["power_source"] == "ac"
    assert settings["fan_curve_active"] is False


def test_reset_persistence_keeps_curve(srv, root, nitro, fc):
    c = fc.presets()["silent"]
    pc = srv.process_command
    assert pc("set_fan_curve", {"curve": c})["success"]
    assert pc("set_thermal_profile", {"profile": "low-power"})["success"]
    assert pc("set_battery_limiter", {"enabled": True})["success"]
    assert pc("reset_persistence", {}) == {"success": True, "data": {"cleared": True}}
    s = saved(root)
    assert s["ac"] == {"thermal_profile": None, "fan_mode": None, "fan_manual": None}
    assert s["settings"] == {"battery_limiter": None, "usb_charging": None, "backlight_timeout": None}
    assert s["curve"] == c


def test_new_commands_not_privileged(damx):
    for c in ("set_fan_mode", "get_fan_curve", "set_fan_curve", "get_persistence",
              "set_persistence", "reset_persistence"):
        assert not damx.is_privileged_command(c)


# ------------------------------------------------------------- restore
def test_restore_on_start(root, damx, pers):
    build_fake_sysfs(root)
    write_state(root, pers, ac={"thermal_profile": "low-power", "fan_mode": "manual", "fan_manual": [30, 40]},
                settings={"battery_limiter": True, "usb_charging": 10, "backlight_timeout": False})
    m = damx.DAMXManager()
    m.restore_state("start")
    assert profile(root) == "low-power"
    assert read(root, FAN) == "30,40\n"
    assert read(root, NITRO + "/battery_limiter") == "1\n"
    assert read(root, NITRO + "/usb_charging") == "10\n"
    assert read(root, NITRO + "/backlight_timeout") == "0\n"


def test_restore_on_start_disabled(root, damx, pers):
    build_fake_sysfs(root)
    write_state(root, pers, restore_on_start=False,
                ac={"thermal_profile": "low-power", "fan_mode": "max"}, settings={"battery_limiter": True})
    m = damx.DAMXManager()
    m.restore_state("start")
    assert profile(root) == "balanced"
    assert read(root, FAN) == "0,0\n"
    assert read(root, NITRO + "/battery_limiter") == "0\n"


def test_saved_profile_not_in_choices_skipped(root, damx, pers):
    build_fake_sysfs(root, choices="low-power balanced")
    write_state(root, pers, ac={"thermal_profile": "performance", "fan_mode": "max"})
    m = damx.DAMXManager()
    assert m.restore_state("start") is False
    assert profile(root) == "balanced"
    assert read(root, FAN) == "100,100\n"  # fan mode still restored


def test_restore_curve_mode_on_start(root, damx, pers, fc):
    build_fake_sysfs(root)
    write_state(root, pers, ac={"fan_mode": "curve"})
    m = damx.DAMXManager()
    try:
        m.restore_state("start")
        assert m.curve_engine.active and m.get_fan_mode() == "curve"
    finally:
        m.curve_engine.stop()


def test_daemon_run_restores_at_start(root, damx, pers, monkeypatch):
    import threading
    import types
    from conftest import wait_for
    build_fake_sysfs(root)
    write_state(root, pers, ac={"thermal_profile": "low-power", "fan_mode": "max"})
    registered = {}
    monkeypatch.setattr(damx, "signal", types.SimpleNamespace(
        signal=lambda s, h: registered.__setitem__(s, h), SIGTERM=15, SIGINT=2, SIGHUP=1))
    d = damx.DAMXDaemon()
    assert d.setup()
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    assert wait_for(lambda: len(registered) == 3 and read(root, FAN) == "100,100\n")
    assert profile(root) == "low-power"
    registered[15](15, None)
    t.join(3)
    assert read(root, FAN) == "100,100\n"  # engine inactive -> fans not touched on shutdown


def _detector(root, damx, psd, pers, *, ac_online="1", start_profile="performance", **state):
    build_fake_sysfs(root, profile=start_profile, ac_online=ac_online)
    write_state(root, pers, **state)
    m = damx.DAMXManager()
    d = psd.PowerSourceDetector(m)
    m.power_monitor = d
    d._sleep_offset = lambda: 0.0
    return m, d


def _unplug(root, d, start=5):
    Path(root, "sys/class/power_supply/ACAD/online").write_text("0\n")
    for t in (start, start + 5, start + 10):
        d.check_power_source(now=t)


def test_ac_change_applies_saved_battery_profile_over_auto_logic(root, damx, psd, pers):
    m, d = _detector(root, damx, psd, pers,
                     battery={"thermal_profile": "low-power", "fan_mode": "manual", "fan_manual": [20, 25]})
    d.check_power_source(now=0)
    assert profile(root) == "performance"
    _unplug(root, d)
    assert d.current_source is False
    assert profile(root) == "low-power"          # not "balanced" from the automatic logic
    assert read(root, FAN) == "20,25\n"


def test_ac_change_without_saved_profile_uses_battery_logic(root, damx, psd, pers):
    m, d = _detector(root, damx, psd, pers, battery={"fan_mode": "auto"})
    d.check_power_source(now=0)
    Path(root, FAN).write_text("50,50\n")
    _unplug(root, d)
    assert profile(root) == "balanced"
    assert read(root, FAN) == "0,0\n"


def test_back_to_ac_restores_ac_profile(root, damx, psd, pers):
    m, d = _detector(root, damx, psd, pers, ac_online="0", start_profile="low-power",
                     ac={"thermal_profile": "performance", "fan_mode": "max"})
    d.check_power_source(now=0)
    Path(root, "sys/class/power_supply/ACAD/online").write_text("1\n")
    for t in (5, 10, 15):
        d.check_power_source(now=t)
    assert profile(root) == "performance" and read(root, FAN) == "100,100\n"


def test_battery_profile_rejected_by_kernel_falls_back(root, damx, psd, pers, monkeypatch):
    m, d = _detector(root, damx, psd, pers, battery={"thermal_profile": "performance"})
    real = m.set_thermal_profile
    monkeypatch.setattr(m, "set_thermal_profile", lambda p: False if p == "performance" else real(p))
    d.check_power_source(now=0)
    _unplug(root, d)
    assert profile(root) == "balanced"


def test_initial_observation_does_not_double_apply(root, damx, psd, pers, monkeypatch):
    m, d = _detector(root, damx, psd, pers, start_profile="balanced",
                     ac={"thermal_profile": "low-power", "fan_mode": "max"})
    m.restore_state("start")
    calls = []
    real = m.set_thermal_profile
    monkeypatch.setattr(m, "set_thermal_profile", lambda p: calls.append(p) or real(p))
    d.check_power_source(now=0)
    assert calls == [] and profile(root) == "low-power"


def test_restore_after_resume(root, damx, psd, pers):
    m, d = _detector(root, damx, psd, pers, start_profile="balanced",
                     ac={"thermal_profile": "low-power", "fan_mode": "max"},
                     settings={"battery_limiter": True})
    d.check_power_source(now=0)
    # firmware reset everything during S3
    Path(root, PP).write_text("balanced\n")
    Path(root, FAN).write_text("0,0\n")
    Path(root, NITRO, "battery_limiter").write_text("0\n")
    d._sleep_offset = lambda: 600.0
    d.check_power_source(now=5)
    assert profile(root) == "low-power"
    assert read(root, FAN) == "100,100\n"
    assert read(root, NITRO + "/battery_limiter") == "1\n"


def test_resume_reasserts_running_curve(root, damx, psd, pers, fc):
    m, d = _detector(root, damx, psd, pers, ac={"fan_mode": "curve"})
    try:
        d.check_power_source(now=0)
        assert m.curve_engine.active
        m.curve_engine._force_write = False
        d._sleep_offset = lambda: 600.0
        d.check_power_source(now=5)
        assert m.curve_engine._force_write is True
    finally:
        m.curve_engine.stop()


def test_no_restore_when_disabled_on_change(root, damx, psd, pers):
    m, d = _detector(root, damx, psd, pers, restore_on_start=False,
                     battery={"thermal_profile": "low-power", "fan_mode": "max"})
    d.check_power_source(now=0)
    _unplug(root, d)
    assert profile(root) == "balanced"   # automatic battery logic only
    assert read(root, FAN) == "0,0\n"
