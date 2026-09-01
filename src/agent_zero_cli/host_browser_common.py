from __future__ import annotations

import asyncio
from functools import lru_cache
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

# Legacy location kept as a public constant for compatibility. The helper source is
# owned by Agent Zero's Browser plugin and is delivered over the connector protocol.
CONTENT_HELPER_PATH = Path(__file__).resolve().parent / "assets" / "browser-page-content.js"
CONTENT_HELPER_PAYLOAD_KEY = "content_helper"
DOM_HELPER_PAYLOAD_KEY = "dom_helper"
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
CHROME_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
HOST_BROWSER_ARTIFACT_ROOT_ENV = "A0_HOST_BROWSER_ARTIFACT_ROOT"
DEFAULT_HOST_BROWSER_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "_a0_connector" / "host_browser"
PLAYWRIGHT_PYTHON_PACKAGE = "playwright"
SAFARI_EXECUTABLE_PATH = Path("/Applications/Safari.app/Contents/MacOS/Safari")
SAFARI_DRIVER_PATH = Path("/usr/bin/safaridriver")
HOST_BROWSER_OZONE_PLATFORM_ENV = "A0_HOST_BROWSER_OZONE_PLATFORM"
HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS_ENV = "A0_HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS"
REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS = 60.0
REMOTE_DEBUGGING_RESTRICTED_MAJOR = 136
REMOTE_DEBUGGING_ENABLE_URL = "chrome://inspect/#remote-debugging"
REMOTE_DEBUGGING_ENABLE_LABEL = "Allow remote debugging for this browser instance"
RELAUNCH_CONTEXT_ID = "_a0_cli_browser_check"
MAX_INSTALL_OUTPUT_CHARS = 4000
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z\d+\-.]*:", re.I)
_SAFE_CONTEXT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SUPPORTED_ACTIONS = {
    "open",
    "open_remote_debugging",
    "list",
    "state",
    "set_active",
    "navigate",
    "back",
    "forward",
    "reload",
    "content",
    "detail",
    "evaluate",
    "click",
    "type",
    "submit",
    "type_submit",
    "scroll",
    "hover",
    "double_click",
    "right_click",
    "drag",
    "wheel",
    "keyboard",
    "key_chord",
    "clipboard",
    "set_viewport",
    "select_option",
    "set_checked",
    "upload_file",
    "mouse",
    "screenshot",
    "screenshot_file",
    "close",
    "close_all",
    "ensure",
    "multi",
    "status",
}
_SENSITIVE_ACTIONS = {"content", "detail", "evaluate", "screenshot", "screenshot_file"}
_VALID_MODIFIERS = {"Control", "Shift", "Alt", "Meta"}
BROWSER_REEXPORTS = [
    "CONTENT_HELPER_PATH",
    "CONTENT_HELPER_PAYLOAD_KEY",
    "DOM_HELPER_PAYLOAD_KEY",
    "DEFAULT_VIEWPORT",
    "CHROME_SINGLETON_FILES",
    "HOST_BROWSER_ARTIFACT_ROOT_ENV",
    "DEFAULT_HOST_BROWSER_ARTIFACT_ROOT",
    "PLAYWRIGHT_PYTHON_PACKAGE",
    "SAFARI_EXECUTABLE_PATH",
    "SAFARI_DRIVER_PATH",
    "HOST_BROWSER_OZONE_PLATFORM_ENV",
    "HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS_ENV",
    "REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS",
    "REMOTE_DEBUGGING_RESTRICTED_MAJOR",
    "REMOTE_DEBUGGING_ENABLE_URL",
    "REMOTE_DEBUGGING_ENABLE_LABEL",
    "RELAUNCH_CONTEXT_ID",
    "MAX_INSTALL_OUTPUT_CHARS",
    "BrowserCandidate",
    "BrowserProfile",
    "ProfileLockState",
    "normalize_action",
    "action_is_sensitive",
    "normalize_url",
    "detect_browser_candidates",
    "a0_managed_user_data_dir",
    "is_a0_managed_family",
    "is_remote_debugging_family",
    "base_browser_family",
    "discover_profiles",
    "discover_remote_debugging_profiles",
    "remote_debugging_endpoint_candidates",
    "normalize_remote_debugging_endpoint",
    "remote_debugging_endpoint_from_user_data_dir",
    "remote_debugging_endpoint_from_active_port_file",
    "remote_debugging_profile_from_candidate",
    "remote_debugging_endpoint_label",
    "normalize_host_browser_selection",
    "profile_lock_state",
    "profile_lock_state_for_profile",
    "is_profile_locked",
    "chromium_launch_args",
    "remote_debugging_restriction_reason",
    "remote_debugging_enable_hint",
    "browser_major_version",
    "is_default_user_data_dir",
    "default_user_data_dirs",
    "coerce_bool",
    "coerce_int",
    "coerce_float",
    "has_ref",
    "require_ref",
    "normalize_modifiers",
    "normalize_upload_paths",
    "artifact_root",
    "safe_context_id",
    "screenshot_output_path",
    "multi_group_key",
    "playwright_python_install_command",
    "playwright_python_install_commands",
    "content_helper_sha256",
    "parse_dom_helper_payload",
    "parse_content_helper_payload",
    "format_profile_rows",
]
__all__ = [*BROWSER_REEXPORTS, "_SUPPORTED_ACTIONS"]


