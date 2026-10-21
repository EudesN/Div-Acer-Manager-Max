"""DaemonServer: framing, dispatcher robustness, authorization, permissions, lifecycle."""
import json
import os
import socket
import stat
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import BASE_REL, build_fake_sysfs, cmd_names, read, wait_for

NITRO = BASE_REL + "/nitro_sense"
CONF = "etc/modprobe.d/linuwu-sense.conf"


def _start(damx, mgr, **kw):
    srv = damx.DaemonServer(mgr, **kw)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    assert wait_for(lambda: srv.running and os.path.exists(damx.SOCKET_PATH))
    return srv, t


@pytest.fixture
def make_server(root, damx):
    build_fake_sysfs(root, four_zone=True)
    for p in [root, *Path(root).rglob("*")]:
        if p.is_dir():
            os.chmod(p, 0o755)
    started = []

    def make(**kw):
        srv, t = _start(damx, damx.DAMXManager(), **kw)
        started.append((srv, t))
        return srv
    yield make
    for srv, t in started:
        srv.stop()
        t.join(3)


@pytest.fixture
def server(make_server):
    return make_server()


def connect(damx):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(damx.SOCKET_PATH)
    return s


def recv_line(s):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def rpc(s, obj):
    """Newline-framed request (current protocol)."""
    s.sendall((json.dumps(obj) + "\n").encode() if not isinstance(obj, bytes) else obj)
    raw = recv_line(s)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1, raw
    return json.loads(raw)


def rpc_legacy(s, obj):
    """Old DAMXClient.cs: no newline, single recv."""
    s.sendall(json.dumps(obj).encode())
    return json.loads(s.recv(65536))


def recv_all(s, wait=0.4):
    """Collect everything the server sends within `wait` seconds."""
    s.settimeout(wait)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf


def as_peer(monkeypatch, damx, uid, gid=None, *, groups=None, primary_gid=None, name="alice"):
    """Fake SO_PEERCRED + user database. groups: {group name: (gid, [members])}."""
    gid = uid if gid is None else gid
    groups = groups or {}
    monkeypatch.setattr(damx, "get_peer_credentials", lambda sock: (4321, uid, gid))
    pw = types.SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid if primary_gid is None else primary_gid)

    def getpwuid(u):
        if u == uid:
            return pw
        raise KeyError(u)

    def getgrnam(n):
        if n in groups:
            g, members = groups[n]
            return types.SimpleNamespace(gr_name=n, gr_gid=g, gr_mem=list(members))
        raise KeyError(n)
    monkeypatch.setattr(damx, "pwd", types.SimpleNamespace(getpwuid=getpwuid))
    monkeypatch.setattr(damx, "grp", types.SimpleNamespace(getgrnam=getgrnam))


# ------------------------------------------------------------- happy path
def test_get_version(server, damx):
    with connect(damx) as s:
        r = rpc(s, {"command": "get_version"})
    assert r == {"success": True, "data": {"version": damx.VERSION}}


def test_ping(server, damx):
    """DAMXClient.ValidateConnection() sends 'ping'."""
    with connect(damx) as s:
        r = rpc(s, {"command": "ping", "params": {}})
        assert r == {"success": True, "data": {"pong": True, "version": damx.VERSION}}
        assert rpc_legacy(s, {"command": "ping"})["data"]["pong"] is True


def test_get_all_settings_fits_client_buffer(server, damx):
    with connect(damx) as s:
        s.sendall(json.dumps({"command": "get_all_settings", "params": {}}).encode())
        raw = recv_all(s)
    assert len(raw) < 4096  # DAMXClient.cs reads a single 4096-byte buffer
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    r = json.loads(raw)
    assert r["success"] and r["data"]["laptop_type"] == "NITRO"
    assert r["data"]["driver_loaded"] is True and r["data"]["fan_mode"] == "auto"


def test_get_supported_features_reports_status(server, damx):
    with connect(damx) as s:
        d = rpc(s, {"command": "get_supported_features"})["data"]
    assert d["laptop_type"] == "NITRO" and d["driver_loaded"] is True and d["driver_status"] == "ok"


