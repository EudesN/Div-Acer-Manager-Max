#!/usr/bin/env python3
# DAMX GPU power info: nvidia-powerd (Dynamic Boost) status and static power limits

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple

from FanCurve import TempSensors

log = logging.getLogger("DAMXDaemon")

POWERD_UNIT = "nvidia-powerd.service"
POWERD_UNIT_DIRS = ("/usr/lib/systemd/system", "/lib/systemd/system", "/etc/systemd/system")
# `systemctl is-enabled` states that mean "the unit file exists"
UNIT_FILE_STATES = ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "masked",
                    "masked-runtime", "static", "indirect", "disabled", "generated", "transient")
SYSTEMCTL_PATH = "/usr/bin/systemctl"
NVIDIA_SMI_TIMEOUT = 2
SYSTEMCTL_QUERY_TIMEOUT = 5
SYSTEMCTL_CHANGE_TIMEOUT = 10
POWERD_CACHE_SECONDS = 10.0
LIMITS_RETRY_SECONDS = 60.0
AWAKE_STATES = ("active", "unsupported", "unknown")  # never query while suspended/suspending

_DEFAULT_RE = re.compile(r"^\s*Default Power Limit\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*W", re.MULTILINE)
_MAX_RE = re.compile(r"^\s*Max Power Limit\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*W", re.MULTILINE)


def parse_power_limits(text: str) -> Tuple[Optional[float], Optional[float]]:
    """First numeric Default / Max Power Limit in `nvidia-smi -q -d POWER` output.

    On recent drivers these come from the "GPU Ceiling Power Limit" block; the
    later per-module values are "N/A" and do not match the numeric pattern.
    """
    d = _DEFAULT_RE.search(text or "")
    m = _MAX_RE.search(text or "")
    return (float(d.group(1)) if d else None, float(m.group(1)) if m else None)


def _systemctl() -> str:
    if os.path.exists(SYSTEMCTL_PATH):
        return SYSTEMCTL_PATH
    return shutil.which("systemctl") or SYSTEMCTL_PATH


class GpuPowerInfo:
    def __init__(self, state, sensors: Optional[TempSensors] = None, clock=time.monotonic):
        self.state = state
        self.sensors = sensors or TempSensors()
        self.clock = clock
        self._lock = threading.Lock()
        limits = state.gpu_power_limits()
        self.default_limit_w: Optional[float] = limits.get("default_limit_w")
        self.max_limit_w: Optional[float] = limits.get("max_limit_w")
        self._limits_tried = float("-inf")
        self._powerd_cache: Optional[Tuple[float, Dict]] = None

    # ------------------------------------------------------------ powerd
    def _systemctl_run(self, args, timeout):
        try:
            return subprocess.run([_systemctl(), *args], capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug(f"systemctl {' '.join(args)} failed: {e}")
            return None

    def powerd_status(self, force: bool = False) -> Dict:
        now = self.clock()
        with self._lock:
            if not force and self._powerd_cache and now - self._powerd_cache[0] < POWERD_CACHE_SECONDS:
                return dict(self._powerd_cache[1])
        enabled_r = self._systemctl_run(["is-enabled", POWERD_UNIT], SYSTEMCTL_QUERY_TIMEOUT)
        active_r = self._systemctl_run(["is-active", POWERD_UNIT], SYSTEMCTL_QUERY_TIMEOUT)
        enabled_state = (enabled_r.stdout or "").strip().splitlines()[0] if enabled_r and enabled_r.stdout else ""
        active_state = (active_r.stdout or "").strip().splitlines()[0] if active_r and active_r.stdout else ""
        installed = (enabled_state in UNIT_FILE_STATES
                     or any(os.path.exists(os.path.join(d, POWERD_UNIT)) for d in POWERD_UNIT_DIRS))
        status = {"installed": installed,
                  "enabled": enabled_state in ("enabled", "enabled-runtime"),
                  "active": active_state == "active"}
        with self._lock:
            self._powerd_cache = (now, status)
        return dict(status)

    def set_powerd(self, enabled: bool) -> Tuple[bool, Optional[str]]:
        if not self.powerd_status(force=True)["installed"]:
            return False, f"{POWERD_UNIT} is not installed"
        args = ["enable" if enabled else "disable", "--now", POWERD_UNIT]
        r = self._systemctl_run(args, SYSTEMCTL_CHANGE_TIMEOUT)
        if r is None or r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip() if r else "systemctl did not finish"
            return False, f"systemctl {' '.join(args)} failed: {detail}"
        log.info(f"{'Enabled' if enabled else 'Disabled'} {POWERD_UNIT}")
        return True, None

    # ------------------------------------------------------------ limits
    def _gpu_status(self) -> Optional[str]:
        return self.sensors.gpu_runtime_status()

    def _maybe_read_limits(self, status: Optional[str]):
        if self.default_limit_w is not None and self.max_limit_w is not None:
            return
        if status not in AWAKE_STATES:
            return  # absent or asleep: never wake the dGPU
        now = self.clock()
        if now - self._limits_tried < LIMITS_RETRY_SECONDS:
            return
        self._limits_tried = now
        smi = shutil.which("nvidia-smi")
        if not smi:
            return
        try:
            r = subprocess.run([smi, "-q", "-d", "POWER"], capture_output=True, text=True,
                               timeout=NVIDIA_SMI_TIMEOUT, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            log.debug(f"nvidia-smi -q -d POWER failed: {e}")
            return
        default, maximum = parse_power_limits(r.stdout)
        if default is None and maximum is None:
            return
        self.default_limit_w, self.max_limit_w = default, maximum
        log.info(f"GPU power limits: default {default} W, max {maximum} W")
        self.state.set_gpu_power_limits(default, maximum)

    @property
    def dynamic_boost_supported(self) -> Optional[bool]:
        if self.default_limit_w is None or self.max_limit_w is None:
            return None
        return self.max_limit_w > self.default_limit_w

    def info(self) -> Dict:
        status = self._gpu_status()
        present = status is not None
        self._maybe_read_limits(status)
        return {
            "nvidia_present": present,
            "gpu_asleep": status == "suspended",
            "powerd": self.powerd_status(),
            "default_limit_w": self.default_limit_w,
            "max_limit_w": self.max_limit_w,
            "dynamic_boost_supported": self.dynamic_boost_supported,
        }

    def summary(self) -> Dict:
        """For get_all_settings: cached values only, never runs nvidia-smi."""
        present = self._gpu_status() is not None
        return {"powerd_active": self.powerd_status()["active"] if present else None,
                "default_limit_w": self.default_limit_w, "max_limit_w": self.max_limit_w}
