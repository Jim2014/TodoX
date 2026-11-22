"""Telegram HTML formatting, Markdown conversion, tag balancing, and worker card rendering."""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Sequence

from harness.router import StepResult
from harness.types import CanonicalTool, HarnessEvent

MAX_TELEGRAM_MESSAGE_LEN = 4000

_TOOL_ICONS: Dict[CanonicalTool, str] = {
    CanonicalTool.SHELL: "⚙️",
    CanonicalTool.READ: "📄",
    CanonicalTool.WRITE: "📝",
    CanonicalTool.EDIT: "✏️",
    CanonicalTool.SEARCH: "🔍",
    CanonicalTool.WEB: "🌐",
    CanonicalTool.AGENT: "🤖",
    CanonicalTool.MCP: "🔌",
    CanonicalTool.QUESTION: "❓",
    CanonicalTool.OTHER: "🔧",
}


def escape_html(text: str) -> str:
    """Escape HTML special characters for safe Telegram HTML rendering."""
    if not text:
        return ""
    return html.escape(text, quote=False)


def balance_telegram_html(text: str) -> str:
    """Ensure all opened Telegram HTML tags are properly deduplicated and closed."""
    for tag in ("code", "b", "i", "s", "u", "blockquote", "pre"):
        text = re.sub(rf"<{tag}>(?:\s*<{tag}>)+", f"<{tag}>", text)
        text = re.sub(rf"</{tag}>(?:\s*</{tag}>)+", f"</{tag}>", text)
        text = re.sub(rf"<{tag}>\s*</{tag}>", "", text)

    valid_tags = {"b", "i", "s", "u", "code", "pre", "blockquote", "a"}
    opened: List[str] = []

    for match in re.finditer(r"<\s*(/)?\s*([a-zA-Z0-9]+)(?:\s+[^>]*)?>", text):
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        if tag_name not in valid_tags:
            continue
        if not is_closing:
            opened.append(tag_name)
        elif tag_name in opened:
            for idx in range(len(opened) - 1, -1, -1):
                if opened[idx] == tag_name:
                    opened.pop(idx)
                    break

    for tag_name in reversed(opened):
        text += f"</{tag_name}>"
    return text


def format_tokens(num: int, thinking: int = 0) -> str:
    """Format token counts cleanly (e.g. '45.2k (45,210)')."""
    safe_num = max(0, int(num))
    if safe_num >= 1_000_000:
        main_str = f"{safe_num / 1_000_000:.2f}M ({safe_num:,})"
    elif safe_num >= 1_000:
        main_str = f"{safe_num / 1_000:.1f}k ({safe_num:,})"
    else:
        main_str = f"{safe_num:,}"

    if thinking > 0:
        safe_th = int(thinking)
        if safe_th >= 1_000_000:
            th_str = f"{safe_th / 1_000_000:.2f}M"
        elif safe_th >= 1_000:
            th_str = f"{safe_th / 1_000:.1f}k"
        else:
            th_str = f"{safe_th:,}"
        return f"{main_str} [thinking: {th_str}]"

    return main_str


def format_duration(seconds: float) -> str:
    """Format elapsed seconds into concise human-readable duration."""
    sec = max(0.0, float(seconds))
    if sec < 60.0:
        return f"{sec:.1f}s"

    tot_sec = int(round(sec))
    hours = tot_sec // 3600
    minutes = (tot_sec % 3600) // 60
    rem_s = tot_sec % 60
    if hours > 0:
        return f"{hours}h {minutes}m {rem_s}s"
    return f"{minutes}m {rem_s}s"


