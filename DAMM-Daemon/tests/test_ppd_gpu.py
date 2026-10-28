"""power-profiles-daemon sync and GPU power info (nvidia-powerd / Dynamic Boost)."""
import json
import os
import subprocess as real_subprocess
import types
from pathlib import Path

import pytest

from conftest import build_fake_sysfs, read

PP = "sys/firmware/acpi/platform_profile"
STATE = "var/lib/damx/state.json"
NEW = "org.freedesktop.UPower.PowerProfiles"
LEGACY = "net.hadess.PowerProfiles"

POWER_SAMPLE = """==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:01:00.0
    GPU Power Readings
        Average Power Draw                : 9.88 W
        Instantaneous Power Draw          : 10.02 W
        GPU Ceiling Power Limit
            Current Power Limit           : 50.00 W
            Requested Power Limit         : N/A
            Default Power Limit           : 50.00 W
        Min Power Limit                   : 1.00 W
        Max Power Limit                   : 65.00 W
        Current Power Limit               : N/A
        Requested Power Limit             : N/A
        Default Power Limit               : N/A
        Min Power Limit                   : N/A
        Max Power Limit                   : N/A
"""

# Realistic `busctl --system monitor --json=short` lines (one JSON object per message).
SIGNAL_NEW = json.dumps({
    "type": "signal", "endian": "l", "flags": 1, "version": 1, "cookie": 812,
    "timestamp-realtime": 1761670000123456, "sender": ":1.12",
    "path": "/org/freedesktop/UPower/PowerProfiles", "interface": "org.freedesktop.DBus.Properties",
    "member": "PropertiesChanged",
    "payload": {"type": "sa{sv}as", "data": [
        "org.freedesktop.UPower.PowerProfiles",
        {"ActiveProfile": {"type": "s", "data": "performance"},
         "ActiveProfileHolds": {"type": "aa{sv}", "data": []}},
        []]}}, separators=(",", ":"))
SIGNAL_LEGACY = json.dumps({
    "type": "signal", "endian": "l", "flags": 1, "version": 1, "cookie": 813,
    "timestamp-realtime": 1761670000223456, "sender": ":1.12",
    "path": "/net/hadess/PowerProfiles", "interface": "org.freedesktop.DBus.Properties",
    "member": "PropertiesChanged",
    "payload": {"type": "sa{sv}as", "data": [
        "net.hadess.PowerProfiles", {"ActiveProfile": {"type": "s", "data": "power-saver"}}, []]}},
    separators=(",", ":"))
SIGNAL_INVALIDATED = json.dumps({
    "type": "signal", "member": "PropertiesChanged", "interface": "org.freedesktop.DBus.Properties",
    "payload": {"type": "sa{sv}as", "data": [NEW, {}, ["ActiveProfile"]]}})
SIGNAL_OTHER = json.dumps({
    "type": "signal", "member": "PropertiesChanged", "interface": "org.freedesktop.DBus.Properties",
    "payload": {"type": "sa{sv}as", "data": [NEW, {"PerformanceDegraded": {"type": "s", "data": ""}}, []]}})


