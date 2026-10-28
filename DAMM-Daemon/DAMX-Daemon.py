#!/usr/bin/env python3
# DAMX-Daemon - Manage Acer laptop features as root service communicating with Linuwu-sense drivers
# Compatible with Predator and Nitro laptops

import argparse
import configparser
import ctypes
import errno
import fcntl
import glob
import grp
import json
import logging
import logging.handlers
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from enum import Enum
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from FanCurve import CurveEngine, presets as fan_curve_presets, validate_curve
from GpuPower import GpuPowerInfo
from Persistence import StateStore
from PowerProfilesSync import PowerProfilesSync, merge_mapping
from PowerSourceDetection import PowerSourceDetector, read_external_power

# Constants
VERSION = "0.5.2"
SOCKET_PATH = "/var/run/DAMX.sock"
LOG_PATH = "/var/log/DAMX_Daemon_Log.log"
CONFIG_PATH = "/etc/DAMX_Daemon/config.ini"
PID_FILE = "/var/run/DAMX-Daemon.pid"
MODPROBE_CONFIG_PATH = "/etc/modprobe.d/linuwu-sense.conf"
# Runtime state (restart counter, ...).  The systemd unit sets
# RuntimeDirectory=damx; the daemon creates it with 0700 if missing.
RUNTIME_DIR = "/run/damx"
# Persistent user settings (systemd unit sets StateDirectory=damx).
STATE_PATH = "/var/lib/damx/state.json"
FAN_MODES = ("auto", "max", "manual", "curve")
PERSISTED_TOGGLES = ("battery_limiter", "backlight_timeout")

MODULE_NAME = "linuwu_sense"
SERVICE_NAME = "damx-daemon.service"
SYSTEMCTL_PATH = "/usr/bin/systemctl"

# ----------------------------------------------------------------- protocol
# Newline-delimited JSON: one request object + "\n" per message, one response
# object + "\n" per request.  Clients that send no newline are still accepted
# when the buffered bytes form complete JSON value(s).
MAX_MESSAGE_SIZE = 64 * 1024
CLIENT_IDLE_TIMEOUT = 300.0      # seconds a connection may stay idle
PARTIAL_MESSAGE_TIMEOUT = 1.0    # seconds to wait for the rest of an incomplete message
MAX_CLIENTS = 16

# ----------------------------------------------------------------- security
DAMX_GROUP = "damx"
PRIVILEGED_GROUPS = ("damx", "wheel", "sudo", "admin")
PERMISSION_DENIED_ERROR = "permission denied: add your user to the 'damx' group"
PRIVILEGED_COMMANDS = frozenset({
    "force_nitro_model",
    "force_predator_model",
    "force_enable_all",
    "set_modprobe_parameter_predator",
    "set_modprobe_parameter_nitro",
    "set_modprobe_parameter_enable_all",
    "remove_modprobe_parameter",
    "restart_daemon",
    "restart_drivers_and_daemon",
    "set_nvidia_powerd",
})
# Any future command with one of these prefixes is privileged as well.
PRIVILEGED_PREFIXES = ("force_", "set_modprobe_parameter", "remove_modprobe_parameter", "restart_")

VALID_MODPROBE_PARAMS = ("nitro_v4", "predator_v4", "enable_all")
HEX_COLOR_RE = re.compile(r"[0-9a-fA-F]{6}")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_CONFIG = {"General": {"LogLevel": "INFO", "AutoDetectFeatures": "True"}}

# Acer ENEK5130 HID RGB controller used by newer Nitro/Predator models where
# linuwu_sense exposes RGB sysfs files but color writes do not affect hardware.
ENEK5130_HID_ID = "HID_ID=0018:00000CF2:00005130"
ENEK5130_HID_NAME = "HID_NAME=ENEK5130:00 0CF2:5130"

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, type_, nr, size):
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(type_) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def HIDIOCSFEATURE(length):
    # linux/hidraw.h: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)
    return _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x06, length)


# Sysfs locations (functions so tests can redirect the filesystem root).
def _module_sysfs_path() -> str:
    return "/sys/module/linuwu_sense"


def _acer_wmi_path(*parts: str) -> str:
    return os.path.join("/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi", *parts)


def _platform_profile_path() -> str:
    return "/sys/firmware/acpi/platform_profile"


def _platform_profile_choices_path() -> str:
    return "/sys/firmware/acpi/platform_profile_choices"


def _tool_path(name: str, default: str) -> str:
    return shutil.which(name) or default


def _systemctl_path() -> str:
    if os.path.exists(SYSTEMCTL_PATH):
        return SYSTEMCTL_PATH
    return shutil.which("systemctl") or SYSTEMCTL_PATH


# Configure logging (level is set from config.ini in DAMXDaemon.load_config)
log = logging.getLogger("DAMXDaemon")
log.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

try:
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1024 * 1024 * 5, backupCount=5)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
except OSError as _e:
    log.warning(f"Cannot open log file {LOG_PATH}: {_e}")


