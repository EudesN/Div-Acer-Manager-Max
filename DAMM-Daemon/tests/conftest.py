"""Test harness for DAMX-Daemon.

The daemon hard-codes absolute paths (/sys/..., /dev/hidraw*, /etc/..., /var/...)
inline all over the code, so plain monkeypatching of "constants" is not enough.
We load the modules from source and rewrite every absolute-path string literal
so it is prefixed with ``_R()`` -- a function returning the current fake root.
Each test gets its own fake root (tmp_path) via the ``root`` fixture.

Paths under /run are rewritten too (runtime state lives in /run/damx).

Nothing here ever runs a real subprocess (rmmod/modprobe/systemctl): the
``subprocess`` module seen by the daemon modules is replaced with a recorder.
"""
import os
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.dont_write_bytecode = True  # keep the source tree free of __pycache__

SRC_DIR = Path(__file__).resolve().parent.parent

_ROOT = {0: tempfile.mkdtemp(prefix="damx-import-root-")}
os.makedirs(os.path.join(_ROOT[0], "var/log"), exist_ok=True)


def _R():
    return _ROOT[0]


_PATH_RE_F = re.compile(r'f"/(sys|dev|etc|var|tmp|proc|run)/')
_PATH_RE = re.compile(r'(?<![\w{])"/(sys|dev|etc|var|tmp|proc|run)/')
_PATH_RE_SQ = re.compile(r"(?<![\w{])'/(sys|dev|etc|var|tmp|proc|run)/")


def _transform(src: str) -> str:
    src = _PATH_RE_F.sub(r'f"{_R()}/\1/', src)
    src = _PATH_RE.sub(r'_R() + "/\1/', src)
    src = _PATH_RE_SQ.sub(r"_R() + '/\1/", src)
    return src


def _load(modname, filename):
    path = SRC_DIR / filename
    src = _transform(path.read_text())
    mod = types.ModuleType(modname)
    mod.__file__ = str(path)
    mod._R = _R
    sys.modules[modname] = mod
    exec(compile(src, str(path), "exec"), mod.__dict__)
    return mod


