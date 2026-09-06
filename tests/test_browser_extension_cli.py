from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_zero_cli import browser_extension


_REAL_COLLECT_SERVER_STATUS = browser_extension._collect_server_status


def _options(command: str, **overrides: object) -> browser_extension.BrowserExtensionOptions:
    values: dict[str, object] = {
        "command": command,
        "json_output": True,
    }
    values.update(overrides)
    return browser_extension.BrowserExtensionOptions(**values)


def _status_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract": "a0.browser-bridge.status.v1",
        "schema_version": 1,
        "companion_version": "2.11.0",
        "state": "not_installed",
        "reason_code": "INSTALL_STATE_MISSING",
        "platform": "macos",
        "architecture": "aarch64",
        "install_root": "resolved",
        "release_trust": "not_configured",
        "native_host": "disabled",
        "registered_browser_count": 0,
    }
    payload.update(overrides)
    return payload


def _install_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract": "a0.browser-bridge.install-plan.v1",
        "schema_version": 1,
        "companion_version": "2.11.0",
        "install_contract": "a0.browser-bridge.install.v1",
        "operation": "install",
        "state": "blocked",
        "reason_code": "RELEASE_EVIDENCE_UNAVAILABLE",
        "mutation_allowed": False,
        "catalog": "not_verified",
        "artifact": "not_verified",
        "platform_signature": "not_verified",
        "platform": "macos",
        "architecture": "aarch64",
        "install_root": "resolved",
        "target_browsers": ["chrome", "edge"],
        "registration_count": 0,
        "rollback": "not_started",
    }
    payload.update(overrides)
    return payload


def test_completed_native_install_requires_full_verified_result() -> None:
    payload = _install_payload(
        state="installed", reason_code="INSTALL_VERIFIED", mutation_allowed=True,
        catalog="verified", artifact="verified", platform_signature="verified",
        registration_count=1, rollback="not_needed",
    )
    assert browser_extension._companion_payload_matches_schema("install", 0, payload)
    for field, value in (("artifact", "not_verified"), ("registration_count", 0), ("mutation_allowed", False)):
        changed = {**payload, field: value}
        assert not browser_extension._companion_payload_matches_schema("install", 0, changed)


@pytest.mark.parametrize("command,exit_code,state,reason,cleanup,disposition,count", [
    ("repair", 0, "repaired", "REPAIR_VERIFIED", "not_attempted", "repaired", 1),
    ("repair", 6, "blocked", "REPAIR_RECOVERY_REQUIRED", "not_attempted", "recovery_required", 0),
    ("uninstall", 4, "action_required", "LOCAL_RETIREMENT_CONFIRMATION_REQUIRED", "not_attempted", "unchanged", 0),
    ("uninstall", 6, "cleanup_pending", "CREDENTIAL_CLEANUP_PENDING", "pending", "registrations_retired_recoverable", 1),
    ("uninstall", 6, "cleanup_pending", "PROFILE_REVOCATION_REQUIRED", "pending", "unchanged", 1),
    ("uninstall", 6, "cleanup_pending", "LOCAL_RETIREMENT_RECOVERY_REQUIRED", "pending", "recovery_required", 0),
])
def test_lifecycle_receipt_preserves_pending_cleanup(command, exit_code, state, reason, cleanup, disposition, count):
    payload = {"contract": "a0.browser-bridge.lifecycle.v1", "schema_version": 1,
               "companion_version": "2.12.0", "operation": command, "state": state,
               "reason_code": reason, "registration_count": count,
               "credential_cleanup": cleanup, "disposition": disposition}
    assert browser_extension._parse_companion_json(command, exit_code, json.dumps(payload)) == payload
    for change in ({"registration_count": True}, {"registration_count": 100},
                   {"credential_cleanup": "complete"}, {"host_path": "/private/not-allowed"},
                   {"reason_code": "raw server message"}):
        with pytest.raises(browser_extension._BrowserExtensionFailure):
            browser_extension._parse_companion_json(command, exit_code, json.dumps({**payload, **change}))
    if exit_code != 0:
        with pytest.raises(browser_extension._BrowserExtensionFailure):
            browser_extension._parse_companion_json(command, 0, json.dumps(payload))


