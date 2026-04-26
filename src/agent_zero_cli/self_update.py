from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent
from typing import Mapping
from urllib.parse import urlparse
from urllib.request import url2pathname


DEFAULT_PACKAGE_SPEC = "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/v1.5.zip"
DEFAULT_PYTHON_SPEC = "3.11"


@dataclass(frozen=True)
class InstallProvenance:
    source_url: str | None = None
    local_path: str | None = None
    editable: bool = False

    @property
    def is_local_checkout(self) -> bool:
        return self.editable or self.local_path is not None


def resolve_package_spec(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    if "A0_PACKAGE_SPEC" in source:
        return source["A0_PACKAGE_SPEC"]
    return DEFAULT_PACKAGE_SPEC


def resolve_python_spec(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    if "A0_PYTHON_SPEC" in source:
        return source["A0_PYTHON_SPEC"]
    return DEFAULT_PYTHON_SPEC


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
    package_spec = resolve_package_spec(env)
    python_spec = resolve_python_spec(env)
    provenance = detect_install_provenance()
    if provenance.is_local_checkout:
        print(_format_local_checkout_notice(provenance))

    uv_executable = shutil.which("uv")
    if uv_executable is None:
        print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
        return 1

    script_path = _write_updater_script(temp_dir=temp_dir)
    argv = [sys.executable, str(script_path), str(os.getpid()), package_spec, python_spec]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as exc:
        _best_effort_remove(script_path)
        print(f"Failed to launch the updater handoff: {exc}")
        return 1

    print("Handing off update to a separate process. The updater will continue here after a0 exits.")
    return 0


def _build_updater_script() -> str:
    return dedent(
        """\
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import sys
        import time


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


        def main(argv):
            if len(argv) != 3:
                print("Invalid updater invocation.", file=sys.stderr)
                return 2

            try:
                parent_pid = int(argv[0])
            except ValueError:
                print("Invalid parent PID.", file=sys.stderr)
                return 2

            package_spec = argv[1]
            python_spec = argv[2]
            _wait_for_parent_exit(parent_pid)

            uv_executable = shutil.which("uv")
            if uv_executable is None:
                print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
                return 1

            try:
                result = subprocess.run(
                    [
                        uv_executable,
                        "tool",
                        "install",
                        "--python",
                        python_spec,
                        "--managed-python",
                        "--upgrade",
                        package_spec,
                    ],
                    check=False,
                )
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
