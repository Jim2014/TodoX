"""Interactive Telegram inline keyboards and card builders for TodoX."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.types import HarnessProfile, HealthStatus, ModelInfo, PermissionPreset
from orchestrator.models import ProjectSummary, Task

from .formatter import escape_html, markdown_to_telegram_html

BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "help", "description": "Show interactive command dashboard"},
    {"command": "projects", "description": "List and manage TodoX projects"},
    {"command": "use", "description": "Switch active project and show briefing"},
    {"command": "summary", "description": "Generate fresh AI summary for active project"},
    {"command": "status", "description": "Show watcher, harness, and project status"},
    {"command": "harness", "description": "Switch active AI CLI harness"},
    {"command": "model", "description": "Select model or fallback index"},
    {"command": "effort", "description": "Configure reasoning effort level"},
    {"command": "permissions", "description": "Configure sandbox permission preset"},
    {"command": "pause", "description": "Pause watcher globally or for a project"},
    {"command": "resume", "description": "Resume watcher globally or for a project"},
    {"command": "stop", "description": "Stop a running project worker"},
    {"command": "steer", "description": "Guide a running worker or append comment"},
    {"command": "output", "description": "View recent worker log lines"},
    {"command": "fix_data", "description": "Validate and repair project JSON files"},
    {"command": "pin", "description": "Pin live orchestrator dashboard"},
    {"command": "restart", "description": "Restart TodoX orchestrator service"},
]

_STATUS_EMOJI: Dict[str, str] = {
    "not_started": "⬜",
    "in_progress": "⚡",
    "blocked": "🚧",
    "need_confirm": "🟡",
    "complete": "✅",
    "failed": "🚨",
}


def _short_cb(prefix: str, value: str, max_bytes: int = 64) -> str:
    """Build callback_data guaranteed to fit within Telegram's 64-byte limit."""
    raw = f"{prefix}{value}"
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return raw
    budget = max(1, max_bytes - len(prefix.encode("utf-8")))
    return prefix + encoded[:budget].decode("utf-8", errors="ignore")


def build_help_text() -> str:
    """Build the English master command reference."""
    return (
        "🤖 <b>TodoX Orchestrator Command Center</b>\n\n"
        "Manage autonomous project trees, switch AI CLI harnesses, and inspect live execution progress.\n\n"
        "<b>Interactive Commands:</b>\n"
        "• <code>/projects</code> — Browse, open, run, or pause projects\n"
        "• <code>/use [PID]</code> — Switch active project &amp; enable direct voice/text chat\n"
        "• <code>/summary [PID]</code> — Generate fresh AI executive summary\n"
        "• <code>/status</code> — Inspect watcher state and running workers\n"
        "• <code>/harness</code> — Switch active AI CLI harness (<code>agy</code>, etc.)\n"
        "• <code>/model</code> — Select model or fallback chain entry\n"
        "• <code>/effort</code> — Adjust reasoning effort (<code>low|medium|high|xhigh</code>)\n"
        "• <code>/permissions</code> — Set sandbox preset (<code>auto|accept-edits|plan|turbo</code>)\n"
        "• <code>/pause [PID]</code> / <code>/resume [PID]</code> — Pause or resume scheduling\n"
        "• <code>/stop [PID]</code> — Terminate an active worker subprocess\n"
        "• <code>/steer [PID] &lt;msg&gt;</code> — Steer active worker or queue Admin comment\n"
        "• <code>/output [lines]</code> — View tail of worker execution log\n"
        "• <code>/fix_data</code> — Scan and repair malformed JSON files\n"
        "• <code>/pin</code> — Pin live status dashboard to top of chat\n\n"
        "<b>Batch VOP Commands:</b>\n"
        "• <code>/new-project -pid ID -title \"...\" -des \"...\"</code>\n"
        "• <code>/new-task -pid ID -ptid ID-0 -title \"...\" -des \"...\"</code>\n"
        "• <code>/new-comment -pid ID -tid ID-1 -msg \"...\"</code>\n"
        "• <code>/get-project -pid ID</code> · <code>/get-file -pid ID -path \"...\"</code>\n\n"
        "💡 <i>Tap a button below or reply directly to any Project/Task card to add a comment.</i>"
    )


