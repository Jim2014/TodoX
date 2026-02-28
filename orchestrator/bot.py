import os
import html
import shlex
import traceback
import platform
import subprocess
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

from .storage import (
    load_projects, load_task, save_task, create_project,
    delete_project, update_project, create_task, delete_task,
    update_task, add_comment, delete_comment, update_comment,
    get_data_dir
)
from .models import Comment

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
        
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "default_gemini.md")
    seed_prompt = ""
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            seed_prompt = f.read()
            
    create_project(pid, title, desc, seed_prompt)
    
    gemini_md_path = os.path.join(get_data_dir(), "projects", pid, "GEMINI.md")
    if seed_prompt:
        os.makedirs(os.path.dirname(gemini_md_path), exist_ok=True)
        with open(gemini_md_path, "w", encoding="utf-8") as f:
            f.write(seed_prompt)
            
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
        from .html_generator import generate_project_html
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
        await update.message.reply_document(document=open(file_path, 'rb'), filename=os.path.basename(path))
    else:
        await update.message.reply_text(f"Error: File '{path}' not found in project {pid}.")

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
from .state import state

async def sys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = state.get_state()
    models = state.get_all_models()
    
    models_list = "\n".join([
        f"{'✅' if i == current['model_index'] else '⬜️'} [{i}] {m}" 
        for i, m in enumerate(models)
    ])
    
    text = (
        "⚙️ *System State*\n\n"
        f"⏳ System Uptime Started: `{current.get('start_time', 'Unknown')}`\n"
        f"🤖 Current Model: `{current['current_model']}` (Index: {current['model_index']})\n"
        f"⏱️ Watcher Interval: `{current['interval_seconds']}s`\n\n"
        "📋 *Available Models:*\n"
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 *TodoX C&C Bot Commands (VOP Format)*\n\n"
        "/help - Show this help message\n"
        "/projects - List all projects and their status\n"
        "/sys - Show system state and available models\n"
        "/upmodel [Index] - Update the AI worker model\n"
        "/restart - Pull latest code, update dependencies, and restart system\n\n"
        "*Projects:*\n"
        "/get-project -pid [PID]\n"
        "/new-project -pid [PID] -title \"[Title]\" -des \"[Description]\"\n"
        "/update-project -pid [PID] -field [Field] -val \"[Value]\"\n"
        "/del-project -pid [PID]\n\n"
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
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        print("Telegram token not set. Bot will not function correctly. Exiting bot loop.")
        return
        
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("sys", sys_command))
    app.add_handler(CommandHandler("upmodel", upmodel_command))
    app.add_handler(CommandHandler("restart", restart_command))
    
    # Catch-all multiline command processor
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'(?m)^/'), handle_multi_line_commands))
    
    print("Starting Telegram Bot (VOP Mode)...")
    app.run_polling()
