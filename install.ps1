$ErrorActionPreference = "Stop"

$LatestReleaseApiUrl = if ($env:A0_LATEST_RELEASE_API_URL) { $env:A0_LATEST_RELEASE_API_URL } else { "https://api.github.com/repos/agent0ai/a0-connector/releases/latest" }
$PythonSpec = if ($env:A0_PYTHON_SPEC) { $env:A0_PYTHON_SPEC } else { "3.12" }
$UvInstallUrl = if ($env:UV_INSTALL_URL) { $env:UV_INSTALL_URL } else { "https://astral.sh/uv/install.ps1" }
$RuntimeConstraintsPath = "constraints/a0-runtime.txt"
$BuildConstraintsPath = "constraints/a0-build.txt"
$ReleaseRawFileUrlBase = "https://raw.githubusercontent.com/agent0ai/a0-connector/refs/tags"

function Resolve-PackageSpec {
    if ($env:A0_PACKAGE_SPEC) {
        return @{
            PackageSpec = $env:A0_PACKAGE_SPEC
            ReleaseTag = $null
        }
    }

    try {
        $headers = @{
            Accept = "application/vnd.github+json"
            "User-Agent" = "a0-cli-installer"
        }
        $release = Invoke-RestMethod -Uri $LatestReleaseApiUrl -Headers $headers
    } catch {
        throw "Could not resolve the latest a0 release from GitHub. Set A0_PACKAGE_SPEC to install from a specific package source. $($_.Exception.Message)"
    }

    $tag = [string]$release.tag_name
    if (-not $tag.Trim()) {
        throw "GitHub latest-release response did not include tag_name. Set A0_PACKAGE_SPEC to install from a specific package source."
    }

    $escapedTag = [uri]::EscapeDataString($tag.Trim())
    return @{
        PackageSpec = "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/$escapedTag.zip"
        ReleaseTag = $tag.Trim()
    }
}

function Get-ReleaseFileUrl([string]$Tag, [string]$Path) {
    $escapedTag = [uri]::EscapeDataString($Tag.Trim())
    return "$ReleaseRawFileUrlBase/$escapedTag/$Path"
}

function Test-Enabled([string]$Value) {
    return @("1", "true", "yes", "on", "enabled") -contains $Value.Trim().ToLowerInvariant()
}

function Resolve-ConstraintSpecs($Target) {
    if ($env:A0_RUNTIME_CONSTRAINTS -and $env:A0_BUILD_CONSTRAINTS) {
        return @{
            Runtime = $env:A0_RUNTIME_CONSTRAINTS
            Build = $env:A0_BUILD_CONSTRAINTS
        }
    }

    if ($Target.ReleaseTag) {
        return @{
            Runtime = Get-ReleaseFileUrl $Target.ReleaseTag $RuntimeConstraintsPath
            Build = Get-ReleaseFileUrl $Target.ReleaseTag $BuildConstraintsPath
        }
    }

    if (Test-Enabled "$env:A0_ALLOW_UNPINNED_UPDATE") {
        return @{
            Runtime = $null
            Build = $null
        }
    }

    throw "A0_PACKAGE_SPEC requires A0_RUNTIME_CONSTRAINTS and A0_BUILD_CONSTRAINTS. Set A0_ALLOW_UNPINNED_UPDATE=1 only for intentional development installs."
}

function Resolve-ConstraintFile([string]$Spec, [string]$Name, [string]$TempDir) {
    if (-not $Spec) {
        return $null
    }

    if ($Spec.StartsWith("http://") -or $Spec.StartsWith("https://")) {
        $target = Join-Path $TempDir $Name
        Invoke-WebRequest -Uri $Spec -OutFile $target
        return $target
    }

    if ($Spec.StartsWith("file://")) {
        $path = ([uri]$Spec).LocalPath
    } else {
        $path = $Spec
    }

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Dependency lock file does not exist: $path"
    }
    return $path
}

function Add-LocalUvToPath {
    $localBin = Join-Path $HOME ".local\bin"
    if (Test-Path $localBin) {
        $env:PATH = "$localBin;$env:PATH"
    }
}

