#!/usr/bin/env python3
# DAMX <-> power-profiles-daemon (ppd) bridge, using busctl (no Python D-Bus dependency)

import copy
import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("DAMXDaemon")

PPD_PROFILES = ("power-saver", "balanced", "performance")
ACER_PROFILES = ("low-power", "quiet", "balanced", "balanced-performance", "performance", "cool", "custom")
SOURCES = ("ac", "battery")

# (bus name, object path, interface): ppd >= 0.20 first, then the legacy name.
BUS_CANDIDATES: List[Tuple[str, str, str]] = [
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
     "org.freedesktop.UPower.PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles"),
]

# When the mapped Acer profile is not offered by the kernel, try these next.
NEAREST_FALLBACK = ("balanced-performance", "balanced", "low-power")

ECHO_WINDOW = 3.0          # ignore PropertiesChanged with the value we just set
MIN_WRITE_INTERVAL = 1.0   # at most one ppd write per second
POLL_INTERVAL = 5.0        # polling fallback period
REDETECT_INTERVAL = 30.0   # look for ppd again when it is not running
BUSCTL_TIMEOUT = 5
MONITOR_QUICK_EXIT = 2.0   # a monitor dying sooner than this counts as "unsupported"
MONITOR_MAX_QUICK_FAILS = 3
MONITOR_MAX_BACKOFF = 60.0
MAX_POLL_FAILURES = 3

_GET_PROPERTY_RE = re.compile(r'^\s*s\s+"([^"]*)"\s*$')


def default_mapping() -> Dict:
    return {
        "enabled": True,
        "to_acer": {
            "ac": {"power-saver": "quiet", "balanced": "balanced", "performance": "balanced-performance"},
            "battery": {"power-saver": "low-power", "balanced": "balanced", "performance": "balanced"},
        },
        "to_ppd": {
            "low-power": "power-saver", "quiet": "power-saver", "balanced": "balanced",
            "balanced-performance": "performance", "performance": "performance",
        },
    }


def validate_mapping(mapping) -> Dict:
    """Validate a complete ppd_sync object; returns a normalised copy or raises ValueError."""
    if not isinstance(mapping, dict):
        raise ValueError("ppd_sync must be a JSON object")
    enabled = mapping.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("'enabled' must be a boolean (true/false)")
    to_acer = mapping.get("to_acer")
    if not isinstance(to_acer, dict) or set(to_acer) != set(SOURCES):
        raise ValueError("'to_acer' must be an object with exactly the keys 'ac' and 'battery'")
    result_acer = {}
    for src in SOURCES:
        entry = to_acer[src]
        if not isinstance(entry, dict) or set(entry) != set(PPD_PROFILES):
            raise ValueError(f"'to_acer.{src}' must map each of {', '.join(PPD_PROFILES)}")
        for ppd, acer in entry.items():
            if acer not in ACER_PROFILES:
                raise ValueError(f"to_acer.{src}.{ppd}: {acer!r} is not a known Acer profile "
                                 f"({', '.join(ACER_PROFILES)})")
        result_acer[src] = dict(entry)
    to_ppd = mapping.get("to_ppd")
    if not isinstance(to_ppd, dict) or not to_ppd:
        raise ValueError("'to_ppd' must be a non-empty object")
    for acer, ppd in to_ppd.items():
        if acer not in ACER_PROFILES:
            raise ValueError(f"to_ppd: {acer!r} is not a known Acer profile ({', '.join(ACER_PROFILES)})")
        if ppd not in PPD_PROFILES:
            raise ValueError(f"to_ppd.{acer}: {ppd!r} is not a ppd profile ({', '.join(PPD_PROFILES)})")
    return {"enabled": enabled, "to_acer": result_acer, "to_ppd": dict(to_ppd)}


