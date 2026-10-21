"""Config loading and KeyboardMonitor."""
import logging
import os
import struct
import threading
import time
from pathlib import Path

import pytest

from conftest import build_fake_sysfs


# ------------------------------------------------------------- config
def test_default_config_created(root, damx):
    d = damx.DAMXDaemon()
    cfg = d.load_config()
    assert Path(damx.CONFIG_PATH).exists()
    assert cfg["General"]["LogLevel"] == "INFO"


def test_config_missing_keys_ok(root, damx):
    Path(damx.CONFIG_PATH).parent.mkdir(parents=True)
    Path(damx.CONFIG_PATH).write_text("[General]\n")
    assert "General" in damx.DAMXDaemon().load_config()


def test_config_unreadable_ok(root, damx):
    Path(damx.CONFIG_PATH).parent.mkdir(parents=True)
    os.mkdir(damx.CONFIG_PATH)  # exists but cannot be read as a file
    damx.DAMXDaemon().load_config()


@pytest.mark.parametrize("content", ["garbage without section\n",
                                     "[General]\n[General]\n",
                                     "[General]\nLogLevel\n",
                                     b"[General]\nLogLevel = \xff\xfe\n"])
def test_corrupt_config_backed_up_and_defaults_used(root, damx, content):
    """A corrupt config.ini used to raise configparser.Error and kill startup."""
    build_fake_sysfs(root)
    cfg = Path(damx.CONFIG_PATH)
    cfg.parent.mkdir(parents=True)
    if isinstance(content, bytes):
        cfg.write_bytes(content)
    else:
        cfg.write_text(content)
    d = damx.DAMXDaemon()
    try:
        assert d.setup() is True
        assert d.config["General"]["LogLevel"] == "INFO"
        backup = Path(damx.CONFIG_PATH + ".bak")
        assert backup.read_bytes() == (content if isinstance(content, bytes) else content.encode())
        assert "loglevel = info" in cfg.read_text().lower()  # fresh default config written
    finally:
        if d.power_monitor:
            d.power_monitor.stop_monitoring()


@pytest.mark.parametrize("level,expected", [("ERROR", logging.ERROR), ("warning", logging.WARNING),
                                            ("DEBUG", logging.DEBUG), ("bogus", logging.INFO)])
def test_loglevel_from_config_honoured(root, damx, level, expected):
    Path(damx.CONFIG_PATH).parent.mkdir(parents=True)
    Path(damx.CONFIG_PATH).write_text(f"[General]\nLogLevel = {level}\n")
    damx.DAMXDaemon().load_config()
    try:
        assert damx.log.level == expected
    finally:
        damx.log.setLevel(logging.DEBUG)


def test_verbose_overrides_loglevel(root, damx):
    Path(damx.CONFIG_PATH).parent.mkdir(parents=True)
    Path(damx.CONFIG_PATH).write_text("[General]\nLogLevel = ERROR\n")
    damx.DAMXDaemon(verbose=True).load_config()
    try:
        assert damx.log.level == logging.DEBUG
    finally:
        damx.log.setLevel(logging.DEBUG)


# ------------------------------------------------------------- keyboard monitor
DEVICES = """I: Bus=0011 Vendor=0001 Product=0001 Version=ab83
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
H: Handlers=sysrq kbd leds event3
B: EV=120013

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="Acer WMI hotkeys"
H: Handlers=kbd event9 rfkill
"""


def test_find_keyboard_device(root, kbm):
    Path(root, "proc/bus/input").mkdir(parents=True)
    Path(root, "proc/bus/input/devices").write_text(DEVICES)
    Path(root, "dev/input").mkdir(parents=True)
    Path(root, "dev/input/event3").write_text("")
    m = kbm.KeyboardMonitor()
    assert m.find_keyboard_device() == str(Path(root, "dev/input/event3"))


def test_find_keyboard_device_missing_node(root, kbm):
    Path(root, "proc/bus/input").mkdir(parents=True)
    Path(root, "proc/bus/input/devices").write_text(DEVICES)
    assert kbm.KeyboardMonitor().find_keyboard_device() is None


def _ev(code, value, etype=1):
    return struct.pack("QQHHi", 0, 0, etype, code, value)


def _run_monitor(kbm, monkeypatch, payload, wait=1.5):
    r, w = os.pipe()
    path = f"/dev/fd/{r}"
    m = kbm.KeyboardMonitor(target_keycode=425)
    hits = []
    monkeypatch.setattr(m, "execute_command", lambda: hits.append(1))
    m.device_path = path
    m.running = True
    t = threading.Thread(target=m.monitor_events, daemon=True)
    t.start()
    os.write(w, payload)
    time.sleep(wait)
    m.running = False
    t.join(3)
    os.close(w)
    try:
        os.close(r)
    except OSError:
        pass
    return hits


def test_keymonitor_single_press(kbm, monkeypatch):
    hits = _run_monitor(kbm, monkeypatch, _ev(425, 1) + _ev(425, 0) + _ev(30, 1))
    assert hits == [1]


def test_keymonitor_reads_all_queued_events(kbm, monkeypatch):
    """Device used to be opened buffered ('rb') + select(): queued events were
    swallowed into Python's buffer and not processed until more input came."""
    payload = _ev(30, 1) + _ev(425, 1)  # some key, then the NitroSense key
    hits = _run_monitor(kbm, monkeypatch, payload)
    assert hits == [1]


def test_keymonitor_many_events_and_partial_event(kbm, monkeypatch):
    payload = b"".join(_ev(30, 1) for _ in range(100)) + _ev(425, 1) + _ev(425, 1)[:10]
    hits = _run_monitor(kbm, monkeypatch, payload, wait=0.8)
    assert hits == [1]


def test_keymonitor_event_size_native(kbm):
    assert kbm.EVENT_SIZE == struct.calcsize("llHHi")


def _fake_pw(monkeypatch, kbm, uid=1000):
    import types
    users = {"alice": types.SimpleNamespace(pw_name="alice", pw_uid=uid, pw_dir="/home/alice")}

    def getpwnam(n):
        if n in users:
            return users[n]
        raise KeyError(n)
    monkeypatch.setattr(kbm, "pwd", types.SimpleNamespace(getpwnam=getpwnam))


def test_keymonitor_dbus_address_uses_desktop_user_uid(root, kbm, fake_sp, monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    _fake_pw(monkeypatch, kbm, uid=1000)
    m = kbm.KeyboardMonitor()
    assert m.execute_command()
    cmd = fake_sp.calls[-1]
    dbus = [c for c in cmd if c.startswith("DBUS_SESSION_BUS_ADDRESS=")][0]
    assert dbus == "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"
    assert "XAUTHORITY=/home/alice/.Xauthority" in cmd


def test_keymonitor_unknown_user(root, kbm, fake_sp, monkeypatch):
    monkeypatch.setenv("SUDO_USER", "mallory")
    _fake_pw(monkeypatch, kbm)
    assert kbm.KeyboardMonitor().execute_command() is False
    assert fake_sp.calls == []
