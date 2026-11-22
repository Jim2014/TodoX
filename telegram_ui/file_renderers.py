"""Self-contained, offline mobile-first HTML file renderers (zero external CDNs)."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Optional

import markdown

BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".svgz",
        ".pdf",
        ".epub",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".rar",
        ".bz2",
        ".xz",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".mov",
        ".webm",
        ".flac",
        ".sqlite",
        ".db",
        ".sqlite3",
    }
)

_EXT_LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".diff": "diff",
    ".patch": "diff",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".txt": "text",
    ".log": "text",
    ".csv": "text",
}

_BASE_CSS = """
:root {
  --bg: #0f172a;
  --card-bg: #1e293b;
  --code-bg: #020617;
  --border: #334155;
  --text: #f8fafc;
  --text-muted: #94a3b8;
  --accent: #38bdf8;
  --accent-bg: rgba(56, 189, 248, 0.14);
  --add-bg: rgba(46, 160, 67, 0.2);
  --add-text: #4ade80;
  --sub-bg: rgba(248, 81, 73, 0.2);
  --sub-text: #f87171;
  --hl-kw: #c084fc;
  --hl-str: #34d399;
  --hl-cmt: #64748b;
  --hl-num: #fb923c;
  --hl-dec: #38bdf8;
  --hl-val: #f472b6;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f8fafc;
    --card-bg: #ffffff;
    --code-bg: #f1f5f9;
    --border: #cbd5e1;
    --text: #0f172a;
    --text-muted: #475569;
    --accent: #0284c7;
    --accent-bg: rgba(2, 132, 199, 0.1);
    --add-bg: #dcfce7;
    --add-text: #166534;
    --sub-bg: #fee2e2;
    --sub-text: #991b1b;
    --hl-kw: #7e22ce;
    --hl-str: #059669;
    --hl-cmt: #64748b;
    --hl-num: #ea580c;
    --hl-dec: #0284c7;
    --hl-val: #db2777;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: var(--font-sans);
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  padding: 12px;
  word-break: break-word;
}
.container {
  max-width: 960px;
  margin: 0 auto;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 4px 16px rgba(0,0,0,0.18);
}
.header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.title { font-size: 1.08rem; font-weight: 700; }
.meta { font-size: 0.78rem; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); }
.badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.badge {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-bg);
  color: var(--accent);
}
.actions { display: flex; gap: 8px; }
.btn {
  padding: 6px 12px;
  font-size: 0.8rem;
  font-weight: 600;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
}
.btn.active { background: var(--accent); color: #ffffff; border-color: var(--accent); }
.content { padding: 16px 18px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.md-body h1, .md-body h2, .md-body h3 { margin: 16px 0 8px; }
.md-body p { margin-bottom: 10px; }
.md-body ul, .md-body ol { padding-left: 22px; margin-bottom: 10px; }
.md-body pre {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  font-family: var(--font-mono);
  font-size: 0.83rem;
  margin: 10px 0;
}
.md-body code {
  font-family: var(--font-mono);
  background: var(--code-bg);
  padding: 1px 4px;
  border-radius: 4px;
  font-size: 0.88em;
}
.md-body pre code { background: transparent; padding: 0; }
.md-body blockquote {
  border-left: 4px solid var(--accent);
  padding: 6px 12px;
  background: var(--accent-bg);
  margin: 10px 0;
  border-radius: 0 6px 6px 0;
}
.table-wrap { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 10px 0; }
.md-body table { width: 100%; border-collapse: collapse; min-width: 420px; }
.md-body th, .md-body td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; }
.md-body th { background: var(--bg); }
.code-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 0.82rem; line-height: 1.5; }
.line-num {
  width: 46px;
  padding: 2px 10px;
  text-align: right;
  color: var(--text-muted);
  border-right: 1px solid var(--border);
  user-select: none;
  vertical-align: top;
}
.line-code { padding: 2px 12px; white-space: pre; word-break: normal; vertical-align: top; }
.diff-add { background: var(--add-bg); color: var(--add-text); display: block; }
.diff-sub { background: var(--sub-bg); color: var(--sub-text); display: block; }
.diff-info { color: var(--accent); font-weight: 700; display: block; }
.hl-kw { color: var(--hl-kw); font-weight: 600; }
.hl-str { color: var(--hl-str); }
.hl-cmt { color: var(--hl-cmt); font-style: italic; }
.hl-num { color: var(--hl-num); }
.hl-dec { color: var(--hl-dec); font-weight: 600; }
.hl-val { color: var(--hl-val); font-weight: 600; }
"""


def is_binary_file(file_path: Path) -> bool:
    """Return True when file extension is binary or initial bytes contain NUL."""
    if file_path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(file_path, "rb") as f:
            return b"\x00" in f.read(4096)
    except OSError:
        return True


def format_file_size(size_bytes: int) -> str:
    """Format byte size into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def highlight_code_syntax(code_text: str, lang: str) -> List[str]:
    """Tokenize code lines with lightweight regex highlighting safe for offline HTML."""
    lines = code_text.splitlines()
    if not lines:
        return [""]

    keywords = {
        "def", "class", "import", "from", "as", "return", "if", "elif", "else",
        "for", "while", "in", "try", "except", "finally", "with", "raise",
        "assert", "yield", "lambda", "async", "await", "pass", "break", "continue",
        "function", "const", "let", "var", "export", "default", "extends", "new",
        "switch", "case", "select", "where", "insert", "update", "delete", "create",
    }

    highlighted: List[str] = []
    for raw_line in lines:
        if lang == "diff":
            esc = html.escape(raw_line, quote=False)
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                highlighted.append(f'<span class="diff-add">{esc}</span>')
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                highlighted.append(f'<span class="diff-sub">{esc}</span>')
            elif raw_line.startswith(("@@", "diff --git")):
                highlighted.append(f'<span class="diff-info">{esc}</span>')
            else:
                highlighted.append(esc)
            continue

        esc = html.escape(raw_line, quote=False)
        if lang in ("python", "bash", "yaml", "toml", "ini"):
            m_cmt = re.search(r"(#.*)$", esc)
            if m_cmt:
                esc = esc[: m_cmt.start()] + f'<span class="hl-cmt">{m_cmt.group(1)}</span>'
        elif lang in ("javascript", "typescript", "css"):
            m_cmt = re.search(r"(//.*)$", esc)
            if m_cmt:
                esc = esc[: m_cmt.start()] + f'<span class="hl-cmt">{m_cmt.group(1)}</span>'

        esc = re.sub(r"(&quot;.*?&quot;|'.*?'|`.*?`)", r'<span class="hl-str">\1</span>', esc)
        if lang == "python":
            esc = re.sub(r"^(\s*)(@\w+)", r'\1<span class="hl-dec">\2</span>', esc)

        def _kw_repl(m: re.Match) -> str:
            word = m.group(0)
            if word.lower() in keywords:
                return f'<span class="hl-kw">{word}</span>'
            if word in ("True", "False", "None", "true", "false", "null", "undefined"):
                return f'<span class="hl-val">{word}</span>'
            return word

        esc = re.sub(r"\b[a-zA-Z_]\w*\b", _kw_repl, esc)
        esc = re.sub(r"\b(\d+(?:\.\d+)?)\b", r'<span class="hl-num">\1</span>', esc)
        highlighted.append(esc)

    return highlighted


