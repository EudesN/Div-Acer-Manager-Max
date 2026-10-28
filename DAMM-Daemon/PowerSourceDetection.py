#!/usr/bin/env python3
# DAMX Power Source Detection - Monitors power source and adjusts thermal profiles accordingly

import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, Optional

# Get logger from main daemon
log = logging.getLogger("DAMXDaemon")

# Kernel platform_profile names (see include/linux/platform_profile.h).
# linuwu_sense rejects "quiet", "balanced-performance" and "performance" while
# on battery (-EOPNOTSUPP), so only these are considered battery friendly.
BATTERY_FRIENDLY_PROFILES = ("low-power", "balanced")
# Order in which profiles are tried when we have to leave a non battery
# friendly profile after unplugging.  "quiet" is deliberately never used.
BATTERY_FALLBACK_ORDER = ("balanced", "low-power")

# power_supply "type" values that represent an external power source.
EXTERNAL_SUPPLY_TYPES = ("Mains", "USB")
# Names used by firmware for the AC adapter; used only when a supply exposes
# no "type" attribute.
LEGACY_AC_NAMES = re.compile(r"^(AC|ACAD|AC\d+|ADP\d*)$")

_UPOWER_ONLINE_RE = re.compile(r"^\s*online:\s+(yes|no)\b", re.MULTILINE)
_UPOWER_ON_BATTERY_RE = re.compile(r"^\s*on-battery:\s+(yes|no)\b", re.MULTILINE)


def read_external_power() -> Optional[bool]:
    """Read /sys/class/power_supply/*/online for Mains/USB supplies.

    True if any external supply is online, False if supplies exist but none is
    online, None if no external supply is exposed.
    """
    base = "/sys/class/power_supply"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    found = False
    for name in names:
        supply = os.path.join(base, name)
        try:
            with open(os.path.join(supply, "type"), "r") as f:
                supply_type = f.read().strip()
        except OSError:
            supply_type = "Mains" if LEGACY_AC_NAMES.match(name) else ""
        if supply_type not in EXTERNAL_SUPPLY_TYPES:
            continue
        try:
            with open(os.path.join(supply, "online"), "r") as f:
                online = f.read().strip()
        except OSError:
            continue
        found = True
        if online == "1":
            return True
    return False if found else None