def build_help_keyboard() -> Dict[str, Any]:
    """Build the master inline keyboard for /help."""
    return {
        "inline_keyboard": [
            [
                {"text": "📁 Projects", "callback_data": "projects:list"},
                {"text": "📊 Status", "callback_data": "status:show"},
            ],
            [
                {"text": "🔌 Harness", "callback_data": "harness:show"},
                {"text": "🧠 Model", "callback_data": "model:show"},
            ],
            [
                {"text": "⚡ Effort", "callback_data": "effort:show"},
                {"text": "🛡️ Permissions", "callback_data": "perm:show"},
            ],
            [
                {"text": "🛠️ Fix Data", "callback_data": "fixdata:run"},
                {"text": "⏸️ Pause Watcher", "callback_data": "watcher:pause"},
            ],
        ]
    }


def build_projects_keyboard(
    projects: Dict[str, ProjectSummary],
    running_pids: Sequence[str],
    paused_projects: Sequence[str],
) -> Tuple[str, Dict[str, Any]]:
    """Build interactive project directory list."""
    if not projects:
        text = (
            "📁 <b>No TodoX projects found yet.</b>\n\n"
            "Create one with:\n"
            "<code>/new-project -pid ALPHA -title \"My Project\" -des \"Goal...\"</code>"
        )
        return text, {
            "inline_keyboard": [
                [{"text": "🔄 Refresh", "callback_data": "projects:list"}, {"text": "📖 Help", "callback_data": "help:show"}]
            ]
        }

    lines = [f"📁 <b>TodoX Projects ({len(projects)})</b>\n"]
    buttons: List[List[Dict[str, str]]] = []
    row: List[Dict[str, str]] = []
    running_set = set(running_pids)
    paused_set = set(paused_projects)

    for pid, proj in sorted(projects.items()):
        icon = "🏃" if pid in running_set else ("⏸️" if pid in paused_set else _STATUS_EMOJI.get(proj.status, "⬜"))
        flags = []
        if pid in running_set:
            flags.append("running")
        if pid in paused_set:
            flags.append("paused")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"{icon} <b>{escape_html(pid)}</b> — {escape_html(proj.title)}\n"
            f"   <code>{escape_html(proj.status)}</code> | Assignee: <code>{escape_html(proj.assignee)}</code>{flag_str}"
        )
        row.append({"text": f"{icon} {pid}", "callback_data": _short_cb("proj:open:", pid)})
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append(
        [
            {"text": "🔄 Refresh", "callback_data": "projects:list"},
            {"text": "📊 Status", "callback_data": "status:show"},
            {"text": "📖 Help", "callback_data": "help:show"},
        ]
    )
    lines.append("\n💡 <i>Tap a project below to inspect tasks, files, or hand off to AI.</i>")
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_project_card(
    pid: str,
    root_task: Task,
    subtasks: Sequence[Task],
    is_running: bool,
    is_paused: bool,
    effective_profile: HarnessProfile,
) -> Tuple[str, Dict[str, Any]]:
    """Build detailed project control card with quick action buttons."""
    status_icon = "🏃" if is_running else ("⏸️" if is_paused else _STATUS_EMOJI.get(root_task.status, "⬜"))
    desc_preview = (root_task.descriptions or "").strip()
    if len(desc_preview) > 350:
        desc_preview = desc_preview[:347] + "..."

    counts: Dict[str, int] = {}
    for t in subtasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    summary_parts = [f"{_STATUS_EMOJI.get(st, '•')} {st}: {cnt}" for st, cnt in sorted(counts.items())]
    subtask_summary = " | ".join(summary_parts) if summary_parts else "No subtasks yet"

    lines = [
        f"{status_icon} <b>Project {escape_html(pid)}</b> — {escape_html(root_task.title)}",
        f"• <b>Status:</b> <code>{escape_html(root_task.status)}</code> | <b>Assignee:</b> <code>{escape_html(root_task.assignee)}</code>",
        f"• <b>Harness:</b> <code>{escape_html(effective_profile.harness)}</code> (<code>{escape_html(effective_profile.model or 'default')}</code> • <code>{escape_html(effective_profile.permission_preset.value)}</code>)",
        f"• <b>Subtasks ({len(subtasks)}):</b> {subtask_summary}",
    ]
    if is_paused:
        lines.append("• <b>Schedule:</b> ⏸️ Paused for this project")
    if desc_preview:
        lines.append(f"\n<b>Description:</b>\n{escape_html(desc_preview)}")
    if root_task.comments:
        last_c = root_task.comments[-1]
        c_prev = last_c.content.strip()
        if len(c_prev) > 220:
            c_prev = c_prev[:217] + "..."
        lines.append(f"\n<b>Latest Comment ({escape_html(last_c.author)}):</b>\n{escape_html(c_prev)}")

    lines.append("\n💬 <i>Reply directly to this card to add an Admin comment to the project.</i>")

    row1 = [
        {"text": f"📋 Tasks ({len(subtasks)})", "callback_data": _short_cb("proj:tasks:", pid)},
        {"text": "📁 Files", "callback_data": _short_cb("proj:files:", f"{pid}:0")},
    ]
    row2 = []
    if is_running:
        row2.append({"text": "🛑 Stop Worker", "callback_data": _short_cb("worker:stop:", pid)})
    else:
        row2.append({"text": "🤖 Assign & Run AI", "callback_data": _short_cb("proj:run:", pid)})
    if is_paused:
        row2.append({"text": "▶️ Resume", "callback_data": _short_cb("proj:resume:", pid)})
    else:
        row2.append({"text": "⏸️ Pause", "callback_data": _short_cb("proj:pause:", pid)})

    row3 = [
        {"text": "💬 Add Comment", "callback_data": _short_cb("comment:prompt:", f"{pid}:{pid}-0")},
        {"text": "📄 Export HTML", "callback_data": _short_cb("proj:html:", pid)},
    ]
    row4 = [
        {"text": "🔄 Refresh", "callback_data": _short_cb("proj:open:", pid)},
        {"text": "⬅️ All Projects", "callback_data": "projects:list"},
    ]

    return "\n".join(lines), {"inline_keyboard": [row1, row2, row3, row4]}