def content_helper_sha256(source: str) -> str:
    return hashlib.sha256(str(source or "").encode("utf-8")).hexdigest()


def parse_content_helper_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
    helper = payload.get(CONTENT_HELPER_PAYLOAD_KEY)
    source = ""
    expected_hash = ""
    required_apis: tuple[str, ...] = ()
    if isinstance(helper, dict):
        source = str(helper.get("source") or "")
        expected_hash = str(helper.get("sha256") or "").strip().lower()
        value = helper.get("required_apis")
        if isinstance(value, (list, tuple)):
            required_apis = tuple(str(item).strip() for item in value if str(item).strip())
    elif isinstance(payload.get("content_helper_source"), str):
        source = str(payload.get("content_helper_source") or "")
        expected_hash = str(payload.get("content_helper_sha256") or "").strip().lower()

    if not source:
        return None

    if "__spaceBrowserPageContent__" not in source:
        raise ValueError("Host browser content helper is missing the expected global API.")
    if "ready" not in source:
        raise ValueError("Host browser content helper is missing the expected ready API.")
    missing = [name for name in required_apis if name not in source]
    if missing:
        raise ValueError(
            "Host browser content helper is missing required API(s): "
            + ", ".join(missing)
        )

    actual_hash = content_helper_sha256(source)
    if expected_hash and expected_hash != actual_hash:
        raise ValueError(
            "Host browser content helper checksum mismatch: "
            f"expected {expected_hash}, got {actual_hash}."
        )
    return source, actual_hash


def parse_dom_helper_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
    helper = payload.get(DOM_HELPER_PAYLOAD_KEY)
    source = ""
    expected_hash = ""
    required_apis: tuple[str, ...] = ()
    if isinstance(helper, dict):
        source = str(helper.get("source") or "")
        expected_hash = str(helper.get("sha256") or "").strip().lower()
        value = helper.get("required_apis")
        if isinstance(value, (list, tuple)):
            required_apis = tuple(str(item).strip() for item in value if str(item).strip())
    elif isinstance(payload.get("dom_helper_source"), str):
        source = str(payload.get("dom_helper_source") or "")
        expected_hash = str(payload.get("dom_helper_sha256") or "").strip().lower()

    if not source:
        return None

    if "__spaceBrowserDomHelper__" not in source:
        raise ValueError("Host browser DOM helper is missing the expected global API.")
    missing = [name for name in required_apis if name not in source]
    if missing:
        raise ValueError(
            "Host browser DOM helper is missing required API(s): "
            + ", ".join(missing)
        )

    actual_hash = content_helper_sha256(source)
    if expected_hash and expected_hash != actual_hash:
        raise ValueError(
            "Host browser DOM helper checksum mismatch: "
            f"expected {expected_hash}, got {actual_hash}."
        )
    return source, actual_hash