def merge_mapping(current: Dict, update: Dict) -> Dict:
    """Partial update (enabled / to_acer[.ac|.battery][.x] / to_ppd[.x]) + full validation."""
    merged = copy.deepcopy(current)
    if "enabled" in update:
        merged["enabled"] = update["enabled"]
    if "to_acer" in update:
        to_acer = update["to_acer"]
        if not isinstance(to_acer, dict):
            raise ValueError("'to_acer' must be an object")
        for src, entry in to_acer.items():
            if src not in SOURCES:
                raise ValueError(f"to_acer: unknown power source {src!r} (expected ac, battery)")
            if not isinstance(entry, dict):
                raise ValueError(f"'to_acer.{src}' must be an object")
            for ppd, acer in entry.items():
                if ppd not in PPD_PROFILES:
                    raise ValueError(f"to_acer.{src}: {ppd!r} is not a ppd profile ({', '.join(PPD_PROFILES)})")
                merged["to_acer"][src][ppd] = acer
    if "to_ppd" in update:
        if not isinstance(update["to_ppd"], dict):
            raise ValueError("'to_ppd' must be an object")
        merged["to_ppd"].update(update["to_ppd"])
    return validate_mapping(merged)


def parse_get_property(stdout: str) -> Optional[str]:
    """`busctl get-property ... ActiveProfile` prints `s "balanced"`."""
    for line in (stdout or "").splitlines():
        m = _GET_PROPERTY_RE.match(line)
        if m:
            return m.group(1)
    return None


def _variant_value(value):
    # busctl JSON encodes variants as {"type": "s", "data": "balanced"}
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def parse_monitor_line(line: str) -> Tuple[bool, Optional[str]]:
    """Parse one `busctl monitor --json=short` line.

    busctl prints one JSON object per message, e.g.
      {"type":"signal",...,"interface":"org.freedesktop.DBus.Properties",
       "member":"PropertiesChanged","payload":{"type":"sa{sv}as","data":
       ["org.freedesktop.UPower.PowerProfiles",{"ActiveProfile":{"type":"s","data":"balanced"}},[]]}}

    Returns (relevant, value): relevant is True for a PropertiesChanged that
    touches ActiveProfile; value is None when it was only invalidated (caller
    re-reads it).  Raises ValueError for lines that are not valid JSON objects.
    """
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("monitor line is not a JSON object")
    member = obj.get("member")
    if member is not None and member != "PropertiesChanged":
        return False, None
    payload = obj.get("payload")
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list) or len(data) < 2:
        return False, None
    changed = _variant_value(data[1])
    invalidated = _variant_value(data[2]) if len(data) > 2 else []
    if isinstance(changed, dict) and "ActiveProfile" in changed:
        value = _variant_value(changed["ActiveProfile"])
        return True, value if isinstance(value, str) else None
    if isinstance(invalidated, list) and "ActiveProfile" in invalidated:
        return True, None
    return False, None


