"""Fan curve: validation, interpolation, hysteresis, safety, engine, sensors, fan modes."""
import copy
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import BASE_REL, read, wait_for

NITRO = BASE_REL + "/nitro_sense"
FAN = NITRO + "/fan_speed"


def curve(cpu=None, gpu=None, hysteresis=4, interval=2):
    pts = [[40, 20], [60, 60], [80, 100]]
    return {"cpu": cpu or copy.deepcopy(pts), "gpu": gpu or copy.deepcopy(pts),
            "hysteresis": hysteresis, "interval": interval}


class FakeSensors:
    def __init__(self, cpu=50.0, gpu=50.0, asleep=False):
        self.cpu, self.gpu, self.asleep = cpu, gpu, asleep
        self.gpu_calls = 0

    def cpu_temp(self):
        if isinstance(self.cpu, Exception):
            raise self.cpu
        return self.cpu

    def gpu_temp(self):
        self.gpu_calls += 1
        if self.asleep:
            return None, True
        return self.gpu, False


def make_engine(fc, sensors=None, c=None):
    writes = []

    def write(cpu, gpu, stop_event):
        if stop_event.is_set():
            return False
        writes.append((cpu, gpu))
        return True
    eng = fc.CurveEngine(write, sensors or FakeSensors())
    eng.curve = fc.validate_curve(c or curve())
    return eng, writes


# ------------------------------------------------------------- validation
def test_presets_and_default(fc):
    p = fc.presets()
    assert set(p) == {"silent", "balanced", "aggressive"}
    assert p["silent"]["cpu"] == [[40, 20], [55, 30], [65, 45], [75, 65], [85, 90]]
    assert p["balanced"]["gpu"] == [[40, 25], [50, 35], [60, 50], [70, 70], [80, 90], [90, 100]]
    assert p["aggressive"]["cpu"] == [[35, 35], [45, 50], [55, 65], [65, 80], [75, 100]]
    for c in p.values():
        assert c["hysteresis"] == 4 and c["interval"] == 2
        assert fc.validate_curve(c) == c
    assert fc.default_curve() == p["balanced"]


@pytest.mark.parametrize("cpu", [
    [[20, 1], [105, 100]],
    [[40, 30], [50, 30], [60, 30]],                      # flat is non-decreasing
    [[30 + i * 5, 10 + i * 10] for i in range(8)],       # 8 points
])
def test_valid_curves(fc, cpu):
    assert fc.validate_curve(curve(cpu=cpu))["cpu"] == cpu


@pytest.mark.parametrize("cpu,msg", [
    ([[40, 0], [60, 50]], "percent must be an integer between 1 and 100"),
    ([[40, 20], [60, 101]], "percent must be an integer between 1 and 100"),
    ([[60, 20], [40, 50]], "strictly increasing"),
    ([[40, 20], [40, 50]], "strictly increasing"),
    ([[40, 50], [60, 40]], "must not decrease"),
    ([[20 + i * 5, 10 + i * 5] for i in range(9)], "2 to 8"),
    ([[40, 20]], "2 to 8"),
    ([[19, 20], [60, 50]], "temperature must be an integer between 20 and 105"),
    ([[40, 20], [106, 50]], "temperature must be an integer between 20 and 105"),
    ([[40.5, 20], [60, 50]], "temperature must be an integer"),
    ([[40, True], [60, 50]], "percent must be an integer"),
    ([[40, 20, 1], [60, 50]], "must be [temp, percent]"),
    ("nope", "2 to 8"),
])
def test_invalid_curves(fc, cpu, msg):
    with pytest.raises(ValueError, match=None) as e:
        fc.validate_curve(curve(cpu=cpu))
    assert msg in str(e.value)


@pytest.mark.parametrize("kw,msg", [({"hysteresis": 11}, "hysteresis"), ({"hysteresis": -1}, "hysteresis"),
                                    ({"interval": 0}, "interval"), ({"interval": 11}, "interval"),
                                    ({"interval": 2.0}, "interval")])
def test_invalid_hysteresis_interval(fc, kw, msg):
    with pytest.raises(ValueError) as e:
        fc.validate_curve(curve(**kw))
    assert msg in str(e.value)


