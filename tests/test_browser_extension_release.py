from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_zero_cli import browser_extension, browser_extension_release as release


def test_distributed_registry_contains_only_reviewed_mac_executable_and_catalog():
    assert release.APPROVED_COMPANION_RELEASES == (
        release.MACOS_2_12_0_RELEASE, release.MACOS_2_12_1_RELEASE,
        release.MACOS_2_12_2_RELEASE, release.MACOS_2_12_3_RELEASE,
    )
    pin = release.MACOS_2_12_0_RELEASE
    assert release._pin_valid(pin)
    assert (pin.version, pin.platform, pin.artifact_arch) == ("2.12.0", "macos", "universal2")
    assert pin.executable_sha256 == "8f8125212bcafa3ead9e8f44c3dc8bc7213ee9f44570dd63638a242f3b067b6f"
    assert pin.executable_size == 10_356_768
    assert pin.catalog_sha256 == "9758f715d7648ee2246c82dc3e9a3574dfc55c07cad24d629166e0cd40ef35e4"
    assert pin.catalog_key_id == "publisher-2026"
    assert pin.extension_origins == ("chrome-extension://nhliclifilepdkoolioacpjpijomfplj/",)
    newest = release.MACOS_2_12_1_RELEASE
    assert release._pin_valid(newest)
    assert (newest.version, newest.platform, newest.artifact_arch) == ("2.12.1", "macos", "universal2")
    assert newest.executable_sha256 == "26e2bd4ca821b5b2ca7cde5336f1348d682f47855fee705cf43aafd992205890"
    assert newest.executable_size == 10_374_432
    assert newest.catalog_sha256 == "3dcde8e12a571a98d8fe9dfd2c4086469d68e03da9129ee18a63505c834c82b3"
    assert newest.catalog_key_id == pin.catalog_key_id
    assert newest.extension_origins == pin.extension_origins
    assert release.MINIMUM_SECURE_COMPANION == "2.12.0"
    newest = release.MACOS_2_12_2_RELEASE
    assert release._pin_valid(newest)
    assert (newest.version, newest.platform, newest.artifact_arch) == ("2.12.2", "macos", "universal2")
    assert newest.executable_sha256 == "8cb84de1e66bbd52771b534cb1bfc83d69eb94ebc87b3f7669547bff68359080"
    assert newest.executable_size == 10_374_256
    assert newest.catalog_sha256 == "f403678be1077192cc10930f3f1a2a43a55abfdaf0d8ba0615f2780153f046ab"
    assert newest.catalog_key_id == pin.catalog_key_id
    assert newest.extension_origins == pin.extension_origins
    newest = release.MACOS_2_12_3_RELEASE
    assert release._pin_valid(newest)
    assert (newest.version, newest.platform, newest.artifact_arch) == ("2.12.3", "macos", "universal2")
    assert newest.executable_sha256 == "803a24e87f2568c5fbb1c9f5de400bd3b8aab60a16a6ac70f3814be4494f482b"
    assert newest.executable_size == 10_376_528
    assert newest.catalog_sha256 == "a40176dcd2048e692996b3ba4f5bf5218fe5ab98fca085f85eaa6cbe9a9ebd29"
    assert newest.catalog_key_id == pin.catalog_key_id
    assert newest.extension_origins == pin.extension_origins


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(release, "_host_target", lambda: ("linux", "x86_64"))
    root = home / ".local/share/agent-zero/browser-bridge"
    executable = root / "releases/2.12.0/linux-x86_64/a0-browser-bridge"
    executable.parent.mkdir(parents=True)
    for directory in (root, root / "releases", executable.parent.parent, executable.parent):
        directory.chmod(0o700)
    (root / "transactions").mkdir(mode=0o700)
    (root / "install.lock").write_bytes(b"a0-browser-bridge-install-lock-v1\n")
    (root / "install.lock").chmod(0o600)
    executable.write_bytes(b"approved fixture executable - never executed")
    executable.chmod(0o700)
    pin = release.ApprovedCompanionRelease(
        "2.12.0", "linux", "x86_64", hashlib.sha256(executable.read_bytes()).hexdigest(),
        executable.stat().st_size, "fixture-release-key", "c" * 64,
        ("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",),
    )
    monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (pin,))
    manifest_path = home / ".config/google-chrome/NativeMessagingHosts/io.agentzero.browser_bridge.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "name": release.NATIVE_HOST, "description": "Agent Zero browser bridge",
        "type": "stdio", "path": str(executable), "allowed_origins": list(pin.extension_origins),
    }
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    state = {
        "schema_version": 1, "install_id": "fixture-install", "channel": "stable",
        "active_version": "2.12.0", "active_artifact_sha256": pin.executable_sha256,
        "active_artifact_size": pin.executable_size, "platform": "linux", "artifact_arch": "x86_64",
        "previous": None, "installed_at_ms": 1000, "source": "a0_cli",
        "registered_browsers": ["chrome"], "release_catalog_key_id": pin.catalog_key_id,
        "registrations": [{"path_sha256": hashlib.sha256(os.fsencode(manifest_path)).hexdigest(),
                           "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}],
        "last_transaction_id": "fixture-transaction",
    }
    state_path = root / "install-state.json"
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o600)
    return root, executable, state_path, state, manifest_path, pin


def test_exact_owned_release_and_registration_resolve_without_execution(installation, monkeypatch):
    _, executable, _, _, _, _ = installation
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("discovery must not execute"))
    assert release.resolve_installed_companion() == str(executable)
    assert browser_extension._resolve_companion_executable() == str(executable)


