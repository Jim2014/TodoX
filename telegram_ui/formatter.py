"""Telegram HTML formatting, Markdown conversion, tag balancing, and worker card rendering."""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.router import StepResult
from harness.types import CanonicalTool, ErrorCategory, HarnessEvent

MAX_TELEGRAM_MESSAGE_LEN = 4000

_HTML_STRUCTURAL_TAG_RE = re.compile(
    r"</?(?:html|head|body|div|style|section|article|table|thead|tbody|tr|td|ul|ol|li)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_RAW_START_RE = re.compile(
    r"(?:<!doctype\s+html|<html\b|<head\b|<style\b|<div\b|<ul\b|<table\b)",
    re.IGNORECASE,
)
_AGY_FILE_BLOCK_RE = re.compile(
    r"(?:^|\r?\n)[ \t]*\[AGY_FILE\][ \t]*\r?\n((?:(?!\[AGY_FILE\]).)*?)\r?\n[ \t]*\[END_AGY_FILE\][ \t]*(?=\r?\n|$)",
    re.DOTALL | re.IGNORECASE,
)

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
    """Ensure all opened Telegram HTML tags are properly deduplicated, nested, and closed."""
    for tag in ("code", "b", "i", "s", "u", "blockquote", "pre"):
        text = re.sub(rf"<{tag}>(?:\s*<{tag}>)+", f"<{tag}>", text)
        text = re.sub(rf"</{tag}>(?:\s*</{tag}>)+", f"</{tag}>", text)
        text = re.sub(rf"<{tag}>\s*</{tag}>", "", text)

    valid_tags = {"b", "i", "s", "u", "code", "pre", "blockquote", "a"}
    stack: List[Tuple[str, str]] = []
    out: List[str] = []
    last_idx = 0

    for match in re.finditer(r"<\s*(/)?\s*([a-zA-Z0-9]+)(?:\s+[^>]*)?>", text):
        out.append(text[last_idx : match.start()])
        last_idx = match.end()
        raw_tag = match.group(0)
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()

        if tag_name not in valid_tags:
            out.append(raw_tag)
            continue

        if not is_closing:
            stack.append((tag_name, raw_tag))
            out.append(raw_tag)
        else:
            pos = -1
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag_name:
                    pos = idx
                    break
            if pos == -1:
                # Drop stray unmatched closing tag so Telegram doesn't reject entities.
                continue

            reopen: List[Tuple[str, str]] = []
            while len(stack) - 1 > pos:
                inner_name, inner_open = stack.pop()
                out.append(f"</{inner_name}>")
                reopen.append((inner_name, inner_open))
            stack.pop()
            out.append(f"</{tag_name}>")
            for inner_name, inner_open in reversed(reopen):
                out.append(inner_open)
                stack.append((inner_name, inner_open))

    out.append(text[last_idx:])
    for tag_name, _ in reversed(stack):
        out.append(f"</{tag_name}>")

    balanced = "".join(out)
    for _ in range(2):
        for tag in ("code", "b", "i", "s", "u", "blockquote", "pre"):
            balanced = re.sub(rf"<{tag}(?:\s+[^>]*)?>\s*</{tag}>", "", balanced)
    return balanced


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
    src = re.sub(r"^([ \t]*)[\*\-\+]\s+(.+)$", r"\1• \2", src, flags=re.MULTILINE)

    for idx, tag in enumerate(inline_codes):
        src = src.replace(f"\x00IC{idx}\x00", tag)
    for idx, tag in enumerate(code_blocks):
        src = src.replace(f"\x00CB{idx}\x00", tag)

    return balance_telegram_html(src)


def looks_like_raw_html_document(text: str) -> bool:
    """Detect whether non-fenced text contains standalone or raw HTML document markup."""
    if not text or "<" not in text:
        return False
    without_fences = re.sub(r"```[\s\S]*?```", "", text).strip()
    if not without_fences:
        return False
    low = without_fences.lower()
    if low.startswith(("<!doctype html", "<html")):
        return True
    if "<!doctype html" in low or ("<html" in low and ("</html>" in low or "<body" in low)):
        return True
    if "</head>" in low and "<body" in low:
        return True
    structural_tags = _HTML_STRUCTURAL_TAG_RE.findall(without_fences)
    if len(structural_tags) >= 3 and any(
        close_tag in low for close_tag in ("</div>", "</li>", "</ul>", "</ol>", "</table>", "</style>")
    ):
        return True
    return False


