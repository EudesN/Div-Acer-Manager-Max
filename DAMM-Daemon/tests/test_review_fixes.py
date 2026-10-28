"""Regression tests for the v6 review: quiet pauses fan control, fan re-apply after
profile changes, GPU polling throttle, ppd safety poll, reload restore."""
import json
import os
from pathlib import Path

import pytest

from conftest import BASE_REL, build_fake_sysfs, read, wait_for
from test_fancurve import FakeSensors, curve
from test_ppd_gpu import FakeProc, FakeSystem

NITRO = BASE_REL + "/nitro_sense"
FAN = NITRO + "/fan_speed"
PP = "sys/firmware/acpi/platform_profile"
CHOICES = "low-power quiet balanced balanced-performance performance"
PAUSED = "Fan control is not available in the Quiet profile"


def driver_like(monkeypatch, m, root):
    """Emulate linuwu_sense: setting quiet/low-power writes fan_speed "0,0"."""
    real = m._write_file

    def write(path, value):
        ok = real(path, value)
        if ok and path.endswith("platform_profile") and str(value).strip() in ("quiet", "low-power"):
            Path(root, FAN).write_text("0,0\n")
        return ok
    monkeypatch.setattr(m, "_write_file", write)


@pytest.fixture
def m(root, damx, monkeypatch):
    build_fake_sysfs(root, choices=CHOICES)
    mgr = damx.DAMXManager()
    driver_like(monkeypatch, mgr, root)
    yield mgr
    mgr.curve_engine.stop()


@pytest.fixture
def srv(m, damx):
    return damx.DaemonServer(m)


# ------------------------------------------------------------- 1. quiet / fan re-apply
def test_low_power_reapplies_manual_fans(srv, m, root):
    assert srv.process_command("set_fan_mode", {"mode": "manual", "cpu": 30, "gpu": 40})["success"]
    assert srv.process_command("set_thermal_profile", {"profile": "low-power"})["success"]
    assert read(root, FAN) == "30,40\n"
    assert m.get_all_settings()["fan_control_paused"] is False


def test_max_reapplied_after_profile_change(srv, m, root):
    assert srv.process_command("set_fan_mode", {"mode": "max"})["success"]
    Path(root, FAN).write_text("0,0\n")  # firmware reset
    assert srv.process_command("set_thermal_profile", {"profile": "balanced"})["success"]
    assert read(root, FAN) == "100,100\n"


def test_quiet_pauses_and_leaving_resumes(srv, m, root):
    assert srv.process_command("set_fan_mode", {"mode": "manual", "cpu": 30, "gpu": 40})["success"]
    assert srv.process_command("set_thermal_profile", {"profile": "quiet"})["success"]
    assert read(root, FAN) == "0,0\n"
    s = srv.process_command("get_all_settings", {})["data"]
    assert s["fan_mode"] == "auto" and s["fan_control_paused"] is True
    # saved per-source fan mode is untouched
    assert m.state.source("ac")["fan_mode"] == "manual" and m.state.source("ac")["fan_manual"] == [30, 40]
    for cmd, params in (("set_fan_mode", {"mode": "max"}),
                        ("set_fan_mode", {"mode": "manual", "cpu": 50, "gpu": 50}),
                        ("set_fan_speed", {"cpu": 100, "gpu": 100}),
                        ("set_fan_curve", {"curve": curve(), "activate": True})):
        assert srv.process_command(cmd, params) == {"success": False, "error": PAUSED}, cmd
    assert read(root, FAN) == "0,0\n"
    assert m.state.source("ac")["fan_mode"] == "manual"
    assert srv.process_command("set_thermal_profile", {"profile": "balanced"})["success"]
    assert read(root, FAN) == "30,40\n"
    s = srv.process_command("get_all_settings", {})["data"]
    assert s["fan_control_paused"] is False and s["fan_mode"] == "manual"