class FakeSubprocess:
    """Records calls instead of running rmmod/modprobe/systemctl."""

    CalledProcessError = __import__("subprocess").CalledProcessError
    TimeoutExpired = __import__("subprocess").TimeoutExpired
    DEVNULL = __import__("subprocess").DEVNULL
    PIPE = __import__("subprocess").PIPE

    def __init__(self):
        self.calls = []
        self.fail = False
        self.stdout = ""

    def run(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        if self.fail:
            if kw.get("check"):
                raise self.CalledProcessError(1, cmd)
        return types.SimpleNamespace(returncode=1 if self.fail else 0, stdout=self.stdout, stderr="")

    def Popen(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        return types.SimpleNamespace(pid=1234)


_real_time = __import__("time")


def fake_time_module():
    """The real time module, except that sleep() returns immediately."""
    ns = types.SimpleNamespace(**{k: getattr(_real_time, k) for k in dir(_real_time) if not k.startswith("_")})
    ns.sleep = lambda s: None
    return ns


# Import order matters: the daemon does `from PowerSourceDetection import ...`
psd_mod = _load("PowerSourceDetection", "PowerSourceDetection.py")
_orig_geteuid = os.geteuid
os.geteuid = lambda: 0  # module-level root check
try:
    daemon_mod = _load("damx_daemon", "DAMX-Daemon.py")
finally:
    os.geteuid = _orig_geteuid
kbm_mod = _load("KeyboardMonitor", "KeyboardMonitor.py")

# Don't spam the console / import-time log file during tests.
import logging  # noqa: E402

_lg = logging.getLogger("DAMXDaemon")
for h in list(_lg.handlers):
    _lg.removeHandler(h)
_lg.addHandler(logging.NullHandler())
_lg.propagate = True


BASE_REL = "sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi"

# Nitro V15 ANV15-51: quirk_acer_nitro -> ACER_CAP_NITRO_SENSE (not v4) ->
# nitro_sense_attr_group = version, fan_speed, battery_limiter,
# battery_calibration, usb_charging, backlight_timeout.  No four_zoned_kb
# (quirk has no four_zone_kb) unless enable_all.
NITRO_FILES = {
    "version": "v6.x\n",
    "fan_speed": "0,0\n",
    "battery_limiter": "0\n",
    "battery_calibration": "0\n",
    "usb_charging": "0\n",
    "backlight_timeout": "1\n",
}


def build_fake_sysfs(root: Path, *, group="nitro_sense", files=None, four_zone=False,
                     profile="balanced",
                     choices="low-power balanced balanced-performance performance",
                     ac_online="1", ac_name="ACAD", module_loaded=True, driver_present=None,
                     ac_type="Mains"):
    """module_loaded: linuwu_sense loaded with a recognised model.
    driver_present (default = module_loaded): /sys/module/linuwu_sense exists."""
    if driver_present is None:
        driver_present = module_loaded
    if driver_present:
        (root / "sys/module/linuwu_sense").mkdir(parents=True, exist_ok=True)
    if module_loaded:
        base = root / BASE_REL / group
        base.mkdir(parents=True)
        for name, content in (NITRO_FILES if files is None else files).items():
            (base / name).write_text(content)
        if four_zone:
            kb = root / BASE_REL / "four_zoned_kb"
            kb.mkdir(parents=True)
            (kb / "four_zone_mode").write_text("0,1,100,1,255,255,255\n")
            (kb / "per_zone_mode").write_text("ff0000,00ff00,0000ff,ffffff,100\n")
    acpi = root / "sys/firmware/acpi"
    acpi.mkdir(parents=True)
    (acpi / "platform_profile").write_text(profile + "\n")
    (acpi / "platform_profile_choices").write_text(choices + "\n")
    if ac_name:
        ps = root / "sys/class/power_supply" / ac_name
        ps.mkdir(parents=True)
        (ps / "online").write_text(ac_online + "\n")
        if ac_type:
            (ps / "type").write_text(ac_type + "\n")
    (root / "dev").mkdir(exist_ok=True)
    (root / "etc/modprobe.d").mkdir(parents=True, exist_ok=True)
    (root / "var/run").mkdir(parents=True, exist_ok=True)
    (root / "var/log").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    (root / "run").mkdir(exist_ok=True)
    return root / BASE_REL / group if module_loaded else None


@pytest.fixture
def damx():
    return daemon_mod


@pytest.fixture
def psd():
    return psd_mod


@pytest.fixture
def kbm():
    return kbm_mod


@pytest.fixture
def fake_sp(monkeypatch):
    fs = FakeSubprocess()
    monkeypatch.setattr(daemon_mod, "subprocess", fs)
    monkeypatch.setattr(psd_mod, "subprocess", fs)
    monkeypatch.setattr(kbm_mod, "subprocess", fs)
    return fs


@pytest.fixture
def root(tmp_path, monkeypatch, fake_sp):
    """Fresh fake filesystem root for each test; all daemon paths point into it."""
    # Unix socket paths are limited to ~107 bytes; keep root short.
    short = Path(tempfile.mkdtemp(prefix="damx-"))
    monkeypatch.setitem(_ROOT, 0, str(short))
    monkeypatch.setattr(daemon_mod, "SOCKET_PATH", str(short / "var/run/DAMX.sock"))
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(short / "etc/DAMX_Daemon/config.ini"))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(short / "var/run/DAMX-Daemon.pid"))
    monkeypatch.setattr(daemon_mod, "MODPROBE_CONFIG_PATH", str(short / "etc/modprobe.d/linuwu-sense.conf"))
    monkeypatch.setattr(daemon_mod, "RUNTIME_DIR", str(short / "run/damx"))
    monkeypatch.setattr(daemon_mod.DAMXManager, "RESTART_COUNTER_FILE", str(short / "run/damx/restart_attempts"))
    # Never touch the real systemctl path
    monkeypatch.setattr(daemon_mod, "SYSTEMCTL_PATH", "/usr/bin/systemctl")
    # Don't actually sleep in __init__/restart paths
    monkeypatch.setattr(daemon_mod, "time", fake_time_module())
    yield short
    import shutil
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def nitro(root, damx):
    """A DAMXManager on a fake Nitro V15 (no four-zone keyboard)."""
    build_fake_sysfs(root)
    return damx.DAMXManager()


@pytest.fixture
def nitro4z(root, damx):
    """Nitro with four_zoned_kb exposed (e.g. enable_all)."""
    build_fake_sysfs(root, four_zone=True)
    return damx.DAMXManager()


def read(root, rel):
    return (Path(root) / rel).read_text()


def cmd_names(calls):
    """Basenames of the executables in recorded subprocess calls."""
    return [[os.path.basename(c[0]), *c[1:]] for c in calls]


def wait_for(pred, timeout=3.0):
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if pred():
            return True
        _t.sleep(0.01)
    return pred()