def test_curve_missing_parts(fc):
    with pytest.raises(ValueError):
        fc.validate_curve({"cpu": [[40, 20], [60, 50]]})
    with pytest.raises(ValueError):
        fc.validate_curve([1, 2])


# ------------------------------------------------------------- interpolation
@pytest.mark.parametrize("temp,pct", [(0, 20), (40, 20), (50, 40), (55, 50), (60, 60), (70, 80),
                                      (80, 100), (90, 100), (61, 62)])
def test_interpolation_and_clamp(fc, temp, pct):
    assert fc.interpolate([[40, 20], [60, 60], [80, 100]], temp) == pct


def test_clamp_above_last_point(fc):
    assert fc.interpolate([[40, 20], [70, 50]], 90) == 50


@pytest.mark.parametrize("temp", [95, 99.5, 120])
def test_safety_100_percent_at_95c(fc, temp):
    assert fc.interpolate([[40, 20], [70, 50]], temp) == 100


# ------------------------------------------------------------- engine logic
def test_engine_writes_target(fc):
    s = FakeSensors(cpu=50, gpu=70)
    eng, writes = make_engine(fc, s)
    eng.step(now=0)
    assert writes == [(40, 80)]
    assert eng.last == {"cpu_temp": 50, "gpu_temp": 70, "cpu_percent": 40, "gpu_percent": 80,
                        "gpu_asleep": False}


def test_hysteresis(fc):
    s = FakeSensors(cpu=60, gpu=60)
    eng, writes = make_engine(fc, s, curve(hysteresis=4))
    eng.step(now=0)
    assert writes[-1] == (60, 60)
    s.cpu = s.gpu = 58      # dropped only 2 C: keep 60 %
    eng.step(now=2)
    assert len(writes) == 1 and eng.last["cpu_percent"] == 60
    s.cpu = s.gpu = 56      # dropped 4 C: decrease to 52 %
    eng.step(now=4)
    assert writes[-1] == (52, 52)
    s.cpu = s.gpu = 57      # increase applies immediately (54 %)
    eng.step(now=6)
    assert writes[-1] == (54, 54)


def test_hysteresis_zero(fc):
    s = FakeSensors(cpu=60, gpu=60)
    eng, writes = make_engine(fc, s, curve(hysteresis=0))
    eng.step(now=0)
    s.cpu = s.gpu = 58
    eng.step(now=2)
    assert writes[-1] == (56, 56)


def test_small_changes_not_written_until_reassert(fc):
    s = FakeSensors(cpu=50, gpu=50)
    eng, writes = make_engine(fc, s, curve(hysteresis=0))
    eng.step(now=0)
    s.cpu = s.gpu = 50.4    # 41 % (+1): below the 2 % threshold
    eng.step(now=2)
    assert writes == [(40, 40)]
    eng.step(now=29.9)
    assert writes == [(40, 40)]
    eng.step(now=30.0)      # re-assert after 30 s even though nothing changed much
    assert writes == [(40, 40), (41, 41)]
    eng.step(now=40)
    assert len(writes) == 2
    eng.step(now=60.0)
    assert len(writes) == 3


def test_safety_override_in_engine(fc):
    s = FakeSensors(cpu=96, gpu=40)
    eng, writes = make_engine(fc, s, curve(cpu=[[40, 20], [70, 50]]))
    eng.step(now=0)
    assert writes == [(100, 20)]


def test_failsafe_after_three_bad_reads(fc):
    s = FakeSensors(cpu=60, gpu=60)
    eng, writes = make_engine(fc, s)
    eng.step(now=0)
    s.cpu = None
    eng.step(now=2)
    eng.step(now=4)
    assert writes == [(60, 60)]
    eng.step(now=6)            # third consecutive failure
    assert writes == [(60, 60), (0, 0)]
    eng.step(now=8)            # keeps retrying, no write spam
    assert writes == [(60, 60), (0, 0)]
    s.cpu = 60                 # recovered
    eng.step(now=10)
    assert writes[-1] == (60, 60)


def test_failsafe_on_exception(fc):
    s = FakeSensors(cpu=60)
    eng, writes = make_engine(fc, s)
    eng.step(now=0)
    s.cpu = RuntimeError("hwmon vanished")
    eng.step(now=2)
    assert writes[-1] == (0, 0)


