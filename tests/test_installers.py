from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_package_keeps_python_floor_at_310() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert '{ name = "agent0ai" }' in pyproject


def test_unix_installer_pins_managed_python() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--no-python-downloads" not in installer
    assert "LATEST_RELEASE_API_URL=" in installer
    assert "releases/latest" in installer
    assert "A0_PACKAGE_SPEC" in installer
    assert "A0_RUNTIME_CONSTRAINTS" in installer
    assert "A0_BUILD_CONSTRAINTS" in installer
    assert "A0_ALLOW_UNPINNED_UPDATE" in installer
    assert "fetch_latest_release_tag" in installer
    assert "constraints/a0-runtime.txt" in installer
    assert "constraints/a0-build.txt" in installer
    assert 'archive/refs/tags/$RELEASE_TAG.zip' in installer
    assert 'PYTHON_SPEC="${A0_PYTHON_SPEC:-3.12}"' in installer
    assert "--force" in installer
    assert '--upgrade-package a0' in installer
    assert "wl-clipboard" in installer
    assert "xclip" in installer
    assert '--constraints "$RUNTIME_CONSTRAINTS"' in installer
    assert '--build-constraints "$BUILD_CONSTRAINTS"' in installer
    assert '--upgrade "$PACKAGE_SPEC"' not in installer


def test_unix_installer_is_sh_compatible() -> None:
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "install.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_windows_installer_pins_managed_python() -> None:
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "--no-python-downloads" not in installer
    assert "$LatestReleaseApiUrl" in installer
    assert "releases/latest" in installer
    assert "A0_PACKAGE_SPEC" in installer
    assert "A0_RUNTIME_CONSTRAINTS" in installer
    assert "A0_BUILD_CONSTRAINTS" in installer
    assert "A0_ALLOW_UNPINNED_UPDATE" in installer
    assert "Resolve-PackageSpec" in installer
    assert "EscapeDataString" in installer
    assert "constraints/a0-runtime.txt" in installer
    assert "constraints/a0-build.txt" in installer
    assert '$PythonSpec = if ($env:A0_PYTHON_SPEC) { $env:A0_PYTHON_SPEC } else { "3.12" }' in installer
    assert '$installArgs = @("tool", "install", "--force", "--python", $PythonSpec, "--managed-python", "--upgrade-package", "a0")' in installer
    assert '"--constraints", $runtimeConstraints' in installer
    assert '"--build-constraints", $buildConstraints' in installer
    assert "Ensure-UvToolInstallBuildConstraints" in installer
    assert "Test-UvToolInstallOption" in installer
    assert "Assert-NoRunningA0ToolProcesses" in installer
    assert "Close all A0 CLI terminal windows" in installer
    assert "does not support --build-constraints" in installer
    assert 'if ($LASTEXITCODE -ne 0)' in installer


def test_release_dependency_locks_are_checked_in() -> None:
    runtime_lock = (ROOT / "constraints" / "a0-runtime.txt").read_text(encoding="utf-8")
    build_lock = (ROOT / "constraints" / "a0-build.txt").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "python devtools/lock_dependencies.py" in runtime_lock
    assert "python devtools/lock_dependencies.py" in build_lock
    assert "--hash=sha256" in runtime_lock
    assert "--hash=sha256" in build_lock
    assert "textual==" in runtime_lock
    assert "playwright==" in runtime_lock
    assert "hatchling==" in build_lock

    assert all("==" in requirement for requirement in pyproject["build-system"]["requires"])
    assert all("==" in requirement for requirement in pyproject["project"]["dependencies"])
    assert not any(
        ">=" in requirement.partition(";")[0]
        for requirement in pyproject["project"]["dependencies"]
    )


def test_root_package_embeds_platform_backends() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)

    dependencies = pyproject["project"]["dependencies"]
    dependency_text = "\n".join(dependencies)
    assert "a0-computer-use-wayland" not in dependency_text
    assert "a0-computer-use-x11" not in dependency_text
    assert "a0-computer-use-macos" not in dependency_text
    assert "a0-computer-use-windows" not in dependency_text

    assert "mss==10.2.0 ; sys_platform == 'linux'" not in dependencies
    assert "python-xlib==0.33 ; sys_platform == 'linux'" not in dependencies
    assert "pyobjc-framework-applicationservices==12.2.2 ; sys_platform == 'darwin'" in dependencies
    assert "pyobjc-framework-quartz==12.2.2 ; sys_platform == 'darwin'" in dependencies
    assert "dxcam==0.3.0 ; sys_platform == 'win32'" in dependencies
    assert "pillow==12.3.0" in dependencies
    assert "pywinauto==0.6.9 ; sys_platform == 'win32'" in dependencies
    assert "textual-serve==1.1.3" in dependencies
    assert any(requirement.startswith("playwright==") for requirement in dependencies)

    entry_points = pyproject["project"]["entry-points"]["a0.computer_use_backends"]
    assert entry_points == {
        "wayland": "a0_computer_use_wayland.backend:WAYLAND_BACKEND_SPEC",
        "macos": "a0_computer_use_macos.backend:MACOS_BACKEND_SPEC",
        "windows": "a0_computer_use_windows.backend:WINDOWS_BACKEND_SPEC",
    }

    wheel_packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "src/agent_zero_cli" in wheel_packages
    assert "packages/a0-computer-use-wayland/src/a0_computer_use_wayland" in wheel_packages
    assert "packages/a0-computer-use-x11/src/a0_computer_use_x11" not in wheel_packages
    assert "packages/a0-computer-use-macos/src/a0_computer_use_macos" in wheel_packages
    assert "packages/a0-computer-use-windows/src/a0_computer_use_windows" in wheel_packages


def test_development_docs_show_workspace_backend_editable_installs() -> None:
    development = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    compact = " ".join(development.split())
    assert "pip install -e ." in development
    assert "The root editable install includes the embedded computer-use backends" in compact
    assert "isolated backend package development" in compact


def test_backend_packages_keep_release_names_and_modules() -> None:
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
    assert "resolve the latest published GitHub release at runtime" in compact
    assert "refs/tags/v1.6.zip" not in compact
    assert "Computer-use backends are embedded in the `a0` wheel" in compact
    assert "managed CPython 3.12 tool environment" in compact
    assert "download the managed Python automatically" in compact
    assert "without requiring `git` to be installed" in readme
    assert "`a0 update`" in readme
    assert "resolves the latest published GitHub release at runtime" in compact
    assert "`A0_PACKAGE_SPEC`" in readme
    assert "`A0_PYTHON_SPEC`" in readme
    assert "`A0_RUNTIME_CONSTRAINTS`" in readme
    assert "`A0_BUILD_CONSTRAINTS`" in readme
    assert "`A0_ALLOW_UNPINNED_UPDATE=1`" in readme
    assert "dependencies pinned to the tested release set" in compact
    assert "Install `uv` or rerun the existing installer." in readme
    assert "Close any still-open A0 CLI terminal windows" in readme