class FakeSystem:
    """Fake subprocess module: busctl (ppd), systemctl (nvidia-powerd), nvidia-smi."""
    CalledProcessError = real_subprocess.CalledProcessError
    TimeoutExpired = real_subprocess.TimeoutExpired
    PIPE = real_subprocess.PIPE
    DEVNULL = real_subprocess.DEVNULL

    def __init__(self, names=(NEW,), active="balanced"):
        self.names = set(names)
        self.active = active
        self.calls = []
        self.unit = {"is-enabled": ("enabled", 0), "is-active": ("active", 0)}
        self.systemctl_rc = 0
        self.popen = None

    def _res(self, rc=0, out="", err=""):
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        exe = os.path.basename(cmd[0])
        if exe == "busctl":
            args = cmd[2:]
            if args[1] not in self.names:
                return self._res(1, "", f"Call failed: The name {args[1]} was not provided by any .service files")
            if args[0] == "get-property":
                return self._res(0, f's "{self.active}"\n')
            if args[0] == "set-property":
                self.active = args[-1]
                return self._res(0)
        if exe == "systemctl":
            if cmd[1] in self.unit:
                out, rc = self.unit[cmd[1]]
                return self._res(rc, out + "\n" if out else "",
                                 "" if out else "Failed to get unit file state for nvidia-powerd.service: "
                                                "No such file or directory\n")
            return self._res(self.systemctl_rc, "", "" if self.systemctl_rc == 0 else "Access denied")
        if exe == "nvidia-smi":
            return self._res(0, POWER_SAMPLE)
        raise AssertionError(f"unexpected command {cmd}")

    def Popen(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if self.popen is None:
            raise FileNotFoundError(cmd[0])
        return self.popen(cmd)

    def busctl(self, verb):
        return [c for c, _ in self.calls if os.path.basename(c[0]) == "busctl" and verb in c]

    def named(self, exe):
        return [(c, kw) for c, kw in self.calls if os.path.basename(c[0]) == exe]


@pytest.fixture
def sysfake(monkeypatch, pps, gp, fake_sp):
    fs = FakeSystem()
    monkeypatch.setattr(pps, "subprocess", fs)
    monkeypatch.setattr(gp, "subprocess", fs)
    monkeypatch.setattr(gp, "POWERD_UNIT_DIRS", ())
    return fs


def manager(root, damx, **kw):
    build_fake_sysfs(root, **kw)
    m = damx.DAMXManager()
    return m


def profile(root):
    return read(root, PP).strip()


# ------------------------------------------------------------- mapping
def test_default_mapping_valid(pps):
    m = pps.default_mapping()
    assert pps.validate_mapping(m) == m
    assert m["to_acer"]["ac"]["performance"] == "balanced-performance"
    assert m["to_acer"]["battery"]["performance"] == "balanced"


@pytest.mark.parametrize("update,msg", [
    ({"to_acer": {"ac": {"performance": "turbo"}}}, "'turbo' is not a known Acer profile"),
    ({"to_acer": {"ac": {"ludicrous": "balanced"}}}, "'ludicrous' is not a ppd profile"),
    ({"to_acer": {"desk": {"balanced": "balanced"}}}, "unknown power source"),
    ({"to_ppd": {"balanced": "fast"}}, "'fast' is not a ppd profile"),
    ({"to_ppd": {"turbo": "performance"}}, "'turbo' is not a known Acer profile"),
    ({"to_acer": "x"}, "'to_acer' must be an object"),
    ({"enabled": "yes"}, "'enabled' must be a boolean"),
])
def test_mapping_validation_errors(pps, update, msg):
    with pytest.raises(ValueError) as e:
        pps.merge_mapping(pps.default_mapping(), update)
    assert msg in str(e.value)


def test_mapping_partial_merge(pps):
    m = pps.merge_mapping(pps.default_mapping(), {"to_acer": {"battery": {"power-saver": "quiet"}},
                                                 "to_ppd": {"cool": "power-saver"}, "enabled": False})
    assert m["to_acer"]["battery"] == {"power-saver": "quiet", "balanced": "balanced", "performance": "balanced"}
    assert m["to_acer"]["ac"]["power-saver"] == "quiet"
    assert m["to_ppd"]["cool"] == "power-saver" and m["enabled"] is False


def test_set_get_ppd_sync_commands(root, damx, sysfake):
    m = manager(root, damx)
    srv = damx.DaemonServer(m)
    r = srv.process_command("get_ppd_sync", {})
    assert r["success"] and r["data"]["available"] is True and r["data"]["bus_name"] == NEW
    assert r["data"]["active_profile"] == "balanced" and r["data"]["power_source"] == "ac"
    r = srv.process_command("set_ppd_sync", {"enabled": False, "to_acer": {"ac": {"power-saver": "low-power"}}})
    assert r["success"] and r["data"]["enabled"] is False
    assert r["data"]["to_acer"]["ac"]["power-saver"] == "low-power"
    saved = json.loads(Path(root, STATE).read_text())["ppd_sync"]
    assert saved["enabled"] is False and saved["to_acer"]["ac"]["power-saver"] == "low-power"
    r = srv.process_command("set_ppd_sync", {"to_ppd": {"balanced": "warp"}})
    assert r == {"success": False, "error": "to_ppd.balanced: 'warp' is not a ppd profile "
                                            "(power-saver, balanced, performance)"}
    assert srv.process_command("set_ppd_sync", {"enabled": 1})["success"] is False
    for c in ("get_ppd_sync", "set_ppd_sync", "get_gpu_power_info"):
        assert not damx.is_privileged_command(c)


def test_invalid_saved_mapping_falls_back(root, pers, pps):
    p = Path(root, STATE)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"ppd_sync": {"enabled": True, "to_acer": {}, "to_ppd": {}},
                             "gpu_power_limits": {"default_limit_w": "x", "max_limit_w": 65}}))
    s = pers.StateStore(str(p)).snapshot()
    assert s["ppd_sync"] == pps.default_mapping()
    assert s["gpu_power_limits"] == {"default_limit_w": None, "max_limit_w": 65.0}


