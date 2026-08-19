@echo off
setlocal
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage

set "ACTION=%~2"
if /I "%ACTION%"=="build" goto :allowed
if /I "%ACTION%"=="submit" goto :allowed
if /I "%ACTION%"=="publish" goto :allowed
if /I "%ACTION%"=="request" goto :allowed
if /I "%ACTION%"=="requests" goto :allowed
if /I "%ACTION%"=="status" goto :allowed

echo TEXPAGE AGENT DENIED: action "%ACTION%" is not available through the low-privilege agent interface. 1>&2
echo Allowed: build, submit, publish, request, requests, status 1>&2
exit /b 3

:allowed
call "%~dp0texpage.cmd" %*
exit /b %ERRORLEVEL%

:usage
echo Usage: texpage-agent.cmd PROJECT ^<build^|submit^|publish^|request^|requests^|status^> [args] 1>&2
exit /b 3
