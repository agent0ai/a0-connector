from __future__ import annotations

from dataclasses import replace
import gzip
import hashlib
import io
from pathlib import Path
import tarfile

import pytest

from agent_zero_cli import browser_extension_bootstrap as bootstrap
from agent_zero_cli import browser_extension_release as release


def test_reviewed_macos_bootstrap_pins_bind_exact_signed_release(monkeypatch):
    monkeypatch.setattr(release, "_host_target", lambda: ("macos", "universal2"))
    pin = bootstrap._select()
    assert pin is not None
    assert pin.companion is release.MACOS_2_12_3_RELEASE
    assert pin.archive_sha256 == "ea3291999470a548b90bdcd1f82b90707364fa18e3f08938e6564b7457e2b127"
    assert pin.archive_size == 4_554_635
    assert pin.archive_sha256 != pin.companion.executable_sha256
    assert pin.archive_url == (
        "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/"
        "native-v2.12.3-macos/v2.12.3/a0-browser-bridge-2.12.3-macos-universal2.tar.gz"
    )
    # Selection is version-based, not registry order; retain reviewed old pins.
    monkeypatch.setattr(bootstrap, "APPROVED_BOOTSTRAPS", tuple(reversed(bootstrap.APPROVED_BOOTSTRAPS)))
    assert bootstrap._select() is pin
    old = next(item for item in bootstrap.APPROVED_BOOTSTRAPS if item.companion is release.MACOS_2_12_0_RELEASE)
    assert old.archive_sha256 == "f9ca468982794f3a767cdfe2d06f1fc308202d27c7c7d8f383e4a3e108d25482"
    assert old.archive_size == 4_546_235
    previous = next(item for item in bootstrap.APPROVED_BOOTSTRAPS if item.companion is release.MACOS_2_12_1_RELEASE)
    assert previous.archive_sha256 == "5dc1db234c820ecf03119c36f637c6042de33ceee5df88adf8bfb6d037fef4f5"
    assert previous.archive_size == 4_541_252
    previous = next(item for item in bootstrap.APPROVED_BOOTSTRAPS if item.companion is release.MACOS_2_12_2_RELEASE)
    assert previous.archive_sha256 == "2681377a297d9943a069b245587a40cdb727c8d8119a733116df3c3eecf6088c"
    assert previous.archive_size == 4_547_309


@pytest.mark.parametrize("target", [("linux", "x86_64"), ("linux", "aarch64"), ("windows", "x86_64"), None])
def test_mac_release_does_not_provision_other_platforms(monkeypatch, target):
    monkeypatch.setattr(release, "_host_target", lambda: target)
    monkeypatch.setattr(bootstrap, "_download", lambda *args: pytest.fail("no artifact for this platform"))
    with bootstrap.acquire_bootstrap() as verified:
        assert verified is None


def fixture_payload(extra=False):
    executable = b"test-only executable bytes; never execute"
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        entry = tarfile.TarInfo("a0-browser-bridge")
        entry.size = len(executable)
        entry.mode = 0o500
        archive.addfile(entry, io.BytesIO(executable))
        if extra:
            archive.addfile(tarfile.TarInfo("unwanted"), io.BytesIO())
    data = gzip.compress(tar.getvalue())
    companion = release.ApprovedCompanionRelease(
        "2.12.0", "macos", "universal2", hashlib.sha256(executable).hexdigest(),
        len(executable), "fixture-root", "c" * 64,
        ("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",),
    )
    pin = bootstrap.ApprovedBootstrap(companion, "https://example.invalid/v2.12.0/payload.tar.gz",
                                      hashlib.sha256(data).hexdigest(), len(data))
    return executable, data, pin


def test_private_bootstrap_checks_final_bytes_and_retains_them_through_invocation(tmp_path, monkeypatch):
    executable, data, pin = fixture_payload()
    monkeypatch.setattr(release, "_host_target", lambda: ("macos", "universal2"))
    monkeypatch.setattr(release, "APPROVED_COMPANION_RELEASES", (pin.companion,))
    monkeypatch.setattr(bootstrap, "APPROVED_BOOTSTRAPS", (pin,))
    monkeypatch.setattr(bootstrap.tempfile, "gettempdir", lambda: str(tmp_path.resolve()))
    monkeypatch.setattr(bootstrap, "_download", lambda _, destination: destination.write(data))
    with bootstrap.acquire_bootstrap() as verified:
        assert verified is not None
        path = Path(verified.path)
        assert path.read_bytes() == executable
        assert path.stat().st_mode & 0o777 == 0o500
        verified.verify()
        path.chmod(0o700)
        path.write_bytes(b"changed")
        with pytest.raises(release.CompanionDiscoveryError, match="CHANGED"):
            verified.verify()
    assert not path.exists()


@pytest.mark.parametrize("invalid", ["second_entry", "concat", "wrong_executable"])
def test_bootstrap_rejects_archive_extension_or_wrong_final_bytes(tmp_path, invalid):
    _, data, pin = fixture_payload(extra=invalid == "second_entry")
    if invalid == "concat":
        data += gzip.compress(b"")
    if invalid == "wrong_executable":
        pin = replace(pin, companion=replace(pin.companion, executable_sha256="f" * 64))
    with (tmp_path / "output").open("wb") as destination:
        with pytest.raises(release.CompanionDiscoveryError):
            bootstrap._extract(io.BytesIO(data), destination, pin)


def test_empty_bootstrap_policy_does_not_fetch_or_trust_paths(monkeypatch):
    monkeypatch.setattr(bootstrap, "APPROVED_BOOTSTRAPS", ())
    monkeypatch.setattr(bootstrap, "_download", lambda *args: pytest.fail("no network without compiled pins"))
    with bootstrap.acquire_bootstrap() as verified:
        assert verified is None


@pytest.mark.parametrize("status,body", [(302, b""), (200, b"changed")])
def test_bootstrap_download_rejects_redirects_and_corruption(tmp_path, monkeypatch, status, body):
    _, data, pin = fixture_payload()
    class Response:
        def __init__(self):
            self.status = status
            self.input = io.BytesIO(body)
        def getheaders(self):
            return [("Content-Length", str(len(data)))]
        def getheader(self, name, default=None):
            return default
        def read(self, size):
            return self.input.read(size)
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, method, path, headers):
            assert method == "GET" and path == "/v2.12.0/payload.tar.gz"
            assert set(headers) == {"Accept-Encoding", "User-Agent"}
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(bootstrap.http.client, "HTTPSConnection", Connection)
    with (tmp_path / "download").open("wb") as destination:
        with pytest.raises(release.CompanionDiscoveryError):
            bootstrap._download(pin, destination)