def test_gpu_asleep_follows_cpu(fc):
    s = FakeSensors(cpu=70, gpu=None, asleep=True)
    eng, writes = make_engine(fc, s, curve(gpu=[[40, 90], [60, 95]]))
    eng.step(now=0)
    assert writes == [(80, 80)]
    assert eng.last["gpu_asleep"] is True and eng.last["gpu_temp"] is None


def test_engine_thread_start_stop(fc):
    s = FakeSensors(cpu=60, gpu=60)
    eng, writes = make_engine(fc, s)
    eng.start(curve(interval=1))
    assert wait_for(lambda: writes == [(60, 60)])
    assert eng.active
    eng.start(curve(interval=1))       # running: update + re-assert, no 2nd thread
    assert wait_for(lambda: len(writes) == 2)
    assert eng.stop() is True
    assert not eng.active
    n = len(writes)
    time.sleep(0.2)
    assert len(writes) == n
    assert eng.stop() is False


def test_engine_stop_write_auto(fc):
    eng, writes = make_engine(fc, FakeSensors(cpu=60, gpu=60))
    eng.start(curve(interval=1))
    assert wait_for(lambda: writes)
    eng.stop(write_auto=True)
    assert writes[-1] == (0, 0)
    writes.clear()
    eng.stop(write_auto=True)          # inactive: nothing written
    assert writes == []


# ------------------------------------------------------------- sensors
def _hwmon(root, idx, name, temps):
    d = Path(root, f"sys/class/hwmon/hwmon{idx}")
    d.mkdir(parents=True)
    (d / "name").write_text(name + "\n")
    for i, (label, milli) in enumerate(temps, 1):
        (d / f"temp{i}_label").write_text(label + "\n")
        (d / f"temp{i}_input").write_text(f"{milli}\n")


def test_cpu_temp_coretemp_package(root, fc):
    _hwmon(root, 0, "acpitz", [("x", 30000)])
    _hwmon(root, 1, "coretemp", [("Core 0", 50000), ("Package id 0", 61500)])
    assert fc.TempSensors().cpu_temp() == 61.5


def test_cpu_temp_k10temp_tctl(root, fc):
    _hwmon(root, 0, "k10temp", [("Tccd1", 40000), ("Tctl", 72000)])
    assert fc.TempSensors().cpu_temp() == 72.0


def test_cpu_temp_acer_fallback_and_missing(root, fc):
    assert fc.TempSensors().cpu_temp() is None
    _hwmon(root, 3, "acer", [("cpu", 55000)])
    assert fc.TempSensors().cpu_temp() == 55.0


def _nvidia(root, status):
    d = Path(root, "sys/bus/pci/devices/0000:01:00.0")
    (d / "power").mkdir(parents=True)
    (d / "vendor").write_text("0x10de\n")
    (d / "class").write_text("0x030000\n")
    (d / "power/runtime_status").write_text(status + "\n")
    igpu = Path(root, "sys/bus/pci/devices/0000:00:02.0")
    igpu.mkdir(parents=True)
    (igpu / "vendor").write_text("0x8086\n")
    (igpu / "class").write_text("0x030000\n")


class SmiRecorder:
    CalledProcessError = __import__("subprocess").CalledProcessError
    TimeoutExpired = __import__("subprocess").TimeoutExpired

    def __init__(self, out="47\n"):
        self.calls = []
        self.out = out

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout=self.out, stderr="")


def test_gpu_asleep_never_calls_nvidia_smi(root, fc, monkeypatch):
    _nvidia(root, "suspended")
    rec = SmiRecorder()
    monkeypatch.setattr(fc, "subprocess", rec)
    monkeypatch.setattr(fc, "shutil", types.SimpleNamespace(which=lambda n: "/usr/bin/nvidia-smi"))
    s = fc.TempSensors()
    for _ in range(5):
        assert s.gpu_temp() == (None, True)
    assert rec.calls == []


def test_gpu_active_reads_nvidia_smi_once(root, fc, monkeypatch):
    _nvidia(root, "active")
    rec = SmiRecorder()
    monkeypatch.setattr(fc, "subprocess", rec)
    monkeypatch.setattr(fc, "shutil", types.SimpleNamespace(which=lambda n: "/usr/bin/nvidia-smi"))
    assert fc.TempSensors().gpu_temp() == (47.0, False)
    assert len(rec.calls) == 1
    cmd, kw = rec.calls[0]
    assert cmd == ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
    assert kw["timeout"] == 2


