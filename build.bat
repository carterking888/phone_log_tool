@echo off
REM ASCII only. Thin launcher: real build lives in build_pyd.bat (pyd obfuscated).
cd /d "%~dp0"
call build_pyd.bat
