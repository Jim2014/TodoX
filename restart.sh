#!/bin/bash
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

# Kill only the TodoX main process
pkill -f "python todox_main.py"

# Restart TodoX in the background
nohup python todox_main.py > todox_run.log 2>&1 &
