from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from textwrap import dedent
from typing import Callable, Mapping
from urllib.parse import quote, urlparse
from urllib.request import Request, url2pathname, urlopen

from agent_zero_cli.text_utils import coerce_bool


PACKAGE_NAME = "a0"
GITHUB_REPOSITORY = "agent0ai/a0-connector"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASE_ARCHIVE_URL_TEMPLATE = (
    f"https://github.com/{GITHUB_REPOSITORY}/archive/refs/tags/{{tag}}.zip"
)
RELEASE_RAW_FILE_URL_TEMPLATE = (
    f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/refs/tags/{{tag}}/{{path}}"
)
RUNTIME_CONSTRAINTS_PATH = "constraints/a0-runtime.txt"
BUILD_CONSTRAINTS_PATH = "constraints/a0-build.txt"
DEFAULT_PYTHON_SPEC = "3.12"
_GITHUB_API_TIMEOUT = 10.0
_VERSION_PATTERN = re.compile(r"^v?(?P<version>\d+(?:\.\d+)*)(?:[-+].*)?$", re.IGNORECASE)


class LatestReleaseError(RuntimeError):
    """Raised when the updater cannot resolve the latest release tag."""


@dataclass(frozen=True)
class InstallProvenance:
    source_url: str | None = None
    local_path: str | None = None
    editable: bool = False

    @property
    def is_local_checkout(self) -> bool:
        return self.editable or self.local_path is not None


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    latest_tag: str
    is_local_checkout: bool = False


@dataclass(frozen=True)
class UpdateTarget:
    package_spec: str
    python_spec: str
    runtime_constraints: str | None
    build_constraints: str | None


def package_spec_for_release_tag(tag: str) -> str:
    clean_tag = tag.strip()
    if not clean_tag:
        raise LatestReleaseError("latest release response did not include a tag name")
    archive_url = RELEASE_ARCHIVE_URL_TEMPLATE.format(tag=quote(clean_tag, safe=""))
    return f"{PACKAGE_NAME} @ {archive_url}"


def release_file_url_for_tag(tag: str, path: str) -> str:
    clean_tag = tag.strip()
    clean_path = path.strip().lstrip("/")
    if not clean_tag:
        raise LatestReleaseError("latest release response did not include a tag name")
    if not clean_path:
        raise LatestReleaseError("release file path is empty")
    return RELEASE_RAW_FILE_URL_TEMPLATE.format(
        tag=quote(clean_tag, safe=""),
        path=quote(clean_path, safe="/"),
    )


def fetch_latest_release_tag(
    *,
    api_url: str = LATEST_RELEASE_API_URL,
    timeout: float = _GITHUB_API_TIMEOUT,
) -> str:
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-cli-self-update",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LatestReleaseError(f"could not fetch latest GitHub release: {exc}") from exc

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        raise LatestReleaseError("latest GitHub release response did not include tag_name")
    return tag.strip()


def resolve_package_spec(
    env: Mapping[str, str] | None = None,
    *,
    latest_release_resolver: Callable[[], str] | None = None,
) -> str:
    source = os.environ if env is None else env
    if "A0_PACKAGE_SPEC" in source:
        return source["A0_PACKAGE_SPEC"]
    resolver = latest_release_resolver or fetch_latest_release_tag
    return package_spec_for_release_tag(resolver())


def resolve_python_spec(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    if "A0_PYTHON_SPEC" in source:
        return source["A0_PYTHON_SPEC"]
    return DEFAULT_PYTHON_SPEC


def resolve_update_target(
    env: Mapping[str, str] | None = None,
    *,
    latest_release_resolver: Callable[[], str] | None = None,
) -> UpdateTarget:
    source = os.environ if env is None else env
    python_spec = resolve_python_spec(source)
    package_spec = source.get("A0_PACKAGE_SPEC")
    release_tag: str | None = None

    if package_spec is None:
        resolver = latest_release_resolver or fetch_latest_release_tag
        release_tag = resolver()
        package_spec = package_spec_for_release_tag(release_tag)

    runtime_constraints = source.get("A0_RUNTIME_CONSTRAINTS")
    build_constraints = source.get("A0_BUILD_CONSTRAINTS")
    if runtime_constraints and build_constraints:
        return UpdateTarget(package_spec, python_spec, runtime_constraints, build_constraints)

    if release_tag is not None:
        return UpdateTarget(
            package_spec,
            python_spec,
            release_file_url_for_tag(release_tag, RUNTIME_CONSTRAINTS_PATH),
            release_file_url_for_tag(release_tag, BUILD_CONSTRAINTS_PATH),
        )

    if coerce_bool(source.get("A0_ALLOW_UNPINNED_UPDATE", "")):
        return UpdateTarget(package_spec, python_spec, None, None)

    raise LatestReleaseError(
        "A0_PACKAGE_SPEC requires A0_RUNTIME_CONSTRAINTS and A0_BUILD_CONSTRAINTS "
        "unless A0_ALLOW_UNPINNED_UPDATE=1 is set"
    )


def update_check_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    value = source.get("A0_UPDATE_CHECK", "").strip().lower()
    return not value or coerce_bool(value, default=True)


def normalized_release_version(value: str) -> str:
    text = value.strip()
    match = _VERSION_PATTERN.match(text)
    if match:
        return match.group("version")
    if text.lower().startswith("v"):
        return text[1:]
    return text


def is_newer_release_version(latest: str, current: str) -> bool:
    latest_key = _numeric_version_key(latest)
    current_key = _numeric_version_key(current)
    if latest_key is None or current_key is None:
        return False
    return latest_key > current_key


def check_for_update(
    current_version: str,
    env: Mapping[str, str] | None = None,
    *,
    latest_release_resolver: Callable[[], str] | None = None,
    provenance_resolver: Callable[[], InstallProvenance] | None = None,
) -> UpdateCheckResult | None:
    if not update_check_enabled(env):
        return None

    resolver = latest_release_resolver or fetch_latest_release_tag
    latest_tag = resolver()
    latest_version = normalized_release_version(latest_tag)
    if not is_newer_release_version(latest_version, current_version):
        return None

    resolve_provenance = provenance_resolver or detect_install_provenance
    provenance = resolve_provenance()
    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest_version,
        latest_tag=latest_tag,
        is_local_checkout=provenance.is_local_checkout,
    )