def format_markdown_tables(text: str) -> str:
    """Convert Markdown tables into mobile-friendly bullet rows."""
    table_pattern = re.compile(r"((?:^\|[^\n]+\|(?:\r?\n|$)){2,})", re.MULTILINE)

    def _repl(match: re.Match) -> str:
        block = match.group(1).strip()
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2 or not re.match(r"^\|[\s\-:|]+\|$", lines[1]):
            return match.group(0)

        def _clean(cell: str) -> str:
            c = cell.strip()
            c = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", c)
            return c.replace("`", "")

        formatted: List[str] = []
        for raw_line in lines[2:]:
            cols = [_clean(c) for c in raw_line.strip("|").split("|")]
            if not any(cols):
                continue
            if len(cols) == 2:
                formatted.append(f"• **{cols[0].strip('*_ ')}**: {cols[1]}")
            elif len(cols) >= 3:
                title = cols[0].strip("*_ ")
                tags = " / ".join(c.strip("*_ ") for c in cols[1:-1] if c.strip())
                tag_str = f" ({tags})" if tags else ""
                formatted.append(f"• **{title}**{tag_str}: {cols[-1]}")
            else:
                formatted.append("• " + " | ".join(cols))

        return "\n" + "\n".join(formatted) + "\n\n"

    return table_pattern.sub(_repl, text)


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard GitHub-flavored Markdown into safe Telegram HTML."""
    if not text:
        return ""

    src = format_markdown_tables(text)
    if src.count("```") % 2 != 0:
        src += "\n```"

    code_blocks: List[str] = []

    def _save_code_block(match: re.Match) -> str:
        lang = (match.group(1) or "").strip().lower()
        code = html.escape(match.group(2).rstrip("\r\n"), quote=False)
        idx = len(code_blocks)
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>'
        else:
            tag = f"<pre><code>{code}</code></pre>"
        code_blocks.append(tag)
        return f"\x00CB{idx}\x00"

    src = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", _save_code_block, src, flags=re.DOTALL)

    inline_codes: List[str] = []

    def _save_inline_code(match: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        return f"\x00IC{idx}\x00"

    src = re.sub(r"`([^`\n]+)`", _save_inline_code, src)
    src = html.escape(src, quote=False)

    def _replace_link(match: re.Match) -> str:
        label = match.group(1).strip()
        url = match.group(2).strip()
        if url.startswith(("http://", "https://", "tg://")):
            return f'<a href="{url}">{label}</a>'
        if "\x00IC" in label or label.startswith("<code>"):
            return label
        return f"<code>{label}</code>"

    src = re.sub(r"\[([^\]]+)\]\(([a-zA-Z0-9_+.-]+://[^\s\)]+|/[^\s\)]+)\)", _replace_link, src)
    src = re.sub(r"^(#{1,6})\s+(.+)$", r"<b>\2</b>", src, flags=re.MULTILINE)
    src = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", src)
    src = re.sub(r"__(.+?)__", r"<b>\1</b>", src)
    src = re.sub(r"(?<!\w)\*([^\*\n]+?)\*(?!\w)", r"<i>\1</i>", src)
    src = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", src)
    src = re.sub(r"~~(.+?)~~", r"<s>\1</s>", src)

    def _replace_blockquote(match: re.Match) -> str:
        raw = match.group(0).rstrip("\r\n")
        lines = [re.sub(r"^&gt;\s?", "", line) for line in raw.split("\n")]
        return f"<blockquote>{chr(10).join(lines)}</blockquote>\n"

    src = re.sub(r"^(?:&gt;.*(?:\n|$))+", _replace_blockquote, src, flags=re.MULTILINE)
    src = re.sub(r"^(?:---|\*\*\*|___)\s*$", "———", src, flags=re.MULTILINE)
    src = re.sub(r"^[\*\-\+]\s+(.+)$", r"• \1", src, flags=re.MULTILINE)

    for idx, tag in enumerate(inline_codes):
        src = src.replace(f"\x00IC{idx}\x00", tag)
    for idx, tag in enumerate(code_blocks):
        src = src.replace(f"\x00CB{idx}\x00", tag)

    return balance_telegram_html(src)


def split_text_chunks(text: str, chunk_size: int = MAX_TELEGRAM_MESSAGE_LEN) -> List[str]:
    """Split long HTML text into balanced chunks strictly within Telegram size limits."""
    if len(text) <= chunk_size:
        return [balance_telegram_html(text)]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        split_idx = remaining.rfind("\n\n", 0, chunk_size)
        if split_idx == -1:
            split_idx = remaining.rfind("\n", 0, chunk_size)
        if split_idx == -1:
            split_idx = remaining.rfind(" ", 0, chunk_size)
        if split_idx == -1 or split_idx < chunk_size // 2:
            split_idx = chunk_size

        chunk = remaining[:split_idx].strip()
        if chunk:
            chunks.append(balance_telegram_html(chunk))
        remaining = remaining[split_idx:].strip()

    if remaining:
        chunks.append(balance_telegram_html(remaining))
    return chunks


def format_harness_tool_event(event: HarnessEvent) -> str:
    """Format a normalized tool event with canonical icon, state, summary, and duration."""
    canon = event.canonical_tool or CanonicalTool.OTHER
    icon = _TOOL_ICONS.get(canon, "🔧")
    label = canon.value if canon != CanonicalTool.OTHER else (event.raw_tool_name or "tool")
    summary = ""
    if isinstance(event.tool_info, dict):
        summary = str(event.tool_info.get("summary") or "").strip()
    if len(summary) > 65:
        summary = summary[:62] + "..."

    detail = f": <code>{escape_html(summary)}</code>" if summary else ""
    dur = (
        f" <i>({event.duration_seconds:.1f}s)</i>"
        if event.duration_seconds is not None and event.duration_seconds > 0
        else ""
    )
    state = (event.tool_state or "ACTIVE").upper()

    if state == "ACTIVE":
        return f"{icon} <i>{escape_html(label)}</i>{detail}..."
    if state == "DONE":
        return f"✅ {icon} <i>{escape_html(label)}</i>{detail}{dur}"
    if state == "ERROR":
        return f"⚠️ {icon} <i>{escape_html(label)} failed</i>{detail}{dur}"
    return f"{icon} <i>{escape_html(label)}</i> [{escape_html(state)}]{detail}{dur}"


def format_worker_live_card(
    pid: str,
    tid: str,
    harness: str,
    model: Optional[str],
    preset: str,
    tool_events: Sequence[str],
    current_text: str = "",
    elapsed_seconds: float = 0.0,
    status_label: str = "Running...",
) -> str:
    """Build a live-updating worker progress card using expandable blockquotes."""
    model_str = model or "default"
    header = (
        f"🤖 <b>{escape_html(pid)} / {escape_html(tid)}</b> • "
        f"<code>{escape_html(harness)}</code> (<code>{escape_html(model_str)}</code> • "
        f"<code>{escape_html(preset)}</code>)\n"
        f"⚡ <i>{escape_html(status_label)}</i> • ⏱ {format_duration(elapsed_seconds)}"
    )

    sections = [header]
    if tool_events:
        max_tools = 10
        if len(tool_events) > max_tools:
            omitted = len(tool_events) - max_tools
            shown = [f"<i>... ({omitted} earlier steps omitted)</i>"] + list(tool_events[-max_tools:])
        else:
            shown = list(tool_events)
        tools_block = "\n".join(shown)
        sections.append(
            f"<blockquote expandable><b>[Progress — {len(tool_events)} step(s)]</b>\n"
            f"{tools_block}</blockquote>"
        )

    body = markdown_to_telegram_html(current_text.strip()) if current_text.strip() else "<i>(executing step...)</i>"
    cursor = " ▌" if current_text.strip() else ""
    sections.append(body + cursor)

    full = "\n\n".join(sections)
    if len(full) <= MAX_TELEGRAM_MESSAGE_LEN:
        return balance_telegram_html(full)

    # Trim streaming body tail while preserving header + recent tool trace.
    recent_header = "\n\n".join(sections[:-1])
    avail = max(120, MAX_TELEGRAM_MESSAGE_LEN - len(recent_header) - 40)
    tail_raw = current_text.strip()[-avail:]
    tail_html = markdown_to_telegram_html(tail_raw)
    return balance_telegram_html(f"{recent_header}\n\n[...]\n{tail_html}{cursor}")


def format_worker_final_card(
    pid: str,
    tid: str,
    result: StepResult,
    task_status: str,
    task_assignee: str,
    latest_comment: str = "",
) -> str:
    """Format the final completion or failure card for a worker step."""
    if result.success:
        badge = "✅ <b>Worker Step Completed</b>"
    elif result.fallback_switched:
        badge = "⚠️ <b>Rate Limit (429) — Switched Fallback</b>"
    elif result.backoff_triggered:
        badge = "⏳ <b>Rate Limit (429) — Backing Off</b>"
    else:
        badge = f"🚨 <b>Worker Step Failed ({escape_html(result.error_category.value)})</b>"

    model_str = result.model or "default"
    meta_line = (
        f"📦 <b>{escape_html(pid)} / {escape_html(tid)}</b>\n"
        f"• <b>Status:</b> <code>{escape_html(task_status)}</code> | "
        f"<b>Assignee:</b> <code>{escape_html(task_assignee)}</code>\n"
        f"• <b>Harness:</b> <code>{escape_html(result.harness)}</code> "
        f"(<code>{escape_html(model_str)}</code>)"
    )

    sections = [f"{badge}\n\n{meta_line}"]

    if result.tool_events:
        shown = list(result.tool_events[-8:])
        if len(result.tool_events) > 8:
            shown.insert(0, f"<i>... ({len(result.tool_events) - 8} earlier steps omitted)</i>")
        sections.append(
            f"<blockquote expandable><b>[Executed Tools — {len(result.tool_events)}]</b>\n"
            + "\n".join(shown)
            + "</blockquote>"
        )

    summary_src = (latest_comment or result.response or result.error_message or "").strip()
    if summary_src:
        if len(summary_src) > 1500:
            summary_src = summary_src[:1497] + "..."
        sections.append(f"<b>Summary:</b>\n{markdown_to_telegram_html(summary_src)}")

    tok_total = result.usage.total_tokens if result.usage else 0
    tok_think = result.usage.thinking_tokens if result.usage else 0
    footer = f"⏱ {format_duration(result.duration_seconds)}"
    if tok_total > 0:
        footer += f" | 🪙 {format_tokens(tok_total, tok_think)} tokens"
    sections.append(f"———\n{footer}")

    return balance_telegram_html("\n\n".join(sections))


def format_pinned_dashboard(
    state_dict: Dict[str, Any],
    running_pids: Sequence[str],
    paused_global: bool,
    paused_projects: Sequence[str],
    project_count: int,
) -> str:
    """Format the pinned top-of-chat TodoX orchestrator dashboard."""
    harness = state_dict.get("default_harness", "agy")
    model = state_dict.get("default_model", "default")
    effort = state_dict.get("default_effort", "high")
    preset = state_dict.get("default_permission_preset", "auto")
    interval = state_dict.get("interval_seconds", 5)

    if paused_global:
        watcher_state = "⏸️ <b>Paused</b>"
    elif running_pids:
        watcher_state = f"⚡ <b>Running ({escape_html(', '.join(running_pids))})</b>"
    else:
        watcher_state = "🟢 <b>Watching</b>"

    paused_str = ", ".join(paused_projects) if paused_projects else "none"
    return (
        "📌 <b>TodoX Orchestrator Dashboard</b>\n\n"
        f"• <b>Watcher:</b> {watcher_state} (every <code>{interval}s</code>)\n"
        f"• <b>Harness:</b> <code>{escape_html(harness)}</code> | <b>Model:</b> <code>{escape_html(model)}</code>\n"
        f"• <b>Effort:</b> <code>{escape_html(effort)}</code> | <b>Permissions:</b> <code>{escape_html(preset)}</code>\n"
        f"• <b>Projects:</b> <code>{project_count}</code> | <b>Paused:</b> <code>{escape_html(paused_str)}</code>\n\n"
        "💡 <i>Tap a quick control below or send /help.</i>"
    )


if __name__ == "__main__":
    ev = HarnessEvent(
        type="tool",
        harness="agy",
        canonical_tool=CanonicalTool.SHELL,
        tool_state="DONE",
        duration_seconds=1.4,
        tool_info={"summary": "pytest -v"},
    )
    tool_line = format_harness_tool_event(ev)
    assert "pytest -v" in tool_line
    live = format_worker_live_card("ALPHA", "ALPHA-1", "agy", "gemini-3.8-flash", "auto", [tool_line], "Working **now**")
    assert "<blockquote expandable>" in live
    assert "<b>now</b>" in live
    md_html = markdown_to_telegram_html("# Heading\n- item `x`")
    assert "<b>Heading</b>" in md_html and "<code>x</code>" in md_html
    print("telegram_ui.formatter self-test passed!")