# ----------------------------------------------------------------- helpers
def _is_int(value) -> bool:
    """True for real ints (bool is rejected even though it subclasses int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _coerce_bool(value) -> Optional[bool]:
    """Accept only true/false or the integers 0/1; anything else is invalid."""
    if isinstance(value, bool):
        return value
    if _is_int(value) and value in (0, 1):
        return bool(value)
    return None


def _is_hex_color(value) -> bool:
    return isinstance(value, str) and HEX_COLOR_RE.fullmatch(value) is not None


def _describe_oserror(e: OSError) -> str:
    if e.errno:
        text = e.strerror or os.strerror(e.errno)
        name = errno.errorcode.get(e.errno)
        if e.errno == errno.EOPNOTSUPP:
            name = "EOPNOTSUPP"  # same value as ENOTSUP on Linux; the kernel uses this name
        return f"{text} ({name})" if name else text
    return str(e)


def _short(value, limit: int = 64) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...'"


def get_peer_credentials(sock: socket.socket) -> Tuple[int, int, int]:
    """Return (pid, uid, gid) of the process on the other end of a unix socket."""
    fmt = '3i'
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(fmt))
    return struct.unpack(fmt, data)


def is_privileged_user(uid: int, gid: int) -> bool:
    """root, or a member (primary or supplementary) of damx/wheel/sudo/admin."""
    if uid == 0:
        return True
    if uid < 0:
        return False

    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        pw = None

    privileged_gids = set()
    for name in PRIVILEGED_GROUPS:
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        privileged_gids.add(group.gr_gid)
        if pw is not None and pw.pw_name in group.gr_mem:
            return True

    user_gids = {gid}
    if pw is not None:
        user_gids.add(pw.pw_gid)
        try:
            user_gids.update(os.getgrouplist(pw.pw_name, pw.pw_gid))
        except OSError:
            pass
    return bool(user_gids & privileged_gids)


def is_privileged_command(command: str) -> bool:
    return command in PRIVILEGED_COMMANDS or command.startswith(PRIVILEGED_PREFIXES)


class LaptopType(Enum):
    UNKNOWN = 0
    PREDATOR = 1
    NITRO = 2


class DAMXManager:
    """Manages all the DAMX-Daemon features"""

    # At most this many automatic module reloads per boot (/run is a tmpfs).
    MAX_RESTART_ATTEMPTS = 1
    RESTART_COUNTER_FILE = os.path.join(RUNTIME_DIR, "restart_attempts")
    DRIVER_SETTLE_POLLS = 20
    DRIVER_SETTLE_INTERVAL = 0.25

    def __init__(self, auto_recover: bool = True):
        # Serialises hardware writes: platform_profile, fan_speed and the
        # multi-packet ENEK RGB sequence.
        self.lock = threading.RLock()
        self._task_lock = threading.Lock()
        self._tls = threading.local()
        # Serialises fan-mode transitions (engine stop/start + manual writes).
        # Never held by the engine thread, so stopping the engine cannot deadlock.
        self._fan_mode_lock = threading.Lock()
        self.power_monitor = None
        self.state = StateStore(STATE_PATH)
        self.curve_engine = CurveEngine(self._engine_write)
        self.ppd_sync = PowerProfilesSync(self)
        self.gpu_power = GpuPowerInfo(self.state)
        self._startup_restore: Optional[Tuple[str, bool]] = None

        log.info(f"** Starting DAMX-Daemon v{VERSION} **")

        self._detect_hardware()
        if self.laptop_type == LaptopType.UNKNOWN and auto_recover:
            self._auto_recover_unknown()

        log.info(f"Detected laptop type: {self.laptop_type.name}")
        log.info(f"Driver loaded: {'Yes' if self.driver_loaded else 'No'} ({self.driver_status})")
        log.info(f"Base path: {self.base_path}")
        log.info(f"Four-zone keyboard: {'Yes' if self.has_four_zone_kb else 'No'}")
        log.info(f"ENEK5130 HID RGB device: {self.enek5130_hid_device or 'Not found'}")
        log.info(f"Available features: {', '.join(sorted(self.available_features))}")
        if self.laptop_type == LaptopType.UNKNOWN:
            log.warning(f"Running in reduced mode: {self.status_message}")

    # ------------------------------------------------------------ errors
    @property
    def last_error(self) -> Optional[str]:
        """Error text of the last failed operation in the calling thread."""
        return getattr(self._tls, "last_error", None)

    def _set_last_error(self, message: Optional[str]):
        self._tls.last_error = message

    # ------------------------------------------------------------ detection
    def _detect_hardware(self):
        with self.lock:
            self.driver_loaded = os.path.exists(_module_sysfs_path())
            self.laptop_type = self._detect_laptop_type()
            self.base_path = self._get_base_path()
            self.has_four_zone_kb = self._check_four_zone_kb()
            self.enek5130_hid_device = self._find_enek5130_hid_device()
            self.available_features = self._detect_available_features()

    @property
    def driver_status(self) -> str:
        """'ok', 'not_loaded' or 'unsupported_model'."""
        if self.laptop_type != LaptopType.UNKNOWN:
            return "ok"
        return "unsupported_model" if self.driver_loaded else "not_loaded"

    @property
    def status_message(self) -> str:
        status = self.driver_status
        if status == "ok":
            return "Driver loaded"
        if status == "not_loaded":
            return ("linuwu_sense driver is not loaded; only the thermal profile is available. "
                    "Install/load the linuwu_sense module.")
        return ("linuwu_sense is loaded but did not recognise this laptop; only the thermal profile "
                "is available. Try forcing the Nitro/Predator model.")

    def _detect_laptop_type(self) -> LaptopType:
        """Detect whether this is a Predator or Nitro laptop"""
        if os.path.exists(_acer_wmi_path("predator_sense")):
            return LaptopType.PREDATOR
        if os.path.exists(_acer_wmi_path("nitro_sense")):
            return LaptopType.NITRO
        return LaptopType.UNKNOWN

    def _get_base_path(self) -> str:
        """Get the base path for VFS access based on laptop type"""
        if self.laptop_type == LaptopType.PREDATOR:
            return _acer_wmi_path("predator_sense")
        if self.laptop_type == LaptopType.NITRO:
            return _acer_wmi_path("nitro_sense")
        return ""

    def _check_four_zone_kb(self) -> bool:
        """Check if four-zone keyboard is available"""
        if self.laptop_type != LaptopType.UNKNOWN:
            return os.path.exists(_acer_wmi_path("four_zoned_kb"))
        return False

    def _detect_available_features(self) -> Set[str]:
        """Detect which features are available on the current laptop"""
        available = set()

        # Always check thermal profile since it's ACPI standard
        if os.path.exists(_platform_profile_path()):
            available.add("thermal_profile")

        # Only check other features if laptop type is recognized
        if self.laptop_type != LaptopType.UNKNOWN and os.path.exists(self.base_path):
            for feature in ("backlight_timeout", "battery_calibration", "battery_limiter",
                            "boot_animation_sound", "fan_speed", "lcd_override", "usb_charging"):
                if os.path.exists(os.path.join(self.base_path, feature)):
                    available.add(feature)

        if self.has_four_zone_kb:
            for feature in ("per_zone_mode", "four_zone_mode"):
                if os.path.exists(_acer_wmi_path("four_zoned_kb", feature)):
                    available.add(feature)

        # Newer Acer keyboards can require direct ENEK5130 HID feature reports.
        if self.enek5130_hid_device:
            available.add("enek5130_hid_rgb")

        return available

    def get_driver_version(self) -> str:
        """Get Driver version"""
        if not self.base_path:
            return "Unknown Version"
        version_file = os.path.join(self.base_path, "version")
        if not os.path.isfile(version_file):
            return "Unknown Version"
        try:
            with open(version_file, "r") as f:
                return f.read().strip() or "Unknown Version"
        except OSError:
            return "Unknown Version"

    # ------------------------------------------------------------ driver recovery
    def _auto_recover_unknown(self):
        """Reload linuwu_sense at most once per boot when the model is not detected."""
        if not self.driver_loaded:
            log.error("linuwu_sense module is not loaded. Please install the linuwu_sense driver; "
                      "not attempting an automatic reload.")
            return

        attempts = self._get_restart_attempts()
        if attempts >= self.MAX_RESTART_ATTEMPTS:
            log.warning(f"Automatic driver reload already attempted this boot ({attempts}); not retrying")
            return

        recorded = self._increment_restart_attempts()
        if recorded is None:
            log.error("Cannot record the driver reload attempt; skipping it to avoid reload loops")
            return

        log.warning(f"Unknown laptop type detected, reloading linuwu_sense once "
                    f"(attempt {recorded}/{self.MAX_RESTART_ATTEMPTS} this boot)")
        if self._reload_module():
            self._wait_for_driver()
        self._detect_hardware()
        if self.laptop_type == LaptopType.UNKNOWN:
            log.error("Laptop type still unknown after driver reload")
        else:
            log.info(f"Driver reload recovered laptop type {self.laptop_type.name}")

    def _counter_path(self) -> str:
        return self.RESTART_COUNTER_FILE

    def _ensure_runtime_dir(self) -> str:
        d = os.path.dirname(self._counter_path())
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(errno.ENOTDIR, f"{d} is not a directory")
        if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) & 0o022:
            os.chmod(d, 0o700)
        return d

    def _get_restart_attempts(self) -> int:
        """Get current restart attempt count (never follows symlinks)."""
        path = self._counter_path()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return 0
        except OSError as e:
            log.warning(f"Ignoring unusable restart counter {path}: {_describe_oserror(e)}")
            return 0
        try:
            data = os.read(fd, 64)
        finally:
            os.close(fd)
        try:
            return max(0, int(data.decode().strip()))
        except (ValueError, UnicodeDecodeError):
            return 0

    def _increment_restart_attempts(self) -> Optional[int]:
        """Increment the restart counter; returns the new value or None on failure."""
        attempts = self._get_restart_attempts() + 1
        path = self._counter_path()
        try:
            self._ensure_runtime_dir()
            if os.path.islink(path):
                os.unlink(path)  # removes the link itself, never its target
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, str(attempts).encode())
            finally:
                os.close(fd)
            return attempts
        except OSError as e:
            log.error(f"Failed to write restart counter {path}: {_describe_oserror(e)}")
            return None

    def _reload_module(self, module_args: Tuple[str, ...] = ()) -> bool:
        """rmmod + modprobe linuwu_sense (the daemon runs as root: no sudo)."""
        rmmod = _tool_path("rmmod", "/usr/sbin/rmmod")
        modprobe = _tool_path("modprobe", "/usr/sbin/modprobe")
        try:
            if os.path.exists(_module_sysfs_path()):
                subprocess.run([rmmod, MODULE_NAME], check=True, capture_output=True, text=True, timeout=60)
                log.info("Removed linuwu_sense module")
            subprocess.run([modprobe, MODULE_NAME, *module_args], check=True,
                           capture_output=True, text=True, timeout=60)
            log.info(f"Loaded linuwu_sense module {' '.join(module_args)}".rstrip())
            return True
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
            log.error(f"Driver reload failed: {e.cmd} exited with {e.returncode} {detail}".rstrip())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error(f"Driver reload failed: {e}")
        return False

    def _wait_for_driver(self):
        for _ in range(self.DRIVER_SETTLE_POLLS):
            if self._detect_laptop_type() != LaptopType.UNKNOWN:
                return
            time.sleep(self.DRIVER_SETTLE_INTERVAL)

    def reload_driver_and_redetect(self, module_args: Tuple[str, ...] = (), restart_daemon: bool = False) -> bool:
        """Reload the module, re-detect features and optionally restart the service.

        Runs in a background thread after the client already got its reply.
        """
        ok = self._reload_module(tuple(module_args))
        if ok:
            self._wait_for_driver()
        self._detect_hardware()
        log.info(f"Re-detected laptop type {self.laptop_type.name}; "
                 f"features: {', '.join(sorted(self.available_features))}")
        if restart_daemon:
            ok = self.restart_daemon() and ok
        return ok

    def restart_daemon(self) -> bool:
        """Ask systemd to restart the service (non-blocking job)."""
        try:
            subprocess.run([_systemctl_path(), "--no-block", "restart", SERVICE_NAME],
                           check=True, capture_output=True, text=True, timeout=30)
            log.info("Requested restart of the DAMX daemon service")
            return True
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
            log.error(f"Failed to restart {SERVICE_NAME}: {e}")
            return False

    @property
    def restart_in_progress(self) -> bool:
        return self._task_lock.locked()

    def start_background_task(self, name: str, func: Callable, *args, **kwargs) -> bool:
        """Run a driver/daemon restart in a detached thread; one at a time."""
        if not self._task_lock.acquire(blocking=False):
            log.warning(f"Not starting {name}: another driver/daemon restart is in progress")
            return False

        def runner():
            try:
                func(*args, **kwargs)
            except Exception:
                log.error(f"Background task {name} failed:\n{traceback.format_exc()}")
            finally:
                self._task_lock.release()

        threading.Thread(target=runner, name=name, daemon=True).start()
        return True

    # ------------------------------------------------------------ modprobe parameter
    def _detect_current_modprobe_param(self) -> str:
        """Read the persistent modprobe parameter from disk."""
        try:
            with open(MODPROBE_CONFIG_PATH, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            return ""
        except OSError as e:
            log.error(f"Failed to read modprobe config: {e}")
            return ""
        for line in content.lower().splitlines():
            line = line.strip()
            if not line.startswith("options"):
                continue
            for param in VALID_MODPROBE_PARAMS:
                if param in line:
                    return param
        return ""

    @property
    def current_modprobe_param(self) -> str:
        return self._detect_current_modprobe_param()

    def get_modprobe_parameter(self) -> str:
        """Get current modprobe parameter (always re-read from disk)"""
        return self._detect_current_modprobe_param()

    @staticmethod
    def _atomic_write(path: str, content: str, mode: int = 0o644):
        """Write a file atomically: temp file in the same dir + fsync + os.replace."""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def set_modprobe_parameter(self, param: str) -> bool:
        """Persist a modprobe parameter.  Applying it (module reload) is up to the caller."""
        if not isinstance(param, str) or param not in VALID_MODPROBE_PARAMS + ("",):
            log.error(f"Invalid modprobe parameter: {_short(param)}")
            self._set_last_error("invalid modprobe parameter")
            return False
        if param == "":
            return self.remove_modprobe_parameter()

        with self.lock:
            try:
                self._atomic_write(MODPROBE_CONFIG_PATH, f"options {MODULE_NAME} {param}=1\n")
            except OSError as e:
                self._set_last_error(_describe_oserror(e))
                log.error(f"Failed to write {MODPROBE_CONFIG_PATH}: {e}")
                return False

        if self._detect_current_modprobe_param() != param:
            self._set_last_error("verification failed after write")
            log.error(f"Verification of modprobe parameter {param} failed")
            return False
        log.info(f"Persisted modprobe parameter: {param}")
        return True

    def remove_modprobe_parameter(self) -> bool:
        """Remove modprobe parameter config file"""
        with self.lock:
            try:
                os.unlink(MODPROBE_CONFIG_PATH)
                log.info("Removed modprobe parameter config")
            except FileNotFoundError:
                pass
            except OSError as e:
                self._set_last_error(_describe_oserror(e))
                log.error(f"Failed to remove modprobe parameter: {e}")
                return False
        return self._detect_current_modprobe_param() == ""

    # ------------------------------------------------------------ sysfs I/O
    def _read_file(self, path: str) -> str:
        """Read from a VFS file"""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception as e:
            log.error(f"Failed to read from {path}: {e}")
            return ""

    def _write_file(self, path: str, value: str) -> bool:
        """Write ``value`` + "\\n" to a VFS file in a single write() call."""
        data = (str(value).strip() + "\n").encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
            try:
                written = os.write(fd, data)
            finally:
                os.close(fd)
            if written != len(data):
                raise OSError(errno.EIO, f"short write ({written}/{len(data)} bytes)")
            self._set_last_error(None)
            return True
        except OSError as e:
            self._set_last_error(_describe_oserror(e))
            log.error(f"Failed to write {str(value).strip()!r} to {path}: {_describe_oserror(e)}")
            return False

    def _set_toggle(self, feature: str, enabled) -> bool:
        if feature not in self.available_features:
            self._set_last_error(f"{feature} is not supported on this device")
            return False
        value = _coerce_bool(enabled)
        if value is None:
            self._set_last_error(f"invalid value {_short(enabled)} for {feature}: expected true/false")
            log.error(f"Invalid boolean for {feature}: {_short(enabled)}")
            return False
        return self._write_file(os.path.join(self.base_path, feature), "1" if value else "0")

    # ------------------------------------------------------------ ENEK5130 HID RGB
    def _find_enek5130_hid_device(self) -> str:
        """Find the ENEK5130 RGB hidraw device by stable sysfs identity."""
        candidates = self._find_enek5130_hid_candidates()
        return candidates[0] if candidates else ""

    def _find_enek5130_hid_candidates(self) -> List[str]:
        """Find all ENEK5130 hidraw candidates by stable sysfs identity."""
        candidates = []
        for dev in sorted(glob.glob('/dev/hidraw*')):
            uevent_path = f"/sys/class/hidraw/{os.path.basename(dev)}/device/uevent"
            try:
                with open(uevent_path, 'r', encoding='utf-8') as f:
                    uevent = f.read()
            except OSError:
                continue

            if ENEK5130_HID_ID in uevent or ENEK5130_HID_NAME in uevent:
                candidates.append(dev)

        return candidates

    def _set_enek5130_hid_report(self, effect: int, red: int, green: int, blue: int,
                                  brightness: int, speed: int, direction: int,
                                  zone_mask: int) -> bool:
        """Send an ENEK5130 keyboard RGB HID feature report.

        Known packet format:
        a4 21 effect brightness speed direction rr gg bb zone_mask 00
        zone_mask 0x01/0x02/0x04/0x08 targets zones 1-4; 0x0f targets all zones.
        """
        candidates = self._find_enek5130_hid_candidates()
        if self.enek5130_hid_device and self.enek5130_hid_device not in candidates:
            candidates.insert(0, self.enek5130_hid_device)

        if not candidates:
            log.debug("ENEK5130 HID RGB device not found")
            return False

        if not all(0 <= value <= 255 for value in [effect, speed, direction]):
            log.error(f"Invalid ENEK5130 effect fields: effect={effect}, speed={speed}, direction={direction}")
            return False

        if not (0 <= brightness <= 100):
            log.error(f"Invalid ENEK5130 brightness. Must be between 0 and 100: {brightness}")
            return False

        if not all(0 <= color <= 255 for color in [red, green, blue]):
            log.error(f"Invalid ENEK5130 RGB values. Must be 0-255: {red},{green},{blue}")
            return False

        if not (0 <= zone_mask <= 0x0F):
            log.error(f"Invalid ENEK5130 zone mask. Must be 0x00-0x0f: {zone_mask:#x}")
            return False

        packet = bytes([
            0xA4, 0x21, effect, brightness,
            speed, direction,
            red, green, blue, zone_mask, 0x00
        ])

        for device in candidates:
            try:
                fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
                try:
                    buf = ctypes.create_string_buffer(packet, len(packet))
                    ret = fcntl.ioctl(fd, HIDIOCSFEATURE(len(packet)), buf, True)
                    log.info(
                        "Set ENEK5130 HID RGB via %s report=%s ret=%s",
                        device,
                        ' '.join(f'{b:02x}' for b in packet),
                        ret
                    )
                    self.enek5130_hid_device = device
                    return True
                finally:
                    os.close(fd)
            except Exception as e:
                log.warning(f"Failed ENEK5130 HID RGB candidate {device}: {e}")

        return False

    def _set_enek5130_zone_color(self, red: int, green: int, blue: int,
                                  brightness: int, zone_mask: int) -> bool:
        """Set a static color on one or more ENEK5130 keyboard zones."""
        return self._set_enek5130_hid_report(0x02, red, green, blue, brightness, 0x00, 0x00, zone_mask)

    def _set_enek5130_static_color(self, red: int, green: int, blue: int, brightness: int,
                                   speed: int = 0, direction: int = 0) -> bool:
        """Set static all-zone color using the Acer ENEK5130 HID feature report."""
        return self._set_enek5130_zone_color(red, green, blue, brightness, 0x0F)

    def _set_enek5130_per_zone_color(self, zones: List[str], brightness: int) -> bool:
        """Set four ENEK5130 zones using confirmed zone masks 0x01,0x02,0x04,0x08."""
        zone_masks = [0x01, 0x02, 0x04, 0x08]
        for zone, zone_mask in zip(zones, zone_masks):
            red = int(zone[0:2], 16)
            green = int(zone[2:4], 16)
            blue = int(zone[4:6], 16)
            if not self._set_enek5130_zone_color(red, green, blue, brightness, zone_mask):
                return False
        return True

    def _set_enek5130_effect(self, mode: int, speed: int, brightness: int,
                             direction: int, red: int, green: int, blue: int) -> bool:
        """Set a confirmed ENEK5130 dynamic lighting effect.

        DAMX UI modes:
        0 Static, 1 Breathing, 2 Neon, 3 Wave, 4 Shifting,
        5 Zoom, 6 Meteor, 7 Twinkling.

        Confirmed on Acer Nitro ANV16S-41 / ENEK5130:
        0x04 breathing, 0x05 neon-like, 0x07 wave/shifting,
        0x09 zoom, 0x0a snake/meteor-like, 0x0b random/twinkling-like.

        Unstable/unknown values are deliberately not mapped here:
        0x01/0x03 black/off, 0x06 short flash/returns to previous,
        0x08 glitch, 0x0c freezes previous lighting effect.
        """
        effect_map = {
            1: 0x04,  # Breathing Mode
            2: 0x05,  # Neon Mode
            3: 0x07,  # Wave Mode
            4: 0x07,  # Shifting Mode (same confirmed native wave/shifting effect)
            5: 0x09,  # Zoom Mode
            6: 0x0A,  # Meteor Mode (snake-like on ENEK5130)
            7: 0x0B,  # Twinkling Mode (random on ENEK5130)
        }

        effect = effect_map.get(mode)
        if effect is None:
            return False

        # DAMX currently exposes speed as 0-9. Although ENEK5130 accepts larger
        # speed bytes for breathing/neon, direct UI values match the app's
        # expected speed feel better than scaling 0-9 to 0-100.
        if effect in [0x04, 0x05]:
            hid_speed = speed
        else:
            hid_speed = min(10, max(1, speed + 1))

        return self._set_enek5130_hid_report(
            effect, red, green, blue, brightness, hid_speed, direction, 0x0F
        )

    # ------------------------------------------------------------ thermal profile
    def get_thermal_profile(self) -> str:
        """Get current thermal profile"""
        if "thermal_profile" not in self.available_features:
            return ""
        return self._read_file(_platform_profile_path())

    def set_thermal_profile(self, profile: str) -> bool:
        """Set thermal profile (only values offered by platform_profile_choices)."""
        if "thermal_profile" not in self.available_features:
            self._set_last_error("thermal profile is not supported on this device")
            return False

        available_profiles = self.get_thermal_profile_choices()
        if not isinstance(profile, str) or profile not in available_profiles:
            self._set_last_error(f"invalid thermal profile {_short(profile)}; "
                                 f"available: {', '.join(available_profiles) or 'none'}")
            log.error(f"Invalid thermal profile: {_short(profile)}. Available profiles: {available_profiles}")
            return False

        with self.lock:
            ok = self._write_file(_platform_profile_path(), profile)
        if ok:
            self._after_profile_change(profile)
        return ok

    def _after_profile_change(self, profile: str):
        """Every successful profile change (user, restore, battery logic, ppd) ends here."""
        try:
            self.ppd_sync.on_acer_profile(profile)
        except Exception:
            log.error(f"ppd sync after profile change failed:\n{traceback.format_exc()}")

    def user_set_thermal_profile(self, profile: str) -> bool:
        """The user path: set the profile and persist it for the current power source."""
        if not self.set_thermal_profile(profile):
            return False
        self.persist_source(thermal_profile=profile)
        return True

    def apply_ppd_for_power_change(self, is_ac: bool) -> bool:
        """No saved profile for the new source: follow ppd's current profile (if synced)."""
        return self.ppd_sync.apply_for_power_change(is_ac)

    def get_thermal_profile_choices(self) -> List[str]:
        """Get available thermal profiles (as advertised by the kernel)"""
        if "thermal_profile" not in self.available_features:
            return []
        choices = self._read_file(_platform_profile_choices_path())
        return choices.split() if choices else []

    # ------------------------------------------------------------ toggles
    def get_backlight_timeout(self) -> str:
        if "backlight_timeout" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "backlight_timeout"))

    def set_backlight_timeout(self, enabled: bool) -> bool:
        return self._set_toggle("backlight_timeout", enabled)

    def get_battery_calibration(self) -> str:
        if "battery_calibration" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "battery_calibration"))

    def set_battery_calibration(self, enabled: bool) -> bool:
        return self._set_toggle("battery_calibration", enabled)

    def get_battery_limiter(self) -> str:
        if "battery_limiter" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "battery_limiter"))

    def set_battery_limiter(self, enabled: bool) -> bool:
        return self._set_toggle("battery_limiter", enabled)

    def get_boot_animation_sound(self) -> str:
        if "boot_animation_sound" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "boot_animation_sound"))

    def set_boot_animation_sound(self, enabled: bool) -> bool:
        return self._set_toggle("boot_animation_sound", enabled)

    def get_lcd_override(self) -> str:
        if "lcd_override" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "lcd_override"))

    def set_lcd_override(self, enabled: bool) -> bool:
        return self._set_toggle("lcd_override", enabled)

    # ------------------------------------------------------------ fans
    def _read_fan_values(self) -> Optional[Tuple[str, str]]:
        try:
            with open(os.path.join(self.base_path, "fan_speed"), 'r') as f:
                speeds = f.read().strip()
        except OSError as e:
            log.error(f"Error reading fan speed: {e}")
            return None
        if "," not in speeds:
            return None
        cpu, gpu = speeds.split(",", 1)
        return cpu.strip(), gpu.strip()

    def get_fan_speed(self) -> Tuple[str, str]:
        """Get CPU and GPU fan speeds"""
        if "fan_speed" not in self.available_features:
            return ("", "")
        return self._read_fan_values() or ("0", "0")

    def get_fan_mode(self) -> str:
        """Current fan mode: curve (engine running), else from the driver: auto, max,
        manual, unknown; '' when fan control is unavailable."""
        if "fan_speed" not in self.available_features:
            return ""
        if self.curve_engine.active:
            return "curve"
        values = self._read_fan_values()
        if values is None:
            return "unknown"
        try:
            cpu, gpu = int(values[0]), int(values[1])
        except ValueError:
            return "unknown"
        if cpu == 0 and gpu == 0:
            return "auto"
        if cpu == 100 and gpu == 100:
            return "max"
        return "manual"

    def _write_fan(self, cpu: int, gpu: int) -> bool:
        with self.lock:
            return self._write_file(os.path.join(self.base_path, "fan_speed"), f"{cpu},{gpu}")

    def _engine_write(self, cpu: int, gpu: int, stop_event: threading.Event) -> bool:
        """Write used by the curve engine; refuses once the engine was stopped."""
        with self.lock:
            if stop_event.is_set():
                return False
            return self._write_fan(cpu, gpu)

    def set_fan_speed(self, cpu: int, gpu: int) -> bool:
        """Set CPU and GPU fan speeds (0 = auto, 1..100 = percent); stops the curve engine."""
        if "fan_speed" not in self.available_features:
            self._set_last_error("fan speed control is not supported on this device")
            return False
        if not (_is_int(cpu) and _is_int(gpu) and 0 <= cpu <= 100 and 0 <= gpu <= 100):
            self._set_last_error("fan speeds must be integers between 0 and 100")
            log.error(f"Invalid fan speeds. Values must be integers 0-100: cpu={_short(cpu)}, gpu={_short(gpu)}")
            return False
        with self._fan_mode_lock:
            self.curve_engine.stop()
            return self._write_fan(cpu, gpu)

    @staticmethod
    def fan_mode_for_speeds(cpu: int, gpu: int) -> str:
        """Legacy set_fan_speed mapping: 0,0 -> auto, 100,100 -> max, else manual."""
        if cpu == 0 and gpu == 0:
            return "auto"
        if cpu == 100 and gpu == 100:
            return "max"
        return "manual"

    def set_fan_mode(self, mode: str, cpu: Optional[int] = None, gpu: Optional[int] = None,
                     curve: Optional[Dict] = None) -> bool:
        """auto/max/manual stop the curve engine before writing; curve starts it
        (with ``curve`` or the saved curve).  Manual accepts 0..100 here so legacy
        values restored from state work; the socket API enforces 1..100."""
        if "fan_speed" not in self.available_features:
            self._set_last_error("fan speed control is not supported on this device")
            return False
        if mode not in FAN_MODES:
            self._set_last_error(f"invalid fan mode {_short(mode)}")
            return False
        with self._fan_mode_lock:
            if mode == "curve":
                try:
                    self.curve_engine.start(curve if curve is not None else self.state.curve())
                except ValueError as e:
                    self._set_last_error(f"invalid fan curve: {e}")
                    return False
                return True
            if mode == "auto":
                cpu, gpu = 0, 0
            elif mode == "max":
                cpu, gpu = 100, 100
            elif not (_is_int(cpu) and _is_int(gpu) and 0 <= cpu <= 100 and 0 <= gpu <= 100):
                self._set_last_error("manual fan speeds must be integers between 1 and 100")
                return False
            self.curve_engine.stop()
            return self._write_fan(cpu, gpu)

    def shutdown_fans(self):
        """On daemon shutdown: stop the engine and hand the fans back to firmware."""
        with self._fan_mode_lock:
            self.curve_engine.stop(write_auto=True)

    # ------------------------------------------------------------ persistence / restore
    def power_source(self) -> str:
        """'ac' or 'battery' (debounced state of the power monitor if available)."""
        pm = self.power_monitor
        if pm is not None and pm.current_source is not None:
            return "ac" if pm.current_source else "battery"
        return "battery" if read_external_power() is False else "ac"

    def persist_source(self, **fields):
        self.state.update_source(self.power_source(), **fields)

    def persist_fan_mode(self, mode: str, cpu: Optional[int] = None, gpu: Optional[int] = None):
        fields = {"fan_mode": mode}
        if mode == "manual":
            fields["fan_manual"] = [cpu, gpu]
        self.persist_source(**fields)

    def _restore_fan(self, entry: Dict):
        mode = entry.get("fan_mode")
        if not mode or "fan_speed" not in self.available_features:
            return
        if mode == "manual":
            manual = entry.get("fan_manual")
            if not manual:
                log.warning("Saved manual fan mode has no speeds; skipping")
                return
            ok = self.set_fan_mode("manual", manual[0], manual[1])
        else:
            ok = self.set_fan_mode(mode)
            if ok and mode == "curve":
                self.curve_engine.request_reassert()
        log.info(f"Restored fan mode '{mode}'" if ok else f"Could not restore fan mode '{mode}': {self.last_error}")

    def restore_power_source(self, is_ac: bool, reason: str = "change") -> bool:
        """Apply the saved profile + fan mode of a power source.

        Returns True when a saved thermal profile was applied (the automatic
        battery logic is then skipped).
        """
        if not self.state.restore_on_start:
            return False
        src = "ac" if is_ac else "battery"
        if reason == "initial" and self._startup_restore is not None:
            started_src, started_applied = self._startup_restore
            self._startup_restore = None
            if started_src == src:
                return started_applied  # already restored at daemon start
        entry = self.state.source(src)
        applied = False
        profile = entry.get("thermal_profile")
        if profile and "thermal_profile" in self.available_features:
            choices = self.get_thermal_profile_choices()
            if profile not in choices:
                log.info(f"Saved {src} thermal profile '{profile}' is not offered by the kernel "
                         f"({', '.join(choices) or 'none'}); skipping")
            elif self.set_thermal_profile(profile):
                log.info(f"Restored {src} thermal profile '{profile}' ({reason})")
                applied = True
            else:
                log.warning(f"Could not restore {src} thermal profile '{profile}': {self.last_error}")
        self._restore_fan(entry)
        return applied

    def restore_state(self, reason: str = "start") -> bool:
        """Full restore at start and after resume: settings + current source."""
        if not self.state.restore_on_start:
            log.info("Settings restore disabled (restore_on_start=false)")
            return False
        settings = self.state.settings()
        for name in ("battery_limiter", "backlight_timeout"):
            value = settings.get(name)
            if value is not None and name in self.available_features:
                if not getattr(self, "set_" + name)(value):
                    log.warning(f"Could not restore {name}: {self.last_error}")
        level = settings.get("usb_charging")
        if level is not None and "usb_charging" in self.available_features:
            if not self.set_usb_charging(level):
                log.warning(f"Could not restore usb_charging: {self.last_error}")
        src = self.power_source()
        applied = self.restore_power_source(src == "ac", reason)
        if reason == "start":
            self._startup_restore = (src, applied)
        return applied

    # ------------------------------------------------------------ usb charging
    def get_usb_charging(self) -> str:
        if "usb_charging" not in self.available_features:
            return ""
        return self._read_file(os.path.join(self.base_path, "usb_charging"))

    def set_usb_charging(self, level: int) -> bool:
        """Set USB charging level (0, 10, 20, 30)"""
        if "usb_charging" not in self.available_features:
            return False
        if not _is_int(level) or level not in (0, 10, 20, 30):
            self._set_last_error("USB charging level must be 0, 10, 20 or 30")
            log.error(f"Invalid USB charging level. Must be 0, 10, 20, or 30: {_short(level)}")
            return False
        return self._write_file(os.path.join(self.base_path, "usb_charging"), str(level))

    # ------------------------------------------------------------ keyboard
    def get_per_zone_mode(self) -> str:
        if "per_zone_mode" not in self.available_features:
            return ""
        return self._read_file(_acer_wmi_path("four_zoned_kb", "per_zone_mode"))

    def set_per_zone_mode(self, zone1: str, zone2: str, zone3: str, zone4: str, brightness: int) -> bool:
        """Set per-zone mode: zone1-zone4 are RRGGBB hex strings, brightness 0-100."""
        if "per_zone_mode" not in self.available_features:
            return False

        zones = [zone1, zone2, zone3, zone4]
        for i, zone in enumerate(zones, 1):
            if not _is_hex_color(zone):
                self._set_last_error(f"zone{i} must be a 6-digit hex color (RRGGBB)")
                log.error(f"Invalid hex color for zone {i}: {_short(zone)}")
                return False

        if not _is_int(brightness) or not (0 <= brightness <= 100):
            self._set_last_error("brightness must be an integer between 0 and 100")
            log.error(f"Invalid brightness. Must be between 0 and 100: {_short(brightness)}")
            return False

        with self.lock:
            if self.enek5130_hid_device:
                try:
                    if self._set_enek5130_per_zone_color(zones, brightness):
                        return True
                except Exception as e:
                    log.warning(f"ENEK5130 per-zone update failed, falling back to sysfs: {e}")
            return self._write_file(_acer_wmi_path("four_zoned_kb", "per_zone_mode"),
                                    f"{zone1},{zone2},{zone3},{zone4},{brightness}")

    def get_four_zone_mode(self) -> str:
        if "four_zone_mode" not in self.available_features:
            return ""
        return self._read_file(_acer_wmi_path("four_zoned_kb", "four_zone_mode"))

    def set_four_zone_mode(self, mode: int, speed: int, brightness: int,
                           direction: int, red: int, green: int, blue: int) -> bool:
        """Set four-zone mode: mode 0-7, speed 0-9, brightness 0-100, direction 1-2, RGB 0-255."""
        if "four_zone_mode" not in self.available_features:
            return False

        checks = (("mode", mode, 0, 7), ("speed", speed, 0, 9), ("brightness", brightness, 0, 100),
                  ("direction", direction, 1, 2), ("red", red, 0, 255), ("green", green, 0, 255),
                  ("blue", blue, 0, 255))
        for name, value, lo, hi in checks:
            if not _is_int(value) or not (lo <= value <= hi):
                self._set_last_error(f"{name} must be an integer between {lo} and {hi}")
                log.error(f"Invalid {name}. Must be an integer between {lo} and {hi}: {_short(value)}")
                return False

        with self.lock:
            if self.enek5130_hid_device:
                try:
                    if mode == 0:
                        if self._set_enek5130_static_color(red, green, blue, brightness):
                            return True
                    elif self._set_enek5130_effect(mode, speed, brightness, direction, red, green, blue):
                        return True
                except Exception as e:
                    log.warning(f"ENEK5130 effect update failed, falling back to sysfs: {e}")
            return self._write_file(_acer_wmi_path("four_zoned_kb", "four_zone_mode"),
                                    f"{mode},{speed},{brightness},{direction},{red},{green},{blue}")

    # ------------------------------------------------------------ summary
    def get_status(self) -> Dict:
        return {
            "laptop_type": self.laptop_type.name,
            "driver_loaded": self.driver_loaded,
            "driver_status": self.driver_status,
            "status_message": self.status_message,
        }

    def get_all_settings(self) -> Dict:
        """Get all DAMX-Daemon settings as a dictionary"""
        settings = {
            "laptop_type": self.laptop_type.name,
            "has_four_zone_kb": self.has_four_zone_kb,
            "available_features": sorted(self.available_features),
            "version": VERSION,
            "driver_version": self.get_driver_version(),
            "driver_loaded": self.driver_loaded,
            "driver_status": self.driver_status,
            "status_message": self.status_message,
            "modprobe_parameter": self.get_modprobe_parameter(),
            "thermal_profile": {
                "current": self.get_thermal_profile(),
                "available": self.get_thermal_profile_choices(),
            },
        }

        for feature in ("backlight_timeout", "battery_calibration", "battery_limiter",
                        "boot_animation_sound", "lcd_override", "usb_charging",
                        "per_zone_mode", "four_zone_mode"):
            if feature in self.available_features:
                settings[feature] = getattr(self, "get_" + feature)()

        if "fan_speed" in self.available_features:
            cpu_fan, gpu_fan = self.get_fan_speed()
            settings["fan_speed"] = {"cpu": cpu_fan, "gpu": gpu_fan}
            settings["fan_mode"] = self.get_fan_mode()

        settings["fan_curve_active"] = self.curve_engine.active
        settings["restore_on_start"] = self.state.restore_on_start
        settings["power_source"] = self.power_source()
        settings["ppd_sync"] = {"available": self.ppd_sync.available, "enabled": self.ppd_sync.enabled,
                                "active_profile": self.ppd_sync.active_profile if self.ppd_sync.available else None}
        settings["gpu_power"] = self.gpu_power.summary()
        return settings


