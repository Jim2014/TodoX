import os
import io
import html
import json
import shlex
import traceback
import asyncio
import platform
import subprocess
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

from orchestrator.storage import (
    load_projects, load_task, save_task, create_project,
    delete_project, update_project, create_task, delete_task,
    update_task, add_comment, delete_comment, update_comment,
    get_data_dir, get_default_worker_prompt,
)
from orchestrator.models import Comment
from harness import HarnessProfile, HarnessRouter, RunRequest, create_default_registry, format_event_log_line

load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN", "")

def parse_args(arg_list):
    parsed = {}
    i = 0
    while i < len(arg_list):
        if arg_list[i].startswith('-'):
            key = arg_list[i][1:] # remove hyphen
            if i + 1 < len(arg_list) and not arg_list[i+1].startswith('-'):
                parsed[key] = arg_list[i+1]
                i += 2
            else:
                parsed[key] = True
                i += 1
        else:
            i += 1
    return parsed

async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    projects = load_projects()
    if not projects:
        await update.message.reply_text("No projects found.")
        return
    text = "<b>Projects List:</b>\n"
    for pid, proj in projects.items():
        title = html.escape(proj.title)
        status = html.escape(proj.status)
        text += f"- <code>/get-project -pid {pid}</code> ({status}): {title}\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def handle_new_project(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    title = args.get('title', 'New Project')
    desc = args.get('des', '')
    
    if not pid:
        await update.message.reply_text("Error: -pid is required.")
        return
        
    if load_task(pid, f"{pid}-0"):
        await update.message.reply_text(f"Error: Project {pid} already exists.")
        return
        
    seed_prompt = get_default_worker_prompt()
    create_project(pid, title, desc, seed_prompt)
    await update.message.reply_text(f"Project {pid} created: {title}")

