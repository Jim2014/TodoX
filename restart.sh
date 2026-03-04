#!/bin/bash

# Ensure only one instance of this script runs at a time
LOCKFILE="/tmp/todox_restart.pid"
if [ -f "$LOCKFILE" ]; then
    PID=$(cat "$LOCKFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Restart is already in progress (PID $PID)."
        exit 1
    fi
fi
echo $$ > "$LOCKFILE"

# Move to the script's directory
cd "$(dirname "$0")"

# Pull latest code
git pull

# Activate conda environment and update dependencies
# We source conda manually to ensure it works in non-interactive shell
CONDA_BASE=$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")
if [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    source "$CONDA_BASE/etc/profile.d/conda.sh"
    conda activate todox
fi

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
elif [ -f "environment.yml" ]; then
    conda env update -f environment.yml
fi

# Kill only the TodoX main process gracefully first
pkill -f "python todox_main.py" || true
sleep 2

# Force kill any remaining TodoX main processes
pkill -9 -f "python todox_main.py" || true

# Kill any orphaned python multiprocessing workers related to todox
pkill -9 -f "todox.*multiprocessing" || true

# Kill any orphaned Web UI processes holding the port
lsof -t -i :8899 | xargs kill -9 2>/dev/null || true

# Restart TodoX in the background
nohup python todox_main.py > todox_run.log 2>&1 &
