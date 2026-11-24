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

& $PythonExe -m PyInstaller --clean --noconfirm "packaging/chocolate_egg_cream.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$BuiltExe = Join-Path $RepoRoot "dist\Chocolate Egg Cream.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "Expected output executable not found: $BuiltExe"
}

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
