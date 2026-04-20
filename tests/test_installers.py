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
    assert 'PACKAGE_SPEC="${A0_PACKAGE_SPEC:-a0}"' in installer
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
    assert '"a0"' in installer
    assert '$installArgs = @("tool", "install", "--upgrade", $PackageSpec)' in installer
    assert 'if ($LASTEXITCODE -ne 0)' in installer


def test_root_package_declares_platform_backend_dependencies() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'a0-computer-use-wayland>=1.5; platform_system == "Linux"' in pyproject
    assert 'a0-computer-use-windows>=1.5; platform_system == "Windows"' in pyproject


def test_development_docs_show_workspace_backend_editable_installs() -> None:
    development = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    compact = " ".join(development.split())
    assert "pip install -e .\\packages\\a0-computer-use-windows -e ." in development
    assert "pip install -e ./packages/a0-computer-use-wayland -e ." in development
    assert "Repo-local editable installs need the matching backend package" in compact


def test_backend_package_scaffolding_reserves_release_names() -> None:
    package_names = {
        "a0-computer-use-wayland": "a0_computer_use_wayland",
        "a0-computer-use-windows": "a0_computer_use_windows",
        "a0-computer-use-x11": "a0_computer_use_x11",
        "a0-computer-use-macos": "a0_computer_use_macos",
    }

    for dist_name, module_name in package_names.items():
        pyproject = (ROOT / "packages" / dist_name / "pyproject.toml").read_text(encoding="utf-8")
        assert f'name = "{dist_name}"' in pyproject
        assert f'packages = ["src/{module_name}"]' in pyproject
        assert (ROOT / "packages" / dist_name / "src" / module_name / "__init__.py").exists()


def test_readme_documents_uv_managed_python_and_git_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compact = " ".join(readme.split())
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh" in compact
    assert "raw.githubusercontent.com/agent0ai/a0-connector/main/install.ps1" in compact
    assert "install the stable `a0` release directly" in compact
    assert "a0-computer-use-wayland" in compact
    assert "a0-computer-use-windows" in compact
    assert "will pick a compatible Python" in compact
    assert "download one if needed" in compact
    assert "without requiring `git` to be installed" in readme
    assert "`a0 update`" in readme
    assert "`A0_PACKAGE_SPEC`" in readme
    assert "Install `uv` or rerun the existing installer." in readme