def test_reduced_mode_status_via_socket(root, damx):
    build_fake_sysfs(root, module_loaded=False)
    srv, t = _start(damx, damx.DAMXManager())
    try:
        with connect(damx) as s:
            d = rpc(s, {"command": "get_all_settings"})["data"]
            f = rpc(s, {"command": "get_supported_features"})["data"]
            fan = rpc(s, {"command": "set_fan_speed", "params": {"cpu": 1, "gpu": 1}})
    finally:
        srv.stop()
        t.join(3)
    assert d["laptop_type"] == "UNKNOWN" and d["driver_loaded"] is False
    assert f["driver_status"] == "not_loaded" and f["available_features"] == ["thermal_profile"]
    assert fan == {"success": False, "error": "Fan speed control is not supported on this device"}


def test_set_fan_speed_via_socket(server, damx, root):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_fan_speed", "params": {"cpu": 40, "gpu": 70}})
        assert r["success"] and read(root, NITRO + "/fan_speed") == "40,70\n"
        r = rpc(s, {"command": "set_fan_speed", "params": {"cpu": 0, "gpu": 0}})
    assert r["success"] and read(root, NITRO + "/fan_speed") == "0,0\n"


@pytest.mark.parametrize("params", [{"cpu": "abc", "gpu": 1}, {"cpu": None, "gpu": 1},
                                    {"cpu": -1, "gpu": 1}, {"cpu": 1, "gpu": 101},
                                    {"cpu": True, "gpu": 1}, {"cpu": 50.5, "gpu": 1},
                                    {"cpu": 50}, {}])
def test_set_fan_speed_bad_values_via_socket(server, damx, root, params):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_fan_speed", "params": params})
        assert r["success"] is False and "error" in r
        assert rpc(s, {"command": "get_version"})["success"]  # connection survives
    assert read(root, NITRO + "/fan_speed") == "0,0\n"


def test_keyboard_via_socket(server, damx, root):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_per_zone_mode",
                    "params": {"zone1": "ff0000", "zone2": "GGGGGG", "zone3": "0", "zone4": "0", "brightness": 5}})
        assert r["success"] is False and "zone2" in r["error"]
        r = rpc(s, {"command": "set_four_zone_mode",
                    "params": {"mode": 9, "speed": 1, "brightness": 50, "direction": 1,
                               "red": 0, "green": 0, "blue": 0}})
        assert r["success"] is False
        r = rpc(s, {"command": "set_per_zone_mode", "params": {"zone1": 123456}})
        assert r["success"] is False
        r = rpc(s, {"command": "set_per_zone_mode",
                    "params": {"zone1": "ff0000", "zone2": "00ff00", "zone3": "0000ff", "zone4": "ffffff",
                               "brightness": 40}})
        assert r["success"] is True
    assert read(root, BASE_REL + "/four_zoned_kb/per_zone_mode") == "ff0000,00ff00,0000ff,ffffff,40\n"


def test_thermal_profile_via_socket(server, damx, root):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_thermal_profile", "params": {"profile": "turbo"}})
        assert r["success"] is False and "available: low-power, balanced" in r["error"]
        r = rpc(s, {"command": "set_thermal_profile", "params": {"profile": "low-power"}})
    assert r == {"success": True, "data": {"profile": "low-power"}}


# ------------------------------------------------------------- validation
@pytest.mark.parametrize("command,params,missing", [
    ("set_battery_limiter", {}, "enabled"),
    ("set_backlight_timeout", {}, "enabled"),
    ("set_thermal_profile", {}, "profile"),
    ("set_usb_charging", {}, "level"),
    ("set_per_zone_mode", {"brightness": 50}, "zone1"),
    ("set_per_zone_mode", {"zone1": "ff0000", "zone2": "ff0000", "zone3": "ff0000", "zone4": "ff0000"},
     "brightness"),
    ("set_four_zone_mode", {"mode": 0}, "speed"),
])
def test_missing_params_rejected(server, damx, root, command, params, missing):
    """Missing params used to default silently (limiter OFF, zones 000000)."""
    Path(root, NITRO, "battery_limiter").write_text("1\n")
    with connect(damx) as s:
        r = rpc(s, {"command": command, "params": params})
    assert r == {"success": False, "error": f"missing required parameter '{missing}'"}
    assert read(root, NITRO + "/battery_limiter") == "1\n"
    assert read(root, BASE_REL + "/four_zoned_kb/per_zone_mode") == "ff0000,00ff00,0000ff,ffffff,100\n"


