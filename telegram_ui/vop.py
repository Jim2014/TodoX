"""Legacy VOP multi-line batch command parser and mixin handlers."""

from __future__ import annotations

from datetime import datetime
import shlex
from typing import Any, Dict, Optional, Sequence

from orchestrator.storage import (
    add_comment,
    create_project,
    create_task,
    delete_comment,
    delete_project,
    delete_task,
    get_default_worker_prompt,
    load_task,
    update_comment,
    update_project,
    update_task,
)

from .formatter import escape_html


def parse_vop_args(arg_list: Sequence[str]) -> Dict[str, Any]:
    """Parse POSIX-style '-key value' tokens used by legacy VOP commands."""
    parsed: Dict[str, Any] = {}
    idx = 0
    while idx < len(arg_list):
        token = arg_list[idx]
        if token.startswith("-"):
            key = token[1:]
            if idx + 1 < len(arg_list) and not arg_list[idx + 1].startswith("-"):
                parsed[key] = arg_list[idx + 1]
                idx += 2
            else:
                parsed[key] = True
                idx += 1
        else:
            idx += 1
    return parsed


class TelegramVopMixin:
    """Provides backwards-compatible VOP batch command parsing and CRUD operations."""

    def _is_vop_batch_text(self, text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith("/")]
        if not lines:
            return False
        first_cmd = lines[0].split(maxsplit=1)[0].split("@")[0].lower()
        return first_cmd in self._vop_handlers or len(lines) > 1

    async def _handle_vop_lines(self, chat_id: int, text: str, message_id: Optional[int]) -> None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("/"):
                continue
            try:
                tokens = shlex.split(line)
            except Exception as exc:
                await self._safe_send(chat_id, f"⚠️ Error parsing line <code>{escape_html(line)}</code>: {escape_html(str(exc))}")
                continue
            if not tokens:
                continue

            cmd = tokens[0].split("@")[0].lower()
            if cmd in self._vop_handlers:
                args = parse_vop_args(tokens[1:])
                await self._vop_handlers[cmd](chat_id, args)
            else:
                arg_str = line.partition(" ")[2].strip()
                await self.dispatch_slash_command(chat_id, cmd, arg_str, message_id)

    async def _vop_new_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        title = str(args.get("title", "New Project"))
        desc = str(args.get("des", ""))
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        if load_task(pid, f"{pid}-0"):
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} already exists.")
            return
        create_project(pid, title, desc, get_default_worker_prompt())
        await self._safe_send(chat_id, f"✅ Project <code>{escape_html(pid)}</code> created: {escape_html(title)}")

    async def _vop_new_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        ptid = str(args.get("ptid", "")).upper()
        title = str(args.get("title", "New Task"))
        desc = str(args.get("des", ""))
        assignee = str(args.get("assignee", "AI"))
        status = str(args.get("status", "not_started"))
        if not pid or not ptid:
            await self._safe_send(chat_id, "Error: -pid and -ptid are required.")
            return
        task = create_task(pid, ptid, title, desc, assignee, status)
        if task:
            await self._safe_send(chat_id, f"✅ Task <code>{escape_html(task.id)}</code> created under <code>{escape_html(ptid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Parent task {escape_html(ptid)} not found.")

    async def _vop_new_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        msg = str(args.get("msg", ""))
        if not pid or not tid or not msg:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -msg are required.")
            return
        if add_comment(pid, tid, "Admin", msg):
            await self._safe_send(chat_id, f"✅ Comment added to <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_update_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        field = str(args.get("field", "")).lower()
        val = args.get("val", "")
        if not pid or not field:
            await self._safe_send(chat_id, "Error: -pid and -field are required.")
            return
        if field == "des":
            field = "descriptions"
        if update_project(pid, **{field: val}):
            await self._safe_send(chat_id, f"✅ Project <code>{escape_html(pid)}</code> updated ({escape_html(field)}).")
        else:
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")

    async def _vop_update_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        field = str(args.get("field", "")).lower()
        val = args.get("val", "")
        if not pid or not tid or not field:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -field are required.")
            return
        if field == "des":
            field = "descriptions"
        if update_task(pid, tid, **{field: val}):
            await self._safe_send(chat_id, f"✅ Task <code>{escape_html(tid)}</code> updated ({escape_html(field)}).")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_update_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        cidx = args.get("cidx")
        val = str(args.get("val", ""))
        if not pid or not tid or cidx is None or not val:
            await self._safe_send(chat_id, "Error: -pid, -tid, -cidx, and -val are required.")
            return
        try:
            idx = int(cidx)
        except ValueError:
            await self._safe_send(chat_id, "Error: -cidx must be an integer.")
            return
        if update_comment(pid, tid, idx, val):
            await self._safe_send(chat_id, f"✅ Comment {idx} updated in <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} or comment index not found.")

    async def _vop_del_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        if delete_project(pid):
            await self._safe_send(chat_id, f"🗑️ Project <code>{escape_html(pid)}</code> deleted.")
        else:
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")

    async def _vop_del_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        if not pid or not tid:
            await self._safe_send(chat_id, "Error: -pid and -tid are required.")
            return
        if delete_task(pid, tid):
            await self._safe_send(chat_id, f"🗑️ Task <code>{escape_html(tid)}</code> deleted.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_del_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        cidx = args.get("cidx")
        if not pid or not tid or cidx is None:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -cidx are required.")
            return
        try:
            idx = int(cidx)
        except ValueError:
            await self._safe_send(chat_id, "Error: -cidx must be an integer.")
            return
        if delete_comment(pid, tid, idx):
            await self._safe_send(chat_id, f"🗑️ Comment {idx} deleted from <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} or comment index not found.")

    async def _vop_get_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        from orchestrator.html_generator import generate_project_html

        try:
            html_bytes = generate_project_html(pid)
            if html_bytes:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await self.client.send_document(
                    chat_id=chat_id,
                    document_content=html_bytes,
                    filename=f"project_{pid}_{ts}.html",
                )
            else:
                await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")
        except Exception as exc:
            await self._safe_send(chat_id, f"Error generating HTML: {escape_html(str(exc))}")

    async def _vop_get_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        if not pid or not tid:
            await self._safe_send(chat_id, "Error: -pid and -tid are required.")
            return
        await self._open_task_card(chat_id, pid, tid)

    async def _vop_get_file(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        rel_path = str(args.get("path", ""))
        if not pid or not rel_path:
            await self._safe_send(chat_id, "Error: -pid and -path are required.")
            return
        await self._send_project_file(chat_id, pid, rel_path)

    async def _vop_fix_data(self, chat_id: int, args: Dict[str, Any]) -> None:
        await self._cmd_fix_data(chat_id)


if __name__ == "__main__":
    assert parse_vop_args(["-pid", "DEMO", "-title", "Demo App"]) == {"pid": "DEMO", "title": "Demo App"}
    print("telegram_ui.vop self-test passed!")
