"""Slash-command routing and interactive command handlers for TodoXTelegramBot."""

from __future__ import annotations

import html
import json
import os
import platform
import subprocess
from typing import List, Optional, Tuple

from harness import HarnessProfile, RunRequest
from orchestrator.briefing import generate_ai_project_briefing
from orchestrator.storage import add_comment, get_data_dir, load_projects, load_task

from .formatter import escape_html
from .keyboards import (
    build_effort_keyboard,
    build_harness_keyboard,
    build_help_keyboard,
    build_help_text,
    build_model_keyboard,
    build_permissions_keyboard,
    build_project_briefing_card,
    build_projects_keyboard,
)


class TelegramCommandsMixin:
    """Implements all `/` interactive orchestrator commands."""

    async def dispatch_slash_command(
        self,
        chat_id: int,
        cmd: str,
        arg: str,
        message_id: Optional[int] = None,
    ) -> None:
        """Route interactive slash commands."""
        clean = cmd.lower().split("@")[0].lstrip("/")
        if clean in ("start", "help"):
            await self._cmd_help(chat_id)
        elif clean == "projects":
            await self._cmd_projects(chat_id)
        elif clean == "use":
            await self._cmd_use(chat_id, arg, reply_to_message_id=message_id)
        elif clean == "summary":
            await self._cmd_summary(chat_id, arg, reply_to_message_id=message_id)
        elif clean == "status":
            await self._cmd_status(chat_id)
        elif clean == "pin":
            await self.update_pinned_banner(chat_id, force_repin=True)
        elif clean == "unpin":
            ok = await self.client.unpin_all_chat_messages(chat_id)
            self.pinned_message_ids.pop(chat_id, None)
            await self._safe_send(
                chat_id,
                "✅ Unpinned all pinned messages in this chat." if ok else "⚠️ Could not unpin messages in this chat.",
                reply_to_message_id=message_id,
            )
        elif clean == "harness":
            await self._cmd_harness(chat_id, arg)
        elif clean == "model":
            await self._cmd_model(chat_id, arg)
        elif clean == "effort":
            await self._cmd_effort(chat_id, arg)
        elif clean in ("permissions", "perm", "perms"):
            await self._cmd_permissions(chat_id, arg)
        elif clean == "pause":
            await self._cmd_pause(chat_id, arg)
        elif clean == "resume":
            await self._cmd_resume(chat_id, arg)
        elif clean == "stop":
            await self._cmd_stop(chat_id, arg)
        elif clean == "steer":
            await self._cmd_steer(chat_id, arg, message_id)
        elif clean == "output":
            await self._cmd_output(chat_id, arg)
        elif clean in ("fix_data", "fix-data"):
            await self._cmd_fix_data(chat_id)
        elif clean == "sys":
            await self._cmd_sys(chat_id)
        elif clean == "upmodel":
            await self._cmd_upmodel(chat_id, arg)
        elif clean == "cancel":
            self._pending_comment_target.pop(chat_id, None)
            await self._safe_send(chat_id, "✅ Cleared any pending comment input.", reply_to_message_id=message_id)
        elif clean == "restart":
            await self._cmd_restart(chat_id)
        else:
            await self._safe_send(
                chat_id,
                f"❓ Unknown command <code>/{escape_html(clean)}</code>. Send /help to see all commands.",
                reply_to_message_id=message_id,
            )

    async def _cmd_use(
        self,
        chat_id: int,
        arg: str = "",
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Switch the active project for natural-language text and voice messages."""
        raw = arg.strip()
        if not raw:
            cur = self._get_active_project(chat_id)
            notice = f"📌 Current active project: <code>{escape_html(cur)}</code>" if cur else "📌 Select an active project:"
            await self._send_project_picker(chat_id, reply_to_message_id=reply_to_message_id, notice=notice)
            return
        token = raw.split()[0].upper()
        if token in ("NONE", "CLEAR", "OFF"):
            self._set_active_project(chat_id, None)
            await self._safe_send(
                chat_id,
                "✅ Cleared active project selection for this chat.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        if load_task(token, f"{token}-0") is None:
            await self._send_project_picker(
                chat_id,
                reply_to_message_id=reply_to_message_id,
                notice=f"⚠️ Project <code>{escape_html(token)}</code> was not found.",
            )
            return
        await self._open_project_briefing(chat_id, token)

    async def _cmd_summary(
        self,
        chat_id: int,
        arg: str = "",
        reply_to_message_id: Optional[int] = None,
        edit_message_id: Optional[int] = None,
    ) -> None:
        """Generate a fresh read-only AI executive summary for the target or active project."""
        clean_pid = arg.strip().split()[0].upper() if arg.strip() else (self._get_active_project(chat_id) or "")
        if not clean_pid:
            await self._send_project_picker(
                chat_id,
                reply_to_message_id=reply_to_message_id,
                notice="📌 Select a project first to generate an AI summary.",
            )
            return
        if load_task(clean_pid, f"{clean_pid}-0") is None:
            await self._safe_send(
                chat_id,
                f"⚠️ Project <code>{escape_html(clean_pid)}</code> not found.",
                reply_to_message_id=reply_to_message_id,
            )
            return

        target_edit_id = edit_message_id
        if target_edit_id is None:
            wait_msg = await self._safe_send(
                chat_id,
                f"✨ Generating executive briefing for <code>{escape_html(clean_pid)}</code>...",
                reply_to_message_id=reply_to_message_id,
            )
            if isinstance(wait_msg, dict) and wait_msg.get("message_id"):
                target_edit_id = int(wait_msg["message_id"])

        briefing = await generate_ai_project_briefing(clean_pid, router=self.router)
        if not briefing:
            await self._safe_send(
                chat_id,
                f"⚠️ Failed to build briefing for <code>{escape_html(clean_pid)}</code>.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        briefing["running"] = clean_pid in self._running_pids()
        briefing["paused"] = self.state_manager.is_project_paused(clean_pid)
        self._set_active_project(chat_id, clean_pid)
        text, markup = build_project_briefing_card(briefing)
        sent = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=target_edit_id)
        self._record_card_target(chat_id, sent, clean_pid, f"{clean_pid}-0")

    async def _cmd_help(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        await self._send_or_edit(
            chat_id,
            build_help_text(),
            reply_markup=build_help_keyboard(),
            edit_message_id=edit_message_id,
        )

    async def _cmd_projects(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        projects = load_projects()
        text, markup = build_projects_keyboard(
            projects=projects,
            running_pids=self._running_pids(),
            paused_projects=self.state_manager.get_paused_projects(),
        )
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_status(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        text, markup = self._render_dashboard_payload()
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_harness(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        target = arg.strip().lower()
        if target:
            if not self.registry.has(target):
                await self._safe_send(
                    chat_id,
                    f"⚠️ Unknown harness <code>{escape_html(target)}</code>. "
                    f"Available: {', '.join(self.registry.list_names())}",
                )
                return
            self.state_manager.set_harness(target)

        health_map = await self.registry.check_all_health()
        st = self.state_manager.get_state()
        text, markup = build_harness_keyboard(health_map, st["default_harness"], st["default_model"])
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_model(
        self,
        chat_id: int,
        arg: str = "",
        edit_message_id: Optional[int] = None,
        force_refresh: bool = False,
    ) -> None:
        st = self.state_manager.get_state()
        cur_harness = st["default_harness"]
        if arg.strip():
            val = arg.strip()
            if val.isdigit():
                self.state_manager.set_model_index(int(val))
            else:
                self.state_manager.set_harness(cur_harness, val)
            st = self.state_manager.get_state()

        models = []
        if self.registry.has(st["default_harness"]):
            instance = self.registry.create(st["default_harness"])
            models = await instance.list_models(force_refresh=force_refresh)

        text, markup = build_model_keyboard(
            models=models,
            current_harness=st["default_harness"],
            current_model=st["default_model"],
            fallback_chain=st["fallback_chain"],
            model_index=st["model_index"],
        )
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_effort(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        if arg.strip():
            if not self.state_manager.set_effort(arg.strip()):
                await self._safe_send(chat_id, "⚠️ Invalid effort. Choose: low, medium, high, xhigh.")
                return
        cur = self.state_manager.get_state()["default_effort"]
        text, markup = build_effort_keyboard(cur)
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_permissions(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        if arg.strip():
            self.state_manager.set_permission_preset(arg.strip())
        cur = self.state_manager.get_state()["default_permission_preset"]
        text, markup = build_permissions_keyboard(cur)
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_pause(self, chat_id: int, arg: str = "") -> None:
        pid = arg.strip().upper()
        if pid:
            if not load_task(pid, f"{pid}-0"):
                await self._safe_send(chat_id, f"⚠️ Project <code>{escape_html(pid)}</code> not found.")
                return
            self.state_manager.pause_project(pid)
            await self._safe_send(chat_id, f"⏸️ Paused autonomous scheduling for <code>{escape_html(pid)}</code>.")
        else:
            self.state_manager.set_watcher_paused(True)
            await self._safe_send(chat_id, "⏸️ Global watcher scheduling paused. Use /resume to continue.")

    async def _cmd_resume(self, chat_id: int, arg: str = "") -> None:
        pid = arg.strip().upper()
        if pid:
            self.state_manager.resume_project(pid)
            await self._safe_send(chat_id, f"▶️ Resumed scheduling for <code>{escape_html(pid)}</code>.")
        else:
            self.state_manager.set_watcher_paused(False)
            await self._safe_send(chat_id, "▶️ Global watcher scheduling resumed.")

    async def _cmd_stop(self, chat_id: int, arg: str = "") -> None:
        if self.watcher is None:
            await self._safe_send(chat_id, "⚠️ Async watcher is not attached.")
            return

        pid = arg.strip().upper()
        running = self._running_pids()
        if not pid:
            if not running:
                await self._safe_send(chat_id, "ℹ️ No project worker is currently running.")
                return
            stopped = []
            for r_pid in running:
                if await self.watcher.stop_project(r_pid):
                    stopped.append(r_pid)
            await self._safe_send(chat_id, f"🛑 Stopped running worker(s): <code>{escape_html(', '.join(stopped))}</code>")
            return

        if await self.watcher.stop_project(pid):
            await self._safe_send(chat_id, f"🛑 Stop signal sent to worker <code>{escape_html(pid)}</code>.")
        else:
            await self._safe_send(chat_id, f"ℹ️ Project <code>{escape_html(pid)}</code> is not currently running.")

    async def _cmd_steer(self, chat_id: int, arg: str = "", message_id: Optional[int] = None) -> None:
        raw = arg.strip()
        if not raw:
            await self._safe_send(chat_id, "Usage: <code>/steer [PID] &lt;instruction&gt;</code>", reply_to_message_id=message_id)
            return

        running = self._running_pids()
        parts = raw.split(maxsplit=1)
        cand_pid = parts[0].upper()
        if len(parts) == 2 and load_task(cand_pid, f"{cand_pid}-0"):
            pid, msg_text = cand_pid, parts[1].strip()
        elif len(running) == 1:
            pid, msg_text = running[0], raw
        else:
            await self._safe_send(
                chat_id,
                "⚠️ Specify project ID when steering: <code>/steer &lt;PID&gt; &lt;instruction&gt;</code>",
                reply_to_message_id=message_id,
            )
            return

        if self.watcher is not None:
            ok, reason = await self.watcher.steer_project(pid, msg_text)
            if ok:
                await self._safe_send(
                    chat_id,
                    f"🎯 Steered guidance into active worker <code>{escape_html(pid)}</code>.",
                    reply_to_message_id=message_id,
                )
                return

        add_comment(pid, f"{pid}-0", "Admin (Steer)", msg_text)
        await self._safe_send(
            chat_id,
            f"📥 Queued steering note as Admin comment on <code>{escape_html(pid)}-0</code> for the next step.",
            reply_to_message_id=message_id,
        )

    async def _cmd_output(self, chat_id: int, arg: str = "") -> None:
        num_lines = 25
        if arg.strip():
            try:
                num_lines = max(1, min(200, int(arg.strip())))
            except ValueError:
                await self._safe_send(chat_id, "⚠️ Usage: /output [number_of_lines]")
                return

        log_path = os.path.join(get_data_dir(), "latest_worker_output.log")
        if not os.path.isfile(log_path):
            await self._safe_send(chat_id, "ℹ️ No worker output log found yet.")
            return

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-num_lines:]).strip()
        if not tail:
            await self._safe_send(chat_id, "ℹ️ Output log is empty.")
            return
        if len(tail) > 3500:
            tail = "..." + tail[-3500:]
        await self._safe_send(chat_id, f"<pre><code class=\"language-text\">{html.escape(tail)}</code></pre>")

    async def _cmd_sys(self, chat_id: int) -> None:
        current = self.state_manager.get_state()
        chain = self.state_manager.get_fallback_chain()
        models_list = "\n".join(
            f"{'✅' if i == current['model_index'] else '⬜'} [{i}] <code>{escape_html(entry['harness'])}</code> / "
            f"<code>{escape_html(entry['model'])}</code>"
            for i, entry in enumerate(chain)
        )
        text = (
            "⚙️ <b>System State</b>\n\n"
            f"⏳ <b>Started:</b> <code>{escape_html(str(current.get('start_time', 'Unknown')))}</code>\n"
            f"🔌 <b>Default Harness:</b> <code>{escape_html(current['default_harness'])}</code>\n"
            f"🤖 <b>Current Model:</b> <code>{escape_html(current['default_model'])}</code> (Index: {current['model_index']})\n"
            f"⚡ <b>Effort:</b> <code>{escape_html(current['default_effort'])}</code> | "
            f"<b>Preset:</b> <code>{escape_html(current['default_permission_preset'])}</code>\n"
            f"⏱️ <b>Watcher Interval:</b> <code>{current['interval_seconds']}s</code>\n\n"
            f"<b>Fallback Chain:</b>\n{models_list}"
        )
        await self._safe_send(chat_id, text)

    async def _cmd_upmodel(self, chat_id: int, arg: str = "") -> None:
        if not arg.strip():
            await self._safe_send(chat_id, "Usage: <code>/upmodel &lt;index&gt;</code>")
            return
        try:
            idx = int(arg.strip())
        except ValueError:
            await self._safe_send(chat_id, "⚠️ Index must be an integer.")
            return
        if self.state_manager.set_model_index(idx):
            await self._safe_send(
                chat_id,
                f"✅ Model updated to index {idx} (<code>{escape_html(self.state_manager.get_current_model())}</code>).",
            )
        else:
            await self._safe_send(
                chat_id,
                f"⚠️ Invalid index. Choose between 0 and {len(self.state_manager.get_all_models()) - 1}.",
            )

    async def _cmd_fix_data(self, chat_id: int) -> None:
        data_dir = get_data_dir()
        projects_dir = os.path.join(data_dir, "projects")
        if not os.path.isdir(projects_dir):
            await self._safe_send(chat_id, "⚠️ Data directory not found.")
            return

        errors: List[Tuple[str, str]] = []
        for pid in sorted(os.listdir(projects_dir)):
            proj_dir = os.path.join(projects_dir, pid)
            if not os.path.isdir(proj_dir):
                continue
            pjson = os.path.join(proj_dir, "project.json")
            if os.path.isfile(pjson):
                try:
                    with open(pjson, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as exc:
                    errors.append((pjson, str(exc)))
            tdir = os.path.join(proj_dir, "tasks")
            if os.path.isdir(tdir):
                for tid in sorted(os.listdir(tdir)):
                    tjson = os.path.join(tdir, tid, "task.json")
                    if os.path.isfile(tjson):
                        try:
                            with open(tjson, "r", encoding="utf-8") as f:
                                json.load(f)
                        except Exception as exc:
                            errors.append((tjson, str(exc)))

        if not errors:
            await self._safe_send(chat_id, "✅ All JSON files across all projects are valid.")
            return

        harness_name = self.state_manager.get_current_harness()
        model_name = self.state_manager.get_current_model()
        await self._safe_send(
            chat_id,
            f"🛠️ Found {len(errors)} JSON error(s). Invoking AI harness (<code>{escape_html(harness_name)}</code>) to repair...",
        )
        details = "\n".join(f"- {path}: {err}" for path, err in errors)
        prompt = (
            "Inspect and repair the following malformed TodoX JSON files:\n"
            f"{details}\n\n"
            "Ensure every file is valid JSON matching the TodoX 9-field schema."
        )
        profile = HarnessProfile(harness=harness_name, model=model_name)
        res = await self.router.run_step(RunRequest(prompt=prompt, working_dir=data_dir, model=model_name), profile=profile)
        if res.success:
            await self._safe_send(chat_id, f"✅ AI harness (<code>{escape_html(harness_name)}</code>) finished repairs.")
        else:
            await self._safe_send(
                chat_id,
                f"❌ AI harness repair failed: <code>{escape_html(res.error_message or res.status)}</code>",
            )

    async def _cmd_restart(self, chat_id: int) -> None:
        await self._safe_send(chat_id, "🔄 Restarting TodoX orchestrator...")
        if platform.system() == "Windows":
            subprocess.Popen(["cmd.exe", "/c", "restart.bat"])
        else:
            subprocess.Popen(["bash", "restart.sh"])


if __name__ == "__main__":
    assert hasattr(TelegramCommandsMixin, "dispatch_slash_command")
    print("telegram_ui.commands self-test passed!")
