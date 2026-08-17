@echo off
REM Run imla in a visible window so you can see what it transcribes.
REM Resolve Python explicitly - a stale PATH resolves "python" to the
REM Microsoft Store stub, which is not a working interpreter.
setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" imla.py %*
if errorlevel 1 pause
