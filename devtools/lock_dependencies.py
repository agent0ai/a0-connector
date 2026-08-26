from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IN = ROOT / "requirements" / "a0-runtime.in"
BUILD_IN = ROOT / "requirements" / "a0-build.in"
RUNTIME_LOCK = ROOT / "constraints" / "a0-runtime.txt"
BUILD_LOCK = ROOT / "constraints" / "a0-build.txt"
PYPROJECT = ROOT / "pyproject.toml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate A0 release dependency locks and sync pyproject pins."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if generated lock metadata would change.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="a0-lock-check-") if args.check else _null_tempdir() as temp_dir:
        runtime_lock = Path(temp_dir) / RUNTIME_LOCK.name if args.check else RUNTIME_LOCK
        build_lock = Path(temp_dir) / BUILD_LOCK.name if args.check else BUILD_LOCK
        runtime_text = _compile_requirements(RUNTIME_IN, runtime_lock)
        build_text = _compile_requirements(BUILD_IN, build_lock)
        pyproject_text = PYPROJECT.read_text(encoding="utf-8")
        updated = _sync_pyproject(
            pyproject_text,
            runtime_requirements=_parse_pinned_requirements(runtime_text),
            build_requirements=_parse_pinned_requirements(build_text),
        )

        changed = updated != pyproject_text
        if args.check:
            changed = (
                changed
                or runtime_text != RUNTIME_LOCK.read_text(encoding="utf-8")
                or build_text != BUILD_LOCK.read_text(encoding="utf-8")
            )
            return 1 if changed else 0

        if changed:
            PYPROJECT.write_text(updated, encoding="utf-8", newline="\n")
    return 0


def _compile_requirements(source: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            str(source.relative_to(ROOT)),
            "--universal",
            "--python-version",
            "3.10",
            "--generate-hashes",
            "--upgrade",
            "--custom-compile-command",
            "python devtools/lock_dependencies.py",
            "-o",
            _path_arg(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return output.read_text(encoding="utf-8")


class _null_tempdir:
    def __enter__(self) -> str:
        return ""

    def __exit__(self, *args: object) -> None:
        del args


def _path_arg(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_pinned_requirements(lock_text: str) -> list[str]:
    requirements: list[str] = []
    for line in lock_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1].isspace():
            continue
        requirement = stripped.removesuffix("\\").strip()
        requirement = re.sub(r"\s+--hash=sha256:[0-9a-f]+.*$", "", requirement).strip()
        if requirement:
            requirements.append(_normalize_marker_quotes(requirement))
    return requirements


def _normalize_marker_quotes(requirement: str) -> str:
    if ";" not in requirement:
        return requirement
    name, marker = requirement.split(";", 1)
    return f"{name.strip()} ; {marker.strip().replace(chr(34), chr(39))}"


def _sync_pyproject(
    text: str,
    *,
    runtime_requirements: list[str],
    build_requirements: list[str],
) -> str:
    lines = text.splitlines()
    lines = _replace_list(lines, "build-system", "requires", build_requirements)
    lines = _replace_list(lines, "project", "dependencies", runtime_requirements)
    return "\n".join(lines) + "\n"


def _replace_list(
    lines: list[str],
    section: str,
    key: str,
    values: list[str],
) -> list[str]:
    current_section = ""
    output: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped.strip("[]")
        if current_section == section and stripped.startswith(f"{key} = ["):
            indent = line[: len(line) - len(line.lstrip())]
            output.append(f"{indent}{key} = [")
            output.extend(f'    "{_toml_escape(value)}",' for value in values)
            if stripped.endswith("]"):
                output.append(f"{indent}]")
                replaced = True
                i += 1
                continue
            i += 1
            while i < len(lines) and lines[i].strip() != "]":
                i += 1
            if i >= len(lines):
                raise SystemExit(f"Unterminated {key} list in [{section}]")
            output.append(lines[i])
            replaced = True
            i += 1
            continue
        output.append(line)
        i += 1
    if not replaced:
        raise SystemExit(f"Could not find {key} list in [{section}]")
    return output


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    raise SystemExit(main())