# ------------------------------------------------------------- detection / parsing
def test_parse_get_property(pps):
    assert pps.parse_get_property('s "balanced"\n') == "balanced"
    assert pps.parse_get_property("") is None
    assert pps.parse_get_property("garbage") is None


def test_parse_monitor_lines(pps):
    assert pps.parse_monitor_line(SIGNAL_NEW) == (True, "performance")
    assert pps.parse_monitor_line(SIGNAL_LEGACY) == (True, "power-saver")
    assert pps.parse_monitor_line(SIGNAL_INVALIDATED) == (True, None)
    assert pps.parse_monitor_line(SIGNAL_OTHER) == (False, None)
    assert pps.parse_monitor_line('{"type":"signal","member":"NameAcquired","payload":{"type":"s","data":[":1.5"]}}') \
        == (False, None)
    # tolerate un-wrapped variants
    plain = {"member": "PropertiesChanged", "payload": {"data": [NEW, {"ActiveProfile": "balanced"}, []]}}
    assert pps.parse_monitor_line(json.dumps(plain)) == (True, "balanced")
    with pytest.raises(ValueError):
        pps.parse_monitor_line("{truncated")
    with pytest.raises(ValueError):
        pps.parse_monitor_line("[1,2]")


@pytest.mark.parametrize("names,expected", [((NEW, LEGACY), NEW), ((LEGACY,), LEGACY), ((), None)])
def test_detect_bus_names(root, damx, sysfake, names, expected):
    sysfake.names = set(names)
    m = manager(root, damx)
    assert m.ppd_sync.detect() is (expected is not None)
    assert (m.ppd_sync.bus[0] if m.ppd_sync.bus else None) == expected


def test_ppd_absent(root, damx, sysfake):
    sysfake.names = set()
    m = manager(root, damx)
    srv = damx.DaemonServer(m)
    d = srv.process_command("get_ppd_sync", {})["data"]
    assert d["available"] is False and d["bus_name"] is None and d["active_profile"] is None
    assert m.get_all_settings()["ppd_sync"] == {"available": False, "enabled": True, "active_profile": None}
    n = len(sysfake.calls)
    assert srv.process_command("set_thermal_profile", {"profile": "low-power"})["success"]
    assert sysfake.busctl("set-property") == [] and len(sysfake.calls) == n
    assert m.ppd_sync.step(now=0) == 30.0  # retries detection later


def test_no_busctl_before_detection(root, damx, sysfake):
    """Nothing talks to D-Bus until the sync was started/queried."""
    m = manager(root, damx)
    assert m.set_thermal_profile("low-power")
    assert sysfake.calls == []