class RequestError(Exception):
    """A request that cannot be served; the message is returned to the client."""


class PeerInfo(NamedTuple):
    pid: int
    uid: int
    gid: int
    privileged: bool


def _ok(data=None, message: Optional[str] = None) -> Dict:
    response = {"success": True, "data": data}
    if message:
        response["message"] = message
    return response


def _err(message: str) -> Dict:
    return {"success": False, "error": message}


def _require(params: Dict, name: str):
    if name not in params:
        raise RequestError(f"missing required parameter '{name}'")
    return params[name]


def _bool_param(params: Dict, name: str) -> bool:
    value = _coerce_bool(_require(params, name))
    if value is None:
        raise RequestError(f"parameter '{name}' must be a boolean (true/false)")
    return value


def _int_param(params: Dict, name: str, lo: int, hi: int) -> int:
    value = _require(params, name)
    if not _is_int(value) or not (lo <= value <= hi):
        raise RequestError(f"parameter '{name}' must be an integer between {lo} and {hi}")
    return value


def _hex_param(params: Dict, name: str) -> str:
    value = _require(params, name)
    if not _is_hex_color(value):
        raise RequestError(f"parameter '{name}' must be a 6-digit hex color (RRGGBB)")
    return value


def _str_param(params: Dict, name: str) -> str:
    value = _require(params, name)
    if not isinstance(value, str) or not value:
        raise RequestError(f"parameter '{name}' must be a non-empty string")
    return value


