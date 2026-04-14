from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_package_keeps_python_floor_at_310() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert '{ name = "agent0ai" }' in pyproject


def test_unix_installer_lets_uv_manage_python() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "ensure_python" not in installer
    assert "python_ok" not in installer
    assert "--no-python-downloads" not in installer
    assert '--python "$PYTHON_CMD"' not in installer
    assert 'a0 @ https://github.com/agent0ai/a0-connector/archive/refs/heads/main.zip' in installer
    assert 'uv tool install --upgrade "$PACKAGE_SPEC"' in installer


def test_unix_installer_is_sh_compatible() -> None:
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "install.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_windows_installer_lets_uv_manage_python() -> None:
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "Resolve-PythonCommand" not in installer
    assert "Test-PythonCommand" not in installer
    assert "--no-python-downloads" not in installer
    assert '"--python"' not in installer
    assert 'a0 @ https://github.com/agent0ai/a0-connector/archive/refs/heads/main.zip' in installer
    assert '$installArgs = @("tool", "install", "--upgrade", $PackageSpec)' in installer
    assert 'if ($LASTEXITCODE -ne 0)' in installer


def test_readme_documents_uv_managed_python_and_git_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compact = " ".join(readme.split())
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh" in compact
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.ps1" in compact
    assert "directly from a GitHub source archive" in compact
    assert 'uv tool install --upgrade "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/heads/main.zip"' in compact
    assert "will pick a compatible Python" in compact
    assert "download one if needed" in compact
    assert "without requiring `git` to be installed" in readme
    assert "`a0 update`" in readme
    assert "`A0_PACKAGE_SPEC`" in readme
    assert "Install `uv` or rerun the existing installer." in readme
