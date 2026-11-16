@echo off
rem Repo-relative wrapper: runs the orchestrator CLI from any directory, in cmd.exe or PowerShell.
rem Needs uv on PATH. Does not touch global state and does not need admin rights or a changed ExecutionPolicy
rem (this is a .cmd file, not a .ps1 script). Usage:  scripts\orchestrator.cmd status
setlocal
uv run --quiet --project "%~dp0..\backend" orchestrator %*
exit /b %ERRORLEVEL%
