#!/usr/bin/env python3
# DAMX fan curve engine: temperature -> fan percent, written to linuwu_sense fan_speed

import copy
import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("DAMXDaemon")

MIN_POINTS = 2
MAX_POINTS = 8
TEMP_MIN, TEMP_MAX = 20, 105
PERCENT_MIN, PERCENT_MAX = 1, 100     # 0 means "firmware auto" on this driver
HYSTERESIS_MIN, HYSTERESIS_MAX = 0, 10
INTERVAL_MIN, INTERVAL_MAX = 1, 10
DEFAULT_HYSTERESIS = 4
DEFAULT_INTERVAL = 2

SAFETY_TEMP = 95          # at or above this temperature the output is 100 %
MIN_WRITE_DELTA = 2       # percent change needed before rewriting fan_speed
REASSERT_SECONDS = 30.0   # rewrite even if unchanged (firmware resets to auto)
FAILSAFE_READS = 3        # consecutive failed temperature reads -> "0,0"

NVIDIA_VENDOR = "0x10de"
NVIDIA_SMI_ARGS = ["--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
NVIDIA_SMI_TIMEOUT = 2


def _preset(cpu, gpu):
    return {"cpu": cpu, "gpu": gpu, "hysteresis": DEFAULT_HYSTERESIS, "interval": DEFAULT_INTERVAL}


PRESETS: Dict[str, Dict] = {
    "silent": _preset([[40, 20], [55, 30], [65, 45], [75, 65], [85, 90]],
                      [[40, 20], [55, 30], [65, 45], [75, 65], [85, 90]]),
    "balanced": _preset([[40, 25], [50, 35], [60, 50], [70, 70], [80, 90], [90, 100]],
                        [[40, 25], [50, 35], [60, 50], [70, 70], [80, 90], [90, 100]]),
    "aggressive": _preset([[35, 35], [45, 50], [55, 65], [65, 80], [75, 100]],
                          [[35, 35], [45, 50], [55, 65], [65, 80], [75, 100]]),
}


def default_curve() -> Dict:
    return copy.deepcopy(PRESETS["balanced"])


def presets() -> Dict[str, Dict]:
    return copy.deepcopy(PRESETS)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_points(name: str, points) -> List[List[int]]:
    if not isinstance(points, list) or not (MIN_POINTS <= len(points) <= MAX_POINTS):
        raise ValueError(f"curve '{name}' must be a list of {MIN_POINTS} to {MAX_POINTS} [temp, percent] points")
    result = []
    for i, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"curve '{name}' point {i} must be [temp, percent]")
        temp, percent = point
        if not _is_int(temp) or not (TEMP_MIN <= temp <= TEMP_MAX):
            raise ValueError(f"curve '{name}' point {i}: temperature must be an integer between "
                             f"{TEMP_MIN} and {TEMP_MAX}")
        if not _is_int(percent) or not (PERCENT_MIN <= percent <= PERCENT_MAX):
            raise ValueError(f"curve '{name}' point {i}: percent must be an integer between "
                             f"{PERCENT_MIN} and {PERCENT_MAX}")
        if result and temp <= result[-1][0]:
            raise ValueError(f"curve '{name}': temperatures must be strictly increasing")
        if result and percent < result[-1][1]:
            raise ValueError(f"curve '{name}': percentages must not decrease")
        result.append([temp, percent])
    return result


def validate_curve(curve) -> Dict:
    """Validate a curve object; returns a normalised copy or raises ValueError."""
    if not isinstance(curve, dict):
        raise ValueError("curve must be a JSON object")
    hysteresis = curve.get("hysteresis", DEFAULT_HYSTERESIS)
    interval = curve.get("interval", DEFAULT_INTERVAL)
    if not _is_int(hysteresis) or not (HYSTERESIS_MIN <= hysteresis <= HYSTERESIS_MAX):
        raise ValueError(f"curve 'hysteresis' must be an integer between {HYSTERESIS_MIN} and {HYSTERESIS_MAX}")
    if not _is_int(interval) or not (INTERVAL_MIN <= interval <= INTERVAL_MAX):
        raise ValueError(f"curve 'interval' must be an integer between {INTERVAL_MIN} and {INTERVAL_MAX}")
    for key in ("cpu", "gpu"):
        if key not in curve:
            raise ValueError(f"curve is missing '{key}'")
    return {
        "cpu": _validate_points("cpu", curve["cpu"]),
        "gpu": _validate_points("gpu", curve["gpu"]),
        "hysteresis": hysteresis,
        "interval": interval,
    }


