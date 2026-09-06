import io
import json
import sys

import pytest

from agent_zero_cli import browser_extension_development as dev
from agent_zero_cli.browser_extension import _BrowserExtensionFailure, _CompanionProcessResult


def _result(action="install"):
    return {
        "contract": dev.DEVELOPMENT_CONTRACT, "schema_version": 1,
        "channel": "local-development", "action": action, "state": "installed",
        "reason_code": "DEVELOPMENT_INSTALLED", "companion_version": "2.12.0",
        "native_host_name": dev.DEVELOPMENT_HOST, "extension_id": dev.DEVELOPMENT_EXTENSION,
        "registered_browsers": ["chrome"], "registration_count": 1,
        "already_current": False, "mutation_allowed": True, "exit_code": 0,
    }


def test_source_install_routes_only_to_native_development(monkeypatch, tmp_path):
    source = tmp_path / "local-bridge"
    source.write_bytes(b"explicit fixture")
    source.chmod(0o700)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _CompanionProcessResult(0, json.dumps(_result()), "private diagnostic not forwarded")

    monkeypatch.setattr(dev, "_run_process", run)
    output = io.StringIO()
    assert dev.run_development(action="install", source_binary=str(source), browsers=["chrome"], yes=True, json_output=True, stdout=output) == 0
    assert calls == [([str(source), "development", "install", "--json", "--browser", "chrome", "--yes"], {"stdin_payload": None, "timeout_seconds": 60})]
    assert json.loads(output.getvalue())["result"]["channel"] == "local-development"
    assert "private" not in output.getvalue() and str(source) not in output.getvalue()


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_mutation_requires_confirmation_before_executing(monkeypatch, action):
    monkeypatch.setattr(dev, "_run_process", lambda *a, **kw: pytest.fail("must not run"))
    output = io.StringIO()
    assert dev.run_development(action=action, source_binary="/does/not/matter", json_output=True, stdout=output) == 4
    assert json.loads(output.getvalue())["code"] == "DEVELOPMENT_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("change", [
    {"channel": "production"}, {"native_host_name": "io.agentzero.browser_bridge"},
    {"state": []}, {"exit_code": True}, {"registered_browsers": ["auto"]},
    {"private_key": "must not appear"},
])
def test_rejects_malformed_or_production_results(change):
    with pytest.raises(_BrowserExtensionFailure):
        dev._decode_result(json.dumps(_result() | change), "install", 0)


def test_rejects_duplicate_fields_and_relative_executable():
    with pytest.raises(_BrowserExtensionFailure):
        dev._decode_result('{"state":"installed","state":"blocked"}', "install", 0)
    with pytest.raises(_BrowserExtensionFailure):
        dev._explicit_source("a0-browser-bridge")


def test_parser_keeps_development_separate():
    from agent_zero_cli.__main__ import _build_parser

    args = _build_parser().parse_args(["browser-extension", "development", "install", "--source-binary", "/tmp/source", "--browser", "chrome", "--yes", "--json"])
    assert args.browser_extension_command == "development"
    assert args.development_action == "install" and args.yes
    assert args.source_binary == "/tmp/source"


@pytest.mark.parametrize("already_current", [False, True])
def test_development_update_delegates_without_install_or_target_changes(monkeypatch, already_current):
    calls = []
    monkeypatch.setattr(dev, "_explicit_source", lambda value: "/explicit/source")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _CompanionProcessResult(0, json.dumps(_result("update") | {
            "reason_code": "DEVELOPMENT_ALREADY_CURRENT" if already_current else "DEVELOPMENT_UPDATED",
            "already_current": already_current,
        }), "private diagnostic")

    monkeypatch.setattr(dev, "_run_process", run)
    output = io.StringIO()
    assert dev.run_development(action="update", source_binary="/explicit/source", yes=True, json_output=True, stdout=output) == 0
    assert calls == [(["/explicit/source", "development", "update", "--json", "--yes"], {
        "stdin_payload": None, "timeout_seconds": 60,
    })]
    payload = json.loads(output.getvalue())
    assert payload["result"]["action"] == "update"
    assert "Reconnect" in payload["message"] and "pairing" in payload["message"]
    assert ("already current" in payload["message"]) is already_current
    assert "private" not in output.getvalue()


def test_development_update_parser_and_target_refusal(monkeypatch):
    from agent_zero_cli.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["browser-extension", "development", "update", "--source-binary", "/explicit/source", "--yes", "--json"])
    assert args.development_action == "update" and args.yes
    with pytest.raises(SystemExit):
        parser.parse_args(["browser-extension", "development", "update", "--source-binary", "/explicit/source", "--browser", "chrome", "--yes"])
    monkeypatch.setattr(dev, "_run_process", lambda *a, **kw: pytest.fail("must not run"))
    output = io.StringIO()
    assert dev.run_development(action="update", source_binary="/explicit/source", browsers=["chrome"], yes=True, json_output=True, stdout=output) == 2
    assert json.loads(output.getvalue())["code"] == "UNEXPECTED_ARGUMENT"


def test_development_update_rejects_other_action_readback():
    with pytest.raises(_BrowserExtensionFailure):
        dev._decode_result(json.dumps(_result("install")), "update", 0)


def test_development_update_recovery_message_preserves_partial_outcome(monkeypatch):
    result = _result("update") | {
        "state": "blocked", "reason_code": "DEVELOPMENT_UPDATE_RECOVERY_REQUIRED",
        "exit_code": 5,
    }
    monkeypatch.setattr(dev, "_explicit_source", lambda value: "/explicit/source")
    monkeypatch.setattr(dev, "_run_process", lambda *a, **kw: _CompanionProcessResult(5, json.dumps(result), "private failure"))
    output = io.StringIO()
    assert dev.run_development(action="update", source_binary="/explicit/source", yes=True, json_output=True, stdout=output) == 5
    payload = json.loads(output.getvalue())
    assert payload["code"] == "DEVELOPMENT_UPDATE_RECOVERY_REQUIRED"
    assert "do not uninstall" in payload["message"] and "same trusted source" in payload["message"]
    assert "private failure" not in output.getvalue()


def test_development_process_deadline_is_bounded():
    from agent_zero_cli.browser_extension import _run_process

    result = _run_process([sys.executable, "-c", "import os,time; os.write(1,bytes([226,130])); time.sleep(5)"], stdin_payload=None, timeout_seconds=0.1)
    assert result.timed_out
    assert result.returncode != 0
    assert result.stdout == ""


def test_uninstall_reports_remaining_credential_cleanup(monkeypatch):
    result = _result("uninstall") | {
        "state": "not_installed", "reason_code": "DEVELOPMENT_UNINSTALLED_CREDENTIAL_CLEANUP_PENDING",
        "registered_browsers": [], "registration_count": 0, "exit_code": 6,
    }
    monkeypatch.setattr(dev, "_explicit_source", lambda value: "/explicit/source")
    monkeypatch.setattr(dev, "_run_process", lambda *args, **kwargs: _CompanionProcessResult(6, json.dumps(result), ""))
    output = io.StringIO()
    assert dev.run_development(action="uninstall", source_binary="/explicit/source", yes=True, json_output=True, stdout=output) == 6
    message = json.loads(output.getvalue())["message"]
    assert "local keys may remain" in message and "Browser settings" in message