def render_markdown_to_html(
    title: str,
    rel_path: str,
    md_content: str,
    pid: str = "",
) -> str:
    """Render Markdown to an offline, standalone HTML page with Rendered/Raw toggle tabs."""
    rendered_html = markdown.markdown(
        md_content,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    rendered_html = re.sub(r"(<table>.*?</table>)", r'<div class="table-wrap">\1</div>', rendered_html, flags=re.DOTALL)

    pid_badge = f'<span class="badge">{html.escape(pid)}</span>' if pid else ""
    raw_escaped = html.escape(md_content, quote=False)
    size_str = format_file_size(len(md_content.encode("utf-8")))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <div class="title">{html.escape(title)}</div>
      <div class="meta">{html.escape(rel_path)}</div>
      <div class="badges">{pid_badge}<span class="badge">Markdown</span><span class="badge">{size_str}</span></div>
    </div>
    <div class="actions">
      <button id="btn-render" class="btn active" onclick="showTab('render')">Rendered</button>
      <button id="btn-raw" class="btn" onclick="showTab('raw')">Raw</button>
    </div>
  </div>
  <div id="view-render" class="content md-body">{rendered_html}</div>
  <div id="view-raw" class="content" style="display:none;"><pre style="white-space:pre-wrap;font-family:var(--font-mono);font-size:0.84rem;">{raw_escaped}</pre></div>
</div>
<script>
function showTab(mode) {{
  document.getElementById('view-render').style.display = mode === 'render' ? 'block' : 'none';
  document.getElementById('view-raw').style.display = mode === 'raw' ? 'block' : 'none';
  document.getElementById('btn-render').classList.toggle('active', mode === 'render');
  document.getElementById('btn-raw').classList.toggle('active', mode === 'raw');
}}
</script>
</body>
</html>"""


def render_code_to_html(
    title: str,
    rel_path: str,
    raw_content: str,
    lang: str,
    file_size_str: str,
    pid: str = "",
) -> str:
    """Render source code, diffs, JSON, or logs into a standalone offline HTML viewer."""
    highlighted = highlight_code_syntax(raw_content, lang)
    rows_html = "\n".join(
        f'<tr><td class="line-num">{idx}</td><td class="line-code">{line}</td></tr>'
        for idx, line in enumerate(highlighted, 1)
    )
    pid_badge = f'<span class="badge">{html.escape(pid)}</span>' if pid else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <div class="title">{html.escape(title)}</div>
      <div class="meta">{html.escape(rel_path)}</div>
      <div class="badges">{pid_badge}<span class="badge">{html.escape(lang)}</span><span class="badge">{len(highlighted)} lines</span><span class="badge">{html.escape(file_size_str)}</span></div>
    </div>
    <div class="actions">
      <button id="copy-btn" class="btn" onclick="copyRaw()">Copy</button>
    </div>
  </div>
  <div class="content" style="padding:0;">
    <table class="code-table">{rows_html}</table>
  </div>
</div>
<textarea id="raw-copy" style="position:absolute;left:-9999px;">{html.escape(raw_content, quote=False)}</textarea>
<script>
function copyRaw() {{
  var ta = document.getElementById('raw-copy');
  ta.select();
  document.execCommand('copy');
  var btn = document.getElementById('copy-btn');
  btn.innerText = 'Copied';
  setTimeout(function() {{ btn.innerText = 'Copy'; }}, 1500);
}}
</script>
</body>
</html>"""


def render_file_to_html(
    file_path: Path,
    rel_path: str,
    pid: str = "",
) -> Optional[bytes]:
    """Render any non-binary workspace file as offline HTML bytes, or None for binary files."""
    if not file_path.is_file() or is_binary_file(file_path):
        return None

    raw_text = file_path.read_text(encoding="utf-8", errors="replace")
    suffix = file_path.suffix.lower()
    if suffix in (".md", ".markdown"):
        html_str = render_markdown_to_html(
            title=file_path.name,
            rel_path=rel_path,
            md_content=raw_text,
            pid=pid,
        )
    else:
        lang = _EXT_LANG_MAP.get(suffix, "text")
        size_str = format_file_size(file_path.stat().st_size)
        html_str = render_code_to_html(
            title=file_path.name,
            rel_path=rel_path,
            raw_content=raw_text,
            lang=lang,
            file_size_str=size_str,
            pid=pid,
        )
    return html_str.encode("utf-8")


if __name__ == "__main__":
    md_html = render_markdown_to_html("README.md", "README.md", "# TodoX\n\n- Works offline\n", pid="DEMO")
    assert "http://" not in md_html and "https://" not in md_html
    code_html = render_code_to_html("main.py", "main.py", "def hello():\n    return 42\n", "python", "28 B", pid="DEMO")
    assert "http://" not in code_html and "https://" not in code_html
    print("telegram_ui.file_renderers self-test passed!")