class DaemonServer:
    """Unix Socket server for IPC with the GUI client"""

    def __init__(self, manager: DAMXManager, max_clients: int = MAX_CLIENTS,
                 idle_timeout: float = CLIENT_IDLE_TIMEOUT):
        self.manager = manager
        self.socket = None
        self.running = False
        self.max_clients = max_clients
        self.idle_timeout = idle_timeout
        self.clients: List[Tuple[socket.socket, threading.Thread]] = []
        self._clients_lock = threading.Lock()
        self._socket_ino = None

        toggles = {
            "set_backlight_timeout": ("backlight_timeout", "Backlight timeout"),
            "set_battery_calibration": ("battery_calibration", "Battery calibration"),
            "set_battery_limiter": ("battery_limiter", "Battery limiter"),
            "set_boot_animation_sound": ("boot_animation_sound", "Boot animation sound"),
            "set_lcd_override": ("lcd_override", "LCD override"),
        }
        self._commands: Dict[str, Callable[[Dict, List[Callable]], Dict]] = {
            "ping": self._cmd_ping,
            "get_version": self._cmd_get_version,
            "get_all_settings": self._cmd_get_all_settings,
            "get_supported_features": self._cmd_get_supported_features,
            "get_thermal_profile": self._cmd_get_thermal_profile,
            "set_thermal_profile": self._cmd_set_thermal_profile,
            "set_fan_speed": self._cmd_set_fan_speed,
            "set_usb_charging": self._cmd_set_usb_charging,
            "set_per_zone_mode": self._cmd_set_per_zone_mode,
            "set_four_zone_mode": self._cmd_set_four_zone_mode,
            "get_modprobe_parameter": self._cmd_get_modprobe_parameter,
            "set_fan_mode": self._cmd_set_fan_mode,
            "get_fan_curve": self._cmd_get_fan_curve,
            "set_fan_curve": self._cmd_set_fan_curve,
            "get_persistence": self._cmd_get_persistence,
            "set_persistence": self._cmd_set_persistence,
            "reset_persistence": self._cmd_reset_persistence,
            "get_gpu_power_info": self._cmd_get_gpu_power_info,
            "set_nvidia_powerd": self._cmd_set_nvidia_powerd,
            "get_ppd_sync": self._cmd_get_ppd_sync,
            "set_ppd_sync": self._cmd_set_ppd_sync,
            "force_nitro_model": self._make_force("nitro_v4"),
            "force_predator_model": self._make_force("predator_v4"),
            "force_enable_all": self._make_force("enable_all"),
            "set_modprobe_parameter_predator": self._make_set_modprobe("predator_v4"),
            "set_modprobe_parameter_nitro": self._make_set_modprobe("nitro_v4"),
            "set_modprobe_parameter_enable_all": self._make_set_modprobe("enable_all"),
            "remove_modprobe_parameter": self._cmd_remove_modprobe_parameter,
            "restart_daemon": self._cmd_restart_daemon,
            "restart_drivers_and_daemon": self._cmd_restart_drivers_and_daemon,
        }
        for command, (feature, label) in toggles.items():
            self._commands[command] = self._make_toggle(feature, label)

    # ------------------------------------------------------------ socket setup
    def _prepare_socket_path(self):
        """Remove a stale socket; refuse to start if another daemon answers on it."""
        if not os.path.lexists(SOCKET_PATH):
            return
        st = os.lstat(SOCKET_PATH)
        if stat.S_ISSOCK(st.st_mode):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(SOCKET_PATH)
            except OSError:
                pass  # nobody listening: stale
            else:
                raise RuntimeError(f"another DAMX daemon is already listening on {SOCKET_PATH}")
            finally:
                probe.close()
        log.info(f"Removing stale socket file {SOCKET_PATH}")
        os.unlink(SOCKET_PATH)

    def _apply_socket_permissions(self):
        try:
            gid = grp.getgrnam(DAMX_GROUP).gr_gid
        except KeyError:
            gid = None
        if gid is not None:
            os.chown(SOCKET_PATH, 0, gid)
            os.chmod(SOCKET_PATH, 0o660)
            log.info(f"Socket {SOCKET_PATH} is root:{DAMX_GROUP} 0660")
        else:
            os.chmod(SOCKET_PATH, 0o666)
            log.warning(f"Group '{DAMX_GROUP}' does not exist: socket {SOCKET_PATH} is world-accessible (0666). "
                        f"Privileged commands still require root or membership in {', '.join(PRIVILEGED_GROUPS)}.")

    def bind(self) -> bool:
        """Create, bind and listen on the socket. Returns False on failure."""
        try:
            self._prepare_socket_path()
        except (RuntimeError, OSError) as e:
            log.error(f"Cannot use socket path {SOCKET_PATH}: {e}")
            return False

        old_umask = os.umask(0o177)  # socket is created 0600, permissions fixed below
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(SOCKET_PATH)
        except OSError as e:
            sock.close()
            log.error(f"Failed to bind {SOCKET_PATH}: {e}")
            return False
        finally:
            os.umask(old_umask)

        self.socket = sock
        try:
            self._socket_ino = os.lstat(SOCKET_PATH).st_ino
            self._apply_socket_permissions()
            sock.listen(self.max_clients)
            sock.settimeout(1.0)  # periodic wake-up for clean shutdown
        except OSError as e:
            log.error(f"Failed to set up socket {SOCKET_PATH}: {e}")
            self.stop()
            return False
        self.running = True
        return True

    def serve(self) -> bool:
        """Accept connections until stop() is called."""
        log.info(f"Server listening on {SOCKET_PATH}")
        while self.running:
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if not self.running or e.errno in (errno.EBADF, errno.EINVAL):
                    break
                log.error(f"Error accepting connection: {e}")
                continue
            self._accept_client(client)
        return True

    def start(self) -> bool:
        """Bind and serve (blocking)."""
        if not self.bind():
            return False
        return self.serve()

    # ------------------------------------------------------------ clients
    def _prune_clients_locked(self):
        self.clients = [(c, t) for (c, t) in self.clients if t.ident is None or t.is_alive()]

    def _accept_client(self, client: socket.socket):
        client.settimeout(self.idle_timeout)
        with self._clients_lock:
            self._prune_clients_locked()
            full = len(self.clients) >= self.max_clients
            if not full:
                thread = threading.Thread(target=self.handle_client, args=(client,),
                                          name="DAMX-Client", daemon=True)
                self.clients.append((client, thread))
        if full:
            log.warning(f"Rejecting client: {self.max_clients} clients already connected")
            try:
                client.sendall(self._encode(_err("too many clients connected")))
            except OSError:
                pass
            client.close()
            return
        thread.start()

    def _remove_client(self, client: socket.socket):
        with self._clients_lock:
            self.clients = [(c, t) for (c, t) in self.clients if c is not client]

    def stop(self):
        """Stop the server and clean up"""
        log.info("Stopping server...")
        self.running = False

        with self._clients_lock:
            clients = list(self.clients)
        for client, _ in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)  # wakes a blocked accept()
            except OSError:
                pass
            try:
                self.socket.close()
            except OSError:
                pass

        self.cleanup_socket()

    def cleanup_socket(self):
        """Remove the socket file, but only if it is the one we created."""
        if self._socket_ino is None:
            return
        try:
            st = os.lstat(SOCKET_PATH)
            if st.st_ino == self._socket_ino and stat.S_ISSOCK(st.st_mode):
                os.unlink(SOCKET_PATH)
                log.info(f"Removed socket file: {SOCKET_PATH}")
        except FileNotFoundError:
            pass
        except OSError as e:
            log.error(f"Failed to remove socket file: {e}")
        self._socket_ino = None

    def _peer_info(self, client_socket: socket.socket) -> PeerInfo:
        try:
            pid, uid, gid = get_peer_credentials(client_socket)
        except OSError as e:
            log.warning(f"SO_PEERCRED failed: {e}; treating client as unprivileged")
            return PeerInfo(-1, -1, -1, False)
        try:
            privileged = is_privileged_user(uid, gid)
        except Exception as e:
            log.warning(f"Privilege check for uid {uid} failed: {e}")
            privileged = False
        return PeerInfo(pid, uid, gid, privileged)

    @staticmethod
    def _encode(response: Dict) -> bytes:
        # No indent: a response never contains a raw newline.
        return (json.dumps(response) + "\n").encode("utf-8")

    def _send(self, client_socket: socket.socket, response: Dict):
        client_socket.sendall(self._encode(response))

    def handle_client(self, client_socket: socket.socket):
        """Handle communication with a client (newline-delimited JSON)."""
        peer = self._peer_info(client_socket)
        buf = b""
        try:
            while self.running:
                client_socket.settimeout(PARTIAL_MESSAGE_TIMEOUT if buf else self.idle_timeout)
                try:
                    data = client_socket.recv(65536)
                except socket.timeout:
                    if buf:
                        log.warning("Discarding incomplete or invalid message")
                        self._send(client_socket, _err("Invalid JSON format"))
                        buf = b""
                        continue
                    log.debug("Closing idle client connection")
                    break
                if not data:
                    break
                buf += data
                buf, keep_open = self._process_buffer(client_socket, buf, peer)
                if not keep_open:
                    break
        except OSError as e:
            if self.running:
                log.debug(f"Client connection error: {e}")
        finally:
            self._remove_client(client_socket)
            try:
                client_socket.close()
            except OSError:
                pass

    def _process_buffer(self, client_socket, buf: bytes, peer: PeerInfo) -> Tuple[bytes, bool]:
        """Handle every complete message in ``buf``; return (remainder, keep_open)."""
        while True:
            newline = buf.find(b"\n")
            if newline < 0:
                break
            line, buf = buf[:newline], buf[newline + 1:]
            if len(line) > MAX_MESSAGE_SIZE:
                self._send(client_socket, _err(f"message too large (limit {MAX_MESSAGE_SIZE} bytes)"))
                return b"", False
            if line.strip():
                self._handle_line(client_socket, line, peer)

        if not buf:
            return buf, True
        if len(buf) > MAX_MESSAGE_SIZE:
            log.warning("Client exceeded the maximum message size; closing connection")
            self._send(client_socket, _err(f"message too large (limit {MAX_MESSAGE_SIZE} bytes)"))
            return b"", False
        # Legacy clients send JSON without a trailing newline.
        return self._process_legacy(client_socket, buf, peer), True

    def _process_legacy(self, client_socket, buf: bytes, peer: PeerInfo) -> bytes:
        try:
            text = buf.decode("utf-8")
        except UnicodeDecodeError:
            return buf  # maybe a split multibyte char; the partial timeout handles garbage
        decoder = json.JSONDecoder()
        idx, length = 0, len(text)
        while True:
            while idx < length and text[idx].isspace():
                idx += 1
            if idx >= length:
                return b""
            try:
                obj, idx = decoder.raw_decode(text, idx)
            except json.JSONDecodeError:
                return text[idx:].encode("utf-8")
            self._handle_message(client_socket, obj, peer)

    def _handle_line(self, client_socket, line: bytes, peer: PeerInfo):
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.error("Invalid JSON received")
            self._send(client_socket, _err("Invalid JSON format"))
            return
        self._handle_message(client_socket, obj, peer)

    def _handle_message(self, client_socket, obj, peer: PeerInfo):
        post_actions: List[Callable] = []
        if not isinstance(obj, dict):
            response = _err("Invalid request: expected a JSON object")
        else:
            command = obj.get("command")
            params = obj.get("params", {})
            if not isinstance(command, str) or not command:
                response = _err("Invalid request: 'command' must be a non-empty string")
            else:
                response = self.process_command(command, params, peer=peer, post_actions=post_actions)
        try:
            self._send(client_socket, response)
        finally:
            # Restarts etc. run only after the reply has been sent.
            self._run_post_actions(post_actions)

    @staticmethod
    def _run_post_actions(actions: List[Callable]):
        for action in actions:
            try:
                action()
            except Exception:
                log.error(f"Deferred action failed:\n{traceback.format_exc()}")

    # ------------------------------------------------------------ dispatch
    def process_command(self, command: str, params: Dict, peer: Optional[PeerInfo] = None,
                        post_actions: Optional[List[Callable]] = None) -> Dict:
        """Process a command.  ``peer`` None means an in-process (trusted) caller.

        Actions appended to ``post_actions`` must run after the response has been
        sent; when the caller passes none they run before returning.
        """
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _err("Invalid request: 'params' must be a JSON object")

        if peer is not None and is_privileged_command(command) and not peer.privileged:
            log.warning(f"Denied privileged command {command} from uid={peer.uid} pid={peer.pid}")
            return _err(PERMISSION_DENIED_ERROR)

        handler = self._commands.get(command)
        if handler is None:
            return _err(f"Unknown command: {command[:64]}")

        log.debug(f"Processing command: {command} with params: {params}")
        actions: List[Callable] = [] if post_actions is None else post_actions
        try:
            response = handler(params, actions)
        except RequestError as e:
            return _err(str(e))
        except Exception as e:
            log.error(f"Error processing command {command}: {e}")
            log.error(traceback.format_exc())
            return _err(str(e))
        if post_actions is None:
            self._run_post_actions(actions)
        return response

    def _require_feature(self, feature: str, label: str):
        if feature not in self.manager.available_features:
            raise RequestError(f"{label} is not supported on this device")

    def _failure(self, message: str) -> Dict:
        detail = self.manager.last_error
        return _err(f"{message}: {detail}" if detail else message)

    def _cmd_ping(self, params, post):
        return _ok({"pong": True, "version": VERSION})

    def _cmd_get_version(self, params, post):
        return _ok({"version": VERSION})

    def _cmd_get_all_settings(self, params, post):
        return _ok(self.manager.get_all_settings())

    def _cmd_get_supported_features(self, params, post):
        data = {
            "available_features": sorted(self.manager.available_features),
            "has_four_zone_kb": self.manager.has_four_zone_kb,
        }
        data.update(self.manager.get_status())
        return _ok(data)

    def _cmd_get_thermal_profile(self, params, post):
        self._require_feature("thermal_profile", "Thermal profile")
        return _ok({"current": self.manager.get_thermal_profile(),
                    "available": self.manager.get_thermal_profile_choices()})

    def _cmd_set_thermal_profile(self, params, post):
        self._require_feature("thermal_profile", "Thermal profile")
        profile = _str_param(params, "profile")
        if self.manager.user_set_thermal_profile(profile):
            return _ok({"profile": profile})
        return self._failure(f"Failed to set thermal profile {_short(profile)}")

    def _make_toggle(self, feature: str, label: str):
        def handler(params, post):
            self._require_feature(feature, label)
            enabled = _bool_param(params, "enabled")
            if getattr(self.manager, "set_" + feature)(enabled):
                if feature in PERSISTED_TOGGLES:
                    self.manager.state.set_setting(feature, enabled)
                return _ok({"enabled": enabled})
            return self._failure(f"Failed to set {label.lower()}")
        return handler

    def _cmd_set_fan_speed(self, params, post):
        self._require_feature("fan_speed", "Fan speed control")
        cpu = _int_param(params, "cpu", 0, 100)
        gpu = _int_param(params, "gpu", 0, 100)
        mode = self.manager.fan_mode_for_speeds(cpu, gpu)
        if self.manager.set_fan_speed(cpu, gpu):
            self.manager.persist_fan_mode(mode, cpu, gpu)
            return _ok({"cpu": cpu, "gpu": gpu})
        return self._failure("Failed to set fan speed")

    def _cmd_set_fan_mode(self, params, post):
        self._require_feature("fan_speed", "Fan speed control")
        mode = _require(params, "mode")
        if mode not in FAN_MODES:
            raise RequestError("parameter 'mode' must be one of auto, max, manual, curve")
        cpu = gpu = None
        if mode == "manual":
            cpu = _int_param(params, "cpu", 1, 100)
            gpu = _int_param(params, "gpu", 1, 100)
        if not self.manager.set_fan_mode(mode, cpu, gpu):
            return self._failure(f"Failed to set fan mode {mode}")
        self.manager.persist_fan_mode(mode, cpu, gpu)
        return _ok({"fan_mode": mode})

    def _cmd_get_fan_curve(self, params, post):
        engine = self.manager.curve_engine
        return _ok({"curve": self.manager.state.curve(), "active": engine.active,
                    "presets": fan_curve_presets(), "last": dict(engine.last)})

    def _cmd_set_fan_curve(self, params, post):
        try:
            curve = validate_curve(_require(params, "curve"))
        except ValueError as e:
            raise RequestError(str(e))
        activate = False
        if "activate" in params:
            activate = _bool_param(params, "activate")
        if activate:
            self._require_feature("fan_speed", "Fan speed control")
        self.manager.state.set_curve(curve)
        if activate or self.manager.curve_engine.active:
            if not self.manager.set_fan_mode("curve", curve=curve):
                return self._failure("Failed to apply fan curve")
            if activate:
                self.manager.persist_fan_mode("curve")
        return _ok({"curve": curve, "active": self.manager.curve_engine.active})

    def _cmd_get_persistence(self, params, post):
        state = self.manager.state.snapshot()
        return _ok({"restore_on_start": state["restore_on_start"], "state": state,
                    "power_source": self.manager.power_source()})

    def _cmd_set_persistence(self, params, post):
        value = _bool_param(params, "restore_on_start")
        self.manager.state.set_restore_on_start(value)
        return _ok({"restore_on_start": value})

    def _cmd_reset_persistence(self, params, post):
        self.manager.state.reset()
        return _ok({"cleared": True})

    def _cmd_get_gpu_power_info(self, params, post):
        return _ok(self.manager.gpu_power.info())

    def _cmd_set_nvidia_powerd(self, params, post):
        enabled = _bool_param(params, "enabled")
        ok, error = self.manager.gpu_power.set_powerd(enabled)
        if not ok:
            return _err(error)
        return _ok({"powerd": self.manager.gpu_power.powerd_status(force=True)})

    def _cmd_get_ppd_sync(self, params, post):
        sync = self.manager.ppd_sync
        if sync.ensure_detected():
            value = sync.read_active()
            if value is not None:
                sync.active_profile = value
        return _ok(sync.status())

    def _cmd_set_ppd_sync(self, params, post):
        update = {k: params[k] for k in ("enabled", "to_acer", "to_ppd") if k in params}
        if "enabled" in update and not isinstance(update["enabled"], bool):
            raise RequestError("parameter 'enabled' must be a boolean (true/false)")
        try:
            mapping = merge_mapping(self.manager.state.ppd_sync(), update)
        except ValueError as e:
            raise RequestError(str(e))
        self.manager.state.set_ppd_sync(mapping)
        self.manager.ppd_sync.ensure_detected()
        return _ok(self.manager.ppd_sync.status())

    def _cmd_set_usb_charging(self, params, post):
        self._require_feature("usb_charging", "USB charging control")
        level = _require(params, "level")
        if not _is_int(level) or level not in (0, 10, 20, 30):
            raise RequestError("parameter 'level' must be one of 0, 10, 20, 30")
        if self.manager.set_usb_charging(level):
            self.manager.state.set_setting("usb_charging", level)
            return _ok({"level": level})
        return self._failure("Failed to set USB charging")

    def _cmd_set_per_zone_mode(self, params, post):
        self._require_feature("per_zone_mode", "Per-zone keyboard mode")
        zones = [_hex_param(params, f"zone{i}") for i in range(1, 5)]
        brightness = _int_param(params, "brightness", 0, 100)
        if self.manager.set_per_zone_mode(*zones, brightness):
            data = {f"zone{i}": z for i, z in enumerate(zones, 1)}
            data["brightness"] = brightness
            return _ok(data)
        return self._failure("Failed to set per-zone mode")

    def _cmd_set_four_zone_mode(self, params, post):
        self._require_feature("four_zone_mode", "Four-zone keyboard mode")
        values = {
            "mode": _int_param(params, "mode", 0, 7),
            "speed": _int_param(params, "speed", 0, 9),
            "brightness": _int_param(params, "brightness", 0, 100),
            "direction": _int_param(params, "direction", 1, 2),
            "red": _int_param(params, "red", 0, 255),
            "green": _int_param(params, "green", 0, 255),
            "blue": _int_param(params, "blue", 0, 255),
        }
        if self.manager.set_four_zone_mode(**values):
            return _ok(values)
        return self._failure("Failed to set four-zone mode")

    def _cmd_get_modprobe_parameter(self, params, post):
        return _ok({"parameter": self.manager.get_modprobe_parameter()})

    # Privileged commands: the reply is sent first, the work runs afterwards in
    # a detached thread (see post_actions).
    def _schedule(self, post: List[Callable], name: str, func: Callable, *args) -> bool:
        if self.manager.restart_in_progress:
            return False
        post.append(lambda: self.manager.start_background_task(name, func, *args))
        return True

    def _make_force(self, param: str):
        def handler(params, post):
            scheduled = self._schedule(post, f"damx-force-{param}",
                                       self.manager.reload_driver_and_redetect, (f"{param}=1",), False)
            message = (f"Reloading linuwu_sense with {param}=1" if scheduled
                       else "A driver/daemon restart is already in progress")
            return _ok({"scheduled": scheduled, "parameter": param}, message)
        return handler

    def _make_set_modprobe(self, param: str):
        def handler(params, post):
            if not self.manager.set_modprobe_parameter(param):
                return self._failure(f"Failed to set modprobe parameter to {param}")
            scheduled = self._schedule(post, "damx-apply-modprobe",
                                       self.manager.reload_driver_and_redetect, (), False)
            return _ok({"parameter": param, "reload_scheduled": scheduled},
                       f"Persisted {param}; reloading linuwu_sense to apply it")
        return handler

    def _cmd_remove_modprobe_parameter(self, params, post):
        if self.manager.remove_modprobe_parameter():
            return _ok({"parameter": ""}, "Successfully removed modprobe parameter")
        return self._failure("Failed to remove modprobe parameter")

    def _cmd_restart_daemon(self, params, post):
        scheduled = self._schedule(post, "damx-restart-daemon", self.manager.restart_daemon)
        return _ok({"scheduled": scheduled},
                   "Restarting DAMX daemon" if scheduled else "A driver/daemon restart is already in progress")

    def _cmd_restart_drivers_and_daemon(self, params, post):
        scheduled = self._schedule(post, "damx-restart-drivers",
                                   self.manager.reload_driver_and_redetect, (), True)
        return _ok({"scheduled": scheduled},
                   "Restarting drivers and daemon" if scheduled
                   else "A driver/daemon restart is already in progress")


