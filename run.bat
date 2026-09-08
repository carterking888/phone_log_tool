@echo off
rem ASCII only. Launch the ADB desktop tool.
set "PY=C:\Users\qifeng.wang\.workbuddy\binaries\python\envs\adbtool\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" main.py
if errorlevel 1 pause