def test_quiet_pauses_curve_engine_and_resumes(srv, m, root, fc):
    m.curve_engine.sensors = FakeSensors(cpu=60, gpu=60)
    m.state.set_curve(fc.validate_curve(curve(interval=1)))
    assert srv.process_command("set_fan_mode", {"mode": "curve"})["success"]
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    assert srv.process_command("set_thermal_profile", {"profile": "quiet"})["success"]
    assert not m.curve_engine.active and read(root, FAN) == "0,0\n"
    assert m.get_fan_mode() == "auto"
    import time
    time.sleep(1.3)
    assert read(root, FAN) == "0,0\n"   # paused engine never writes
    assert srv.process_command("set_thermal_profile", {"profile": "balanced"})["success"]
    assert m.curve_engine.active
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    assert m.state.source("ac")["fan_mode"] == "curve"


def test_curve_reasserted_after_low_power(srv, m, root, fc):
    m.curve_engine.sensors = FakeSensors(cpu=60, gpu=60)
    m.state.set_curve(fc.validate_curve(curve(interval=1)))
    assert srv.process_command("set_fan_mode", {"mode": "curve"})["success"]
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    assert srv.process_command("set_thermal_profile", {"profile": "low-power"})["success"]
    assert m.curve_engine.active
    assert wait_for(lambda: read(root, FAN) == "60,60\n")  # re-asserted after the driver's 0,0


def test_restore_while_quiet_is_deferred(root, damx, pers, monkeypatch):
    build_fake_sysfs(root, choices=CHOICES, profile="quiet")
    p = Path(root, "var/lib/damx/state.json")
    p.parent.mkdir(parents=True)
    state = pers.default_state()
    state["ac"].update(thermal_profile="quiet", fan_mode="max")
    state["battery"].update(thermal_profile="balanced", fan_mode="max")
    p.write_text(json.dumps(state))
    mgr = damx.DAMXManager()
    driver_like(monkeypatch, mgr, root)
    assert mgr.fan_control_paused is True           # quiet at start-up
    mgr.restore_state("start")
    assert read(root, FAN) == "0,0\n" and mgr.fan_control_paused
    assert mgr.set_thermal_profile("balanced")
    assert read(root, FAN) == "100,100\n"            # deferred mode applied on leaving quiet


def test_ppd_power_saver_to_quiet_pauses(m, root, monkeypatch, pps, gp, fake_sp):
    sysfake = FakeSystem()
    monkeypatch.setattr(pps, "subprocess", sysfake)
    assert m.set_fan_mode("max")
    m.ppd_sync.detect()
    m.ppd_sync.on_ppd_profile("power-saver", now=1)   # ac map: power-saver -> quiet
    assert read(root, PP).strip() == "quiet"
    assert m.fan_control_paused and read(root, FAN) == "0,0\n"
    m.ppd_sync.stop()


def test_no_fan_feature_no_pause(root, damx):
    build_fake_sysfs(root, module_loaded=False, choices=CHOICES)
    mgr = damx.DAMXManager()
    assert mgr.set_thermal_profile("quiet")
    assert mgr.fan_control_paused is False


# ------------------------------------------------------------- 2. GPU polling throttle
def test_engine_reads_gpu_at_most_every_10s(fc):
    from test_fancurve import make_engine
    s = FakeSensors(cpu=60, gpu=50)
    eng, writes = make_engine(fc, s)
    eng.gpu_read_interval = fc.GPU_READ_INTERVAL
    for t in range(0, 21, 2):
        if t == 4:
            s.gpu = 80   # changes between reads are picked up at the next read only
        eng.step(now=t)
        if t == 4:
            assert eng.last["gpu_temp"] == 50 and eng.last["gpu_percent"] == 40  # cached
    assert s.gpu_calls == 3          # t=0, 10, 20
    assert eng.last["gpu_temp"] == 80
    assert eng.last["cpu_temp"] == 60   # CPU still read every step


