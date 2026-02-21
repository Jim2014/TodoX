import os
import re
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

from .storage import (
    load_projects, load_task, save_task, create_project,
    delete_project, update_project, create_task, delete_task,
    update_task, add_comment, delete_comment, update_comment
)
from .models import Comment

load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN", "")

async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    projects = load_projects()
    if not projects:
        await update.message.reply_text("No projects found.")
        return
    text = "Projects List:\n"
    for pid, proj in projects.items():
        text += f"- {pid} ({proj.status}): {proj.title} [Assignee: {proj.assignee}]\n"
    await update.message.reply_text(text)

async def p_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /p [PID]")
        return
    pid = context.args[0].upper()
    task = load_task(pid, f"{pid}-0")
    if not task:
        await update.message.reply_text(f"Project {pid} not found.")
        return
    text = f"Project: {task.id}\nTitle: {task.title}\nStatus: {task.status}\nAssignee: {task.assignee}\nDesc: {task.descriptions}\nChildren: {', '.join(task.child_ids)}"
    await update.message.reply_text(text)

async def t_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /t [TID]")
        return
    tid = context.args[0].upper()
    pid = tid.split('-')[0]
    task = load_task(pid, tid)
    if not task:
        await update.message.reply_text(f"Task {tid} not found.")
        return
    
    comments_text = "\n".join([f"[{c.time}] {c.author}: {c.content}" for c in task.comments[-5:]])
    text = f"Task: {task.id}\nTitle: {task.title}\nStatus: {task.status}\nAssignee: {task.assignee}\n\nRecent Comments:\n{comments_text}"
    if not comments_text:
        text += "None"
    await update.message.reply_text(text)

async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /new [PID] "[Desc]"
    text = " ".join(context.args)
    match = re.match(r'^(\w+)\s+"(.*)"$', text)
    if not match:
        await update.message.reply_text('Usage: /new [PID] "[Desc]"')
        return
    pid, desc = match.groups()
    pid = pid.upper()
    
    # Check if exists
    if load_task(pid, f"{pid}-0"):
        await update.message.reply_text(f"Project {pid} already exists.")
        return
        
    # Read seed prompt
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "default_gemini.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        seed_prompt = f.read()
        
    create_project(pid, desc, desc, seed_prompt)
    
    # Also write GEMINI.md into the new directory
    from .storage import get_data_dir
    gemini_md_path = os.path.join(get_data_dir(), "projects", pid, "GEMINI.md")
    with open(gemini_md_path, "w", encoding="utf-8") as f:
        f.write(seed_prompt)
        
    await update.message.reply_text(f"Project {pid} created: {desc}")

async def comment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /comment [TID] "[Msg]"
    text = " ".join(context.args)
    match = re.match(r'^([\w\-]+)\s+"(.*)"$', text)
    if not match:
        await update.message.reply_text('Usage: /comment [TID] "[Msg]"')
        return
    tid, msg = match.groups()
    tid = tid.upper()
    pid = tid.split('-')[0]
    
    task = load_task(pid, tid)
    if not task:
        await update.message.reply_text(f"Task {tid} not found.")
        return
        
    task.comments.append(Comment(
        author="Admin",
        time=datetime.now().isoformat(),
        content=msg
    ))
    save_task(pid, task)
    await update.message.reply_text(f"Comment added to {tid}.")

async def assign_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /assign [TID] [Role]")
        return
    tid = context.args[0].upper()
    role = context.args[1]
    pid = tid.split('-')[0]
    
    task = load_task(pid, tid)
    if not task:
        await update.message.reply_text(f"Task {tid} not found.")
        return
        
    task.assignee = role
    if role.upper() == "AI":
        task.status = "in_progress"
    save_task(pid, task)
    await update.message.reply_text(f"Task {tid} assigned to {role}. Status updated to {task.status}.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 *TodoX C&C Bot Commands*\n\n"
        "/help - Show this help message\n"
        "/projects - List all projects and their status\n"
        "/p [PID] - Show details of a specific project\n"
        "/t [TID] - Show details and recent comments of a task\n"
        '/new [PID] "[Desc]" - Create a new project\n'
        '/delp [PID] - Delete project\n'
        '/updp [PID] [Field] [Value] - Update project field (status, title, assignee)\n'
        '/newt [PID] [ParentTID] "[Title]" "[Desc]" - Create a task\n'
        '/delt [TID] - Delete a task\n'
        '/updt [TID] [Field] [Value] - Update a task field\n'
        '/comment [TID] "[Msg]" - Add a comment to a task\n'
        '/delc [TID] [Index] - Delete a comment\n'
        '/updc [TID] [Index] "[Msg]" - Update a comment\n'
        "/assign [TID] [Role] - Assign a task (e.g., Admin, AI). Assigning to AI triggers execution."
    )
    await update.message.reply_text(help_text)

