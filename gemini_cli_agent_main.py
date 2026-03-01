import os
import shlex
import asyncio
import html
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("GCLI_TG_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# Verify if we should restrict to admin
if not ADMIN_CHAT_ID:
    print("WARNING: ADMIN_CHAT_ID not found in .env. The /gcli command will be available to ANYONE who messages the bot.")

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

# Configurable Model Aliases
SMALL_MODEL = "gemini-2.5-flash"
LARGE_MODEL = "gemini-2.5-pro"

MAX_LOG_LINES = 1000
log_history = []

def append_to_log(text: str):
    global log_history
    lines = text.split('\n')
    for line in lines:
        if line.strip():
            log_history.append(line.strip())
    # Keep only the last MAX_LOG_LINES
    if len(log_history) > MAX_LOG_LINES:
        log_history = log_history[-MAX_LOG_LINES:]

async def gcli_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security check
    chat_id = str(update.effective_chat.id)
    if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access.")
        return

    num_lines = 10
    args = context.args if context.args is not None else update.message.text.split()[1:]
    if args:
        try:
            num_lines = int(args[0])
        except ValueError:
            await update.message.reply_text("Usage: /gcli_log [number]\nNumber must be an integer.")
            return

    if not log_history:
        await update.message.reply_text("Log is empty.")
        return

    # Get the requested number of lines, bounded by available history
    lines_to_show = log_history[-num_lines:] if num_lines > 0 else []
    
    if not lines_to_show:
        await update.message.reply_text("No lines to show.")
        return
        
    text = "\n".join(lines_to_show)
    if len(text) > 3500:
        text = "..." + text[-3500:]
        
    text_html = html.escape(text)
    await update.message.reply_text(f"<b>Last {len(lines_to_show)} log lines:</b>\n<pre><code class=\"language-text\">{text_html}</code></pre>", parse_mode='HTML')

async def gcli_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_incoming = update.message.text if update.message else "No text"
    print(f"\n[TG-RECV] Received command: {raw_incoming}")
    
    # Security check: only allow ADMIN_CHAT_ID if it is set
    chat_id = str(update.effective_chat.id)
    if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
        print(f"[TG-REJECT] Unauthorized chat ID: {chat_id}")
        await update.message.reply_text("Unauthorized access. Your chat ID is not registered as ADMIN.")
        return

    raw_text = update.message.text.partition(' ')[2]
    if not raw_text.strip():
        print(f"[TG-REJECT] Missing arguments.")
        await update.message.reply_text("Usage: /gcli [-m model | -sm | -lm] [-o lines] -msg [instruction]\nExample: /gcli -sm -o 10 -msg check status")
        return
        
    try:
        msg_idx = raw_text.find('-msg ')
        if msg_idx == -1:
            print(f"[TG-REJECT] Could not find '-msg ' in {raw_text}")
            await update.message.reply_text("Error: -msg argument is required.\nUsage: /gcli [-m model | -sm | -lm] [-o lines] -msg [instruction]")
            return
            
        instruction = raw_text[msg_idx + 5:].strip()
        pre_args = shlex.split(raw_text[:msg_idx])
        parsed_args = parse_args(pre_args)
    except Exception as e:
        print(f"[TG-ERROR] Parse exception: {e}")
        await update.message.reply_text(f"Error parsing arguments: {e}")
        return
        
    if not instruction:
        print("[TG-REJECT] Instruction was empty.")
        await update.message.reply_text("Error: -msg argument is required.\nUsage: /gcli [-m model | -sm | -lm] [-o lines] -msg [instruction]")
        return
        
    # Model Resolution Priority: Explicit (-m) -> Small (-sm) -> Large (-lm) -> None (Default)
    model = ''
    if 'm' in parsed_args and isinstance(parsed_args['m'], str):
        model = parsed_args['m']
    elif 'sm' in parsed_args:
        model = SMALL_MODEL
    elif 'lm' in parsed_args:
        model = LARGE_MODEL
        
    out_lines = 0
    if 'o' in parsed_args:
        try:
            out_lines = int(parsed_args['o'])
        except ValueError:
            print("[TG-REJECT] -o flag was not an integer.")
            await update.message.reply_text("Error: -o must be an integer.")
            return

    await _execute_gcli(update, instruction, model, out_lines)

async def _execute_gcli(update: Update, instruction: str, model: str, out_lines: int):
    status_text = f"⏳ Running Gemini CLI with instructions:\n<pre><code>{html.escape(instruction)}</code></pre>"
    if model:
        status_text = f"⏳ Running Gemini CLI [Model: {model}] with instructions:\n<pre><code>{html.escape(instruction)}</code></pre>"
        
    status_msg = await update.message.reply_text(status_text, parse_mode='HTML')
    print(f"--- Executing Gemini CLI ---")
    print(f"Instruction: {instruction}")
    print(f"Model: {model if model else 'default'}")
    print(f"----------------------------")
    append_to_log(f"--- Executing: {instruction} ---")
    
    try:
        import shutil
        import sys
        import subprocess
        
        gemini_exec = shutil.which("gemini")
        if not gemini_exec:
            raise FileNotFoundError("Could not find 'gemini' executable in system PATH.")
            
        # Build command dynamically
        cmd_args = [gemini_exec, "-y", "-p", instruction]
        if model:
            cmd_args.extend(["-m", model])
            
        all_output = []
        chunk_buffer = []

        if sys.platform == "win32":
            # Run the exact worker.py Popen implementation in a background thread to avoid blocking Telegram
            def run_windows_process():
                cmd_str = subprocess.list2cmdline(cmd_args)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                
                process = subprocess.Popen(
                    cmd_str,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, # Merge stderr into stdout exactly like worker.py
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
                
                for line in iter(process.stdout.readline, ''):
                    decoded_line = line.rstrip('\n')
                    print(f"STDOUT: {decoded_line}")
                    append_to_log(f"STDOUT: {decoded_line}")
                    all_output.append(decoded_line)
                    
                    if out_lines > 0:
                        chunk_buffer.append(decoded_line)
                        if len(chunk_buffer) >= out_lines:
                            chunk = "\n".join(chunk_buffer)
                            chunk_buffer.clear()
                            chunk_html = html.escape(chunk)
                            if len(chunk_html) > 4000:
                                chunk_html = chunk_html[:4000] + "..."
                            
                            # We can't safely call await update.message.reply_text from a background thread synchronously.
                            # So we schedule it onto the main asyncio loop.
                            async def send_chunk(text_chunk):
                                try:
                                    await update.message.reply_text(f"<pre><code class=\"language-text\">{text_chunk}</code></pre>", parse_mode='HTML')
                                except Exception as e:
                                    print(f"EXCEPTION sending chunk: {e}")
                                    append_to_log(f"EXCEPTION sending chunk: {e}")
                                    
                            asyncio.run_coroutine_threadsafe(send_chunk(chunk_html), asyncio.get_running_loop())
                
                process.stdout.close()
                process.wait()

            await asyncio.to_thread(run_windows_process)
            
        else:
            # Mac/Linux run via exec securely
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            async def read_stream(stream, prefix):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded_line = line.decode('utf-8', errors='replace').rstrip('\r\n')
                    print(f"{prefix}: {decoded_line}")
                    append_to_log(f"{prefix}: {decoded_line}")
                    all_output.append(decoded_line)
                    
                    if out_lines > 0:
                        chunk_buffer.append(decoded_line)
                        if len(chunk_buffer) >= out_lines:
                            chunk = "\n".join(chunk_buffer)
                            chunk_buffer.clear()
                            chunk_html = html.escape(chunk)
                            if len(chunk_html) > 4000:
                                chunk_html = chunk_html[:4000] + "..."
                            try:
                                await update.message.reply_text(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>", parse_mode='HTML')
                            except Exception as e:
                                print(f"EXCEPTION sending chunk: {e}")
                                append_to_log(f"EXCEPTION sending chunk: {e}")

            await asyncio.gather(
                read_stream(process.stdout, "STDOUT"),
                read_stream(process.stderr, "STDERR")
            )
            await process.wait()
        
        if out_lines > 0:
            if chunk_buffer:
                chunk = "\n".join(chunk_buffer)
                chunk_html = html.escape(chunk)
                if len(chunk_html) > 4000:
                    chunk_html = chunk_html[:4000] + "..."
                try:
                    await update.message.reply_text(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>", parse_mode='HTML')
                except Exception as e:
                    pass
            await update.message.reply_text(f"✅ Finished Gemini CLI execution.")
        else:
            response = "\n".join(all_output)
            if not response:
                response = "Command finished with no output."
                append_to_log("Command finished with no output.")
            
            if len(response) > 4000:
                response = "..." + response[-4000:]
                
            response_html = html.escape(response)
            await status_msg.edit_text(f"✅ Finished Gemini CLI:\n<pre><code class=\"language-text\">{response_html}</code></pre>", parse_mode='HTML')
            
    except Exception as e:
        print(f"❌ EXCEPTION executing gemini cli: {e}")
        append_to_log(f"EXCEPTION: {e}")
        try:
            await status_msg.edit_text(f"❌ Failed to execute gemini cli: {e}")
        except Exception as edit_err:
            await update.message.reply_text(f"❌ Failed to execute gemini cli: {e}")

async def gcli_commit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instruction = "Examine the current git status and git diff. Add any temporary or generated files that should not be tracked to .gitignore. Then, commit all other meaningful changes with a detailed and descriptive commit message, and push the commit to the remote repository."
    
    print(f"\n[TG-RECV-WRAPPER] Routing /gcli_commit to main gcli engine")
    await _execute_gcli(update, instruction, SMALL_MODEL, 10)

async def post_init(app: ApplicationBuilder):
    if ADMIN_CHAT_ID:
        welcome_text = (
            "🚀 *Gemini CLI Agent is online and ready!*\n\n"
            "*Available Commands:*\n"
            "• `/gcli [-m model | -sm | -lm] [-o lines] -msg [instruction]`\n"
            "  Run a command via Gemini CLI.\n"
            "  _Example:_ `/gcli -sm -o 10 -msg check status`\n\n"
            "• `/gcli_commit`\n"
            "  Auto-commit and push changes using Gemini.\n\n"
            "• `/gcli_log [number]`\n"
            "  View recent output history (max 1000 lines).\n"
            "  _Example:_ `/gcli_log 20` (defaults to 10 if number omitted)\n"
        )
        try:
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID, 
                text=welcome_text,
                parse_mode='Markdown'
            )
            print(f"Sent startup message to admin chat ID: {ADMIN_CHAT_ID}")
        except Exception as e:
            print(f"Failed to send startup message: {e}")

def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_gemini_cli_telegram_bot_token_here":
        print("GCLI_TG_TOKEN not set or invalid in .env file. Exiting.")
        return
        
    print("=========================================")
    print("Starting Gemini CLI Agent Polling...")
    print("Ensure GCLI_TG_TOKEN is distinct from TodoX TG_TOKEN to avoid Conflict exceptions.")
    print("=========================================")
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("gcli", gcli_command))
    app.add_handler(CommandHandler("gcli_log", gcli_log_command))
    app.add_handler(CommandHandler("gcli_commit", gcli_commit_command))
    
    # Catch-all for when formatting breaks CommandHandler (e.g. backticks)
    async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text if update.message and update.message.text else ""
        text = text.strip().strip("`").strip()
        
        # We can't overwrite update.message.text in telegram v20, 
        # so we have to manually route by examining the raw string directly
        if text.startswith("/gcli_log"):
            # Mock the context args manually
            parts = text.split()
            context.args = parts[1:] if len(parts) > 1 else []
            await gcli_log_command(update, context)
        elif text.startswith("/gcli_commit"):
            await gcli_commit_command(update, context)
        elif text.startswith("/gcli"):
            # Reconstruct the string without backticks to pass validation inside gcli_command parser
            update.message._text = text # forcefully set internal private var for the rare case we need it
            await gcli_command(update, context)
        else:
            print(f"[TG-DEBUG] Ignored unrelated text: {text}")
            
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^[\\`]?/gcli'), fallback_handler))
    
    try:
        app.run_polling()
    except Exception as e:
        print(f"Error while polling: {e}")

if __name__ == '__main__':
    main()