function Install-Uv {
    $installerDir = Join-Path ([IO.Path]::GetTempPath()) ("a0-uv-install-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $installerDir | Out-Null
    try {
        $installerPath = Join-Path $installerDir "install.ps1"
        Invoke-WebRequest -UseBasicParsing -Uri $UvInstallUrl -OutFile $installerPath
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File $installerPath
        if ($LASTEXITCODE -ne 0) {
            throw "uv installer exited with code $LASTEXITCODE."
        }
    } finally {
        Remove-Item -LiteralPath $installerDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Add-LocalUvToPath

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv was installed but is not on PATH in this shell yet. Open a new terminal, then rerun this installer."
    }
}

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        return
    }

    Install-Uv
}

function Test-UvToolInstallOption([string]$Option) {
    try {
        $helpText = (& uv tool install --help 2>&1) -join "`n"
    } catch {
        return $false
    }

    return $helpText.Contains($Option)
}

function Ensure-UvToolInstallBuildConstraints {
    if (Test-UvToolInstallOption "--build-constraints") {
        return $true
    }

    Write-Host "Updating uv to support locked build dependencies..."
    try {
        Install-Uv
    } catch {
        Write-Warning "Could not update uv automatically: $($_.Exception.Message)"
    }

    return (Test-UvToolInstallOption "--build-constraints")
}

function Test-PathPrefix([string]$PathValue, [string]$Prefix) {
    if ([string]::IsNullOrWhiteSpace($PathValue) -or [string]::IsNullOrWhiteSpace($Prefix)) {
        return $false
    }
    return $PathValue.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoRunningA0ToolProcesses([string]$ToolDir) {
    if ([string]::IsNullOrWhiteSpace($ToolDir) -or -not (Test-Path -LiteralPath $ToolDir)) {
        return
    }

    $toolRoot = [IO.Path]::GetFullPath($ToolDir).TrimEnd('\')
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        if ($_.ProcessId -eq $PID) {
            return $false
        }
        $exe = [string]$_.ExecutablePath
        $cmd = [string]$_.CommandLine
        (Test-PathPrefix $exe $toolRoot) -or
            ($cmd.IndexOf($toolRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
    } | Select-Object ProcessId, Name, ExecutablePath, CommandLine)

    if ($running.Count -eq 0) {
        return
    }

    $summary = ($running | ForEach-Object {
        "$($_.Name) pid=$($_.ProcessId)"
    }) -join ", "
    throw "A0 CLI is still running from $toolRoot ($summary). Close all A0 CLI terminal windows, then rerun this installer."
}

Ensure-Uv
$supportsBuildConstraints = Ensure-UvToolInstallBuildConstraints
$Target = Resolve-PackageSpec

$toolBin = (& uv tool dir --bin).Trim()
if ($toolBin) {
    $env:PATH = "$toolBin;$env:PATH"
}

try {
    uv tool update-shell | Out-Null
} catch {
}

$toolDir = Join-Path ((& uv tool dir).Trim()) "a0"
Assert-NoRunningA0ToolProcesses $toolDir

$lockTempDir = Join-Path ([IO.Path]::GetTempPath()) ("a0-install-locks-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $lockTempDir | Out-Null
try {
    $constraintSpecs = Resolve-ConstraintSpecs $Target
    $runtimeConstraints = Resolve-ConstraintFile $constraintSpecs.Runtime "a0-runtime.txt" $lockTempDir
    $buildConstraints = Resolve-ConstraintFile $constraintSpecs.Build "a0-build.txt" $lockTempDir

    $installArgs = @("tool", "install", "--force", "--python", $PythonSpec, "--managed-python", "--upgrade-package", "a0")
    if ($runtimeConstraints) {
        $installArgs += @("--constraints", $runtimeConstraints)
    }
    if ($buildConstraints) {
        if ($supportsBuildConstraints) {
            $installArgs += @("--build-constraints", $buildConstraints)
        } else {
            Write-Warning "This uv version does not support --build-constraints; continuing with runtime constraints. Run 'uv self update' to apply build constraints on future installs."
        }
    }
    if (-not $runtimeConstraints -or -not $buildConstraints) {
        Write-Warning "Installing a0 without dependency locks."
    }
    $installArgs += $Target.PackageSpec

    & uv @installArgs
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed for package spec: $($Target.PackageSpec)"
    }
} finally {
    Remove-Item -LiteralPath $lockTempDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "a0 is installed."
Write-Host ""
Write-Host "Run:"
Write-Host "  a0"
Write-Host ""
Write-Host "Managed Python:"
Write-Host "  $PythonSpec"
Write-Host ""
if ($toolBin) {
    Write-Host "If 'a0' is not available in your current shell yet, open a new terminal."
    Write-Host "uv installs tool executables in:"
    Write-Host "  $toolBin"
}