def test_uninstall_pending_inventory_cannot_claim_removal_or_completion():
    payload = {"contract": "a0.browser-bridge.lifecycle.v1", "schema_version": 1,
               "companion_version": "2.12.0", "operation": "uninstall",
               "state": "cleanup_pending", "reason_code": "PROFILE_REVOCATION_REQUIRED",
               "registration_count": 1, "credential_cleanup": "pending", "disposition": "unchanged"}
    for change in ({"registration_count": 0}, {"disposition": "registrations_retired_recoverable"},
                   {"credential_cleanup": "complete"}, {"reason_code": "CREDENTIAL_INVENTORY_UNAVAILABLE"}):
        assert not browser_extension._companion_payload_matches_schema("uninstall", 6, {**payload, **change})
    assert not browser_extension._companion_payload_matches_schema("repair", 6, payload)


@pytest.mark.parametrize("command", ["install", "repair"])
def test_fresh_install_invokes_only_retained_verified_bootstrap(monkeypatch, capsys, command):
    from contextlib import contextmanager
    from agent_zero_cli import browser_extension_bootstrap
    events = []
    class Verified:
        path = "/private/fixture/a0-browser-bridge"
        descriptor = 99
        def verify(self):
            events.append("verify")
    @contextmanager
    def acquire():
        events.append("acquire")
        try:
            yield Verified()
        finally:
            events.append("cleanup")
    def resolve():
        if command == "repair":
            raise browser_extension._BrowserExtensionFailure(5, "COMPANION_STATE_INVALID", "Damaged fixture")
        return None
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", resolve)
    monkeypatch.setattr(browser_extension_bootstrap, "acquire_bootstrap", acquire)
    def run(argv, **kwargs):
        events.append("run")
        assert argv == [Verified.path, command, "--browser", "auto", "--json"]
        assert kwargs["timeout_seconds"] == 600 and kwargs["stdin_payload"] is None
        if command == "repair":
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "contract": "a0.browser-bridge.lifecycle.v1", "schema_version": 1,
                "companion_version": "2.12.0", "operation": "repair", "state": "repaired",
                "reason_code": "REPAIR_VERIFIED", "registration_count": 1,
                "credential_cleanup": "not_attempted", "disposition": "repaired",
            }), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(_install_payload(
            state="installed", reason_code="INSTALL_VERIFIED", mutation_allowed=True,
            catalog="verified", artifact="verified", platform_signature="verified",
            registration_count=1, rollback="not_needed",
        )), "")
    monkeypatch.setattr(browser_extension, "_run_process", run)
    assert browser_extension.run_browser_extension(_options(command)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and "/private/fixture" not in json.dumps(result)
    assert events == ["acquire", "verify", "run", "verify", "cleanup"]


def _remote_foundation_status() -> dict[str, object]:
    return {
        "scope": "extension_bridge_foundation",
        "foundation_contract": "a0.browser-bridge.foundation.v1",
        "status_contract": "a0.browser-bridge.status.v1",
        "gate": {
            "gate_contract": "a0.browser-bridge.rollout.v1",
            "state": "preview",
            "reason_code": "rollout_preview",
            "configured": True,
        },
        "selection": {
            "state": "not_selected",
            "reason_code": "host_selection_automatic",
        },
        "layers": {
            "server": {
                "state": "configured",
                "reason_code": "rollout_preview",
            }
        },
    }


@pytest.fixture(autouse=True)
def _stub_remote_server_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_checked(host: str, *, remember_session: bool) -> browser_extension._ServerProbe:
        del host, remember_session
        return browser_extension._ServerProbe(
            "not_checked",
            "test_remote_not_checked",
            "not_checked",
        )

    monkeypatch.setattr(browser_extension, "_collect_server_status", not_checked)
    monkeypatch.setattr(
        "agent_zero_cli.config.load_config",
        lambda: SimpleNamespace(instance_url="", remember_host=False),
    )


@pytest.mark.parametrize(
    ("command", "expected_exit", "expected_code"),
    [
        ("install", browser_extension.EXIT_UNAVAILABLE, "COMPANION_CATALOG_UNAVAILABLE"),
        ("repair", browser_extension.EXIT_UNAVAILABLE, "COMPANION_CATALOG_UNAVAILABLE"),
        ("status", browser_extension.EXIT_NOT_INSTALLED, "COMPANION_NOT_INSTALLED"),
        ("doctor", browser_extension.EXIT_NOT_INSTALLED, "COMPANION_NOT_INSTALLED"),
        ("pair", browser_extension.EXIT_NOT_INSTALLED, "COMPANION_NOT_INSTALLED"),
        ("update", browser_extension.EXIT_NOT_INSTALLED, "COMPANION_NOT_INSTALLED"),
        ("uninstall", browser_extension.EXIT_NOT_INSTALLED, "COMPANION_NOT_INSTALLED"),
    ],
)
def test_missing_companion_fails_closed_with_one_json_object(
    command: str,
    expected_exit: int,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: None)

    exit_code = browser_extension.run_browser_extension(_options(command))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert exit_code == expected_exit
    expected_payload = {
        "schema_version": 1,
        "contract": "a0.browser-extension.cli.v1",
        "command": command,
        "ok": False,
        "code": expected_code,
        "exit_code": expected_exit,
        "message": payload["message"],
    }
    if command in {"install", "status", "doctor", "pair"}:
        expected_payload["server"] = {
            "state": "not_checked",
            "reason_code": "test_remote_not_checked",
            "authentication": "not_checked",
        }
    assert payload == expected_payload


def test_untrusted_same_name_path_executable_is_not_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / browser_extension.COMPANION_EXECUTABLE
    executable.write_text("not a trusted release", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert browser_extension._resolve_companion_executable() is None


def test_saved_host_remote_status_is_collected_even_without_local_companion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, bool]] = []

    async def collect(host: str, *, remember_session: bool) -> browser_extension._ServerProbe:
        calls.append((host, remember_session))
        return browser_extension._ServerProbe(
            "checked",
            "remote_status_checked",
            "authenticated",
            {"rollout_state": "preview"},
        )

    monkeypatch.setattr(browser_extension, "_collect_server_status", collect)
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: None)
    monkeypatch.setattr(
        "agent_zero_cli.config.load_config",
        lambda: SimpleNamespace(
            instance_url="http://localhost:50080/",
            remember_host=True,
        ),
    )

    exit_code = browser_extension.run_browser_extension(_options("status"))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == browser_extension.EXIT_NOT_INSTALLED
    assert calls == [("http://localhost:50080", True)]
    assert payload["server"] == {
        "state": "checked",
        "reason_code": "remote_status_checked",
        "authentication": "authenticated",
        "foundation": {"rollout_state": "preview"},
    }
    assert payload["code"] == "COMPANION_NOT_INSTALLED"


