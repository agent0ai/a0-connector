#!/usr/bin/env python3
"""Opt-in Linux executable/Secret Service smoke; never uses the desktop bus."""

import argparse
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import select
import struct
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = "nhliclifilepdkoolioacpjpijomfplj"
ORIGIN = f"chrome-extension://{EXTENSION}/"


@contextmanager
def process(args, env):
    child = subprocess.Popen(args, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        child.stdin.close()
        child.stdout.close()


def read_exact(pipe, length):
    result = bytearray()
    deadline = time.monotonic() + 12
    while len(result) < length:
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([pipe], [], [], remaining)[0], "Native response timed out"
        chunk = os.read(pipe.fileno(), length - len(result))
        assert chunk, "Native port closed before its response"
        result.extend(chunk)
    return bytes(result)


def request(child, method, params):
    request_id = secrets.token_hex(8)
    data = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
    child.stdin.write(struct.pack("=I", len(data)) + data)
    child.stdin.flush()
    length = struct.unpack("=I", read_exact(child.stdout, 4))[0]
    assert 0 < length <= 786432
    result = json.loads(read_exact(child.stdout, length))
    assert result.get("id") == request_id
    assert "result" in result, result.get("error", {}).get("code")
    return result["result"]


def hello(child, profile="linux-smoke-profile"):
    params = json.loads((ROOT / "tests/fixtures/native-rpc-v1/hello.valid.json").read_text())["params"]
    params["extension"].update(id=EXTENSION, version="0.1.1", install_instance_id=profile)
    return request(child, "bridge.hello", params)


def run(binary, previous_binary=None):
    assert os.uname().sysname == "Linux"
    assert binary.is_absolute() and binary.is_file()
    writer = previous_binary or binary
    assert writer.is_absolute() and writer.is_file()
    with tempfile.TemporaryDirectory(prefix="a0-linux-native-smoke-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        env = dict(os.environ)
        for variable in ("DISPLAY", "WAYLAND_DISPLAY", "GNOME_KEYRING_CONTROL", "GNOME_KEYRING_PID"):
            env.pop(variable, None)
        for variable, name in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                               ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime"), ("TMPDIR", "tmp")]:
            directory = root / name
            directory.mkdir(mode=0o700)
            env[variable] = str(directory)
        # New bus and keyring directories make the production service name safe in this test.
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={root / 'bus'}"
        env.pop("DBUS_STARTER_ADDRESS", None)
        env.pop("DBUS_STARTER_BUS_TYPE", None)
        bus = stack.enter_context(process(["/usr/bin/dbus-daemon", "--session", "--nofork", "--print-address=1",
                                           f"--address={env['DBUS_SESSION_BUS_ADDRESS']}"], env))
        announced = b""
        while not announced.endswith(b"\n") and len(announced) < 1024:
            announced += read_exact(bus.stdout, 1)
        assert announced.startswith(env["DBUS_SESSION_BUS_ADDRESS"].encode())
        subprocess.run(["/usr/bin/gdbus", "wait", "--session", "--timeout=5", "org.freedesktop.DBus"],
                       env=env, check=True, timeout=6, stdout=subprocess.DEVNULL)
        keyring = stack.enter_context(process(["/usr/bin/gnome-keyring-daemon", "--foreground", "--unlock",
                                               "--components=secrets", f"--control-directory={root / 'keyring-control'}"], env))
        keyring.stdin.write(secrets.token_hex(32).encode())
        keyring.stdin.close()
        subprocess.run(["/usr/bin/gdbus", "wait", "--session", "--timeout=5", "org.freedesktop.secrets"],
                       env=env, check=True, timeout=6, stdout=subprocess.DEVNULL)

        exchanges = []

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/plugins/_a0_connector/browser_bridge_exchange":
                    self.send_error(403)
                    return
                exchanges.append(body)
                response = {
                    "contract": "a0.browser-bridge.trust.v1", "trust_version": 1, "state": "paired",
                    "bridge_id": "bridge-linux-smoke", "server_instance_id": "server-linux-smoke",
                    "server_base_url": address, "subject_id": "single_user", "extension_id": EXTENSION,
                    "display_name": "Isolated Linux test", "key_generation": 1,
                    "scopes": ["bridge.connect", "context.list", "context.read", "context.message",
                               "browser.operate", "browser.control", "browser.artifact", "browser.approval"],
                    "protocols": {"connector": "a0-connector.v1", "browser": "a0.browser-bridge.v1",
                                  "adapter": "a0.browser-bridge.adapter.v1", "mv3_runtime": "a0.browser-bridge.mv3-runtime.v1"},
                    "policy": {"site_mode": "ask_per_site"}, "created_at_ms": 1,
                    "native_runtime_location": "user_browser_host", "docker_install_target": False,
                    "connector_session_ready": False, "browser_control_ready": False,
                }
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        address = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(server.server_close)
        stack.callback(server.shutdown)

        with process([str(writer), ORIGIN], env) as child:
            assert hello(child)["server"]["state"] == "unpaired"
            paired = request(child, "pairing.exchange", {
                "contract_version": 1, "pairing_code": "A0B1-DEADBEEF-" + "A" * 32,
                "server_base_origin": address,
            })
            assert paired["state"] == "paired"
            child.stdin.close()
            assert child.wait(timeout=3) == 0, "Clean native EOF failed"
        assert len(exchanges) == 1 and "public_key" in exchanges[0]
        assert not {"private_key", "private_seed", "api_key"} & exchanges[0].keys()
        with process([str(binary), ORIGIN], env) as child:
            saved = hello(child)
            assert saved["server"]["state"] == "paired"
            assert saved["negotiated"]["actions"] == [], "Pairing alone granted control"
            assert request(child, "pairing.status", {"contract_version": 1})["state"] == "paired"
        with process([str(binary), ORIGIN], env) as child:
            assert hello(child, "other-linux-profile")["server"]["state"] == "unpaired"
        with process([str(binary), ORIGIN], env) as child:
            hello(child)
            assert request(child, "pairing.disconnect", {"contract_version": 1})["state"] == "unpaired"
        with process([str(binary), ORIGIN], env) as child:
            assert hello(child)["server"]["state"] == "unpaired"
        unavailable = {**env, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root / 'missing-bus'}"}
        with process([str(binary), ORIGIN], unavailable) as child:
            assert hello(child)["server"]["state"] == "repair_required"
        rejected = subprocess.run([str(binary), "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"],
                                  input=b"", capture_output=True, env=env, timeout=3)
        assert rejected.returncode == 5 and rejected.stdout == b""
        assert list((root / "data" / "keyrings").glob("*.keyring")), "Expected isolated Secret Service storage"
        keyring.terminate()
        keyring.wait(timeout=3)
        for mode in ([], ["--locked-collection"]):
            with process([
                "/usr/bin/python3", "-u", str(ROOT / "tests/linux_locked_service.py"),
                "--address", env["DBUS_SESSION_BUS_ADDRESS"], *mode,
            ], env) as locked:
                subprocess.run(["/usr/bin/gdbus", "wait", "--session", "--timeout=5", "org.freedesktop.secrets"],
                               env=env, check=True, timeout=6, stdout=subprocess.DEVNULL)
                with process([str(binary), ORIGIN], env) as child:
                    assert hello(child)["server"]["state"] == "repair_required"
                    expected = b"search\nunlock\n" if mode else b"search\n"
                    assert read_exact(locked.stdout, len(expected)) == expected
                    assert not select.select([locked.stdout], [], [], 0)[0], "Native host opened a blocking unlock prompt"
                    child.stdin.close()
                    assert child.wait(timeout=3) == 0
        print(json.dumps({"platform": "linux", "architecture": os.uname().machine,
                          "native_framing": "passed", "secret_service_persistence": "passed",
                          "profile_isolation": "passed", "disconnect_cleanup": "passed",
                          "missing_keyring_denial": "passed", "wrong_origin_denial": "passed",
                          "locked_keyring_denial": "passed",
                          "locked_collection_denial": "passed",
                          "previous_binary_credentials": "passed" if previous_binary else "not_tested",
                          "core_admission": "not_tested", "published_release": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--previous-binary", type=Path)
    args = parser.parse_args()
    run(args.binary, args.previous_binary)
