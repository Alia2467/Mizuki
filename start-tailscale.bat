@echo off
setlocal
REM ============================================================
REM  Tailscale - FULL RESET (clear corrupted state & re-login)
REM  Fixes "no backend" when a simple restart doesn't work.
REM  Double-click to run. Will re-launch as Administrator if needed.
REM ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "TS=C:\Program Files\Tailscale\tailscale.exe"

echo [1/6] Stopping service & killing processes...
sc stop Tailscale >nul 2>&1
taskkill /f /im tailscaled.exe >nul 2>&1
taskkill /f /im tailscale.exe >nul 2>&1
taskkill /f /im tailscale-ipn.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo [2/6] Clearing Tailscale state...
if exist "C:\ProgramData\Tailscale" rd /s /q "C:\ProgramData\Tailscale" 2>nul
if exist "%LocalAppData%\Tailscale" rd /s /q "%LocalAppData%\Tailscale" 2>nul
if exist "C:\Windows\System32\config\systemprofile\AppData\Local\Tailscale" rd /s /q "C:\Windows\System32\config\systemprofile\AppData\Local\Tailscale" 2>nul

echo [3/6] Starting service...
sc start Tailscale >nul 2>&1
timeout /t 6 /nobreak >nul

echo [4/6] Connecting to tailnet (login required)...
"%TS%" up

echo.
echo [5/6] Current status:
"%TS%" status

echo.
echo ============================================================
echo If a login URL appeared above, open it in your browser to
echo sign in with your Tailscale account.
echo After login, note the new 100.x.x.x IP and update it in the
echo phone app if it changed.
echo ============================================================
echo.
pause
