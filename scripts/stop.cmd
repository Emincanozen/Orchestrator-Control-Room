@echo off
rem Stops the background server started by start.cmd / "orchestrator dashboard" (by the pid in server.pid in the data directory only).
setlocal
set "INTERACTIVE="
echo %CMDCMDLINE% | "%SystemRoot%\System32\find.exe" /i "/c" >nul && set "INTERACTIVE=1"
if defined ORCHESTRATOR_NO_PAUSE set "INTERACTIVE="
call "%~dp0orchestrator.cmd" stop
set "RC=%ERRORLEVEL%"
if defined INTERACTIVE pause
exit /b %RC%