# ------------------------------------------------------------- direction 2 (DAMX -> ppd)
def test_direction2_user_change_syncs_ppd(root, damx, sysfake):
    m = manager(root, damx)
    m.ppd_sync.detect()
    srv = damx.DaemonServer(m)
    assert srv.process_command("set_thermal_profile", {"profile": "low-power"})["success"]
    assert m.ppd_sync.drain()
    sets = sysfake.busctl("set-property")
    assert sets == [["/usr/bin/busctl" if sets and sets[0][0] == "/usr/bin/busctl" else sets[0][0],
                     "--system", "set-property", NEW, "/org/freedesktop/UPower/PowerProfiles", NEW,
                     "ActiveProfile", "s", "power-saver"]]
    assert sysfake.active == "power-saver"
    # same ppd value -> no write
    assert m.set_thermal_profile("low-power")
    assert m.ppd_sync.drain()
    assert len(sysfake.busctl("set-property")) == 1
    m.ppd_sync.stop()


def test_direction2_rate_limited(root, damx, sysfake, monkeypatch):
    """At most one ppd write per second; only the latest queued value is written."""
    import time as _t
    stamps = []
    real_run = sysfake.run

    def run(cmd, **kw):
        if "set-property" in cmd:
            stamps.append(_t.monotonic())
        return real_run(cmd, **kw)
    monkeypatch.setattr(sysfake, "run", run)
    m = manager(root, damx)
    s = m.ppd_sync
    s.detect()
    s.on_acer_profile("low-power")
    assert s.drain()
    s.on_acer_profile("balanced-performance")   # within 1 s -> queued
    s.on_acer_profile("balanced")               # replaces the queued value
    assert s.drain(5)
    assert [c[-1] for c in sysfake.busctl("set-property")] == ["power-saver", "balanced"]
    assert stamps[1] - stamps[0] >= 0.99
    s.stop()


def test_set_property_does_not_block_profile_change(root, damx, sysfake, monkeypatch):
    import threading
    import time as _t
    gate = threading.Event()
    real_run = sysfake.run

    def slow(cmd, **kw):
        if "set-property" in cmd:
            gate.wait(5)
        return real_run(cmd, **kw)
    monkeypatch.setattr(sysfake, "run", slow)
    m = manager(root, damx)
    m.ppd_sync.detect()
    t0 = _t.monotonic()
    assert m.set_thermal_profile("low-power")
    assert _t.monotonic() - t0 < 0.5, "profile change waited for busctl"
    gate.set()
    assert m.ppd_sync.drain()
    assert sysfake.active == "power-saver"
    m.ppd_sync.stop()


