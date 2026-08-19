@echo off
setlocal
if defined TEXPAGE_BRIDGE_PYTHON (
  "%TEXPAGE_BRIDGE_PYTHON%" "%~dp0texpage_bridge.py" %*
) else (
  python "%~dp0texpage_bridge.py" %*
)
exit /b %ERRORLEVEL%
