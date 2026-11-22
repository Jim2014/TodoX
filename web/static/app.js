(() => {
  "use strict";

  const COLUMNS = [
    { id: "not_started", label: "Backlog", statuses: ["not_started"] },
    { id: "in_progress", label: "In Progress", statuses: ["in_progress"] },
    { id: "need_confirm", label: "Needs Review", statuses: ["need_confirm", "blocked"] },
    { id: "complete", label: "Done", statuses: ["complete"] },
    { id: "failed", label: "Failed", statuses: ["failed"] },
  ];

  const bodyDataset = document.body.dataset;
  const appState = {
    page: bodyDataset.page || "dashboard",
    pid: (bodyDataset.pid || "").toUpperCase(),
    tid: (bodyDataset.tid || "").toUpperCase(),
    activeTab: "board",
    showDock: localStorage.getItem("todox_show_dock") !== "false",
    autoScroll: true,
    projectsPayload: null,
    projectPayload: null,
    harnessesPayload: null,
    events: [],
    selectedFilePath: "",
    fileFilter: "",
    openTaskId: "",
    eventSource: null,
    refreshTimer: null,
    dragTid: "",
  };

  function escapeHtml(raw) {
    return String(raw ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatDuration(sec) {
    const val = Number(sec || 0);
    if (!Number.isFinite(val) || val <= 0) return "0.0s";
    if (val < 60) return `${val.toFixed(1)}s`;
    const mins = Math.floor(val / 60);
    const rem = Math.round(val % 60);
    return `${mins}m ${rem}s`;
  }

  function formatTokens(n) {
    const num = Number(n || 0);
    if (!Number.isFinite(num) || num <= 0) return "0";
    if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
    if (num >= 1_000) return `${(num / 1_000).toFixed(1)}k`;
    return String(num);
  }

  function formatTimeShort(ts) {
    if (!ts) return "";
    const str = String(ts);
    const tIdx = str.indexOf("T");
    if (tIdx !== -1) return str.slice(tIdx + 1, tIdx + 9);
    return str.slice(0, 19);
  }

  function renderMarkdown(raw) {
    const src = String(raw || "");
    if (!src.trim()) return '<span class="muted">No content.</span>';

    const blocks = [];
    let work = src.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (_, _lang, code) => {
      const idx = blocks.length;
      blocks.push(`<pre><code>${escapeHtml(code.replace(/\n+$/, ""))}</code></pre>`);
      return `\u0000BLK${idx}\u0000`;
    });

    const inlines = [];
    work = work.replace(/`([^`\n]+)`/g, (_, code) => {
      const idx = inlines.length;
      inlines.push(`<code>${escapeHtml(code)}</code>`);
      return `\u0000INL${idx}\u0000`;
    });

    work = escapeHtml(work);
    work = work.replace(/^#{1,6}\s+(.+)$/gm, "<strong>$1</strong>");
    work = work.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    work = work.replace(/(^|[^\w])\*([^\*\n]+?)\*(?!\w)/g, "$1<em>$2</em>");
    work = work.replace(
      /\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s\)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    work = work.replace(/^[ \t]*[\*\-\+]\s+(.+)$/gm, "• $1");
    work = work.replace(/\n/g, "<br>");

    inlines.forEach((htmlChunk, idx) => {
      work = work.replaceAll(`\u0000INL${idx}\u0000`, htmlChunk);
    });
    blocks.forEach((htmlChunk, idx) => {
      work = work.replaceAll(`\u0000BLK${idx}\u0000`, htmlChunk);
    });
    return work;
  }

  function showToast(msg, isError = false) {
    const region = document.getElementById("toast-region");
    if (!region) return;
    const el = document.createElement("div");
    el.className = `toast${isError ? " error" : ""}`;
    el.textContent = msg;
    region.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, 3200);
  }

  async function apiJson(method, url, body = null) {
    const opts = { method, headers: {} };
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const text = await resp.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = { detail: text };
    }
    if (!resp.ok) {
      throw new Error((parsed && parsed.detail) || `HTTP ${resp.status}`);
    }
    return parsed;
  }

  /* Theme management via <meta name="color-scheme"> + localStorage */
  function getStoredTheme() {
    try {
      const val = localStorage.getItem("color-scheme");
      return val === "light" || val === "dark" ? val : "system";
    } catch {
      return "system";
    }
  }

  function applyTheme(mode) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (!meta) return;
    if (mode === "light" || mode === "dark") {
      meta.content = mode;
      try {
        localStorage.setItem("color-scheme", mode);
      } catch {}
    } else {
      meta.content = "light dark";
      try {
        localStorage.removeItem("color-scheme");
      } catch {}
    }
    document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.themeChoice === mode);
    });
  }

  window.addEventListener("storage", (ev) => {
    if (ev.key === "color-scheme") {
      applyTheme(getStoredTheme());
    }
  });

  /* Dialog closedby="any" Safari fallback per modern-web-guidance */
  function setupDialogDismissFallbacks() {
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e, dlg) => {
        const r = dlg.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dlg of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dlg.addEventListener("pointerdown", (e) => {
          pressedOutside = outside(e, dlg);
        });
        dlg.addEventListener("pointerup", (e) => {
          if (pressedOutside && outside(e, dlg)) {
            dlg.close();
          }
        });
      }
    }

    document.addEventListener("click", (ev) => {
      const closeBtn = ev.target.closest("[data-close-dialog]");
      if (closeBtn) {
        const dlg = document.getElementById(closeBtn.dataset.closeDialog);
        if (dlg && dlg.open) dlg.close();
      }
    });
  }

  function isUserEditing() {
    const active = document.activeElement;
    if (!active) return false;
    const tag = active.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  function updateHeaderFromState(sysState) {
    const crumbs = document.getElementById("breadcrumbs");
    if (crumbs) {
      if (appState.page === "dashboard") {
        crumbs.innerHTML = '<span>/</span> <strong>Projects</strong>';
      } else {
        crumbs.innerHTML =
          `<span>/</span> <a href="/">Projects</a> <span>/</span> <strong>${escapeHtml(appState.pid)}</strong>` +
          (appState.openTaskId ? ` <span>/</span> <span>${escapeHtml(appState.openTaskId)}</span>` : "");
      }
    }

    const watcherBtn = document.getElementById("watcher-toggle-btn");
    if (watcherBtn && sysState) {
      const paused = Boolean(sysState.watcher_paused);
      watcherBtn.textContent = paused ? "Paused (Resume)" : "Watching";
      watcherBtn.className = `btn btn-sm ${paused ? "btn-warning" : "btn-ghost"}`;
    }
  }

  function setSseStatus(status, label) {
    const badge = document.getElementById("sse-badge");
    const lbl = document.getElementById("sse-label");
    if (!badge || !lbl) return;
    badge.className = `status-chip ${status}`;
    lbl.textContent = label;
  }

  function renderLogEntriesHtml(events) {
    if (!events || events.length === 0) {
      return '<div class="muted" style="padding:12px;">Waiting for live worker activity...</div>';
    }
    return events
      .map((ev) => {
        const evType = escapeHtml(ev.type || "info");
        const scope = ev.tid ? `${ev.pid}/${ev.tid}` : ev.pid || "GLOBAL";
        const badgePart = ev.harness ? ` • ${escapeHtml(ev.harness)}` : "";
        return (
          `<div class="log-row log-${evType}">` +
          `<div class="log-meta">` +
          `<span>${escapeHtml(formatTimeShort(ev.ts))}</span>` +
          `<strong>${escapeHtml(scope)}</strong>` +
          `<span>[${evType}${badgePart}]</span>` +
          `</div>` +
          `<div>${escapeHtml(ev.message || "")}</div>` +
          `</div>`
        );
      })
      .join("");
  }

  function renderStreamPanelHtml(pid, tall = false) {
    const runtime = pid
      ? appState.projectPayload?.runtime || appState.projectsPayload?.runtime?.[pid]
      : null;
    const isRunning = Boolean(runtime);
    const title = pid ? `Live Worker Stream — ${pid}` : "Global Activity Stream";
    const statusChip = isRunning
      ? `<span class="status-chip chip-info running-pulse"><span class="pulse-dot"></span>Running ${escapeHtml(runtime.tid || pid)}</span>`
      : '<span class="status-chip chip-muted">Idle</span>';

    const steerControls = pid
      ? `<form id="steer-form" class="steer-bar" data-pid="${escapeHtml(pid)}">` +
        `<input name="msg" placeholder="Steer active worker or queue instruction..." autocomplete="off">` +
        `<button type="submit" class="btn btn-sm btn-primary">Steer</button>` +
        (isRunning
          ? `<button type="button" class="btn btn-sm btn-danger" data-action="stop-worker" data-pid="${escapeHtml(pid)}">Stop</button>`
          : "") +
        `</form>`
      : "";

    return (
      `<section class="stream-panel">` +
      `<div class="stream-header">` +
      `<div><h3>${escapeHtml(title)}</h3></div>` +
      `<div style="display:flex;align-items:center;gap:6px;">${statusChip}` +
      `<button type="button" class="btn btn-xs btn-ghost" style="color:#cbd5e1;border-color:#334155;" data-action="clear-stream-view">Clear</button>` +
      `</div></div>` +
      `<div id="live-log-feed" class="stream-feed${tall ? " tall" : ""}">${renderLogEntriesHtml(appState.events)}</div>` +
      steerControls +
      `</section>`
    );
  }

  function appendStreamEventToDom(ev) {
    appState.events.push(ev);
    if (appState.events.length > 200) {
      appState.events = appState.events.slice(-200);
    }
    const feed = document.getElementById("live-log-feed");
    if (!feed) return;
    if (appState.events.length === 1) {
      feed.innerHTML = "";
    }
    const row = document.createElement("div");
    const evType = escapeHtml(ev.type || "info");
    row.className = `log-row log-${evType}`;
    const scope = ev.tid ? `${ev.pid}/${ev.tid}` : ev.pid || "GLOBAL";
    const badgePart = ev.harness ? ` • ${escapeHtml(ev.harness)}` : "";
    row.innerHTML =
      `<div class="log-meta"><span>${escapeHtml(formatTimeShort(ev.ts))}</span>` +
      `<strong>${escapeHtml(scope)}</strong><span>[${evType}${badgePart}]</span></div>` +
      `<div>${escapeHtml(ev.message || "")}</div>`;
    feed.appendChild(row);
    while (feed.children.length > 200) {
      feed.removeChild(feed.firstChild);
    }
    if (appState.autoScroll) {
      feed.scrollTop = feed.scrollHeight;
    }
  }

  /* Render Dashboard Page */
  function renderDashboard() {
    const payload = appState.projectsPayload;
    if (!payload) return;
    const root = document.getElementById("app-root");
    const projects = payload.projects || [];
    const sys = payload.system_state || {};
    updateHeaderFromState(sys);

    const runningCount = projects.filter((p) => p.running).length;
    const needConfirmTotal = projects.reduce((acc, p) => acc + (p.task_counts?.need_confirm || 0), 0);
    const totalTasks = projects.reduce((acc, p) => acc + (p.total_tasks || 0), 0);
    const doneTasks = projects.reduce((acc, p) => acc + (p.completed_tasks || 0), 0);

    const cardsHtml = projects.length
      ? projects
          .map((p) => {
            const pct = p.total_tasks > 0 ? Math.round((p.completed_tasks / p.total_tasks) * 100) : 0;
            const counts = p.task_counts || {};
            const statusChip = p.running
              ? `<span class="status-chip chip-info running-pulse"><span class="pulse-dot"></span>Running ${escapeHtml(p.runtime?.tid || "")}</span>`
              : p.paused
              ? '<span class="status-chip chip-warn">Paused</span>'
              : `<span class="status-chip chip-muted">${escapeHtml(p.status)}</span>`;

            const prof = p.effective_profile || {};
            return (
              `<article class="project-card">` +
              `<div class="project-card-top">` +
              `<div>` +
              `<span class="project-pid">${escapeHtml(p.id)}</span>` +
              `<h2 class="project-title"><a href="/project/${encodeURIComponent(p.id)}">${escapeHtml(p.title || p.id)}</a></h2>` +
              `</div>` +
              `${statusChip}` +
              `</div>` +
              `<p class="project-desc">${escapeHtml(p.descriptions || "No description provided.")}</p>` +
              `<div class="progress-track" title="${pct}% complete"><div class="progress-fill" style="width:${pct}%"></div></div>` +
              `<div class="card-stats-row">` +
              `<span>Tasks: <strong>${p.completed_tasks}/${p.total_tasks}</strong></span>` +
              (counts.need_confirm ? `<span class="chip-warn" style="padding:1px 6px;border-radius:6px;">${counts.need_confirm} Review</span>` : "") +
              (counts.failed ? `<span class="chip-danger" style="padding:1px 6px;border-radius:6px;">${counts.failed} Failed</span>` : "") +
              `<span class="mono">${escapeHtml(prof.harness || "agy")} · ${escapeHtml(prof.model || "default")}</span>` +
              `</div>` +
              `<div class="project-card-actions">` +
              `<div style="display:flex;gap:6px;">` +
              `<a class="btn btn-sm btn-primary" href="/project/${encodeURIComponent(p.id)}">Open Board</a>` +
              (p.running
                ? `<button type="button" class="btn btn-sm btn-danger" data-action="stop-worker" data-pid="${escapeHtml(p.id)}">Stop</button>`
                : `<button type="button" class="btn btn-sm btn-secondary" data-action="run-project" data-pid="${escapeHtml(p.id)}">Run AI</button>`) +
              `</div>` +
              `<button type="button" class="btn btn-sm btn-ghost" data-action="delete-project" data-pid="${escapeHtml(p.id)}">Delete</button>` +
              `</div>` +
              `</article>`
            );
          })
          .join("")
      : '<div class="metric-card"><p class="muted">No projects yet. Create one to launch autonomous workers.</p></div>';

    root.innerHTML =
      `<div class="section-header">` +
      `<div><h1>Project Hub</h1><p class="muted" style="margin:2px 0 0;">` +
      `Harness: <code class="mono">${escapeHtml(sys.default_harness || "agy")}</code> · ` +
      `Model: <code class="mono">${escapeHtml(sys.default_model || "default")}</code> · ` +
      `Effort: <code class="mono">${escapeHtml(sys.default_effort || "high")}</code> · ` +
      `Preset: <code class="mono">${escapeHtml(sys.default_permission_preset || "auto")}</code>` +
      `</p></div>` +
      `<button type="button" class="btn btn-primary" data-open-dialog="create-project-dialog">+ New Project</button>` +
      `</div>` +
      `<div class="metrics-row">` +
      `<div class="metric-card"><div class="metric-label">Projects</div><div class="metric-val">${projects.length}</div><div class="metric-sub">${runningCount} running now</div></div>` +
      `<div class="metric-card"><div class="metric-label">Tickets Done</div><div class="metric-val">${doneTasks}/${totalTasks}</div><div class="metric-sub">Across all workspaces</div></div>` +
      `<div class="metric-card"><div class="metric-label">Needs Review</div><div class="metric-val">${needConfirmTotal}</div><div class="metric-sub">Awaiting Admin sign-off</div></div>` +
      `<div class="metric-card"><div class="metric-label">Watcher State</div><div class="metric-val">${sys.watcher_paused ? "Paused" : "Active"}</div><div class="metric-sub">Interval ${escapeHtml(sys.interval_seconds || 5)}s</div></div>` +
      `</div>` +
      `<div class="dashboard-split">` +
      `<div class="projects-grid">${cardsHtml}</div>` +
      `${renderStreamPanelHtml("", false)}` +
      `</div>`;

    const feed = document.getElementById("live-log-feed");
    if (feed && appState.autoScroll) feed.scrollTop = feed.scrollHeight;
  }

  /* Render Kanban Card */
  function renderTicketCardHtml(task, isProjectRunning, runtime) {
    const isActiveWorker = isProjectRunning && runtime && runtime.tid === task.id;
    const extraClass = isActiveWorker ? " is-running" : "";
    const statusBadge =
      task.status === "blocked"
        ? '<span class="badge badge-blocked">Blocked</span>'
        : isActiveWorker
        ? '<span class="badge badge-live running-pulse"><span class="pulse-dot"></span>Running</span>'
        : "";
    const tok = task.execution_meta?.total_tokens || 0;
    const quickAction =
      task.status === "need_confirm"
        ? `<button type="button" class="btn btn-xs btn-success" data-action="approve-task" data-tid="${escapeHtml(task.id)}">Approve</button>`
        : task.status !== "complete" && !isActiveWorker
        ? `<button type="button" class="btn btn-xs btn-secondary" data-action="run-task" data-tid="${escapeHtml(task.id)}">Run AI</button>`
        : "";

    return (
      `<div class="ticket-card${extraClass}" draggable="true" data-tid="${escapeHtml(task.id)}">` +
      `<div class="ticket-top">` +
      `<span class="ticket-id">${escapeHtml(task.id)}</span>` +
      `<div style="display:flex;gap:4px;align-items:center;">${statusBadge}` +
      `<span class="badge${task.assignee === "AI" ? " badge-ai" : ""}">${escapeHtml(task.assignee)}</span>` +
      `</div></div>` +
      `<h4 class="ticket-title">${escapeHtml(task.title || "(Untitled)")}</h4>` +
      `<p class="ticket-desc">${escapeHtml(task.descriptions || "")}</p>` +
      `<div class="ticket-footer">` +
      `<div style="display:flex;gap:6px;align-items:center;" class="muted">` +
      `<span style="font-size:11px;">💬 ${(task.comments || []).length}</span>` +
      (tok > 0 ? `<span style="font-size:11px;" class="mono">🪙 ${formatTokens(tok)}</span>` : "") +
      `</div>` +
      `${quickAction}` +
      `</div>` +
      `</div>`
    );
  }

  function renderFileTreeNodesHtml(nodes, filterQuery) {
    if (!nodes || !nodes.length) return "";
    const q = (filterQuery || "").trim().toLowerCase();
    return nodes
      .map((node) => {
        if (node.type === "dir") {
          const childHtml = renderFileTreeNodesHtml(node.children || [], q);
          if (q && !childHtml) return "";
          return (
            `<details class="tree-folder" open>` +
            `<summary>📁 ${escapeHtml(node.name)}</summary>` +
            `<div class="tree-children">${childHtml}</div>` +
            `</details>`
          );
        }
        if (q && !node.path.toLowerCase().includes(q)) return "";
        const isActive = appState.selectedFilePath === node.path;
        const icon = node.ext === ".html" || node.ext === ".htm" ? "🌐" : node.ext === ".md" ? "📝" : "📄";
        return (
          `<button type="button" class="file-btn${isActive ? " active" : ""}" data-file-path="${escapeHtml(node.path)}">` +
          `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${icon} ${escapeHtml(node.name)}</span>` +
          `</button>`
        );
      })
      .join("");
  }

  function buildFilePreviewUrl(pid, relPath) {
    const encodedPath = relPath.split("/").map(encodeURIComponent).join("/");
    const lower = relPath.toLowerCase();
    const isHtmlOrMd = lower.endsWith(".html") || lower.endsWith(".htm") || lower.endsWith(".md") || lower.endsWith(".markdown");
    const isImage = /\.(png|jpe?g|gif|webp|svg)$/i.test(lower);
    const suffix = isHtmlOrMd || isImage ? "" : "?preview=1";
    return `/project/${encodeURIComponent(pid)}/files/${encodedPath}${suffix}`;
  }

  function renderProjectWorkspace() {
    const payload = appState.projectPayload;
    if (!payload) return;
    const root = document.getElementById("app-root");
    const proj = payload.project;
    const tasks = payload.tasks || [];
    const sys = payload.system_state || {};
    const runtime = payload.runtime;
    const isRunning = Boolean(payload.running);
    const isPaused = Boolean(payload.paused);
    const prof = payload.effective_profile || {};
    updateHeaderFromState(sys);

    const statusBadge = isRunning
      ? `<span class="status-chip chip-info running-pulse"><span class="pulse-dot"></span>Running ${escapeHtml(runtime?.tid || "")} (${escapeHtml(runtime?.last_tool || "active")})</span>`
      : isPaused
      ? '<span class="status-chip chip-warn">Paused</span>'
      : `<span class="status-chip chip-muted">${escapeHtml(proj.status)}</span>`;

    const heroHtml =
      `<section class="project-hero">` +
      `<div class="project-hero-top">` +
      `<div>` +
      `<div class="project-hero-meta">` +
      `<span class="project-pid">${escapeHtml(payload.pid)}</span>` +
      `<h1 style="margin:0;font-size:18px;">${escapeHtml(proj.title || payload.pid)}</h1>` +
      `${statusBadge}` +
      `<span class="badge mono">${escapeHtml(prof.harness || "agy")} · ${escapeHtml(prof.model || "default")} · ${escapeHtml(prof.permission_preset || "auto")}</span>` +
      `</div>` +
      `<details style="margin-top:8px;"><summary class="muted" style="cursor:pointer;font-size:12.5px;">Project Goal &amp; Root Ticket (${escapeHtml(proj.id)})</summary>` +
      `<div class="md-rendered" style="margin-top:8px;padding:10px;background:var(--bg-subtle);border-radius:8px;">${renderMarkdown(proj.descriptions)}</div></details>` +
      `</div>` +
      `<div class="project-hero-actions">` +
      `<button type="button" class="btn btn-sm btn-primary" data-open-dialog="create-task-dialog">+ New Ticket</button>` +
      (isRunning
        ? `<button type="button" class="btn btn-sm btn-danger" data-action="stop-worker" data-pid="${escapeHtml(payload.pid)}">Stop Worker</button>`
        : `<button type="button" class="btn btn-sm btn-success" data-action="run-project" data-pid="${escapeHtml(payload.pid)}">Run AI Step</button>`) +
      `<button type="button" class="btn btn-sm btn-secondary" data-action="toggle-pause-project" data-pid="${escapeHtml(payload.pid)}" data-paused="${isPaused ? "1" : "0"}">${isPaused ? "Resume" : "Pause"}</button>` +
      `<button type="button" class="btn btn-sm btn-ghost" data-action="open-task" data-tid="${escapeHtml(proj.id)}">Root / Harness</button>` +
      `</div>` +
      `</div>` +
      `</section>`;

    const tabsHtml =
      `<div class="view-tabs">` +
      `<div class="tab-pills" role="tablist">` +
      `<button type="button" class="tab-pill${appState.activeTab === "board" ? " active" : ""}" data-tab="board">Kanban Board (${tasks.length})</button>` +
      `<button type="button" class="tab-pill${appState.activeTab === "stream" ? " active" : ""}" data-tab="stream">Live Stream</button>` +
      `<button type="button" class="tab-pill${appState.activeTab === "files" ? " active" : ""}" data-tab="files">Files &amp; Preview</button>` +
      `</div>` +
      (appState.activeTab === "board"
        ? `<button type="button" class="btn btn-sm btn-ghost" data-action="toggle-dock">${appState.showDock ? "Hide Live Dock" : "Show Live Dock"}</button>`
        : "") +
      `</div>`;

    let bodyHtml = "";
    if (appState.activeTab === "board") {
      const colsHtml = COLUMNS.map((col) => {
        const colTasks = tasks.filter((t) => col.statuses.includes(t.status));
        return (
          `<section class="kanban-col" data-col-status="${escapeHtml(col.id)}">` +
          `<div class="kanban-col-header">` +
          `<span class="col-title-wrap"><span class="col-dot dot-${escapeHtml(col.id)}"></span>${escapeHtml(col.label)}</span>` +
          `<span class="col-count">${colTasks.length}</span>` +
          `</div>` +
          `<div class="kanban-cards">` +
          (colTasks.length
            ? colTasks.map((t) => renderTicketCardHtml(t, isRunning, runtime)).join("")
            : '<p class="muted" style="font-size:12px;text-align:center;margin:16px 0;">Drop ticket here</p>') +
          `</div>` +
          `</section>`
        );
      }).join("");

      const dockHtml = appState.showDock ? renderStreamPanelHtml(payload.pid, false) : "";
      bodyHtml =
        `<div class="workspace-split${appState.showDock ? " with-dock" : ""}">` +
        `<div class="kanban-board">${colsHtml}</div>` +
        `${dockHtml}` +
        `</div>`;
    } else if (appState.activeTab === "stream") {
      bodyHtml = renderStreamPanelHtml(payload.pid, true);
    } else {
      const treeHtml = renderFileTreeNodesHtml(payload.files || [], appState.fileFilter) || '<p class="muted">No files matching filter.</p>';
      const previewUrl = appState.selectedFilePath ? buildFilePreviewUrl(payload.pid, appState.selectedFilePath) : "";
      bodyHtml =
        `<div class="files-layout">` +
        `<aside class="files-sidebar">` +
        `<input id="file-filter-input" class="search-input" type="search" placeholder="Filter workspace files..." value="${escapeHtml(appState.fileFilter)}">` +
        `<div class="files-tree-scroll">${treeHtml}</div>` +
        `</aside>` +
        `<section class="preview-pane">` +
        `<div class="preview-toolbar">` +
        `<span class="mono" style="font-size:12px;">${escapeHtml(appState.selectedFilePath || "Select a file from the left tree to preview (HTML, Markdown, Code)")}</span>` +
        (previewUrl
          ? `<a class="btn btn-xs btn-secondary" href="${escapeHtml(previewUrl)}" target="_blank" rel="noopener noreferrer">Open in New Tab</a>`
          : "") +
        `</div>` +
        (previewUrl
          ? `<iframe class="preview-iframe" src="${escapeHtml(previewUrl)}" title="Workspace File Preview"></iframe>`
          : '<div class="loading-shell"><p class="muted">Tip: Click any <code>.html</code> game/report or <code>.md</code> file to preview live right here.</p></div>') +
        `</section>` +
        `</div>`;
    }

    root.innerHTML = `${heroHtml}${tabsHtml}${bodyHtml}`;
    const feed = document.getElementById("live-log-feed");
    if (feed && appState.autoScroll) feed.scrollTop = feed.scrollHeight;
  }

  /* Slide-Over Task Inspector */
  async function openTaskInspector(tid) {
    const cleanTid = tid.toUpperCase();
    appState.openTaskId = cleanTid;
    updateHeaderFromState(appState.projectPayload?.system_state);

    const drawer = document.getElementById("task-drawer");
    const body = document.getElementById("task-drawer-body");
    if (!drawer || !body) return;

    body.innerHTML = '<div class="loading-shell"><p class="muted">Loading ticket details...</p></div>';
    if (!drawer.open) drawer.showModal();

    try {
      const [taskData, harnessData] = await Promise.all([
        apiJson("GET", `/api/tasks/${encodeURIComponent(appState.pid)}/${encodeURIComponent(cleanTid)}`),
        appState.harnessesPayload ? Promise.resolve(appState.harnessesPayload) : apiJson("GET", "/api/harnesses"),
      ]);
      appState.harnessesPayload = harnessData;

      const t = taskData.task;
      const eff = taskData.effective_profile || {};
      const hc = t.harness_config || {};
      const meta = t.execution_meta || {};
      const comments = t.comments || [];
      const harnesses = (harnessData.harnesses || []).map((h) => h.name);

      const commentsHtml = comments.length
        ? comments
            .map(
              (c, idx) =>
                `<div class="comment-card">` +
                `<div class="comment-head">` +
                `<div><strong>${escapeHtml(c.author)}</strong> <span class="muted">${escapeHtml(formatTimeShort(c.time))}</span></div>` +
                `<button type="button" class="btn btn-xs btn-ghost" data-action="delete-comment" data-tid="${escapeHtml(t.id)}" data-idx="${idx}">Delete</button>` +
                `</div>` +
                `<div class="md-rendered">${renderMarkdown(c.content)}</div>` +
                `</div>`
            )
            .join("")
        : '<p class="muted">No comments or execution notes yet.</p>';

      body.innerHTML =
        `<div class="drawer-header">` +
        `<div>` +
        `<span class="ticket-id">${escapeHtml(t.id)}</span>` +
        `<h2 id="drawer-title">${escapeHtml(t.title || t.id)}</h2>` +
        `</div>` +
        `<div style="display:flex;gap:6px;align-items:center;">` +
        (t.id !== `${appState.pid}-0`
          ? `<button type="button" class="btn btn-xs btn-danger" data-action="delete-task" data-tid="${escapeHtml(t.id)}">Delete</button>`
          : "") +
        `<button type="button" class="btn-icon" data-close-dialog="task-drawer" aria-label="Close">✕</button>` +
        `</div>` +
        `</div>` +
        `<div class="drawer-body">` +
        `<div class="telemetry-strip">` +
        `<span>Effective: <strong class="mono">${escapeHtml(eff.harness || "agy")}</strong> / <code class="mono">${escapeHtml(eff.model || "default")}</code></span>` +
        `<span>Effort: <code class="mono">${escapeHtml(eff.effort || "high")}</code></span>` +
        `<span>Preset: <code class="mono">${escapeHtml(eff.permission_preset || "auto")}</code></span>` +
        (meta.total_steps
          ? `<span>Steps: <strong>${meta.total_steps}</strong> · Tokens: <strong class="mono">${formatTokens(meta.total_tokens)}</strong> · ${formatDuration(meta.total_duration_seconds)}</span>`
          : "") +
        `</div>` +
        `<form id="task-edit-form" data-tid="${escapeHtml(t.id)}" style="display:flex;flex-direction:column;gap:10px;">` +
        `<label class="field"><span>Title</span><input name="title" value="${escapeHtml(t.title || "")}" required></label>` +
        `<label class="field"><span>Description</span><textarea name="descriptions" rows="4">${escapeHtml(t.descriptions || "")}</textarea></label>` +
        `<div class="field-row">` +
        `<label class="field"><span>Status</span><select name="status">` +
        ["not_started", "in_progress", "need_confirm", "blocked", "complete", "failed"]
          .map((s) => `<option value="${s}"${t.status === s ? " selected" : ""}>${escapeHtml(s)}</option>`)
          .join("") +
        `</select></label>` +
        `<label class="field"><span>Assignee</span><select name="assignee">` +
        ["Admin", "AI"]
          .map((a) => `<option value="${a}"${t.assignee === a ? " selected" : ""}>${escapeHtml(a)}</option>`)
          .join("") +
        `</select></label>` +
        `</div>` +
        `<details style="border:1px solid var(--border);border-radius:8px;padding:10px;">` +
        `<summary style="cursor:pointer;font-weight:600;font-size:12.5px;">Harness &amp; Model Override (optional)</summary>` +
        `<div class="field-row" style="margin-top:10px;">` +
        `<label class="field"><span>Harness Override</span><select name="hc_harness"><option value="">Inherit (${escapeHtml(eff.harness || "agy")})</option>` +
        harnesses.map((h) => `<option value="${escapeHtml(h)}"${hc.harness === h ? " selected" : ""}>${escapeHtml(h)}</option>`).join("") +
        `</select></label>` +
        `<label class="field"><span>Model Override</span><input name="hc_model" value="${escapeHtml(hc.model || "")}" placeholder="Inherit (${escapeHtml(eff.model || "default")})"></label>` +
        `<label class="field"><span>Effort</span><select name="hc_effort"><option value="">Inherit</option>` +
        ["low", "medium", "high", "xhigh"].map((e) => `<option value="${e}"${hc.effort === e ? " selected" : ""}>${e}</option>`).join("") +
        `</select></label>` +
        `<label class="field"><span>Permission Preset</span><select name="hc_preset"><option value="">Inherit</option>` +
        ["auto", "accept-edits", "plan", "turbo"].map((p) => `<option value="${p}"${hc.permission_preset === p ? " selected" : ""}>${p}</option>`).join("") +
        `</select></label>` +
        `<label class="field"><span>Session Mode</span><select name="hc_session"><option value="">Inherit</option>` +
        ["stateless", "sticky"].map((m) => `<option value="${m}"${hc.session_mode === m ? " selected" : ""}>${m}</option>`).join("") +
        `</select></label>` +
        `</div>` +
        `</details>` +
        `<div style="display:flex;flex-wrap:wrap;gap:8px;">` +
        `<button type="submit" class="btn btn-sm btn-primary">Save Changes</button>` +
        `<button type="button" class="btn btn-sm btn-success" data-action="run-task-from-drawer" data-tid="${escapeHtml(t.id)}">Save &amp; Run AI</button>` +
        `<button type="button" class="btn btn-sm btn-secondary" data-action="approve-task" data-tid="${escapeHtml(t.id)}">Approve Done</button>` +
        `</div>` +
        `</form>` +
        `<hr style="border:0;border-top:1px solid var(--border);margin:4px 0;">` +
        `<div>` +
        `<h3 style="margin:0 0 8px;font-size:14px;">Comments &amp; Execution Log (${comments.length})</h3>` +
        `<div class="comments-list">${commentsHtml}</div>` +
        `</div>` +
        `<form id="drawer-comment-form" data-tid="${escapeHtml(t.id)}" style="display:flex;flex-direction:column;gap:8px;margin-top:4px;">` +
        `<label class="field"><span>New Admin Comment</span>` +
        `<textarea name="msg" rows="3" placeholder="Add review feedback or instruction for AI..." required></textarea></label>` +
        `<div style="display:flex;gap:8px;">` +
        `<button type="submit" class="btn btn-sm btn-secondary">Add Comment</button>` +
        `<button type="button" class="btn btn-sm btn-primary" data-action="comment-and-run" data-tid="${escapeHtml(t.id)}">Comment &amp; Run AI</button>` +
        `</div>` +
        `</form>` +
        `</div>`;
    } catch (err) {
      body.innerHTML = `<div class="modal-body"><p class="toast error">${escapeHtml(err.message)}</p></div>`;
    }
  }

  /* Settings Modal */
  async function openSettingsModal() {
    const dlg = document.getElementById("settings-dialog");
    const body = document.getElementById("settings-body");
    if (!dlg || !body) return;

    body.innerHTML = '<p class="muted">Loading orchestrator configuration...</p>';
    dlg.showModal();

    try {
      const [sys, hData] = await Promise.all([
        apiJson("GET", "/api/system_state"),
        apiJson("GET", "/api/harnesses"),
      ]);
      appState.harnessesPayload = hData;
      const curTheme = getStoredTheme();
      const harnesses = hData.harnesses || [{ name: "agy" }];
      const curHarness = sys.default_harness || "agy";
      const curModel = sys.default_model || "default";
      const modelsList = (hData.models && hData.models[curHarness]) || [];

      const modelOptions = [`<option value="default"${curModel === "default" ? " selected" : ""}>default</option>`]
        .concat(
          modelsList.map(
            (m) =>
              `<option value="${escapeHtml(m.id)}"${curModel === m.id ? " selected" : ""}>${escapeHtml(m.name || m.id)} (${escapeHtml(m.id)})</option>`
          )
        )
        .join("");

      body.innerHTML =
        `<div>` +
        `<span style="display:block;font-size:12.5px;font-weight:600;color:var(--text-muted);margin-bottom:6px;">Appearance Theme</span>` +
        `<div class="theme-pills">` +
        `<button type="button" class="theme-pill${curTheme === "system" ? " active" : ""}" data-theme-choice="system">System</button>` +
        `<button type="button" class="theme-pill${curTheme === "light" ? " active" : ""}" data-theme-choice="light">Light</button>` +
        `<button type="button" class="theme-pill${curTheme === "dark" ? " active" : ""}" data-theme-choice="dark">Dark</button>` +
        `</div>` +
        `</div>` +
        `<label class="field"><span>Default AI Harness</span><select name="default_harness">` +
        harnesses.map((h) => `<option value="${escapeHtml(h.name)}"${curHarness === h.name ? " selected" : ""}>${escapeHtml(h.name)}</option>`).join("") +
        `</select></label>` +
        `<label class="field"><span>Default Model</span><select name="default_model">${modelOptions}</select></label>` +
        `<div class="field-row">` +
        `<label class="field"><span>Reasoning Effort</span><select name="default_effort">` +
        ["low", "medium", "high", "xhigh"].map((e) => `<option value="${e}"${sys.default_effort === e ? " selected" : ""}>${e}</option>`).join("") +
        `</select></label>` +
        `<label class="field"><span>Permission Preset</span><select name="default_permission_preset">` +
        ["auto", "accept-edits", "plan", "turbo"]
          .map((p) => `<option value="${p}"${sys.default_permission_preset === p ? " selected" : ""}>${p}</option>`)
          .join("") +
        `</select></label>` +
        `</div>` +
        `<label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;">` +
        `<input type="checkbox" name="watcher_paused"${sys.watcher_paused ? " checked" : ""} style="width:auto;"> Pause Autonomous Watcher Scheduling` +
        `</label>`;
    } catch (err) {
      body.innerHTML = `<p class="muted">Failed to load settings: ${escapeHtml(err.message)}</p>`;
    }
  }

  /* Data Loaders & SSE Connection */
  async function loadCurrentViewData(preserveEditing = false) {
    if (preserveEditing && isUserEditing()) return;
    if (appState.page === "dashboard") {
      appState.projectsPayload = await apiJson("GET", "/api/projects");
      renderDashboard();
    } else {
      appState.projectPayload = await apiJson("GET", `/api/projects/${encodeURIComponent(appState.pid)}`);
      if (!appState.selectedFilePath && appState.projectPayload.files?.length) {
        const findHtml = (nodes) => {
          for (const n of nodes) {
            if (n.type === "file" && (n.ext === ".html" || n.ext === ".htm")) return n.path;
            if (n.type === "dir") {
              const nested = findHtml(n.children || []);
              if (nested) return nested;
            }
          }
          return "";
        };
        appState.selectedFilePath = findHtml(appState.projectPayload.files);
      }
      renderProjectWorkspace();
    }
  }

  function scheduleBoardRefresh() {
    if (appState.refreshTimer) clearTimeout(appState.refreshTimer);
    appState.refreshTimer = setTimeout(() => {
      loadCurrentViewData(true).catch(() => {});
    }, 180);
  }

  function connectSse() {
    if (appState.eventSource) {
      appState.eventSource.close();
      appState.eventSource = null;
    }
    const qs = appState.pid ? `?pid=${encodeURIComponent(appState.pid)}` : "";
    const es = new EventSource(`/api/events/stream${qs}`);
    appState.eventSource = es;

    es.onopen = () => setSseStatus("chip-live", "Live");
    es.onerror = () => setSseStatus("chip-warn", "Reconnecting");

    es.addEventListener("snapshot", (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        appState.events = snap.events || [];
        if (snap.system_state) updateHeaderFromState(snap.system_state);
        const feed = document.getElementById("live-log-feed");
        if (feed) {
          feed.innerHTML = renderLogEntriesHtml(appState.events);
          if (appState.autoScroll) feed.scrollTop = feed.scrollHeight;
        }
        setSseStatus("chip-live", "Live");
      } catch {}
    });

    ["step_start", "tool", "text_delta", "step_finish", "board_change", "control_action"].forEach((evType) => {
      es.addEventListener(evType, (ev) => {
        try {
          const item = JSON.parse(ev.data);
          appendStreamEventToDom(item);
          if (evType === "step_start" || evType === "step_finish" || evType === "board_change") {
            scheduleBoardRefresh();
          }
        } catch {}
      });
    });
  }

  /* Event Delegation */
  function setupEventHandlers() {
    const taskDrawer = document.getElementById("task-drawer");
    if (taskDrawer) {
      taskDrawer.addEventListener("close", () => {
        appState.openTaskId = "";
        updateHeaderFromState(appState.projectPayload?.system_state);
      });
    }

    document.addEventListener("click", async (ev) => {
      const openDlg = ev.target.closest("[data-open-dialog]");
      if (openDlg) {
        const dlg = document.getElementById(openDlg.dataset.openDialog);
        if (dlg && !dlg.open) dlg.showModal();
        return;
      }

      if (ev.target.closest("#settings-btn")) {
        openSettingsModal();
        return;
      }

      if (ev.target.closest("#watcher-toggle-btn")) {
        try {
          const sys = await apiJson("GET", "/api/system_state");
          const updated = await apiJson("POST", "/api/system_state", { watcher_paused: !sys.watcher_paused });
          updateHeaderFromState(updated);
          showToast(updated.watcher_paused ? "Watcher paused" : "Watcher resumed");
          scheduleBoardRefresh();
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const themeBtn = ev.target.closest("[data-theme-choice]");
      if (themeBtn) {
        applyTheme(themeBtn.dataset.themeChoice);
        return;
      }

      const tabBtn = ev.target.closest("[data-tab]");
      if (tabBtn) {
        appState.activeTab = tabBtn.dataset.tab;
        renderProjectWorkspace();
        return;
      }

      const fileBtn = ev.target.closest("[data-file-path]");
      if (fileBtn) {
        appState.selectedFilePath = fileBtn.dataset.filePath;
        renderProjectWorkspace();
        return;
      }

      const actionEl = ev.target.closest("[data-action]");
      if (actionEl) {
        ev.stopPropagation();
        const act = actionEl.dataset.action;
        const pid = actionEl.dataset.pid || appState.pid;
        const tid = actionEl.dataset.tid;
        try {
          if (act === "toggle-dock") {
            appState.showDock = !appState.showDock;
            localStorage.setItem("todox_show_dock", String(appState.showDock));
            renderProjectWorkspace();
          } else if (act === "clear-stream-view") {
            appState.events = [];
            const feed = document.getElementById("live-log-feed");
            if (feed) feed.innerHTML = renderLogEntriesHtml([]);
          } else if (act === "run-project") {
            await apiJson("POST", `/api/projects/${encodeURIComponent(pid)}/run`);
            showToast(`Queued AI run for ${pid}`);
            scheduleBoardRefresh();
          } else if (act === "stop-worker") {
            await apiJson("POST", `/api/workers/${encodeURIComponent(pid)}/stop`);
            showToast(`Stop signal sent to ${pid}`);
          } else if (act === "toggle-pause-project") {
            const paused = actionEl.dataset.paused === "1";
            await apiJson("POST", "/api/system_state", paused ? { resume_project: pid } : { pause_project: pid });
            showToast(paused ? `Resumed ${pid}` : `Paused ${pid}`);
            scheduleBoardRefresh();
          } else if (act === "delete-project") {
            if (!window.confirm(`Delete project ${pid} and all of its tickets?`)) return;
            await apiJson("DELETE", `/api/projects/${encodeURIComponent(pid)}`);
            showToast(`Deleted project ${pid}`);
            await loadCurrentViewData(false);
          } else if (act === "run-task") {
            await apiJson("POST", `/api/tasks/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}/run`);
            showToast(`Assigned ${tid} to AI`);
            scheduleBoardRefresh();
          } else if (act === "approve-task") {
            await apiJson("POST", `/api/tasks/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}/approve`, {});
            showToast(`Approved ${tid}`);
            if (taskDrawer && taskDrawer.open) await openTaskInspector(tid);
            scheduleBoardRefresh();
          } else if (act === "delete-task") {
            if (!window.confirm(`Delete ticket ${tid}?`)) return;
            await apiJson("DELETE", `/api/tasks/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}`);
            if (taskDrawer && taskDrawer.open) taskDrawer.close();
            showToast(`Deleted ${tid}`);
            await loadCurrentViewData(false);
          } else if (act === "open-task") {
            await openTaskInspector(tid);
          } else if (act === "delete-comment") {
            await apiJson("DELETE", `/api/comments/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}/${actionEl.dataset.idx}`);
            await openTaskInspector(tid);
            scheduleBoardRefresh();
          } else if (act === "run-task-from-drawer") {
            const form = document.getElementById("task-edit-form");
            if (form) await saveTaskForm(form);
            await apiJson("POST", `/api/tasks/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}/run`);
            showToast(`Saved & queued ${tid} for AI`);
            await openTaskInspector(tid);
            scheduleBoardRefresh();
          } else if (act === "comment-and-run") {
            const cForm = document.getElementById("drawer-comment-form");
            const msg = cForm?.querySelector('textarea[name="msg"]')?.value.trim();
            if (!msg) {
              showToast("Write a comment first", true);
              return;
            }
            await apiJson("POST", `/api/comments/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}`, { msg });
            await apiJson("POST", `/api/tasks/${encodeURIComponent(pid)}/${encodeURIComponent(tid)}/run`);
            showToast(`Commented & queued ${tid} for AI`);
            await openTaskInspector(tid);
            scheduleBoardRefresh();
          }
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const card = ev.target.closest(".ticket-card[data-tid]");
      if (card) {
        openTaskInspector(card.dataset.tid);
      }
    });

    document.addEventListener("input", (ev) => {
      if (ev.target.id === "file-filter-input") {
        appState.fileFilter = ev.target.value;
        const container = document.querySelector(".files-tree-scroll");
        if (container && appState.projectPayload) {
          container.innerHTML =
            renderFileTreeNodesHtml(appState.projectPayload.files || [], appState.fileFilter) ||
            '<p class="muted">No files matching filter.</p>';
        }
      }
    });

    document.addEventListener("change", (ev) => {
      if (ev.target.name === "default_harness" && ev.target.closest("#settings-form")) {
        const modelSelect = document.querySelector('#settings-form select[name="default_model"]');
        const modelsList = (appState.harnessesPayload?.models && appState.harnessesPayload.models[ev.target.value]) || [];
        if (modelSelect) {
          modelSelect.innerHTML = ['<option value="default">default</option>']
            .concat(
              modelsList.map(
                (m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name || m.id)} (${escapeHtml(m.id)})</option>`
              )
            )
            .join("");
        }
      }
    });

    /* Kanban Drag and Drop */
    document.addEventListener("dragstart", (ev) => {
      const card = ev.target.closest(".ticket-card[data-tid]");
      if (!card) return;
      appState.dragTid = card.dataset.tid;
      card.classList.add("dragging");
      if (ev.dataTransfer) {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", appState.dragTid);
      }
    });

    document.addEventListener("dragend", () => {
      appState.dragTid = "";
      document.querySelectorAll(".ticket-card.dragging").forEach((el) => el.classList.remove("dragging"));
      document.querySelectorAll(".kanban-col.drag-over").forEach((el) => el.classList.remove("drag-over"));
    });

    document.addEventListener("dragover", (ev) => {
      const col = ev.target.closest(".kanban-col[data-col-status]");
      if (!col || !appState.dragTid) return;
      ev.preventDefault();
      col.classList.add("drag-over");
    });

    document.addEventListener("dragleave", (ev) => {
      const col = ev.target.closest(".kanban-col[data-col-status]");
      if (col && !col.contains(ev.relatedTarget)) {
        col.classList.remove("drag-over");
      }
    });

    document.addEventListener("drop", async (ev) => {
      const col = ev.target.closest(".kanban-col[data-col-status]");
      if (!col) return;
      ev.preventDefault();
      col.classList.remove("drag-over");
      const tid = appState.dragTid || (ev.dataTransfer ? ev.dataTransfer.getData("text/plain") : "");
      const newStatus = col.dataset.colStatus;
      if (!tid || !newStatus) return;

      const existing = appState.projectPayload?.tasks?.find((t) => t.id === tid);
      if (existing && existing.status === newStatus) return;

      const payload = { status: newStatus };
      if (newStatus === "in_progress") payload.assignee = "AI";
      if (newStatus === "complete") payload.assignee = "Admin";
      try {
        await apiJson("PUT", `/api/tasks/${encodeURIComponent(appState.pid)}/${encodeURIComponent(tid)}`, payload);
        showToast(`Moved ${tid} → ${newStatus}`);
        await loadCurrentViewData(false);
      } catch (err) {
        showToast(err.message, true);
      }
    });

    /* Forms */
    document.addEventListener("submit", async (ev) => {
      const form = ev.target;
      ev.preventDefault();
      try {
        if (form.id === "create-project-form") {
          const fd = new FormData(form);
          const pid = String(fd.get("pid") || "").trim().toUpperCase();
          await apiJson("POST", "/api/projects", {
            pid,
            title: String(fd.get("title") || "").trim(),
            desc: String(fd.get("desc") || "").trim(),
          });
          form.reset();
          document.getElementById("create-project-dialog")?.close();
          window.location.href = `/project/${encodeURIComponent(pid)}`;
        } else if (form.id === "create-task-form") {
          const fd = new FormData(form);
          const res = await apiJson("POST", `/api/tasks/${encodeURIComponent(appState.pid)}/${encodeURIComponent(appState.pid)}-0`, {
            title: String(fd.get("title") || "").trim(),
            desc: String(fd.get("desc") || "").trim(),
            status: String(fd.get("status") || "not_started"),
            assignee: String(fd.get("assignee") || "Admin"),
          });
          form.reset();
          document.getElementById("create-task-dialog")?.close();
          showToast(`Created ticket ${res.tid}`);
          await loadCurrentViewData(false);
        } else if (form.id === "task-edit-form") {
          await saveTaskForm(form);
          showToast(`Saved ${form.dataset.tid}`);
          await openTaskInspector(form.dataset.tid);
          await loadCurrentViewData(false);
        } else if (form.id === "drawer-comment-form") {
          const tid = form.dataset.tid;
          const msg = String(new FormData(form).get("msg") || "").trim();
          if (!msg) return;
          await apiJson("POST", `/api/comments/${encodeURIComponent(appState.pid)}/${encodeURIComponent(tid)}`, { msg });
          await openTaskInspector(tid);
          scheduleBoardRefresh();
        } else if (form.id === "steer-form") {
          const pid = form.dataset.pid || appState.pid;
          const input = form.querySelector('input[name="msg"]');
          const msg = String(input?.value || "").trim();
          if (!msg) return;
          await apiJson("POST", `/api/workers/${encodeURIComponent(pid)}/steer`, { msg });
          if (input) input.value = "";
          showToast(`Steering sent to ${pid}`);
        } else if (form.id === "settings-form") {
          const fd = new FormData(form);
          await apiJson("POST", "/api/system_state", {
            default_harness: fd.get("default_harness"),
            default_model: fd.get("default_model"),
            default_effort: fd.get("default_effort"),
            default_permission_preset: fd.get("default_permission_preset"),
            watcher_paused: form.querySelector('input[name="watcher_paused"]')?.checked || false,
          });
          document.getElementById("settings-dialog")?.close();
          showToast("Orchestrator settings updated");
          await loadCurrentViewData(false);
        }
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  async function saveTaskForm(form) {
    const tid = form.dataset.tid;
    const fd = new FormData(form);
    const harnessConfig = {
      harness: String(fd.get("hc_harness") || "").trim(),
      model: String(fd.get("hc_model") || "").trim(),
      effort: String(fd.get("hc_effort") || "").trim(),
      permission_preset: String(fd.get("hc_preset") || "").trim(),
      session_mode: String(fd.get("hc_session") || "").trim(),
    };
    await apiJson("PUT", `/api/tasks/${encodeURIComponent(appState.pid)}/${encodeURIComponent(tid)}`, {
      title: String(fd.get("title") || ""),
      descriptions: String(fd.get("descriptions") || ""),
      status: String(fd.get("status") || "not_started"),
      assignee: String(fd.get("assignee") || "Admin"),
      harness_config: harnessConfig,
    });
  }

  async function init() {
    applyTheme(getStoredTheme());
    setupDialogDismissFallbacks();
    setupEventHandlers();
    connectSse();
    try {
      await loadCurrentViewData(false);
      if (appState.page === "task" && appState.tid) {
        await openTaskInspector(appState.tid);
      }
    } catch (err) {
      const root = document.getElementById("app-root");
      if (root) {
        root.innerHTML = `<div class="loading-shell"><p class="toast error">Failed to load TodoX data: ${escapeHtml(err.message)}</p></div>`;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