def test_remote_status_restores_verifies_and_projects_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[object] = []

    class FakeClient:
        def __init__(self, host: str) -> None:
            self.host = host
            self.restored: list[str] = []
            self.persisted: list[str] = []
            clients.append(self)

        async def fetch_capabilities(self) -> dict[str, object]:
            return {"features": [browser_extension.FOUNDATION_FEATURE]}

        def restore_session(self, host: str) -> bool:
            self.restored.append(host)
            return True

        async def verify_session(self) -> bool:
            return True

        def persist_session(self, host: str) -> None:
            self.persisted.append(host)

        async def fetch_browser_extension_status(self) -> dict[str, object]:
            return _remote_foundation_status()

        async def disconnect(self, *, close_http: bool, notify: bool) -> None:
            assert close_http is True
            assert notify is False

    monkeypatch.setattr("agent_zero_cli.client.A0Client", FakeClient)

    import asyncio

    result = asyncio.run(
        _REAL_COLLECT_SERVER_STATUS(
            "http://localhost:50080",
            remember_session=True,
        )
    )

    assert result.as_dict() == {
        "state": "checked",
        "reason_code": "remote_status_checked",
        "authentication": "authenticated",
        "foundation": {
            "foundation_contract": "a0.browser-bridge.foundation.v1",
            "status_contract": "a0.browser-bridge.status.v1",
            "rollout_state": "preview",
            "rollout_reason_code": "rollout_preview",
            "selection_state": "not_selected",
            "selection_reason_code": "host_selection_automatic",
            "server_layer_state": "configured",
            "server_layer_reason_code": "rollout_preview",
        },
    }
    assert len(clients) == 1
    fake_client = clients[0]
    assert fake_client.restored == ["http://localhost:50080"]
    assert fake_client.persisted == ["http://localhost:50080"]


