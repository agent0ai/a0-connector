"""Read-only discovery of an installed, release-pinned native companion.

This registry is package source, not downloaded metadata or user configuration.
Entries may be added only after the release pipeline has verified the catalog,
platform signature, provenance and exact extracted executable. Empty production
pins remain unavailable. Installation and browser registration stay native-owned.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any, Iterator


MINIMUM_SECURE_COMPANION = "2.12.0"
NATIVE_HOST = "io.agentzero.browser_bridge"
_MAX_STATE = 64 * 1024
_MAX_EXECUTABLE = 512 * 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}/\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_STATE_KEYS = frozenset((
    "schema_version", "install_id", "channel", "active_version",
    "active_artifact_sha256", "active_artifact_size", "platform", "artifact_arch",
    "previous", "installed_at_ms", "source", "registered_browsers",
    "release_catalog_key_id", "registrations", "last_transaction_id",
))
_MACOS_SUFFIXES = {
    "chrome": "Google/Chrome", "edge": "Microsoft Edge",
    "brave": "BraveSoftware/Brave-Browser", "vivaldi": "Vivaldi",
    "opera": "com.operasoftware.Opera", "chromium": "Chromium",
}
_LINUX_SUFFIXES = {
    "chrome": "google-chrome", "edge": "microsoft-edge",
    "brave": "BraveSoftware/Brave-Browser", "vivaldi": "vivaldi",
    "opera": "opera", "chromium": "chromium",
}


@dataclass(frozen=True)
class ApprovedCompanionRelease:
    version: str
    platform: str
    artifact_arch: str
    executable_sha256: str
    executable_size: int
    catalog_key_id: str
    catalog_sha256: str
    extension_origins: tuple[str, ...]


# Reviewed release-specific executable pins only. No test identities or ad-hoc
# development artifacts may be added to this production tuple.
MACOS_2_12_0_RELEASE = ApprovedCompanionRelease(
    version="2.12.0",
    platform="macos",
    artifact_arch="universal2",
    executable_sha256="8f8125212bcafa3ead9e8f44c3dc8bc7213ee9f44570dd63638a242f3b067b6f",
    executable_size=10_356_768,
    catalog_key_id="publisher-2026",
    catalog_sha256="9758f715d7648ee2246c82dc3e9a3574dfc55c07cad24d629166e0cd40ef35e4",
    extension_origins=("chrome-extension://nhliclifilepdkoolioacpjpijomfplj/",),
)
MACOS_2_12_1_RELEASE = ApprovedCompanionRelease(
    version="2.12.1",
    platform="macos",
    artifact_arch="universal2",
    executable_sha256="26e2bd4ca821b5b2ca7cde5336f1348d682f47855fee705cf43aafd992205890",
    executable_size=10_374_432,
    catalog_key_id="publisher-2026",
    catalog_sha256="3dcde8e12a571a98d8fe9dfd2c4086469d68e03da9129ee18a63505c834c82b3",
    extension_origins=("chrome-extension://nhliclifilepdkoolioacpjpijomfplj/",),
)
MACOS_2_12_2_RELEASE = ApprovedCompanionRelease(
    version="2.12.2",
    platform="macos",
    artifact_arch="universal2",
    executable_sha256="8cb84de1e66bbd52771b534cb1bfc83d69eb94ebc87b3f7669547bff68359080",
    executable_size=10_374_256,
    catalog_key_id="publisher-2026",
    catalog_sha256="f403678be1077192cc10930f3f1a2a43a55abfdaf0d8ba0615f2780153f046ab",
    extension_origins=("chrome-extension://nhliclifilepdkoolioacpjpijomfplj/",),
)
MACOS_2_12_3_RELEASE = ApprovedCompanionRelease(
    version="2.12.3",
    platform="macos",
    artifact_arch="universal2",
    executable_sha256="803a24e87f2568c5fbb1c9f5de400bd3b8aab60a16a6ac70f3814be4494f482b",
    executable_size=10_376_528,
    catalog_key_id="publisher-2026",
    catalog_sha256="a40176dcd2048e692996b3ba4f5bf5218fe5ab98fca085f85eaa6cbe9a9ebd29",
    extension_origins=("chrome-extension://nhliclifilepdkoolioacpjpijomfplj/",),
)
APPROVED_COMPANION_RELEASES: tuple[ApprovedCompanionRelease, ...] = (
    MACOS_2_12_0_RELEASE, MACOS_2_12_1_RELEASE, MACOS_2_12_2_RELEASE, MACOS_2_12_3_RELEASE,
)


class CompanionDiscoveryError(RuntimeError):
    """Fixed redacted reason only: never include paths, state or native output."""

    def __init__(self, code: str = "COMPANION_INSTALL_INTEGRITY_FAILED") -> None:
        super().__init__(code)
        self.code = code


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 32 or not _VERSION.fullmatch(value):
        raise CompanionDiscoveryError()
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _host_target() -> tuple[str, str] | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        return "macos", "universal2"
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "linux", "x86_64"
        if machine in {"aarch64", "arm64"}:
            return "linux", "aarch64"
    # Windows requires the native HKCU/DACL transaction before discovery can
    # safely trust its install state. Do not emulate Unix ownership there.
    return None


def _absolute(value: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise CompanionDiscoveryError()
    return path


def _paths(target: str) -> tuple[Path, Path, Path]:
    home = _absolute(os.environ.get("HOME", ""))
    if target == "macos":
        return home, home / "Library/Application Support/Agent Zero/Browser Bridge", home
    data = _absolute(os.environ["XDG_DATA_HOME"]) if os.environ.get("XDG_DATA_HOME") else home / ".local/share"
    config = _absolute(os.environ["XDG_CONFIG_HOME"]) if os.environ.get("XDG_CONFIG_HOME") else home / ".config"
    return home, data / "agent-zero/browser-bridge", config


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _directory(path: Path, *, private: bool = False) -> Iterator[int]:
    """Walk absolute directory components using retained no-follow handles."""
    descriptors: list[int] = []
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        descriptors.append(descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022):
            raise CompanionDiscoveryError()
        yield descriptor
        if _identity(os.lstat(path)) != _identity(os.fstat(descriptor)):
            raise CompanionDiscoveryError()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _file(path: Path, *, private: bool, executable: bool = False) -> Iterator[int]:
    with _directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or before.st_mode & (0o077 if private else 0o022)
                    or (executable and not before.st_mode & 0o100)):
                raise CompanionDiscoveryError()
            yield descriptor
            if (_identity(before) != _identity(os.fstat(descriptor))
                    or _identity(before) != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                raise CompanionDiscoveryError()
        finally:
            os.close(descriptor)


def _read(path: Path, *, private: bool = True) -> bytes:
    with _file(path, private=private) as descriptor:
        size = os.fstat(descriptor).st_size
        if size > _MAX_STATE:
            raise CompanionDiscoveryError()
        chunks = bytearray()
        while len(chunks) <= _MAX_STATE:
            chunk = os.read(descriptor, min(16384, _MAX_STATE + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) != size:
            raise CompanionDiscoveryError()
        return bytes(chunks)


def _json(bytes_: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CompanionDiscoveryError()
            result[key] = value
        return result
    value = json.loads(bytes_, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(CompanionDiscoveryError()))
    if not isinstance(value, dict):
        raise CompanionDiscoveryError()
    return value


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _pin_valid(pin: ApprovedCompanionRelease) -> bool:
    return (_version(pin.version) >= _version(MINIMUM_SECURE_COMPANION)
            and bool(_SHA256.fullmatch(pin.executable_sha256))
            and bool(_SHA256.fullmatch(pin.catalog_sha256))
            and bool(_IDENTIFIER.fullmatch(pin.catalog_key_id))
            and _bounded_int(pin.executable_size, 1, _MAX_EXECUTABLE)
            and 1 <= len(pin.extension_origins) <= 16
            and len(set(pin.extension_origins)) == len(pin.extension_origins)
            and all(_ORIGIN.fullmatch(origin) for origin in pin.extension_origins))


def _validate_state(state: dict[str, Any], target: tuple[str, str]) -> ApprovedCompanionRelease:
    if (set(state) != _STATE_KEYS or type(state["schema_version"]) is not int
            or state["schema_version"] != 1 or state["channel"] != "stable"
            or (state["platform"], state["artifact_arch"]) != target
            or state["source"] not in {"a0_cli", "interactive_installer"}
            or not _bounded_int(state["installed_at_ms"], 0, (1 << 53) - 1)
            or any(not isinstance(state[key], str) or not _IDENTIFIER.fullmatch(state[key])
                   for key in ("install_id", "last_transaction_id", "release_catalog_key_id"))):
        raise CompanionDiscoveryError()
    _version(state["active_version"])
    previous = state["previous"]
    if previous is not None:
        if (not isinstance(previous, dict) or set(previous) != {"version", "sha256"}
                or not isinstance(previous["sha256"], str) or not _SHA256.fullmatch(previous["sha256"])):
            raise CompanionDiscoveryError()
        _version(previous["version"])
    matches = [pin for pin in APPROVED_COMPANION_RELEASES
               if (pin.version, pin.platform, pin.artifact_arch) == (state["active_version"], *target)]
    if len(matches) != 1 or not _pin_valid(matches[0]):
        raise CompanionDiscoveryError("COMPANION_RELEASE_NOT_APPROVED")
    pin = matches[0]
    if (state["release_catalog_key_id"] != pin.catalog_key_id
            or state["active_artifact_sha256"] != pin.executable_sha256
            or type(state["active_artifact_size"]) is not int
            or state["active_artifact_size"] != pin.executable_size):
        raise CompanionDiscoveryError()
    return pin


def _registration_paths(state: dict[str, Any], home: Path, config: Path) -> set[Path]:
    browsers = state["registered_browsers"]
    if (not isinstance(browsers, list) or not 1 <= len(browsers) <= 6
            or any(not isinstance(browser, str) or browser not in _MACOS_SUFFIXES for browser in browsers)
            or len(set(browsers)) != len(browsers)):
        raise CompanionDiscoveryError()
    paths: set[Path] = set()
    for browser in browsers:
        if state["platform"] == "macos":
            paths.add(home / "Library/Application Support" / _MACOS_SUFFIXES[browser] / "NativeMessagingHosts" / f"{NATIVE_HOST}.json")
        else:
            paths.add(config / _LINUX_SUFFIXES[browser] / "NativeMessagingHosts" / f"{NATIVE_HOST}.json")
            if browser == "opera":
                paths.add(config / _LINUX_SUFFIXES["chrome"] / "NativeMessagingHosts" / f"{NATIVE_HOST}.json")
    return paths


def _verify_registrations(state: dict[str, Any], pin: ApprovedCompanionRelease,
                          binary: Path, home: Path, config: Path) -> None:
    registrations = state["registrations"]
    if not isinstance(registrations, list) or not 1 <= len(registrations) <= 7:
        raise CompanionDiscoveryError()
    records: dict[str, str] = {}
    for entry in registrations:
        if (not isinstance(entry, dict) or set(entry) != {"path_sha256", "manifest_sha256"}
                or any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in entry.values())
                or entry["path_sha256"] in records):
            raise CompanionDiscoveryError()
        records[entry["path_sha256"]] = entry["manifest_sha256"]
    paths = _registration_paths(state, home, config)
    if {hashlib.sha256(os.fsencode(path)).hexdigest() for path in paths} != set(records):
        raise CompanionDiscoveryError()
    for path in paths:
        raw = _read(path, private=False)
        if hashlib.sha256(raw).hexdigest() != records[hashlib.sha256(os.fsencode(path)).hexdigest()]:
            raise CompanionDiscoveryError()
        manifest = _json(raw)
        if manifest != {"name": NATIVE_HOST, "description": "Agent Zero browser bridge",
                        "type": "stdio", "path": str(binary), "allowed_origins": list(pin.extension_origins)}:
            raise CompanionDiscoveryError()


def resolve_installed_companion() -> str | None:
    """Return only a privately held, immutable, exact release-pinned executable.

    No path leaves this internal API through the machine-output contract. This
    is discovery, not installation authority or a fresh signature verification.
    """
    if not APPROVED_COMPANION_RELEASES:
        return None
    target = _host_target()
    if target is None:
        return None
    try:
        home, root, config = _paths(target[0])
        try:
            os.lstat(root)
        except FileNotFoundError:
            return None
        with _directory(root, private=True), _file(root / "install.lock", private=True) as lock:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CompanionDiscoveryError("COMPANION_INSTALL_BUSY") from None
            with _directory(root / "transactions", private=True) as transactions:
                if os.listdir(transactions):
                    raise CompanionDiscoveryError("COMPANION_INSTALL_RECOVERY_REQUIRED")
            raw_state = _read(root / "install-state.json")
            state = _json(raw_state)
            pin = _validate_state(state, target)
            binary = root / "releases" / pin.version / f"{pin.platform}-{pin.artifact_arch}" / "a0-browser-bridge"
            with _directory(root / "releases", private=True), _directory(binary.parent.parent, private=True), _directory(binary.parent, private=True):
                with _file(binary, private=True, executable=True) as executable:
                    if os.fstat(executable).st_size != pin.executable_size:
                        raise CompanionDiscoveryError()
                    digest = hashlib.sha256()
                    total = 0
                    while True:
                        chunk = os.read(executable, 1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > pin.executable_size:
                            raise CompanionDiscoveryError()
                        digest.update(chunk)
                    if total != pin.executable_size or digest.hexdigest() != pin.executable_sha256:
                        raise CompanionDiscoveryError()
                _verify_registrations(state, pin, binary, home, config)
                if _read(root / "install-state.json") != raw_state:
                    raise CompanionDiscoveryError()
            return str(binary)
    except CompanionDiscoveryError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise CompanionDiscoveryError() from None
