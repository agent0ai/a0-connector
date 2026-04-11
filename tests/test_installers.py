from __future__ import annotations

from pathlib import Path


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
    assert 'git+https://github.com/agent0ai/a0-connector' in installer
    assert 'uv tool install --upgrade "$PACKAGE_SPEC"' in installer


def test_windows_installer_lets_uv_manage_python() -> None:
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "Resolve-PythonCommand" not in installer
    assert "Test-PythonCommand" not in installer
    assert "--no-python-downloads" not in installer
    assert '"--python"' not in installer
    assert 'git+https://github.com/agent0ai/a0-connector' in installer
    assert '$installArgs = @("tool", "install", "--upgrade", $PackageSpec)' in installer


def test_readme_documents_uv_managed_python_and_git_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compact = " ".join(readme.split())
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh" in compact
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.ps1" in compact
    assert "directly from GitHub" in compact
    assert "uv tool install git+https://github.com/agent0ai/a0-connector" in compact
    assert "will pick a compatible Python" in compact
    assert "download one if needed" in compact