def mask_streaming_protocol_and_html(text: str) -> str:
    """Replace in-flight protocol payloads and raw HTML blocks with concise preview status notes."""
    if not text:
        return ""

    masked = text
    if "[AGY_VOICE" in masked or "[agy_voice" in masked.lower():
        masked = re.sub(
            r"\[AGY_VOICE.*?(?:\[END_AGY_VOICE\]|$)",
            "",
            masked,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
    masked = re.sub(r"\[/?AGY_TEXT\]", "", masked, flags=re.IGNORECASE).strip()
    masked = re.sub(r"\[/?AGY_DISPLAY\]", "", masked, flags=re.IGNORECASE).strip()

    masked = _AGY_FILE_BLOCK_RE.sub("\n\n📄 *(HTML report attachment ready...)*", masked)
    masked = re.sub(
        r"(?:^|\r?\n)[ \t]*\[AGY_FILE\][ \t]*\r?\n[\s\S]*$",
        "\n\n📄 *(Generating standalone HTML report attachment...)*",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"^\[AGY_FILE\][\s\S]*$",
        "📄 *(Generating standalone HTML report attachment...)*",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"(?:^|\r?\n)[ \t]*\[AGY_ACTIONS\][ \t]*\r?\n[\s\S]*$",
        "",
        masked,
        flags=re.IGNORECASE,
    )

    if looks_like_raw_html_document(masked):
        pos = 0
        for part in re.split(r"(```[\s\S]*?```)", masked):
            if part.startswith("```"):
                pos += len(part)
                continue
            match = _HTML_RAW_START_RE.search(part)
            if match:
                prefix_text = masked[: pos + match.start()].strip()
                note = "📄 *(Generating standalone HTML document...)*"
                return f"{prefix_text}\n\n{note}" if prefix_text else note
            pos += len(part)

    return masked.strip()


def build_auto_export_preview(
    clean_text: str,
    filename: str,
    prefix: str = "report",
    max_preview_chars: int = 900,
) -> str:
    """Condense long reports or raw HTML documents into a clean single-message Telegram summary."""
    header = f"📄 _Full {prefix} generated and attached below: `{filename}`_"
    body = (clean_text or "").strip()
    if not body:
        return header

    if looks_like_raw_html_document(body):
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        title_txt = html.unescape(title_match.group(1).strip()) if title_match else ""
        stripped = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", body, flags=re.IGNORECASE)
        stripped = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = html.unescape(re.sub(r"[ \t]+", " ", stripped))
        excerpt_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        excerpt = " ".join(excerpt_lines)
        if len(excerpt) > 360:
            cut = excerpt[:357].rsplit(" ", 1)[0].strip()
            excerpt = (cut or excerpt[:357]) + "..."
        parts = [header]
        if title_txt and title_txt.lower() not in excerpt.lower():
            parts.append(f"**{title_txt}**")
        if excerpt:
            parts.append(excerpt)
        return "\n\n".join(parts)

    if len(body) <= 1500:
        return f"{header}\n\n{body}"

    preview = body[:max_preview_chars]
    split_para = preview.rfind("\n\n", 250)
    if split_para != -1:
        preview = preview[:split_para].rstrip()
    else:
        split_line = preview.rfind("\n", 250)
        if split_line != -1:
            preview = preview[:split_line].rstrip()
        else:
            split_space = preview.rfind(" ", 250)
            if split_space != -1:
                preview = preview[:split_space].rstrip()
            preview += "..."

    if preview.count("```") % 2 != 0:
        preview = f"{preview}\n```"

    return f"{header}\n\n{preview}\n\n_... (full {prefix} attached below)_"


def wrap_raw_html_for_mobile(
    title: str,
    raw_html_content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Wrap raw HTML content into an offline, mobile-responsive HTML document (zero CDNs)."""
    stripped = (raw_html_content or "").strip()
    low = stripped.lower()
    is_full_doc = "<!doctype html" in low or "<html" in low

    if is_full_doc:
        doc = stripped
        if 'name="viewport"' not in low and "name='viewport'" not in low:
            viewport_tag = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            if "<head" in low:
                doc = re.sub(r"(<head[^>]*>)", rf"\1\n{viewport_tag}", doc, count=1, flags=re.IGNORECASE)
            elif "<html" in low:
                doc = re.sub(
                    r"(<html[^>]*>)",
                    rf"\1\n<head>\n{viewport_tag}\n</head>",
                    doc,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                doc = f"{viewport_tag}\n{doc}"

        if "overflow-x" not in doc.lower():
            responsive_style = (
                "<style>\n"
                "pre, code, table { overflow-x: auto; -webkit-overflow-scrolling: touch; max-width: 100%; }\n"
                "body { -webkit-text-size-adjust: 100%; margin: 0; padding: 12px; box-sizing: border-box; }\n"
                "img { max-width: 100%; height: auto; }\n"
                "</style>"
            )
            if "</head>" in doc.lower():
                doc = re.sub(r"(</head>)", f"{responsive_style}\n\\1", doc, count=1, flags=re.IGNORECASE)
            else:
                doc = f"{responsive_style}\n{doc}"
        return doc

    meta = metadata or {}
    badges: List[str] = []
    for key, icon in (("project", "📦"), ("model", "🤖"), ("time", "⏱"), ("tokens", "🪙")):
        val = meta.get(key)
        if val:
            badges.append(f'<span class="badge">{icon} {html.escape(str(val))}</span>')
    badges_html = " ".join(badges)
    badge_row = f'<div class="badges">{badges_html}</div>' if badges_html else ""

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<style>\n"
        ":root { --bg:#0f172a; --card:#1e293b; --border:#334155; --text:#f8fafc; --muted:#94a3b8; --accent:#38bdf8; }\n"
        "@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --card:#ffffff; --border:#cbd5e1; --text:#0f172a; --muted:#475569; --accent:#0284c7; } }\n"
        "* { box-sizing: border-box; }\n"
        "body { margin:0; padding:12px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }\n"
        ".container { max-width:900px; margin:0 auto; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; }\n"
        ".header { border-bottom:1px solid var(--border); padding-bottom:10px; margin-bottom:14px; }\n"
        ".header h2 { margin:0; font-size:1.1rem; }\n"
        ".badges { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }\n"
        ".badge { font-size:0.75rem; padding:2px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); }\n"
        "pre, code, table { overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%; }\n"
        "img { max-width:100%; height:auto; }\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="container">\n'
        f'  <div class="header"><h2>{html.escape(title)}</h2>{badge_row}</div>\n'
        f'  <div class="content">{stripped}</div>\n'
        "</div>\n"
        "</body>\n"
        "</html>"
    )


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
        summary = re.sub(r"\s+", " ", str(event.tool_info.get("summary") or "")).strip()
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


def _build_tools_blockquote(events: Sequence[str], heading: str, max_items: int) -> str:
    """Format a collapsible Telegram blockquote containing up to max_items escaped tool lines."""
    total = len(events)
    if total > max_items:
        omitted = total - max_items
        shown = [f"<i>... ({omitted} earlier steps omitted)</i>"] + list(events[-max_items:])
    else:
        shown = list(events)
    return f"<blockquote expandable><b>[{escape_html(heading)} — {total}]</b>\n" + "\n".join(shown) + "</blockquote>"


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
        sections.append(_build_tools_blockquote(tool_events, "Progress", max_items=10))

    masked_text = mask_streaming_protocol_and_html(current_text)
    body = markdown_to_telegram_html(masked_text) if masked_text else "<i>(executing step...)</i>"
    cursor = " ▌" if masked_text else ""
    sections.append(body + cursor)

    full = "\n\n".join(sections)
    if len(full) <= MAX_TELEGRAM_MESSAGE_LEN:
        balanced = balance_telegram_html(full)
        if len(balanced) <= MAX_TELEGRAM_MESSAGE_LEN:
            return balanced

    # Trim streaming body tail while preserving header + compact tool trace within 4000 chars.
    recent_header = "\n\n".join(sections[:-1])
    if len(recent_header) > 2200 and tool_events:
        recent_header = f"{header}\n\n{_build_tools_blockquote(tool_events, 'Progress', max_items=3)}"
    if len(recent_header) > 2500:
        recent_header = header

    avail = max(80, MAX_TELEGRAM_MESSAGE_LEN - len(recent_header) - len(cursor) - 60)
    while avail >= 40:
        tail_raw = masked_text[-avail:]
        tail_html = markdown_to_telegram_html(tail_raw)
        candidate = balance_telegram_html(f"{recent_header}\n\n[...]\n{tail_html}{cursor}")
        if len(candidate) <= MAX_TELEGRAM_MESSAGE_LEN:
            return candidate
        avail //= 2

    return balance_telegram_html(header[: MAX_TELEGRAM_MESSAGE_LEN - 32])


def format_worker_final_card(
    pid: str,
    tid: str,
    result: StepResult,
    task_status: str,
    task_assignee: str,
    latest_comment: str = "",
    formatted_tool_events: Optional[Sequence[str]] = None,
    auto_export_filename: Optional[str] = None,
) -> str:
    """Format the final completion, cancellation, or failure card for a worker step."""
    is_cancelled = result.error_category == ErrorCategory.CANCELLED
    if result.success:
        badge = "✅ <b>Worker Step Completed</b>"
    elif is_cancelled:
        badge = "🛑 <b>Worker Step Stopped</b>"
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
    head_section = f"{badge}\n\n{meta_line}"

    escaped_tools: List[str] = []
    if formatted_tool_events:
        escaped_tools = list(formatted_tool_events)
    elif result.tool_events:
        for raw_ev in result.tool_events:
            single_line = re.sub(r"\s+", " ", str(raw_ev)).strip()
            if len(single_line) > 90:
                single_line = single_line[:87] + "..."
            escaped_tools.append(escape_html(single_line))

    tools_section = _build_tools_blockquote(escaped_tools, "Executed Tools", max_items=8) if escaped_tools else ""

    summary_section = ""
    if is_cancelled:
        cancel_msg = escape_html(re.sub(r"\s+", " ", result.error_message or "Execution interrupted by Admin.").strip())
        summary_section = f"<b>Summary:</b>\n{cancel_msg}"
    else:
        raw_summary = (latest_comment or result.response or result.error_message or "").strip()
        if raw_summary:
            if looks_like_raw_html_document(raw_summary):
                fname = auto_export_filename or f"{pid}_{tid}_report.html"
                summary_src = build_auto_export_preview(raw_summary, fname)
            elif auto_export_filename:
                summary_src = build_auto_export_preview(raw_summary, auto_export_filename)
            elif len(raw_summary) > 1500:
                summary_src = raw_summary[:1497] + "..."
            else:
                summary_src = raw_summary
            summary_section = f"<b>Summary:</b>\n{markdown_to_telegram_html(summary_src)}"

    tok_total = result.usage.total_tokens if result.usage else 0
    tok_think = result.usage.thinking_tokens if result.usage else 0
    footer = f"⏱ {format_duration(result.duration_seconds)}"
    if tok_total > 0:
        footer += f" | 🪙 {format_tokens(tok_total, tok_think)} tokens"
    footer_section = f"———\n{footer}"

    parts = [p for p in (head_section, tools_section, summary_section, footer_section) if p]
    card = balance_telegram_html("\n\n".join(parts))
    if len(card) <= MAX_TELEGRAM_MESSAGE_LEN:
        return card

    if escaped_tools and len(tools_section) > 1400:
        tools_section = _build_tools_blockquote(escaped_tools, "Executed Tools", max_items=4)

    if summary_section:
        base_len = len(head_section) + (len(tools_section) + 2 if tools_section else 0) + len(footer_section) + 40
        avail = max(100, MAX_TELEGRAM_MESSAGE_LEN - base_len)
        raw_plain = (latest_comment or result.response or result.error_message or "").strip()
        while avail >= 60:
            snippet = raw_plain[:avail] + ("..." if len(raw_plain) > avail else "")
            if looks_like_raw_html_document(raw_plain):
                snippet = build_auto_export_preview(raw_plain, auto_export_filename or f"{pid}_{tid}_report.html")
            candidate = balance_telegram_html(
                "\n\n".join(p for p in (head_section, tools_section, f"<b>Summary:</b>\n{markdown_to_telegram_html(snippet)}", footer_section) if p)
            )
            if len(candidate) <= MAX_TELEGRAM_MESSAGE_LEN:
                return candidate
            avail //= 2

    trimmed = balance_telegram_html("\n\n".join(p for p in (head_section, tools_section, footer_section) if p))
    return trimmed[:MAX_TELEGRAM_MESSAGE_LEN] if len(trimmed) > MAX_TELEGRAM_MESSAGE_LEN else trimmed


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
        tool_info={"summary": "node - <<'EOF'\nconsole.log(1)\nEOF"},
    )
    tool_line = format_harness_tool_event(ev)
    assert "&lt;&lt;" in tool_line and "\n" not in tool_line
    live = format_worker_live_card(
        "ALPHA",
        "ALPHA-1",
        "agy",
        "gemini-3.8-flash",
        "auto",
        [tool_line],
        "Status ok\n\n<!DOCTYPE html><html><body><div>Hidden HTML",
    )
    assert "<blockquote expandable>" in live
    assert "Generating standalone HTML document" in live
    assert "<div>" not in live
    final = format_worker_final_card(
        "GO",
        "GO-5",
        StepResult(
            success=True,
            harness="agy",
            tool_events=["[tool:shell] DONE: python -m json.tool a.json > /dev/null && echo ok (1.0s)"],
        ),
        "complete",
        "Admin",
        latest_comment="Done",
    )
    assert "&gt;" in final and "&amp;&amp;" in final
    assert "<b>foo <i>bar</i></b><i> baz</i>" == balance_telegram_html("<b>foo <i>bar</b> baz</i>")
    md_html = markdown_to_telegram_html("# Heading\n- item `x`")
    assert "<b>Heading</b>" in md_html and "<code>x</code>" in md_html
    print("telegram_ui.formatter self-test passed!")