async def delp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delp [PID]")
        return
    pid = context.args[0].upper()
    if delete_project(pid):
        await update.message.reply_text(f"Project {pid} deleted.")
    else:
        await update.message.reply_text(f"Project {pid} not found.")

async def updp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /updp [PID] [Field] [Value]")
        return
    pid = context.args[0].upper()
    field = context.args[1].lower()
    value = " ".join(context.args[2:])
    task = update_project(pid, **{field: value})
    if task:
        await update.message.reply_text(f"Project {pid} updated.")
    else:
        await update.message.reply_text(f"Project {pid} not found.")

async def newt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    match = re.match(r'^([\w\-]+)\s+([\w\-]+)\s+"(.*?)"\s+"(.*?)"$', text)
    if not match:
        await update.message.reply_text('Usage: /newt [PID] [ParentTID] "[Title]" "[Desc]"')
        return
    pid, ptid, title, desc = match.groups()
    pid = pid.upper()
    ptid = ptid.upper()
    
    task = create_task(pid, ptid, title, desc)
    if task:
        await update.message.reply_text(f"Task {task.id} created under {ptid}.")
    else:
        await update.message.reply_text(f"Parent task {ptid} not found.")

async def delt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delt [TID]")
        return
    tid = context.args[0].upper()
    pid = tid.split('-')[0]
    
    if delete_task(pid, tid):
        await update.message.reply_text(f"Task {tid} deleted.")
    else:
        await update.message.reply_text(f"Task {tid} not found.")

async def updt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /updt [TID] [Field] [Value]")
        return
    tid = context.args[0].upper()
    pid = tid.split('-')[0]
    field = context.args[1].lower()
    value = " ".join(context.args[2:])
    
    task = update_task(pid, tid, **{field: value})
    if task:
        await update.message.reply_text(f"Task {tid} updated.")
    else:
        await update.message.reply_text(f"Task {tid} not found.")

async def delc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /delc [TID] [Index]")
        return
    tid = context.args[0].upper()
    pid = tid.split('-')[0]
    try:
        idx = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Index must be an integer.")
        return
        
    task = delete_comment(pid, tid, idx)
    if task:
        await update.message.reply_text(f"Comment {idx} deleted from {tid}.")
    else:
        await update.message.reply_text(f"Task {tid} or comment index not found.")

async def updc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    match = re.match(r'^([\w\-]+)\s+(\d+)\s+"(.*)"$', text)
    if not match:
        await update.message.reply_text('Usage: /updc [TID] [Index] "[Msg]"')
        return
    tid, idx, msg = match.groups()
    tid = tid.upper()
    pid = tid.split('-')[0]
    idx = int(idx)
    
    task = update_comment(pid, tid, idx, msg)
    if task:
        await update.message.reply_text(f"Comment {idx} updated in {tid}.")
    else:
        await update.message.reply_text(f"Task {tid} or comment index not found.")

async def post_init(app: ApplicationBuilder):
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        try:
            await app.bot.send_message(chat_id=admin_chat_id, text="🚀 TodoX Orchestrator is online and ready.")
            print(f"Sent startup message to admin chat ID: {admin_chat_id}")
        except Exception as e:
            print(f"Failed to send startup message: {e}")

def run_bot():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        print("Telegram token not set. Bot will not function correctly. Exiting bot loop.")
        return
        
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("p", p_command))
    app.add_handler(CommandHandler("t", t_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("comment", comment_command))
    app.add_handler(CommandHandler("assign", assign_command))
    app.add_handler(CommandHandler("help", help_command))
    
    app.add_handler(CommandHandler("delp", delp_command))
    app.add_handler(CommandHandler("updp", updp_command))
    app.add_handler(CommandHandler("newt", newt_command))
    app.add_handler(CommandHandler("delt", delt_command))
    app.add_handler(CommandHandler("updt", updt_command))
    app.add_handler(CommandHandler("delc", delc_command))
    app.add_handler(CommandHandler("updc", updc_command))
    
    print("Starting Telegram Bot...")
    app.run_polling()