def build_tasks_keyboard(pid: str, subtasks: Sequence[Task]) -> Tuple[str, Dict[str, Any]]:
    """Build interactive task selector list for a project."""
    lines = [f"📋 <b>Tasks for {escape_html(pid)} ({len(subtasks)})</b>\n"]
    buttons: List[List[Dict[str, str]]] = []

    if not subtasks:
        lines.append("<i>No subtasks created yet.</i>")
    else:
        row: List[Dict[str, str]] = []
        for t in subtasks[:20]:
            icon = _STATUS_EMOJI.get(t.status, "⬜")
            title_short = t.title[:36] + ("..." if len(t.title) > 36 else "")
            lines.append(
                f"{icon} <b>{escape_html(t.id)}</b>: {escape_html(title_short)} "
                f"(<code>{escape_html(t.status)}</code> • <code>{escape_html(t.assignee)}</code>)"
            )
            row.append({"text": f"{icon} {t.id}", "callback_data": _short_cb("task:open:", f"{pid}:{t.id}")})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    buttons.append(
        [
            {"text": f"⬅️ Back to {pid}", "callback_data": _short_cb("proj:open:", pid)},
            {"text": "🔄 Refresh", "callback_data": _short_cb("proj:tasks:", pid)},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_task_card(
    pid: str,
    task: Task,
    effective_profile: HarnessProfile,
) -> Tuple[str, Dict[str, Any]]:
    """Build detailed task card with status/assignee hand-off controls."""
    icon = _STATUS_EMOJI.get(task.status, "⬜")
    desc = (task.descriptions or "").strip()
    if len(desc) > 500:
        desc = desc[:497] + "..."

    lines = [
        f"{icon} <b>Task {escape_html(task.id)}</b> — {escape_html(task.title)}",
        f"• <b>Project:</b> <code>{escape_html(pid)}</code> | <b>Parent:</b> <code>{escape_html(task.parent_id or 'none')}</code>",
        f"• <b>Status:</b> <code>{escape_html(task.status)}</code> | <b>Assignee:</b> <code>{escape_html(task.assignee)}</code>",
        f"• <b>Harness:</b> <code>{escape_html(effective_profile.harness)}</code> (<code>{escape_html(effective_profile.model or 'default')}</code>)",
    ]
    if task.execution_meta and task.execution_meta.total_steps > 0:
        lines.append(
            f"• <b>Telemetry:</b> {task.execution_meta.total_steps} step(s) | "
            f"{task.execution_meta.total_tokens:,} tokens"
        )
    if desc:
        lines.append(f"\n<b>Description:</b>\n{escape_html(desc)}")

    if task.comments:
        lines.append("\n<b>Recent Comments:</b>")
        for c in task.comments[-3:]:
            prev = c.content.strip()
            if len(prev) > 220:
                prev = prev[:217] + "..."
            lines.append(f"• <b>{escape_html(c.author)}</b>: {escape_html(prev)}")

    lines.append("\n💬 <i>Reply directly to this card to add an Admin comment to this task.</i>")

    buttons = [
        [
            {"text": "🤖 Assign AI", "callback_data": _short_cb("task:ai:", f"{pid}:{task.id}")},
            {"text": "👤 Assign Admin", "callback_data": _short_cb("task:admin:", f"{pid}:{task.id}")},
            {"text": "✅ Mark Complete", "callback_data": _short_cb("task:done:", f"{pid}:{task.id}")},
        ],
        [
            {"text": "💬 Add Comment", "callback_data": _short_cb("comment:prompt:", f"{pid}:{task.id}")},
            {"text": "🔄 Refresh", "callback_data": _short_cb("task:open:", f"{pid}:{task.id}")},
        ],
        [
            {"text": "📋 All Tasks", "callback_data": _short_cb("proj:tasks:", pid)},
            {"text": f"⬅️ {pid}", "callback_data": _short_cb("proj:open:", pid)},
        ],
    ]
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_files_keyboard(
    pid: str,
    rel_files: Sequence[str],
    page: int = 0,
    page_size: int = 8,
) -> Tuple[str, Dict[str, Any]]:
    """Build paginated workspace file browser keyboard."""
    total = len(rel_files)
    total_pages = max(1, (total + page_size - 1) // page_size)
    cur_page = max(0, min(page, total_pages - 1))
    start = cur_page * page_size
    end = min(total, start + page_size)

    lines = [f"📁 <b>Files in {escape_html(pid)}</b> (Page {cur_page + 1}/{total_pages}, {total} total)\n"]
    buttons: List[List[Dict[str, str]]] = []

    if total == 0:
        lines.append("<i>No files found in this project workspace.</i>")
    else:
        for idx in range(start, end):
            rel = rel_files[idx]
            lines.append(f"• <code>{escape_html(rel)}</code>")
            short_label = rel if len(rel) <= 32 else "..." + rel[-29:]
            buttons.append([{"text": f"📄 {short_label}", "callback_data": _short_cb("file:get:", f"{pid}:{idx}")}])

    nav_row: List[Dict[str, str]] = []
    if cur_page > 0:
        nav_row.append({"text": "◀️ Prev", "callback_data": _short_cb("proj:files:", f"{pid}:{cur_page - 1}")})
    if cur_page + 1 < total_pages:
        nav_row.append({"text": "Next ▶️", "callback_data": _short_cb("proj:files:", f"{pid}:{cur_page + 1}")})
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [
            {"text": f"⬅️ Back to {pid}", "callback_data": _short_cb("proj:open:", pid)},
            {"text": "🔄 Refresh", "callback_data": _short_cb("proj:files:", f"{pid}:{cur_page}")},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_harness_keyboard(
    health_map: Dict[str, HealthStatus],
    current_harness: str,
    current_model: str,
) -> Tuple[str, Dict[str, Any]]:
    """Build interactive harness selection menu with live health status."""
    lines = [
        "🔌 <b>AI Agent Harness Configuration</b>\n",
        f"• <b>Active Harness:</b> <code>{escape_html(current_harness)}</code>",
        f"• <b>Active Model:</b> <code>{escape_html(current_model)}</code>\n",
        "<b>Registered Harnesses:</b>",
    ]
    buttons: List[List[Dict[str, str]]] = []

    for name, st in sorted(health_map.items()):
        is_active = name == current_harness
        avail_icon = "🟢" if st.available else "🔴"
        active_tag = " 🌟 <b>[ACTIVE]</b>" if is_active else ""
        ver = f" v{escape_html(st.version)}" if st.version else ""
        lines.append(f"• {avail_icon} <b>{escape_html(name)}</b>{ver}{active_tag}\n   <i>{escape_html(st.message)}</i>")
        btn_text = f"✓ {name}" if is_active else f"{avail_icon} Switch to {name}"
        buttons.append([{"text": btn_text, "callback_data": _short_cb("harness:set:", name)}])

    buttons.append(
        [
            {"text": "🧠 Switch Model", "callback_data": "model:show"},
            {"text": "🔄 Probe Health", "callback_data": "harness:refresh"},
            {"text": "📊 Status", "callback_data": "status:show"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_model_keyboard(
    models: Sequence[ModelInfo],
    current_harness: str,
    current_model: str,
    fallback_chain: Sequence[Dict[str, str]],
    model_index: int,
) -> Tuple[str, Dict[str, Any]]:
    """Build model & fallback chain selector keyboard."""
    lines = [
        "🧠 <b>Model &amp; Fallback Configuration</b>\n",
        f"• <b>Active Harness:</b> <code>{escape_html(current_harness)}</code>",
        f"• <b>Active Model:</b> <code>{escape_html(current_model)}</code> (Fallback Index: {model_index})\n",
    ]
    buttons: List[List[Dict[str, str]]] = []

    if models:
        lines.append("<b>Discovered Models:</b>")
        for m in models[:10]:
            is_active = current_model == m.id or current_model == m.raw_id
            marker = "🌟 " if is_active else "• "
            lines.append(f"{marker}{m.icon} <b>{escape_html(m.name)}</b> (<code>{escape_html(m.id)}</code>)")
            btn_label = f"✓ {m.name}" if is_active else f"{m.icon} {m.name}"
            buttons.append([{"text": btn_label[:36], "callback_data": _short_cb("model:id:", m.id)}])

    if fallback_chain:
        lines.append("\n<b>Fallback Chain:</b>")
        chain_row: List[Dict[str, str]] = []
        for idx, entry in enumerate(fallback_chain[:6]):
            is_cur = idx == model_index
            lines.append(
                f"{'✅' if is_cur else '⬜'} [{idx}] <code>{escape_html(entry['harness'])}</code> / "
                f"<code>{escape_html(entry['model'])}</code>"
            )
            chain_row.append({"text": f"{'✓ ' if is_cur else ''}[{idx}] {entry['model'][:14]}", "callback_data": f"model:idx:{idx}"})
            if len(chain_row) == 2:
                buttons.append(chain_row)
                chain_row = []
        if chain_row:
            buttons.append(chain_row)

    buttons.append(
        [
            {"text": "⚡ Effort", "callback_data": "effort:show"},
            {"text": "🔄 Refresh", "callback_data": "model:refresh"},
            {"text": "📊 Status", "callback_data": "status:show"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_effort_keyboard(current_effort: str) -> Tuple[str, Dict[str, Any]]:
    """Build reasoning effort selector."""
    levels = [
        ("low", "🟢 Low", "Minimal reasoning latency"),
        ("medium", "🟡 Medium", "Balanced speed and depth"),
        ("high", "🔴 High", "Deep multi-step reasoning (Recommended)"),
        ("xhigh", "🟣 XHigh", "Maximum reasoning budget when supported"),
    ]
    lines = [
        "⚡ <b>Reasoning Effort Setting</b>\n",
        f"• <b>Current Effort:</b> <code>{escape_html(current_effort)}</code>\n",
    ]
    row: List[Dict[str, str]] = []
    rows: List[List[Dict[str, str]]] = []
    for eid, label, desc in levels:
        active = current_effort == eid
        lines.append(f"{'🌟 ' if active else '• '}<b>{label}</b> (<code>{eid}</code>) — <i>{desc}</i>")
        row.append({"text": f"✓ {label}" if active else label, "callback_data": f"effort:set:{eid}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🧠 Model", "callback_data": "model:show"}, {"text": "📊 Status", "callback_data": "status:show"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_permissions_keyboard(current_preset: str) -> Tuple[str, Dict[str, Any]]:
    """Build sandbox permission preset selector."""
    presets = [
        (PermissionPreset.AUTO.value, "🛡️ Auto", "Native OS sandbox; safe workspace commands auto-approved"),
        (PermissionPreset.ACCEPT_EDITS.value, "✏️ Accept Edits", "Workspace edits auto-approved; unsandboxed shell restricted"),
        (PermissionPreset.PLAN.value, "📋 Plan", "Read-only analysis & planning"),
        (PermissionPreset.TURBO.value, "⚡ Turbo", "Unrestricted execution (--dangerously-skip-permissions)"),
    ]
    lines = [
        "🛡️ <b>Worker Permission Preset</b>\n",
        f"• <b>Active Preset:</b> <code>{escape_html(current_preset)}</code>\n",
    ]
    row: List[Dict[str, str]] = []
    rows: List[List[Dict[str, str]]] = []
    for pid_val, label, desc in presets:
        active = current_preset == pid_val
        lines.append(f"{'🌟 ' if active else '• '}<b>{label}</b> (<code>{pid_val}</code>) — <i>{desc}</i>")
        row.append({"text": f"✓ {label}" if active else label, "callback_data": f"perm:set:{pid_val}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🔌 Harness", "callback_data": "harness:show"}, {"text": "📊 Status", "callback_data": "status:show"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_worker_running_keyboard(pid: str) -> Dict[str, Any]:
    """Build live inline keyboard displayed under active worker progress cards."""
    return {
        "inline_keyboard": [
            [
                {"text": "🛑 Stop", "callback_data": _short_cb("worker:stop:", pid)},
                {"text": "📄 Live Log", "callback_data": _short_cb("worker:log:", pid)},
                {"text": "📂 Project", "callback_data": _short_cb("proj:open:", pid)},
            ]
        ]
    }


def build_worker_done_keyboard(
    pid: str,
    tid: str,
    task_status: str,
    has_error: bool = False,
) -> Dict[str, Any]:
    """Build contextual follow-up actions after a worker step completes or fails."""
    row1: List[Dict[str, str]] = []
    if task_status in ("need_confirm", "blocked"):
        row1.append({"text": "✅ Approve & Resume AI", "callback_data": _short_cb("task:ai:", f"{pid}:{tid}")})
    elif has_error or task_status == "failed":
        row1.append({"text": "🔄 Retry AI", "callback_data": _short_cb("task:ai:", f"{pid}:{tid}")})
        row1.append({"text": "🔌 Switch Harness", "callback_data": "harness:show"})
    else:
        row1.append({"text": "📋 View Task", "callback_data": _short_cb("task:open:", f"{pid}:{tid}")})

    row2 = [
        {"text": f"📂 Open {pid}", "callback_data": _short_cb("proj:open:", pid)},
        {"text": "📄 HTML Report", "callback_data": _short_cb("proj:html:", pid)},
    ]
    return {"inline_keyboard": [row1, row2]}


def build_pinned_banner_keyboard(paused_global: bool) -> Dict[str, Any]:
    """Build quick control buttons for the pinned dashboard banner."""
    pause_btn = (
        {"text": "▶️ Resume Watcher", "callback_data": "watcher:resume"}
        if paused_global
        else {"text": "⏸️ Pause Watcher", "callback_data": "watcher:pause"}
    )
    return {
        "inline_keyboard": [
            [
                {"text": "📁 Projects", "callback_data": "projects:list"},
                {"text": "📊 Status", "callback_data": "status:show"},
                {"text": "🔄 Refresh", "callback_data": "status:show"},
            ],
            [
                pause_btn,
                {"text": "📖 Help", "callback_data": "help:show"},
            ],
        ]
    }


def build_project_briefing_card(briefing: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Build concise active-project briefing card focused on summary, attention items, and voice/chat."""
    pid = str(briefing.get("pid") or "")
    title = str(briefing.get("title") or pid)
    is_running = bool(briefing.get("running"))
    is_paused = bool(briefing.get("paused"))
    status = str(briefing.get("status") or "not_started")
    total = int(briefing.get("total_tasks") or 0)
    done = int(briefing.get("completed_tasks") or 0)
    pct = int(briefing.get("progress_pct") or 0)
    runtime = briefing.get("runtime") or {}

    icon = "🏃" if is_running else ("⏸️" if is_paused else _STATUS_EMOJI.get(status, "⬜"))
    state_label = (
        f"Running {escape_html(str(runtime.get('tid') or pid))}"
        if is_running
        else ("Paused" if is_paused else escape_html(status))
    )

    lines = [
        f"{icon} <b>Project {escape_html(pid)}</b> — {escape_html(title)} 🌟 <i>[ACTIVE]</i>",
        f"• <b>Progress:</b> <code>{done}/{total} ({pct}%)</code> | <b>State:</b> <code>{state_label}</code>",
    ]

    attention = briefing.get("attention_tasks") or []
    if attention:
        lines.append("• <b>Needs Attention:</b>")
        for item in attention[:3]:
            st_icon = _STATUS_EMOJI.get(item.get("status", ""), "🟡")
            lines.append(
                f"  {st_icon} <code>{escape_html(item['id'])}</code>: {escape_html(item.get('title', '')[:48])}"
            )

    src_map = {
        "ai_refresh": "AI Executive Summary",
        "worker_step": "Latest Worker Step",
        "latest_ticket": "Latest Ticket Update",
        "project_goal": "Project Goal",
    }
    src_label = src_map.get(str(briefing.get("summary_source") or ""), "Summary")
    if briefing.get("is_ai_summary_stale"):
        src_label += " (newer ticket updates available)"

    summary_raw = str(briefing.get("summary_text") or "").strip()
    if len(summary_raw) > 1200:
        summary_raw = summary_raw[:1197] + "..."
    lines.append(f"\n📌 <b>{escape_html(src_label)}:</b>\n{markdown_to_telegram_html(summary_raw)}")
    lines.append("\n💬 <i>Active project set. Send text or a voice note anytime to chat with this workspace.</i>")

    buttons: List[List[Dict[str, str]]] = [
        [
            {"text": "✨ AI Summary", "callback_data": _short_cb("proj:summary:", pid)},
            {"text": "🔊 Voice Brief", "callback_data": _short_cb("proj:voice:", pid)},
        ]
    ]

    row2: List[Dict[str, str]] = []
    if attention:
        first_att = attention[0]
        if first_att.get("status") == "need_confirm":
            row2.append(
                {
                    "text": f"✅ Approve {first_att['id']}",
                    "callback_data": _short_cb("task:approve:", f"{pid}:{first_att['id']}"),
                }
            )
    if is_running:
        row2.append({"text": "🛑 Stop Worker", "callback_data": _short_cb("worker:stop:", pid)})
    else:
        row2.append({"text": "🤖 Run AI", "callback_data": _short_cb("proj:run:", pid)})
    if row2:
        buttons.append(row2)

    buttons.append(
        [
            {"text": f"📋 Tasks ({total})", "callback_data": _short_cb("proj:tasks:", pid)},
            {"text": "📁 Files", "callback_data": _short_cb("proj:files:", f"{pid}:0")},
            {"text": "⚙️ Controls", "callback_data": _short_cb("proj:card:", pid)},
        ]
    )
    buttons.append(
        [
            {"text": "🔄 Refresh", "callback_data": _short_cb("proj:open:", pid)},
            {"text": "⬅️ All Projects", "callback_data": "projects:list"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


if __name__ == "__main__":
    sample_profile = HarnessProfile(harness="agy", model="gemini-3.8-flash", effort="high")
    sample_task = Task(id="ALPHA-0", title="Alpha", descriptions="Test", status="in_progress", assignee="AI")
    _, proj_kb = build_project_card("ALPHA", sample_task, [], False, False, sample_profile)
    _, task_kb = build_task_card("ALPHA", sample_task, sample_profile)
    _, files_kb = build_files_keyboard("ALPHA", ["a.py", "b.md"], page=0)
    _, eff_kb = build_effort_keyboard("high")
    _, perm_kb = build_permissions_keyboard("auto")
    for kb in (
        build_help_keyboard(),
        proj_kb,
        task_kb,
        files_kb,
        eff_kb,
        perm_kb,
        build_worker_running_keyboard("ALPHA"),
        build_worker_done_keyboard("ALPHA", "ALPHA-1", "failed", has_error=True),
        build_pinned_banner_keyboard(False),
    ):
        for row in kb["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64, btn["callback_data"]
    print("telegram_ui.keyboards self-test passed!")
