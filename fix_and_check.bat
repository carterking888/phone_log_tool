@echo off
REM ASCII only. Run this once after unzipping adb_tool on a new PC.
REM 1) Remove Mark-of-the-Web (blocked .NET DLLs cause
REM    "Failed to resolve Python.Runtime.Loader.Initialize")
REM 2) .NET Framework >= 4.7.2 (Release >= 461808) - required by pythonnet 3.x
REM 3) WebView2 Runtime - required by the app window (pywebview/EdgeChromium).
REM    Windows 11 ships it; Windows 10 often does NOT. Missing WebView2 makes
REM    the exe start and then show nothing.
cd /d "%~dp0"

echo [1/3] Unblocking files (MOTW) ...
powershell -NoProfile -Command "Get-ChildItem -Recurse -File | Unblock-File"

echo.
echo [2/3] .NET Framework check ...
reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" /v Release
echo Release ^>= 461808 means .NET Framework 4.7.2+ (required by pythonnet 3.x).
echo If lower, install .NET Framework 4.8 Runtime.

echo.
echo [3/3] WebView2 Runtime check (required by the app window) ...
set "WV2="
for /f "tokens=3" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv 2^>nul ^| findstr /i /c:"pv"') do set "WV2=%%A"
if not defined WV2 for /f "tokens=3" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv 2^>nul ^| findstr /i /c:"pv"') do set "WV2=%%A"
if defined WV2 (
  echo WebView2 Runtime: FOUND version %WV2%
) else (
  echo WebView2 Runtime: NOT FOUND
  echo The window cannot start without it. Install the Evergreen Runtime:
  echo https://developer.microsoft.com/microsoft-edge/webview2/
)
echo.
pause
