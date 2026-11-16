@echo off
rem Double-click to start the AI Orchestrator and open the dashboard.
rem Checks uv and Node, creates the Python environment and builds the frontend when missing,
rem then runs "orchestrator dashboard" (starts the server in the background, opens the browser).
rem Needs no admin rights. Outside this folder it only writes the uv/npm caches and the orchestrator data
rem (%LOCALAPPDATA%\AI-Orchestrator and %LOCALAPPDATA%\AI-Orchestrator-worktrees, or ORCHESTRATOR_DATA_DIR).
setlocal EnableExtensions
set "INTERACTIVE="
echo %CMDCMDLINE% | "%SystemRoot%\System32\find.exe" /i "/c" >nul && set "INTERACTIVE=1"
if defined ORCHESTRATOR_NO_PAUSE set "INTERACTIVE="
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

where uv >nul 2>&1
if errorlevel 1 goto :no_uv
where node >nul 2>&1
if errorlevel 1 goto :no_node
where npm.cmd >nul 2>&1
if errorlevel 1 goto :no_node

if exist "%ROOT%\backend\.venv\Scripts\python.exe" goto :have_venv
echo [1/3] Creating the Python environment ^(uv sync^) - first start only ...
uv sync --project "%ROOT%\backend"
if errorlevel 1 goto :fail
:have_venv

if exist "%ROOT%\frontend\dist\index.html" goto :have_dist
echo [2/3] Building the dashboard ^(npm ci, npm run build^) - first start only ...
pushd "%ROOT%\frontend"
call npm.cmd ci
if errorlevel 1 goto :fail_pop
call npm.cmd run build
if errorlevel 1 goto :fail_pop
popd
:have_dist

echo [3/3] Starting the orchestrator ...
call "%~dp0orchestrator.cmd" dashboard
if errorlevel 1 goto :fail
echo.
echo Running in the background. Stop it with scripts\stop.cmd or "orchestrator stop".
if defined INTERACTIVE timeout /t 5 >nul
exit /b 0

:no_uv
echo uv is not installed or not on PATH.
echo Install it from https://docs.astral.sh/uv/getting-started/installation/ then run this again.
goto :fail
:no_node
echo Node.js 22 ^(with npm^) is not installed or not on PATH.
echo Install it from https://nodejs.org then open a new terminal and run this again.
goto :fail
:fail_pop
popd
:fail
echo.
echo The orchestrator did not start. See the messages above; "scripts\orchestrator.cmd doctor" checks your setup.
if defined INTERACTIVE pause
exit /b 1