class PowerProfilesSync:
    """Two-way sync between ppd's ActiveProfile and the Acer platform profile."""

    def __init__(self, manager, clock=time.monotonic, poll_interval: float = POLL_INTERVAL):
        self.manager = manager
        self.clock = clock
        self.poll_interval = poll_interval
        self.bus: Optional[Tuple[str, str, str]] = None
        self.active_profile: Optional[str] = None
        self.detected = False
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._last_set: Optional[Tuple[str, float]] = None
        self._last_write = float("-inf")
        self._pending: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._proc_started = 0.0
        self._buf = b""
        self.monitor_supported = True
        self._quick_fails = 0
        self._restarts = 0
        self._next_monitor = 0.0
        self._poll_failures = 0

    # ------------------------------------------------------------ config
    @property
    def mapping(self) -> Dict:
        return self.manager.state.ppd_sync()

    @property
    def enabled(self) -> bool:
        return bool(self.mapping.get("enabled"))

    @property
    def available(self) -> bool:
        return self.bus is not None

    def status(self) -> Dict:
        mapping = self.mapping
        return {"available": self.available, "bus_name": self.bus[0] if self.bus else None,
                "active_profile": self.active_profile if self.available else None,
                "enabled": mapping["enabled"], "to_acer": mapping["to_acer"], "to_ppd": mapping["to_ppd"],
                "power_source": self.manager.power_source()}

    # ------------------------------------------------------------ busctl
    @staticmethod
    def _busctl() -> str:
        return shutil.which("busctl") or "/usr/bin/busctl"

    def _run(self, args: List[str]):
        try:
            return subprocess.run([self._busctl(), "--system", *args], capture_output=True, text=True,
                                  timeout=BUSCTL_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug(f"busctl {' '.join(args[:1])} failed: {e}")
            return None

    def _get(self, bus) -> Optional[str]:
        r = self._run(["get-property", bus[0], bus[1], bus[2], "ActiveProfile"])
        if r is None or r.returncode != 0:
            return None
        return parse_get_property(r.stdout)

    def detect(self) -> bool:
        """Find ppd on the system bus (new name first, then legacy)."""
        with self._lock:
            self.detected = True
            for bus in BUS_CANDIDATES:
                value = self._get(bus)
                if value is not None:
                    if self.bus != bus:
                        log.info(f"power-profiles-daemon found as {bus[0]} (active: {value})")
                    self.bus, self.active_profile = bus, value
                    self._poll_failures = 0
                    return True
            if self.bus is not None:
                log.info("power-profiles-daemon is no longer available")
            self.bus = None
            self.active_profile = None
            return False

    def ensure_detected(self) -> bool:
        if not self.detected:
            return self.detect()
        return self.available

    def read_active(self) -> Optional[str]:
        bus = self.bus
        return self._get(bus) if bus else None

    def _write(self, value: str, now: float) -> bool:
        if now - self._last_write < MIN_WRITE_INTERVAL:
            self._pending = value
            return False
        bus = self.bus
        if bus is None:
            return False
        self._last_write = now
        self._pending = None
        r = self._run(["set-property", bus[0], bus[1], bus[2], "ActiveProfile", "s", value])
        if r is None or r.returncode != 0:
            log.warning(f"Could not set ppd profile to {value}: {(r.stderr or '').strip() if r else 'busctl failed'}")
            return False
        self._last_set = (value, now)
        self.active_profile = value
        log.info(f"Synced power-profiles-daemon to '{value}'")
        return True

    def flush(self, now: Optional[float] = None):
        """Write a rate-limited pending ppd value once the 1 s window has passed."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._pending is not None and now - self._last_write >= MIN_WRITE_INTERVAL:
                value = self._pending
                if value == self.active_profile:
                    self._pending = None
                else:
                    self._write(value, now)

    # ------------------------------------------------------------ direction 2: DAMX -> ppd
    def on_acer_profile(self, profile: str, now: Optional[float] = None):
        """Called after every successful platform profile change."""
        if getattr(self._tls, "from_ppd", False):
            return  # applied because of ppd: do not echo it back
        now = self.clock() if now is None else now
        with self._lock:
            if not self.available or not self.enabled:
                return
            target = self.mapping["to_ppd"].get(profile)
            if target is None:
                return
            if self.active_profile is None:
                self.active_profile = self.read_active()
            if target == self.active_profile:
                self._pending = None
                return
            self._write(target, now)

    # ------------------------------------------------------------ direction 1: ppd -> DAMX
    def resolve_acer(self, value: str, source: str) -> Optional[str]:
        mapped = self.mapping["to_acer"].get(source, {}).get(value)
        if mapped is None:
            return None
        choices = self.manager.get_thermal_profile_choices()
        for candidate in (mapped, *NEAREST_FALLBACK):
            if candidate in choices:
                if candidate != mapped:
                    log.info(f"ppd '{value}' maps to '{mapped}' on {source}, which is not available; "
                             f"using '{candidate}'")
                return candidate
        log.warning(f"No available Acer profile for ppd '{value}' on {source}")
        return None

    def _apply(self, value: str, source: str) -> bool:
        target = self.resolve_acer(value, source)
        if target is None:
            return False
        if self.manager.get_thermal_profile() == target:
            return True
        self._tls.from_ppd = True
        try:
            ok = self.manager.user_set_thermal_profile(target)
        finally:
            self._tls.from_ppd = False
        if ok:
            log.info(f"Applied Acer profile '{target}' for ppd '{value}' on {source}")
        return ok

    def on_ppd_profile(self, value: str, now: Optional[float] = None):
        """ActiveProfile observed (monitor signal or poll)."""
        now = self.clock() if now is None else now
        with self._lock:
            previous, self.active_profile = self.active_profile, value
            if self._last_set and value == self._last_set[0] and now - self._last_set[1] < ECHO_WINDOW:
                return  # our own write coming back
            if value == previous or not self.enabled:
                return
            source = self.manager.power_source()
        log.info(f"power-profiles-daemon switched to '{value}'")
        self._apply(value, source)

    def apply_for_power_change(self, is_ac: bool) -> bool:
        """Direction 3: no saved profile for the new source -> follow ppd's profile."""
        if not self.available or not self.enabled:
            return False
        value = self.active_profile or self.read_active()
        if value is None:
            return False
        return self._apply(value, "ac" if is_ac else "battery")

    # ------------------------------------------------------------ polling / monitor
    def poll(self, now: Optional[float] = None):
        value = self.read_active()
        if value is None:
            self._poll_failures += 1
            if self._poll_failures >= MAX_POLL_FAILURES:
                log.warning("power-profiles-daemon stopped answering; will look for it again")
                self._kill_monitor()
                with self._lock:
                    self.bus = None
            return
        self._poll_failures = 0
        self.on_ppd_profile(value, now)

    def monitor_command(self) -> List[str]:
        name, path, _ = self.bus
        match = (f"type='signal',sender='{name}',interface='org.freedesktop.DBus.Properties',"
                 f"member='PropertiesChanged',path='{path}'")
        return [self._busctl(), "--system", "monitor", "--json=short", "--match", match]

    def _spawn_monitor(self, now: float):
        try:
            self._proc = subprocess.Popen(self.monitor_command(), stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.warning(f"Cannot run busctl monitor ({e}); polling ppd every {self.poll_interval:.0f} s")
            self.monitor_supported = False
            self._proc = None
            return
        self._proc_started = now
        self._buf = b""

    def _kill_monitor(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(2)
        except Exception:
            try:
                proc.kill()
                proc.wait(2)
            except Exception:
                pass

    def _monitor_died(self, now: float):
        proc, self._proc = self._proc, None
        if proc is None or self._stop.is_set():
            return
        try:
            proc.wait(1)
        except Exception:
            pass
        if now - self._proc_started < MONITOR_QUICK_EXIT:
            self._quick_fails += 1
        else:
            self._quick_fails = 0
        if self._quick_fails >= MONITOR_MAX_QUICK_FAILS:
            log.warning("busctl monitor --json is not usable here; polling ppd every "
                        f"{self.poll_interval:.0f} s instead")
            self.monitor_supported = False
        else:
            self._restarts += 1
            backoff = min(MONITOR_MAX_BACKOFF, 2.0 ** self._restarts)
            log.info(f"busctl monitor exited; restarting in {backoff:.0f} s (polling meanwhile)")
            self._next_monitor = now + backoff
        self.poll(now)  # catch a change made while nobody was watching

    def handle_monitor_line(self, line: str, now: Optional[float] = None) -> bool:
        """Returns False on a parse problem (the caller restarts the monitor)."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("{"):
            log.debug(f"Ignoring non-JSON busctl output: {line[:80]}")
            return True
        try:
            relevant, value = parse_monitor_line(line)
        except ValueError as e:
            log.warning(f"Unparsable busctl monitor output ({e}); restarting monitor")
            return False
        if not relevant:
            return True
        if value is None:
            self.poll(now)
        else:
            self._restarts = 0
            self.on_ppd_profile(value, now)
        return True

    def _pump_monitor(self, timeout: float, now: Optional[float] = None):
        proc = self._proc
        fd = proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        now = self.clock() if now is None else now
        if not ready:
            if proc.poll() is not None:
                self._monitor_died(now)
            return
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._monitor_died(now)
            return
        self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            if not self.handle_monitor_line(raw.decode("utf-8", "replace"), now):
                self._kill_monitor()
                self._restarts += 1
                self._next_monitor = now + min(MONITOR_MAX_BACKOFF, 2.0 ** self._restarts)
                self.poll(now)
                return

    def step(self, now: Optional[float] = None, pump_timeout: Optional[float] = None) -> float:
        """One iteration of the worker; returns how long to wait before the next."""
        given = now
        now = self.clock() if now is None else now
        if not self.available:
            if not self.detect():
                return REDETECT_INTERVAL
        self.flush(now)
        if self.monitor_supported and self._proc is None and now >= self._next_monitor:
            self._spawn_monitor(now)
        if self._proc is not None:
            self._pump_monitor(self.poll_interval if pump_timeout is None else pump_timeout, given)
            return 0.0
        self.poll(now)
        return min(self.poll_interval, MIN_WRITE_INTERVAL) if self._pending else self.poll_interval

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="DAMX-PPDSync", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                wait = self.step()
            except Exception:
                log.exception("ppd sync iteration failed")
                self._kill_monitor()
                wait = self.poll_interval
            if wait and self._stop.wait(wait):
                break
        self._kill_monitor()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._kill_monitor()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