@pytest.mark.parametrize("failure", [
    "digest", "size", "symlink_binary", "hardlink_binary", "private_root", "private_state",
    "duplicate_state", "unknown_state", "catalog_key", "below_floor", "unknown_release",
    "recovery", "manifest_path", "manifest_origin", "manifest_digest", "state_symlink",
])
def test_untrusted_or_changed_install_fails_before_any_execution(installation, monkeypatch, failure):
    root, executable, state_path, state, manifest_path, pin = installation
    if failure == "digest":
        executable.write_bytes(b"x" * pin.executable_size)
    elif failure == "size":
        executable.write_bytes(b"short")
    elif failure == "symlink_binary":
        retained = executable.with_name("other")
        executable.rename(retained)
        executable.symlink_to(retained)
    elif failure == "hardlink_binary":
        os.link(executable, executable.with_name("other"))
    elif failure == "private_root":
        root.chmod(0o755)
    elif failure == "private_state":
        state_path.chmod(0o644)
    elif failure == "duplicate_state":
        state_path.write_text(state_path.read_text().replace('"channel": "stable"', '"channel": "stable", "channel": "stable"'))
    elif failure == "unknown_state":
        state["active_binary"] = str(executable)
        state_path.write_text(json.dumps(state))
    elif failure == "catalog_key":
        state["release_catalog_key_id"] = "attacker"
        state_path.write_text(json.dumps(state))
    elif failure == "below_floor":
        state["active_version"] = "2.11.0"
        state_path.write_text(json.dumps(state))
        monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (replace(pin, version="2.11.0"),))
    elif failure == "unknown_release":
        monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (replace(pin, version="2.13.0"),))
    elif failure == "recovery":
        (root / "transactions/pending.json").write_text("{}")
    elif failure in {"manifest_path", "manifest_origin"}:
        manifest = json.loads(manifest_path.read_text())
        manifest["path" if failure == "manifest_path" else "allowed_origins"] = "/unexpected" if failure == "manifest_path" else ["chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"]
        manifest_path.write_text(json.dumps(manifest))
        state["registrations"][0]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        state_path.write_text(json.dumps(state))
    elif failure == "manifest_digest":
        manifest_path.write_text(manifest_path.read_text() + " ")
    elif failure == "state_symlink":
        retained = state_path.with_name("other-state")
        state_path.rename(retained)
        state_path.symlink_to(retained)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(release.CompanionDiscoveryError) as error:
        release.resolve_installed_companion()
    assert str(root) not in str(error.value)


def test_empty_compiled_registry_never_uses_path_or_installed_self_report(installation, monkeypatch):
    _, executable, _, _, _, _ = installation
    monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", ())
    monkeypatch.setenv("PATH", str(executable.parent))
    assert release.resolve_installed_companion() is None


def test_cli_discovery_errors_remain_fixed_and_pathless(installation):
    _, executable, _, _, _, _ = installation
    executable.write_bytes(b"changed")
    with pytest.raises(browser_extension._BrowserExtensionFailure) as error:
        browser_extension._resolve_companion_executable()
    assert error.value.exit_code == browser_extension.EXIT_INTEGRITY
    assert error.value.code == "COMPANION_INSTALL_INTEGRITY_FAILED"
    assert str(executable) not in error.value.message