async def handle_new_task(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    ptid = args.get('ptid', '').upper()
    title = args.get('title', 'New Task')
    desc = args.get('des', '')
    assignee = args.get('assignee', 'AI')
    status = args.get('status', 'not_started')
    
    if not pid or not ptid:
        await update.message.reply_text("Error: -pid and -ptid are required.")
        return
        
    task = create_task(pid, ptid, title, desc, assignee, status)
    if task:
        await update.message.reply_text(f"Task {task.id} created under {ptid}.")
    else:
        await update.message.reply_text(f"Error: Parent task {ptid} not found.")

async def handle_new_comment(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    msg = args.get('msg', '')
    
    if not pid or not tid or not msg:
        await update.message.reply_text("Error: -pid, -tid, and -msg are required.")
        return
        
    task = add_comment(pid, tid, "Admin", msg)
    if task:
        await update.message.reply_text(f"Comment added to {tid}.")
    else:
        await update.message.reply_text(f"Error: Task {tid} not found.")

async def handle_update_project(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    field = args.get('field', '').lower()
    val = args.get('val', '')
    
    if not pid or not field:
        await update.message.reply_text("Error: -pid and -field are required.")
        return
        
    # Map field names if they differ
    if field == 'des': field = 'descriptions'
    
    task = update_project(pid, **{field: val})
    if task:
        await update.message.reply_text(f"Project {pid} updated ({field} -> {val}).")
    else:
        await update.message.reply_text(f"Error: Project {pid} not found.")

async def handle_update_task(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    field = args.get('field', '').lower()
    val = args.get('val', '')
    
    if not pid or not tid or not field:
        await update.message.reply_text("Error: -pid, -tid, and -field are required.")
        return
        
    if field == 'des': field = 'descriptions'
    
    task = update_task(pid, tid, **{field: val})
    if task:
        await update.message.reply_text(f"Task {tid} updated ({field} -> {val}).")
    else:
        await update.message.reply_text(f"Error: Task {tid} not found.")

async def handle_update_comment(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    cidx = args.get('cidx')
    val = args.get('val', '')
    
    if not pid or not tid or cidx is None or not val:
        await update.message.reply_text("Error: -pid, -tid, -cidx, and -val are required.")
        return
        
    try:
        cidx = int(cidx)
    except ValueError:
        await update.message.reply_text("Error: -cidx must be an integer.")
        return
        
    task = update_comment(pid, tid, cidx, val)
    if task:
        await update.message.reply_text(f"Comment {cidx} updated in {tid}.")
    else:
        await update.message.reply_text(f"Error: Task {tid} or comment index not found.")

async def handle_del_project(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    if not pid:
        await update.message.reply_text("Error: -pid is required.")
        return
    if delete_project(pid):
        await update.message.reply_text(f"Project {pid} deleted.")
    else:
        await update.message.reply_text(f"Error: Project {pid} not found.")

async def handle_del_task(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    if not pid or not tid:
        await update.message.reply_text("Error: -pid and -tid are required.")
        return
    if delete_task(pid, tid):
        await update.message.reply_text(f"Task {tid} deleted.")
    else:
        await update.message.reply_text(f"Error: Task {tid} not found.")

async def handle_del_comment(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    cidx = args.get('cidx')
    
    if not pid or not tid or cidx is None:
        await update.message.reply_text("Error: -pid, -tid, and -cidx are required.")
        return
        
    try:
        cidx = int(cidx)
    except ValueError:
        await update.message.reply_text("Error: -cidx must be an integer.")
        return
        
    task = delete_comment(pid, tid, cidx)
    if task:
        await update.message.reply_text(f"Comment {cidx} deleted from {tid}.")
    else:
        await update.message.reply_text(f"Error: Task {tid} or comment index not found.")

async def handle_get_project(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    if not pid:
        await update.message.reply_text("Error: -pid is required.")
        return
        
    try:
        from orchestrator.html_generator import generate_project_html
        from datetime import datetime
        html_bytes = generate_project_html(pid)
        if html_bytes:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await update.message.reply_document(document=html_bytes, filename=f"project_{pid}_{timestamp}.html")
        else:
            await update.message.reply_text(f"Error: Project {pid} not found.")
    except Exception as e:
        await update.message.reply_text(f"Error generating HTML: {e}\n{traceback.format_exc()}")

async def handle_get_task(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    tid = args.get('tid', '').upper()
    if not pid or not tid:
        await update.message.reply_text("Error: -pid and -tid are required.")
        return
        
    task = load_task(pid, tid)
    if not task:
        await update.message.reply_text(f"Error: Task {tid} not found.")
        return
        
    comments_text = "\n".join([f"[{c.time}] {c.author}: {c.content}" for c in task.comments[-5:]])
    text = f"Task: {task.id}\nTitle: {task.title}\nStatus: {task.status}\nAssignee: {task.assignee}\n\nRecent Comments:\n{comments_text}"
    if not comments_text:
        text += "None"
    await update.message.reply_text(text)

async def handle_get_file(update: Update, args: dict):
    pid = args.get('pid', '').upper()
    path = args.get('path', '')
    if not pid or not path:
        await update.message.reply_text("Error: -pid and -path are required.")
        return
        
    file_path = os.path.join(get_data_dir(), "projects", pid, path)
    if os.path.exists(file_path):
        if path.lower().endswith(".md"):
            try:
                from orchestrator.html_generator import generate_markdown_viewer_html
                
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                html_bytes = generate_markdown_viewer_html(pid, path, content)
                html_io = io.BytesIO(html_bytes)
                
                filename = os.path.basename(path) + ".html"
                await update.message.reply_document(document=html_io, filename=filename)
            except Exception as e:
                await update.message.reply_text(f"Error rendering MD: {e}")
        else:
            with open(file_path, 'rb') as f:
                await update.message.reply_document(document=f, filename=os.path.basename(path))
    else:
        await update.message.reply_text(f"Error: File '{path}' not found in project {pid}.")


async def run_harness_fix_stream(
    instruction: str,
    cwd: str,
    send_chunk_callback,
    log_callback=None,
    harness_name: str = None,
    model: str = None,
    out_lines: int = 10,
):
    """Execute JSON repair via HarnessRouter while streaming buffered HTML chunks."""
    eff_harness = harness_name or state.get_current_harness()
    eff_model = model if model is not None else state.get_current_model()
    router = HarnessRouter(registry=create_default_registry(), state_manager=state)
    profile = HarnessProfile(harness=eff_harness, model=eff_model)
    chunk_buffer = []

    async def on_event(event):
        line = format_event_log_line(event)
        if not line:
            return
        if log_callback:
            log_callback(line)
        if out_lines > 0:
            chunk_buffer.append(line)
            if len(chunk_buffer) >= out_lines:
                chunk = "\n".join(chunk_buffer)
                chunk_buffer.clear()
                chunk_html = html.escape(chunk)
                if len(chunk_html) > 4000:
                    chunk_html = chunk_html[:4000] + "..."
                await send_chunk_callback(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>")

    result = await router.run_step(
        RunRequest(prompt=instruction, working_dir=cwd, model=eff_model),
        profile=profile,
        on_event=on_event,
    )

    if out_lines > 0 and chunk_buffer:
        chunk = "\n".join(chunk_buffer)
        chunk_buffer.clear()
        chunk_html = html.escape(chunk)
        if len(chunk_html) > 4000:
            chunk_html = chunk_html[:4000] + "..."
        await send_chunk_callback(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>")

    if not result.success:
        raise RuntimeError(result.error_message or f"Harness '{eff_harness}' failed with status {result.status}")

    return result.output_lines


async def handle_fix_data(update: Update, args: dict):
    data_dir = get_data_dir()
    projects_dir = os.path.join(data_dir, "projects")
    
    if not os.path.exists(projects_dir):
        await update.message.reply_text("Data directory not found.")
        return

    errors = []
    
    for pid in os.listdir(projects_dir):
        project_dir = os.path.join(projects_dir, pid)
        if not os.path.isdir(project_dir):
            continue

        project_json_path = os.path.join(project_dir, "project.json")
        if os.path.exists(project_json_path):
            try:
                with open(project_json_path, "r", encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                errors.append((project_json_path, str(e)))
                
        tasks_dir = os.path.join(project_dir, "tasks")
        if os.path.exists(tasks_dir):
            for tid in os.listdir(tasks_dir):
                task_dir = os.path.join(tasks_dir, tid)
                if os.path.isdir(task_dir):
                    task_json_path = os.path.join(task_dir, "task.json")
                    if os.path.exists(task_json_path):
                        try:
                            with open(task_json_path, "r", encoding="utf-8") as f:
                                json.load(f)
                        except Exception as e:
                            errors.append((task_json_path, str(e)))

    if not errors:
        await update.message.reply_text("✅ All json files in all projects are valid.")
        return

    error_details = "\n".join([f"- {path}: {err}" for path, err in errors])
    prompt = (
        f"Check the following JSON files across the system which have formatting errors:\n"
        f"{error_details}\n\n"
        "Please fix the JSON format errors and update the files. Ensure the output is valid JSON."
    )

    harness_name = state.get_current_harness()
    await update.message.reply_text(f"Found {len(errors)} JSON errors. Invoking AI harness ({harness_name}) to fix...")

    try:
        async def send_chunk(chunk_html):
            try:
                await update.message.reply_text(chunk_html, parse_mode='HTML')
            except Exception as e:
                print(f"EXCEPTION sending chunk: {e}")

        await run_harness_fix_stream(
            instruction=prompt,
            cwd=data_dir,
            send_chunk_callback=send_chunk,
            log_callback=print,
            harness_name=harness_name,
            out_lines=10,
        )

        await update.message.reply_text(f"✅ AI harness ({harness_name}) finished working on fixes.")

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to run AI harness ({harness_name}): {e}\n{traceback.format_exc()}")

COMMAND_MAP = {
    '/new-project': handle_new_project,
    '/new-task': handle_new_task,
    '/new-comment': handle_new_comment,
    '/update-project': handle_update_project,
    '/update-task': handle_update_task,
    '/update-comment': handle_update_comment,
    '/del-project': handle_del_project,
    '/del-task': handle_del_task,
    '/del-comment': handle_del_comment,
    '/get-project': handle_get_project,
    '/get-task': handle_get_task,
    '/get-file': handle_get_file,
    '/fix-data': handle_fix_data,
}

async def handle_multi_line_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
        
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line.startswith('/'):
            continue # skip non-commands
            
        try:
            tokens = shlex.split(line)
        except Exception as e:
            await update.message.reply_text(f"Error parsing line '{line}': {e}")
            continue
            
        if not tokens:
            continue
            
        cmd = tokens[0].lower()
        if cmd in COMMAND_MAP:
            args = parse_args(tokens[1:])
            await COMMAND_MAP[cmd](update, args)
        elif cmd in ['/sys', '/upmodel', '/help']:
            pass # Handle traditionally or skip, let CommandHandler catch it
        else:
            await update.message.reply_text(f"Unknown command: {cmd}")

# Legacy sys commands remaining attached to context for ease
from orchestrator.state import state

async def sys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = state.get_state()
    chain = state.get_fallback_chain()

    models_list = "\n".join([
        f"{'✅' if i == current['model_index'] else '⬜️'} [{i}] {entry['harness']} / {entry['model']}"
        for i, entry in enumerate(chain)
    ])

    text = (
        "⚙️ *System State*\n\n"
        f"⏳ System Uptime Started: `{current.get('start_time', 'Unknown')}`\n"
        f"🔌 Default Harness: `{current.get('default_harness', 'agy')}`\n"
        f"🤖 Current Model: `{current['current_model']}` (Index: {current['model_index']})\n"
        f"🧠 Reasoning Effort: `{current.get('default_effort', 'high')}`\n"
        f"🛡️ Permission Preset: `{current.get('default_permission_preset', 'auto')}`\n"
        f"⏱️ Watcher Interval: `{current['interval_seconds']}s`\n\n"
        "📋 *Fallback Chain / Available Models:*\n"
        f"{models_list}"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def upmodel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /upmodel [Index]")
        return
        
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Index must be an integer.")
        return
        
    if state.set_model_index(idx):
        await update.message.reply_text(f"Model successfully updated to index {idx} ({state.get_current_model()}).")
    else:
        await update.message.reply_text(f"Invalid index. Must be between 0 and {len(state.get_all_models()) - 1}.")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting TodoX... The system will go offline for a moment.")
    if platform.system() == "Windows":
        subprocess.Popen(["cmd.exe", "/c", "restart.bat"])
    else:
        subprocess.Popen(["bash", "restart.sh"])

async def output_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    num_lines = 10
    if context.args:
        try:
            num_lines = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Number of lines must be an integer.")
            return

    output_log_path = os.path.join(get_data_dir(), "latest_worker_output.log")
    if not os.path.exists(output_log_path):
        await update.message.reply_text("No worker output found.")
        return

    try:
        with open(output_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        last_lines = lines[-num_lines:] if num_lines > 0 else []
        if not last_lines:
            await update.message.reply_text("Output log is empty.")
            return
            
        text = "".join(last_lines)
        if len(text) > 3500:
            text = "..." + text[-3500:]
            
        text = html.escape(text)
        await update.message.reply_text(f"<pre><code class=\"language-text\">{text}</code></pre>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Error reading output: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 *TodoX C&C Bot Commands (VOP Format)*\n\n"
        "/help - Show this help message\n"
        "/projects - List all projects and their status\n"
        "/sys - Show system state and available models\n"
        "/upmodel [Index] - Update the AI worker model\n"
        "/output [number] - Show last [number] lines of AI worker output\n"
        "/restart - Pull latest code, update dependencies, and restart system\n\n"
        "*Projects:*\n"
        "/get-project -pid [PID]\n"
        "/new-project -pid [PID] -title \"[Title]\" -des \"[Description]\"\n"
        "/update-project -pid [PID] -field [Field] -val \"[Value]\"\n"
        "/del-project -pid [PID]\n"
        "/fix-data\n\n"
        "*Tasks:*\n"
        "/get-task -pid [PID] -tid [TID]\n"
        "/new-task -pid [PID] -ptid [ParentTID] -title \"[Title]\" -des \"[Description]\"\n"
        "/update-task -pid [PID] -tid [TID] -field [Field] -val \"[Value]\"\n"
        "/del-task -pid [PID] -tid [TID]\n\n"
        "*Comments:*\n"
        "/new-comment -pid [PID] -tid [TID] -msg \"[Message]\"\n"
        "/update-comment -pid [PID] -tid [TID] -cidx [Index] -val \"[Message]\"\n"
        "/del-comment -pid [PID] -tid [TID] -cidx [Index]\n\n"
        "*Files:*\n"
        "/get-file -pid [PID] -path \"[relative/path]\"\n\n"
        "_Commands can be batched by placing each on a new line._"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def post_init(app: ApplicationBuilder):
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        try:
            await app.bot.send_message(chat_id=admin_chat_id, text="🚀 TodoX Orchestrator is online and ready (VOP Mode).")
            print(f"Sent startup message to admin chat ID: {admin_chat_id}")
        except Exception as e:
            print(f"Failed to send startup message: {e}")

def run_bot():
    """Start the upgraded async TodoXTelegramBot standalone."""
    from telegram_ui import TodoXTelegramBot, parse_allowed_chat_ids

    allowed_ids = parse_allowed_chat_ids(
        os.environ.get("ALLOWED_CHAT_IDS", ""),
        os.environ.get("ADMIN_CHAT_ID", ""),
    )
    allow_all = os.environ.get("ALLOW_ALL_CHATS", "false").strip().lower() in ("1", "true", "yes")
    bot = TodoXTelegramBot(
        bot_token=TELEGRAM_TOKEN,
        allowed_chat_ids=allowed_ids,
        allow_all_chats=allow_all,
        state_manager=state,
    )
    if not bot.is_token_valid():
        print("Telegram token not set. Bot will not function correctly. Exiting bot loop.")
        return

    print("Starting upgraded TodoX Telegram Bot...")
    asyncio.run(bot.start())