def interpolate(points: List[List[int]], temp: float) -> int:
    """Linear interpolation; clamped to the first/last point; >= 95 C -> 100 %."""
    if temp >= SAFETY_TEMP:
        return 100
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            return int(round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0)))
    return points[-1][1]


class _Channel:
    """Hysteresis state for one fan."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.target: Optional[int] = None
        self.ref_temp: Optional[float] = None

    def update(self, points, temp: float, hysteresis: int) -> int:
        raw = interpolate(points, temp)
        if self.target is None or raw > self.target:
            self.target, self.ref_temp = raw, temp          # increases apply immediately
        elif raw < self.target and temp <= self.ref_temp - hysteresis:
            self.target, self.ref_temp = raw, temp          # decrease only after cooling enough
        return self.target


# ---------------------------------------------------------------- sensors
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


class TempSensors:
    """CPU temperature from hwmon and NVIDIA dGPU temperature (without waking it)."""

    def __init__(self):
        self._cpu_path: Optional[str] = None
        self._gpu_dev: Optional[str] = None
        self._gpu_searched = False

    # CPU ---------------------------------------------------------------
    def _find_cpu_path(self) -> Optional[str]:
        hwmons = sorted(glob.glob("/sys/class/hwmon/hwmon*"))
        wanted = {"coretemp": "Package id 0", "k10temp": "Tctl"}
        fallback = None
        for hw in hwmons:
            name = _read(os.path.join(hw, "name")) or ""
            if name in wanted:
                for label_path in sorted(glob.glob(os.path.join(hw, "temp*_label"))):
                    if _read(label_path) == wanted[name]:
                        return label_path[:-len("_label")] + "_input"
                first = os.path.join(hw, "temp1_input")
                if os.path.exists(first):
                    return first
            elif fallback is None and name.startswith(("acer", "linuwu")):
                first = os.path.join(hw, "temp1_input")
                if os.path.exists(first):
                    fallback = first
        return fallback

    def cpu_temp(self) -> Optional[float]:
        if self._cpu_path is None:
            self._cpu_path = self._find_cpu_path()
            if self._cpu_path is None:
                return None
        raw = _read(self._cpu_path)
        try:
            return int(raw) / 1000.0
        except (TypeError, ValueError):
            self._cpu_path = None  # rediscover next time
            return None

    # GPU ---------------------------------------------------------------
    def _find_nvidia_gpu(self) -> Optional[str]:
        for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
            if (_read(os.path.join(dev, "vendor")) or "").lower() != NVIDIA_VENDOR:
                continue
            if (_read(os.path.join(dev, "class")) or "").startswith("0x03"):
                return dev
        return None

    def gpu_runtime_status(self) -> Optional[str]:
        """runtime_status of the NVIDIA dGPU ('active', 'suspended', ...); None if no dGPU."""
        if not self._gpu_searched:
            self._gpu_dev = self._find_nvidia_gpu()
            self._gpu_searched = True
        if self._gpu_dev is None:
            return None
        return _read(os.path.join(self._gpu_dev, "power", "runtime_status")) or "unknown"

    def gpu_temp(self) -> Tuple[Optional[float], bool]:
        """(temperature or None, asleep).  Never runs nvidia-smi on a suspended GPU."""
        status = self.gpu_runtime_status()
        if status is None:
            return None, False
        if status == "suspended":
            return None, True
        smi = shutil.which("nvidia-smi")
        if not smi:
            return None, False
        try:
            result = subprocess.run([smi, *NVIDIA_SMI_ARGS], capture_output=True, text=True,
                                    timeout=NVIDIA_SMI_TIMEOUT, check=True)
            return float((result.stdout or "").strip().splitlines()[0]), False
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                ValueError, IndexError) as e:
            log.debug(f"nvidia-smi temperature read failed: {e}")
            return None, False


# ---------------------------------------------------------------- engine
class CurveEngine:
    """One daemon thread applying a fan curve; stopped through an Event.

    ``write_fn(cpu, gpu, stop_event)`` performs the sysfs write under the
    manager's lock and must refuse to write once ``stop_event`` is set, so a
    manual fan change that stopped the engine can never be overwritten.
    """

    def __init__(self, write_fn: Callable[[int, int, threading.Event], bool],
                 sensors: Optional[TempSensors] = None, clock: Callable[[], float] = time.monotonic):
        self._write_fn = write_fn
        self.sensors = sensors or TempSensors()
        self.clock = clock
        self.curve = default_curve()
        self._ctl = threading.Lock()
        self._stop = threading.Event()
        self._stop.set()
        self._thread: Optional[threading.Thread] = None
        self._cpu = _Channel()
        self._gpu = _Channel()
        self._failures = 0
        self._failsafe = False
        self._last_written: Optional[Tuple[int, int]] = None
        self._last_write_time = 0.0
        self._force_write = False
        self.last = self._empty_last()

    @staticmethod
    def _empty_last() -> Dict:
        return {"cpu_temp": None, "gpu_temp": None, "cpu_percent": None, "gpu_percent": None,
                "gpu_asleep": False}

    @property
    def active(self) -> bool:
        return not self._stop.is_set() and self._thread is not None and self._thread.is_alive()

    def _reset(self):
        self._cpu.reset()
        self._gpu.reset()
        self._failures = 0
        self._failsafe = False
        self._last_written = None
        self._last_write_time = 0.0
        self._force_write = False

    def start(self, curve: Dict):
        """Start (or, if running, update the curve of and re-assert) the engine."""
        curve = validate_curve(curve)
        with self._ctl:
            self.curve = curve
            if self.active:
                self._cpu.reset()
                self._gpu.reset()
                self._force_write = True
                return
            self._reset()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="DAMX-FanCurve", daemon=True)
            self._thread.start()
            log.info("Fan curve engine started")

    def request_reassert(self):
        self._force_write = True

    def stop(self, write_auto: bool = False, timeout: float = 5.0) -> bool:
        """Stop the engine; returns True if it was running.  Optionally writes "0,0"."""
        with self._ctl:
            was_active = self.active
            stop_event, thread = self._stop, self._thread
            stop_event.set()
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if was_active:
            log.info("Fan curve engine stopped")
            if write_auto:
                self._write_fn(0, 0, threading.Event())
        return was_active

    def _run(self, stop_event: threading.Event):
        while not stop_event.is_set():
            self.step(stop_event=stop_event)
            if stop_event.wait(self.curve.get("interval", DEFAULT_INTERVAL)):
                break

    def step(self, now: Optional[float] = None, stop_event: Optional[threading.Event] = None):
        """One iteration (public for tests)."""
        if now is None:
            now = self.clock()
        if stop_event is None:
            stop_event = self._stop if self.active else threading.Event()
        try:
            target = self._compute()
        except Exception:
            log.exception("Fan curve engine error; falling back to firmware auto")
            self._failures = max(self._failures, FAILSAFE_READS)
            target = None
        if target is None:
            if self._failures >= FAILSAFE_READS:
                if not self._failsafe:
                    log.error("Fan curve: temperature unavailable; fan control returned to firmware (0,0)")
                    self._failsafe = True
                    self._force_write = True
                    self._cpu.reset()
                    self._gpu.reset()
                target = (0, 0)
            else:
                return
        self._maybe_write(target, now, stop_event)

    def _compute(self) -> Optional[Tuple[int, int]]:
        curve = self.curve
        cpu_temp = self.sensors.cpu_temp()
        if cpu_temp is None:
            self._failures += 1
            self.last.update(cpu_temp=None)
            log.debug(f"Fan curve: CPU temperature read failed ({self._failures})")
            return None
        if self._failsafe:
            log.info("Fan curve: temperature readings recovered")
        self._failures = 0
        self._failsafe = False

        gpu_temp, asleep = self.sensors.gpu_temp()
        cpu_pct = self._cpu.update(curve["cpu"], cpu_temp, curve["hysteresis"])
        if gpu_temp is None:
            # dGPU asleep (or no readable dGPU): follow the CPU curve, never wake it.
            self._gpu.reset()
            gpu_pct = cpu_pct
        else:
            gpu_pct = self._gpu.update(curve["gpu"], gpu_temp, curve["hysteresis"])
        self.last = {"cpu_temp": cpu_temp, "gpu_temp": gpu_temp, "cpu_percent": cpu_pct,
                     "gpu_percent": gpu_pct, "gpu_asleep": asleep}
        return cpu_pct, gpu_pct

    def _maybe_write(self, target: Tuple[int, int], now: float, stop_event: threading.Event):
        last = self._last_written
        due = (self._force_write or last is None
               or abs(target[0] - last[0]) >= MIN_WRITE_DELTA
               or abs(target[1] - last[1]) >= MIN_WRITE_DELTA
               or now - self._last_write_time >= REASSERT_SECONDS)
        if not due:
            return
        if self._write_fn(target[0], target[1], stop_event):
            self._last_written = target
            self._last_write_time = now
            self._force_write = False