def test_no_nvidia_gpu_no_process(root, fc, monkeypatch):
    rec = SmiRecorder()
    monkeypatch.setattr(fc, "subprocess", rec)
    assert fc.TempSensors().gpu_temp() == (None, False)
    assert rec.calls == []


# ------------------------------------------------------------- manager fan modes
def _engine_on(nitro, fc, temp=60):
    nitro.curve_engine.sensors = FakeSensors(cpu=temp, gpu=temp)
    nitro.state.set_curve(fc.validate_curve(curve(interval=1)))


def test_set_fan_mode_transitions(nitro, root, fc):
    _engine_on(nitro, fc)
    try:
        assert nitro.set_fan_mode("curve")
        assert wait_for(lambda: read(root, FAN) == "60,60\n")
        assert nitro.get_fan_mode() == "curve" and nitro.get_all_settings()["fan_curve_active"] is True
        assert nitro.set_fan_mode("manual", 30, 40)
        assert not nitro.curve_engine.active
        time.sleep(1.3)  # an engine iteration would have run by now
        assert read(root, FAN) == "30,40\n" and nitro.get_fan_mode() == "manual"
        assert nitro.set_fan_mode("max") and read(root, FAN) == "100,100\n"
        assert nitro.set_fan_mode("curve")
        assert wait_for(lambda: read(root, FAN) == "60,60\n")
        assert nitro.set_fan_mode("auto") and read(root, FAN) == "0,0\n"
        assert nitro.get_fan_mode() == "auto"
        assert nitro.set_fan_mode("turbo") is False
    finally:
        nitro.curve_engine.stop()


def test_legacy_set_fan_speed_stops_engine(nitro, root, fc):
    _engine_on(nitro, fc)
    assert nitro.set_fan_mode("curve")
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    assert nitro.set_fan_speed(25, 35)
    assert not nitro.curve_engine.active
    assert read(root, FAN) == "25,35\n"


def test_manual_set_cannot_race_engine_write(nitro, root, fc):
    """The engine is stopped before the manual write and never writes afterwards."""
    _engine_on(nitro, fc)
    with nitro.lock:   # engine thread blocks inside its write
        assert nitro.set_fan_mode("curve")
        time.sleep(0.2)
        t = threading.Thread(target=nitro.set_fan_mode, args=("auto",))
        t.start()
        time.sleep(0.2)
    t.join(5)
    time.sleep(0.2)
    assert read(root, FAN) == "0,0\n"
    assert not nitro.curve_engine.active


def test_shutdown_writes_auto_when_active(nitro, root, fc):
    _engine_on(nitro, fc)
    assert nitro.set_fan_mode("curve")
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    nitro.shutdown_fans()
    assert read(root, FAN) == "0,0\n" and not nitro.curve_engine.active


def test_shutdown_inactive_leaves_fans(nitro, root):
    assert nitro.set_fan_mode("manual", 30, 30)
    nitro.shutdown_fans()
    assert read(root, FAN) == "30,30\n"


def test_daemon_cleanup_writes_auto(root, damx, fc, monkeypatch):
    from conftest import build_fake_sysfs
    build_fake_sysfs(root)
    d = damx.DAMXDaemon()
    assert d.setup()
    _engine_on(d.manager, fc)
    assert d.manager.set_fan_mode("curve")
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    d.cleanup()
    assert read(root, FAN) == "0,0\n"


# ------------------------------------------------------------- commands
@pytest.fixture
def srv(nitro, damx):
    s = damx.DaemonServer(nitro)
    yield s
    nitro.curve_engine.stop()