def test_sensors_suspending_counts_as_asleep(root, fc, monkeypatch):
    import types
    d = Path(root, "sys/bus/pci/devices/0000:01:00.0")
    (d / "power").mkdir(parents=True)
    (d / "vendor").write_text("0x10de\n")
    (d / "class").write_text("0x030000\n")
    (d / "power/runtime_status").write_text("suspending\n")
    calls = []
    monkeypatch.setattr(fc, "subprocess", types.SimpleNamespace(run=lambda *a, **k: calls.append(a)))
    monkeypatch.setattr(fc, "shutil", types.SimpleNamespace(which=lambda n: "/usr/bin/nvidia-smi"))
    assert fc.TempSensors().gpu_temp() == (None, True)
    assert calls == []


# ------------------------------------------------------------- 3. ppd safety poll while monitoring
@pytest.fixture
def sysfake(monkeypatch, pps, gp, fake_sp):
    fs = FakeSystem()
    monkeypatch.setattr(pps, "subprocess", fs)
    monkeypatch.setattr(gp, "subprocess", fs)
    return fs


def test_poll_every_30s_while_monitor_up(m, root, sysfake):
    s = m.ppd_sync
    proc = FakeProc(close=False)              # monitor that never reports anything
    sysfake.popen = lambda cmd: proc
    s.step(now=0, pump_timeout=0.01)
    base = len(sysfake.busctl("get-property"))
    s.step(now=10, pump_timeout=0.01)
    s.step(now=29, pump_timeout=0.01)
    assert len(sysfake.busctl("get-property")) == base
    sysfake.active = "performance"            # change the monitor missed
    s.step(now=31, pump_timeout=0.01)
    assert len(sysfake.busctl("get-property")) == base + 1
    assert read(root, PP).strip() == "balanced-performance"
    assert s._proc is proc                    # monitor kept running
    s.stop()
    assert proc.terminated


def test_safety_poll_respects_loop_guard(m, root, sysfake):
    s = m.ppd_sync
    s.clock = lambda: 100.0
    proc = FakeProc(close=False)
    sysfake.popen = lambda cmd: proc
    s.step(now=100, pump_timeout=0.01)
    assert m.set_thermal_profile("low-power")  # hook sets ppd -> power-saver
    assert s.drain()
    s.active_profile = "balanced"             # stale cache: poll will see a "difference"
    s._last_poll = 70.0
    s.step(now=101, pump_timeout=0.01)        # poll within 3 s of our write -> echo ignored
    assert read(root, PP).strip() == "low-power"
    s.stop()


# ------------------------------------------------------------- 6. restore after driver reload
def test_reload_driver_restores_state(root, damx, pers, fake_sp):
    build_fake_sysfs(root, choices=CHOICES)
    p = Path(root, "var/lib/damx/state.json")
    p.parent.mkdir(parents=True)
    state = pers.default_state()
    state["ac"].update(thermal_profile="low-power", fan_mode="max")
    state["settings"]["battery_limiter"] = True
    p.write_text(json.dumps(state))
    mgr = damx.DAMXManager()
    # the reloaded driver comes back with defaults
    Path(root, FAN).write_text("0,0\n")
    Path(root, NITRO, "battery_limiter").write_text("0\n")
    assert mgr.reload_driver_and_redetect(("nitro_v4=1",))
    assert [os.path.basename(c[0]) for c in fake_sp.calls] == ["rmmod", "modprobe"]
    assert read(root, PP).strip() == "low-power"
    assert read(root, FAN) == "100,100\n"
    assert read(root, NITRO + "/battery_limiter") == "1\n"


def test_reload_unknown_model_does_not_restore(root, damx, pers, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True, choices=CHOICES)
    p = Path(root, "var/lib/damx/state.json")
    p.parent.mkdir(parents=True)
    state = pers.default_state()
    state["ac"].update(thermal_profile="low-power")
    p.write_text(json.dumps(state))
    mgr = damx.DAMXManager(auto_recover=False)
    mgr.reload_driver_and_redetect()
    assert read(root, PP).strip() == "balanced"