@dataclass(frozen=True)
class BrowserCandidate:
    family: str
    label: str
    executable_path: str
    user_data_dir: Path


@dataclass(frozen=True)
class BrowserProfile:
    family: str
    family_label: str
    executable_path: str
    user_data_dir: Path
    profile_directory: str
    display_name: str
    cdp_endpoint: str = ""

    @property
    def profile_path(self) -> Path:
        return self.user_data_dir

    @property
    def profile_path_display(self) -> str:
        if self.is_safari:
            return self.executable_path
        return self.cdp_endpoint or str(self.profile_path)

    @property
    def profile_label(self) -> str:
        return self.profile_directory

    @property
    def is_remote_debugging(self) -> bool:
        return bool(self.cdp_endpoint)

    @property
    def is_safari(self) -> bool:
        return self.family == "safari"

    @property
    def browser_id(self) -> str:
        if self.cdp_endpoint:
            if self.user_data_dir != Path():
                return normalize_host_browser_selection(self.family)
            return normalize_host_browser_selection(self.cdp_endpoint)
        label = normalize_host_browser_selection(self.profile_label)
        family = normalize_host_browser_selection(self.family)
        return f"{family}:{label or 'default'}"

    @property
    def browser_label(self) -> str:
        return f"{self.family_label} - {self.display_name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "browser_id": self.browser_id,
            "browser_label": self.browser_label,
            "family": self.family,
            "family_label": self.family_label,
            "executable_path": self.executable_path,
            "profile_path": self.profile_path_display,
            "profile_label": self.profile_label,
            "display_name": self.display_name,
            "cdp_endpoint": self.cdp_endpoint,
            "locked": False
            if self.is_remote_debugging or self.is_safari
            else is_profile_locked(self.profile_path),
        }


@dataclass(frozen=True)
class ProfileLockState:
    locked: bool
    lock_files: tuple[str, ...] = ()
    owner_pid: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "locked": self.locked,
            "lock_files": list(self.lock_files),
            "owner_pid": self.owner_pid,
        }


def normalize_action(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "setactive": "set_active",
        "activate": "set_active",
        "focus": "set_active",
        "typesubmit": "type_submit",
        "keychord": "key_chord",
        "close_browser": "close",
        "close_all_browsers": "close_all",
    }
    return aliases.get(normalized, normalized)


def action_is_sensitive(payload: dict[str, Any]) -> bool:
    action = normalize_action(payload.get("action"))
    if action in _SENSITIVE_ACTIONS:
        return True
    if action == "list" and coerce_bool(payload.get("include_content")):
        return True
    if action == "multi":
        calls = payload.get("calls")
        if isinstance(calls, list):
            return any(action_is_sensitive(call) for call in calls if isinstance(call, dict))
    return False


def normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Browser navigation requires a non-empty URL.")
    if not _URL_SCHEME_RE.match(raw):
        raise ValueError(
            "Host browser navigation requires an Agent Zero-normalized URL with a scheme."
        )
    return raw


def detect_browser_candidates() -> list[BrowserCandidate]:
    system = platform.system()
    if system == "Darwin":
        return _detect_macos_candidates()
    if system == "Windows":
        return _detect_windows_candidates()
    return _detect_linux_candidates()