class PowerSourceDetector:
    """Detects power source changes and manages automatic profile switching.

    A single daemon thread polls the power source every ``check_interval``
    seconds.  A change of power source is only acted upon once it has been
    observed for ``min_stable_checks`` consecutive checks spanning at least
    ``debounce_seconds`` (adapters flap under heavy GPU load with the battery
    limiter enabled, GitHub #229).  After a resume from suspend, changes are
    ignored for ``resume_grace_seconds`` (GitHub #253).
    """

    MAX_METHOD_FAILURES = 3
    RESUME_JUMP_THRESHOLD = 2.0  # seconds of suspended time between two checks

    def __init__(self, manager, check_interval: float = 5.0, debounce_seconds: float = 10.0,
                 min_stable_checks: int = 3, resume_grace_seconds: float = 30.0,
                 fallback_min_interval: float = 30.0):
        self.manager = manager
        self.current_source: Optional[bool] = None
        self.check_interval = check_interval
        self.debounce_seconds = debounce_seconds
        self.min_stable_checks = min_stable_checks
        self.resume_grace_seconds = resume_grace_seconds
        self.fallback_min_interval = fallback_min_interval

        self._candidate: Optional[bool] = None
        self._candidate_since = 0.0
        self._candidate_count = 0
        self._grace_until = 0.0
        self._last_sleep_offset: Optional[float] = None

        self._method_failures: Dict[str, int] = {"upower": 0, "acpi": 0}
        self._fallback_cache = None  # (monotonic timestamp, value)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        log.info("PowerSourceDetector initialized")

    # ------------------------------------------------------------ lifecycle
    def start_monitoring(self):
        """Start periodic power source checking in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="DAMX-PowerMonitor", daemon=True)
        self._thread.start()
        log.info("Monitoring power source started")

    def stop_monitoring(self, timeout: float = 5.0):
        """Stop periodic checking and wait for the monitor thread to exit."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.check_power_source()
            except Exception:
                log.exception("Power source check failed")
            if self._stop_event.wait(self.check_interval):
                break

    # ------------------------------------------------------------ checking
    def _sleep_offset(self) -> float:
        """Time spent suspended since boot (CLOCK_BOOTTIME - CLOCK_MONOTONIC).

        CLOCK_MONOTONIC stops during suspend, CLOCK_BOOTTIME does not, so the
        difference grows by the suspended time on every resume.  Falls back to
        wall clock minus monotonic time.
        """
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        except (AttributeError, OSError):
            return time.time() - time.monotonic()

    def _detect_resume(self, now: float) -> bool:
        offset = self._sleep_offset()
        last, self._last_sleep_offset = self._last_sleep_offset, offset
        if last is not None and offset - last > self.RESUME_JUMP_THRESHOLD:
            log.info("Resume from suspend detected; ignoring power source changes for %.0f s",
                     self.resume_grace_seconds)
            self._grace_until = now + self.resume_grace_seconds
            self._reset_candidate()
            self._notify_resume()
            return True
        return False

    def _notify_resume(self):
        """Firmware resets fans/profiles on resume: let the manager restore state."""
        restore = getattr(self.manager, "restore_state", None)
        if not callable(restore):
            return
        try:
            restore("resume")
        except Exception:
            log.exception("Restoring settings after resume failed")

    def _reset_candidate(self):
        self._candidate = None
        self._candidate_since = 0.0
        self._candidate_count = 0

    def check_power_source(self, now: Optional[float] = None):
        """Run one check; acts only on a debounced, stable change."""
        if now is None:
            now = time.monotonic()
        try:
            self._detect_resume(now)
        except Exception:
            log.exception("Resume detection failed")

        try:
            state = self._is_ac_connected()
        except Exception:
            log.exception("Error checking power status")
            return
        if state is None:
            return

        if self.current_source is None:
            # First observation at startup: accept immediately.
            self.current_source = state
            self._reset_candidate()
            self._safe_handle(state, initial=True)
            return

        if state == self.current_source:
            if self._candidate is not None:
                log.debug("Power source flap ignored (returned to %s)", "AC" if state else "battery")
            self._reset_candidate()
            return

        if now < self._grace_until:
            log.debug("Ignoring power source change during post-resume grace period")
            self._reset_candidate()
            return

        if self._candidate != state:
            self._candidate = state
            self._candidate_since = now
            self._candidate_count = 1
        else:
            self._candidate_count += 1

        if (self._candidate_count >= self.min_stable_checks
                and now - self._candidate_since >= self.debounce_seconds):
            self.current_source = state
            self._reset_candidate()
            if not self._stop_event.is_set():
                self._safe_handle(state)

    def _safe_handle(self, state: bool, initial: bool = False):
        try:
            if self._restore_saved(state, initial):
                return  # a saved per-source profile takes precedence
        except Exception:
            log.exception("Restoring saved settings for the power source failed")
        if not initial:
            try:
                follow_ppd = getattr(self.manager, "apply_ppd_for_power_change", None)
                if callable(follow_ppd) and follow_ppd(state):
                    return  # no saved profile: followed power-profiles-daemon's profile
            except Exception:
                log.exception("Applying the power-profiles-daemon profile failed")
        try:
            self._handle_power_change(state)
        except Exception:
            log.exception("Error handling power source change")

    def _restore_saved(self, state: bool, initial: bool) -> bool:
        restore = getattr(self.manager, "restore_power_source", None)
        if not callable(restore):
            return False
        log.info("Power source is %s", "AC" if state else "battery")
        return bool(restore(state, "initial" if initial else "change"))

    # ------------------------------------------------------------ detection
    def _check_sysfs(self) -> Optional[bool]:
        """Read /sys/class/power_supply/*/online for Mains/USB supplies."""
        return read_external_power()

    def _is_ac_connected(self) -> Optional[bool]:
        """True on AC, False on battery, None if unknown."""
        state = self._check_sysfs()
        if state is not None:
            return state

        now = time.monotonic()
        if self._fallback_cache is not None and now - self._fallback_cache[0] < self.fallback_min_interval:
            return self._fallback_cache[1]

        result = None
        for name, method in (("upower", self._check_using_upower), ("acpi", self._check_using_acpi)):
            if self._method_failures[name] >= self.MAX_METHOD_FAILURES:
                continue
            value = method()
            if value is None:
                self._method_failures[name] += 1
                if self._method_failures[name] >= self.MAX_METHOD_FAILURES:
                    log.warning("Power detection via %s keeps failing; disabling it", name)
                continue
            self._method_failures[name] = 0
            result = value
            break
        self._fallback_cache = (now, result)
        return result

    def _run_tool(self, name, cmd) -> Optional[str]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            return result.stdout or ""
        except FileNotFoundError:
            log.debug("%s is not installed", name)
            self._method_failures[name] = self.MAX_METHOD_FAILURES
            return None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            log.debug("%s check failed: %s", name, e)
            return None

    def _check_using_upower(self) -> Optional[bool]:
        """Check power status using upower (handles padded 'online:   yes')."""
        out = self._run_tool("upower", ["upower", "-d"])
        if out is None:
            return None
        states = _UPOWER_ONLINE_RE.findall(out)
        if states:
            return "yes" in states
        on_battery = _UPOWER_ON_BATTERY_RE.search(out)
        if on_battery:
            return on_battery.group(1) == "no"
        return None

    def _check_using_acpi(self) -> Optional[bool]:
        """Check power status using acpi."""
        out = self._run_tool("acpi", ["acpi", "-a"])
        if out is None:
            return None
        if "on-line" in out:
            return True
        if "off-line" in out:
            return False
        return None

    # ------------------------------------------------------------ actions
    def _handle_power_change(self, is_plugged_in: bool):
        """Handle a (debounced) power source change."""
        features = getattr(self.manager, "available_features", set())
        if "thermal_profile" not in features:
            return

        if is_plugged_in:
            log.info("Switched to AC power")
            return

        log.info("Switched to battery power")
        current_profile = self.manager.get_thermal_profile()
        if current_profile in BATTERY_FRIENDLY_PROFILES:
            log.info("Keeping battery friendly profile '%s'", current_profile)
            return

        available_profiles = self.manager.get_thermal_profile_choices()
        for candidate in BATTERY_FALLBACK_ORDER:
            if candidate not in available_profiles:
                continue
            if self.manager.set_thermal_profile(candidate):
                log.info("Auto-switched thermal profile to '%s' for battery power", candidate)
                return
            log.warning("Could not switch to '%s' on battery: %s", candidate,
                        getattr(self.manager, "last_error", None) or "unknown error")
        log.warning("No battery friendly thermal profile could be applied (available: %s)",
                    ", ".join(available_profiles) or "none")
