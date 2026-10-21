"""DAMXManager: feature detection, sysfs reads/writes, validation."""
import errno
import os
import stat
import threading
from pathlib import Path

import pytest

from conftest import BASE_REL, build_fake_sysfs, cmd_names, read

NITRO = BASE_REL + "/nitro_sense"
KB = BASE_REL + "/four_zoned_kb"


# ---------------------------------------------------------------- detection
def test_nitro_v15_feature_detection(nitro, damx):
    assert nitro.laptop_type == damx.LaptopType.NITRO
    assert nitro.base_path.endswith("acer-wmi/nitro_sense")
    assert nitro.available_features == {
        "thermal_profile", "fan_speed", "battery_limiter",
        "battery_calibration", "usb_charging", "backlight_timeout",
    }
    assert nitro.has_four_zone_kb is False
    assert nitro.get_driver_version() == "v6.x"


def test_predator_detection(root, damx):
    build_fake_sysfs(root, group="predator_sense",
                     files={"fan_speed": "0,0\n", "lcd_override": "0\n",
                            "boot_animation_sound": "1\n"})
    m = damx.DAMXManager()
    assert m.laptop_type == damx.LaptopType.PREDATOR
    assert {"lcd_override", "boot_animation_sound", "fan_speed"} <= m.available_features


def test_four_zone_detection(nitro4z):
    assert nitro4z.has_four_zone_kb
    assert {"per_zone_mode", "four_zone_mode"} <= nitro4z.available_features


COUNTER = "run/damx/restart_attempts"


def test_module_absent_runs_reduced_mode_without_reload(root, damx, fake_sp):
    """GitHub #70: an absent module must not trigger rmmod/modprobe loops."""
    build_fake_sysfs(root, module_loaded=False)
    m = damx.DAMXManager()
    assert fake_sp.calls == []
    assert m.laptop_type == damx.LaptopType.UNKNOWN
    assert m.driver_loaded is False and m.driver_status == "not_loaded"
    assert m.available_features == {"thermal_profile"}
    assert not Path(root, COUNTER).exists()
    # Requests for unsupported features fail cleanly
    assert m.set_fan_speed(50, 50) is False
    assert m.get_fan_speed() == ("", "")
    s = m.get_all_settings()
    assert s["laptop_type"] == "UNKNOWN" and s["driver_loaded"] is False
    assert s["driver_status"] == "not_loaded" and s["status_message"]