def test_user_ppd_change_drops_queued_write(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.detect()
    s.on_acer_profile("low-power")              # written now
    assert s.drain()
    s.on_acer_profile("balanced-performance")   # queued (rate limit)
    assert s._pending == "performance"
    s.on_ppd_profile("balanced", now=s.clock() + 0.1)   # user changed ppd meanwhile
    assert s._pending is None
    assert s.drain(3)
    assert [c[-1] for c in sysfake.busctl("set-property")] == ["power-saver"]
    s.stop()


def test_direction2_disabled(root, damx, sysfake):
    m = manager(root, damx)
    m.ppd_sync.detect()
    m.state.set_ppd_sync(dict(m.state.ppd_sync(), enabled=False))
    assert m.set_thermal_profile("low-power")
    assert sysfake.busctl("set-property") == []


def test_direction2_from_restore_and_battery_logic(root, damx, psd, pers, sysfake):
    m = manager(root, damx, profile="performance", ac_online="0")
    m.ppd_sync.detect()
    d = psd.PowerSourceDetector(m)
    m.power_monitor = d
    d.check_power_source(now=0)            # battery logic -> balanced
    assert profile(root) == "balanced"
    assert sysfake.active == "balanced" and sysfake.busctl("set-property") == []  # already balanced
    m.ppd_sync._last_write = float("-inf")
    m.state.update_source("battery", thermal_profile="low-power")
    m.restore_state("resume")
    assert m.ppd_sync.drain()
    assert profile(root) == "low-power" and sysfake.active == "power-saver"


# ------------------------------------------------------------- direction 1 (ppd -> DAMX)
def test_direction1_applies_and_persists(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.detect()
    s.on_ppd_profile("performance", now=50)
    assert profile(root) == "balanced-performance"
    assert m.state.source("ac")["thermal_profile"] == "balanced-performance"
    assert sysfake.busctl("set-property") == []   # no echo back to ppd


def test_direction1_on_battery_uses_battery_map(root, damx, sysfake):
    m = manager(root, damx, ac_online="0")
    m.ppd_sync.detect()
    m.ppd_sync.on_ppd_profile("power-saver", now=1)
    assert profile(root) == "low-power"
    assert m.state.source("battery")["thermal_profile"] == "low-power"
    assert m.state.source("ac")["thermal_profile"] is None


@pytest.mark.parametrize("choices,expected", [
    ("low-power balanced performance", "balanced"),          # balanced-performance missing
    ("low-power quiet", "low-power"),
    ("low-power balanced balanced-performance", "balanced-performance"),
])
def test_direction1_nearest_available(root, damx, sysfake, choices, expected):
    m = manager(root, damx, choices=choices, profile="low-power" if expected != "low-power" else "quiet")
    m.ppd_sync.detect()
    m.ppd_sync.on_ppd_profile("performance", now=1)
    assert profile(root) == expected


def test_loop_guard_ignores_own_echo(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.clock = lambda: 100.0
    s.detect()
    s.on_acer_profile("low-power")                 # we set ppd power-saver
    assert s.drain()
    assert sysfake.active == "power-saver"
    s.active_profile = "balanced"                  # pretend the signal raced our cached value
    s.on_ppd_profile("power-saver", now=101)       # echo within 3 s -> ignored
    assert profile(root) == "balanced"             # (power-saver on AC would map to quiet->...)
    s.on_ppd_profile("performance", now=101.5)     # a different value is a real change
    assert profile(root) == "balanced-performance"


def test_loop_guard_window_expires(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.clock = lambda: 100.0
    s.detect()
    s.on_acer_profile("low-power")
    assert s.drain()
    s.active_profile = "balanced"
    s.on_ppd_profile("power-saver", now=103.5)     # after the 3 s window -> applied
    assert profile(root) == "balanced-performance"  # quiet not offered -> nearest


def test_direction1_disabled(root, damx, sysfake):
    m = manager(root, damx)
    m.ppd_sync.detect()
    m.state.set_ppd_sync(dict(m.state.ppd_sync(), enabled=False))
    m.ppd_sync.on_ppd_profile("power-saver", now=1)
    assert profile(root) == "balanced"


# ------------------------------------------------------------- AC/battery interplay
def _detector(root, damx, psd, *, active="balanced", **kw):
    m = manager(root, damx, **kw)
    d = psd.PowerSourceDetector(m)
    m.power_monitor = d
    d._sleep_offset = lambda: 0.0
    m.ppd_sync.detect()
    return m, d


def _unplug(root, d):
    Path(root, "sys/class/power_supply/ACAD/online").write_text("0\n")
    for t in (5, 10, 15):
        d.check_power_source(now=t)


def test_power_change_without_saved_profile_follows_ppd(root, damx, psd, sysfake):
    sysfake.active = "power-saver"
    m, d = _detector(root, damx, psd, profile="performance")
    d.check_power_source(now=0)
    _unplug(root, d)
    assert profile(root) == "low-power"   # to_acer.battery.power-saver (battery logic would say balanced)
    assert sysfake.busctl("set-property") == []


def test_power_change_saved_profile_wins_then_syncs_ppd(root, damx, psd, sysfake):
    sysfake.active = "power-saver"
    m, d = _detector(root, damx, psd, profile="performance")
    m.state.update_source("battery", thermal_profile="balanced")
    d.check_power_source(now=0)
    _unplug(root, d)
    assert profile(root) == "balanced"
    assert m.ppd_sync.drain()
    assert sysfake.active == "balanced"   # direction 2 after the restore


def test_power_change_ppd_sync_disabled_uses_battery_logic(root, damx, psd, sysfake):
    sysfake.active = "power-saver"
    m, d = _detector(root, damx, psd, profile="performance")
    m.state.set_ppd_sync(dict(m.state.ppd_sync(), enabled=False))
    d.check_power_source(now=0)
    _unplug(root, d)
    assert profile(root) == "balanced"


# ------------------------------------------------------------- monitor / polling
class FakeProc:
    def __init__(self, lines=b"", rc=None, close=True):
        self.r, self.w = os.pipe()
        if lines:
            os.write(self.w, lines)
        if close:
            os.close(self.w)
            self.w = None
        self.stdout = os.fdopen(self.r, "rb", buffering=0)
        self.rc = rc
        self.terminated = False

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15
        if self.w is not None:
            os.close(self.w)
            self.w = None

    def kill(self):
        self.terminate()


def test_monitor_signal_applies_profile(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    procs = []

    def popen(cmd):
        p = FakeProc((SIGNAL_NEW + "\n").encode(), close=False)
        procs.append((cmd, p))
        return p
    sysfake.popen = popen
    s.step(now=0, pump_timeout=1)
    cmd = procs[0][0]
    assert cmd[1:5] == ["--system", "monitor", "--json=short", "--match"]
    assert cmd[5] == ("type='signal',sender='org.freedesktop.UPower.PowerProfiles',"
                      "interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',"
                      "path='/org/freedesktop/UPower/PowerProfiles'")
    assert profile(root) == "balanced-performance"
    s.stop()
    assert procs[0][1].terminated and s._proc is None


def test_monitor_parse_problem_restarts_with_backoff(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    spawned = []
    sysfake.popen = lambda cmd: spawned.append(FakeProc(b"{broken json\n", close=False)) or spawned[-1]
    s.step(now=0, pump_timeout=1)
    assert spawned[0].terminated and s._proc is None
    assert s._next_monitor == 2.0
    polls = len(sysfake.busctl("get-property"))
    s.step(now=1)                 # still backing off: polls instead
    assert len(spawned) == 1 and len(sysfake.busctl("get-property")) == polls + 1
    s.step(now=2, pump_timeout=0.1)
    assert len(spawned) == 2
    s.stop()


def test_monitor_unsupported_falls_back_to_polling(root, damx, sysfake):
    """busctl without --json support exits immediately -> polling every 5 s."""
    m = manager(root, damx)
    s = m.ppd_sync
    spawned = []
    sysfake.popen = lambda cmd: spawned.append(FakeProc(rc=1)) or spawned[-1]
    now = 0.0
    while s.monitor_supported and now < 1000:
        s.step(now=now, pump_timeout=0.1)
        now = s._next_monitor if s._proc is None else now
    assert s.monitor_supported is False and len(spawned) == 3
    sysfake.active = "power-saver"
    assert s.step(now=now + 1) == 5.0
    assert profile(root) == "balanced-performance"   # quiet not offered -> nearest
    assert len(spawned) == 3


def test_popen_failure_falls_back_to_polling(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    sysfake.popen = None   # FileNotFoundError
    assert s.step(now=0) == 5.0
    assert s.monitor_supported is False


def test_invalidated_signal_triggers_poll(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.detect()
    sysfake.active = "performance"
    assert s.handle_monitor_line(SIGNAL_INVALIDATED, now=1)
    assert profile(root) == "balanced-performance"
    assert s.handle_monitor_line("Monitoring bus message stream.", now=2)
    assert s.handle_monitor_line("{nope", now=3) is False


def test_ppd_disappears_then_redetected(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.detect()
    s.monitor_supported = False
    sysfake.names = set()
    for t in range(3):
        s.step(now=t)
    assert s.available is False
    sysfake.names = {LEGACY}
    s.step(now=40)
    assert s.available and s.bus[0] == LEGACY


def test_thread_start_stop(root, damx, sysfake):
    m = manager(root, damx)
    s = m.ppd_sync
    s.poll_interval = 0.05
    s.monitor_supported = False
    s.start()
    from conftest import wait_for
    assert wait_for(lambda: len(sysfake.busctl("get-property")) >= 3)
    s.stop()
    assert not s._thread.is_alive()


# ------------------------------------------------------------- GPU power
def _nvidia(root, status):
    d = Path(root, "sys/bus/pci/devices/0000:01:00.0")
    (d / "power").mkdir(parents=True, exist_ok=True)
    (d / "vendor").write_text("0x10de\n")
    (d / "class").write_text("0x030000\n")
    (d / "power/runtime_status").write_text(status + "\n")


@pytest.fixture
def with_smi(monkeypatch, gp):
    monkeypatch.setattr(gp, "shutil", types.SimpleNamespace(which=lambda n: f"/usr/bin/{n}"))


def test_parse_power_limits(gp):
    assert gp.parse_power_limits(POWER_SAMPLE) == (50.0, 65.0)
    assert gp.parse_power_limits("Default Power Limit : N/A\n") == (None, None)


def test_gpu_info_reads_limits_once_when_awake(root, damx, sysfake, with_smi):
    m = manager(root, damx)
    _nvidia(root, "active")
    srv = damx.DaemonServer(m)
    d = srv.process_command("get_gpu_power_info", {})["data"]
    assert d == {"nvidia_present": True, "gpu_asleep": False,
                 "powerd": {"installed": True, "enabled": True, "active": True},
                 "default_limit_w": 50.0, "max_limit_w": 65.0, "dynamic_boost_supported": True}
    smi = sysfake.named("nvidia-smi")
    assert len(smi) == 1 and smi[0][0][1:] == ["-q", "-d", "POWER"] and smi[0][1]["timeout"] == 2
    srv.process_command("get_gpu_power_info", {})
    assert len(sysfake.named("nvidia-smi")) == 1
    assert json.loads(Path(root, STATE).read_text())["gpu_power_limits"] == {
        "default_limit_w": 50.0, "max_limit_w": 65.0}
    s = m.get_all_settings()
    assert s["gpu_power"] == {"powerd_active": True, "default_limit_w": 50.0, "max_limit_w": 65.0}
    assert len(json.dumps({"success": True, "data": s})) < 4096


@pytest.mark.parametrize("status", ["suspended", "suspending"])
def test_gpu_asleep_never_runs_nvidia_smi(root, damx, sysfake, with_smi, status):
    m = manager(root, damx)
    _nvidia(root, status)
    for _ in range(3):
        d = m.gpu_power.info()
    assert d["gpu_asleep"] is True and d["nvidia_present"] is True
    assert d["default_limit_w"] is None and d["dynamic_boost_supported"] is None
    assert sysfake.named("nvidia-smi") == []


def test_cached_limits_known_while_asleep_after_restart(root, damx, sysfake, with_smi, pers):
    p = Path(root, STATE)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"gpu_power_limits": {"default_limit_w": 50.0, "max_limit_w": 65.0}}))
    m = manager(root, damx)
    _nvidia(root, "suspended")
    d = m.gpu_power.info()
    assert (d["default_limit_w"], d["max_limit_w"], d["dynamic_boost_supported"]) == (50.0, 65.0, True)
    assert sysfake.named("nvidia-smi") == []


def test_no_nvidia_gpu(root, damx, sysfake, with_smi):
    m = manager(root, damx)
    d = m.gpu_power.info()
    assert d["nvidia_present"] is False and d["gpu_asleep"] is False
    assert sysfake.named("nvidia-smi") == []
    assert m.get_all_settings()["gpu_power"] == {"powerd_active": None, "default_limit_w": None,
                                                 "max_limit_w": None}


@pytest.mark.parametrize("enabled,active,expected", [
    (("enabled", 0), ("active", 0), {"installed": True, "enabled": True, "active": True}),
    (("disabled", 1), ("inactive", 3), {"installed": True, "enabled": False, "active": False}),
    (("", 1), ("inactive", 3), {"installed": False, "enabled": False, "active": False}),
])
def test_powerd_status_parsing(root, damx, sysfake, enabled, active, expected):
    sysfake.unit = {"is-enabled": enabled, "is-active": active}
    m = manager(root, damx)
    assert m.gpu_power.powerd_status(force=True) == expected
    calls = [c for c, _ in sysfake.named("systemctl")]
    assert calls[-2][1:] == ["is-enabled", "nvidia-powerd.service"]
    assert calls[-1][1:] == ["is-active", "nvidia-powerd.service"]


def test_powerd_status_cached(root, damx, sysfake):
    m = manager(root, damx)
    m.gpu_power.powerd_status()
    m.gpu_power.powerd_status()
    assert len(sysfake.named("systemctl")) == 2


def _powerd_changes(sysfake):
    return [(c, kw) for c, kw in sysfake.named("systemctl") if "--now" in c]


def test_set_nvidia_powerd_replies_then_changes(root, damx, sysfake, monkeypatch):
    import threading
    import time as _t
    from conftest import wait_for
    m = manager(root, damx)
    srv = damx.DaemonServer(m)
    gate = threading.Event()
    real_run = sysfake.run

    def slow(cmd, **kw):
        if "--now" in cmd:
            gate.wait(5)
        return real_run(cmd, **kw)
    monkeypatch.setattr(sysfake, "run", slow)
    t0 = _t.monotonic()
    r = srv.process_command("set_nvidia_powerd", {"enabled": False})
    assert _t.monotonic() - t0 < 1.0, "reply waited for systemctl"
    assert r == {"success": True, "data": {"scheduled": True,
                                           "powerd": {"installed": True, "enabled": True, "active": True}}}
    gate.set()
    assert wait_for(lambda: _powerd_changes(sysfake))
    cmd, kw = _powerd_changes(sysfake)[0]
    assert cmd[0].startswith("/") and cmd[1:] == ["disable", "--now", "nvidia-powerd.service"]
    assert kw["timeout"] == 10
    # status queries for the reply use a short timeout (reply worst case < 4.5 s)
    queries = [kw for c, kw in sysfake.named("systemctl") if c[1] in ("is-enabled", "is-active")]
    assert queries[0]["timeout"] == 2


def test_set_nvidia_powerd_errors(root, damx, sysfake):
    from conftest import wait_for
    m = manager(root, damx)
    srv = damx.DaemonServer(m)
    assert srv.process_command("set_nvidia_powerd", {})["error"] == "missing required parameter 'enabled'"
    sysfake.systemctl_rc = 1
    assert srv.process_command("set_nvidia_powerd", {"enabled": True})["success"] is True  # scheduled
    assert wait_for(lambda: _powerd_changes(sysfake))
    assert m.gpu_power.set_powerd(True) == (False, "systemctl enable --now nvidia-powerd.service failed: "
                                                   "Access denied")
    m.gpu_power._powerd_cache = None
    sysfake.unit = {"is-enabled": ("", 1), "is-active": ("inactive", 3)}
    r = srv.process_command("set_nvidia_powerd", {"enabled": True})
    assert r == {"success": False, "error": "nvidia-powerd.service is not installed"}


def test_set_nvidia_powerd_requires_privilege(root, damx, sysfake):
    m = manager(root, damx)
    srv = damx.DaemonServer(m)
    peer = damx.PeerInfo(1, 1000, 1000, False)
    r = srv.process_command("set_nvidia_powerd", {"enabled": False}, peer=peer)
    assert r == {"success": False, "error": damx.PERMISSION_DENIED_ERROR}
    assert sysfake.named("systemctl") == []
    assert srv.process_command("get_gpu_power_info", {}, peer=peer)["success"]
    r = srv.process_command("set_nvidia_powerd", {"enabled": False}, peer=damx.PeerInfo(1, 0, 0, True))
    assert r["success"] and r["data"]["scheduled"] is True


def test_daemon_lifecycle_stops_ppd_monitor(root, damx, sysfake, monkeypatch):
    import threading
    from conftest import wait_for
    build_fake_sysfs(root)
    procs = []
    sysfake.popen = lambda cmd: procs.append(FakeProc(close=False)) or procs[-1]
    registered = {}
    monkeypatch.setattr(damx, "signal", types.SimpleNamespace(
        signal=lambda s, h: registered.__setitem__(s, h), SIGTERM=15, SIGINT=2, SIGHUP=1))
    d = damx.DAMXDaemon()
    assert d.setup()
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    assert wait_for(lambda: procs and len(registered) == 3)
    registered[15](15, None)
    t.join(5)
    assert not t.is_alive() and procs[0].terminated