@pytest.mark.parametrize("value", ["false", "true", 2, None, [], 0.0])
def test_boolean_params_must_be_bool(server, damx, root, value):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_battery_limiter", "params": {"enabled": value}})
    assert r == {"success": False, "error": "parameter 'enabled' must be a boolean (true/false)"}
    assert read(root, NITRO + "/battery_limiter") == "0\n"


@pytest.mark.parametrize("value,expected", [(True, "1\n"), (False, "0\n"), (1, "1\n"), (0, "0\n")])
def test_boolean_params_accepted(server, damx, root, value, expected):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_battery_limiter", "params": {"enabled": value}})
    assert r["success"] is True and read(root, NITRO + "/battery_limiter") == expected


# ------------------------------------------------------------- robustness
def test_unknown_command(server, damx):
    with connect(damx) as s:
        r = rpc(s, {"command": "rm_rf"})
    assert r["success"] is False and "Unknown command" in r["error"]


@pytest.mark.parametrize("payload", [b"{not json", b"\xff\xfe\x00", b"[1,2]", b'"str"', b"42",
                                     b'{"command":"set_fan_speed","params":null}',
                                     b'{"command":"set_fan_speed","params":[1,2]}',
                                     b'{"command":null}'])
@pytest.mark.parametrize("newline", [False, True])
def test_malformed_requests_get_error_and_connection_survives(server, damx, payload, newline):
    with connect(damx) as s:
        s.sendall(payload + (b"\n" if newline else b""))
        r = json.loads(recv_line(s))
        assert r["success"] is False and r["error"]
        assert rpc(s, {"command": "get_version"})["success"]


def test_two_messages_in_one_recv(server, damx):
    with connect(damx) as s:
        s.sendall(b'{"command":"get_version"}\n{"command":"ping"}\n')
        raw = recv_all(s)
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 3, raw
    assert [json.loads(x)["success"] for x in lines[:2]] == [True, True]
    assert "pong" in json.loads(lines[1])["data"]


def test_two_legacy_messages_in_one_recv(server, damx):
    """Old clients without newline whose requests got coalesced."""
    with connect(damx) as s:
        s.sendall(b'{"command":"get_version"}{"command":"get_version"}')
        raw = recv_all(s)
    assert raw.count(b'"success": true') == 2 and raw.count(b"\n") == 2, raw


@pytest.mark.parametrize("newline", [False, True])
def test_request_split_across_recvs(server, damx, newline):
    with connect(damx) as s:
        s.sendall(b'{"command":"get_')
        time.sleep(0.2)
        s.sendall(b'version"}' + (b"\n" if newline else b""))
        raw = recv_all(s)
    assert raw.startswith(b'{"success": true') and raw.count(b"\n") == 1, raw


def test_request_remainder_kept_between_lines(server, damx):
    with connect(damx) as s:
        s.sendall(b'{"command":"get_version"}\n{"command":"pi')
        first = recv_line(s)
        s.sendall(b'ng"}\n')
        second = recv_line(s)
    assert json.loads(first)["data"] == {"version": damx.VERSION}
    assert json.loads(second)["data"]["pong"] is True


def test_large_message_single_reply(server, damx):
    """>4096-byte request used to be chopped into several 'Invalid JSON' replies."""
    big = json.dumps({"command": "set_thermal_profile", "params": {"profile": "x" * 20000}}).encode()
    for payload in (big, big + b"\n"):
        with connect(damx) as s:
            s.sendall(payload)
            raw = recv_all(s)
        assert raw.count(b'"success"') == 1, raw[:200]
        assert len(raw) < 1024  # the bogus value is not echoed back in full


