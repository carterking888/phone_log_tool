@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  ADB Tool - pyd obfuscated build (ASCII only, no BOM)
REM  1. deps from requirements.txt (+ cython / pyinstaller)
REM  2. cythonize core/*.py -> .pyd       (setup_pyd.py)
REM  3. pyinstaller onedir, no source .py (pyd_pack.py)
REM     -> pyd_pack also copies fix_and_check.bat + portable adb,
REM        then packs dist\adb_tool_pyd_win64.zip
REM  Notes:
REM   - MSVC/SDK env is injected inside setup_pyd.py, no VS prompt needed
REM   - main.py stays .py (entry only); core/*.py are compiled to .pyd
REM   - old dist\adb_tool is renamed (not deleted) before packing
REM   - extra args are forwarded to pyd_pack.py (e.g. --no-zip)
REM
REM iOS support (pymobiledevice3) is OPTIONAL:
REM   the bat asks Y/N at step 2; Y sets WITH_IOS=1 (~65MB output, needs
REM   pymobiledevice3 installed in the build env). N keeps the package
REM   Android-only (~34MB). WITHOUT iOS the packaged exe cannot see iPhones.
REM After packing, core/*.pyd are moved out to build\_pyd_shadow_* so that
REM source mode (run.bat) is not shadowed by stale compiled modules.
REM ============================================================

set "PY=C:\Users\qifeng.wang\.workbuddy\binaries\python\envs\adbtool\Scripts\python.exe"
if not exist "%PY%" (
    echo [WARN] %PY% not found, fallback to PATH python
    set "PY=python"
)

echo.
echo [1/3] Install dependencies ...
"%PY%" -m pip install -q -r requirements.txt
"%PY%" -m pip install -q "cython>=3.0" "pyinstaller>=6.0" setuptools wheel
if errorlevel 1 ( echo [FAIL] pip install error & pause & exit /b 1 )

REM ---- iOS support: ask every time (env WITH_IOS is read by adb_tool.spec) ----
REM   Y -> WITH_IOS=1: bundles pymobiledevice3 (~65MB output, iOS devices work)
REM   N -> Android-only (~34MB output). iOS features only in source mode
REM        (python main.py) where the venv has pymobiledevice3 installed.
REM Note: if N, the packaged exe has NO iOS support at all (device page will
REM       not list iPhones). qh3 (QUIC/tunnel, unused by this tool) is always
REM       excluded to save ~5MB.
set "WITH_IOS=0"
choice /c YN /n /m "Include iOS support (pymobiledevice3, output ~65MB)? [Y/N] "
if errorlevel 2 goto ask_done
"%PY%" -c "import pymobiledevice3" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] pymobiledevice3 is not installed in the build env.
    echo        Run: "%PY%" -m pip install pymobiledevice3
    pause
    exit /b 1
)
set "WITH_IOS=1"
:ask_done
if "%WITH_IOS%"=="1" (echo     -^> WITH_IOS=1, iOS support bundled) else (echo     -^> Android-only build)

echo.
echo [2/3] Cython build: core/*.py -^> .pyd ...
"%PY%" setup_pyd.py build_ext --inplace
if errorlevel 1 ( echo [FAIL] cython build error & pause & exit /b 1 )

echo.
echo [3/3] PyInstaller pack + zip (pyd, source .py excluded) ...
"%PY%" pyd_pack.py %1 %2 %3
if errorlevel 1 ( echo [FAIL] pack error & pause & exit /b 1 )

echo.
echo Done:
echo   dist\adb_tool\adb_tool.exe
echo   dist\adb_tool_pyd_win64.zip   ^<- distribute this one
pause