class DAMXDaemon:
    """Main daemon class that manages the lifecycle"""

    def __init__(self, verbose: bool = False):
        self.running = False
        self.verbose = verbose
        self.manager = None
        self.server = None
        self.config = None
        self.power_monitor = None
        self._wrote_pid = False

    @staticmethod
    def _default_config() -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.read_dict(DEFAULT_CONFIG)
        return config

    def _write_default_config(self, config: configparser.ConfigParser):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, 'w') as f:
                config.write(f)
        except OSError as e:
            log.warning(f"Could not write default config {CONFIG_PATH}: {e}")

    def load_config(self):
        """Load configuration; a corrupt file is backed up and replaced by defaults."""
        config = self._default_config()

        if not os.path.exists(CONFIG_PATH):
            log.info(f"Creating default config at {CONFIG_PATH}")
            self._write_default_config(config)
        else:
            try:
                config.read(CONFIG_PATH, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError) as e:
                backup = CONFIG_PATH + ".bak"
                log.error(f"Config file {CONFIG_PATH} is corrupt ({e}); backing it up to {backup} "
                          f"and using defaults")
                try:
                    os.replace(CONFIG_PATH, backup)
                except OSError as be:
                    log.error(f"Could not back up corrupt config: {be}")
                config = self._default_config()
                self._write_default_config(config)

        self.config = config

        level = config.get("General", "LogLevel", fallback="INFO").strip().upper()
        if level not in LOG_LEVELS:
            log.warning(f"Invalid LogLevel {level!r} in config; using INFO")
            level = "INFO"
        if self.verbose:
            level = "DEBUG"
        log.setLevel(getattr(logging, level))
        log.info(f"Log level set to {level}")
        return config

    def setup(self):
        """Set up the daemon"""
        try:
            self.load_config()
        except Exception as e:
            log.error(f"Failed to load config, using defaults: {e}")
            self.config = self._default_config()

        try:
            self.manager = DAMXManager()
            log.info(f"Driver Version: {self.manager.get_driver_version()}")
            self.power_monitor = PowerSourceDetector(self.manager)
            self.manager.power_monitor = self.power_monitor
            return True
        except Exception as e:
            log.error(f"Failed to set up daemon: {e}")
            log.error(traceback.format_exc())
            return False

    def _write_pid_file(self):
        try:
            fd = os.open(PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            self._wrote_pid = True
        except OSError as e:
            log.warning(f"Could not write PID file {PID_FILE}: {e}")

    def run(self):
        """Run the daemon. Exits with status 1 on fatal errors."""
        self.server = DaemonServer(self.manager)
        if not self.server.bind():
            log.critical("Could not start the socket server; exiting")
            self.cleanup()
            sys.exit(1)

        self._write_pid_file()
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                signal.signal(signum, self.signal_handler)

        exit_code = 0
        try:
            self.running = True
            try:
                self.manager.ppd_sync.detect()  # so start-up restore can sync ppd
            except Exception:
                log.error(f"ppd detection failed:\n{traceback.format_exc()}")
            try:
                self.manager.restore_state("start")
            except Exception:
                log.error(f"Restoring saved settings failed:\n{traceback.format_exc()}")
            if self.power_monitor:
                self.power_monitor.start_monitoring()
            self.manager.ppd_sync.start()
            self.server.serve()
        except Exception as e:
            log.error(f"Error running daemon: {e}")
            log.error(traceback.format_exc())
            exit_code = 1
        finally:
            self.cleanup()
        if exit_code:
            sys.exit(exit_code)

    def cleanup(self):
        """Clean up resources"""
        log.info("Cleaning up resources...")
        if self.server:
            self.server.stop()
        if self.power_monitor:
            self.power_monitor.stop_monitoring()
        if self.manager:
            try:
                self.manager.ppd_sync.stop()  # terminates busctl monitor
            except Exception:
                log.error(f"Stopping ppd sync failed:\n{traceback.format_exc()}")
            try:
                self.manager.shutdown_fans()  # engine active -> "0,0" (firmware auto)
            except Exception:
                log.error(f"Stopping the fan curve engine failed:\n{traceback.format_exc()}")
        if self._wrote_pid:
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass
            self._wrote_pid = False
        log.info("Daemon stopped")

    def signal_handler(self, sig, frame):
        """Handle SIGTERM/SIGINT/SIGHUP: clean shutdown."""
        log.info(f"Received signal {sig}, shutting down...")
        self.running = False
        if self.server:
            self.server.running = False
            if self.server.socket:
                try:
                    self.server.socket.shutdown(socket.SHUT_RDWR)  # wake accept() now
                except OSError:
                    pass


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="DAMX-Daemon")
    parser.add_argument('-v', '--verbose', action='store_true', help="Enable verbose logging")
    parser.add_argument('--version', action='version', version=f"DAMX-Daemon v{VERSION}")
    parser.add_argument('--debug', action='store_true', help="Enable debug mode")
    parser.add_argument('--config', type=str, help=f"Path to config file (default: {CONFIG_PATH})")
    return parser.parse_args()


def main():
    """Main function"""
    args = parse_args()

    if os.geteuid() != 0:
        print("This daemon must run as root. Please use sudo or run as root.", file=sys.stderr)
        sys.exit(1)

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = args.config

    daemon = DAMXDaemon(verbose=args.verbose or args.debug)
    if not daemon.setup():
        log.error("Failed to set up daemon, exiting...")
        sys.exit(1)
    daemon.run()


if __name__ == "__main__":
    main()