@pytest.mark.parametrize(
    ("command", "host", "expected_exit", "expected_code"),
    [
        ("status", "https://user:password@example.test", 2, "INVALID_HOST"),
        ("install", "https://example.test?a=1", 2, "INVALID_HOST"),
        ("pair", "http://example.test", 5, "INSECURE_PAIRING_HOST"),
    ],
)
def test_invalid_or_insecure_host_fails_before_companion_resolution(
    command: str,
    host: str,
    expected_exit: int,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolved: list[bool] = []
    monkeypatch.setattr(
        browser_extension,
        "_resolve_companion_executable",
        lambda: resolved.append(True) or "/trusted/bridge",
    )

    exit_code = browser_extension.run_browser_extension(_options(command, host=host))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == expected_exit
    assert payload["code"] == expected_code
    assert resolved == []


def test_install_passes_complete_explicit_browser_set_to_companion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/a0-browser-bridge")

    def fake_run(argv: list[str], *, stdin_payload: str | None) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), stdin_payload))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(_install_payload()),
            stderr="",
        )

    monkeypatch.setattr(browser_extension, "_run_process", fake_run)

    exit_code = browser_extension.run_browser_extension(
        _options("install", browsers=("chrome", "edge", "chrome"))
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls == [
        (
            [
                "/trusted/a0-browser-bridge",
                "install",
                "--browser",
                "chrome",
                "--browser",
                "edge",
                "--json",
            ],
            None,
        )
    ]
    assert payload["result"] == _install_payload()


def test_default_browser_target_is_auto(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")

    def fake_run(argv: list[str], *, stdin_payload: str | None) -> subprocess.CompletedProcess[str]:
        del stdin_payload
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "Repaired", "")

    monkeypatch.setattr(browser_extension, "_run_process", fake_run)

    assert browser_extension.run_browser_extension(_options("repair", json_output=False)) == 0
    capsys.readouterr()
    assert calls == [["/trusted/bridge", "repair", "--browser", "auto"]]


def test_auto_cannot_be_mixed_with_explicit_browser_targets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(browser_extension, "_run_process", lambda *args, **kwargs: started.append(True))

    exit_code = browser_extension.run_browser_extension(
        _options("install", browsers=("auto", "chrome"))
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == browser_extension.EXIT_USAGE
    assert payload["code"] == "AMBIGUOUS_BROWSER_SELECTION"
    assert started == []


def test_pairing_explicit_terminal_handoff_never_starts_native_pair(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pairing_code = "A0B1-ABC1-VERYSECRETCODE"
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")

    async def fake_pairing_bundle(host: str, *, remember_session: bool) -> dict[str, object]:
        assert host == "http://localhost:50080"
        assert remember_session is True
        return {"pairing_code": pairing_code, "expires_at_ms": 12345, "extension_id": "a" * 32}

    monkeypatch.setattr(browser_extension, "_create_pairing_bundle", fake_pairing_bundle)
    monkeypatch.setattr(
        "agent_zero_cli.config.load_config",
        lambda: SimpleNamespace(instance_url="", remember_host=True),
    )

    def fake_run(argv: list[str], *, stdin_payload: str | None) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), stdin_payload))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"schema_version": 1, "pairing_code": pairing_code}),
            stderr=pairing_code,
        )

    monkeypatch.setattr(browser_extension, "_run_process", fake_run)

    import io
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    terminal = Terminal()
    exit_code = browser_extension.run_browser_extension(
        _options("pair", host="http://localhost:50080", json_output=False), stdout=terminal,
    )

    captured = capsys.readouterr()
    assert exit_code == browser_extension.EXIT_ACTION_REQUIRED
    assert calls == []
    assert pairing_code in terminal.getvalue()
    assert "options.html" in terminal.getvalue()
    assert "not complete until Options confirms" in terminal.getvalue()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("json_output", [True, False])
def test_pairing_json_or_redirected_output_never_creates_secret(monkeypatch, capsys, json_output):
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    async def forbidden(*args, **kwargs):
        pytest.fail("must not create a pairing intent")
    monkeypatch.setattr(browser_extension, "_create_pairing_bundle", forbidden)
    assert browser_extension.run_browser_extension(_options("pair", json_output=json_output)) == browser_extension.EXIT_ACTION_REQUIRED
    captured = capsys.readouterr()
    assert "No pairing code was created" in captured.out + captured.err


