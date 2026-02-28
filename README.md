# TodoX

TodoX is an AI-driven project management and development system based on a flat physical architecture and an atomic stateless command protocol. The orchestrator allows admins to assign recursive components of a task tree (`projects.json` and nested `task.json`) to either human Admins or AI workers.

## Features

- **C&C via Telegram**: Operate your task pipeline seamlessly from any device.
- **Narrative Logic**: System activities are pushed to the `comments` arrays for an auditable paper trail.
- **無状态执行 (Stateless Execution)**: Both Web API, Bot handlers, and the AI Worker use standard local JSON structs to load and serialize working memory, making TodoX simple to maintain and naturally resilient to faults.

## Prerequisites & Installation

This project mandates the use of Conda for environment management and requires the `gemini` CLI tool for AI operations.

1. Create and activate the conda environment using `environment.yml`:
   ```bash
   conda env create -f environment.yml
   conda activate todox
   ```
3. Ensure Gemini CLI is installed and accessible in your system's PATH. The orchestrator calls the headless `gemini` executable directly.

4. Configure Environment Variables:
   Rename `.env.template` to `.env` and fill in your keys:
   ```env
   DATA_DIR=./data
   TG_TOKEN=your_telegram_bot_token_here
   ADMIN_CHAT_ID=your_personal_chat_id_here
   ```

## Usage

1. Start the Orchestrator, Web Panel, and AI background Watcher:
   ```bash
   conda activate todox
   python todox_main.py
   ```
   
2. If `TG_TOKEN` is correctly set, your Telegram bot will be online and accept commands like:
   - `/new [PID] "[Project Desc]"`
   - `/projects`
   - `/p [PID]`
   - `/assign [TID] AI` (Triggers AI execution)
   
3. Visit `http://localhost:8899` to view the read-only Visualization Panel.
