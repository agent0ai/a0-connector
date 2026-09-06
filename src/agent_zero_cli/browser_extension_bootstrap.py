"""Private fresh-host bootstrap from independently packaged final release pins.

Only bootstrap acquisition/extraction lives here. The pinned native executable
owns catalog/platform/provenance verification and every installation mutation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit
import zlib

from . import browser_extension_release as release


@dataclass(frozen=True)
class ApprovedBootstrap:
    companion: release.ApprovedCompanionRelease
    archive_url: str
    archive_sha256: str
    archive_size: int


# Source-only release output, populated after genuine release verification.
# This CLI is independently built, so final companion hashes are not circular.
APPROVED_BOOTSTRAPS: tuple[ApprovedBootstrap, ...] = (
    ApprovedBootstrap(
        companion=release.MACOS_2_12_3_RELEASE,
        archive_url=(
            "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/"
            "native-v2.12.3-macos/v2.12.3/"
            "a0-browser-bridge-2.12.3-macos-universal2.tar.gz"
        ),
        archive_sha256="ea3291999470a548b90bdcd1f82b90707364fa18e3f08938e6564b7457e2b127",
        archive_size=4_554_635,
    ),
    ApprovedBootstrap(
        companion=release.MACOS_2_12_2_RELEASE,
        archive_url=(
            "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/"
            "native-v2.12.2-macos/v2.12.2/"
            "a0-browser-bridge-2.12.2-macos-universal2.tar.gz"
        ),
        archive_sha256="2681377a297d9943a069b245587a40cdb727c8d8119a733116df3c3eecf6088c",
        archive_size=4_547_309,
    ),
    ApprovedBootstrap(
        companion=release.MACOS_2_12_1_RELEASE,
        archive_url=(
            "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/"
            "native-v2.12.1-macos-r2/v2.12.1/"
            "a0-browser-bridge-2.12.1-macos-universal2.tar.gz"
        ),
        archive_sha256="5dc1db234c820ecf03119c36f637c6042de33ceee5df88adf8bfb6d037fef4f5",
        archive_size=4_541_252,
    ),
    ApprovedBootstrap(
        companion=release.MACOS_2_12_0_RELEASE,
        archive_url=(
            "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/"
            "native-v2.12.0-macos-r2/v2.12.0/"
            "a0-browser-bridge-2.12.0-macos-universal2.tar.gz"
        ),
        archive_sha256="f9ca468982794f3a767cdfe2d06f1fc308202d27c7c7d8f383e4a3e108d25482",
        archive_size=4_546_235,
    ),
)
_LIMIT = 512 * 1024 * 1024


def _select() -> ApprovedBootstrap | None:
    target = release._host_target()
    candidates = [pin for pin in APPROVED_BOOTSTRAPS
                  if (pin.companion.platform, pin.companion.artifact_arch) == target]
    if not candidates:
        return None
    versions = [release._version(pin.companion.version) for pin in candidates]
    if len(set(versions)) != len(versions):
        raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_POLICY_INVALID")
    pin = max(candidates, key=lambda item: release._version(item.companion.version))
    url = urlsplit(pin.archive_url)
    if (pin.companion not in release.APPROVED_COMPANION_RELEASES
            or not release._pin_valid(pin.companion)
            or not release._SHA256.fullmatch(pin.archive_sha256)
            or not release._bounded_int(pin.archive_size, 1, _LIMIT)
            or url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.port not in {None, 443}
            or f"/v{pin.companion.version}/" not in url.path
            or not url.path.endswith(".tar.gz") or "%" in url.path
            or any(part in {".", ".."} for part in url.path.split("/"))):
        raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_POLICY_INVALID")
    return pin


def _download(pin: ApprovedBootstrap, destination: BinaryIO) -> None:
    url = urlsplit(pin.archive_url)
    connection = http.client.HTTPSConnection(url.hostname, url.port or 443,
                                            timeout=15, context=ssl.create_default_context())
    deadline = time.monotonic() + 120
    try:
        # http.client uses no environment proxy, cookies, redirects or netrc.
        connection.request("GET", url.path, headers={"Accept-Encoding": "identity", "User-Agent": "a0-browser-bootstrap/1"})
        response = connection.getresponse()
        lengths = [value for key, value in response.getheaders() if key.lower() == "content-length"]
        if (response.status != 200 or len(lengths) != 1 or lengths[0] != str(pin.archive_size)
                or response.getheader("Content-Encoding", "identity") != "identity"
                or response.getheader("Transfer-Encoding") is not None):
            raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_DOWNLOAD_REJECTED")
        total = 0
        digest = hashlib.sha256()
        while True:
            if time.monotonic() >= deadline:
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_DOWNLOAD_TIMEOUT")
            chunk = response.read(min(65536, pin.archive_size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > pin.archive_size:
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_DOWNLOAD_REJECTED")
            digest.update(chunk)
            destination.write(chunk)
        if total != pin.archive_size or digest.hexdigest() != pin.archive_sha256:
            raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_DIGEST_MISMATCH")
        destination.flush()
        os.fsync(destination.fileno())
    finally:
        connection.close()


def _extract(archive: BinaryIO, destination: BinaryIO, pin: ApprovedBootstrap) -> None:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    header = bytearray()
    remaining = pin.companion.executable_size
    digest = hashlib.sha256()
    trailing = 0
    total = 0
    limit = min(_LIMIT + 64 * 512 + 1024, max(1024 * 1024, pin.archive_size * 64))

    def consume(chunk: bytes) -> None:
        nonlocal remaining, trailing, total
        total += len(chunk)
        if total > limit:
            raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")
        if len(header) < 512:
            take = min(512 - len(header), len(chunk))
            header.extend(chunk[:take])
            chunk = chunk[take:]
            if len(header) == 512:
                def octal(field: bytes) -> int:
                    field = field.strip(b"\0 ")
                    if not field or any(byte not in b"01234567" for byte in field):
                        raise ValueError()
                    return int(field, 8)
                if (header[:100].rstrip(b"\0") != b"a0-browser-bridge"
                        or header[156:157] not in (b"0", b"\0")
                        or header[257:265] != b"ustar\x0000"
                        or any(header[157:257]) or any(header[345:500])
                        or octal(header[124:136]) != remaining
                        or not octal(header[100:108]) & 0o100
                        or octal(header[148:156]) != sum(header[:148]) + 256 + sum(header[156:])):
                    raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")
        if len(header) == 512 and chunk:
            take = min(remaining, len(chunk))
            destination.write(chunk[:take])
            digest.update(chunk[:take])
            remaining -= take
            rest = chunk[take:]
            if any(rest):
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")
            trailing += len(rest)
            if trailing > 64 * 512 + 511:
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")

    archive.seek(0)
    while data := archive.read(65536):
        while data:
            consume(decoder.decompress(data, 65536))
            data = decoder.unconsumed_tail
            if decoder.unused_data:
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")
    consume(decoder.flush())
    padding = (-pin.companion.executable_size) % 512
    if (not decoder.eof or len(header) != 512 or remaining or trailing < padding + 1024
            or (trailing - padding) % 512 or digest.hexdigest() != pin.companion.executable_sha256):
        raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_ARCHIVE_INVALID")
    destination.flush()
    os.fsync(destination.fileno())


@dataclass
class VerifiedBootstrap:
    path: str
    pin: ApprovedBootstrap
    descriptor: int
    identity: tuple[int, ...]

    def verify(self) -> None:
        path = Path(self.path)
        with release._directory(path.parent, private=True):
            info = os.fstat(self.descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o777 != 0o500
                    or release._identity(os.lstat(path)) != self.identity
                    or release._identity(info) != self.identity):
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_CHANGED")
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(self.descriptor, 65536):
                total += len(chunk)
                if total > self.pin.companion.executable_size:
                    raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_CHANGED")
                digest.update(chunk)
            if (total != self.pin.companion.executable_size
                    or digest.hexdigest() != self.pin.companion.executable_sha256
                    or release._identity(os.fstat(self.descriptor)) != self.identity):
                raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_CHANGED")


@contextmanager
def acquire_bootstrap() -> Iterator[VerifiedBootstrap | None]:
    """Retain the private verified bootstrap through its native invocation."""
    try:
        pin = _select()
        if pin is None:
            yield None
            return
        parent = Path(tempfile.gettempdir()).resolve(strict=True)
        info = parent.stat()
        if (not stat.S_ISDIR(info.st_mode)
                or not ((info.st_uid == os.getuid() and info.st_mode & 0o077 == 0)
                        or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_STAGING_UNAVAILABLE")
        with tempfile.TemporaryDirectory(prefix="a0-bootstrap-", dir=parent) as temporary:
            root = Path(temporary)
            with release._directory(root, private=True):
                pass
            archive_path = root / "payload.tar.gz"
            executable = root / "a0-browser-bridge"
            with archive_path.open("x+b") as archive:
                os.fchmod(archive.fileno(), 0o600)
                _download(pin, archive)
                with executable.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    _extract(archive, output, pin)
                    os.fchmod(output.fileno(), 0o500)
            with release._file(executable, private=True, executable=True):
                pass
            descriptor = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                verified = VerifiedBootstrap(str(executable), pin, descriptor, release._identity(os.fstat(descriptor)))
                verified.verify()
                yield verified
            finally:
                os.close(descriptor)
    except release.CompanionDiscoveryError:
        raise
    except (OSError, ValueError, TypeError, zlib.error, http.client.HTTPException):
        raise release.CompanionDiscoveryError("COMPANION_BOOTSTRAP_UNAVAILABLE") from None