def format_update_available_message(result: UpdateCheckResult) -> str:
    if result.is_local_checkout:
        return (
            f"a0 CLI update available: {result.latest_version} "
            f"(current checkout reports {result.current_version}). "
            "Pull this checkout to update this runtime, or run `a0 update` for the standalone tool channel."
        )
    return (
        f"a0 CLI update available: {result.latest_version} "
        f"(installed {result.current_version}). Run `a0 update` after exiting to upgrade."
    )


def detect_install_provenance(distribution_name: str = "a0") -> InstallProvenance:
    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        return InstallProvenance()

    try:
        direct_url_text = dist.read_text("direct_url.json")
    except OSError:
        return InstallProvenance()

    if not direct_url_text:
        return InstallProvenance()

    try:
        payload = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return InstallProvenance()

    source_url = payload.get("url") if isinstance(payload.get("url"), str) else None
    dir_info = payload.get("dir_info")
    editable = isinstance(dir_info, dict) and bool(dir_info.get("editable"))
    return InstallProvenance(
        source_url=source_url,
        local_path=_file_url_to_path(source_url) if source_url else None,
        editable=editable,
    )


def run_self_update_handoff(
    *,
    env: Mapping[str, str] | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
) -> int:
    provenance = detect_install_provenance()
    if provenance.is_local_checkout:
        print(_format_local_checkout_notice(provenance))

    uv_executable = shutil.which("uv")
    if uv_executable is None:
        print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
        return 1

    try:
        target = resolve_update_target(env)
    except LatestReleaseError as exc:
        print(f"Failed to resolve a locked a0 update target: {exc}")
        print(
            "Set A0_PACKAGE_SPEC with A0_RUNTIME_CONSTRAINTS and "
            "A0_BUILD_CONSTRAINTS for a custom locked package source."
        )
        return 1

    script_path = _write_updater_script(temp_dir=temp_dir)
    argv = _build_updater_argv(
        uv_executable=uv_executable,
        script_path=script_path,
        parent_pid=os.getpid(),
        target=target,
    )
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as exc:
        _best_effort_remove(script_path)
        print(f"Failed to launch the updater handoff: {exc}")
        return 1

    print("Handing off update to a separate process. The updater will continue here after a0 exits.")
    return 0


def _build_updater_argv(
    *,
    uv_executable: str,
    script_path: Path,
    parent_pid: int,
    target: UpdateTarget,
) -> list[str]:
    script_args = [
        str(script_path),
        str(parent_pid),
        target.package_spec,
        target.python_spec,
        target.runtime_constraints or "",
        target.build_constraints or "",
    ]
    updater_python = _find_uv_managed_python(uv_executable, target.python_spec)
    if updater_python:
        return [updater_python, *script_args]

    return [
        uv_executable,
        "run",
        "--no-project",
        "--python",
        target.python_spec,
        "--managed-python",
        "--script",
        *script_args,
    ]


