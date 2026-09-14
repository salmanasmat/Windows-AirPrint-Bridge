# build.ps1 — Build AirPrint Bridge executable for Windows service deployment
Write-Host "Building AirPrint Bridge with PyInstaller..." -ForegroundColor Green

# Verify PyInstaller is installed
if (-not (Get-Command "pyinstaller" -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

# NOTE: --console is required for Windows service compatibility.
# Using --windowed causes a PID mismatch with the Service Control Manager (SCM Error 7039).
python -m PyInstaller --onefile `
    --console `
    --hidden-import win32timezone `
    --hidden-import win32gui `
    --hidden-import win32ui `
    --hidden-import win32con `
    --hidden-import pythoncom `
    --hidden-import pywintypes `
    --hidden-import fitz `
    --hidden-import PIL `
    --hidden-import PIL.Image `
    --hidden-import PIL.ImageWin `
    --name "AirPrintBridge" `
    .\airprint_bridge.py

if ($LASTEXITCODE -eq 0) {
    Write-Host "Build successful! Output: dist\AirPrintBridge.exe" -ForegroundColor Green
} else {
    Write-Host "Build FAILED!" -ForegroundColor Red
    exit 1
}
