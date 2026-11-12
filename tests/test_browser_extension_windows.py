"""Native ACL/handle tests; registry calls are substituted, never written here."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows files required")
if os.name == "nt":
    from agent_zero_cli import browser_extension_windows as windows
from agent_zero_cli import browser_extension_release as release
from agent_zero_cli import browser_extension_bootstrap as bootstrap


def write(path, data):
    with windows.create_file(path) as stream:
        stream.write(data)


def test_private_handles_acl_hardlinks_and_lock(tmp_path):
    root = tmp_path / "private space"
    windows.create_directory(root)
    path = root / "state.json"
    write(path, b"owned")
    with windows.file(path) as descriptor:
        assert os.read(descriptor, 5) == b"owned"
        with pytest.raises(OSError):
            path.write_bytes(b"foreign")
        with pytest.raises(OSError):
            path.unlink()
        with windows.lock(descriptor):
            # Shared discovery locks coexist; native install takes an exclusive lock.
            with windows.file(path) as other, windows.lock(other):
                pass
    link = root / "link.json"
    os.link(path, link)
    with pytest.raises(ValueError):
        with windows.file(path):
            pass
    link.unlink()
    foreign = root / "foreign.json"
    foreign.write_bytes(b"inherited ACL")
    with pytest.raises(ValueError):
        with windows.file(foreign):
            pass
    for invalid in [r"\\server\share\file", r"\\?\C:\file", r"C:\file:stream", r"C:\folder\file."]:
        with pytest.raises(ValueError):
            windows.local_path(Path(invalid))


def test_windows_pinned_discovery_checks_files_and_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(release, "_host_target", lambda: ("windows", "x86_64"))
    _, root, _ = release._paths("windows")
    binary = root / "releases/2.12.3/windows-x86_64/a0-browser-bridge.exe"
    manifest = root / "manifests/chrome" / f"{release.NATIVE_HOST}.json"
    for directory in [root.parent, root, root / "transactions", root / "releases",
                      binary.parent.parent, binary.parent, root / "manifests", manifest.parent]:
        windows.create_directory(directory)
    data = b"test fixture never executed"
    write(binary, data)
    write(root / "install.lock", b"lock")
    pin = replace(release.MACOS_2_12_3_RELEASE, platform="windows", artifact_arch="x86_64",
                  executable_size=len(data), executable_sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (pin,))
    manifest_bytes = json.dumps({"name": release.NATIVE_HOST, "description": "Agent Zero browser bridge",
        "type": "stdio", "path": str(binary), "allowed_origins": list(pin.extension_origins)}).encode()
    write(manifest, manifest_bytes)
    state = {"schema_version": 1, "install_id": "test-install", "channel": "stable",
        "active_version": pin.version, "active_artifact_sha256": pin.executable_sha256,
        "active_artifact_size": pin.executable_size, "platform": pin.platform, "artifact_arch": pin.artifact_arch,
        "previous": None, "installed_at_ms": 1000, "source": "a0_cli", "registered_browsers": ["chrome", "brave"],
        "release_catalog_key_id": pin.catalog_key_id, "last_transaction_id": "test-transaction",
        "registrations": [{"path_sha256": hashlib.sha256(os.fsencode(manifest)).hexdigest(),
                           "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}]}
    write(root / "install-state.json", json.dumps(state).encode())
    checks = []
    monkeypatch.setattr(windows, "verify_registration", lambda path, host: checks.append((path, host)))
    assert release.resolve_installed_companion() == str(binary)
    assert checks == [(manifest, release.NATIVE_HOST)]
    binary.write_bytes(b"changed")
    with pytest.raises(release.CompanionDiscoveryError):
        release.resolve_installed_companion()


def test_windows_bootstrap_retains_final_bytes_and_removes_only_generated_files(tmp_path, monkeypatch):
    data = b"unsigned fixture only; never invoked"
    pin = replace(release.MACOS_2_12_3_RELEASE, platform="windows", artifact_arch="x86_64",
                  executable_size=len(data), executable_sha256=hashlib.sha256(data).hexdigest())
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo("a0-browser-bridge.exe")
        info.mode = 0o500
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw = archive.getvalue()
    approved = bootstrap.ApprovedBootstrap(pin, "https://example.invalid/v2.12.3/test.tar.gz", hashlib.sha256(raw).hexdigest(), len(raw))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(release, "_host_target", lambda: ("windows", "x86_64"))
    monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (pin,))
    monkeypatch.setattr(bootstrap, "APPROVED_BOOTSTRAPS", (approved,))
    monkeypatch.setattr(bootstrap, "_download", lambda _, stream: stream.write(raw))
    with bootstrap.acquire_bootstrap() as acquired:
        assert acquired is not None
        acquired.verify()
        path = Path(acquired.path)
        with pytest.raises(OSError):
            path.write_bytes(b"changed")
        with pytest.raises(OSError):
            path.unlink()
    assert not path.parent.exists()
    assert not list(tmp_path.iterdir())
