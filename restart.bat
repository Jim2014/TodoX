@echo off
cd /d "%~dp0"

:: Pull latest code
git pull

:: Activate conda environment
call conda activate todox

:: Update dependencies
if exist "requirements.txt" (
    pip install -r requirements.txt
) else if exist "environment.yml" (
    conda env update -f environment.yml
)

:: Kill only the TodoX main process based on command line string matching
for /f "tokens=2" %%a in ('wmic process where "name='python.exe' and commandline like '%%todox_main.py%%'" get processid ^| findstr /r "[0-9]"') do taskkill /F /PID %%a
for /f "tokens=2" %%a in ('wmic process where "name='pythonw.exe' and commandline like '%%todox_main.py%%'" get processid ^| findstr /r "[0-9]"') do taskkill /F /PID %%a

:: Kill process listening on port 8899
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8899" ^| find "LISTENING"') do taskkill /F /PID %%a

:: Wait a brief moment to ensure processes close
timeout /t 2 /nobreak >nul

:: Restart TodoX in the background without opening a new console window
start "" pythonw todox_main.py
