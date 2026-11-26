# Build a standalone Windows x64 executable (no console window, no Python required).
# Usage: pwsh -File scripts/build_windows.ps1 [-PythonExe .\.venv\Scripts\python.exe]

param(
    [string]$PythonExe = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$Version = & $PythonExe -c "import pathlib, re; t = pathlib.Path('chocolate_egg_cream/__init__.py').read_text(); print(re.search(r'__version__\s*=\s*[\x22\x27]([^\x22\x27]+)', t).group(1))"
$Version = $Version.Trim()
Write-Host "Building Chocolate Egg Cream $Version for Windows x64..."

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "build", "dist"

# Optional build-time injection of dedicated Chocolate Egg Cream Last.fm app credentials.
# Never committed to git; removed immediately after PyInstaller finishes.
$BundledCredsFile = Join-Path $RepoRoot "chocolate_egg_cream\_bundled_lastfm.py"
$CreatedCreds = $false
if ($env:CEC_LASTFM_API_KEY -and $env:CEC_LASTFM_API_SECRET) {
    $k = $env:CEC_LASTFM_API_KEY.Trim().Replace('\', '\\').Replace('"', '\"')
    $s = $env:CEC_LASTFM_API_SECRET.Trim().Replace('\', '\\').Replace('"', '\"')
    "API_KEY = `"$k`"`nAPI_SECRET = `"$s`"`n" | Set-Content -Encoding UTF8 $BundledCredsFile
    $CreatedCreds = $true
    Write-Host "Injected build-time Last.fm app credentials (gitignored)."
}

try {
    & $PythonExe -m PyInstaller --clean --noconfirm "packaging/chocolate_egg_cream.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($CreatedCreds -and (Test-Path $BundledCredsFile)) {
        Remove-Item -Force $BundledCredsFile
    }
}

$BuiltExe = Join-Path $RepoRoot "dist\Chocolate Egg Cream.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "Expected output executable not found: $BuiltExe"
}

# Artifact gate: run the packaged executable's own diagnostics. This proves
# the shipped EXE (not the source tree) has its CA bundle, recognition stack,
# audio capture helper and does a real TLS handshake. Windowed build: the
# result goes to a JSON report because stdout does not exist there.
$VerifyReport = Join-Path $RepoRoot "dist\verify-report.json"
$verify = Start-Process -FilePath $BuiltExe -ArgumentList @(
    "verify", "--report", "`"$VerifyReport`""
) -Wait -PassThru
if ($verify.ExitCode -ne 0) {
    if (Test-Path $VerifyReport) { Get-Content $VerifyReport | Write-Host }
    throw "Packaged verify failed with exit code $($verify.ExitCode)"
}
if (-not (Test-Path $VerifyReport)) {
    throw "Packaged verify wrote no report: $VerifyReport"
}
$report = Get-Content $VerifyReport -Raw | ConvertFrom-Json
if (-not $report.ok) {
    Get-Content $VerifyReport | Write-Host
    throw "Packaged verify reported failures"
}
if ($report.version -ne $Version) {
    throw "Packaged verify reports version $($report.version), expected $Version"
}
Write-Host "Packaged verify: OK ($($report.checks.Count) checks, version $($report.version))"

$ReleaseDir = Join-Path $RepoRoot "dist\release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$ArtifactName = "ChocolateEggCream-$Version-windows-x64.exe"
$ArtifactPath = Join-Path $ReleaseDir $ArtifactName
Copy-Item -Force $BuiltExe $ArtifactPath

$Hash = (Get-FileHash -Algorithm SHA256 $ArtifactPath).Hash.ToLower()
"$Hash  $ArtifactName" | Set-Content -Encoding ASCII (Join-Path $ReleaseDir "$ArtifactName.sha256")

$SizeMB = [Math]::Round((Get-Item $ArtifactPath).Length / 1MB, 2)
Write-Host "Created: $ArtifactPath ($SizeMB MB)"
Write-Host "SHA-256: $Hash"