@pytest.mark.parametrize("wrong_origin", [False, True])
def test_pairing_creation_uses_authenticated_csrf_once_and_checks_exact_host(monkeypatch, wrong_origin):
    import asyncio
    import httpx
    import time
    from agent_zero_cli import browser_extension_release as releases
    now = int(time.time() * 1000)
    pin = releases.ApprovedCompanionRelease("2.12.0", "macos", "universal2", "a" * 64, 10, "fixture", "b" * 64,
                                           ("chrome-extension://" + "a" * 32 + "/",))
    monkeypatch.setattr(releases, "APPROVED_COMPANION_RELEASES", (pin,))
    monkeypatch.setattr(releases, "_host_target", lambda: ("macos", "universal2"))
    bundle = {
        "contract": "a0.browser-bridge.trust.v1", "trust_version": 1, "state": "pairing_pending",
        "pairing_id": "fixture-pairing", "pairing_code": "A0B1-1234ABCD-" + "0" * 32,
        "server": {"base_url": "https://wrong.invalid" if wrong_origin else "http://localhost:50080", "instance_fingerprint": "sha256:" + "a" * 20},
        "extension_id": "a" * 32, "display_name": "My browser", "created_at_ms": now,
        "expires_at_ms": now + 300000, "expires_in_seconds": 300, "native_runtime_location": "user_browser_host",
        "docker_install_target": False, "connector_session_ready": False, "browser_control_ready": False,
    }
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url == "http://localhost:50080/api/plugins/_a0_connector/browser_bridge_pairing"
        assert request.headers["X-CSRF-Token"] == "fixture-csrf"
        assert json.loads(request.content) == {"action": "create", "display_name": "My browser"}
        return httpx.Response(201, json=bundle)
    class Client:
        def __init__(self, host):
            self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            self.authenticated = False
        async def fetch_capabilities(self):
            assert self.authenticated
            return {"features": ["browser_bridge_pairing_v1"]}
        def restore_session(self, host):
            pass
        async def verify_session(self):
            self.authenticated = True
            return True
        async def _csrf_headers(self):
            return {"X-CSRF-Token": "fixture-csrf"}
    monkeypatch.setattr("agent_zero_cli.client.A0Client", Client)
    if wrong_origin:
        with pytest.raises(browser_extension._BrowserExtensionFailure, match="Pairing could not be prepared"):
            asyncio.run(browser_extension._create_pairing_bundle("http://localhost:50080", remember_session=False))
    else:
        result = asyncio.run(browser_extension._create_pairing_bundle("http://localhost:50080", remember_session=False))
        assert result == bundle
    assert len(calls) == 1


def test_process_environment_drops_credential_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A0_PASSWORD", "top-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "also-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "cloud-secret")
    monkeypatch.setenv("SESSION_COOKIE", "browser-secret")
    monkeypatch.setenv("USERNAME", "private-user")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/Users/test")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/dbus-test")
    environment = browser_extension._companion_environment()

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == "/Users/test"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus-test"
    assert "A0_PASSWORD" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "SESSION_COOKIE" not in environment
    assert "USERNAME" not in environment


@pytest.mark.parametrize(
    ("stream_name", "file_descriptor", "limit"),
    [
        ("stdout", 1, browser_extension._MAX_COMPANION_STDOUT_BYTES),
        ("stderr", 2, browser_extension._MAX_COMPANION_STDERR_BYTES),
    ],
)
def test_process_capture_is_bounded_and_terminates_on_overflow(
    stream_name: str,
    file_descriptor: int,
    limit: int,
) -> None:
    script = (
        "import os,time;"
        f"data=b'x'*{limit + browser_extension._PIPE_READ_BYTES};"
        f"fd={file_descriptor};"
        "\nwhile data:\n n=os.write(fd,data); data=data[n:]\n"
        "time.sleep(30)"
    )

    result = browser_extension._run_process(
        [sys.executable, "-c", script],
        stdin_payload=None,
    )

    assert result.overflow_stream == stream_name
    captured = result.stdout if stream_name == "stdout" else result.stderr
    # A cut frame may split UTF-8; retain the stop reason, never partial output.
    assert captured == ""


def test_process_capture_rejects_non_utf8_output() -> None:
    script = "import os; os.write(1, bytes([255]))"

    with pytest.raises(browser_extension._BrowserExtensionFailure) as failure:
        browser_extension._run_process(
            [sys.executable, "-c", script],
            stdin_payload=None,
        )

    assert failure.value.exit_code == browser_extension.EXIT_INTEGRITY
    assert failure.value.code == "COMPANION_OUTPUT_ENCODING_INVALID"


