import os
import sys
import shutil
import asyncio
import subprocess
import html
import traceback

async def run_gemini_cli_stream(instruction: str, model: str, out_lines: int, send_chunk_callback, log_callback=None, cwd=None):
    """
    Executes the Gemini CLI program with streaming outputs, robust error handling, 
    and cross-platform PTY workarounds.
    
    Args:
        instruction (str): The prompt to pass to Gemini CLI (-p)
        model (str): Optional model string to pass (-m)
        out_lines (int): Number of lines to buffer before calling the callback
        send_chunk_callback (callable): Async function taking (html_string) to stream output back
        log_callback (callable, optional): Sync function taking (log_string) for server-side logging
    """
    if log_callback:
        log_callback(f"--- Executing: {instruction} ---")
        
    try:
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
            # Grab the active loop before entering the background thread
            loop = asyncio.get_running_loop()
            
            # Run the exact worker.py Popen implementation in a background thread to avoid blocking
            def run_windows_process():
                cmd_str = subprocess.list2cmdline(cmd_args)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                
                # Force node into a dumb, non-interactive terminal that safely bypasses conpty and node-pty
                env = os.environ.copy()
                env["CI"] = "true"
                env["FORCE_COLOR"] = "0"
                env["TERM"] = "dumb"
                
                process = subprocess.Popen(
                    cmd_str,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, 
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=cwd,
                    env=env,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
                
                for line in iter(process.stdout.readline, ''):
                    decoded_line = line.rstrip('\n')
                    
                    if any(x in decoded_line for x in [
                        "conpty_console_list_agent.js",
                        "var consoleProcessList = getConsoleProcessList(shellPid)",
                        "Error: AttachConsole failed",
                        "node-pty",
                        "node:internal/modules/cjs",
                        "TracingChannel.traceSync",
                        "Execution Async Resource",
                        "wrapModuleLoad",
                        "Function.executeUserEntryPoint",
                        "node:internal/main/run_main_module"
                    ]) or decoded_line.strip() == "^" or (
                        not decoded_line.strip() and all_output and "var consoleProcessList = getConsoleProcessList(shellPid);" in all_output[-1]
                    ):
                        continue
                        
                    print(f"STDOUT: {decoded_line}")
                    if log_callback:
                        log_callback(f"STDOUT: {decoded_line}")
                    all_output.append(decoded_line)
                    
                    if out_lines > 0:
                        chunk_buffer.append(decoded_line)
                        if len(chunk_buffer) >= out_lines:
                            chunk = "\n".join(chunk_buffer)
                            chunk_buffer.clear()
                            chunk_html = html.escape(chunk)
                            if len(chunk_html) > 4000:
                                chunk_html = chunk_html[:4000] + "..."
                            
                            async def notify():
                                try:
                                    await send_chunk_callback(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>")
                                except Exception as e:
                                    print(f"EXCEPTION sending chunk: {e}")
                                    if log_callback:
                                        log_callback(f"EXCEPTION sending chunk: {e}")
                            asyncio.run_coroutine_threadsafe(notify(), loop)
                
                process.stdout.close()
                process.wait()

            await asyncio.to_thread(run_windows_process)
            
        else:
            # Mac/Linux run via exec securely
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            async def read_stream(stream, prefix):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded_line = line.decode('utf-8', errors='replace').rstrip('\r\n')
                    
                    if any(x in decoded_line for x in [
                        "conpty_console_list_agent.js",
                        "var consoleProcessList = getConsoleProcessList(shellPid)",
                        "Error: AttachConsole failed",
                        "node-pty",
                        "node:internal/modules/cjs",
                        "TracingChannel.traceSync",
                        "Execution Async Resource",
                        "wrapModuleLoad",
                        "Function.executeUserEntryPoint",
                        "node:internal/main/run_main_module"
                    ]) or decoded_line.strip() == "^" or (
                        not decoded_line.strip() and all_output and "var consoleProcessList = getConsoleProcessList(shellPid);" in all_output[-1]
                    ):
                        continue
                        
                    print(f"{prefix}: {decoded_line}")
                    if log_callback:
                        log_callback(f"{prefix}: {decoded_line}")
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
                                await send_chunk_callback(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>")
                            except Exception as e:
                                print(f"EXCEPTION sending chunk: {e}")
                                if log_callback:
                                    log_callback(f"EXCEPTION sending chunk: {e}")

            await asyncio.gather(
                read_stream(process.stdout, "STDOUT"),
                read_stream(process.stderr, "STDERR")
            )
            await process.wait()
        
        # Flush remaining chunk buffer
        if out_lines > 0:
            if chunk_buffer:
                chunk = "\n".join(chunk_buffer)
                chunk_html = html.escape(chunk)
                if len(chunk_html) > 4000:
                    chunk_html = chunk_html[:4000] + "..."
                try:
                    await send_chunk_callback(f"<pre><code class=\"language-text\">{chunk_html}</code></pre>")
                except Exception as e:
                    pass
        
        return all_output
            
    except Exception as e:
        print(f"❌ EXCEPTION executing gemini cli: {e}")
        if log_callback:
            log_callback(f"EXCEPTION: {e}")
        raise
