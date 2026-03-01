@echo off
cd /d "%~dp0"

echo === TodoX Restarting System ===

:: 1. Release file locks by killing all project-related processes FIRST
echo Shutting down existing TodoX processes...

:: Kill Web server on port 8899
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8899" ^| find "LISTENING"') do taskkill /F /PID %%a 2>nul
if %ERRORLEVEL% EQU 0 echo Killed Web Process.

:: Kill Main Orchestrator and any sub-processes running todox_main.py
:: Using powershell for modern and reliable command-line matching
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%.exe' and CommandLine like '%%todox_main.py%%'\" | Foreach-Object { echo \"Killing Orchestrator (PID $($_.ProcessId))...\"; Stop-Process -Id $_.ProcessId -Force }" 2>nul

:: Give the OS a few seconds to fully release file handles
timeout /t 3 /nobreak >nul

:: 2. Pull latest code
echo Pulling latest changes from git...
git pull

:: 3. Update dependencies
echo Setting up environment and dependencies...
call conda activate todox
if exist "requirements.txt" (
    pip install -r requirements.txt
) else if exist "environment.yml" (
    conda env update -f environment.yml
)

:: 4. Restart the system
echo Starting TodoX in background...
:: Clear log file to prevent access conflicts, but use append if deletion fails
del todox.log 2>nul
start "" pythonw todox_main.py > todox.log 2>&1

echo Restart sequence complete.