def test_json_output_redacts_private_fields_paths_urls_and_pairing_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = str(Path.home() / "Library/Application Support/Agent Zero/install-state.json")
    pairing_code = "A0B1-ABC1-VERYSECRETCODE"
    companion_payload = _status_payload(
        reason_code=f"read {private_path} and {pairing_code}",
        platform="https://agent.example.test/private?q=secret",
    )
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(companion_payload),
            private_path,
        ),
    )

    exit_code = browser_extension.run_browser_extension(_options("doctor"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    serialized = json.dumps(payload)
    assert exit_code == 0
    assert private_path not in serialized
    assert pairing_code not in serialized
    assert payload["result"]["reason_code"] == "read [redacted-path]"
    assert payload["result"]["platform"] == "https://agent.example.test"
    assert captured.err == ""

    sanitized = browser_extension._sanitize_json_value(
        {"private_key": "secret-value", "path": private_path, "state": "healthy"}
    )
    assert sanitized == {"state": "healthy"}


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "[]",
        "{}",
        '{"schema_version":1,"contract":"a0.browser-bridge.status.v1"}',
        '{"schema_version":true,"contract":"a0.browser-bridge.status.v1"}',
        '{"schema_version":1,"contract":"a0.browser-bridge.unknown.v1"}',
    ],
)
def test_invalid_companion_json_fails_closed_without_echoing_raw_output(
    stdout: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(argv, 0, stdout, ""),
    )

    exit_code = browser_extension.run_browser_extension(_options("status"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == browser_extension.EXIT_INTEGRITY
    assert payload["code"] == "COMPANION_OUTPUT_INVALID"
    assert stdout not in captured.out


def test_unrecognized_companion_exit_code_maps_to_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(
            argv,
            99,
            '{"schema_version":1,"contract":"a0.browser-bridge.status.v1"}',
            "",
        ),
    )

    exit_code = browser_extension.run_browser_extension(_options("status"))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == browser_extension.EXIT_INTEGRITY
    assert payload["code"] == "COMPANION_EXIT_INVALID"


def test_oversized_companion_json_fails_before_parsing_or_echoing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oversized = "x" * (browser_extension._MAX_COMPANION_JSON_BYTES + 1)
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(argv, 0, oversized, ""),
    )

    exit_code = browser_extension.run_browser_extension(_options("status"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == browser_extension.EXIT_INTEGRITY
    assert payload["code"] == "COMPANION_OUTPUT_TOO_LARGE"
    assert oversized not in captured.out


@pytest.mark.parametrize("returncode", [3, 4, 5, 6, 7])
def test_stable_companion_exit_codes_are_preserved(
    returncode: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(
            argv,
            returncode,
            json.dumps(_status_payload()),
            "",
        ),
    )

    assert browser_extension.run_browser_extension(_options("status")) == returncode
    assert json.loads(capsys.readouterr().out)["exit_code"] == returncode


def test_human_companion_stderr_is_bounded_and_path_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = str(Path.home() / "private/bridge.log")
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")
    monkeypatch.setattr(
        browser_extension,
        "_run_process",
        lambda argv, *, stdin_payload: subprocess.CompletedProcess(
            argv,
            0,
            "Status complete",
            f"{private_path} {'x' * 9000}",
        ),
    )

    assert browser_extension.run_browser_extension(_options("status", json_output=False)) == 0

    captured = capsys.readouterr()
    assert captured.out == "Status complete\n"
    assert private_path not in captured.err
    assert captured.err.startswith("[redacted-path]")
    assert len(captured.err) <= 8193


def test_uninstall_flags_are_delegated_without_host_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: "/trusted/bridge")

    def fake_run(argv: list[str], *, stdin_payload: str | None) -> subprocess.CompletedProcess[str]:
        assert stdin_payload is None
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "Uninstalled", "")

    monkeypatch.setattr(browser_extension, "_run_process", fake_run)

    assert browser_extension.run_browser_extension(
        _options("uninstall", json_output=False, yes=True, force_local=True, keep_logs=True)
    ) == 0
    capsys.readouterr()
    assert calls == [["/trusted/bridge", "uninstall", "--yes", "--force-local", "--keep-logs"]]