def test_cmd_set_fan_mode(srv, root, damx):
    pc = srv.process_command
    assert pc("set_fan_mode", {"mode": "max"}) == {"success": True, "data": {"fan_mode": "max"}}
    assert read(root, FAN) == "100,100\n"
    assert pc("set_fan_mode", {"mode": "manual", "cpu": 30, "gpu": 45})["data"] == {"fan_mode": "manual"}
    assert read(root, FAN) == "30,45\n"
    assert pc("set_fan_mode", {"mode": "manual", "cpu": 30}) == {
        "success": False, "error": "missing required parameter 'gpu'"}
    assert pc("set_fan_mode", {"mode": "manual", "cpu": 0, "gpu": 50}) == {
        "success": False, "error": "parameter 'cpu' must be an integer between 1 and 100"}
    assert pc("set_fan_mode", {"mode": "turbo"}) == {
        "success": False, "error": "parameter 'mode' must be one of auto, max, manual, curve"}
    assert pc("set_fan_mode", {}) == {"success": False, "error": "missing required parameter 'mode'"}
    assert read(root, FAN) == "30,45\n"


@pytest.mark.parametrize("cpu,gpu,mode", [(0, 0, "auto"), (100, 100, "max"), (30, 60, "manual"),
                                          (0, 50, "manual")])
def test_legacy_set_fan_speed_mapping(srv, nitro, cpu, gpu, mode):
    r = srv.process_command("set_fan_speed", {"cpu": cpu, "gpu": gpu})
    assert r["success"]
    saved = nitro.state.source("ac")
    assert saved["fan_mode"] == mode
    if mode == "manual":
        assert saved["fan_manual"] == [cpu, gpu]
    assert nitro.get_all_settings()["fan_mode"] == mode


def test_cmd_fan_curve(srv, nitro, root, fc):
    pc = srv.process_command
    r = pc("get_fan_curve", {})
    assert r["success"] and r["data"]["active"] is False
    assert r["data"]["curve"] == fc.default_curve()
    assert set(r["data"]["presets"]) == {"silent", "balanced", "aggressive"}
    assert r["data"]["last"] == {"cpu_temp": None, "gpu_temp": None, "cpu_percent": None,
                                 "gpu_percent": None, "gpu_asleep": False}
    bad = curve(cpu=[[40, 0], [60, 50]])
    r = pc("set_fan_curve", {"curve": bad})
    assert r["success"] is False and "percent must be an integer between 1 and 100" in r["error"]
    assert pc("set_fan_curve", {})["error"] == "missing required parameter 'curve'"
    # save without activating
    c = curve(interval=1)
    r = pc("set_fan_curve", {"curve": c})
    assert r == {"success": True, "data": {"curve": c, "active": False}}
    assert nitro.state.curve() == c and nitro.state.source("ac")["fan_mode"] is None
    # activate
    nitro.curve_engine.sensors = FakeSensors(cpu=60, gpu=60)
    r = pc("set_fan_curve", {"curve": c, "activate": True})
    assert r["data"]["active"] is True
    assert wait_for(lambda: read(root, FAN) == "60,60\n")
    assert nitro.state.source("ac")["fan_mode"] == "curve"
    assert pc("get_all_settings", {})["data"]["fan_mode"] == "curve"
    # running engine picks up a new curve immediately
    c2 = curve(cpu=[[40, 30], [70, 90]], interval=1)
    assert pc("set_fan_curve", {"curve": c2})["data"]["active"] is True
    assert wait_for(lambda: read(root, FAN).startswith("70,"))
    assert pc("set_fan_curve", {"curve": c2, "activate": "yes"})["success"] is False


def test_cmd_set_fan_mode_curve_uses_saved_curve(srv, nitro, root, fc):
    nitro.state.set_curve(fc.validate_curve(curve(cpu=[[40, 33], [90, 33]], gpu=[[40, 44], [90, 44]],
                                                  interval=1)))
    nitro.curve_engine.sensors = FakeSensors(cpu=60, gpu=60)
    assert srv.process_command("set_fan_mode", {"mode": "curve"})["success"]
    assert wait_for(lambda: read(root, FAN) == "33,44\n")
    r = srv.process_command("get_fan_curve", {})["data"]
    assert r["active"] is True and r["last"]["cpu_percent"] == 33


def test_fan_commands_unsupported_without_driver(root, damx):
    from conftest import build_fake_sysfs
    build_fake_sysfs(root, module_loaded=False)
    s = damx.DaemonServer(damx.DAMXManager())
    assert s.process_command("set_fan_mode", {"mode": "auto"}) == {
        "success": False, "error": "Fan speed control is not supported on this device"}
    assert s.process_command("get_fan_curve", {})["success"]