@pytest.mark.parametrize("newline", [False, True])
def test_oversized_message_rejected_and_closed(server, damx, newline):
    payload = b'{"command":"get_version","pad":"' + b"x" * (70 * 1024) + b'"}' + (b"\n" if newline else b"")
    with connect(damx) as s:
        try:
            s.sendall(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        raw = recv_all(s, wait=2)
    assert raw.count(b"\n") == 1
    r = json.loads(raw)
    assert r == {"success": False, "error": f"message too large (limit {damx.MAX_MESSAGE_SIZE} bytes)"}


def test_responses_are_single_lines(server, damx):
    with connect(damx) as s:
        r = rpc(s, {"command": "set_thermal_profile", "params": {"profile": "a\nb"}})
    assert r["success"] is False


# ------------------------------------------------------------- security: socket
def test_socket_world_accessible_without_damx_group(root, damx, monkeypatch, make_server):
    monkeypatch.setattr(damx, "grp", types.SimpleNamespace(getgrnam=lambda n: (_ for _ in ()).throw(KeyError(n))))
    make_server()
    st = os.stat(damx.SOCKET_PATH)
    assert stat.S_IMODE(st.st_mode) == 0o666


@pytest.mark.skipif(os.geteuid() != 0, reason="chown to another group needs root")
def test_socket_restricted_to_damx_group(root, damx, monkeypatch, make_server):
    fake = types.SimpleNamespace(gr_gid=4242, gr_mem=[], gr_name="damx")
    monkeypatch.setattr(damx, "grp", types.SimpleNamespace(
        getgrnam=lambda n: fake if n == "damx" else (_ for _ in ()).throw(KeyError(n))))
    make_server()
    st = os.stat(damx.SOCKET_PATH)
    assert stat.S_IMODE(st.st_mode) == 0o660
    assert (st.st_uid, st.st_gid) == (0, 4242)


# ------------------------------------------------------------- security: authorization
def test_privileged_command_set(damx):
    assert damx.PRIVILEGED_COMMANDS == {
        "force_nitro_model", "force_predator_model", "force_enable_all",
        "set_modprobe_parameter_predator", "set_modprobe_parameter_nitro",
        "set_modprobe_parameter_enable_all", "remove_modprobe_parameter",
        "restart_daemon", "restart_drivers_and_daemon"}
    for c in ("force_anything", "restart_x", "set_modprobe_parameter_foo"):
        assert damx.is_privileged_command(c)
    for c in ("get_modprobe_parameter", "set_fan_speed", "set_thermal_profile", "ping",
              "get_all_settings", "set_battery_limiter", "set_per_zone_mode"):
        assert not damx.is_privileged_command(c)


@pytest.mark.parametrize("command", sorted([
    "force_nitro_model", "force_predator_model", "force_enable_all",
    "set_modprobe_parameter_predator", "set_modprobe_parameter_nitro",
    "set_modprobe_parameter_enable_all", "remove_modprobe_parameter",
    "restart_daemon", "restart_drivers_and_daemon"]))
def test_unprivileged_peer_denied(server, damx, root, fake_sp, monkeypatch, command):
    as_peer(monkeypatch, damx, 1000, groups={"wheel": (10, ["bob"]), "damx": (977, [])})
    Path(root, CONF).write_text("options linuwu_sense nitro_v4=1\n")
    with connect(damx) as s:
        r = rpc(s, {"command": command})
    assert r == {"success": False, "error": "permission denied: add your user to the 'damx' group"}
    time.sleep(0.1)
    assert fake_sp.calls == []
    assert read(root, CONF) == "options linuwu_sense nitro_v4=1\n"


def test_unprivileged_peer_can_use_hardware_commands(server, damx, root, monkeypatch):
    as_peer(monkeypatch, damx, 1000)
    with connect(damx) as s:
        assert rpc(s, {"command": "set_fan_speed", "params": {"cpu": 30, "gpu": 30}})["success"]
        assert rpc(s, {"command": "set_thermal_profile", "params": {"profile": "low-power"}})["success"]
        assert rpc(s, {"command": "set_battery_limiter", "params": {"enabled": True}})["success"]
        assert rpc(s, {"command": "get_all_settings"})["success"]
        assert rpc(s, {"command": "get_modprobe_parameter"})["success"]


@pytest.mark.parametrize("kind", ["root", "member_wheel", "member_damx", "primary_sudo", "member_admin"])
def test_privileged_peers_allowed(server, damx, root, fake_sp, monkeypatch, kind):
    if kind == "root":
        as_peer(monkeypatch, damx, 0)
    elif kind == "member_wheel":
        as_peer(monkeypatch, damx, 1000, groups={"wheel": (10, ["alice"])})
    elif kind == "member_damx":
        as_peer(monkeypatch, damx, 1000, groups={"damx": (977, ["alice"])})
    elif kind == "primary_sudo":
        as_peer(monkeypatch, damx, 1000, gid=1000, primary_gid=27, groups={"sudo": (27, [])})
    else:
        as_peer(monkeypatch, damx, 1000, groups={"admin": (115, ["alice"])})
    with connect(damx) as s:
        r = rpc(s, {"command": "set_modprobe_parameter_nitro"})
    assert r["success"] is True and r["data"]["parameter"] == "nitro_v4"
    assert read(root, CONF) == "options linuwu_sense nitro_v4=1\n"
    assert wait_for(lambda: any(os.path.basename(c[0]) == "modprobe" for c in fake_sp.calls))


def test_peer_effective_gid_membership(damx, monkeypatch):
    as_peer(monkeypatch, damx, 1000, gid=977, primary_gid=1000, groups={"damx": (977, [])})
    assert damx.is_privileged_user(1000, 977) is True
    assert damx.is_privileged_user(1000, 1000) is False
    assert damx.is_privileged_user(0, 0) is True
    assert damx.is_privileged_user(-1, -1) is False


def test_peercred_failure_is_unprivileged(server, damx, fake_sp, monkeypatch):
    def broken(sock):
        raise OSError(95, "Operation not supported")
    monkeypatch.setattr(damx, "get_peer_credentials", broken)
    with connect(damx) as s:
        r = rpc(s, {"command": "restart_daemon"})
        assert r["error"] == damx.PERMISSION_DENIED_ERROR
        assert rpc(s, {"command": "ping"})["success"]


def test_real_peer_credentials(server, damx):
    with connect(damx) as s:
        # our own process is the peer of the client socket
        conn_creds = damx.get_peer_credentials(s)
    assert conn_creds[1] == os.geteuid() and conn_creds[0] > 0


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to drop to nobody")
def test_real_unprivileged_user_cannot_rewrite_modprobe(server, damx, root, fake_sp):
    """uid 65534 (nobody) connects and asks root to rewrite /etc/modprobe.d."""
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: become nobody and send the command
        try:
            os.close(r_fd)
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(damx.SOCKET_PATH)
            s.sendall(b'{"command":"set_modprobe_parameter_enable_all"}\n')
            os.write(w_fd, recv_line(s))
            os._exit(0)
        except BaseException:
            os._exit(3)
    os.close(w_fd)
    _, status = os.waitpid(pid, 0)
    reply = os.read(r_fd, 65536)
    os.close(r_fd)
    assert os.WEXITSTATUS(status) == 0, "child could not talk to the socket"
    assert json.loads(reply) == {"success": False, "error": damx.PERMISSION_DENIED_ERROR}
    assert not Path(root, CONF).exists()
    assert fake_sp.calls == []


# ------------------------------------------------------------- deferred restarts
class BlockingSubprocess:
    """Fake subprocess whose run() blocks until released."""

    def __init__(self, base):
        self.base = base
        self.calls = base.calls
        self.entered = threading.Event()
        self.release = threading.Event()
        for k in ("CalledProcessError", "TimeoutExpired", "DEVNULL", "PIPE"):
            setattr(self, k, getattr(base, k))

    def run(self, cmd, *a, **kw):
        self.entered.set()
        self.release.wait(5)
        return self.base.run(cmd, *a, **kw)


def test_restart_replies_before_restarting(server, damx, fake_sp, monkeypatch):
    blocking = BlockingSubprocess(fake_sp)
    monkeypatch.setattr(damx, "subprocess", blocking)
    with connect(damx) as s:
        r = rpc(s, {"command": "restart_drivers_and_daemon"})
        assert r["success"] is True and r["data"]["scheduled"] is True
        assert blocking.entered.wait(2)  # restart runs after the reply was received
        r2 = rpc(s, {"command": "restart_daemon"})  # coalesced while in progress
        assert r2["success"] is True and r2["data"]["scheduled"] is False
        blocking.release.set()
        assert rpc(s, {"command": "ping"})["success"]
    assert wait_for(lambda: len(fake_sp.calls) == 3)
    assert cmd_names(fake_sp.calls) == [
        ["rmmod", "linuwu_sense"], ["modprobe", "linuwu_sense"],
        ["systemctl", "--no-block", "restart", "damx-daemon.service"]]
    assert all(c[0].startswith("/") for c in fake_sp.calls[2:]), "systemctl must be a full path"
    assert not any("sudo" in c for c in fake_sp.calls)


def test_force_model_reloads_with_parameter(server, damx, fake_sp):
    with connect(damx) as s:
        r = rpc(s, {"command": "force_nitro_model"})
    assert r["success"] and r["data"] == {"scheduled": True, "parameter": "nitro_v4"}
    assert wait_for(lambda: len(fake_sp.calls) == 2)
    assert cmd_names(fake_sp.calls) == [["rmmod", "linuwu_sense"], ["modprobe", "linuwu_sense", "nitro_v4=1"]]


def test_restart_daemon_uses_systemctl_only(server, damx, fake_sp):
    with connect(damx) as s:
        assert rpc(s, {"command": "restart_daemon"})["success"]
    assert wait_for(lambda: len(fake_sp.calls) == 1)
    assert cmd_names(fake_sp.calls) == [["systemctl", "--no-block", "restart", "damx-daemon.service"]]


def test_modprobe_parameter_persisted_then_applied(server, damx, root, fake_sp):
    """Issues #191/#48/#118: value must survive and be reported from disk."""
    with connect(damx) as s:
        r = rpc(s, {"command": "set_modprobe_parameter_predator"})
        assert r["success"] and r["data"]["parameter"] == "predator_v4"
        assert wait_for(lambda: len(fake_sp.calls) == 2)
        assert rpc(s, {"command": "get_modprobe_parameter"})["data"] == {"parameter": "predator_v4"}
        assert rpc(s, {"command": "get_all_settings"})["data"]["modprobe_parameter"] == "predator_v4"
        # changed behind the daemon's back
        Path(root, CONF).write_text("options linuwu_sense enable_all=1\n")
        assert rpc(s, {"command": "get_modprobe_parameter"})["data"] == {"parameter": "enable_all"}
        r = rpc(s, {"command": "remove_modprobe_parameter"})
        assert r["success"] and r["data"] == {"parameter": ""}
        assert rpc(s, {"command": "get_all_settings"})["data"]["modprobe_parameter"] == ""
    assert cmd_names(fake_sp.calls) == [["rmmod", "linuwu_sense"], ["modprobe", "linuwu_sense"]]


def test_no_stray_print_in_get_modprobe_parameter(server, damx, capsys):
    with connect(damx) as s:
        rpc(s, {"command": "get_modprobe_parameter"})
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------- resources / lifecycle
def test_client_list_pruned(server, damx):
    for _ in range(25):
        with connect(damx) as s:
            rpc(s, {"command": "get_version"})
    assert wait_for(lambda: len(server.clients) == 0), f"{len(server.clients)} dead clients retained"


def test_client_cap(make_server, damx):
    srv = make_server(max_clients=2)
    a, b = connect(damx), connect(damx)
    try:
        assert rpc(a, {"command": "ping"})["success"] and rpc(b, {"command": "ping"})["success"]
        with connect(damx) as c:
            r = json.loads(recv_line(c))
            assert r == {"success": False, "error": "too many clients connected"}
            assert c.recv(10) == b""
        assert len(srv.clients) == 2
    finally:
        a.close()
        b.close()
    assert wait_for(lambda: len(srv.clients) == 0)
    with connect(damx) as d:
        assert rpc(d, {"command": "ping"})["success"]


def test_idle_client_disconnected(make_server, damx):
    srv = make_server(idle_timeout=0.3)
    with connect(damx) as s:
        assert rpc(s, {"command": "ping"})["success"]
        s.settimeout(3)
        assert s.recv(10) == b""  # server closed the idle connection
    assert wait_for(lambda: len(srv.clients) == 0)


def test_stop_removes_socket_file(server, damx):
    assert os.path.exists(damx.SOCKET_PATH)
    server.stop()
    assert not os.path.exists(damx.SOCKET_PATH)


def test_stale_socket_file_replaced(root, damx):
    build_fake_sysfs(root)
    Path(damx.SOCKET_PATH).write_text("stale")
    srv, t = _start(damx, damx.DAMXManager())
    assert stat.S_ISSOCK(os.stat(damx.SOCKET_PATH).st_mode)
    srv.stop()
    t.join(3)


def test_stale_unix_socket_replaced(root, damx):
    build_fake_sysfs(root)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(damx.SOCKET_PATH)
    dead.close()  # socket file left behind, nobody listening
    srv, t = _start(damx, damx.DAMXManager())
    with connect(damx) as s:
        assert rpc(s, {"command": "ping"})["success"]
    srv.stop()
    t.join(3)


def test_live_daemon_socket_not_stolen(server, damx):
    second = damx.DaemonServer(server.manager)
    assert second.bind() is False
    second.stop()  # must not remove the first daemon's socket
    assert os.path.exists(damx.SOCKET_PATH)
    with connect(damx) as s:
        assert rpc(s, {"command": "ping"})["success"]


def _no_signals(monkeypatch, damx):
    monkeypatch.setattr(damx, "signal", types.SimpleNamespace(
        signal=lambda *a: None, SIGTERM=15, SIGINT=2, SIGHUP=1))


def test_second_daemon_exits_nonzero(server, damx, monkeypatch):
    _no_signals(monkeypatch, damx)
    d = damx.DAMXDaemon()
    assert d.setup()
    with pytest.raises(SystemExit) as e:
        d.run()
    assert e.value.code == 1
    assert os.path.exists(damx.SOCKET_PATH)
    assert not os.path.exists(damx.PID_FILE)


@pytest.mark.parametrize("signum", [15, 2, 1])
def test_daemon_run_signal_cleanup(root, damx, monkeypatch, signum):
    """SIGTERM/SIGINT/SIGHUP -> accept loop exits -> socket and PID file removed."""
    build_fake_sysfs(root)
    registered = {}
    monkeypatch.setattr(damx, "signal", types.SimpleNamespace(
        signal=lambda s, h: registered.__setitem__(s, h), SIGTERM=15, SIGINT=2, SIGHUP=1))
    d = damx.DAMXDaemon()
    assert d.setup()
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    assert wait_for(lambda: os.path.exists(damx.PID_FILE) and set(registered) == {1, 2, 15})
    assert os.path.exists(damx.SOCKET_PATH)
    assert read(root, "var/run/DAMX-Daemon.pid") == str(os.getpid())
    assert d.power_monitor.is_running()
    registered[signum](signum, None)
    t.join(3)
    assert not t.is_alive()
    assert not os.path.exists(damx.SOCKET_PATH)
    assert not os.path.exists(damx.PID_FILE)
    assert not d.power_monitor.is_running()


def test_bind_failure_exits_nonzero(root, damx, monkeypatch):
    """systemd must see a failure, not a clean exit."""
    build_fake_sysfs(root)
    _no_signals(monkeypatch, damx)
    monkeypatch.setattr(damx, "SOCKET_PATH", str(Path(root, "nonexistent/dir/DAMX.sock")))
    d = damx.DAMXDaemon()
    assert d.setup()
    with pytest.raises(SystemExit) as e:
        d.run()
    assert e.value.code == 1


def test_main_requires_root(damx, monkeypatch):
    monkeypatch.setattr(damx.sys, "argv", ["DAMX-Daemon"])
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as e:
        damx.main()
    assert e.value.code == 1


def test_main_setup_failure_exits_nonzero(root, damx, monkeypatch):
    monkeypatch.setattr(damx.sys, "argv", ["DAMX-Daemon"])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(damx.DAMXDaemon, "setup", lambda self: False)
    with pytest.raises(SystemExit) as e:
        damx.main()
    assert e.value.code == 1


def test_no_dead_module_level_signal_handler(damx):
    assert not hasattr(damx, "signal_handler")