def _detect_macos_candidates() -> list[BrowserCandidate]:
    home = Path.home()
    specs = [
        ("chrome", "Google Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), home / "Library/Application Support/Google/Chrome"),
        ("chromium", "Chromium", Path("/Applications/Chromium.app/Contents/MacOS/Chromium"), home / "Library/Application Support/Chromium"),
        ("edge", "Microsoft Edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"), home / "Library/Application Support/Microsoft Edge"),
        ("brave", "Brave", Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"), home / "Library/Application Support/BraveSoftware/Brave-Browser"),
        ("opera", "Opera", Path("/Applications/Opera.app/Contents/MacOS/Opera"), home / "Library/Application Support/com.operasoftware.Opera"),
        ("vivaldi", "Vivaldi", Path("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"), home / "Library/Application Support/Vivaldi"),
    ]
    candidates = [
        BrowserCandidate(f, label, str(exe), profile)
        for f, label, exe, profile in specs
        if exe.exists()
    ]
    candidates = _with_a0_managed_candidates(candidates)
    if SAFARI_EXECUTABLE_PATH.exists():
        candidates.append(
            BrowserCandidate(
                "safari",
                "Safari",
                str(SAFARI_EXECUTABLE_PATH),
                Path(),
            )
        )
    return candidates


def _detect_windows_candidates() -> list[BrowserCandidate]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming_app_data = Path(os.environ.get("APPDATA", ""))
    program_files = [Path(os.environ.get("PROGRAMFILES", "")), Path(os.environ.get("PROGRAMFILES(X86)", ""))]
    specs: list[tuple[str, str, list[Path], Path]] = [
        (
            "chrome",
            "Google Chrome",
            [base / "Google/Chrome/Application/chrome.exe" for base in program_files if str(base)],
            local_app_data / "Google/Chrome/User Data",
        ),
        (
            "chromium",
            "Chromium",
            [local_app_data / "Chromium/Application/chrome.exe"],
            local_app_data / "Chromium/User Data",
        ),
        (
            "edge",
            "Microsoft Edge",
            [base / "Microsoft/Edge/Application/msedge.exe" for base in program_files if str(base)],
            local_app_data / "Microsoft/Edge/User Data",
        ),
        (
            "brave",
            "Brave",
            [local_app_data / "BraveSoftware/Brave-Browser/Application/brave.exe"],
            local_app_data / "BraveSoftware/Brave-Browser/User Data",
        ),
        (
            "opera",
            "Opera",
            [
                local_app_data / "Programs/Opera/opera.exe",
                *[base / "Opera/opera.exe" for base in program_files if str(base)],
            ],
            roaming_app_data / "Opera Software/Opera Stable",
        ),
        (
            "vivaldi",
            "Vivaldi",
            [local_app_data / "Vivaldi/Application/vivaldi.exe"],
            local_app_data / "Vivaldi/User Data",
        ),
    ]
    candidates: list[BrowserCandidate] = []
    for family, label, executables, profile in specs:
        executable = next((path for path in executables if path.exists()), None)
        if executable is not None:
            candidates.append(BrowserCandidate(family, label, str(executable), profile))
    return _with_a0_managed_candidates(candidates)


def _detect_linux_candidates() -> list[BrowserCandidate]:
    home_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    specs = [
        ("chrome", "Google Chrome", ("google-chrome", "google-chrome-stable"), home_config / "google-chrome"),
        ("chromium", "Chromium", ("chromium", "chromium-browser"), home_config / "chromium"),
        ("edge", "Microsoft Edge", ("microsoft-edge", "microsoft-edge-stable"), home_config / "microsoft-edge"),
        ("edge-dev", "Microsoft Edge Dev", ("microsoft-edge-dev",), home_config / "microsoft-edge-dev"),
        ("brave", "Brave", ("brave-browser", "brave-browser-stable", "brave"), home_config / "BraveSoftware/Brave-Browser"),
        ("opera", "Opera", ("opera", "opera-stable"), home_config / "opera"),
        ("vivaldi", "Vivaldi", ("vivaldi", "vivaldi-stable"), home_config / "vivaldi"),
    ]
    candidates: list[BrowserCandidate] = []
    for family, label, names, profile in specs:
        executable = next((shutil.which(name) for name in names if shutil.which(name)), None)
        if executable:
            candidates.append(BrowserCandidate(family, label, executable, profile))
    return _with_a0_managed_candidates(candidates)


def _with_a0_managed_candidates(candidates: list[BrowserCandidate]) -> list[BrowserCandidate]:
    expanded: list[BrowserCandidate] = []
    for candidate in candidates:
        expanded.append(candidate)
        expanded.append(
            BrowserCandidate(
                family=f"{candidate.family}-a0",
                label=f"{candidate.label} (A0 controlled profile)",
                executable_path=candidate.executable_path,
                user_data_dir=a0_managed_user_data_dir(candidate.family),
            )
        )
    return expanded


def a0_managed_user_data_dir(family: str) -> Path:
    family_slug = _SAFE_CONTEXT_RE.sub("-", str(family or "chrome").strip().lower()).strip("-")
    system = platform.system()
    if system == "Darwin":
        root = Path.home() / "Library/Application Support/A0/Browser Profiles"
    elif system == "Windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        root = local_app_data / "A0/Browser Profiles"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "a0/browser-profiles"
    return root / family_slug


def is_a0_managed_family(family: str) -> bool:
    return str(family or "").strip().lower().endswith("-a0")


def is_remote_debugging_family(family: str) -> bool:
    return str(family or "").strip().lower().endswith("-cdp")


def base_browser_family(family: str) -> str:
    normalized = str(family or "").strip().lower()
    for suffix in ("-a0", "-cdp"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def discover_profiles(candidate: BrowserCandidate) -> list[BrowserProfile]:
    if candidate.family == "safari":
        return [
            BrowserProfile(
                family="safari",
                family_label=candidate.label,
                executable_path=candidate.executable_path,
                user_data_dir=Path(),
                profile_directory="Default",
                display_name="Automation window",
            )
        ]
    root = candidate.user_data_dir.expanduser()
    if is_a0_managed_family(candidate.family):
        return [
            BrowserProfile(
                family=candidate.family,
                family_label=candidate.label,
                executable_path=candidate.executable_path,
                user_data_dir=root,
                profile_directory="Default",
                display_name="A0 controlled",
            )
        ]
    if not root.exists():
        return []
    display_names = _profile_display_names(root)
    profile_dirs = _profile_directories(root)
    profiles: list[BrowserProfile] = []
    for profile_dir in profile_dirs:
        display = display_names.get(profile_dir.name) or profile_dir.name
        profiles.append(
            BrowserProfile(
                family=candidate.family,
                family_label=candidate.label,
                executable_path=candidate.executable_path,
                user_data_dir=root,
                profile_directory=profile_dir.name,
                display_name=display,
            )
        )
    return profiles


def discover_remote_debugging_profiles(candidates: Iterable[BrowserCandidate] | None = None) -> list[BrowserProfile]:
    profiles: list[BrowserProfile] = []
    seen: set[str] = set()
    candidate_list = list(candidates) if candidates is not None else detect_browser_candidates()
    for candidate in candidate_list:
        if is_a0_managed_family(candidate.family) or candidate.family == "safari":
            continue
        endpoint = remote_debugging_endpoint_from_user_data_dir(candidate.user_data_dir)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        profiles.append(remote_debugging_profile_from_candidate(candidate, endpoint))
    for endpoint in remote_debugging_endpoint_candidates():
        if endpoint in seen:
            continue
        seen.add(endpoint)
        profiles.append(
            BrowserProfile(
                family="chrome-cdp",
                family_label="Chromium-family browser (remote debugging)",
                executable_path="",
                user_data_dir=Path(),
                profile_directory=remote_debugging_endpoint_label(endpoint),
                display_name="Remote debugging allowed",
                cdp_endpoint=endpoint,
            )
        )
    return profiles


def remote_debugging_endpoint_candidates() -> list[str]:
    raw = os.environ.get(HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS_ENV, "")
    values = re.split(r"[\s,]+", raw.strip()) if raw.strip() else []
    endpoints: list[str] = []
    seen: set[str] = set()
    for value in values:
        endpoint = normalize_remote_debugging_endpoint(value)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints


def normalize_remote_debugging_endpoint(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        if not re.fullmatch(r"(?:\[[^\]]+\]|[^/:\s]+):\d+", raw):
            return ""
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if not parsed.netloc:
        return ""
    if parsed.scheme in {"http", "https"}:
        if parsed.path not in {"", "/", "/json/version"}:
            return ""
        path = "" if parsed.path in {"", "/"} else parsed.path
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    if parsed.scheme in {"ws", "wss"} and parsed.path in {"", "/"}:
        scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunsplit((scheme, parsed.netloc, "", parsed.query, ""))
    if parsed.scheme not in {"ws", "wss"} or not parsed.path:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def remote_debugging_endpoint_from_user_data_dir(user_data_dir: Path | str) -> str:
    return remote_debugging_endpoint_from_active_port_file(Path(user_data_dir).expanduser() / "DevToolsActivePort")


def remote_debugging_endpoint_from_active_port_file(active_port_file: Path | str) -> str:
    path = Path(active_port_file).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if len(lines) < 2:
        return ""
    port = lines[0].strip()
    devtools_path = lines[1].strip()
    if not port.isdigit() or not devtools_path.startswith("/"):
        return ""
    return normalize_remote_debugging_endpoint(f"ws://localhost:{int(port)}{devtools_path}")


def remote_debugging_profile_from_candidate(candidate: BrowserCandidate, endpoint: str) -> BrowserProfile:
    family = f"{base_browser_family(candidate.family)}-cdp"
    return BrowserProfile(
        family=family,
        family_label=f"{candidate.label} (remote debugging)",
        executable_path="",
        user_data_dir=candidate.user_data_dir.expanduser(),
        profile_directory=remote_debugging_endpoint_label(endpoint),
        display_name="Remote debugging allowed",
        cdp_endpoint=endpoint,
    )


def remote_debugging_endpoint_label(endpoint: str) -> str:
    normalized = normalize_remote_debugging_endpoint(endpoint) or str(endpoint or "")
    parsed = urlsplit(normalized if "://" in normalized else f"ws://{normalized}")
    return parsed.netloc or "remote debugging"


def normalize_host_browser_selection(value: object) -> str:
    endpoint = normalize_remote_debugging_endpoint(value)
    if endpoint:
        return endpoint
    raw = re.sub(r"\s+", "_", str(value or "").strip().lower())
    return "".join(ch for ch in raw if ch.isalnum() or ch in {"_", "-", ":", ".", "/"})[:200]


def _profile_directories(user_data_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for name in ("Default", "Guest Profile"):
        path = user_data_dir / name
        if path.is_dir():
            candidates.append(path)
    candidates.extend(sorted(path for path in user_data_dir.glob("Profile *") if path.is_dir()))
    if not candidates and user_data_dir.exists():
        candidates.append(user_data_dir / "Default")
    return candidates


def _profile_display_names(user_data_dir: Path) -> dict[str, str]:
    local_state = user_data_dir / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except Exception:
        return {}
    info_cache = data.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        return {}
    names: dict[str, str] = {}
    for profile_dir, info in info_cache.items():
        if isinstance(info, dict):
            name = str(info.get("name") or info.get("user_name") or "").strip()
            if name:
                names[str(profile_dir)] = name
    return names


def profile_lock_state(profile_path: Path | str) -> ProfileLockState:
    root = Path(profile_path).expanduser()
    lock_files: list[str] = []
    owner_pid: int | None = None
    for name in CHROME_SINGLETON_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            lock_files.append(str(path))
            if name == "SingletonLock":
                owner_pid = owner_pid or _singleton_lock_owner_pid(path)
    if owner_pid is not None and not _pid_is_alive(owner_pid):
        return ProfileLockState(locked=False, lock_files=(), owner_pid=owner_pid)
    return ProfileLockState(locked=bool(lock_files), lock_files=tuple(lock_files), owner_pid=owner_pid)


def profile_lock_state_for_profile(profile: BrowserProfile) -> ProfileLockState:
    if profile.is_safari:
        return ProfileLockState(False)
    if profile.is_remote_debugging:
        return ProfileLockState(False)
    return profile_lock_state(profile.profile_path)


def is_profile_locked(profile_path: Path | str) -> bool:
    return profile_lock_state(profile_path).locked


def _singleton_lock_owner_pid(path: Path) -> int | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    raw_pid = target.rsplit("-", 1)[-1]
    if raw_pid.isdigit():
        return int(raw_pid)
    return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def chromium_launch_args(profile_directory: str) -> list[str]:
    args = [f"--profile-directory={profile_directory}"]
    explicit_ozone = os.environ.get(HOST_BROWSER_OZONE_PLATFORM_ENV, "").strip()
    if explicit_ozone:
        args.append(f"--ozone-platform={explicit_ozone}")
    elif (
        platform.system() == "Linux"
        and os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("DISPLAY")
    ):
        args.append("--ozone-platform=wayland")
    return args


def remote_debugging_restriction_reason(profile: BrowserProfile) -> str:
    if profile.is_remote_debugging:
        return ""
    if is_a0_managed_family(profile.family):
        return ""
    major = browser_major_version(profile.executable_path)
    if (
        major is not None
        and major >= REMOTE_DEBUGGING_RESTRICTED_MAJOR
        and is_default_user_data_dir(profile.family, profile.user_data_dir)
    ):
        managed_family = f"{profile.family}-a0"
        return (
            "This Chromium-family browser blocks Playwright remote debugging for its default "
            f"data directory in version {major}+. {remote_debugging_enable_hint()} "
            "Or choose Clean Agent profile in Browser settings, or select the "
            f"A0-controlled local profile with /browser profile "
            f"{managed_family} Default, then run /browser relaunch. "
            "Cookies and site data stay inside that separate browser profile on this host."
        )
    return ""


def remote_debugging_enable_hint() -> str:
    return (
        f"Open {REMOTE_DEBUGGING_ENABLE_URL} in the browser you want Agent Zero to use, "
        f"enable \"{REMOTE_DEBUGGING_ENABLE_LABEL}\", choose a browser profile and click "
        "Allow if Chrome asks, then retry."
    )


@lru_cache(maxsize=32)
def browser_major_version(executable_path: str) -> int | None:
    if platform.system() == "Windows":
        try:
            import win32api

            version = win32api.GetFileVersionInfo(executable_path, "\\")
            return (int(version["FileVersionMS"]) >> 16) & 0xFFFF
        except Exception:
            return None
    try:
        result = subprocess.run(
            [executable_path, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    match = re.search(r"\b(\d+)\.", result.stdout or "")
    if not match:
        return None
    return int(match.group(1))


def is_default_user_data_dir(family: str, user_data_dir: Path | str) -> bool:
    normalized_family = str(family or "").strip().lower().removesuffix("-a0")
    root = _resolve_path(user_data_dir)
    return any(root == _resolve_path(path) for path in default_user_data_dirs(normalized_family))


def default_user_data_dirs(family: str) -> list[Path]:
    normalized_family = str(family or "").strip().lower()
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        base = home / "Library/Application Support"
        mapping = {
            "chrome": base / "Google/Chrome",
            "chromium": base / "Chromium",
            "edge": base / "Microsoft Edge",
            "edge-dev": base / "Microsoft Edge Dev",
        }
    elif system == "Windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        mapping = {
            "chrome": local_app_data / "Google/Chrome/User Data",
            "chromium": local_app_data / "Chromium/User Data",
            "edge": local_app_data / "Microsoft/Edge/User Data",
            "edge-dev": local_app_data / "Microsoft/Edge Dev/User Data",
        }
    else:
        home_config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        mapping = {
            "chrome": home_config / "google-chrome",
            "chromium": home_config / "chromium",
            "edge": home_config / "microsoft-edge",
            "edge-dev": home_config / "microsoft-edge-dev",
        }
    return [mapping[normalized_family]] if normalized_family in mapping else []


def _resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled", ""}:
        return False
    return default


def coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def has_ref(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def require_ref(value: object, action: str) -> int | str:
    if not has_ref(value):
        raise ValueError(f"{action} requires ref")
    return value  # type: ignore[return-value]


def normalize_modifiers(modifiers: list[str] | str | None) -> list[str] | None:
    if modifiers is None:
        return None
    raw = [modifiers] if isinstance(modifiers, str) else list(modifiers)
    normalized = [str(item).strip() for item in raw if str(item).strip()]
    if not normalized:
        return None
    invalid = set(normalized) - _VALID_MODIFIERS
    if invalid:
        raise ValueError(f"unsupported modifiers: {sorted(invalid)}; allowed: {sorted(_VALID_MODIFIERS)}")
    return normalized


def normalize_upload_paths(path: str = "", paths: list[str] | None = None) -> list[str]:
    raw_paths: list[str] = []
    if isinstance(paths, list):
        raw_paths.extend(str(item or "").strip() for item in paths)
    if str(path or "").strip():
        raw_paths.append(str(path or "").strip())
    normalized: list[str] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Upload file does not exist on the CLI host: {candidate}")
        normalized.append(str(candidate))
    if not normalized:
        raise ValueError("upload_file requires path or non-empty paths")
    return normalized


def artifact_root() -> Path:
    configured = os.environ.get(HOST_BROWSER_ARTIFACT_ROOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_HOST_BROWSER_ARTIFACT_ROOT


def safe_context_id(context_id: str) -> str:
    return _SAFE_CONTEXT_RE.sub("_", str(context_id or "default")).strip("._") or "default"


def screenshot_output_path(context_id: str, browser_id: int, path: str = "") -> tuple[Path, str, str]:
    raw_path = str(path or "").strip()
    if raw_path:
        output_path = Path(raw_path).expanduser()
        if not output_path.is_absolute():
            output_path = artifact_root() / safe_context_id(context_id) / output_path
        suffix = output_path.suffix.lower()
        if suffix == ".png":
            return output_path, "png", "image/png"
        if suffix not in {".jpg", ".jpeg"}:
            output_path = output_path.with_suffix(".jpg") if not suffix else output_path.with_name(f"{output_path.name}.jpg")
        return output_path, "jpeg", "image/jpeg"

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    millis = int((time.time() % 1) * 1000)
    output_path = artifact_root() / safe_context_id(context_id) / f"host-browser-{int(browser_id)}-{timestamp}-{millis:03d}.jpg"
    return output_path, "jpeg", "image/jpeg"


def multi_group_key(call: dict[str, Any]) -> Any:
    value = call.get("browser_id")
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    if raw.startswith("browser-"):
        raw = raw.split("-", 1)[1]
    try:
        return int(raw)
    except ValueError:
        return raw


def playwright_python_install_command(python_executable: str = sys.executable) -> list[str]:
    return playwright_python_install_commands(python_executable)[0]


def playwright_python_install_commands(python_executable: str = sys.executable) -> list[list[str]]:
    uv = shutil.which("uv")
    if uv:
        return [
            [
                uv,
                "pip",
                "install",
                "--python",
                python_executable,
                PLAYWRIGHT_PYTHON_PACKAGE,
            ]
        ]
    return [[python_executable, "-m", "pip", "install", PLAYWRIGHT_PYTHON_PACKAGE]]


async def _run_install_command(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    return int(process.returncode or 0), output


def _trim_install_output(output: str) -> str:
    cleaned = str(output or "").strip()
    if not cleaned:
        return "no output"
    if len(cleaned) <= MAX_INSTALL_OUTPUT_CHARS:
        return cleaned
    return "..." + cleaned[-MAX_INSTALL_OUTPUT_CHARS:]


def format_profile_rows(profiles: Iterable[BrowserProfile]) -> list[str]:
    rows = []
    for profile in profiles:
        lock = (
            "allowed"
            if profile.is_remote_debugging
            else "locked"
            if not profile.is_safari and is_profile_locked(profile.profile_path)
            else "ready"
        )
        rows.append(
            f"{profile.family} {profile.profile_label} - {profile.display_name} "
            f"({profile.profile_path_display}) [{lock}]"
        )
    return rows
