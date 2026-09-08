@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  ADB Tool - macOS packaging helper (run on Windows, ASCII only)
REM
REM  PyInstaller CANNOT cross-compile: a Windows machine can never
REM  produce a mac .app directly. This bat therefore offers the two
REM  realistic paths:
REM
REM   build_mac_win.bat            (or: pack)
REM       -> dist\adb_tool_src_mac.zip  (clean source package)
REM       Copy it to any mac, then:
REM           unzip adb_tool_src_mac.zip -d adb_tool && cd adb_tool
REM           chmod +x build_mac.sh && ./build_mac.sh
REM       Output on the mac: dist\adb_tool.app + adb_tool_macos_<arch>.zip
REM
REM   build_mac_win.bat gh [arch] [dmg]
REM       -> trigger the GitHub Actions macos runner (.github/workflows/
REM          build-mac.yml), wait, then download zip/dmg artifacts.
REM       Requires: a git repo with a GitHub remote + gh CLI logged in.
REM       arch: both(default) | arm64 | x86_64    dmg: true(default) | false
REM
REM  Notes:
REM   - source zip excludes build junk: dist/build/.venv_mac/__pycache__/
REM     .workbuddy/.idea/.git, core\*.pyd|*.so|*.c, *.log
REM   - after packing, source mode stays unaffected (nothing moved)
REM ============================================================

set "MODE=%~1"
if "%MODE%"=="" set "MODE=pack"
if /i "%MODE%"=="pack" goto pack
if /i "%MODE%"=="gh" goto gh

echo [ERROR] unknown mode: %MODE%
echo usage:  build_mac_win.bat [pack^|gh] [arch] [dmg]
exit /b 1

:pack
echo [1/3] Stage clean source tree ...
set "STAGE=%TEMP%\adb_tool_src_mac_stage"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
robocopy . "%STAGE%" /E ^
  /XD dist build .venv_mac .venv __pycache__ .workbuddy .idea .git ^
  /XF *.pyd *.so *.c *.log *.pyc >nul
if errorlevel 8 ( echo [FAIL] robocopy error & exit /b 1 )

echo [2/3] Zip staging -> dist\adb_tool_src_mac.zip ...
if not exist dist mkdir dist
if exist dist\adb_tool_src_mac.zip del /q dist\adb_tool_src_mac.zip
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%\*' -DestinationPath 'dist\adb_tool_src_mac.zip' -Force"
if errorlevel 1 ( echo [FAIL] zip error & exit /b 1 )
rmdir /s /q "%STAGE%"

echo [3/3] Done.
for %%Z in (dist\adb_tool_src_mac.zip) do echo   output: %%Z  (%%~zZ bytes)
echo.
echo   Next steps (on any mac):
echo     1. copy dist\adb_tool_src_mac.zip to the mac
echo     2. unzip adb_tool_src_mac.zip -d adb_tool  ^&^&  cd adb_tool
echo     3. chmod +x build_mac.sh  ^&^&  ./build_mac.sh        (add --dmg if needed)
echo     4. pick dist\adb_tool_macos_^<arch^>.zip and send it to the target mac
echo        (arch must match the target chip: arm64 = M series, x86_64 = Intel)
exit /b 0

:gh
set "ARCH=%~2"
if "%ARCH%"=="" set "ARCH=both"
set "DMG=%~3"
if "%DMG%"=="" set "DMG=true"

echo [1/4] Preflight ...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [ERROR] not a git repository. Either init one with a GitHub remote,
  echo         or use "build_mac_win.bat pack" and build on a real mac.
  exit /b 1
)
where gh >nul 2>&1
if errorlevel 1 ( echo [ERROR] gh CLI not found. Install: winget install GitHub.cli & exit /b 1 )
gh auth status >nul 2>&1
if errorlevel 1 ( echo [ERROR] gh not logged in. Run: gh auth login & exit /b 1 )
git remote get-url origin >nul 2>&1
if errorlevel 1 ( echo [ERROR] no git remote "origin". Add it first: git remote add origin https://github.com/USER/REPO.git & exit /b 1 )

for /f "delims=" %%A in ('git status --porcelain') do set "DIRTY=1"
if defined DIRTY (
  echo [WARN] working tree has uncommitted changes. Commit ^& push first:
  echo          git add -A ^&^& git commit -m "build" ^&^& git push
  echo        Aborting to avoid building stale code.
  exit /b 1
)
git push origin HEAD 2>nul || echo [WARN] push failed, workflow may build an older commit.

echo [2/4] Dispatch workflow (arch=%ARCH%, dmg=%DMG%) ...
gh workflow run build-mac.yml -f arch=%ARCH% -f dmg=%DMG%
if errorlevel 1 ( echo [FAIL] dispatch error & exit /b 1 )
timeout /t 5 /nobreak >nul
for /f "tokens=1" %%R in ('gh run list --workflow=build-mac.yml --limit 1 --json databaseId --jq ".[0].databaseId"') do set "RUN=%%R"
if not defined RUN ( echo [FAIL] cannot read run id & exit /b 1 )
echo     run id: %RUN%

echo [3/4] Waiting for run to finish (arm64+x86_64 build, ~5-10 min) ...
gh run watch %RUN% --exit-status --interval 30
if errorlevel 1 ( echo [FAIL] run failed. Log: gh run view %RUN% --log-failed & exit /b 1 )

echo [4/4] Downloading artifacts ...
if not exist dist mkdir dist
gh run download %RUN% --dir dist\mac_artifacts
if errorlevel 1 ( echo [FAIL] download error & exit /b 1 )
dir /s /b dist\mac_artifacts\*.zip dist\mac_artifacts\*.dmg 2>nul
echo.
echo   Done. Distribute dist\mac_artifacts\adb_tool-macos-<arch>\*.zip
echo   to the matching-chip mac (arm64 = M series, x86_64 = Intel).
exit /b 0
