# ============================================================
# Build the BlogCast Docker image and export it as blogcast.tar
#
# Required files:
#   Dockerfile
#   entrypoint.sh
#   requirements.txt
#   blogcast.py
#
# Output:
#   blogcast.tar
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "[1/5] Checking Docker..." -ForegroundColor Cyan
docker version | Out-Null

if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running."
}

if (Test-Path ".\blogcast.tar") {
    Write-Host "Removing previous blogcast.tar..." -ForegroundColor DarkGray
    Remove-Item ".\blogcast.tar" -Force
}

Write-Host "[2/5] Building image blogcast:latest..." -ForegroundColor Cyan

docker build `
    --platform linux/amd64 `
    --pull `
    -t blogcast:latest `
    .

if ($LASTEXITCODE -ne 0) {
    throw "Docker image build failed."
}

Write-Host "[3/5] Verifying runtime dependencies..." -ForegroundColor Cyan

docker run --rm `
    --platform linux/amd64 `
    --entrypoint python `
    blogcast:latest `
    -c "import babel, pydub, edge_tts, requests, bs4, mutagen, langdetect, pypdf; from pydub.utils import which; assert which('ffmpeg'), 'ffmpeg binary not found in PATH'; print('Dependencies and ffmpeg OK')"

if ($LASTEXITCODE -ne 0) {
    throw "Dependency verification failed."
}

Write-Host "[4/5] Exporting blogcast.tar..." -ForegroundColor Cyan

docker save -o blogcast.tar blogcast:latest

if ($LASTEXITCODE -ne 0) {
    throw "Image export failed."
}

Write-Host "[5/5] Complete!" -ForegroundColor Green

$tar = Get-Item ".\blogcast.tar"
$mb = [math]::Round($tar.Length / 1MB, 1)

Write-Host ""
Write-Host "Created: $($tar.FullName)" -ForegroundColor Green
Write-Host "Size:    $mb MB" -ForegroundColor Green
Write-Host ""

Write-Host "You can now import blogcast.tar into Docker, Synology Container Manager," -ForegroundColor Yellow
Write-Host "or another compatible container platform." -ForegroundColor Yellow
