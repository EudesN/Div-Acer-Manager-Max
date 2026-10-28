#!/usr/bin/env python3
# DAMX settings persistence: /var/lib/damx/state.json

import copy
import json
import logging
import os
import tempfile
import threading
from typing import Dict, Optional

from FanCurve import default_curve, validate_curve
from PowerProfilesSync import default_mapping, validate_mapping

log = logging.getLogger("DAMXDaemon")

STATE_VERSION = 1
SOURCES = ("ac", "battery")
FAN_MODES = ("auto", "max", "manual", "curve")


def _empty_source() -> Dict:
    return {"thermal_profile": None, "fan_mode": None, "fan_manual": None}


def _empty_settings() -> Dict:
    return {"battery_limiter": None, "usb_charging": None, "backlight_timeout": None}


def default_state() -> Dict:
    return {
        "version": STATE_VERSION,
        "restore_on_start": True,
        "ac": _empty_source(),
        "battery": _empty_source(),
        "curve": default_curve(),
        "settings": _empty_settings(),
        "ppd_sync": default_mapping(),
        "gpu_power_limits": {"default_limit_w": None, "max_limit_w": None},
    }


def _positive_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 10000


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_manual(v) -> bool:
    return (isinstance(v, list) and len(v) == 2 and all(_is_int(x) and 0 <= x <= 100 for x in v))


def _sanitize(data: Dict) -> Dict:
    """Merge a loaded object into defaults, dropping invalid fields."""
    state = default_state()
    if isinstance(data.get("restore_on_start"), bool):
        state["restore_on_start"] = data["restore_on_start"]
    for src in SOURCES:
        entry = data.get(src)
        if not isinstance(entry, dict):
            continue
        profile = entry.get("thermal_profile")
        if isinstance(profile, str) and profile:
            state[src]["thermal_profile"] = profile
        if entry.get("fan_mode") in FAN_MODES:
            state[src]["fan_mode"] = entry["fan_mode"]
        if _valid_manual(entry.get("fan_manual")):
            state[src]["fan_manual"] = list(entry["fan_manual"])
    try:
        state["curve"] = validate_curve(data.get("curve"))
    except ValueError as e:
        if "curve" in data:
            log.warning(f"Saved fan curve is invalid ({e}); using the default curve")
    settings = data.get("settings")
    if isinstance(settings, dict):
        for key in ("battery_limiter", "backlight_timeout"):
            if isinstance(settings.get(key), bool):
                state["settings"][key] = settings[key]
        if _is_int(settings.get("usb_charging")) and settings["usb_charging"] in (0, 10, 20, 30):
            state["settings"]["usb_charging"] = settings["usb_charging"]
    if "ppd_sync" in data:
        try:
            state["ppd_sync"] = validate_mapping(data["ppd_sync"])
        except ValueError as e:
            log.warning(f"Saved ppd_sync mapping is invalid ({e}); using defaults")
    limits = data.get("gpu_power_limits")
    if isinstance(limits, dict):
        for key in ("default_limit_w", "max_limit_w"):
            if _positive_number(limits.get(key)):
                state["gpu_power_limits"][key] = float(limits[key])
    return state


class StateStore:
    """Thread-safe persistent daemon state (atomic writes, dir 0755, file 0644)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data = self._load()

    # ------------------------------------------------------------ disk
    def _load(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("state is not a JSON object")
        except FileNotFoundError:
            return default_state()
        except (ValueError, UnicodeDecodeError, OSError) as e:
            backup = self.path + ".bak"
            log.error(f"State file {self.path} is corrupt ({e}); moved to {backup}, using defaults")
            try:
                os.replace(self.path, backup)
            except OSError as be:
                log.error(f"Could not back up corrupt state file: {be}")
            return default_state()
        return _sanitize(raw)

    def save(self) -> bool:
        with self._lock:
            content = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
            directory = os.path.dirname(self.path) or "."
            try:
                os.makedirs(directory, mode=0o755, exist_ok=True)
                os.chmod(directory, 0o755)
                fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                        f.flush()
                        os.fsync(f.fileno())
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, self.path)
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
                return True
            except OSError as e:
                log.error(f"Failed to save state to {self.path}: {e}")
                return False

    # ------------------------------------------------------------ accessors
    def snapshot(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data)

    @property
    def restore_on_start(self) -> bool:
        with self._lock:
            return bool(self.data["restore_on_start"])

    def source(self, src: str) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data[src])

    def curve(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data["curve"])

    def settings(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data["settings"])

    # ------------------------------------------------------------ mutators (save immediately)
    def update_source(self, src: str, **fields) -> bool:
        with self._lock:
            self.data[src].update(fields)
            return self.save()

    def set_setting(self, name: str, value) -> bool:
        with self._lock:
            self.data["settings"][name] = value
            return self.save()

    def set_curve(self, curve: Dict) -> bool:
        with self._lock:
            self.data["curve"] = copy.deepcopy(curve)
            return self.save()

    def set_restore_on_start(self, value: bool) -> bool:
        with self._lock:
            self.data["restore_on_start"] = bool(value)
            return self.save()

    def reset(self) -> bool:
        """Clear saved ac/battery/settings; keep the curve and restore_on_start."""
        with self._lock:
            self.data["ac"] = _empty_source()
            self.data["battery"] = _empty_source()
            self.data["settings"] = _empty_settings()
            return self.save()

    def ppd_sync(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data["ppd_sync"])

    def set_ppd_sync(self, mapping: Dict) -> bool:
        with self._lock:
            self.data["ppd_sync"] = copy.deepcopy(mapping)
            return self.save()

    def gpu_power_limits(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self.data["gpu_power_limits"])

    def set_gpu_power_limits(self, default_w: Optional[float], max_w: Optional[float]) -> bool:
        with self._lock:
            self.data["gpu_power_limits"] = {"default_limit_w": default_w, "max_limit_w": max_w}
            return self.save()

    def manual_value(self, src: str) -> Optional[list]:
        with self._lock:
            return copy.deepcopy(self.data[src]["fan_manual"])