def test_unknown_model_reloads_driver_at_most_once_per_boot(root, damx, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    m = damx.DAMXManager()
    assert cmd_names(fake_sp.calls) == [["rmmod", "linuwu_sense"], ["modprobe", "linuwu_sense"]]
    assert not any("sudo" in c or any("systemctl" in x for x in c) for c in fake_sp.calls)
    assert read(root, COUNTER) == "1"
    assert m.laptop_type == damx.LaptopType.UNKNOWN
    assert m.driver_loaded is True and m.driver_status == "unsupported_model"
    # Second start in the same boot: no further reload, no exit
    fake_sp.calls.clear()
    m2 = damx.DAMXManager()
    assert fake_sp.calls == []
    assert m2.get_all_settings()["driver_status"] == "unsupported_model"


def test_unknown_model_reload_failure_continues(root, damx, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    fake_sp.fail = True
    m = damx.DAMXManager()
    assert m.laptop_type == damx.LaptopType.UNKNOWN
    assert len(fake_sp.calls) == 1  # rmmod failed -> no modprobe
    assert read(root, COUNTER) == "1"


def test_unknown_model_reload_recovers(root, damx, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    orig = fake_sp.run

    def run(cmd, *a, **kw):
        if os.path.basename(cmd[0]) == "modprobe":
            base = Path(root, NITRO)
            base.mkdir(parents=True)
            (base / "fan_speed").write_text("0,0\n")
        return orig(cmd, *a, **kw)
    fake_sp.run = run
    m = damx.DAMXManager()
    assert m.laptop_type == damx.LaptopType.NITRO
    assert "fan_speed" in m.available_features


def test_restart_counter_never_follows_symlink(root, damx, fake_sp):
    """Counter lives in /run/damx and is opened with O_NOFOLLOW (was /tmp, CWE-377)."""
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    victim = Path(root, "etc/victim.conf")
    victim.write_text("important\n")
    Path(root, "run/damx").mkdir(parents=True)
    os.symlink(victim, Path(root, COUNTER))
    damx.DAMXManager()
    assert victim.read_text() == "important\n", "root followed a symlink"
    assert not Path(root, COUNTER).is_symlink()
    assert read(root, COUNTER) == "1"


def test_runtime_dir_created_private(root, damx, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    damx.DAMXManager()
    assert stat.S_IMODE(os.stat(Path(root, "run/damx")).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(Path(root, COUNTER)).st_mode) == 0o600


def test_corrupt_restart_counter_treated_as_zero(root, damx, fake_sp):
    build_fake_sysfs(root, module_loaded=False, driver_present=True)
    Path(root, "run/damx").mkdir(parents=True)
    Path(root, COUNTER).write_text("garbage")
    damx.DAMXManager()
    assert read(root, COUNTER) == "1"
    assert len(fake_sp.calls) == 2


def test_get_all_settings_shape_matches_csharp_DAMXSettings(nitro4z):
    s = nitro4z.get_all_settings()
    # C# DAMXSettings: strings for toggles, objects for thermal_profile/fan_speed
    for k in ("laptop_type", "version", "driver_version", "modprobe_parameter",
              "backlight_timeout", "battery_calibration", "battery_limiter",
              "usb_charging", "per_zone_mode", "four_zone_mode",
              "driver_status", "status_message", "fan_mode"):
        assert isinstance(s[k], str), k
    assert isinstance(s["has_four_zone_kb"], bool)
    assert s["driver_loaded"] is True and s["driver_status"] == "ok"
    assert isinstance(s["available_features"], list)
    assert set(s["thermal_profile"]) == {"current", "available"}
    assert isinstance(s["fan_speed"]["cpu"], str) and isinstance(s["fan_speed"]["gpu"], str)


# ---------------------------------------------------------------- thermal
def test_set_thermal_profile_valid(nitro, root):
    assert nitro.set_thermal_profile("performance")
    assert read(root, "sys/firmware/acpi/platform_profile") == "performance\n"
    assert nitro.get_thermal_profile() == "performance"


def test_thermal_profiles_come_from_kernel_choices(root, damx):
    """Profiles are never hard-coded: if the kernel stops advertising
    'performance' (ANV15-51 without firmware support) it cannot be set."""
    build_fake_sysfs(root, choices="low-power balanced")
    m = damx.DAMXManager()
    assert m.get_thermal_profile_choices() == ["low-power", "balanced"]
    assert m.get_all_settings()["thermal_profile"]["available"] == ["low-power", "balanced"]
    assert m.set_thermal_profile("performance") is False
    assert "available: low-power, balanced" in m.last_error
    assert read(root, "sys/firmware/acpi/platform_profile") == "balanced\n"


def _fail_writes_to(monkeypatch, suffix, err):
    """Make write() on a sysfs file fail like the kernel does; returns attempted values."""
    real_write = os.write
    attempted = []

    def fake_write(fd, data):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            target = ""
        if target.endswith(suffix):
            attempted.append(bytes(data))
            raise OSError(err, os.strerror(err))
        return real_write(fd, data)
    monkeypatch.setattr(os, "write", fake_write)
    return attempted


def test_thermal_profile_kernel_error_reported_without_fallback(nitro, root, monkeypatch, damx):
    attempted = _fail_writes_to(monkeypatch, "platform_profile", errno.EOPNOTSUPP)
    srv = damx.DaemonServer(nitro)
    r = srv.process_command("set_thermal_profile", {"profile": "performance"})
    assert r["success"] is False
    assert "Operation not supported (EOPNOTSUPP)" in r["error"]
    # no silent fallback to another profile
    assert attempted == [b"performance\n"]


def test_thermal_profile_eio_reported(nitro, monkeypatch, damx):
    _fail_writes_to(monkeypatch, "platform_profile", errno.EIO)
    r = damx.DaemonServer(nitro).process_command("set_thermal_profile", {"profile": "low-power"})
    assert r["success"] is False and "(EIO)" in r["error"]


@pytest.mark.parametrize("bad", ["turbo", "", "../../etc/passwd", "balanced\nperformance", None, 5])
def test_set_thermal_profile_invalid(nitro, root, bad):
    assert nitro.set_thermal_profile(bad) is False
    assert read(root, "sys/firmware/acpi/platform_profile") == "balanced\n"


def test_set_thermal_profile_kernel_rejects_on_battery(nitro, root, monkeypatch):
    """Kernel returns -EOPNOTSUPP for performance/balanced-performance/quiet on battery."""
    import errno
    real_write = nitro._write_file.__func__

    def kernel_like(self, path, value):
        if path.endswith("platform_profile") and value in ("performance", "balanced-performance", "quiet"):
            try:
                raise OSError(errno.EOPNOTSUPP, "Operation not supported")
            except Exception:
                return False
        return real_write(self, path, value)
    monkeypatch.setattr(type(nitro), "_write_file", kernel_like)
    assert nitro.set_thermal_profile("performance") is False
    assert nitro.set_thermal_profile("low-power") is True


# ---------------------------------------------------------------- fan speed
def test_fan_speed_set_get(nitro, root):
    assert nitro.set_fan_speed(50, 60)
    assert read(root, NITRO + "/fan_speed") == "50,60\n"
    assert nitro.get_fan_speed() == ("50", "60")
    assert nitro.set_fan_speed(0, 100)
    assert nitro.set_fan_speed(100, 0)


@pytest.mark.parametrize("cpu,gpu", [(-1, 50), (101, 50), (50, -1), (50, 101)])
def test_fan_speed_out_of_range(nitro, root, cpu, gpu):
    assert nitro.set_fan_speed(cpu, gpu) is False
    assert read(root, NITRO + "/fan_speed") == "0,0\n"


@pytest.mark.parametrize("bad", ["abc", None, [1], 50.5, True, False, "50"])
def test_fan_speed_non_int_rejected(nitro, root, bad):
    """Floats/bools/strings are rejected instead of being written ("True,50")
    or raising TypeError."""
    assert nitro.set_fan_speed(bad, 50) is False
    assert nitro.set_fan_speed(50, bad) is False
    assert read(root, NITRO + "/fan_speed") == "0,0\n"


def test_fan_auto_written_in_single_write_with_newline(nitro, root, monkeypatch):
    """PR #261: "0,0" must reach the driver exactly like `echo "0,0" > fan_speed`."""
    Path(root, NITRO, "fan_speed").write_text("50,50\n")
    real_write = os.write
    writes = []

    def rec(fd, data):
        writes.append(bytes(data))
        return real_write(fd, data)
    monkeypatch.setattr(os, "write", rec)
    assert nitro.set_fan_speed(0, 0)
    assert writes == [b"0,0\n"]
    assert read(root, NITRO + "/fan_speed") == "0,0\n"


def test_write_file_strips_whitespace(nitro, root):
    p = str(Path(root, NITRO, "usb_charging"))
    assert nitro._write_file(p, "  10 \n")
    assert read(root, NITRO + "/usb_charging") == "10\n"


@pytest.mark.parametrize("content,mode", [("0,0\n", "auto"), ("50,60\n", "manual"),
                                          ("100,100\n", "max"), ("garbage\n", "unknown")])
def test_fan_mode_reported_from_driver(nitro, root, content, mode):
    """Issue #134: GUI needs the real fan mode after reboot."""
    Path(root, NITRO, "fan_speed").write_text(content)
    assert nitro.get_fan_mode() == mode
    assert nitro.get_all_settings()["fan_mode"] == mode


def test_hardware_writes_serialised_by_lock(nitro, root):
    done = threading.Event()
    with nitro.lock:
        t = threading.Thread(target=lambda: (nitro.set_fan_speed(10, 10), done.set()))
        t.start()
        assert not done.wait(0.2), "fan write did not wait for the manager lock"
    assert done.wait(2)
    t.join()
    assert read(root, NITRO + "/fan_speed") == "10,10\n"


def test_fan_speed_malformed_read(nitro, root):
    Path(root, NITRO, "fan_speed").write_text("garbage\n")
    assert nitro.get_fan_speed() == ("0", "0")


# ---------------------------------------------------------------- toggles
@pytest.mark.parametrize("feat", ["battery_limiter", "battery_calibration", "backlight_timeout"])
def test_toggles(nitro, root, feat):
    setter = getattr(nitro, "set_" + feat)
    assert setter(True) and read(root, f"{NITRO}/{feat}") == "1\n"
    assert setter(False) and read(root, f"{NITRO}/{feat}") == "0\n"
    assert setter(1) and read(root, f"{NITRO}/{feat}") == "1\n"
    assert setter(0) and read(root, f"{NITRO}/{feat}") == "0\n"


@pytest.mark.parametrize("bad", ["false", "true", "0", 2, None, 1.0])
def test_battery_limiter_rejects_non_boolean(nitro, root, bad):
    """The JSON string "false" used to be truthy and enabled the limiter."""
    assert nitro.set_battery_limiter(bad) is False
    assert read(root, NITRO + "/battery_limiter") == "0\n"


@pytest.mark.parametrize("lvl", [0, 10, 20, 30])
def test_usb_charging_valid(nitro, root, lvl):
    assert nitro.set_usb_charging(lvl)
    assert read(root, NITRO + "/usb_charging") == f"{lvl}\n"


@pytest.mark.parametrize("lvl", [5, -10, 40, "10", None, False, 10.0])
def test_usb_charging_invalid(nitro, root, lvl):
    assert nitro.set_usb_charging(lvl) is False


def test_unsupported_features_on_nitro_v15(nitro):
    assert nitro.set_lcd_override(True) is False
    assert nitro.set_boot_animation_sound(True) is False
    assert nitro.set_per_zone_mode("ff0000", "ff0000", "ff0000", "ff0000", 50) is False
    assert nitro.set_four_zone_mode(0, 0, 50, 1, 1, 2, 3) is False


def test_sysfs_write_error_returns_false(nitro, root):
    os.remove(Path(root, NITRO, "battery_limiter"))
    os.mkdir(Path(root, NITRO, "battery_limiter"))  # open(..,'w') -> IsADirectoryError
    assert nitro.set_battery_limiter(True) is False


# ---------------------------------------------------------------- keyboard
def test_per_zone_valid(nitro4z, root):
    assert nitro4z.set_per_zone_mode("ff0000", "00FF00", "0000ff", "123abc", 80)
    assert read(root, KB + "/per_zone_mode") == "ff0000,00FF00,0000ff,123abc,80\n"


@pytest.mark.parametrize("zone", ["zzzzzz", "fffff", "fffffff", "", "#fffff", "ff,ff0",
                                  "1_2345", " 12345", "-12345", "0x1234", "ff00ff\n", None, 123456])
def test_per_zone_invalid_hex(nitro4z, root, zone):
    """Only ^[0-9a-fA-F]{6}$ is accepted (int(x,16) allowed "_", "-", " ", "0x")."""
    assert nitro4z.set_per_zone_mode(zone, "000000", "000000", "000000", 50) is False
    assert read(root, KB + "/per_zone_mode") == "ff0000,00ff00,0000ff,ffffff,100\n"


@pytest.mark.parametrize("b", [-1, 101, True, 50.0, "50"])
def test_per_zone_bad_brightness(nitro4z, b):
    assert nitro4z.set_per_zone_mode("ff0000", "ff0000", "ff0000", "ff0000", b) is False


def test_four_zone_valid(nitro4z, root):
    assert nitro4z.set_four_zone_mode(3, 4, 80, 2, 10, 20, 30)
    assert read(root, KB + "/four_zone_mode") == "3,4,80,2,10,20,30\n"


@pytest.mark.parametrize("args", [
    (8, 0, 50, 1, 0, 0, 0), (-1, 0, 50, 1, 0, 0, 0), (0, 10, 50, 1, 0, 0, 0),
    (0, 0, 101, 1, 0, 0, 0), (0, 0, 50, 3, 0, 0, 0), (0, 0, 50, 1, 256, 0, 0),
    (0, 0, 50, 1, 0, -1, 0), (True, 0, 50, 1, 0, 0, 0), (0, 0, 50.0, 1, 0, 0, 0),
    (0, 0, 50, 1, "1", 0, 0),
])
def test_four_zone_invalid(nitro4z, args):
    assert nitro4z.set_four_zone_mode(*args) is False


def _fake_enek(root):
    Path(root, "dev/hidraw0").write_bytes(b"")
    d = Path(root, "sys/class/hidraw/hidraw0/device")
    d.mkdir(parents=True)
    (d / "uevent").write_text("HID_ID=0018:00000CF2:00005130\nHID_NAME=ENEK5130:00 0CF2:5130\n")


def test_enek_falls_back_to_sysfs_when_ioctl_fails(root, damx):
    build_fake_sysfs(root, four_zone=True)
    _fake_enek(root)
    m = damx.DAMXManager()
    assert "enek5130_hid_rgb" in m.available_features
    assert m.set_per_zone_mode("ff0000", "00ff00", "0000ff", "ffffff", 50)
    assert read(root, KB + "/per_zone_mode") == "ff0000,00ff00,0000ff,ffffff,50\n"


def test_enek_per_zone_0x_prefix_rejected_cleanly(root, damx):
    """'0x1234' used to pass validation and crash the ENEK path (zone[0:2]=='0x')."""
    build_fake_sysfs(root, four_zone=True)
    _fake_enek(root)
    m = damx.DAMXManager()
    assert m.set_per_zone_mode("0x1234", "000000", "000000", "000000", 50) is False
    assert read(root, KB + "/per_zone_mode") == "ff0000,00ff00,0000ff,ffffff,100\n"


# ---------------------------------------------------------------- modprobe
CONF = "etc/modprobe.d/linuwu-sense.conf"


def test_set_modprobe_parameter_writes_without_reloading(nitro, root, fake_sp):
    """The manager only persists; the server reloads the module after replying."""
    assert nitro.set_modprobe_parameter("nitro_v4")
    assert read(root, CONF) == "options linuwu_sense nitro_v4=1\n"
    assert stat.S_IMODE(os.stat(Path(root, CONF)).st_mode) == 0o644
    assert fake_sp.calls == []
    assert [p.name for p in Path(root, "etc/modprobe.d").iterdir()] == ["linuwu-sense.conf"]


def test_set_modprobe_parameter_rejects_injection(nitro, root):
    assert nitro.set_modprobe_parameter("nitro_v4=1\ninstall x /bin/sh") is False
    assert nitro.set_modprobe_parameter(None) is False
    assert not Path(root, CONF).exists()


def test_modprobe_write_is_atomic(nitro, root, monkeypatch):
    """A failure mid-update leaves the previous file intact and no temp files."""
    assert nitro.set_modprobe_parameter("predator_v4")

    def boom(*a):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "replace", boom)
    assert nitro.set_modprobe_parameter("nitro_v4") is False
    assert read(root, CONF) == "options linuwu_sense predator_v4=1\n"
    assert [p.name for p in Path(root, "etc/modprobe.d").iterdir()] == ["linuwu-sense.conf"]
    assert "(EIO)" in nitro.last_error


def test_modprobe_parameter_reread_from_disk(nitro, root):
    """Issues #191/#48/#118: the value shown must be what is on disk, not a cache."""
    assert nitro.get_modprobe_parameter() == ""
    Path(root, CONF).write_text("# managed by DAMX\noptions linuwu_sense enable_all=1\n")
    assert nitro.get_modprobe_parameter() == "enable_all"
    assert nitro.get_all_settings()["modprobe_parameter"] == "enable_all"
    Path(root, CONF).write_text("# nitro_v4 is only mentioned in a comment\n")
    assert nitro.get_modprobe_parameter() == ""


def test_remove_modprobe_parameter(nitro, root):
    assert nitro.set_modprobe_parameter("enable_all")
    assert nitro.remove_modprobe_parameter()
    assert not Path(root, CONF).exists()
    assert nitro.remove_modprobe_parameter()  # idempotent
    assert nitro.get_all_settings()["modprobe_parameter"] == ""