def _find_uv_managed_python(uv_executable: str, python_spec: str) -> str | None:
    try:
        result = subprocess.run(
            [
                uv_executable,
                "python",
                "find",
                python_spec,
                "--managed-python",
                "--no-project",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _build_updater_script() -> str:
    return dedent(
        """\
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import sys
        import tempfile
        import time
        from urllib.parse import urlparse
        from urllib.request import url2pathname, urlopen


        def _wait_for_parent_exit(parent_pid):
            if parent_pid <= 0:
                return
            if os.name == "nt":
                import ctypes

                wait_timeout = 258
                synchronize = 0x00100000
                kernel32 = ctypes.windll.kernel32
                kernel32.OpenProcess.restype = ctypes.c_void_p
                handle = kernel32.OpenProcess(synchronize, False, parent_pid)
                if not handle:
                    return
                try:
                    while True:
                        result = kernel32.WaitForSingleObject(handle, 100)
                        if result != wait_timeout:
                            return
                finally:
                    kernel32.CloseHandle(handle)
                return

            while True:
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    return
                except PermissionError:
                    return
                time.sleep(0.1)


        def _prepare_constraint(spec, name, temp_dir):
            if not spec:
                return None

            parsed = urlparse(spec)
            if parsed.scheme in {"http", "https"}:
                target = Path(temp_dir) / name
                try:
                    with urlopen(spec, timeout=30.0) as response:
                        target.write_bytes(response.read())
                except OSError as exc:
                    raise RuntimeError(f"Failed to download dependency lock {spec}: {exc}") from exc
                return str(target)

            if parsed.scheme == "file":
                path = Path(url2pathname(parsed.path))
            else:
                path = Path(spec)
            if not path.is_file():
                raise RuntimeError(f"Dependency lock file does not exist: {path}")
            return str(path)


        def _uv_tool_install_supports(uv_executable, option):
            try:
                result = subprocess.run(
                    [uv_executable, "tool", "install", "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                return False
            return option in f"{result.stdout}\\n{result.stderr}"


        def main(argv):
            if len(argv) != 5:
                print("Invalid updater invocation.", file=sys.stderr)
                return 2

            try:
                parent_pid = int(argv[0])
            except ValueError:
                print("Invalid parent PID.", file=sys.stderr)
                return 2

            package_spec = argv[1]
            python_spec = argv[2]
            runtime_constraints = argv[3]
            build_constraints = argv[4]
            _wait_for_parent_exit(parent_pid)

            uv_executable = shutil.which("uv")
            if uv_executable is None:
                print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
                return 1
            supports_build_constraints = _uv_tool_install_supports(
                uv_executable,
                "--build-constraints",
            )

            with tempfile.TemporaryDirectory(prefix="a0-update-locks-") as temp_dir:
                try:
                    runtime_lock = _prepare_constraint(
                        runtime_constraints,
                        "a0-runtime.txt",
                        temp_dir,
                    )
                    build_lock = _prepare_constraint(
                        build_constraints,
                        "a0-build.txt",
                        temp_dir,
                    )
                except RuntimeError as exc:
                    print(exc)
                    return 1

                command = [
                    uv_executable,
                    "tool",
                    "install",
                    "--force",
                    "--python",
                    python_spec,
                    "--managed-python",
                    "--upgrade-package",
                    "a0",
                ]
                if runtime_lock:
                    command.extend(["--constraints", runtime_lock])
                if build_lock:
                    if supports_build_constraints:
                        command.extend(["--build-constraints", build_lock])
                    else:
                        print(
                            "Warning: this uv version does not support "
                            "--build-constraints; continuing with runtime constraints."
                        )
                if not runtime_lock or not build_lock:
                    print("Warning: running a0 update without dependency locks.")
                command.append(package_spec)

                try:
                    result = subprocess.run(command, check=False)
                except OSError as exc:
                    print(f"Failed to run uv: {exc}")
                    return 1

            if result.returncode == 0:
                print("Update complete. Run a0.")
            return result.returncode


        if __name__ == "__main__":
            exit_code = 1
            try:
                exit_code = main(sys.argv[1:])
            finally:
                try:
                    Path(__file__).unlink()
                except OSError:
                    pass
            raise SystemExit(exit_code)
        """
    )


def _write_updater_script(temp_dir: str | os.PathLike[str] | None = None) -> Path:
    fd, script_path = tempfile.mkstemp(
        prefix="a0-update-",
        suffix=".py",
        text=True,
        dir=temp_dir,
    )
    path = Path(script_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_build_updater_script())
    return path


def _file_url_to_path(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None

    path = url2pathname(parsed.path)
    if parsed.netloc:
        return f"//{parsed.netloc}{path}"
    return path


def _numeric_version_key(value: str) -> tuple[int, ...] | None:
    version = normalized_release_version(value)
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    numbers = tuple(int(part) for part in parts)
    return numbers + (0,) * max(0, 3 - len(numbers))


def _format_local_checkout_notice(provenance: InstallProvenance) -> str:
    location = provenance.local_path or "this checkout"
    return (
        f"Notice: current a0 runtime comes from a local or editable checkout at {location}. "
        "`a0 update` updates the standalone uv-managed tool channel and will not modify this checkout."
    )


def _best_effort_remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
