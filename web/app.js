"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [
    ...root.querySelectorAll(selector),
  ];
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const icon = (name, extra = "") =>
    `<svg class="icon ${esc(extra)}" aria-hidden="true"><use href="/static/icons.svg#${esc(name)}"/></svg>`;
  const query = (values) =>
    new URLSearchParams(
      Object.fromEntries(
        Object.entries(values).filter(([, v]) => v !== undefined && v !== null),
      ),
    ).toString();
  const list = (value) => (Array.isArray(value) ? value : value ? [value] : []);
  const number = (value) => Number(value || 0).toLocaleString();
  const repoName = (repo) => repo.full_name || `${repo.owner}/${repo.name}`;
  const jobState = (job) => String(job.state || "unknown").toLowerCase();
  const isActive = (job) =>
    [
      "received",
      "queued",
      "retrieving",
      "working",
      "verifying",
      "responding",
      "running",
      "pending",
      "stopping",
    ].includes(jobState(job));
  const canRetry = (job) => ["failed", "stopped"].includes(jobState(job));
  const isComplete = (job) =>
    ["done", "completed", "succeeded"].includes(jobState(job));
  const labelState = (value) =>
    String(value || "").toLowerCase() === "received"
      ? "Queued"
      : String(value || "unknown")
          .replace(/[_-]/g, " ")
          .replace(/^\w/, (c) => c.toUpperCase());
  const stateClass = (value) => {
    value = String(value || "").toLowerCase();
    if (
      ["done", "completed", "succeeded", "active", "running", "pass"].includes(
        value,
      )
    )
      return "success";
    if (["failed", "error", "fail"].includes(value)) return "error";
    if (
      ["stopped", "cancelled", "paused", "warn", "stopping", "waiting-on-you"].includes(
        value,
      )
    )
      return "warning";
    if (["working", "retrieving", "verifying", "responding"].includes(value))
      return "running";
    return "";
  };
  const badge = (state) =>
    `<span class="badge badge-${stateClass(state)}">${esc(labelState(state))}</span>`;
  const jobBadge = (job) =>
    job.notes?.result_export_error
      ? '<span class="badge badge-warning">Results need attention</span>'
      : badge(job.state);
  const date = (value) => {
    if (!value) return "Not recorded";
    const d = new Date(
      typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : value,
    );
    return Number.isNaN(d.getTime())
      ? String(value)
      : new Intl.DateTimeFormat(undefined, {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        }).format(d);
  };
  const ago = (value) => {
    if (!value) return "Time not recorded";
    const d = new Date(
      typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : value,
    );
    if (Number.isNaN(d.getTime())) return date(value);
    const minutes = Math.floor((Date.now() - d.getTime()) / 60000);
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} hr ago`;
    return date(value);
  };
  const targetURL = (repo, kind, n) =>
    /^[\w.-]+\/[\w.-]+$/.test(repo || "") && /^\d+$/.test(String(n))
      ? `https://github.com/${encodeURI(repo)}/${(taskType(kind)?.takes || (kind === "fix-pr" ? "pr" : "issue")) === "pr" ? "pull" : "issues"}/${n}`
      : "#";
  const button = (text, action, name, cls = "", attrs = "") =>
    `<button type="button" class="button ${cls}" data-action="${esc(action)}" ${attrs}>${name ? icon(name) : ""}${esc(text)}</button>`;
  const empty = (name, title, description, action = "") =>
    `<div class="empty-state"><div class="empty-icon">${icon(name)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
  const loading = (text) =>
    `<div class="inline-loader" role="status"><span class="spinner"></span>${esc(text)}</div>`;
  const info = (text, type = "") =>
    `<div class="inline-notice ${type}">${icon(type === "warning" ? "alert" : "info")}<span>${text}</span></div>`;
  function targetContext(target) {
    if (!target) return "";
    const comments = list(target.comments?.nodes || target.comments);
    return `<details class="source-context" open><summary>Read the ${target.__typename === "Issue" ? "issue" : "work item"} description${comments.length ? " and discussion" : ""}</summary><div><p class="source-body">${esc(target.body || "This item has no description.")}</p>${comments.map((c) => `<div class="source-comment"><strong>${esc(c.author?.login || c.user?.login || c.author || "GitHub comment")}</strong><p class="source-body">${esc(c.body || "")}</p></div>`).join("")}</div></details>`;
  }
  const A = {
    state: null,
    view: "overview",
    jobsSearch: "",
    coverageSearch: "",
    jobsFilter: "all",
    repoFilter: "all",
    dialog: null,
    dialogVersion: 0,
    commandUsersVersion: 0,
    wizard: null,
    folder: null,
    settingsDirty: false,
    refreshing: false,
    connected: false,
    diagnostics: null,
    githubTimer: null,
    taskTypes: null,
    taskTypesRequest: null,
    digest: null,
    fence: null,
    hintShown: { text: "", at: 0 },
  };
  const views = [
    ["overview", "Overview", "overview"],
    ["jobs", "Tasks", "jobs"],
    ["fleet", "Agent fleet", "spark"],
    ["doorbell", "Updates & decisions", "watch"],
    ["learn", "Explore & learn", "help"],
    ["repos", "Repositories", "repo"],
    ["watches", "Review watches", "watch"],
    ["fence", "Checks before publishing", "shield"],
    ["settings", "Settings", "settings"],
    ["help", "Help & getting started", "help"],
  ];
  const commandUserNames = new Map();
  const main = $("#main");
  const dialog = $("#app-dialog");
  const folderDialog = $("#folder-dialog");

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
      this.hint = "";
    }
  }
  async function api(path, data, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      options.timeout || 30000,
    );
    try {
      const response = await fetch(path, {
        method: data === undefined ? "GET" : "POST",
        headers: (() => {
          const headers =
            data === undefined
              ? { Accept: "application/json" }
              : {
                  "Content-Type": "application/json",
                  Accept: "application/json",
                };
          const token = sessionStorage.getItem("fatchin-token");
          if (token) headers.Authorization = `Bearer ${token}`;
          return headers;
        })(),
        body: data === undefined ? undefined : JSON.stringify(data),
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      let result;
      try {
        result = await response.json();
      } catch (error) {
        if (error.name === "AbortError") throw error;
        throw new ApiError(
          "The worker returned an unreadable response. Refresh the page and try again.",
          response.status,
        );
      }
      if (!response.ok) {
        if (response.status === 401 && !options.retried) {
          const supplied = window.prompt(
            "This FatChin instance requires an admin token.",
          );
          if (supplied && supplied.trim()) {
            sessionStorage.setItem("fatchin-token", supplied.trim());
            return api(path, data, { ...options, retried: true });
          }
        }
        const failure = new ApiError(
          result.message ||
            result.error ||
            `The request failed (${response.status}).`,
          response.status,
        );
        failure.hint = typeof result.hint === "string" ? result.hint : "";
        throw failure;
      }
      if (typeof result.hint === "string") noticeHint(result.hint);
      return result;
    } catch (error) {
      if (error.name === "AbortError")
        throw new ApiError(
          data === undefined
            ? "The worker took too long to respond. Check its connection and try again."
            : "The worker took too long to confirm this action. Refresh status before trying again; the action may already have completed.",
          0,
        );
      if (error instanceof ApiError) throw error;
      throw new ApiError(
        data === undefined
          ? "Cannot reach the worker. Check your connection, then try again."
          : "The connection ended before the worker confirmed this action. Refresh status before trying again; the action may already have completed.",
        0,
      );
    } finally {
      clearTimeout(timeout);
    }
  }
  function toast(message, error = false) {
    const el = document.createElement("div");
    el.className = `toast${error ? " error" : ""}`;
    el.innerHTML = `${icon(error ? "alert" : "check-circle")}<span>${esc(message)}</span><button class="icon-button" aria-label="Dismiss notification">${icon("x")}</button>`;
    $("button", el).addEventListener("click", () => el.remove());
    $("#toast-region").append(el);
    setTimeout(() => el.remove(), error ? 14000 : 7000);
  }
  function reportError(error) {
    toast(error.message, true);
    if (error.status === 409) refresh(true);
  }
  // A reply may carry a next-step hint. It never blocks anything: a repeated
  // identical hint (the page refreshes every 10 seconds) waits a minute.
  function noticeHint(text) {
    const value = String(text || "").trim();
    if (!value) return;
    const now = Date.now();
    if (A.hintShown.text === value && now - A.hintShown.at < 60000) return;
    A.hintShown = { text: value, at: now };
    toast(value);
  }
  function dialogError(error, root = $("#dialog-body")) {
    $(".dialog-error", root)?.remove();
    const el = document.createElement("div");
    el.className = "dialog-error";
    el.setAttribute("role", "alert");
    el.textContent = error.message;
    root.prepend(el);
    root.scrollTop = 0;
    if (error.status === 409) refresh(true);
  }
  async function busy(element, fn) {
    if (element?.disabled) return;
    const wasDisabled = element?.disabled;
    if (element) {
      element.disabled = true;
      element.setAttribute("aria-busy", "true");
    }
    try {
      return await fn();
    } catch (error) {
      reportError(error);
    } finally {
      if (element?.isConnected) {
        element.disabled = wasDisabled;
        element.removeAttribute("aria-busy");
      }
    }
  }
  function repos() {
    return A.state?.repos || [];
  }
  function jobs() {
    return [...(A.state?.jobs || [])].sort(
      (a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0),
    );
  }
  function watches() {
    return A.state?.watches || [];
  }
  function workspaceFleet() {
    return A.state?.fleet_summary || {};
  }
  function watchedRepositories() {
    return list(workspaceFleet().repositories);
  }
  function workspaceRepositoryCount() {
    return new Set([...repos().map(repoName),
        ...watchedRepositories().map((r) => r.repo), ...watchedRepositories().map((r) => r.repo)]).size;
  }
  function fleetWorkspaceStatus() {
    const fleet = workspaceFleet();
    if (!list(fleet.fleets).length) return "";
    const active = fleet.active_agents || {}, configured = fleet.configured_agents || {};
    const error = fleet.supervisor?.discovery_error || list(fleet.fleets).map((f) => f.error).find(Boolean);
    return `<section id="workspace-fleet-status" class="panel workspace-fleet-status" aria-label="Live account-wide fleet"><div><strong>${icon("watch")}${number(fleet.repository_count)} repositories in your fleet</strong><span>${esc(list(fleet.fleets).every((f) => f.status === "paused") ? "Paused after current tasks" : "Watching PRs, comments and code · at most 4 agents per repository")}</span></div><div class="workspace-provider"><strong>GLM</strong><span>${number(active.glm)} working · ${number(configured.glm)} configured</span></div><div class="workspace-provider"><strong>DeepSeek</strong><span>${number(active.deepseek)} working · ${number(configured.deepseek)} configured</span></div><a class="button button-small" href="#fleet">Open agent fleet ${icon("arrow")}</a>${error ? `<p class="field-error">${esc(error)}</p>` : ""}</section>`;
  }
  function coveragePanel() {
    const all = watchedRepositories();
    if (!list(workspaceFleet().fleets).length) return "";
    const rows = all.filter((r) => r.repo.toLowerCase().includes(A.coverageSearch.toLowerCase()));
    return `<section class="panel fleet-coverage"><div class="panel-header"><div><h2>Watched repositories <span class="count">${number(all.length)}</span></h2><p>Every project discovered in your fleet’s current scope. New accessible projects join future scans.</p></div><a href="#fleet">Manage fleet ${icon("arrow")}</a></div><div class="toolbar"><div class="search-field">${icon("search")}<input id="coverage-search" type="search" aria-label="Search watched repositories" placeholder="Find a watched repository…" value="${esc(A.coverageSearch)}"></div><span class="muted">${number(rows.length)} of ${number(all.length)} projects</span></div><div class="coverage-scroll"><table class="coverage-table"><thead><tr><th>Repository</th><th>Following</th><th>Work</th><th>Status</th></tr></thead><tbody>${rows.map((r) => `<tr><td><a href="https://github.com/${encodeURI(r.repo).replace(/"/g, "%22")}" target="_blank" rel="noopener noreferrer">${esc(r.repo)} ${icon("external")}</a></td><td>Pull requests, comments and code</td><td>${number(r.tasks?.running)} active · ${number(r.tasks?.queued)} queued${r.tasks?.blocked ? `<span class="cell-sub">${number(r.tasks.blocked)} need attention</span>` : ""}</td><td>${badge(r.status)}</td></tr>`).join("")}</tbody></table>${rows.length ? "" : empty("repo", all.length ? "No matching repository" : "Discovering your repositories", all.length ? "Try another project name." : "The fleet is reading the repositories accessible to your connected GitHub account.")}</div></section>`;
  }
  function fleetTasksPanel() {
    const fleet = workspaceFleet();
    if (!list(fleet.fleets).length) return "";
    const rows = list(fleet.recent_tasks).filter((i) =>
      (A.repoFilter === "all" || i.repo === A.repoFilter) &&
      (A.jobsFilter === "all" || (A.jobsFilter === "active" ? ["queued","running","retrying"].includes(i.status) : A.jobsFilter === "failed" ? ["blocked","failed"].includes(i.status) : i.status === A.jobsFilter)) &&
      `${i.title} ${i.repo} ${i.kind}`.toLowerCase().includes(A.jobsSearch.toLowerCase()));
    return `<section class="panel"><div class="panel-header"><div><h2>Fleet tasks</h2><p>${number(fleet.task_counts?.running)} working · ${number(fleet.task_counts?.queued)} queued · ${number(fleet.task_counts?.blocked)} need attention. Showing recent work; use the fleet window for the full queue.</p></div><a href="#fleet">All fleet work ${icon("arrow")}</a></div><div class="table-wrap"><table class="jobs-table fleet-task-table"><thead><tr><th>Task</th><th>Repository</th><th>Agent</th><th>Status</th><th>Next step</th></tr></thead><tbody>${rows.map((i) => `<tr><td>${esc(i.title || labelState(i.kind))}<span class="cell-sub">${esc(labelState(i.kind))}</span></td><td>${esc(i.repo)}</td><td>${esc(i.provider === "deepseek" ? "DeepSeek" : i.provider === "glm" ? "GLM" : "Waiting for an agent")}${i.slot ? ` · ${esc(i.slot)}` : ""}</td><td>${badge(i.status)}</td><td><button class="button button-small" data-action="fleet-open-item" data-repo="${esc(i.repo)}" data-title="${esc(i.title || "")}">${i.status === "blocked" ? "Read issue and continue" : "Follow in agent fleet"} ${icon("arrow")}</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }
  function settings() {
    return A.state?.settings || {};
  }
  function fleetSettingsPanel() {
    const fleets = list(workspaceFleet().fleets);
    if (!fleets.length) return "";
    return `<section class="panel settings-section"><div class="panel-header"><div><h2>Account-wide fleet controls</h2><p>These controls apply to both GLM and DeepSeek across your watched repositories.</p></div></div><div class="panel-body">${fleets.map((f) => `<div class="fleet-control-summary"><div><strong>${esc(f.account)}</strong><span>Reports: ${esc(f.output_dir)}</span></div><div class="heading-actions">${button(f.status === "paused" ? "Resume fleet" : "Pause after current tasks", f.status === "paused" ? "fleet-resume" : "fleet-pause", f.status === "paused" ? "play" : "pause", "button-small", `data-id="${esc(f.id)}"`)}${button("Stop fleet", "fleet-stop", "stop", "button-small", `data-id="${esc(f.id)}"`)}<a class="button button-small" href="#fleet">Agent counts and repository scope</a></div></div>`).join("")}</div></section>`;
  }
  function model() {
    return { ...(settings().model || {}), ...(A.state?.model || {}) };
  }
  // The kinds of work come from the worker once per page load. renderView()
  // rebuilds the page every 10 seconds; it reads this cache, never the API.
  function taskTypes() {
    return list(A.taskTypes);
  }
  function taskType(id) {
    return taskTypes().find((type) => type.id === id) || null;
  }
  function kindLabel(kind) {
    return (
      taskType(kind)?.label ||
      (kind === "fix-pr"
        ? "Fix a pull request"
        : kind === "implement-issue"
          ? "Implement an issue"
          : String(kind || "Task"))
    );
  }
  function loadTaskTypes() {
    if (A.taskTypes) return Promise.resolve(A.taskTypes);
    if (!A.taskTypesRequest)
      A.taskTypesRequest = api("/api/task-types")
        .then((reply) => (A.taskTypes = list(reply.items)))
        .finally(() => {
          A.taskTypesRequest = null;
        });
    return A.taskTypesRequest;
  }
  function rememberFocus(root) {
    const focused = document.activeElement;
    if (!focused || focused === root || !root.contains(focused))
      return () => {};
    const id = focused.id,
      tag = focused.tagName,
      href = focused.getAttribute("href");
    const action = focused.dataset.action;
    const identity =
      focused.dataset.id || focused.dataset.repo || focused.dataset.watchKey;
    const text = focused.textContent.trim();
    return () => {
      const candidate = id
        ? document.getElementById(id)
        : $$("a,button,input,select,textarea,summary,[tabindex]", root).find(
            (element) => {
              if (element.tagName !== tag) return false;
              if (href !== null) return element.getAttribute("href") === href;
              if (action) {
                if (element.dataset.action !== action) return false;
                return identity
                  ? (element.dataset.id ||
                      element.dataset.repo ||
                      element.dataset.watchKey) === identity
                  : element.textContent.trim() === text;
              }
              return element.textContent.trim() === text;
            },
          );
      if (candidate && root.contains(candidate) && !candidate.disabled)
        candidate.focus({ preventScroll: true });
    };
  }
  function renderNav() {
    const restoreFocus = rememberFocus($("#navigation"));
    $("#navigation").innerHTML = views
      .map(
        ([id, label, name]) =>
          `${id === "settings" ? '<hr class="nav-separator">' : ""}<a class="nav-link${A.view === id ? " active" : ""}" href="#${id}"${A.view === id ? ' aria-current="page"' : ""}>${icon(name)}<span>${esc(label)}</span>${id === "jobs" && (jobs().filter(isActive).length + Number(workspaceFleet().task_counts?.running || 0)) ? `<span class="count">${number(jobs().filter(isActive).length + Number(workspaceFleet().task_counts?.running || 0))}</span>` : ""}</a>`,
      )
      .join("");
    restoreFocus();
    const view = views.find((v) => v[0] === A.view);
    $("#breadcrumb").innerHTML =
      `FatChin <span>/</span> <strong>${esc(view?.[1] || "Overview")}</strong>`;
    document.title = `${view?.[1] || "Overview"} · FatChin`;
  }
  function renderConnection() {
    const service = A.state?.service || {};
    const active = !!service.active;
    const fleetWorking = Number(workspaceFleet().active_agents?.glm || 0) + Number(workspaceFleet().active_agents?.deepseek || 0);
    const status = A.connected && list(workspaceFleet().fleets).some((f) => f.status === "running") ? `${number(fleetWorking)} agents working · ${number(workspaceFleet().repository_count)} projects` : A.connected
      ? active
        ? service.polling_enabled === false
          ? "Polling paused"
          : "Worker running"
        : "Worker stopped"
      : "Connection lost";
    const color = A.connected && list(workspaceFleet().fleets).some((f) => f.status === "running") ? "green" : A.connected
      ? active
        ? service.polling_enabled === false
          ? "amber"
          : "green"
        : "muted"
      : "red";
    $("#sidebar-service").innerHTML =
      `<span class="status-dot ${color}"></span>${esc(status)}`;
    $("#live-label").innerHTML =
      `<span class="status-dot ${color}"></span>${esc(A.connected ? "Live status" : "Connection lost")}`;
    $("#app-version").textContent = A.state?.version
      ? `Version ${A.state.version}`
      : "";
  }
  async function refresh(render = true) {
    if (A.refreshing) {
      const refreshed = await A.refreshing;
      if (refreshed && render && !(A.view === "settings" && A.settingsDirty))
        renderView();
      return refreshed;
    }
    A.refreshing = (async () => {
      try {
        A.state = await api("/api/state");
        A.connected = true;
        renderConnection();
        renderNav();
        if ($("#workspace-fleet-status")) $("#workspace-fleet-status").outerHTML = fleetWorkspaceStatus();
        $("#last-updated").textContent =
          `Updated ${new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date())} · Refreshes every 10 seconds`;
        if (render && !(A.view === "settings" && A.settingsDirty)) renderView();
        return true;
      } catch (error) {
        A.connected = false;
        renderConnection();
        $("#last-updated").textContent =
          "Connection lost · Reconnecting automatically";
        if (!A.state)
          main.innerHTML = empty(
            "alert",
            "Your worker is unavailable",
            error.message,
            button("Try again", "refresh", "refresh", "button-primary"),
          );
        else if (render) toast(error.message, true);
        return false;
      } finally {
        A.refreshing = null;
      }
    })();
    return A.refreshing;
  }
  function pageHeading(title, description, actions = "") {
    return `<div class="page-heading"><div><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ""}</div>`;
  }
  function pendingBanner() {
    return A.state?.service?.restart_required
      ? `<div class="banner banner-warning">${icon("alert")}<div><strong>Saved changes are waiting for a restart</strong>Restart the worker to apply repository and settings changes. A restart requires an idle worker. Stop queued or running tasks first, then wait for the current step to finish.</div>${button("Restart worker", "service-restart", "refresh", "button-small")}</div>`
      : "";
  }
  function activity(job) {
    const title =
      job.notes?.title ||
      `${taskType(job.kind)?.takes === "pr" || job.kind === "fix-pr" ? "Pull request" : "Issue"} #${job.number}`;
    return `<div class="activity-row"><div class="activity-icon">${icon((taskType(job.kind)?.takes || (job.kind === "fix-pr" ? "pr" : "issue")) === "pr" ? "pr" : "issue")}</div><div class="activity-info"><button class="row-button" data-action="job-detail" data-id="${esc(job.job_id)}"><strong>${esc(title)}</strong><small><span>${esc(job.repo_full_name || job.repo)} #${esc(job.number)}</span><span>·</span><span>${esc(kindLabel(job.kind))}</span></small></button></div><div class="activity-meta">${jobBadge(job)}<small>${esc(ago(job.updated_at || job.created_at))}</small></div></div>`;
  }
  function renderOverview() {
    const state = A.state;
    const s = state.service || {};
    const m = model();
    const summary = state.summary || {};
    const recent = jobs().slice(0, 5);
    const active = jobs().filter(isActive).length;
    return `${pageHeading("Your work, moving forward.", "Choose a project, give FatChin a task, and follow its checks and results here.")}${pendingBanner()}
      ${!state.github?.configured ? `<div class="banner banner-warning">${icon("github")}<div><strong>Connect GitHub to get started</strong>${esc(state.github?.error || "Connect an account with access to the repositories you want to work on.")}</div>${button("Connect GitHub", "github-connect", "external", "button-small")}</div>` : ""}
      <section class="hero" aria-label="Start a task"><div><p class="eyebrow">FROM FEEDBACK TO FINISHED</p><h2>A clear next step<br>for your GitHub project.</h2><p>A repository is your project on GitHub. Ask FatChin to explain it, prepare a change, or fix review feedback. You choose the project and results folder, then review what the task can change.</p>${button("Create a task", "new-task", "plus", "button-primary")}<a class="button button-quiet" href="#help">See how it works ${icon("arrow")}</a></div><div class="hero-graphic" aria-hidden="true"><div class="hero-node">${icon("issue")}</div><div class="hero-node">${icon("branch")}</div><div class="hero-node">${icon("check-circle")}</div></div></section>
      <section class="metrics" aria-label="Workspace statistics">${[
        [
          "Active tasks",
          Number(summary.jobs_open ?? active) + Number(workspaceFleet().task_counts?.running || 0),
          active ? "Queued or currently running" : "Ready for your next task",
          "jobs",
        ],
        [
          list(workspaceFleet().fleets).length ? "Fleet tasks completed" : "Completed today",
          list(workspaceFleet().fleets).length ? Number(workspaceFleet().task_counts?.done || 0) : summary.jobs_done_today ?? 0,
          list(workspaceFleet().fleets).length ? "Completed fleet reviews and fixes" : "Successful tasks · UTC day",
          "check-circle",
        ],
        ["Repositories", workspaceRepositoryCount(), "Projects covered by this workspace", "repo"],
        [
          "Model calls today",
          Number(summary.model_calls_today || 0) + Number(workspaceFleet().usage?.glm || 0) + Number(workspaceFleet().usage?.deepseek || 0),
          list(workspaceFleet().fleets).length ? `GLM ${number(workspaceFleet().usage?.glm)} · DeepSeek ${number(workspaceFleet().usage?.deepseek)}` : `${number(m.max_calls_per_day)} local daily call limit`,
          "spark",
        ],
      ]
        .map(
          ([label, value, caption, name]) =>
            `<div class="metric"><div class="metric-top">${label}${icon(name)}</div><div class="metric-value">${number(value)}</div><p class="metric-caption">${esc(caption)}</p></div>`,
        )
        .join("")}</section>
      ${fleetTasksPanel()}<div class="dashboard-grid"><section class="panel"><div class="panel-header"><div><h2>Individual task activity</h2><p>Your latest manually queued tasks</p></div><a href="#jobs">View all tasks ${icon("arrow")}</a></div>${recent.length ? `<div class="activity-list">${recent.map(activity).join("")}</div>` : empty("jobs", "A fresh start", "Your tasks will appear here as you create them.", button("Create your first task", "new-task", "plus", "button-small"))}</section>
      <section class="panel"><div class="panel-header"><div><h2>Worker status</h2><p>The service doing the work behind the scenes</p></div>${badge(s.active ? "running" : "stopped")}</div><div class="status-list"><div class="status-row"><span>${icon("github")}GitHub account</span><strong>${esc(state.github?.login || "Not connected")}</strong></div><div class="status-row"><span>${icon("spark")}Single-task model</span><strong>${esc(m.model_id || "Not configured")}</strong></div><div class="status-row"><span>${icon("clock")}Checks for updates</span><strong>${s.polling_enabled === false ? "Paused" : `Every ${number(s.poll_interval_seconds || settings().poll_interval_seconds)} seconds`}</strong></div><div class="status-row"><span>${icon("watch")}Watched repositories</span><strong>${number(new Set([...watches().map((w) => w.repo), ...watchedRepositories().map((r) => r.repo)]).size)} covered</strong></div></div><div class="panel-actions">${s.active ? button(s.polling_enabled === false ? "Resume polling" : "Pause polling", s.polling_enabled === false ? "service-resume" : "service-pause", s.polling_enabled === false ? "play" : "pause", "button-small") : button("Start worker", "service-start", "play", "button-small")}${button("Manage", "go-settings", "settings", "button-small")}</div><div class="panel-note">${s.current_job_id ? `Busy with a run for ${esc(s.member || s.current_member || "a member")} · ${esc(s.current_job_id)}<br>` : ""}${s.last_poll_iso ? `Last update check: ${esc(date(s.last_poll_iso))}` : "No update check has been recorded yet."}</div></section></div>
      <section class="quick-actions" aria-label="Quick actions"><a class="quick-action" href="#learn">${icon("help")}<span><strong>Explore a project or learn Git</strong><small>Read the code, then practice one explained move at a time</small></span>${icon("arrow","arrow-icon")}</a><button class="quick-action" data-action="new-repo">${icon("repo")}<span><strong>Add a repository</strong><small>Work across more of your projects</small></span>${icon("arrow", "arrow-icon")}</button><button class="quick-action" data-action="new-watch">${icon("watch")}<span><strong>Watch pull request reviews</strong><small>Follow up when new feedback arrives</small></span>${icon("arrow", "arrow-icon")}</button><a class="quick-action" href="#help">${icon("help")}<span><strong>Make yourself at home</strong><small>A quick guide to your workspace</small></span>${icon("arrow", "arrow-icon")}</a></section>`;
  }
  function renderJobs() {
    const all = jobs();
    const filtered = all.filter(
      (j) =>
        (A.jobsFilter === "all" ||
          (A.jobsFilter === "active"
            ? isActive(j)
            : A.jobsFilter === "done"
              ? isComplete(j)
              : jobState(j) === A.jobsFilter)) &&
        (A.repoFilter === "all" || j.repo_full_name === A.repoFilter) &&
        `${j.notes?.title || ""} ${j.repo_full_name} ${j.number} ${j.job_id}`
          .toLowerCase()
          .includes(A.jobsSearch.toLowerCase()),
    );
    return `${pageHeading("Tasks", "See what FatChin is doing, read its results, and stop or retry a task when needed.", button("New task", "new-task", "plus", "button-primary"))}${pendingBanner()}${fleetTasksPanel()}<section class="panel"><div class="toolbar"><div class="search-field">${icon("search")}<input id="jobs-search" aria-label="Search tasks" placeholder="Search tasks or repositories…" value="${esc(A.jobsSearch)}"></div><div class="filter-group"><select id="jobs-filter" aria-label="Filter by task status">${[
      ["all", "All statuses"],
      ["active", "Active"],
      ["waiting-on-you", "Waiting on you"],
      ["done", "Completed"],
      ["failed", "Failed"],
      ["stopped", "Stopped"],
    ]
      .map(
        ([v, l]) =>
          `<option value="${v}"${A.jobsFilter === v ? " selected" : ""}>${l}</option>`,
      )
      .join(
        "",
      )}</select><select id="jobs-repo-filter" aria-label="Filter by repository"><option value="all">All repositories</option>${[
      ...new Set([
        ...repos().map(repoName),
        ...watchedRepositories().map((r) => r.repo),
        ...all.map((j) => j.repo_full_name),
      ]),
    ]
      .filter(Boolean)
      .map(
        (r) =>
          `<option${r === A.repoFilter ? " selected" : ""}>${esc(r)}</option>`,
      )
      .join(
        "",
      )}</select></div></div>${filtered.length ? `<div class="table-wrap"><table class="jobs-table"><thead><tr><th>Task</th><th>Repository</th><th>Status</th><th>Model calls</th><th>Updated</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${filtered.map((j) => `<tr><td><button class="cell-title" data-action="job-detail" data-id="${esc(j.job_id)}">${esc(j.notes?.title || `${kindLabel(j.kind)}${j.number ? ` #${j.number}` : ""}`)}</button><span class="cell-sub">${esc(kindLabel(j.kind))}${j.number ? ` · #${esc(j.number)}` : ""}</span></td><td>${esc(j.repo_full_name || j.repo)}</td><td>${jobBadge(j)}</td><td>${number(j.model_calls_used)}</td><td><span title="${esc(date(j.updated_at || j.created_at))}">${esc(ago(j.updated_at || j.created_at))}</span></td><td><div class="table-actions"><button class="icon-button" data-action="job-detail" data-id="${esc(j.job_id)}" aria-label="Open task details">${icon("chevron")}</button></div></td></tr>`).join("")}</tbody></table></div><div class="table-foot">${filtered.length} of ${all.length} tasks · Select a task to see its results and controls.</div>` : empty("jobs", all.length ? "No matching tasks" : "Your task history starts here", all.length ? "Try a different search or status filter." : "Create a task to fix a pull request or implement an issue.", all.length ? "" : button("Create a task", "new-task", "plus", "button-primary"))}</section>`;
  }
  function renderRepos() {
    return `${pageHeading("Repositories", "See every watched project. Saved project settings below customize checks for individual tasks.", button("Add repository", "new-repo", "plus", "button-primary"))}${pendingBanner()}${coveragePanel()}${
      repos().length
        ? `<div class="repo-grid">${repos()
            .map(
              (r) =>
                `<article class="repo-card"><div class="repo-card-header"><div class="activity-icon">${icon("repo")}</div><div class="repo-card-name"><p>${esc(r.owner)}</p><h3>${esc(r.name)}</h3></div><span class="badge">Configured</span></div><div class="repo-card-meta"><span>${icon("branch")}${esc(r.base_branch)}</span><span>${icon("terminal")}${list(r.checks).length} verification ${list(r.checks).length === 1 ? "check" : "checks"}</span></div><div class="repo-card-checks">${
                  list(r.checks).length
                    ? list(r.checks)
                        .map((c) => `<code>${esc(c)}</code>`)
                        .join("")
                    : "No verification commands configured."
                }</div>${r.allow_unknown_rulesets ? `<div class="inline-notice warning" style="margin-bottom:15px">${icon("alert")}<span>The saved exception allows publishing when GitHub cannot reveal its branch rules. Review this in Edit before relying on it.</span></div>` : ""}<div class="repo-card-footer">${button("Edit", "edit-repo", "edit", "button-small", `data-repo="${esc(repoName(r))}"`)}${button("New task", "new-task", "plus", "button-small", `data-repo="${esc(repoName(r))}"`)}<a href="https://github.com/${encodeURI(repoName(r))}" target="_blank" rel="noopener noreferrer">Open GitHub ${icon("external")}</a><button class="icon-button" data-action="remove-repo" data-repo="${esc(repoName(r))}" aria-label="Remove ${esc(repoName(r))}">${icon("trash")}</button></div></article>`,
            )
            .join("")}</div>`
        : `<section class="panel">${empty("repo", "Bring your repositories together", "Search GitHub, choose a base branch, and add the checks that verify the worker’s changes.", button("Add your first repository", "new-repo", "plus", "button-primary"))}</section>`
    }<div class="spaced-top">${info("Adding a repository lets the worker read authorized commands in its issue and pull request comments. Add a review watch to act automatically on new review feedback. You can use any repository your connected GitHub account can access.")}</div>`;
  }
  function renderWatches() {
    return `${pageHeading("Review watches", "Your fleet follows all projects listed below. Additional manual review watches remain available.", button("Add review watch", "new-watch", "plus", "button-primary"))}${pendingBanner()}${coveragePanel()}<div class="watch-intro">${icon("watch")}<div><h3>Additional manual review watches</h3><p>Watch one pull request, or all current and future open pull requests in a repository. New review comments can queue coding tasks automatically, within your local worker call controls. Existing comments become the starting point; use New task to fix earlier feedback.</p></div></div><section class="panel">${
      watches().length
        ? `<div class="table-wrap"><table class="watches-table"><thead><tr><th>Repository</th><th>Watching</th><th>Results folder</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${watches()
            .map(
              (w, i) =>
                `<tr><td>${esc(w.repo || w.repo_full_name)}</td><td>${w.mode === "all" || w.scope === "all" ? `${icon("pr")} All open & new pull requests` : `${icon("pr")} Pull request #${esc(w.number)}`}</td><td>${w.output_dir ? `<span title="${esc(w.output_dir)}">${esc(w.account || "Member folder")}</span><span class="cell-sub mono">${esc(w.output_dir)}</span>` : '<span class="muted">Uses existing worker folder</span>'}</td><td>${badge(w.closed ? "stopped" : A.state.service?.polling_enabled === false ? "paused" : "watching")}</td><td><button class="icon-button" data-action="remove-watch" data-index="${i}" data-watch-key="${esc(`${w.repo || w.repo_full_name}|${w.mode || w.scope || "selected"}|${w.number || ""}`)}" aria-label="Remove review watch">${icon("trash")}</button></td></tr>`,
            )
            .join(
              "",
            )}</tbody></table></div><div class="table-foot">Removing a watch stops future automatic follow-up. Tasks already queued remain in task history.</div>`
        : empty(
            "watch",
            "Give reviews a follow-up plan",
            "Choose a pull request or a repository and a folder for its results. The worker will check for new feedback.",
            button(
              "Create a review watch",
              "new-watch",
              "plus",
              "button-primary",
            ),
          )
    }</section>`;
  }

  const field = (id, label, value, hint = "", type = "text", attrs = "") =>
    `<div class="field"><label for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" name="${esc(id)}" type="${type}" value="${esc(value)}" ${attrs}>${hint ? `<span class="field-hint">${hint}</span>` : ""}</div>`;
  function renderSettings() {
    const s = settings(),
      m = model(),
      service = A.state.service || {};
    return `${pageHeading("Settings", "Make the worker fit your repositories, model, and way of working.")}${pendingBanner()}${fleetSettingsPanel()}<form id="settings-form" data-revision="${esc(A.state.config_revision)}"><div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections"><a href="#settings" data-scroll="connections">Connections</a><a href="#settings" data-scroll="worker-controls">Worker controls</a><a href="#settings" data-scroll="model-limits">Model & limits</a><a href="#settings" data-scroll="command-settings">GitHub commands</a><a href="#settings" data-scroll="advanced-settings">Advanced</a></nav><div>
      <section class="panel settings-section" id="connections"><div class="panel-header"><div><h2>Connections for individual tasks</h2><p>The fleet pools and their live status appear above. These settings control the model used for individually queued tasks.</p></div></div><div class="panel-body"><div class="status-row" style="padding-top:0"><span>${icon("github")}GitHub account</span><strong>${esc(A.state.github?.login || "Not connected")}</strong></div><div class="heading-actions spaced-top">${button(A.state.github?.configured ? "Reconnect GitHub" : "Connect GitHub", "github-connect", "external", "button-small")}${button("Run setup checks", "diagnostics", "check-circle", "button-small")}${button("Test model connection", "provider-check", "spark", "button-small")}</div><hr class="field-divider"><div class="field-grid">${field("model_id", "Model name", m.model_id, "The exact model ID supported by your provider.", "text", 'required autocomplete="off"')}${field("base_url", "Model server address", m.base_url, "OpenAI-compatible base URL; omit the final <code>/chat/completions</code> path.", "url", "required")}<div class="field"><label for="auth_mode">Credential source</label><select name="auth_mode" id="auth_mode"><option value="broker-lease"${m.auth_mode === "broker-lease" ? " selected" : ""}>Company secret broker</option><option value="env"${m.auth_mode === "env" ? " selected" : ""}>Dedicated provider API key</option></select><span class="field-hint">Choose where the worker gets its model credentials.</span></div><div class="field"><label>Update a credential</label>${button("Store a new credential", "store-credential", "shield", "")}<span class="field-hint">Write-only: a saved key is never displayed in the browser.</span></div></div><div class="spaced-top">${info("Setup checks make no model request. Test model connection asks before sending exactly one small model request. Save connection changes and restart before testing them.")}</div></div></section>
      <section class="panel settings-section" id="worker-controls"><div class="panel-header"><div><h2>Individual-task worker controls</h2><p>FatChin checks GitHub regularly for new commands and review feedback; this is called polling. Queued tasks run separately.</p></div>${badge(service.active ? "running" : "stopped")}</div><div class="panel-body"><div class="heading-actions">${service.active ? `${button(service.polling_enabled === false ? "Resume polling" : "Pause polling", service.polling_enabled === false ? "service-resume" : "service-pause", service.polling_enabled === false ? "play" : "pause")}${button("Restart worker", "service-restart", "refresh")}${button("Stop worker", "service-stop", "stop", "button-danger")}` : button("Start worker", "service-start", "play", "button-primary")}</div><p class="field-hint spaced-top">Pause polling takes effect on the next check; queued tasks continue. Stop and restart require an idle worker. Use Tasks to stop queued or running tasks, then wait for the current step to finish.</p><div class="field-grid spaced-top">${field("poll_interval_seconds", "Check for updates every (seconds)", s.poll_interval_seconds, "A lower value checks GitHub more often.", "number", 'min="5" step="1" required')}<div class="field"><label for="poll_repos">Polling after restart</label><select name="poll_repos" id="poll_repos"><option value="true"${s.poll_repos !== false ? " selected" : ""}>Enabled</option><option value="false"${s.poll_repos === false ? " selected" : ""}>Disabled</option></select><span class="field-hint">The saved starting preference for the worker.</span></div></div></div></section>
      <section class="panel settings-section" id="model-limits"><div class="panel-header"><div><h2>Individual-task call limits</h2><p>Worker controls are separate from your provider’s subscription allowance.</p></div></div><div class="panel-body"><div class="field-grid">${field("max_calls_per_job", "Maximum calls per task (local)", m.max_calls_per_job, "A task stops requesting more calls when this limit is reached.", "number", 'min="1" step="1" required')}${field("max_calls_per_day", "Maximum calls per day (local)", m.max_calls_per_day, "Shared across tasks. The worker uses its UTC day boundary.", "number", 'min="1" step="1" required')}${field("max_tokens", "Maximum response tokens", m.max_tokens, "Limits the length of each model response. A token is a small piece of text.", "number", 'min="1" step="1" required')}${field("max_tool_iterations", "Maximum coding steps per task", s.max_tool_iterations, "Each step may read files, edit code, or run commands.", "number", 'min="1" step="1" required')}</div><div class="spaced-top">${info("These local worker controls count logical model calls; they do not describe your provider’s subscription allowance. A call can retry transport failures up to three HTTP attempts. Automatic review watches share the same local controls.")}</div></div></section>
      <section class="panel settings-section" id="command-settings"><div class="panel-header"><div><h2>GitHub comment commands</h2><p>Start and manage work directly from an issue or pull request.</p></div></div><div class="panel-body"><div class="field-grid">${field("command_prefix", "Command prefix", s.command_prefix, "Example: <code>/agent fix</code> on a pull request.", "text", "required")}<div class="field"><label for="command-user-lookup">Add a GitHub command user</label><div class="input-action"><input id="command-user-lookup" placeholder="GitHub username" autocomplete="off"><button id="command-user-add" type="button" class="button">${icon("plus")}Add user</button></div><div class="heading-actions">${button("Add connected account", "add-connected-user", "github", "button-small")}</div><span class="field-hint">Choose who can start work through GitHub comments. The company workspace itself remains available through the company network.</span><div id="command-user-feedback" class="field-hint" aria-live="polite"></div><div id="command-user-list" class="command-users" aria-label="Allowed GitHub comment users"></div></div><div class="field field-full"><details><summary>Advanced: allowed GitHub user IDs</summary><div><label for="allowed_command_users" class="field-label">One numeric GitHub user ID per line</label><textarea name="allowed_command_users" id="allowed_command_users" rows="3" placeholder="No comment command users configured">${esc(list(s.allowed_command_users).join("\n"))}</textarea><p class="field-hint">Username lookup adds the permanent account ID here. You can edit this list to remove users or import IDs. Save and restart to apply.</p></div></details></div></div></div></section>
      <section class="panel settings-section" id="advanced-settings"><div class="panel-header"><div><h2>Advanced settings</h2><p>Execution, storage, and provider transport.</p></div></div><div class="panel-body"><div class="field-grid">${field("run_command_timeout_seconds", "Verification command timeout (seconds)", s.run_command_timeout_seconds, "Maximum time allowed for each repository check.", "number", 'min="1" step="1" required')}${field("run_command_output_cap", "Maximum command output (characters)", s.run_command_output_cap, "Long command output is truncated at this limit.", "number", 'min="1000" step="1" required')}${field("state_dir", "Worker state folder", s.state_dir, "Installed location, shown for reference. Changing this requires a coordinated state migration.", "text", "readonly")}${field("worktree_root", "Temporary checkout folder", s.worktree_root, "Installed location, shown for reference. Each task uses a separate checkout here.", "text", "readonly")}${field("github_host", "GitHub host", s.github_host || "github.com", "Installed GitHub connection host, shown for reference.", "text", "readonly")}${field("rest_api_version", "GitHub API version", s.rest_api_version || "2022-11-28", "Version date sent to GitHub.", "text", "required")}${field("request_timeout_seconds", "Model request timeout (seconds)", m.request_timeout_seconds, "How long to wait for a provider response.", "number", 'min="1" step="1" required')}${field("broker_base_url", "Company credential service address", m.broker_base_url, "Used when the company credential service supplies the key.", "url")}${field("broker_consumer_id", "Application credential record", m.broker_consumer_id, "Technical setting: the application record used to request its dedicated credential.")}${field("broker_server_id", "Provider credential record", m.broker_server_id, "Technical setting: the provider record used to request a temporary credential.")}${field("lease_ttl_seconds", "Credential lease lifetime (seconds)", m.lease_ttl_seconds, "How long a temporary credential from the company service remains valid.", "number", 'min="1" max="900" step="1"')}<div class="field"><label for="disable_reasoning">Provider reasoning mode</label><select id="disable_reasoning" name="disable_reasoning"><option value="false"${m.disable_reasoning !== true ? " selected" : ""}>Use provider default</option><option value="true"${m.disable_reasoning === true ? " selected" : ""}>Disable separate reasoning output</option></select><span class="field-hint">Provider-specific compatibility option. Leave the default unless your provider supports disabling reasoning output.</span></div></div></div></section>
      <div class="settings-save"><p id="settings-save-hint">Changes apply after you save and restart the worker.</p><div class="heading-actions">${button("Reload saved", "settings-reload", "refresh", "button-small")}<button type="submit" class="button button-primary">${icon("check")}Save settings</button></div></div></div></div></form>`;
  }

  function renderHelp() {
    const prefix = settings().command_prefix || "/agent";
    return `${pageHeading("A little guidance. A lot of progress.", "Everything you need to start, follow, and finish your work.")}<div class="help-layout"><div><section class="help-card"><h2>Understand the words, then choose a goal</h2><details open><summary>GitHub words in everyday English</summary><div><p><strong>Repository</strong>: a project and its saved history. <strong>Branch</strong>: a separate line of work. <strong>Commit</strong>: a saved set of changes. <strong>Pull request</strong>: a proposed branch change for review. <strong>Issue</strong>: a description of a problem or piece of work. <strong>Check</strong>: a command or test used to verify a change. <strong>Draft</strong>: a proposal waiting for your decision.</p></div></details><details><summary>Explore a project or learn Git without a terminal</summary><div><p>Open <a href="#learn">Explore &amp; learn</a>. Browse a project’s files, saved changes and branch comparisons. Other useful views explains which outside tool answers which question and when it can be used.</p><p>Choose Learn step by step for Guide me or Show me. Read an explanation, perform one move, inspect the result, then choose Next move. Git basics changes only a disposable practice project. Export before ending if you want to keep the transcript.</p><p><a href="/docs/EXPLORE-AND-LEARN.md" target="_blank" rel="noopener noreferrer">Read the exploration and learning guide.</a></p></div></details><h2>Start with a familiar workflow</h2><details open><summary>Keep your GitHub projects reviewed and corrected</summary><div><ol><li><strong>Open <a href="#fleet">Agent fleet</a>.</strong> If it is running, follow the existing work. Use Resume fleet if paused or stopped; no duplicate dispatch or agent reset is needed.</li><li><strong>For a first dispatch, choose your member account and reports folder.</strong> Browse folders shows the server’s folders. Reuse your saved destination; reopening restores it. Leave All accessible writable repositories selected to include the whole connected account, or choose several projects.</li><li><strong>Choose the two pools.</strong> Defaults are 10 GLM and 10 DeepSeek agents. Dispatch 20 agents authorizes reviews, specific corrections and correction pull requests in that scope. You retain the merge decision.</li></ol><p>At most <strong>four agents work in one repository</strong>, across both pools and all task types. Twenty busy agents therefore span at least five repositories. Every task gets a fresh worker and conversation, then that worker retires. Code changes use a separate isolated working copy and correction branch.</p><p><a href="/docs/WORKFLOW.md" target="_blank" rel="noopener noreferrer">Follow the complete push-to-merge walkthrough.</a></p></div></details><details><summary>Follow a push through review, correction and merge readiness</summary><div><p>New remote work is discovered on periodic scans. Actionable comments come first, then newest-created pull requests, then eligible background code reviews. The current assignment finishes before an agent takes the next one.</p><ol><li>A fresh agent reviews the change and records supported findings.</li><li>Another fresh agent fixes the specific finding and runs the project’s checks.</li><li>The published correction goes immediately to a different agent’s review queue.</li><li>Current reviewed changes that satisfy checks, approvals and merge requirements appear in <strong>Ready to merge</strong>. Click through to GitHub, merge there, then use Delete branch if appropriate.</li></ol><p>Further findings update the same correction branch. After three automatic repair rounds the item waits for a person. All agents use one GitHub identity; another worker cannot supply another person’s required approval.</p></div></details><details><summary>Find all repositories, comments and merge-ready requests</summary><div><p><strong>Repositories</strong> and <strong>Review watches</strong> show every discovered fleet project with search. The smaller lists of saved individual-task projects and manual watches are separate; you do not need to add hundreds of repositories yourself.</p><p><strong>Agents &amp; queue</strong> shows assignments. <strong>Open comments</strong> links source feedback, corrections and independent reviews. <strong>Ready to merge</strong> links eligible requests. <strong>Activity</strong> records handoffs and recovery. Recent fleet tasks in Tasks open their matching evidence; Settings has controls for both pools.</p></div></details><details><summary>Choose one individual task instead</summary><div><p>Use <strong>New task</strong> for one of the twelve task types. These use saved individual-task repository settings. If the project is missing, add it in Repositories with its base branch and check commands, then apply the configuration while the shared worker is idle.</p><p>Choose a target, your account and a results folder, read the final review, then queue the task. Draft-producing tasks use <strong>Waiting on you → Post / Edit / Discard</strong>. The fleet dispatch separately authorizes its automatic cycle; it does not wait for Post on every correction.</p></div></details><details><summary>Fix review feedback on a pull request</summary><div><p>Select <strong>New task → Fix a pull request</strong>. Choose a configured repository and an open pull request, then a folder you own. The worker reads its review history, edits an isolated checkout, runs configured checks, and pushes verified changes to the pull request. It can reply to and resolve review threads when the correction is supported by checks and the feedback has not changed.</p><p>Fixing an existing pull request can push commits and post comments on GitHub. Review the final step before you queue the task.</p></div></details><details><summary>Turn an issue into an implementation</summary><div><p>Select <strong>New task → Implement an issue</strong>. Choose an issue, then a results folder. The worker uses the repository’s configured base branch, creates a separate checkout, writes and verifies changes, and can open a pull request with its results.</p><p>Give the issue a clear problem, expected behavior, and acceptance criteria. Better source information helps the worker produce useful changes.</p></div></details><details><summary>Add a manual watch for future review comments</summary><div><p>Open <strong>Review watches → Add review watch</strong>. Select one pull request or all open and future pull requests in a configured repository. Choose an account and folder for automatic results, then confirm the model usage and GitHub side effects.</p><p>The first check records existing comments as its starting point. Later feedback can queue work automatically. Use New task to address feedback that already existed when you created the watch. Closed pull requests do not receive automatic follow-up. Remove a manual watch to stop its future submissions; queued tasks remain. This does not remove the project from an account-wide fleet. Change fleet scope separately.</p></div></details><details><summary>Choose a folder and find your results</summary><div><p>Choose your own account, then <strong>Browse folders</strong>. Navigate with folder rows or the parent button. Use <strong>New folder</strong> to explicitly create a folder, then <strong>Use this folder</strong>. The worker checks that the folder belongs to your account and is writable before accepting work.</p><p>Task details show the selected destination, result summary, checks, changed files, and recorded errors. Use <strong>Browse task files</strong> to inspect available files or <strong>Download report</strong> to save the task’s JSON record. Temporary checkouts are working files; your selected folder is the results destination.</p></div></details><details><summary>Control individual tasks and the shared service</summary><div><p><strong>Pause polling</strong> stops individual-task discovery on the next update check; tasks already queued continue. Use the separate fleet controls for account-wide work. <strong>Stop worker</strong> stops an idle service. <strong>Restart worker</strong> applies saved settings when the worker is idle. Stop queued or running tasks first and wait for the current step to finish.</p><p><strong>Stop task</strong> requests a graceful stop of that task. An in-progress command or model request may finish before the worker observes it. Already pushed GitHub changes are kept. <strong>Retry task</strong> queues a fresh attempt using the task’s repository and destination; it may use more model calls and may write to GitHub again. Only terminal tasks offer Retry.</p></div></details><details><summary>Use commands directly on GitHub</summary><div><p>In a configured repository, an allowed GitHub user can comment:</p><ul><li><code>${esc(prefix)} implement</code> on an issue to implement it.</li><li><code>${esc(prefix)} fix</code> on a pull request to address review feedback.</li><li><code>${esc(prefix)} status</code> to ask for task status.</li><li><code>${esc(prefix)} stop</code> to request a task stop.</li></ul><p>The saved command user IDs control who may start work from GitHub comments. The graphical workspace uses the same worker and queue.</p></div></details></section><section class="help-card"><h2>When something needs attention</h2><details><summary>A fleet item is waiting, failed a check or needs recovery</summary><div><p>Worker exits and stalls recover automatically. Provider and GitHub rate limits wait through a cooldown. A missing or failed project check remains visible on that item.</p><p>Choose <strong>Set verification command</strong>, enter the project’s documented test or build command, then <strong>Save and continue</strong>. Waiting work is reconsidered automatically, without a worker reset. For a fork PR this updates the source fork’s settings. Expand <strong>Verification failed — read the command output</strong> for an actual failure; choose <strong>Retry this item</strong> after addressing its cause.</p><p>A code correction needs passing local checks on its proposed version. Missing permissions, moved branches, required approvals and automation expense holds require attention; retrying does not bypass them.</p></div></details><details><summary>Pause the fleet or change its scope without losing history</summary><div><p><strong>Pause after current tasks</strong> stops new assignments and lets current work finish. <strong>Resume fleet</strong> allows assignments again. <strong>Stop now</strong> prevents new write admission and requests cancellation; a write already admitted or sent may still finish. Closing this browser does not stop the fleet.</p><p>To change pools, repositories or destination, stop the fleet, wait for workers to finish stopping, then dispatch the replacement setup. History stays. These controls are available in Agent fleet and Settings.</p></div></details><details><summary>A task failed, stopped, or reached a limit</summary><div><p>Open the task in Tasks. Read its recorded error and verification output before retrying. A failed verification is a real failure; it is not presented as a successful result. If model calls reached a limit, review usage in Settings before increasing it. Retry only when the cause is addressed.</p></div></details><details><summary>A repository, branch, issue, or pull request is missing</summary><div><p>Check GitHub access in Settings. Repository searches show repositories accessible to the connected account. Fleet coverage appears in Repositories and Review watches after discovery; check its selected scope and scan status. Individual New task jobs require saved project settings before submission. Target lists show open issues or pull requests; you can enter a known number to look it up. Branches must exist in the selected repository.</p></div></details><details><summary>Changes are saved but the worker has not changed</summary><div><p>Changes to ordinary repository and model settings require a worker restart. Fleet Set verification command → Save and continue takes effect without an agent reset. The yellow banner remains until the running service has loaded them. A restart requires an idle worker; stop queued or running tasks first and wait for the current step to finish. Pause polling is a separate control and takes effect without a configuration restart.</p><p>If another browser saved settings while you were editing, the server refuses a stale save. Reload the latest settings, review them, and reapply your changes.</p></div></details><details><summary>GitHub writes are held or rules cannot be checked</summary><div><p>Before writing to GitHub, the worker checks repository workflows and rules that might trigger paid services. When it cannot verify that a write is allowed, it holds the write and records why. A repository’s advanced option can allow an unknown ruleset only when the GitHub plan prevents checking it. Review that exception carefully; it is not a general bypass.</p></div></details><details><summary>A folder cannot be used</summary><div><p>Choose your account, then a folder inside your home space. Use New folder in the browser if needed. If FatChin cannot write there, choose another folder or ask the owner to grant access. The worker never silently redirects results to a shared destination.</p></div></details></section></div><aside><section class="help-card"><h2>Is everything ready?</h2><div class="help-block"><h3>Check the connections first</h3><p>Verify GitHub, configuration, storage, and the worker without starting a coding task.</p><div class="spaced-top">${button("Run setup checks", "diagnostics", "check-circle", "button-small")}</div></div><div class="help-block"><h3>Understand model usage</h3><p>The worker’s local controls count logical model calls and are separate from your provider’s subscription allowance. A call can retry a transport failure up to three HTTP attempts. Manual watches share those local per-task and daily controls. The fleet has separate counters and bounded tasks; an individual-worker daily limit does not pause it. Each pool still follows its selected provider’s subscription and service limits.</p></div><div class="help-block"><h3>Private company workspace</h3><p>This interface is available through the company network. GitHub access and model credentials belong to the shared worker. Results go to the member’s chosen folder.</p></div></section><section class="help-card"><h2>Workspace documentation</h2><div class="help-block"><p><a href="/docs/WORKFLOW.md" target="_blank" rel="noopener noreferrer">Push-to-merge walkthrough</a> · Start the fleet, follow each stage and resolve waiting work.</p><p><a href="/docs/AGENT-FLEET.md" target="_blank" rel="noopener noreferrer">Agent fleet guide</a> · Both pools, priorities, four-agent repository limit and recovery.</p><p><a href="/docs/GUI-GUIDE.md" target="_blank" rel="noopener noreferrer">User guide</a> · Complete task, repository, and review workflows.</p><p><a href="/docs/GUI-OPERATIONS.md" target="_blank" rel="noopener noreferrer">Operations guide</a> · Setup, services, backups, and recovery.</p><p><a href="/docs/GUI-CAPABILITIES.md" target="_blank" rel="noopener noreferrer">Capability reference</a> · Every control and its real behavior.</p><p><a href="/docs/GUI-ACCEPTANCE.md" target="_blank" rel="noopener noreferrer">Acceptance checklist</a> · What is tested and how to verify it.</p></div></section><section class="help-card"><h2>Keyboard shortcuts</h2><div class="help-block"><div class="shortcut"><span>New task</span><span><kbd>Alt</kbd> + <kbd>N</kbd></span></div><div class="shortcut"><span>Refresh workspace</span><span><kbd>Alt</kbd> + <kbd>R</kbd></span></div><div class="shortcut"><span>Close dialog</span><kbd>Esc</kbd></div><div class="shortcut"><span>Move through controls</span><kbd>Tab</kbd></div><div class="shortcut"><span>Activate a control</span><kbd>Enter</kbd></div></div></section><section class="help-card"><h2>Task stages</h2><div class="help-block"><p><strong>Queued</strong> — waiting its turn.</p><p><strong>Retrieving</strong> — reading GitHub context.</p><p><strong>Working</strong> — coding in a separate checkout.</p><p><strong>Verifying</strong> — running the repository checks.</p><p><strong>Responding</strong> — publishing supported results.</p><p><strong>Done</strong> — finished successfully.</p><p><strong>Failed / Stopped</strong> — inspect the recorded details before retrying.</p></div></section></aside></div>`;
  }
  function doorbellSentence(entry, fallback) {
    if (entry.text) return entry.text;
    const item = `${entry.repo || "This repository"}${entry.number && entry.number !== "*" ? ` #${entry.number}` : ""}`;
    return fallback ? `${fallback} for ${item}.` : `${item}.`;
  }
  function doorbellRow(entry, iconName, sentence) {
    const job = jobs().find((j) => j.job_id && j.job_id === entry.job_id);
    const where = entry.repo
      ? `${esc(entry.repo)}${entry.number && entry.number !== "*" ? ` #${esc(entry.number)}` : ""}`
      : esc(kindLabel(entry.task));
    return `<div class="doorbell-row"><span class="doorbell-icon">${icon(iconName)}</span><div><p>${esc(sentence)}</p><small>${where} · ${esc(ago(entry.at || entry.updated_at))}</small></div><div class="doorbell-meta">${entry.fleet_item ? `<a class="button button-small" href="#fleet">Open fleet</a>` : ""}${job ? `<button class="icon-button" data-action="job-detail" data-id="${esc(job.job_id)}" aria-label="Open task details">${icon("chevron")}</button>` : ""}</div></div>`;
  }
  function draftRow(job) {
    const note = `draft-note-${job.job_id}`;
    const title = job.notes?.title || `${kindLabel(job.kind)} #${job.number}`;
    const sentence = `A draft for ${job.repo_full_name || job.repo}${job.number ? ` #${job.number}` : ""} is ready for your decision.`;
    return `<div class="doorbell-row draft-row"><span class="doorbell-icon">${icon("edit")}</span><div><p>${esc(sentence)}</p><small>${esc(title)} · ${esc(ago(job.updated_at || job.created_at))}</small><div class="draft-actions">${[
      ["post", "Post", "check"],
      ["edit", "Edit", "edit"],
      ["discard", "Discard", "trash"],
    ]
      .map(
        ([suffix, label, name]) =>
          `<button type="button" class="button button-small" data-action="draft-${suffix}" data-id="${esc(job.job_id)}" aria-describedby="${esc(note)}">${icon(name)}${label}</button>`,
      )
      .join(
        "",
      )}</div><p class="field-hint" id="${esc(note)}">A draft is a proposal, not a posted change. Post sends it after the required checks; Edit changes its wording. Discard stops remaining drafts. Previously posted changes remain on GitHub.</p></div></div>`;
  }
  function loadDigest(force = false) {
    const state = (A.digest ||= {
      data: null,
      at: 0,
      loading: false,
      error: "",
    });
    if (state.loading) return;
    if (!force && state.at && Date.now() - state.at < (state.data ? 5000 : 15000))
      return;
    state.loading = true;
    return api("/api/digest")
      .then((reply) => {
        state.data = reply;
        state.error = "";
        return true;
      })
      .catch((error) => {
        state.error = error.message;
        return false;
      })
      .finally(() => {
        state.loading = false;
        state.at = Date.now();
        if (A.view === "doorbell") renderView();
      });
  }
  function renderDoorbell() {
    loadDigest();
    const state = A.digest;
    const data = state?.data;
    const waiting = data?.waiting_on_you || {};
    const drafts = list(waiting.drafts);
    const waitingEvents = list(waiting.events);
    const done = list(data?.done);
    const watched = list(data?.watched);
    const heading = pageHeading(
      "Updates & decisions",
      "Read finished work, review drafts before posting, and see which reviews FatChin follows.",
    );
    if (!data)
      return `${heading}${state?.error ? info(esc(state.error), "error") : loading("Reading finished work and decisions")}`;
    return `${heading}${state.error ? info(esc(state.error), "error") : ""}${fleetTasksPanel()}<div class="doorbell-grid"><section class="panel"><div class="panel-header"><div><h2>Done</h2><p>Newest first</p></div><span class="muted">${number(done.length)}</span></div>${
      done.length
        ? `<div class="doorbell-list">${done.map((entry) => doorbellRow(entry, "check-circle", doorbellSentence(entry, "Finished"))).join("")}</div>`
        : empty(
            "check-circle",
            "Nothing has finished yet",
            "Each task appears here in one line as soon as it finishes, newest first.",
          )
    }</section><section class="panel"><div class="panel-header"><div><h2>Waiting on you</h2><p>Drafts and decisions, newest first</p></div><span class="muted">${number(drafts.length + waitingEvents.length)}</span></div>${
      drafts.length || waitingEvents.length
        ? `<div class="doorbell-list">${[
            ...drafts.map(draftRow),
            ...waitingEvents.map((entry) =>
              doorbellRow(entry, "edit", doorbellSentence(entry, "Needs your decision")),
            ),
          ].join("")}</div>`
        : empty(
            "edit",
            "Nothing is waiting for you",
            "Drafts that need your decision appear here with Post, Edit, and Discard.",
          )
    }</section><section class="panel"><div class="panel-header"><div><h2>Watched</h2><p>Reviews the worker follows, newest first</p></div><span class="muted">${number(watched.length)}</span></div>${
      watched.length
        ? `<div class="doorbell-list">${watched
            .map((entry) =>
              doorbellRow(
                entry,
                "watch",
                doorbellSentence(
                  entry,
                  entry.mode === "all"
                    ? "Watching every open pull request"
                    : "Watching this pull request",
                ),
              ),
            )
            .join("")}</div>`
        : empty(
            "watch",
            "No review watches yet",
            "Reviews you ask FatChin to follow appear here when their watch is active.",
          )
    }</section></div>`;
  }
  function loadFence(force = false) {
    const state = (A.fence ||= {
      data: null,
      authority: null,
      at: 0,
      loading: false,
      error: "",
    });
    if (state.loading) return Promise.resolve(state);
    if (!force && state.at && Date.now() - state.at < (state.data ? 5000 : 15000))
      return Promise.resolve(state);
    state.loading = true;
    return Promise.all([api("/api/fence"), api("/api/authority")])
      .then(([data, authority]) => {
        state.data = data;
        state.authority = authority;
        state.error = "";
      })
      .catch((error) => {
        state.error = error.message;
      })
      .finally(() => {
        state.loading = false;
        state.at = Date.now();
        if (A.view === "fence") renderView();
      });
  }
  function fenceRepo(item) {
    const unchecked = item.protection === "no-checks";
    const automatic = item.protection === "automatic";
    const direct = item.post_authority === "auto";
    return `<section class="panel fence-repo${unchecked ? " fence-danger" : ""}"><div class="panel-header"><div><h2>${esc(item.repo)}</h2><p>${item.base_branch ? `Issue work starts from ${esc(item.base_branch)}` : "Base branch not recorded"}</p></div><span class="badge badge-${automatic ? "" : unchecked ? "error" : "success"}">${automatic ? "Selected during each task" : unchecked ? "No checks" : "Checks configured"}</span></div><div class="panel-body">${
      unchecked
        ? `<div class="inline-notice error">${icon("alert")}<span>No check commands are saved. Add tests in Repositories before asking FatChin to publish code changes.</span></div>`
        : `<div class="inline-notice">${icon("check-circle")}<span>${automatic ? "The fleet detects recognized project checks and records actual results on each task. Unknown commands hold publishing until you set them." : `${number(item.checks_configured)} ${item.checks_configured === 1 ? "check" : "checks"} configured. Each task shows whether they passed.`}</span></div>`
    }<div class="status-list"><div class="status-row"><span>${icon("watch")}Automatic review follow-up</span><strong>${item.watches_armed ? "Watching for feedback" : "No active watch"}</strong></div><div class="status-row"><span>${icon("shield")}Repository rules</span><strong>${item.rulesets_readable === "unknown" || item.rulesets_readable === false ? "Could not be read" : "Readable"}</strong></div></div><div class="fence-toggle"><div><strong>${direct ? "Posts directly" : "Asks you first"}</strong><p>${direct ? "Drafts for this repository can be posted without a second confirmation." : "Drafts wait for you. Nothing is posted until you choose Post."}</p><p class="field-hint">Default: ask me every time. Merging, deleting, closing, and releasing always ask.</p></div><button type="button" class="button button-small${direct ? " button-primary" : ""}" role="switch" aria-checked="${direct}" data-action="authority-toggle" data-repo="${esc(item.repo)}" data-mode="${direct ? "ask" : "auto"}">${direct ? `${icon("shield")}Switch to asking first` : `${icon("play")}Post directly`}</button></div></div></section>`;
  }
  function renderFence() {
    loadFence();
    const state = A.fence;
    const data = state?.data;
    const heading = pageHeading(
      "Checks before publishing",
      "See which tests and GitHub rules must pass before a change can be posted.",
    );
    if (!data)
      return `${heading}${state?.error ? info(esc(state.error), "error") : loading("Reading project checks and posting settings")}`;
    const explanation =
      state.authority?.explanation ||
      "Drafts wait for you unless you choose to post directly for a repository. Merging, deleting, closing and releasing always ask.";
    return `${heading}${state.error ? info(esc(state.error), "error") : ""}<section class="panel"><div class="panel-header"><div><h2>Today at a glance</h2><p>What the worker did on its own</p></div></div><div class="status-list"><div class="status-row"><span>${icon("spark")}Model calls used today</span><strong>${number(data.model_calls_today)}</strong></div><div class="status-row"><span>${icon("shield")}Copilot review runs cancelled</span><strong>${number(data.copilot_runs_cancelled)}</strong></div><div class="status-row"><span>${icon("play")}GitHub Actions runs started by the worker</span><strong>${number(data.actions_runs_started_by_worker)}</strong></div></div><div class="panel-note">Model calls are the worker’s own local count; your provider bills separately. Cancelled runs are Copilot reviews that started from the worker’s own pushes.${data.note ? ` ${esc(data.note)}` : ""}</div></section><section class="panel"><div class="panel-header"><div><h2>Posting permission for individual-task drafts</h2><p>Fleet corrections follow the scope you dispatched; these settings govern manually prepared drafts</p></div></div><div class="panel-body">${esc(explanation)}</div></section>${list(data.repos)
      .map(fenceRepo)
      .join("")}${list(data.repos).length ? "" : `<section class="panel">${empty("repo", "No repositories to guard yet", "Add a repository to see its checks and posting permission here.")}</section>`}`;
  }
  async function draftAction(id, action) {
    const names = { post: "Post", edit: "Edit", discard: "Discard" };
    try {
      const path = `/api/jobs/${encodeURIComponent(id)}`;
      const { job } = await api(path);
      const drafts = list(job?.notes?.drafts).filter((draft) =>
        !list(job?.notes?.posted).some((posted) => posted.name === draft.name));
      if (!drafts.length) throw new Error("No unposted draft is available. Refresh the task to read its current result.");
      const apply = async (data) => {
        const reply = await api(`${path}/draft-action`, data);
        toast(reply.hint || reply.message || `${names[data.action] || "Draft"} recorded.`);
        await loadDigest(true);
      };
      const post = async (draft) => {
        const type = taskType(job.kind);
        if (type?.post_confirm_required || type?.profile === "high-stakes") {
          const merging = job.kind === "sync-branch";
          confirmDialog(
            merging ? "Merge these branches and push?" : "Apply this change to GitHub?",
            merging
              ? `This merges ${job.notes?.values?.source || "the source branch"} into ${job.notes?.values?.target || "the target branch"} in ${job.repo_full_name} and pushes directly, without a pull request. Continue only if you want that branch changed.`
              : `Confirm the proposed ${type.label} action for ${job.repo_full_name || "this project"}. Read the draft below before continuing.`,
            merging ? "Confirm merge and push" : "Confirm and post",
            () => apply({ action: "post", name: draft.name, confirm: true }),
            { danger: true, details: `<div class="effects"><strong>${esc(draft.title || draft.name)}</strong><p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(draft.body)}</p></div>` },
          );
        } else {
          await apply({ action: "post", name: draft.name });
          if (dialog.open) closeDialog();
        }
      };
      if (action === "discard") return await apply({ action, name: drafts[0].name });
      if (action === "post" && drafts.length === 1) return await post(drafts[0]);
      openDialog(action === "edit" ? "Edit your draft" : "Choose a draft to post", "YOUR DECISION",
        `<form id="draft-decision-form"><div class="wizard-stack"><div class="field"><label for="draft-choice">Draft</label><select id="draft-choice">${drafts.map((draft, index) => `<option value="${index}">${esc(draft.title || draft.name)}</option>`).join("")}</select><span class="field-hint">Already posted drafts are excluded. Each post sends only the draft you select.</span></div><div class="field"><label for="draft-body">Draft text</label><textarea id="draft-body" rows="12"${action === "edit" ? " required" : " readonly"}></textarea></div></div></form>`,
        `${button("Cancel", "dialog-close")}<button class="button button-primary" type="submit" form="draft-decision-form">${action === "edit" ? "Save draft" : "Post selected draft"}</button>`);
      const selectDraft = () => drafts[Number($("#draft-choice").value)];
      const showDraft = () => { $("#draft-body").value = selectDraft().body || ""; };
      $("#draft-choice").addEventListener("change", showDraft);
      showDraft();
      $("#draft-decision-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const draft = selectDraft();
        const body = $("#draft-body").value;
        busy($('button[form="draft-decision-form"]'), async () => {
          try {
            if (action === "edit") {
              await apply({ action, name: draft.name, body });
              closeDialog();
            } else await post(draft);
          } catch (error) { dialogError(error); }
        });
      });
    } catch (error) {
      toast([error.message, error.hint].filter(Boolean).join(" "), true);
    }
  }
  // Explicit reads and one-step actions: rendering never runs a lesson.
  const E = {
    catalog:null,catalogLoading:false,catalogError:"",data:null,repo:"",ref:"",
    path:"",view:"tree",base:"",head:"",loading:false,error:"",request:0,
    client:"cursor",tab:"browse",
  };
  const L = {
    session:null,loading:false,error:"",busy:false,request:0,
    setup:{mode:"guide",lesson:"git-basics",account:"",output_dir:""},
    draft:null,question:"",startId:"",restoreChecked:false,
  };
  const commandText = (value) => Array.isArray(value) ? value.join(" ") : String(value || "");
  const explanationText = (value) => Array.isArray(value) ? value.join(" ") : String(value || "");
  function safeLink(value, official = false) {
    try {
      const url = new URL(value);
      const allowed = official ? ["github.com","github.dev","raw.githubusercontent.com","docs.github.com"] : ["github.com","github.dev","raw.githubusercontent.com","docs.github.com","gitdiagram.com","gitingest.com","deepwiki.com","gitmcp.io","github1s.com","docs.devin.ai"];
      return url.protocol === "https:" && !url.username && !url.password && allowed.includes(url.hostname) ? url.href : "";
    } catch { return ""; }
  }
  function exploreLink(key, label, official = false) {
    const allowed = official || E.data?.repository?.private === false;
    const url = allowed && safeLink(E.data?.links?.[key], official);
    return url ? `<a class="button button-small" href="${esc(url)}" target="fatchin-explorer" rel="noopener noreferrer">${esc(label)} ${icon("external")}</a>` : "";
  }
  function loadExploreCatalog() {
    if (E.catalog || E.catalogLoading || E.catalogError) return;
    E.catalogLoading = true;
    api("/api/explore/catalog").then((reply) => {
      E.catalog=reply; E.catalogError="";
    }).catch((error) => {E.catalogError=error.message;}).finally(() => {
      E.catalogLoading=false;
      if(A.view==="learn")renderView();
    });
  }
  async function loadExplore(values = {}) {
    Object.assign(E,values);
    if (/^https:\/\/github\.com\//i.test(E.repo.trim())) {
      try {const u=new URL(E.repo.trim());const parts=u.pathname.replace(/\/$/,"").split("/").filter(Boolean);if(u.hostname==="github.com"&&!u.search&&!u.hash&&parts.length===2)E.repo=parts.join("/").replace(/\.git$/,"");} catch { /* The server returns the validation message. */ }
    }
    if(!E.repo.trim()){E.error="Choose a project or enter owner/repository before opening it.";renderView();return;}
    const request=++E.request;
    E.loading=true;E.error="";renderView();
    try {
      const reply=await api(`/api/explore?${query({repo:E.repo.trim(),view:E.view,ref:E.ref||undefined,path:E.path||undefined,base:E.base||undefined,head:E.head||undefined})}`);
      if(request!==E.request)return;
      E.data=reply;
      E.repo=reply.repository?.full_name||E.repo;
      E.ref=reply.repository?.ref||E.ref||reply.repository?.default_branch||"";
    } catch(error) {
      if(request!==E.request)return;
      E.error=error.message;E.data=null;
    } finally {
      if(request===E.request){E.loading=false;if(A.view==="learn")renderView();}
    }
  }
  function exploreReceipts() {
    const receipts=list(E.data?.receipts);
    return receipts.length ? `<details class="source-context spaced-top"><summary>What FatChin read, and the Git command to remember</summary><div>${receipts.map((r)=>`<p>${esc(r.explanation)}</p><pre>${esc(commandText(r.command))}</pre>${r.git_equivalent?`<p class="field-hint">Equivalent command in a local copy of this project (shown for learning; FatChin used the read request above):</p><pre>${esc(commandText(r.git_equivalent))}</pre>`:""}`).join("")}</div></details>` : "";
  }
  function explorerContent() {
    const d=E.data;
    if(E.loading)return loading("Reading the selected project. This does not change it.");
    if(!d)return empty("repo","Get to know a project","Choose a saved project or paste a GitHub repository URL, then choose Open project. Files, history and comparisons stay inside FatChin.");
    let body="";
    if(E.view==="file"&&d.file){
      const file=d.file;
      body=`<div class="explore-file-heading"><h3>${esc(file.path)}</h3><div class="heading-actions">${exploreLink("source","GitHub source",true)}${exploreLink("raw","Raw file",true)}${exploreLink("permalink","Permanent link",true)}${exploreLink("blame","Who changed each line",true)}</div></div>${file.binary?info("This is a binary file, such as an image or archive. Open it on GitHub to inspect or download it.") : `<pre class="explore-source" tabindex="0" aria-label="File contents">${esc(file.text||(file.truncated?"Preview unavailable for this large file. Choose Raw file or GitHub source to inspect it.":"This text file is empty."))}</pre>`}${file.truncated?info("This preview is shortened. Open GitHub source to read the full file.","warning"):""}`;
    }else if(E.view==="history"){
      body=`<p class="explore-intro">A commit is a saved change. Open its patch or diff to see the exact lines that changed.</p>${list(d.history).length?`<ol class="explore-history">${d.history.map((c)=>{
        const sha=String(c.sha||"");
        const repo=d.repository?.full_name||"";
        const valid=/^[a-f0-9]{7,64}$/i.test(sha)&&/^[\w.-]+\/[\w.-]+$/.test(repo);
        const url=valid?`https://github.com/${repo}/commit/${sha}`:"";
        return `<li><div><strong>${esc(c.message)}</strong><p class="field-hint">${esc(c.author)} · ${esc(date(c.date))} · <code>${esc(sha.slice(0,10))}</code></p></div>${url?`<div class="heading-actions"><a class="button button-small" href="${esc(url)}" target="fatchin-explorer" rel="noopener noreferrer">View change ${icon("external")}</a><a class="button button-small" href="${esc(url)}.patch" target="fatchin-explorer" rel="noopener noreferrer">Patch</a><a class="button button-small" href="${esc(url)}.diff" target="fatchin-explorer" rel="noopener noreferrer">Diff</a></div>`:""}</li>`;
      }).join("")}</ol>`:empty("clock","No saved changes found","The selected branch or file has no history to display.")}`;
    }else if(E.view==="compare"){
      const c=d.comparison;
      body=c?`<p class="explore-intro">Compare shows changes in the second branch since the branches last shared a commit. GitHub calls this a three-dot comparison.</p><p>Shared starting commit: <code>${esc(c.merge_base||"Not reported")}</code></p>${list(c.files).length?`<ul class="explore-compare">${c.files.map((f)=>`<li>${icon("file")}<span>${esc(f.filename||f.path||f.name||f)}${f.status?`<small>${esc(f.status)}${f.additions!==undefined?` · +${esc(f.additions)} / −${esc(f.deletions||0)} lines`:""}</small>`:""}</span></li>`).join("")}</ul>`:empty("branch","No changed files","These branches have no file differences in this comparison.")}${exploreLink("compare","Open full comparison",true)}`:info("Choose the starting branch and the branch containing changes, then choose Compare branches.");
    }else{
      const parent=E.path.includes("/")?E.path.slice(0,E.path.lastIndexOf("/")):"";
      body=`<div class="explore-file-heading"><h3>${esc(E.path||"Project files")}</h3>${E.path?button("Parent folder","explore-parent","back","button-small",`data-path="${esc(parent)}"`):""}</div>${list(d.items).length?`<div class="explore-files" role="group" aria-label="Project files">${d.items.map((f,i)=>`<button type="button" class="explore-file" data-action="explore-file" data-index="${i}">${icon(["dir","tree","directory"].includes(f.type)?"folder":"file")}<span>${esc(f.name||f.path)}</span><span class="field-hint">${["dir","tree","directory"].includes(f.type)?"Open folder":"Read file"}</span>${icon("chevron")}</button>`).join("")}</div>`:empty("folder","This folder has no files to show","Try another branch or open the repository on GitHub.")}`;
    }
    return `${body}${d.truncated||d.comparison?.truncated?info("This view reached its display limit. Open GitHub for more of the listing or comparison.","warning"):""}${exploreReceipts()}`;
  }
  function explorerTools() {
    if(!E.catalog)return E.catalogError?`${info(esc(E.catalogError),"error")}${button("Retry tool list","explore-catalog","refresh")}`:loading("Loading the source-checked tool guide");
    const keyMap={gitdiagram:"diagram",gitingest:"ingest",deepwiki:"wiki",githubdev:"editor","github-dev":"editor",github1s:"viewer",gitmcp:"mcp"};
    const isPublic=E.data?.repository?.private===false;
    return `<p class="explore-intro">Choose a view for your question. External tools open in a separate tab; FatChin cannot see what you do there. Their availability and free-use limits can change.</p>${!isPublic?info("External repository tools become available after FatChin confirms that the selected project is public. Private and unverified projects stay inside FatChin or official GitHub views."):info("Opening an external tool sends this public repository’s address to that provider. FatChin does not send its GitHub or model credentials.")}<div class="explore-tool-grid">${list(E.catalog.tools).map((t)=>{
      const key=keyMap[t.id]||t.id;
      const official=["editor","source","raw","permalink","blame","patch","diff","compare","archive","avatar"].includes(key);
      const source=safeLink(t.source);
      const title=t.title||t.id;
      return `<article class="explore-tool"><div class="explore-tool-title">${icon(key==="editor"||key==="viewer"?"terminal":key==="diagram"?"branch":"file")}<h3>${esc(title)}</h3></div><p>${esc(t.purpose)}</p><dl><div><dt>Use it when</dt><dd>${esc(t.when)}</dd></div><div><dt>How to start</dt><dd>${esc(t.how)}</dd></div><div><dt>What to expect</dt><dd>${esc(explanationText(t.limits))}</dd></div></dl><div class="heading-actions">${exploreLink(key,`Open ${title}`,official)||`<span class="field-hint">${!E.data?"Open a project first.":!isPublic&&!official?"Public projects only in this workspace.":"This view is unavailable for the selection."}</span>`}${source?`<a href="${esc(source)}" target="fatchin-source" rel="noopener noreferrer">Provider documentation ${icon("external")}</a>`:""}</div></article>`;
    }).join("")}</div><section class="learn-subsection"><h3>Connect your own assistant with GitMCP</h3><p>GitMCP lets a compatible assistant query public documentation and code. It does not give that assistant permission to change this project. Configuration differs by client; some clients need a bridge installed.</p><div class="field spaced-top"><label for="explore-mcp-client">Your assistant or editor</label><select id="explore-mcp-client">${list(E.catalog.clients).map((c)=>`<option value="${esc(c.id)}"${E.client===c.id?" selected":""}>${esc(c.label)}</option>`).join("")}</select></div>${isPublic&&E.data?.mcp?.[E.client]?`<pre id="explore-mcp-config">${esc(typeof E.data.mcp[E.client]==="string"?E.data.mcp[E.client]:JSON.stringify(E.data.mcp[E.client],null,2))}</pre>${button("Copy this configuration","explore-copy-mcp","copy","button-small")}`:info("Open a public project to generate its client-specific configuration.")}<p class="field-hint">Checked ${esc(E.catalog.verified)}. <a href="/docs/GITHUB-EXPLORER-FACTS.md" target="fatchin-source" rel="noopener noreferrer">Read the claim-by-claim source notes.</a></p></section>`;
  }
  function explorerShortcuts() {
    return `<p class="explore-intro">These shortcuts work on the relevant GitHub page, with focus outside a text field. GitHub lets you disable single-key shortcuts in accessibility settings. They do not control FatChin.</p><div class="shortcut-grid">${list(E.catalog?.shortcuts).map((s)=>`<article class="explore-tool"><kbd>${esc(s.key)}</kbd><h3>${esc(s.action)}</h3><p>${esc(s.where)}</p></article>`).join("")}</div><section class="learn-subsection"><h3>Useful links for the selected project</h3><p>Source keeps GitHub’s line-number interface. Raw file returns the file itself. A permanent link pins the file to its saved commit. A ZIP contains a source snapshot, not Git history; submodules and large-file storage have extra limits.</p><div class="heading-actions spaced-top">${exploreLink("source","Source with line numbers",true)}${exploreLink("raw","Raw file",true)}${exploreLink("permalink","Commit-pinned file",true)}${exploreLink("blame","Line-by-line history",true)}${exploreLink("archive","Download source ZIP",true)}${exploreLink("avatar","Owner’s avatar",true)}${exploreLink("github","Open project on GitHub",true)}</div></section>`;
  }
  function narrativeHTML(value) {
    // Escape first, then support only noninteractive typography. Never make links,
    // insert raw HTML, or turn model-written command examples into actions.
    const text=esc(value).replace(/\r\n/g,"\n");
    const inline=(part)=>part.replace(/`([^`\n]+)`/g,"<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g,"<strong>$1</strong>");
    const paragraphs=(part)=>part.trim().split(/\n{2,}/).filter(Boolean).map((paragraph)=>{
      const lines=paragraph.split("\n");
      if(lines.every((line)=>/^\s*(?:[-*]|\d+[.)])\s+/.test(line)))
        return `<ul>${lines.map((line)=>`<li>${inline(line.replace(/^\s*(?:[-*]|\d+[.)])\s+/,""))}</li>`).join("")}</ul>`;
      return `<p>${inline(paragraph.replace(/^#{1,4}\s+(.+)$/gm,"**$1**")).replace(/\n/g,"<br>")}</p>`;
    }).join("");
    const fence=/```(?:[A-Za-z0-9_-]+)?\n([\s\S]*?)```/g;
    let html="",offset=0,match;
    while((match=fence.exec(text))){
      html+=paragraphs(text.slice(offset,match.index));
      html+=`<p class="field-hint">Example in the explanation</p><pre class="learn-generated-code">${match[1]}</pre>`;
      offset=fence.lastIndex;
    }
    return html+paragraphs(text.slice(offset));
  }

  function receiptOutcome(receipt) {
    if(receipt.observed===false)
      return receipt.reported_by==="user" ? "You reported this completed" : "Outcome not confirmed";
    const command=commandText(receipt.command);
    if(receipt.exit_code===1 && /^git diff --no-index(?:\s|$)/.test(command))
      return "Differences found";
    return receipt.exit_code===0 ? "Completed" : receipt.exit_code==null ? "Recorded" : "Command needs attention";
  }
  function unsavedPractice() {
    return L.draft !== null && L.draft !== (L.session?.practice?.content || "");
  }
  function lessonStatus(s) {
    return {ready:"Ready for your next move",completed:"Step completed",finished:"Lesson finished",paused:"Paused",running:"Performing this step",preparing:"Preparing an explanation",uncertain:"Check what completed",ended:"Session ended"}[s]||labelState(s);
  }
  function sessionStorage(action,value) {
    try {
      if(action==="get")return localStorage.getItem("fatchin-learning-session");
      if(value)localStorage.setItem("fatchin-learning-session",value);
      else localStorage.removeItem("fatchin-learning-session");
    }catch{return "";}
  }
  async function restoreLesson(id=sessionStorage("get")) {
    if(!id||L.loading)return;
    L.loading=true;L.error="";renderView();
    try{
      const r=await api(`/api/learn?${query({id})}`);
      if(!r.session)throw new Error("The saved lesson could not be read. Reload its session before trying a new action.");
      L.session=r.session;L.draft=null;
    }catch(error){L.error=error.message;}
    finally{L.loading=false;if(A.view==="learn")renderView();}
  }
  function lessonSetup() {
    const setup=L.setup;
    return `<section class="panel"><div class="panel-header"><div><h2>Learn by doing, one move at a time</h2><p>Practice real Git commands in a disposable project, or tour the project above.</p></div></div><form id="learn-start-form" class="panel-body"><div class="learn-mode-grid" role="group" aria-label="Choose how to learn">${[["guide","Guide me","You choose each prepared action and edit the practice file yourself. FatChin explains each move."],["show","Show me","Read the explanation, then let FatChin perform one visible step. You decide when to continue."]].map(([value,title,description])=>`<label class="choice-card${setup.mode===value?" selected":""}"><input type="radio" name="learn-mode" value="${value}"${setup.mode===value?" checked":""}><span><strong>${title}</strong><p>${description}</p></span></label>`).join("")}</div><div class="field spaced-top"><label for="learn-lesson">What would you like to learn?</label><select id="learn-lesson"><option value="git-basics"${setup.lesson==="git-basics"?" selected":""}>Git basics: edit, inspect, save and branch</option><option value="repo-tour"${setup.lesson==="repo-tour"?" selected":""}>Project tour: files, history and comparisons</option></select><span class="field-hint">Git basics uses a fresh practice project. A project tour reads the selected project without changing it.</span></div><div class="field-grid spaced-top">${destinationFields(setup,"learn")}</div><p class="field-hint spaced-top">Your selected folder receives a transcript only when you choose Export. Practice changes stay in a disposable project. Asking for an explanation sends selected source excerpts and lesson evidence to the configured model provider.</p><div class="heading-actions spaced-top"><button type="submit" id="learn-start" class="button button-primary"${L.busy?" disabled":""}>${icon("play")}Start a lesson</button>${sessionStorage("get")?button("Resume saved lesson","learn-restore","refresh"):""}</div></form></section>`;
  }
  function lessonWorkspace() {
    const s=L.session;
    if(L.loading)return loading("Reading the saved lesson. No step will run automatically.");
    if(!s)return lessonSetup();
    const step=s.step||{};
    const ready=s.status==="ready",completed=s.status==="completed";
    const ended=["ended","finished"].includes(s.status),busyNow=L.busy;
    const canAsk=!["paused","ended","uncertain"].includes(s.status)&&!busyNow;
    const narrated=Boolean(step.narration&&step.narration_source);
    const editing=["edit","edit-file","practice-edit"].includes(step.action_kind);
    const external=["external","external-tab"].includes(step.action_kind);
    const command=commandText(step.command),content=L.draft??(s.mode==="show"&&editing&&ready?step.preview_content:s.practice?.content)??"";
    let controls="";
    if(ready){
      if(!narrated)controls=`${button("Explain this move","learn-narrate","spark","button-primary",busyNow?"disabled":"")}${button("Use the built-in explanation","learn-builtin","help","",busyNow?"disabled":"")}`;
      else if(external)controls=button("I completed this in the other tab","learn-ack","check","button-primary",busyNow?"disabled":"");
      else if(s.mode==="guide"&&editing)controls=button("Save my practice edit","learn-edit","check","button-primary",busyNow?"disabled":"");
      else controls=button(s.mode==="guide"?"Perform this Git action":"Run this one step",s.mode==="guide"?"learn-perform":"learn-run","play","button-primary",busyNow?"disabled":"");
    }else if(completed)controls=button("Next move","learn-next","arrow","button-primary",busyNow?"disabled":"");
    else if(s.status==="paused")controls=button("Resume lesson","learn-resume","play","button-primary",busyNow?"disabled":"");
    else if(s.status==="uncertain")controls=button("Check the practice project","learn-reconcile","search","button-primary",busyNow?"disabled":"");
    const transcript=list(s.receipts);
    return `<section class="panel learn-session" aria-label="Learning session"><div class="panel-header"><div><p class="eyebrow">${s.lesson==="git-basics"?"DISPOSABLE PRACTICE PROJECT":"READ-ONLY PROJECT TOUR"}</p><h2>${s.mode==="guide"?"You make the moves. FatChin explains.":"Watch one move, then choose the next."}</h2><p>${esc(s.repo||"Git basics")} · ${esc(lessonStatus(s.status))}</p></div>${badge(s.status)}</div><div class="panel-body">${L.error?`<div role="alert">${info(esc(L.error),"error")}</div>`:""}${s.error?info(esc(typeof s.error==="string"?s.error:JSON.stringify(s.error)),"warning"):""}${s.status==="failed"?info("This command did not complete successfully. Read the recorded output, export your transcript, then end this lesson and begin a fresh practice session.","warning"):""}${s.status==="uncertain"?info("The previous request ended before its result was confirmed. Check the project before continuing so a saved change is not repeated.","warning"):""}${s.status==="paused"?info("The lesson is paused. Resume when you are ready. A command that already finished remains in the record."):""}${s.status==="ended"?info("This lesson has ended and its disposable practice files are no longer available. Previously exported transcripts remain in your folder."):""}${s.status==="finished"?info("You completed this lesson. Export your transcript to keep the commands, explanations and practice text."):""}${!ended&&step.id?`<div class="learn-step"><p class="eyebrow">MOVE ${number(Number(s.step_index||0)+1)}</p><h3>${esc(step.title)}</h3><p class="learn-intent">${esc(step.intent)}</p>${step.instruction?`<p>${esc(step.instruction)}</p>`:""}${command?`<div class="learn-command"><span class="field-label">Prepared command or read request</span><pre>${esc(command)}</pre><p>${esc(step.explanation)}</p>${step.memory_aid?`<p class="learn-memory"><strong>Remember it:</strong> ${esc(step.memory_aid)}</p>`:""}</div>`:""}${narrated?`<div class="learn-narration" role="status"><div>${icon(step.narration_source==="built-in"?"help":"spark")}<strong>${step.narration_source==="built-in"?"Built-in explanation":"Model explanation"}</strong></div><div class="learn-narration-text">${narrativeHTML(step.narration)}</div></div>`:`<p class="field-hint">Choose an explanation first. FatChin waits for you to read it before an action becomes available.</p>`}${s.lesson==="git-basics"&&s.practice?`<details class="learn-practice"${editing?" open":""}><summary>${editing?"Edit your practice file: ":"Inspect practice file: "}${esc(s.practice.filename||"lesson.txt")}</summary><div><label for="learn-practice-content">Practice file contents</label><textarea id="learn-practice-content" class="learn-file-editor" rows="6" spellcheck="false"${s.mode!=="guide"||!ready||!editing||busyNow?" readonly":""}>${esc(content)}</textarea><p class="field-hint">${s.mode==="guide"&&ready&&editing?"Write a short sentence, then choose Save my practice edit. This changes only the disposable practice file.":s.mode === "show" && ready && editing ? "This is the prepared edit. It is saved only when you choose Run this one step." : "This is the saved practice text. Only the prepared edit step can change it."}</p></div></details>`:""}<div class="learn-primary-actions">${controls}${busyNow?loading("Waiting for this request. No following step will start."):""}</div><p class="field-hint">${external?"This acknowledgement records what you report. FatChin does not observe the other tab.":completed?"Read the result below. Choose Next move when you are ready; nothing continues automatically.":"Your real project is unchanged. Every action stays in this lesson’s practice project or reads the selected repository."}</p></div>`:""}<div class="learn-session-actions">${!ended&&s.status!=="paused"?button("Pause","learn-pause","pause"):""}${button("Reload session","learn-restore","refresh","",L.loading?"disabled":"")}${button("Export transcript","learn-export","download","",busyNow?"disabled":"")}${!ended?button("End lesson","learn-end","stop","button-danger"):button("Start another lesson","learn-new","plus")}</div><p class="field-hint">Transcript destination: <code>${esc(s.output_dir)}</code>. Ending a lesson removes its disposable files; export first to keep learning work.</p>${s.status!=="ended"?`<form id="learn-question-form" class="learn-question"><label for="learn-question">Ask about this move or its result</label><div class="input-action"><input id="learn-question" value="${esc(L.question)}" maxlength="2000" placeholder="For example: How is add different from commit?" required><button class="button" type="submit"${canAsk?"":" disabled"}>${icon("help")}Ask</button></div><p class="field-hint">One model explanation, without running a command or advancing the lesson.</p></form>`:""}${s.answer?`<div class="learn-narration"><strong>Answer about your lesson</strong><div class="learn-narration-text">${narrativeHTML(s.answer)}</div></div>`:""}<section class="learn-subsection" aria-label="Visible action record"><h3>What actually happened</h3>${transcript.length?`<ol class="learn-receipts">${transcript.map((r)=>`<li><div><strong>${receiptOutcome(r)}</strong><time>${esc(date(r.at))}</time></div>${r.command?`<pre>${esc(commandText(r.command))}</pre>`:""}<pre>${esc(typeof r.output==="string"?r.output:JSON.stringify(r.output??"",null,2))}</pre>${r.observed===false?'<p class="field-hint">FatChin did not confirm this action.</p>':""}${r.narration?`<details class="learn-receipt-explanation"><summary>Explanation before this move</summary><div class="learn-narration-text">${narrativeHTML(r.narration)}</div></details>`:""}</li>`).join("")}</ol>`:'<p class="muted">No action has run yet. Explanations and prepared commands appear above before anything happens.</p>'}</section>${s.repo?`<section class="learn-subsection"><h3>Ready to make a real project change?</h3><p>This leaves the paced lesson. The ordinary worker prepares documentation or issue drafts in the background; it does not pause after each command. Review your request, follow progress in Tasks, then decide what to post in Updates & decisions. The task uses this lesson’s account and results folder.</p><div class="heading-actions spaced-top">${button("Draft documentation","learn-handoff","edit","",'data-kind="write-docs"')}${button("Draft issues from my request","learn-handoff","issue","",'data-kind="triage-issues"')}</div></section>`:""}</div></section>`;
  }
  function renderLearn() {
    loadExploreCatalog();
    if(!L.restoreChecked){L.restoreChecked=true;const id=sessionStorage("get");if(id){E.tab="lesson";queueMicrotask(()=>restoreLesson(id));}}
    const configured=[...new Set([...repos().map(repoName), ...watchedRepositories().map((r) => r.repo)])];
    return `${pageHeading("Explore & learn","Understand a project, find the right view, and learn Git at your own pace.")}${L.error&&!L.session?info(esc(L.error),"error"):""}<section class="panel explore-selection"><div class="panel-header"><div><h2>Choose a project to explore</h2><p>Opening files and comparisons reads the project. It does not edit code or start the model.</p></div></div><form id="explore-form" class="panel-body"><div class="field-grid"><div class="field"><label for="explore-repo">Saved project</label><select id="explore-repo"><option value="">Choose a saved project…</option>${configured.map((r)=>`<option value="${esc(r)}"${E.repo===r?" selected":""}>${esc(r)}</option>`).join("")}</select></div><div class="field"><label for="explore-custom">Or enter a public repository</label><input id="explore-custom" value="${esc(configured.includes(E.repo)?"":E.repo)}" placeholder="owner/repository or https://github.com/owner/repository" autocomplete="off"><span class="field-hint">Inspect a public project without adding it to the worker.</span></div></div><div class="heading-actions spaced-top"><button class="button button-primary" type="submit"${E.loading?" disabled":""}>${icon("repo")}Open project</button>${E.data?`<span class="badge">${E.data.repository?.private===false?"Public project":E.data.repository?.private===true?"Private project":"Visibility unverified"}</span><span class="field-hint">Viewing <code>${esc(E.data.repository?.sha?.slice(0,10)||E.ref)}</code></span>`:""}</div></form></section><div class="explore-tabs" role="tablist" aria-label="Explore and learn sections">${[["browse","Files & history","folder"],["tools","Other useful views","external"],["shortcuts","GitHub shortcuts","terminal"],["lesson","Learn step by step","help"]].map(([id,label,name])=>`<button type="button" id="explore-tab-${id}" class="explore-tab${E.tab===id?" active":""}" role="tab" aria-selected="${E.tab===id}" aria-controls="explore-panel" tabindex="${E.tab===id?0:-1}" data-action="explore-tab" data-tab="${id}">${icon(name)}${label}</button>`).join("")}</div><div id="explore-panel" role="tabpanel" aria-labelledby="explore-tab-${esc(E.tab)}">${E.tab==="lesson"?lessonWorkspace():E.tab==="tools"?`<section class="panel"><div class="panel-body">${explorerTools()}</div></section>`:E.tab==="shortcuts"?`<section class="panel"><div class="panel-body">${explorerShortcuts()}</div></section>`:`<section class="panel"><div class="panel-body">${E.error?`<div role="alert">${info(esc(E.error),"error")}</div>`:""}${E.data?`<div class="explore-view-controls"><div class="field"><label for="explore-ref">Branch</label><select id="explore-ref">${!list(E.data.branches).some((b)=>b.name===E.ref)?`<option value="${esc(E.ref)}" selected>${esc(E.ref)} (selected version)</option>`:""}${list(E.data.branches).map((b)=>`<option value="${esc(b.name)}"${b.name===E.ref?" selected":""}>${esc(b.name)}</option>`).join("")}</select><details class="explore-version-tools"><summary>Open a tag or saved commit</summary><div class="input-action"><input id="explore-version" aria-label="Tag or commit to inspect" placeholder="For example, v1.0 or a commit ID" value="${esc(E.otherRef||"")}">${button("Open version","explore-version","arrow","button-small")}</div><p class="field-hint">A tag names a release. A commit ID identifies one saved version. Both are read-only here.</p></details></div><div class="heading-actions">${button("Files","explore-view","folder",E.view==="tree"||E.view==="file"?"button-primary":"",'data-view="tree"')}${button("Saved changes","explore-view","clock",E.view==="history"?"button-primary":"",'data-view="history"')}${button("Compare branches","explore-view","branch",E.view==="compare"?"button-primary":"",'data-view="compare"')}</div></div>${E.view==="compare"?`<form id="explore-compare-form" class="explore-compare-form"><div class="field"><label for="explore-base">Starting branch, tag or commit</label><input id="explore-base" list="explore-versions" value="${esc(E.base)}" autocomplete="off"><datalist id="explore-versions">${list(E.data.branches).map((b)=>`<option value="${esc(b.name)}"></option>`).join("")}</datalist></div><div class="field"><label for="explore-head">Version with changes</label><input id="explore-head" list="explore-versions" value="${esc(E.head)}" autocomplete="off"></div><button class="button" type="submit">${icon("branch")}Compare branches</button></form>`:""}`:""}${explorerContent()}</div></section>`}</div>`;
  }
  async function startLesson() {
    if(L.busy)return;
    validateDestination(L.setup);
    if(L.setup.lesson==="repo-tour"&&!E.data)throw new Error("Open the project above before starting its tour.");
    L.startId ||= crypto.randomUUID();
    await mutateLesson("start",{...L.setup,request_id:L.startId,repo:E.data?.repository?.full_name||"",ref:E.ref,path:E.view==="file"?E.path:""});
    if(L.session)L.startId="";
  }
  async function mutateLesson(action,extra={}) {
    if(L.busy&&!["pause","end"].includes(action))return;
    const request=++L.request;
    const wasBusy=L.busy;
    L.busy=true;L.error="";renderView();
    try{
      let session=L.session;
      if(["pause","end"].includes(action)&&wasBusy&&session)
        session=(await api(`/api/learn?${query({id:session.id})}`)).session;
      const payload=action==="start"?{action,...extra}:{action,id:session.id,revision:session.revision,step_id:session.step?.id,...extra};
      const reply=await api("/api/learn",payload,{timeout:180000});
      if(request!==L.request)return;
      if(!reply.session)throw new Error("The lesson response was incomplete. Reload the session before trying the action again.");
      L.session=reply.session;if(["start","edit","run","perform","next","end"].includes(action))L.draft=null;sessionStorage("set",reply.session.id);
      if(action==="export")toast("Transcript exported to your selected results folder.");
    }catch(error){
      if(request!==L.request)return;
      L.error=error.message;
      if(action!=="start"&&L.session?.id){
        try{L.session=(await api(`/api/learn?${query({id:L.session.id})}`)).session||L.session;}
        catch{/* Keep the recorded error and last known state. */}
      }
    }finally{if(request===L.request)L.busy=false;if(A.view==="learn")renderView();}
  }
  function bindLearn() {
    $("#explore-form")?.addEventListener("submit",(e)=>{
      e.preventDefault();
      const repo=$("#explore-custom").value.trim()||$("#explore-repo").value;
      loadExplore({repo,ref:"",path:"",view:"tree",base:"",head:""});
    });
    $("#explore-repo")?.addEventListener("change",(e)=>{E.repo=e.target.value;$("#explore-custom").value="";});
    $("#explore-custom")?.addEventListener("input",(e)=>{E.repo=e.target.value;$("#explore-repo").value="";});
    $("#explore-version")?.addEventListener("input",(e)=>{E.otherRef=e.target.value;});
    $("#explore-ref")?.addEventListener("change",(e)=>loadExplore({ref:e.target.value,path:"",view:"tree"}));
    $("#explore-compare-form")?.addEventListener("submit",(e)=>{e.preventDefault();loadExplore({base:$("#explore-base").value,head:$("#explore-head").value,view:"compare"});});
    $("#explore-base")?.addEventListener("change",(e)=>{E.base=e.target.value;});
    $("#explore-head")?.addEventListener("change",(e)=>{E.head=e.target.value;});
    $("#explore-mcp-client")?.addEventListener("change",(e)=>{E.client=e.target.value;renderView();});
    $$(".explore-tab").forEach((el)=>el.addEventListener("keydown",(e)=>{
      const tabs=$$(".explore-tab"),index=tabs.indexOf(el);
      const next=e.key==="ArrowRight"?(index+1)%tabs.length:e.key==="ArrowLeft"?(index+tabs.length-1)%tabs.length:e.key==="Home"?0:e.key==="End"?tabs.length-1:-1;
      if(next>=0){e.preventDefault();E.tab=tabs[next].dataset.tab;renderView();$("#explore-tab-"+E.tab)?.focus();}
    }));
    $("#learn-start-form")?.addEventListener("submit",(e)=>{e.preventDefault();startLesson().catch((error)=>{L.error=error.message;renderView();});});
    $$('input[name="learn-mode"]').forEach((el)=>el.addEventListener("change",(e)=>{L.setup.mode=e.target.value;renderView();}));
    $("#learn-lesson")?.addEventListener("change",(e)=>{L.setup.lesson=e.target.value;});
    bindDestination(L.setup,"learn");
    $("#learn-practice-content")?.addEventListener("input",(e)=>{L.draft=e.target.value;});
    $("#learn-question")?.addEventListener("input",(e)=>{L.question=e.target.value;});
    $("#learn-question-form")?.addEventListener("submit",(e)=>{e.preventDefault();if(L.question.trim())mutateLesson("question",{question:L.question.trim()});});
  }
  async function learnAction(element) {
    const action=element.dataset.action;
    if(action==="explore-tab"){E.tab=element.dataset.tab;renderView();$("#explore-tab-"+E.tab)?.focus();return;}
    if(action==="explore-catalog"){E.catalogError="";loadExploreCatalog();renderView();return;}
    if(action==="explore-file"){
      const f=E.data?.items?.[Number(element.dataset.index)];
      if(f)return loadExplore({path:f.path,view:["dir","tree","directory"].includes(f.type)?"tree":"file"});
      return;
    }
    if(action==="explore-version"){const ref=E.otherRef?.trim();if(!ref){E.error="Enter the tag or saved commit to inspect.";renderView();return;}return loadExplore({ref,path:"",view:"tree"});}
    if(action==="explore-parent")return loadExplore({path:element.dataset.path,view:"tree"});
    if(action==="explore-view"){
      if(element.dataset.view==="compare"){
        E.view="compare";E.base ||= E.data?.repository?.default_branch||E.ref;
        E.head ||= list(E.data?.branches).find((b)=>b.name!==E.base)?.name||E.ref;
        renderView();return;
      }
      return loadExplore({view:element.dataset.view,path:""});
    }
    if(action==="explore-copy-mcp"){
      const value=E.data?.repository?.private===false&&E.data?.mcp?.[E.client];
      if(!value)return;
      try{await navigator.clipboard.writeText(typeof value==="string"?value:JSON.stringify(value,null,2));toast("Client configuration copied.");}
      catch{
        const el=$("#explore-mcp-config");
        if(el){const range=document.createRange();range.selectNodeContents(el);getSelection().removeAllRanges();getSelection().addRange(range);toast("Copy the highlighted configuration with your browser’s Copy command.");}
      }return;
    }
    if(action==="learn-restore"){
      if(unsavedPractice())return confirmDialog("Reload and discard your unsaved practice text?","The text you have typed has not been saved to the practice project. Reloading replaces it with the saved file. Choose Cancel, then Save my practice edit to keep it.","Discard unsaved text and reload",async()=>{closeDialog();await restoreLesson(L.session?.id);});
      return restoreLesson(L.session?.id);
    }
    if(action==="learn-export"&&unsavedPractice()){
      L.error="Save my practice edit before exporting. The transcript includes the saved file; text still in the editor has not been saved.";
      toast(L.error,true);renderView();return;
    }
    if(action==="learn-new"){L.session=null;L.draft=null;L.error="";sessionStorage("set","");renderView();return;}
    if(action==="learn-end")return confirmDialog("End this lesson?",unsavedPractice() ? "You have unsaved text in the editor. Choose Cancel, then Save my practice edit and Export transcript to keep it. Ending now discards that text and removes the disposable practice files; your real repository and existing exports remain." : "The disposable practice project will be removed after any running action finishes. Export first if you want to keep its text and command record. Your real repository and previously exported transcripts remain in place.","End lesson",async()=>{closeDialog();await mutateLesson("end");});
    if(action==="learn-handoff"){
      const s=L.session;if(!s)return;
      if(!repos().some((r)=>repoName(r)===s.repo)){
        openDialog("Add this project before creating a task","PROJECT CHANGE",`<p class="dialog-copy">Learning can inspect public projects without configuration. To request a real change in <strong>${esc(s.repo)}</strong>, add it in Repositories, set its checks and restart the idle worker. Then return to this lesson.</p>`,button("Close","dialog-close"));return;
      }
      newTask(s.repo);
      if(A.wizard){
        A.wizard.kind=element.dataset.kind;A.wizard.learning_session_id=s.id;
        A.wizard.account=s.account;A.wizard.output_dir=s.output_dir;
        A.wizard.values={focus:"Explain this project for a new user, based on my learning session.",words:L.question||""};
        renderWizard();
      }return;
    }
    const mapped={"learn-narrate":"narrate","learn-builtin":"narrate","learn-run":"run","learn-perform":"perform","learn-edit":"edit","learn-next":"next","learn-pause":"pause","learn-resume":"resume","learn-export":"export","learn-ack":"ack","learn-reconcile":"reconcile"}[action];
    if(mapped)return mutateLesson(mapped,action==="learn-builtin"?{builtin:true}:action==="learn-edit"?{content:L.draft??L.session?.practice?.content??""}:{});
  }

  // Fleet forms stay in the browser while live status updates underneath them.
  const F = {
    data: null, loading: null, busy: false, version: 0, setupExpanded: true, error: "", lastGood: "", tab: "agents",
    setup: {account: "", output_dir: "", scope: "all", repos: [], workers: 10, deepseek_workers: 10, dry_run: true},
    search: "", visibleLimit: 200, repoFilter: "all", repoQuery: "", repoResults: [], repoError: "", searching: false,
  };
  const fleetKinds = {review_pr: "Review a pull request", fix_comment: "Fix a review comment", review_code: "Review project code"};
  function currentFleet() {
    return list(F.data?.fleets).find((f) => f.account === F.setup.account) || null;
  }
  function fleetRows(key) {
    const selected = currentFleet();
    return list(F.data?.[key]).filter((row) => !F.setup.account || (selected && row.fleet_id === selected.id));
  }
  function fleetURL(value) {
    try {
      const u = new URL(String(value || ""));
      return u.protocol === "https:" && u.hostname === "github.com" && !u.username && !u.password && !u.port ? u.href : "";
    } catch { return ""; }
  }
  function fleetLink(value, label, cls = "") {
    const url = fleetURL(typeof value === "object" ? value?.url || value?.html_url : value);
    return url ? `<a class="${esc(cls)}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ${icon("external")}</a>` : "";
  }
  function fleetStatus(status) {
    const names = {idle: "Available", awaiting_merge: "Correction awaiting merge", unverified: "Needs a fresh check", unavailable: "Provider unavailable", queued: "Waiting its turn", running: "Working", retrying: "Retrying automatically", recovering: "Resetting automatically", backoff: "Waiting before retry", blocked: "Needs attention", done: "Finished", ready: "Ready to merge", dismissed: "No change needed", paused: "Paused", pausing: "Finishing current work", stopped: "Stopped", stopping: "Stopping", waiting: "Watching for work"};
    const cls = ["blocked", "retrying", "recovering", "backoff", "paused", "pausing", "stopping"].includes(status) ? "warning" : ["ready", "done", "running"].includes(status) ? "success" : "";
    return `<span class="badge badge-${cls}">${esc(names[status] || labelState(status))}</span>`;
  }
  function fleetFiltered(rows) {
    const term = F.search.trim().toLowerCase();
    return rows.filter((row) => (F.repoFilter === "all" || row.repo === F.repoFilter) && (!term || [row.repo, row.title, row.number, row.error, row.result?.summary, row.id].join(" ").toLowerCase().includes(term)));
  }
  function fleetProvenance(item) {
    const r = item.result || {};
    const source = item.source_comment_url || item.comment_url || item.source_comment?.url || r.source_comment_url;
    const correction = item.fix_pr || item.correction_pr || r.fix_pr || r.correction_pr;
    const review = item.review_url || r.review_url;
    const original = item.original_pr_url || item.source_pr_url || r.original_pr_url;
    const links = [fleetLink(source, "Source comment"), fleetLink(correction, "Correction pull request"), fleetLink(review, "Independent review"), fleetLink(original, "Original pull request")].filter(Boolean);
    const author = r.fix_pr?.author_slot ?? item.author_slot ?? r.author_slot;
    const reviewer = item.reviewer_slot ?? r.reviewer_slot;
    return links.length || author != null || reviewer != null ? `<div class="fleet-provenance">${links.join("")}${author != null ? `<span>Change: agent ${esc(author)}</span>` : ""}${reviewer != null ? `<span>Review: agent ${esc(reviewer)}</span>` : ""}</div>` : "";
  }
  function fleetFailedChecks(item) {
    return list(item.result?.checks || item.checks).some((check) => check && typeof check === "object" && (check.passed === false || check.status === "failed" || Number(check.exit ?? check.exit_code ?? 0) !== 0));
  }
  function fleetChecks(checks) {
    if (!checks || (Array.isArray(checks) && !checks.length)) return "";
    const entries = list(checks);
    const failed = entries.some((check) => check && typeof check === "object" && (check.passed === false || check.status === "failed" || Number(check.exit ?? check.exit_code ?? 0) !== 0));
    return `<details class="fleet-check-details"><summary>${failed ? "Verification failed — read the command output" : "Verification results"}</summary><div class="fleet-check-results">${entries.map((check) => {
      if (typeof check === "string") return `<p>${esc(check)}</p>`;
      if (!check || typeof check !== "object") return "";
      const exit = check.exit ?? check.exit_code;
      const passed = check.passed === true || check.status === "passed" || check.status === "success" || (check.passed !== false && exit === 0);
      const failed = check.passed === false || check.status === "failed" || (exit != null && Number(exit) !== 0);
      const output = check.output_tail ?? check.output ?? "";
      return `<div class="fleet-check-result"><div><strong>${esc(check.command || check.name || "Repository check")}</strong><span class="badge badge-${passed ? "success" : failed ? "error" : ""}">${passed ? "Passed" : failed ? "Failed" : "Outcome not confirmed"}${exit != null ? ` · exit ${esc(exit)}` : ""}</span></div>${check.stage ? `<p class="field-hint">Stage: ${esc(check.stage === "dependencies" ? "Preparing the project’s dependencies" : labelState(check.stage))}</p>` : ""}${output ? `<pre tabindex="0" aria-label="Verification command output">${esc(output)}</pre>` : '<p class="field-hint">This check returned no command output.</p>'}</div>`;
    }).join("")}</div></details>`;
  }
  function fleetItem(item, ready = false) {
    const result = item.result || {};
    const title = item.title || fleetKinds[item.kind] || "Work item";
    const url = item.url || item.html_url || result.url;
    const detail = item.error || result.error || item.blocked_reason || result.summary || "";
    const failedChecks = fleetFailedChecks(item);
    const canRetryItem = ["blocked", "failed"].includes(item.status) || (item.status === "done" && failedChecks);
    const checks = result.checks || item.checks;
    const nextStep = result.next_action || item.next_action;
    const nextAction = nextStep === "configure_checks" ? "Choose Set verification command to save this project’s check. Waiting work resumes automatically." : nextStep || (failedChecks ? "Read the failed command and its output below. If this project needs a different check, choose Set verification command. After correcting the reported setup problem, choose Retry this item." : "");
    return `<article class="fleet-item" data-fleet-item="${esc(item.id)}"><div class="fleet-item-icon">${icon(item.kind === "fix_comment" ? "issue" : "pr")}</div><div class="fleet-item-content"><div class="fleet-item-heading"><h3>${fleetLink(url, title) || esc(title)}</h3>${fleetStatus(ready ? (F.error ? "unverified" : "ready") : failedChecks ? "blocked" : item.kind === "fix_comment" && item.status === "done" && result.fix_pr ? "awaiting_merge" : item.status)}</div><p class="fleet-item-location">${esc(item.repo || "Repository not recorded")}${item.number ? ` · #${esc(item.number)}` : ""} · ${esc(fleetKinds[item.kind] || "Pull request")}</p>${detail ? `<p class="fleet-item-detail">${esc(detail)}</p>` : ""}${fleetProvenance(item)}${ready ? `<p class="field-hint">Reviewed version <code>${esc((item.head_sha || result.head_sha || "").slice(0, 10) || "Recorded by GitHub")}</code>. Recheck GitHub’s merge controls, then merge and choose Delete branch there.</p>` : ""}${nextAction ? `<p class="fleet-next-action"><strong>Next step:</strong> ${esc(nextAction)}</p>` : ""}${fleetChecks(checks)}${canRetryItem ? `<div class="heading-actions spaced-top">${button("Retry this item", "fleet-retry", "refresh", "button-small", `data-id="${esc(item.id)}" data-fleet-id="${esc(item.fleet_id)}"${F.busy ? " disabled" : ""}`)}${button("Set verification command", "fleet-configure-checks", "check-circle", "button-small", `data-repo="${esc(item.source_repo || item.repo)}" data-id="${esc(item.id)}"${F.busy ? " disabled" : ""}`)}</div>` : ""}</div></article>`;
  }
  function fleetSlots() {
    const workers = list(F.data?.workers);
    const selected = currentFleet();
    const items = list(F.data?.items);
    return [["glm", "GLM-5.3-flash", 0], ["deepseek", "DeepSeek v4.1 flash 1017", 10]].map(([provider, label, offset]) => `<section class="fleet-provider-group"><div class="fleet-provider-heading"><h2>${esc(label)}</h2><span>10 agent slots · Fresh worker for every task</span></div><div class="fleet-slots">${Array.from({length: 10}, (_, i) => {
      const slot = offset + i + 1;
      const worker = workers.find((w) => Number(w.slot) === slot);
      const item = items.find((row) => row.id === worker?.item_id);
      const other = worker && selected && worker.fleet_id && worker.fleet_id !== selected.id;
      const providerConfig = list(F.data?.providers).find((p) => p.id === provider);
      const status = worker?.status || (providerConfig?.available === false ? "unavailable" : "idle");
      return `<article class="fleet-slot${["running", "working"].includes(status) ? " is-working" : ""}"><div class="fleet-slot-heading"><span class="fleet-slot-number">${String(slot).padStart(2, "0")}</span>${fleetStatus(status)}</div><h3>${esc(item ? fleetKinds[item.kind] || item.kind : other ? "Helping another member" : worker?.stage || (currentFleet()?.status === "running" ? "Ready for the next item" : "Ready when you start"))}</h3><p>${esc(item?.repo || worker?.repo || (other ? "Shared twenty-agent capacity" : label))}</p>${item ? `<p class="fleet-slot-task">${fleetLink(item.url, item.title || `Item #${item.number || ""}`) || esc(item.title || "Work in progress")}</p>` : ""}<div class="fleet-slot-footer"><span>${esc(worker?.stage || (status === "idle" ? "No task in progress" : labelState(status)))}</span>${Number(worker?.restarts) > 0 ? `<span>${number(worker.restarts)} automatic resets</span>` : ""}</div></article>`;
    }).join("")}</div></section>`).join("");
  }
  function fleetNotice() {
    const s = F.data?.supervisor || {};
    const errors = [...new Set([s.error, s.discovery_error, s.scan_error, currentFleet()?.error].filter(Boolean))];
    return `${F.error ? `<div role="alert">${info(`${esc(F.error)}${F.lastGood ? ` Last successful update: ${esc(date(F.lastGood))}. The lists below show saved information; merge readiness cannot be confirmed until the connection returns.` : ""}`, "error")}</div>` : ""}${errors.map((e) => info(`${esc(e)} Discovery may be incomplete. Work that needs this information waits until it can be checked.`, "warning")).join("")}${s.scan_partial || s.discovery_complete === false ? info("GitHub returned only part of the scan. Missing items are not treated as closed. Readiness is withheld wherever current evidence is incomplete.", "warning") : ""}${F.data && s.active === false ? info("The fleet coordinator is not running. Your saved work is retained. Check the worker status in Settings if you intended this fleet to be running.", "warning") : ""}`;
  }
  function fleetWindow() {
    if (!F.data) return F.loading ? loading("Loading your agent fleet") : empty("jobs", "Fleet status is unavailable", "Refresh to try the connection again.", button("Refresh fleet", "fleet-refresh", "refresh"));
    const ready = fleetRows("ready"), comments = fleetRows("comments"), items = fleetRows("items");
    const active = items.filter((item) => ["queued", "running", "retrying", "blocked"].includes(item.status) || (item.status === "done" && fleetFailedChecks(item)));
    const tabs = [["agents", "Agents & queue", "jobs", active.length], ["ready", "Ready to merge", "check-circle", ready.length], ["comments", "Open comments", "issue", comments.length], ["activity", "Activity", "clock", null]];
    let body;
    if (F.tab === "agents") {
      const queue = fleetFiltered(active).sort((a, b) => ({fix_comment: 0, review_pr: 1, review_code: 2}[a.kind] ?? 3) - ({fix_comment: 0, review_pr: 1, review_code: 2}[b.kind] ?? 3) || new Date(b.created_at || 0) - new Date(a.created_at || 0));
      body = `${fleetSlots()}<section class="panel fleet-list"><div class="panel-header"><div><h2>Next in line</h2><p>Actionable comments come first, then newest pull requests. Each agent finishes its current task before taking the newest eligible item.</p></div></div>${queue.length ? queue.slice(0, F.visibleLimit).map((item) => fleetItem(item)).join("") + (queue.length > F.visibleLimit ? `<div class="panel-actions"><span>Showing ${number(F.visibleLimit)} of ${number(queue.length)} matches</span>${button("Show more tasks", "fleet-show-more", "plus", "button-small")}</div>` : "") : empty("check-circle", items.length ? "No work is waiting in this view" : "A clear view of what’s next", currentFleet()?.status === "running" ? "The fleet checks for new requests and reviews changed code. It waits when everything available has already been reviewed." : "Start a fleet to discover pull requests and review comments across your repositories.")}</section>`;
    } else if (F.tab === "ready") {
      const rows = fleetFiltered(ready);
      body = `<section class="panel fleet-list"><div class="panel-header"><div><h2>${F.error ? "Last recorded merge candidates" : "Ready for your merge decision"}</h2><p>${F.error ? "These were recorded earlier. Their current merge conditions could not be checked. Open GitHub to inspect the latest state." : "These requests have a current review and verified merge conditions. Open an item to merge it on GitHub, then delete its branch there."}</p></div></div>${info("Drafts, changed code, unresolved findings, missing checks, unknown merge conditions and required approvals keep a request out of this list. A different agent reviews a correction; all agents share the connected GitHub identity, so this does not create a second person’s approval.")}${rows.length ? rows.map((item) => fleetItem(item, true)).join("") : empty("pr", ready.length ? "No matching merge candidates" : "Nothing is ready to merge yet", ready.length ? "Clear the search or repository filter to see other items." : "Agents will review new pull requests and work through their findings. Open comments shows what still needs attention.")}</section>`;
    } else if (F.tab === "comments") {
      const rows = fleetFiltered(comments);
      body = `<section class="panel fleet-list"><div class="panel-header"><div><h2>Feedback with a next step</h2><p>The next free agent picks up an actionable comment, changes only what it asks for, and sends the correction to another agent for review.</p></div></div>${info("Follow Source comment → Correction pull request → Independent review. A comment stays open until its intended correction is confirmed merged. Unclear feedback or missing project checks is held for attention.")}${rows.length ? rows.map((item) => fleetItem(item)).join("") : empty("issue", comments.length ? "No matching comments" : "No open findings in this view", comments.length ? "Try another search or repository filter." : "Actionable review feedback will appear here with its status and links to the correction.")}</section>`;
    } else {
      const selected = currentFleet();
      const events = list(F.data.events).filter((event) => !F.setup.account || !event.fleet_id || event.fleet_id === selected?.id).slice().reverse().slice(0, 100);
      body = `<section class="panel"><div class="panel-header"><div><h2>Work and automatic recovery</h2><p>Resets, retries and task handoffs are recorded here. Repeated failures stay visible while other agents continue.</p></div></div>${events.length ? `<ol class="fleet-events">${events.map((event) => `<li><div>${icon("clock")}<time>${esc(date(event.at))}</time>${event.slot ? `<span>Agent ${esc(event.slot)}</span>` : ""}</div><p>${esc(event.message)}</p></li>`).join("")}</ol>` : empty("clock", "Activity will appear here", "Starting a fleet records discovery, work and recovery in this view.")}</section>`;
    }
    return `${fleetNotice()}${F.data.usage ? `<div class="fleet-usage" aria-label="Fleet model calls today"><span>Model calls today</span><strong>GLM ${number(F.data.usage.glm?.calls_today)}</strong><strong>DeepSeek ${number(F.data.usage.deepseek?.calls_today)}</strong></div>` : ""}<div class="fleet-tabs" role="tablist" aria-label="Fleet windows">${tabs.map(([id, title, symbol, count]) => `<button id="fleet-tab-${id}" type="button" class="fleet-tab${F.tab === id ? " active" : ""}" role="tab" aria-selected="${F.tab === id}" aria-controls="fleet-panel" tabindex="${F.tab === id ? 0 : -1}" data-action="fleet-tab" data-tab="${id}">${icon(symbol)}${esc(title)}${count != null ? `<span class="count">${number(count)}</span>` : ""}</button>`).join("")}</div><div id="fleet-panel" role="tabpanel" aria-labelledby="fleet-tab-${esc(F.tab)}">${body}</div>`;
  }
  function fleetControls() {
    const f = currentFleet(), status = f?.status;
    const running = ["running", "active", "pausing"].includes(status);
    return `<div class="fleet-control-summary"><div>${f ? `${fleetStatus(status)}<span>${esc(f.account)} · ${f.scope === "all" ? "All accessible writable repositories" : `${list(f.repos).length} selected repositories`} · Up to ${number(Number(f.workers || 0) + Number(f.deepseek_workers || 0))} agents</span>` : '<span>Choose your account and results folder to start.</span>'}</div><div class="heading-actions">${running ? button("Pause after current tasks", "fleet-pause", "pause", "", `data-id="${esc(f.id)}"${F.busy ? " disabled" : ""}`) : f && ["paused", "stopped"].includes(status) ? button("Resume fleet", "fleet-resume", "play", "button-primary", `data-id="${esc(f.id)}"${F.busy ? " disabled" : ""}`) : ""}${f && !["stopped"].includes(status) ? button("Stop now", "fleet-stop", "stop", "button-danger-quiet", `data-id="${esc(f.id)}"${F.busy ? " disabled" : ""}`) : ""}${button("Refresh fleet", "fleet-refresh", "refresh", "button-quiet", F.loading || F.busy ? "disabled" : "")}</div></div>`;
  }
  function renderFleet() {
    const f = currentFleet();
    const github = F.data?.github || A.state.github || {};
    const known = [...new Set([...list(A.state.repos).map(repoName), ...watchedRepositories().map((r) => r.repo), ...list(F.data?.items).map((r) => r.repo), ...F.setup.repos].filter(Boolean))].sort();
    const chosen = F.setup.scope === "selected";
    return `${pageHeading("Your agent fleet", "Twenty helping hands. Every project. One place to follow the work.")}<div class="fleet-intro"><div><p class="eyebrow">CONTINUOUS GITHUB CARE</p><h2>Review. Fix. Review again.</h2><p>Dispatch ten GLM agents and ten DeepSeek agents across your accessible projects. They work through feedback, review the newest requests first, and keep checking changed code while you focus elsewhere.</p></div><div class="fleet-identity">${icon("github")}<div><strong>${esc(github.login ? `Connected as ${github.login}` : "GitHub connection not confirmed")}</strong><span>GLM + DeepSeek · Up to 20 at once</span><span>Uses this installation’s connected GitHub account</span></div></div></div><section class="panel fleet-setup"><div class="panel-header"><div><h2>Choose where your fleet works</h2><p>All writable projects are included by default. At most four agents work in one repository; a full fleet spans at least five. Reports belong in your own folder.</p></div>${f && f.status !== "stopped" ? button(F.setupExpanded ? "Hide setup" : "Review setup", "fleet-setup-toggle", "settings", "button-small", `aria-expanded="${F.setupExpanded}" aria-controls="fleet-start-form"`) : '<span class="badge">Ready to set up</span>'}</div><form id="fleet-start-form" class="panel-body"${f && f.status !== "stopped" && !F.setupExpanded ? " hidden" : ""}><div class="field-grid"><div class="field"><label for="fleet-account">Your member account</label><select id="fleet-account" required><option value="">Choose your account…</option>${list(A.state.accounts).map((account) => `<option value="${esc(account.account)}"${F.setup.account === account.account ? " selected" : ""}>${esc(account.account)}</option>`).join("")}</select><span class="field-hint">Select the account that owns your results folder.</span></div>${[["glm", "fleet-workers", "GLM agents", "workers"], ["deepseek", "fleet-deepseek-workers", "DeepSeek agents", "deepseek_workers"]].map(([id, inputId, label, key]) => { const provider = list(F.data?.providers).find((p) => p.id === id); return `<div class="field"><label for="${inputId}">${label} working at once</label><select id="${inputId}">${Array.from({length: 11}, (_, i) => `<option value="${i}"${Number(F.setup[key]) === i ? " selected" : ""}>${i}${i === 10 ? " — full pool" : i === 0 ? " — do not dispatch" : i === 1 ? " — one task at a time" : ""}</option>`).join("")}</select><span class="field-hint">${esc(provider?.model_id || (id === "glm" ? "GLM-5.3-flash" : "DeepSeek v4.1 flash 1017"))}. A fresh worker starts for each task and retires afterward.</span>${provider?.available === false ? `<span class="field-error">${esc(provider.error || "This provider is unavailable. Choose zero for this pool until the connection is ready.")}</span>` : ""}</div>`; }).join("")}<div class="field fleet-destination"><label for="fleet-destination">Save reports in</label><div class="input-action"><input id="fleet-destination" value="${esc(F.setup.output_dir)}" placeholder="Choose an existing folder in your account" required autocomplete="off">${button("Browse folders", "fleet-browse", "folder", "", !F.setup.account ? "disabled" : "")}</div><span class="field-hint">Choose or create a project folder with Browse folders. Nothing starts until this destination is checked.</span></div><div class="field fleet-scope"><label for="fleet-scope">Repositories to include</label><select id="fleet-scope"><option value="all"${!chosen ? " selected" : ""}>All accessible writable repositories</option><option value="selected"${chosen ? " selected" : ""}>Choose several repositories</option></select><span class="field-hint">Includes projects you own, collaborate on, or can write to through an organization. New accessible projects join future scans.</span></div></div>${chosen ? `<div class="fleet-repo-selection"><label for="fleet-repo-query" class="field-label">Find repositories to include</label><div class="input-action"><input id="fleet-repo-query" value="${esc(F.repoQuery)}" placeholder="Repository name or owner" autocomplete="off">${button("Search GitHub", "fleet-search-repos", "search", "", F.searching ? "disabled" : "")}</div><div id="fleet-repo-results">${fleetRepoOptions()}</div></div>` : ""}<div class="field fleet-dry-run"><label for="fleet-dry-run"><input type="checkbox" id="fleet-dry-run"${F.setup.dry_run ? " checked" : ""}> Dry run — plan and verify, write nothing to GitHub</label><span class="field-hint">Reviews, checks and the exact comments or pull requests it would send are recorded in your report folder. No comment, push, pull request or branch change is sent.</span></div><div class="fleet-start-footer"><p><strong>Dry run is on by default.</strong> Starting authorizes reviews, narrow comment fixes and correction pull requests in that mode; nothing is sent to GitHub. Uncheck it only after the recorded actions match what you would approve. You make the merge decision. Known project types use their recognized checks. If FatChin cannot identify a check, add the project’s verification command from its waiting item.</p><button id="fleet-start" type="submit" class="button button-primary"${F.busy || F.loading || (f && !["stopped"].includes(f.status)) ? " disabled" : ""}>${icon("play")}Dispatch ${number(F.setup.workers + F.setup.deepseek_workers)} agents</button></div>${f && f.status !== "stopped" ? '<p class="field-hint">Your fleet is already configured. Stop it before dispatching a replacement with different settings.</p>' : ""}</form><div id="fleet-controls" class="panel-body fleet-controls">${fleetControls()}</div></section><div class="fleet-explainer"><div>${icon("refresh")}<span><strong>Recovery is automatic.</strong> A crashed worker resets itself. Every new task gets a fresh process and conversation. Repeated failures are shown for attention while healthy agents continue.</span></div><div>${icon("shield")}<span><strong>You stay in charge of merging.</strong> Pause finishes current tasks. Stop blocks new writes; a request already sent to GitHub may still finish.</span></div></div><div class="fleet-window-filter"><div class="search-input">${icon("search")}<input id="fleet-search" type="search" value="${esc(F.search)}" placeholder="Search this window" aria-label="Search fleet items"></div><div class="field"><label for="fleet-repo-filter" class="sr-only">Filter by repository</label><select id="fleet-repo-filter"><option value="all">Every repository</option>${known.map((repo) => `<option value="${esc(repo)}"${F.repoFilter === repo ? " selected" : ""}>${esc(repo)}</option>`).join("")}</select></div><span id="fleet-updated" class="field-hint">${F.lastGood ? `Updated ${esc(ago(F.lastGood))}` : "Waiting for fleet status"}</span></div><div id="fleet-monitor">${fleetWindow()}</div>`;
  }
  function fleetRepoOptions() {
    const options = new Map([...F.setup.repos.map((full_name) => ({full_name})), ...F.repoResults].map((r) => [r.full_name, r]));
    return `${F.repoError ? `<p role="alert" class="field-error">${esc(F.repoError)}</p>` : ""}${F.searching ? loading("Searching your GitHub repositories") : ""}<p class="field-hint">${number(F.setup.repos.length)} selected. Search results can be limited; searching again keeps your selections.</p>${options.size ? `<div class="fleet-repo-options">${[...options.values()].map((repo) => `<label class="fleet-repo-option"><input type="checkbox" id="fleet-repo-${esc(encodeURIComponent(repo.full_name))}" data-fleet-repo="${esc(repo.full_name)}"${F.setup.repos.includes(repo.full_name) ? " checked" : ""}><span><strong>${esc(repo.full_name)}</strong>${repo.description ? `<span>${esc(repo.description)}</span>` : ""}</span></label>`).join("")}</div>` : '<p class="field-hint">Search by name, then select every project you want to include.</p>'}`;
  }
  function updateFleetMonitor() {
    if (A.view !== "fleet") return;
    const root = $("#fleet-monitor");
    if (!root) return;
    const restore = rememberFocus(root);
    const details = $$("details", root).map((el) => el.open);
    root.innerHTML = fleetWindow();
    $$("details", root).forEach((el, i) => { if (i < details.length) el.open = details[i]; });
    restore();
    const controls = $("#fleet-controls");
    if (controls) {const focus = rememberFocus(controls); controls.innerHTML = fleetControls(); focus();}
    const repoFilter = $("#fleet-repo-filter");
    if (repoFilter && document.activeElement !== repoFilter) {
      const repos = [...new Set([...list(A.state.repos).map(repoName), ...watchedRepositories().map((r) => r.repo), ...list(F.data?.items).map((row) => row.repo), ...F.setup.repos].filter(Boolean))].sort();
      repoFilter.innerHTML = '<option value="all">Every repository</option>' + repos.map((repo) => `<option value="${esc(repo)}"${repo === F.repoFilter ? " selected" : ""}>${esc(repo)}</option>`).join("");
      if (F.repoFilter !== "all" && !repos.includes(F.repoFilter)) repoFilter.add(new Option(F.repoFilter, F.repoFilter, true, true));
    }
    if ($("#fleet-updated")) $("#fleet-updated").textContent = F.lastGood ? `Updated ${ago(F.lastGood)}` : "Waiting for fleet status";
    if ($("#fleet-start")) $("#fleet-start").disabled = !!(F.busy || F.loading || (currentFleet() && currentFleet().status !== "stopped"));
  }
  async function loadFleet() {
    if (F.loading || F.busy) return F.loading;
    const version = F.version, initialLoad = !F.data;
    F.loading = (async () => {
      try { const data = await api("/api/fleet"); if (version === F.version) {F.data = data; restoreFleetSetup(); F.error = ""; F.lastGood = new Date().toISOString();} }
      catch (error) { if (version === F.version) F.error = error.message; }
      finally { F.loading = null; if (initialLoad && F.data && A.view === "fleet") renderView(); else updateFleetMonitor(); }
    })();
    updateFleetMonitor();
    return F.loading;
  }
  async function mutateFleet(action, extra = {}) {
    if (F.busy) return;
    F.busy = true; F.version++; F.error = ""; updateFleetMonitor();
    try {
      F.data = await api("/api/fleet", {action, ...extra}, {timeout: 60000});
      restoreFleetSetup();
      await refresh(false);
      F.lastGood = new Date().toISOString();
      if (action === "start") F.setupExpanded = false;
      if (action === "stop") F.setupExpanded = true;
      toast(({start: "Fleet dispatched. Follow its work below.", pause: "Pause requested. Current tasks can finish.", resume: "Fleet resumed.", stop: "Stop requested. Requests already sent to GitHub may still finish.", retry: "Retry requested. The item keeps its history.", refresh: "A fresh scan has been requested.", configure_checks: "Project checks saved. Waiting fixes will be picked up automatically."})[action] || "Fleet updated.");
    } catch (error) { F.error = error.message; }
    finally { F.busy = false; if (A.view === "fleet" || (A.view === "settings" && !A.settingsDirty)) renderView(); }
  }
  function restoreFleetSetup() {
    if (F.setup.account) return;
    const existing = list(F.data?.fleets).find((f) => f.status !== "stopped");
    if (!existing) return;
    Object.assign(F.setup, {account: existing.account, output_dir: existing.output_dir || "", scope: existing.scope || "all",
      repos: [...list(existing.repos)], workers: Number(existing.workers || 0), deepseek_workers: Number(existing.deepseek_workers || 0)});
    F.setupExpanded = false;
  }
  function bindFleet() {
    if (!F.data && !F.loading && !F.error) loadFleet();
    $("#fleet-start-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      if (F.setup.workers + F.setup.deepseek_workers < 1) {F.error = "Choose at least one agent from either pool."; updateFleetMonitor(); return;}
      const unavailable = list(F.data?.providers).find((provider) => provider.available === false && Number(F.setup[provider.id === "deepseek" ? "deepseek_workers" : "workers"]) > 0);
      if (unavailable) {F.error = unavailable.error || `${unavailable.label || unavailable.id} is unavailable. Select zero for that pool to use the other provider.`; updateFleetMonitor(); return;}
      if (F.setup.scope === "selected" && !F.setup.repos.length) {F.error = "Choose at least one repository, or include all accessible writable repositories."; updateFleetMonitor(); return;}
      if (!F.setup.account || !F.setup.output_dir.trim()) {F.error = "Choose your account and an existing results folder before dispatching agents."; updateFleetMonitor(); return;}
      mutateFleet("start", {...F.setup, output_dir: F.setup.output_dir.trim(), repos: F.setup.scope === "all" ? [] : [...F.setup.repos]});
    });
    $("#fleet-account")?.addEventListener("change", (event) => {
      F.setup.account = event.target.value;
      const f = currentFleet();
      F.setupExpanded = !f || f.status === "stopped";
      F.setup.output_dir = f?.output_dir || "";
      F.setup.scope = f?.scope || "all"; F.setup.repos = [...list(f?.repos)]; F.setup.workers = Number(f?.workers ?? 10); F.setup.deepseek_workers = Number(f?.deepseek_workers ?? 10); F.setup.dry_run = Boolean(f?.dry_run);
      renderView();
    });
    for (const [id, key] of [["fleet-workers", "workers"], ["fleet-deepseek-workers", "deepseek_workers"]]) $("#" + id)?.addEventListener("change", (event) => {F.setup[key] = Number(event.target.value); $("#fleet-start").innerHTML = `${icon("play")}Dispatch ${number(F.setup.workers + F.setup.deepseek_workers)} agents`;});
    $("#fleet-destination")?.addEventListener("input", (event) => {F.setup.output_dir = event.target.value;});
    $("#fleet-dry-run")?.addEventListener("change", (event) => {F.setup.dry_run = event.target.checked;});
    $("#fleet-scope")?.addEventListener("change", (event) => {F.setup.scope = event.target.value; renderView();});
    $("#fleet-repo-query")?.addEventListener("input", (event) => {F.repoQuery = event.target.value;});
    $("#fleet-repo-query")?.addEventListener("keydown", (event) => {if (event.key === "Enter") {event.preventDefault(); fleetAction({dataset: {action: "fleet-search-repos"}});}});
    $("#fleet-start-form")?.addEventListener("change", (event) => {
      const repo = event.target.dataset.fleetRepo;
      if (!repo) return;
      F.setup.repos = event.target.checked ? [...new Set([...F.setup.repos, repo])] : F.setup.repos.filter((r) => r !== repo);
      const root = $("#fleet-repo-results"), restore = rememberFocus(root); root.innerHTML = fleetRepoOptions(); restore();
    });
    $("#fleet-search")?.addEventListener("input", (event) => {F.search = event.target.value; F.visibleLimit = 200; updateFleetMonitor();});
    $("#fleet-repo-filter")?.addEventListener("change", (event) => {F.repoFilter = event.target.value; updateFleetMonitor();});
    $("#fleet-monitor")?.addEventListener("keydown", (event) => {
      if (!event.target.matches('[role="tab"]') || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const ids = ["agents", "ready", "comments", "activity"], index = ids.indexOf(F.tab);
      F.tab = event.key === "Home" ? ids[0] : event.key === "End" ? ids[3] : ids[(index + (event.key === "ArrowRight" ? 1 : -1) + ids.length) % ids.length];
      updateFleetMonitor(); $("#fleet-tab-" + F.tab).focus();
    });
  }
  async function fleetAction(element) {
    const action = element.dataset.action;
    if (action === "fleet-open-item") {F.tab = "agents"; F.search = element.dataset.title || element.dataset.repo || ""; F.repoFilter = element.dataset.repo || "all"; F.visibleLimit = 200; location.hash = "fleet"; if (A.view === "fleet") renderView(); return;}
    if (action === "fleet-setup-toggle") {F.setupExpanded = !F.setupExpanded; renderView(); return;}
    if (action === "fleet-show-more") {F.visibleLimit += 200; updateFleetMonitor(); return;}
    if (action === "fleet-tab") {F.tab = element.dataset.tab; updateFleetMonitor(); return;}
    if (action === "fleet-browse") return browseFolders(F.setup.account, F.setup.output_dir, (path) => {F.setup.output_dir = path; if ($("#fleet-destination")) $("#fleet-destination").value = path;});
    if (action === "fleet-refresh") {if (!F.data) return loadFleet(); return mutateFleet("refresh", currentFleet() ? {fleet_id: currentFleet().id} : {});}
    if (action === "fleet-configure-checks") {
      const repo = element.dataset.repo;
      const config = list(A.state.repos).find((r) => repoName(r) === repo);
      const item = list(F.data?.items).find((r) => r.id === element.dataset.id);
      openDialog("Set this project’s verification command", "CONTINUE A WAITING FIX", `<p class="dialog-copy">FatChin checks a correction before publishing it. Save the command you normally use to test <strong>${esc(repo)}</strong>; waiting fixes will resume automatically after the checks are saved.</p>${item?.error ? info(esc(item.error), "warning") : ""}<form id="fleet-checks-form"><div class="field"><label for="fleet-checks-command">Command that checks this project</label><textarea id="fleet-checks-command" rows="4" required placeholder="For example: npm test">${esc(list(config?.checks).join("\n"))}</textarea><span class="field-hint">Use a command from the project’s documentation. Examples: <code>npm test</code>, <code>python -m pytest</code>, or <code>cargo test</code>. Put separate commands on separate lines. These commands run in an isolated copy of this project.</span></div><div class="field spaced-top"><label for="fleet-checks-base">Default branch, if known</label><input id="fleet-checks-base" value="${esc(config?.base_branch || "")}" placeholder="Leave empty to use GitHub’s default branch"><span class="field-hint">A branch is a line of saved work, often called main or master.</span></div></form>`, `${button("Cancel", "dialog-close")}<button class="button button-primary" type="submit" form="fleet-checks-form">${icon("check")}Save and continue</button>`);
      const version = A.dialogVersion;
      $("#fleet-checks-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (F.busy) return;
        const checks = $("#fleet-checks-command").value.split("\n").map((s) => s.trim()).filter(Boolean);
        const base = $("#fleet-checks-base").value.trim();
        if (!checks.length) return dialogError(new Error("Enter the command this project uses to check a change."));
        const payload = {repo, checks}; if (base) payload.base_branch = base;
        await mutateFleet("configure_checks", payload);
        if (!currentDialog(version)) return;
        if (F.error) dialogError(new Error(F.error));
        else {closeDialog(); refresh(false);}
      });
      return;
    }
    if (action === "fleet-search-repos") {
      if (F.searching) return;
      const q = F.repoQuery.trim();
      if (!q) {F.repoError = "Enter a repository name or owner to search."; $("#fleet-repo-results").innerHTML = fleetRepoOptions(); return;}
      F.searching = true; F.repoError = ""; $("#fleet-repo-results").innerHTML = fleetRepoOptions();
      try {const reply = await api(`/api/github/repositories?${query({q})}`); F.repoResults = list(reply.items); if (!F.repoResults.length) F.repoError = "No accessible repositories matched. Try the owner or a shorter name.";}
      catch (error) {F.repoError = error.message;}
      finally {F.searching = false; if ($("#fleet-repo-results")) $("#fleet-repo-results").innerHTML = fleetRepoOptions();}
      return;
    }
    const operation = {"fleet-pause": "pause", "fleet-resume": "resume", "fleet-stop": "stop", "fleet-retry": "retry"}[action];
    if (operation) return mutateFleet(operation, action === "fleet-retry" ? {item_id: element.dataset.id, fleet_id: element.dataset.fleetId} : {fleet_id: element.dataset.id});
  }

  function renderView() {
    if (!A.state) return;
    const sameView = A.renderedView === A.view;
    const restoreFocus = sameView ? rememberFocus(main) : () => {};
    const details = sameView
      ? $$("details", main).map((element) => element.open)
      : [];
    const renderers = {
      overview: renderOverview,
      jobs: renderJobs,
      doorbell: renderDoorbell,
      repos: renderRepos,
      watches: renderWatches,
      fence: renderFence,
      settings: renderSettings,
      help: renderHelp,
      learn: renderLearn,
      fleet: renderFleet,
    };
    main.innerHTML = fleetWorkspaceStatus() + (renderers[A.view] || renderOverview)();
    bindView();
    if (sameView)
      $$("details", main).forEach((element, index) => {
        if (index < details.length) element.open = details[index];
      });
    A.renderedView = A.view;
    restoreFocus();
  }
  function bindView() {
    $("#coverage-search")?.addEventListener("input", (e) => {
      const start = e.target.selectionStart;
      A.coverageSearch = e.target.value;
      renderView();
      const input = $("#coverage-search"); input.focus(); input.setSelectionRange(start, start);
    });
    if (A.view === "learn") bindLearn();
    if (A.view === "fleet") bindFleet();
    $("#jobs-search")?.addEventListener("input", (e) => {
      const start = e.target.selectionStart;
      A.jobsSearch = e.target.value;
      renderView();
      const el = $("#jobs-search");
      el.focus();
      el.setSelectionRange(start, start);
    });
    $("#jobs-filter")?.addEventListener("change", (e) => {
      A.jobsFilter = e.target.value;
      renderView();
    });
    $("#jobs-repo-filter")?.addEventListener("change", (e) => {
      A.repoFilter = e.target.value;
      renderView();
    });
    $("#settings-form")?.addEventListener("input", () => {
      A.settingsDirty = true;
      $("#settings-save-hint").textContent = "You have unsaved changes.";
    });
    $("#settings-form")?.addEventListener("submit", saveSettings);
    $("#command-user-add")?.addEventListener("click", (e) =>
      busy(e.currentTarget, () =>
        addCommandUser($("#command-user-lookup").value.trim()),
      ),
    );
    $("#command-user-lookup")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        busy($("#command-user-add"), () =>
          addCommandUser(e.target.value.trim()),
        );
      }
    });
    $("#allowed_command_users")?.addEventListener("change", loadCommandUsers);
    if ($("#command-user-list")) loadCommandUsers();
  }
  async function loadCommandUsers() {
    const version = ++A.commandUsersVersion;
    const field = $("#allowed_command_users");
    const root = $("#command-user-list");
    if (!field || !root) return;
    const ids = [
      ...new Set(field.value.split(/[\s,]+/).filter((v) => /^\d+$/.test(v))),
    ];
    if (A.state.github?.id)
      commandUserNames.set(String(A.state.github.id), A.state.github.login);
    function renderUsers() {
      if (!root.isConnected || A.commandUsersVersion !== version) return;
      root.innerHTML = ids.length
        ? ids
            .map(
              (id) =>
                `<span class="user-chip"><span>${esc(commandUserNames.get(id) || `GitHub user ${id}`)}</span><button type="button" data-action="remove-command-user" data-id="${esc(id)}" aria-label="Remove ${esc(commandUserNames.get(id) || `GitHub user ${id}`)}">${icon("x")}</button></span>`,
            )
            .join("")
        : '<span class="field-hint">No GitHub comment users are configured. Add a username or your connected account.</span>';
    }
    renderUsers();
    const unresolved = ids.filter((id) => !commandUserNames.has(id));
    for (let start = 0; start < unresolved.length; start += 5) {
      const results = await Promise.allSettled(
        unresolved.slice(start, start + 5).map(async (id) => {
          const user = await api(`/api/github/user?${query({ id })}`);
          if (user.login) commandUserNames.set(id, user.login);
        }),
      );
      if (!root.isConnected || A.commandUsersVersion !== version) return;
      renderUsers();
      if (results.some((r) => r.status === "rejected"))
        root.title =
          "Some GitHub usernames could not be loaded. Their permanent user IDs remain visible and can still be removed.";
    }
  }
  async function addCommandUser(login, connected = false) {
    try {
      if (!login && !connected)
        throw new Error(
          "Enter a GitHub username, or choose Add connected account.",
        );
      const user = await api(
        `/api/github/user?${query({ login: connected ? "" : login })}`,
      );
      if (!user.id)
        throw new Error("GitHub did not return an account ID for this user.");
      const field = $("#allowed_command_users");
      if (!field) return;
      const values = field.value.split(/[\s,]+/).filter(Boolean);
      commandUserNames.set(String(user.id), user.login);
      const duplicate = values.includes(String(user.id));
      if (!duplicate) values.push(String(user.id));
      field.value = values.join("\n");
      field.dispatchEvent(new Event("input", { bubbles: true }));
      $("#command-user-feedback").textContent =
        `${user.login} (${user.id}) ${duplicate ? "is already on the list." : "was added. Save settings and restart to apply."}`;
      $("#command-user-lookup").value = "";
      loadCommandUsers();
    } catch (error) {
      reportError(error);
    }
  }

  function openDialog(title, eyebrow, body, footer = "", wide = false) {
    A.dialogVersion++;
    if (A.githubTimer) {
      clearTimeout(A.githubTimer);
      A.githubTimer = null;
    }
    if (dialog.open) dialog.close();
    A.dialog = title;
    dialog.classList.toggle("wide", wide);
    $("#dialog-title").textContent = title;
    $("#dialog-eyebrow").textContent = eyebrow;
    $("#dialog-body").innerHTML = body;
    $("#dialog-footer").innerHTML = footer;
    dialog.showModal();
  }
  function closeDialog() {
    A.dialogVersion++;
    if (A.githubTimer) {
      clearTimeout(A.githubTimer);
      A.githubTimer = null;
    }
    dialog.close();
    A.dialog = null;
    A.wizard = null;
  }
  const currentDialog = (version) => dialog.open && A.dialogVersion === version;
  function confirmDialog(
    title,
    description,
    confirmText,
    callback,
    { danger = false, details = "" } = {},
  ) {
    openDialog(
      title,
      "PLEASE REVIEW",
      `<p class="dialog-copy">${esc(description)}</p>${details}`,
      `${button("Cancel", "dialog-close", "", "")}<button class="button ${danger ? "button-danger" : "button-primary"}" id="confirm-action">${esc(confirmText)}</button>`,
    );
    const version = A.dialogVersion;
    $("#confirm-action").addEventListener("click", (e) =>
      busy(e.currentTarget, async () => {
        try {
          await callback();
          if (currentDialog(version)) closeDialog();
          await refresh();
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
          else reportError(error);
        }
      }),
    );
  }

  const taskGuides = {
    "fix-pr": {when:"A pull request already has review feedback that needs a code change.",input:"Choose the pull request containing the feedback.",result:"Checked fixes may be pushed to the existing pull request; supported comments can be answered and resolved.",recovery:"Read failed checks before retrying. Stop prevents future steps; changes already pushed remain."},
    "implement-issue": {when:"An issue describes a change clearly enough to build.",input:"Choose the issue with the expected behavior and acceptance criteria.",result:"The worker can push a new branch, open a pull request and comment on the issue.",recovery:"Review the new pull request. Fix unclear requirements before retrying a failed task."},
    "explain-repo": {when:"You need a plain-language tour or an answer about unfamiliar code.",input:"Choose the project and, optionally, ask a specific question.",result:"A report with code references is saved to your folder. GitHub stays unchanged.",recovery:"Ask a narrower question if the answer needs more detail. Check cited files yourself."},
    "review-pr": {when:"You want an explanation of a proposed change and its likely problems.",input:"Choose the pull request and any review focus.",result:"A review draft explains the change. Read and edit it before choosing whether to post.",recovery:"Discard the draft to keep it off GitHub, or edit the wording before posting."},
    "summarise-pr": {when:"You need a short explanation of what a pull request changes.",input:"Choose the pull request to summarize.",result:"A summary is saved to your results folder; inspect task details for its recorded output.",recovery:"Read the original changes when the summary raises a question."},
    "write-docs": {when:"Setup instructions or project documentation need to match the code.",input:"Choose the project and describe the documentation to improve.",result:"A documentation pull request is prepared as a draft. Learning-session tasks ask you before posting; other tasks follow the project’s posting setting.",recovery:"Edit or discard the draft. Publishing runs the configured checks before GitHub is changed."},
    "triage-issues": {when:"Your notes describe several pieces of work that need clear issue descriptions.",input:"Paste your request or rough notes in Your words.",result:"Separate issue drafts are prepared. Learning-session tasks ask you before posting; other tasks follow the project’s posting setting.",recovery:"Edit unclear titles or descriptions before posting; discard drafts you do not need."},
    "organize-work": {when:"Issues need a small, useful set of labels so work is easier to find.",input:"Choose the project; FatChin reads its open issues.",result:"A label proposal lists where each label fits. Posting creates the labels; assigning them to issues remains your choice.",recovery:"Edit the proposed labels or discard the draft before posting."},
    "sync-branch": {when:"One existing branch needs changes that are already saved on another.",input:"Choose the branch to take changes from, then the branch to update.",result:"After your decision and successful checks, the branches are merged and the updated branch is pushed.",recovery:"A conflict stops the action. Inspect the reported files and resolve the conflict before trying again."},
    "backup-repo": {when:"You need a restorable copy of the repository’s Git branches and saved history.",input:"Choose the project and your results folder.",result:"A checked Git bundle is saved if it fits the 50 MB export limit. It excludes issues, pull request discussions, submodule repositories and large-file storage objects.",recovery:"Read the restore instructions in the report. Oversized backups remain in the recorded temporary location for recovery."},
    "hygiene-sweep": {when:"You want a report about common maintenance problems before deciding what to fix.",input:"Choose the project to inspect.",result:"Suggested fixes and their evidence are saved to your folder. The report does not automatically repair the project.",recovery:"Review each finding, then create a separate task for the fixes you want."},
    "new-project": {when:"You want a new private GitHub repository with starter files.",input:"Give the project a name and a one-sentence idea.",result:"A project proposal is prepared for your decision. Posting creates a private repository and its starter files.",recovery:"Check the name and contents before posting. Discard the proposal if you do not want a new repository."},
    "cleanup-merged": {when:"A pull request is already merged and you want to finish its follow-up work.",input:"Choose the merged pull request and, optionally, its release tag.",result:"After explicit confirmation, this can close linked issues, delete the merged branch and publish a release.",recovery:"Read every proposed change before confirming. Those GitHub changes may need separate manual recovery."},
  };
  function taskGuide(type) {
    const guide=taskGuides[type?.id];
    return guide?`<section class="task-guide" aria-label="About this kind of task"><h3>Is this the right task?</h3><dl><div><dt>Use it when</dt><dd>${esc(guide.when)}</dd></div><div><dt>What you provide</dt><dd>${esc(guide.input)}</dd></div><div><dt>What you get</dt><dd>${esc(guide.result)}</dd></div><div><dt>If you need to stop or correct it</dt><dd>${esc(guide.recovery)}</dd></div></dl></section>`:"";
  }
  function availableWizardTypes(w) {
    return w?.learning_session_id ? taskTypes().filter((t)=>["write-docs","triage-issues"].includes(t.id)) : taskTypes();
  }

  function preferredTaskType() {
    return taskTypes().find((type) => type.id === "fix-pr") || taskTypes()[0] || null;
  }
  function newTask(prefill = "") {
    A.wizard = {
      step: 0,
      repo: prefill || "",
      kind: preferredTaskType()?.id || "",
      number: "",
      target: null,
      account: "",
      output_dir: "",
      values: {},
      targets: [],
      loading: false,
      requestId: 0,
    };
    renderWizard(true);
    if (!taskTypes().length)
      loadTaskTypes()
        .then(() => {
          const w = A.wizard;
          if (!w) return;
          if (!w.kind) w.kind = preferredTaskType()?.id || "";
          renderWizard();
        })
        .catch((error) => {
          if (A.wizard && dialog.open) dialogError(error);
          else reportError(error);
        });
  }
  function wizardFlow(w) {
    return ["repo", "none"].includes(taskType(w.kind)?.takes) ? [0, 2, 3] : [0, 1, 2, 3];
  }
  function wizardSteps() {
    const w = A.wizard;
    const labels = ["Repository", "Work item", "Destination", "Review"];
    return `<div class="steps" aria-label="Task setup progress">${wizardFlow(w)
      .map(
        (step, i) =>
          `<div class="step ${step === w.step ? "active" : step < w.step ? "done" : ""}"${step === w.step ? ' aria-current="step"' : ""}><span class="step-number">${step < w.step ? icon("check") : i + 1}</span><span>${labels[step]}</span></div>`,
      )
      .join("")}</div>`;
  }
  function taskFieldInputs(type, w) {
    const fields = list(type.fields);
    if (!fields.length) return "";
    return `<div class="field-grid">${fields
      .map((item) => {
        const id = `task-field-${item.name}`;
        return `<div class="field"><label for="${esc(id)}">${esc(item.label || item.name)}${item.required || /optional/i.test(item.label || "") ? "" : " (optional)"}</label>${item.name === "words" ? `<textarea id="${esc(id)}" name="task-field" data-field="${esc(item.name)}" rows="5" maxlength="2000"${item.required ? " required" : ""}>${esc((w.values || {})[item.name] || "")}</textarea>` : `<input id="${esc(id)}" name="task-field" data-field="${esc(item.name)}" value="${esc((w.values || {})[item.name] || "")}" maxlength="2000"${item.required ? " required" : ""} autocomplete="off">`}</div>`;
      })
      .join("")}</div>`;
  }
  function renderWizard(initial = false) {
    const w = A.wizard;
    if (!w) return;
    const type = taskType(w.kind);
    const unchecked = !list(repos().find((r) => repoName(r) === w.repo)?.checks).length && (["write", "high-stakes"].includes(type?.profile) || w.kind === "write-docs") && type?.takes !== "none";
    const noun = type?.takes === "pr" ? "Pull request" : "Issue";
    const previousFocus = document.activeElement;
    const focusId = previousFocus?.id;
    const focusValue =
      previousFocus?.name === "task-kind" ? previousFocus.value : null;
    const stepChanged = w.renderedStep !== w.step;
    w.renderedStep = w.step;
    let body = "";
    if (w.step === 0)
      body = `<div class="wizard-stack"><p class="dialog-copy" style="margin:0">Start with the project and the kind of work you need.</p><div class="field"><label for="task-repo">${type?.takes === "none" ? "Existing repository (not needed for a new project)" : "Repository"}</label><select id="task-repo"${type?.takes === "none" ? " disabled" : " required"}><option value="">Choose a repository…</option>${repos()
        .map(
          (r) =>
            `<option value="${esc(repoName(r))}"${w.repo === repoName(r) ? " selected" : ""}>${esc(repoName(r))}</option>`,
        )
        .join(
          "",
        )}</select><span class="field-hint">Need another project? <button class="text-link" data-action="new-repo">Search GitHub and add a repository</button>.</span></div><div><span class="field-label" style="margin-bottom:9px">What would you like to do?</span>${
        taskTypes().length
          ? `<div class="choice-grid">${availableWizardTypes(w)
              .map(
                (item) =>
                  `<label class="choice-card${w.kind === item.id ? " selected" : ""}"><input type="radio" name="task-kind" value="${esc(item.id)}"${w.kind === item.id ? " checked" : ""}><span>${icon(item.takes === "pr" ? "pr" : item.takes === "issue" ? "issue" : "repo")}<strong>${esc(item.label)}</strong><p>${esc(item.help)}</p></span></label>`,
              )
              .join("")}</div>`
          : loading("Loading the kinds of work this worker can do")
      }</div>${type ? taskGuide(type) + taskFieldInputs(type, w) : ""}</div>`;
    if (w.step === 1)
      body = `<div class="wizard-stack"><div class="selection-summary">${icon("repo")}<div><strong>${esc(w.repo)}</strong><small>${esc(type?.label || kindLabel(w.kind))}</small></div></div><div class="field"><label for="task-target">${noun}</label><select id="task-target"><option value="">${w.loading ? "Loading work items…" : w.kind === "cleanup-merged" ? "Choose a finished pull request…" : "Choose an open item…"}</option>${w.targets.map((t) => `<option value="${t.number}"${String(w.number) === String(t.number) ? " selected" : ""}>#${t.number} — ${esc(t.title)}</option>`).join("")}</select><span class="field-hint">${w.kind === "cleanup-merged" ? "Finished pull requests are listed. Choose one that has already merged." : "Open issues or pull requests are listed."} You can also look up a known number below.</span></div><div class="field"><label for="task-number">Look up by number</label><div class="input-action"><input id="task-number" type="number" min="1" step="1" placeholder="For example, 42" value="${esc(w.number)}"><button class="button" id="lookup-target" type="button">${icon("search")}Look up</button></div></div>${w.target ? `<div class="selection-summary">${icon(type?.takes === "pr" ? "pr" : "issue")}<div><strong>#${esc(w.target.number)} · ${esc(w.target.title)}</strong><small>${esc(labelState(w.target.state))}${w.target.headRefName ? ` · ${esc(w.target.headRefName)}` : ""} · <a href="${targetURL(w.repo, w.kind, w.target.number)}" target="_blank" rel="noopener noreferrer">View on GitHub ${icon("external")}</a></small></div></div>` : ""}<div id="target-loading">${w.loading ? loading("Loading open items") : ""}</div></div>`;
    if (w.step === 2)
      body = `<div class="wizard-stack"><p class="dialog-copy" style="margin:0">${w.learning_session_id ? "This task keeps the lesson’s account and results folder. Its draft still waits for your decision." : "Choose where this task will save its report and results. Select your own account and a folder inside your home space."}</p>${destinationFields(w, "task")}${info("The worker uses a separate temporary checkout for code changes. Your selected folder keeps the task results. No destination is selected for you.")}</div>`;
    if (w.step === 3) {
      const answers = list(type?.fields)
        .map((item) => {
          const value = String((w.values || {})[item.name] ?? "").trim();
          return value
            ? `<div><dt>${esc(item.label || item.name)}</dt><dd>${esc(value)}</dd></div>`
            : "";
        })
        .join("");
      const itemRow =
        ["repo", "none"].includes(type?.takes)
          ? ""
          : `<div><dt>${noun}</dt><dd>#${esc(w.number)} · ${esc(w.target?.title || "")}</dd></div>`;
      const source =
        ["repo", "none"].includes(type?.takes)
          ? "this task’s answers and any selected project context"
          : noun === "Pull request"
            ? "the selected pull request and review history"
            : "the selected issue and its discussion";
      const writes = taskGuides[type?.id]?.result || (
        type?.takes === "repo"
          ? "The task details list anything it changes on GitHub. Nothing is posted for you without a confirmation."
          : noun === "Pull request"
            ? "It may push commits, post comments, and resolve supported review threads on the selected pull request."
            : "It may push a branch, open a pull request, and post a result comment on the issue.");
      body = `<div class="wizard-stack"><dl class="review-list"><div><dt>Work</dt><dd>${esc(type?.label || kindLabel(w.kind))}</dd></div><div><dt>Repository</dt><dd>${esc(type?.takes === "none" ? "A new private project" : w.repo)}</dd></div>${itemRow}${answers}<div><dt>Results folder</dt><dd>${esc(w.output_dir)}<small>Account: ${esc(w.account)}</small></dd></div><div><dt>Model</dt><dd>${esc(model().model_id)}<small>Local worker controls: up to ${number(model().max_calls_per_job)} logical calls for this task; ${number(model().max_calls_per_day)} shared per UTC day. These values do not describe your provider’s subscription allowance. A call may retry up to three HTTP attempts.</small></dd></div><div><dt>Checks</dt><dd>${
        list(repos().find((r) => repoName(r) === w.repo)?.checks)
          .map((c) => `<code>${esc(c)}</code>`)
          .join("<br>") || "No verification commands configured"
      }</dd></div></dl>${unchecked ? `<label class="no-check-ack"><input id="task-no-check-ack" type="checkbox"${w.accept_no_checks ? " checked" : ""}><span><strong>No test commands are saved for this project.</strong> I accept publishing without saved verification commands for this one task. GitHub rules and workflow checks still apply. To avoid this exception, cancel and add checks in Repositories first.</span></label>` : ""}${targetContext(w.target)}<div class="effects"><strong>What happens when you queue this task</strong><ul><li>The worker reads ${source} and works in an isolated checkout.</li><li>It uses the configured model connection for coding requests.</li><li>${type?.profile === "read-only" ? "Read-only tasks save a report; inspect its evidence before acting on its suggestions." : "Before code is published, the required saved checks must succeed. Draft tasks wait for the posting decision shown in Checks before publishing."}</li><li>${writes}</li><li>A report and results are written to your chosen folder.</li>${
        w.kind === "fix-pr"
          ? "<li>After success, this pull request is watched automatically. New inline review feedback can queue further model requests and GitHub updates within the local worker call controls. Remove this in Review watches when no longer needed.</li>"
          : w.kind !== "implement-issue"
            ? ""
            : "<li>After success, the pull request it opens is watched automatically. New inline review feedback can queue further model requests and GitHub updates within the local worker call controls. Remove this in Review watches when no longer needed.</li>"
      }</ul></div>${!A.state.service?.active ? info("The worker is stopped. Start it from Overview or Settings before queuing a task.", "warning") : ""}</div>`;
    }
    if (initial)
      openDialog("Create a task", "A FEW DETAILS, THEN WE’RE OFF", "");
    $("#dialog-body").innerHTML = wizardSteps() + body;
    $("#dialog-footer").innerHTML =
      `${button(w.step ? "Back" : "Cancel", w.step ? "wizard-back" : "dialog-close", w.step ? "back" : "")}<div class="footer-right">${w.step === 3 ? `<button class="button button-primary" id="queue-task"${A.state.service?.active ? "" : " disabled"}>${icon("play")}Queue task</button>` : button("Continue", "wizard-next", "arrow", "button-primary")}</div>`;
    $("#task-repo")?.addEventListener("change", (e) => {
      w.requestId++;
      w.loading = false;
      w.repo = e.target.value;
      w.accept_no_checks=false;
      w.number = "";
      w.target = null;
      w.targets = [];
    });
    $$('input[name="task-kind"]').forEach((el) =>
      el.addEventListener("change", (e) => {
        w.kind = e.target.value;
        w.accept_no_checks=false;
        w.requestId++;
        w.loading = false;
        w.number = "";
        w.target = null;
        w.targets = [];
        renderWizard();
      }),
    );
    $$('[data-field]').forEach((el) =>
      el.addEventListener("input", (e) => {
        w.values[e.target.dataset.field] = e.target.value;
      }),
    );
    $("#task-target")?.addEventListener("change", (e) => {
      w.number = e.target.value;
      w.target = null;
      if (w.number) loadWizardTarget(w);
      else renderWizard();
    });
    $("#task-number")?.addEventListener("input", (e) => {
      w.number = e.target.value;
      w.target = null;
    });
    $("#lookup-target")?.addEventListener("click", (e) =>
      busy(e.currentTarget, () => loadWizardTarget(w)),
    );
    bindDestination(w, "task");
    if (w.learning_session_id) ["#task-account", "#task-browse"].forEach((selector) => {const el=$(selector);if(el)el.disabled=true;});
    $("#task-no-check-ack")?.addEventListener("change", (e) => {w.accept_no_checks=e.target.checked;});
    $("#queue-task")?.addEventListener("click", (e) =>
      busy(e.currentTarget, async () => {
        const version = A.dialogVersion;
        try {
          if (unchecked && !w.accept_no_checks) throw new Error("Add project check commands first, or explicitly accept publishing without them for this task.");
          const request = {
            repo: w.repo,
            kind: w.kind,
            account: w.account,
            output_dir: w.output_dir,
          };
          if (w.accept_no_checks === true) request.accept_no_checks = true;
          if (w.learning_session_id) request.learning_session_id = w.learning_session_id;
          if (["issue", "pr"].includes(type?.takes)) request.number = Number(w.number);
          for (const item of list(type?.fields)) {
            const value = String((w.values || {})[item.name] ?? "").trim();
            if (value) request[item.name] = value;
          }
          const r = await api("/api/jobs", request);
          const relevant = currentDialog(version) && A.wizard === w;
          if (relevant) {
            closeDialog();
            location.hash = "jobs";
          }
          await refresh(relevant);
          toast("Task queued. Open it in Tasks to follow progress.");
          if (relevant && r.job && A.view === "jobs" && !dialog.open)
            showJob(r.job);
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
          else reportError(error);
        }
      }),
    );
    if (stepChanged)
      (
        $("#dialog-body select, #dialog-body input, #queue-task") ||
        $("#dialog-close")
      ).focus();
    else if (!previousFocus?.isConnected) {
      if (focusValue)
        $(`input[name="task-kind"][value="${focusValue}"]`)?.focus();
      else if (focusId) document.getElementById(focusId)?.focus();
    }
  }
  async function loadWizardTarget(w) {
    const requestId = ++w.requestId;
    const requested = w.number,
      repo = w.repo,
      kind = w.kind;
    const stillCurrent = () =>
      A.wizard === w &&
      w.step === 1 &&
      w.requestId === requestId &&
      w.repo === repo &&
      w.kind === kind &&
      w.number === requested;
    try {
      if (!/^\d+$/.test(String(w.number)) || Number(w.number) < 1)
        throw new Error("Enter a positive issue or pull request number.");
      w.loading = true;
      renderWizard();
      const r = await api(
        `/api/github/target?${query({ repo, kind, number: requested })}`,
      );
      if (!stillCurrent()) return;
      w.target = r.item;
      w.loading = false;
      renderWizard();
    } catch (error) {
      if (!stillCurrent()) return;
      w.loading = false;
      renderWizard();
      dialogError(error);
    }
  }
  async function wizardNext() {
    const w = A.wizard;
    try {
      if (w.step === 0) {
        if (!w.kind) {
          await loadTaskTypes();
          if (!w.kind) w.kind = preferredTaskType()?.id || "";
        }
        const type = taskType(w.kind);
        if (!type) {
          renderWizard();
          throw new Error("Choose the kind of work you need.");
        }
        if (type.takes !== "none" && !w.repo) throw new Error("Choose a repository to continue.");
        for (const item of list(type.fields)) {
          const value = String((w.values || {})[item.name] ?? "").trim();
          if (item.required && !value)
            throw new Error(
              `Fill in “${item.label || item.name}” before continuing.`,
            );
        }
        if (["repo", "none"].includes(type.takes)) {
          w.step = 2;
          renderWizard();
          return;
        }
        w.step = 1;
        const requestId = ++w.requestId,
          repo = w.repo,
          kind = w.kind;
        const stillCurrent = () =>
          A.wizard === w &&
          w.step === 1 &&
          w.requestId === requestId &&
          w.repo === repo &&
          w.kind === kind;
        w.loading = true;
        renderWizard();
        try {
          const r = await api(
            `/api/github/targets?${query({ repo, kind, state: type.allows_closed || kind === "cleanup-merged" ? "closed" : "open" })}`,
          );
          if (!stillCurrent()) return;
          w.targets = r.items || [];
        } catch (error) {
          if (!stillCurrent()) return;
          w.loading = false;
          renderWizard();
          dialogError(error);
          return;
        }
        w.loading = false;
        renderWizard();
        return;
      }
      if (w.step === 1) {
        if (w.kind === "cleanup-merged" && String(w.target?.state).toLowerCase() !== "merged") throw new Error("Choose a pull request that has already merged. Closing a pull request without merging it does not complete its changes.");
        if (!w.target || String(w.target.number) !== String(w.number))
          throw new Error(
            "Choose an open item or look up its number before continuing.",
          );
        if (
          w.kind !== "cleanup-merged" && !taskType(w.kind)?.allows_closed &&
          ["closed", "merged"].includes(String(w.target.state).toLowerCase())
        )
          throw new Error(
            "Choose an open issue or pull request. This item is closed.",
          );
      }
      if (w.step === 2) {
        validateDestination(w);
        if (w.learning_session_id && (w.account !== L.session?.account || w.output_dir !== L.session?.output_dir)) throw new Error("A task created from this lesson uses the lesson’s account and results folder. Start a separate task to choose a different destination.");
      }
      w.step++;
      renderWizard();
    } catch (error) {
      dialogError(error);
    }
  }

  function destinationFields(obj, prefix) {
    return `<div class="field"><label for="${prefix}-account">Your account</label><select id="${prefix}-account" required><option value="">Choose your account…</option>${list(
      A.state.accounts,
    )
      .map(
        (a) =>
          `<option value="${esc(a.account)}"${obj.account === a.account ? " selected" : ""}>${esc(a.label || a.account)} · ${esc(a.path)}</option>`,
      )
      .join(
        "",
      )}</select><span class="field-hint">Select the account that owns your output folder.</span></div><div class="field"><label for="${prefix}-destination">Destination folder</label><div class="input-action"><input id="${prefix}-destination" value="${esc(obj.output_dir)}" placeholder="Choose a folder in your account" readonly aria-describedby="${prefix}-folder-hint"><button class="button" id="${prefix}-browse" type="button"${obj.account ? "" : " disabled"}>${icon("folder")}Browse folders</button></div><span id="${prefix}-folder-hint" class="field-hint">Use the folder browser to select or create a real folder. Results are written here.</span></div>`;
  }
  function validateDestination(obj) {
    if (!obj.account) throw new Error("Choose your account.");
    if (!obj.output_dir)
      throw new Error(
        "Error: No destination folder specified. Supply an output path within your user space.",
      );
    const home = list(A.state.accounts).find(
      (a) => a.account === obj.account,
    )?.path;
    if (home && obj.output_dir.replace(/\/$/, "") === home.replace(/\/$/, ""))
      throw new Error(
        "Choose or create a project folder inside your home folder. Your whole home folder cannot be used as the results destination.",
      );
  }
  function bindDestination(obj, prefix) {
    $(`#${prefix}-account`)?.addEventListener("change", (e) => {
      obj.account = e.target.value;
      obj.output_dir = "";
      $(`#${prefix}-destination`).value = "";
      $(`#${prefix}-browse`).disabled = !obj.account;
    });
    $(`#${prefix}-browse`)?.addEventListener("click", () =>
      browseFolders(obj.account, obj.output_dir, (path) => {
        obj.output_dir = path;
        const el = $(`#${prefix}-destination`);
        if (el) el.value = path;
      }),
    );
  }
  async function browseFolders(account, path, callback) {
    A.folder = { account, path: path || "", callback, data: null };
    $("#folder-body").innerHTML = loading("Opening your folders");
    $("#folder-footer").innerHTML = "";
    folderDialog.showModal();
    await loadFolders(path);
  }
  async function loadFolders(path) {
    const folder = A.folder;
    if (!folder) return;
    const requestId = (folder.requestId = (folder.requestId || 0) + 1);
    const stillCurrent = () =>
      A.folder === folder &&
      folderDialog.open &&
      folder.requestId === requestId;
    $("#folder-body").innerHTML = loading("Loading folders");
    $("#folder-footer").innerHTML = button("Cancel", "folder-close");
    try {
      const r = await api(
        `/api/folders?${query({ account: folder.account, path: path || "" })}`,
      );
      if (!stillCurrent()) return;
      folder.data = r;
      folder.path = r.path;
      const home = list(A.state.accounts).find(
        (a) => a.account === folder.account,
      )?.path;
      const atHome =
        home && r.path.replace(/\/$/, "") === home.replace(/\/$/, "");
      $("#folder-body").innerHTML =
        `<p class="dialog-copy">Browse folders in <strong>${esc(folder.account)}</strong>’s account. Select this folder, or open a subfolder.</p><div class="folder-location"><button class="icon-button" id="folder-up" aria-label="Go to parent folder"${r.parent ? "" : " disabled"}>${icon("back")}</button><code title="${esc(r.path)}">${esc(r.path)}</code></div><div class="folder-list" aria-label="Subfolders">${
          list(r.items).length
            ? list(r.items)
                .map(
                  (item, i) =>
                    `<button class="folder-row" data-folder-index="${i}">${icon("folder")}<span>${esc(item.name)}</span>${icon("chevron")}</button>`,
                )
                .join("")
            : empty(
                "folder",
                "No subfolders",
                "You can use this folder or create a new one below.",
              )
        }</div><label for="folder-new-name" class="field-label">Create a new folder here</label><form id="folder-create-form" class="folder-create" style="margin-top:7px"><input id="folder-new-name" name="name" aria-label="New folder name" placeholder="New folder name" maxlength="200" required><button class="button" type="submit">${icon("plus")}New folder</button></form>${atHome ? '<p class="field-hint spaced-top">Choose or create a project folder inside your home folder. Your whole home folder cannot be used as the results destination.</p>' : ""}${r.writable === false ? `<p class="field-error spaced-top">This folder is not writable by the worker. Choose another folder or ask the owner to correct access.</p>` : '<p class="folder-note">A new folder is created only when you choose New folder. Your destination is selected only when you choose Use this folder.</p>'}`;
      $("#folder-footer").innerHTML =
        `${button("Cancel", "folder-close")}<button class="button button-primary" id="folder-select"${r.writable === false || atHome ? " disabled" : ""}>${icon("check")}Use this folder</button>`;
      $("#folder-up").addEventListener(
        "click",
        () => r.parent && loadFolders(r.parent),
      );
      $$("[data-folder-index]", $("#folder-body")).forEach((el) =>
        el.addEventListener("click", () =>
          loadFolders(r.items[Number(el.dataset.folderIndex)].path),
        ),
      );
      $("#folder-select").addEventListener("click", () => {
        folder.callback(r.path);
        folderDialog.close();
        A.folder = null;
      });
      $("#folder-create-form").addEventListener("submit", (e) => {
        e.preventDefault();
        const name = $("#folder-new-name").value.trim();
        busy($("button", e.currentTarget), async () => {
          try {
            const result = await api("/api/folders", {
              account: folder.account,
              parent: r.path,
              name,
            });
            if (A.folder === folder && folderDialog.open) {
              await loadFolders(result.path);
              toast("Folder created. Choose Use this folder to select it.");
            } else toast("Folder created.");
          } catch (error) {
            if (A.folder === folder && folderDialog.open)
              dialogError(error, $("#folder-body"));
            else reportError(error);
          }
        });
      });
    } catch (error) {
      if (!stillCurrent()) return;
      $("#folder-body").innerHTML = empty(
        "alert",
        "This folder could not be opened",
        error.message,
        button("Try again", "folder-retry", "refresh"),
      );
    }
  }

  async function showJob(job) {
    const notes = job.notes || {};
    const id = job.job_id;
    const output = notes.output_dir || job.output_dir;
    const verification = notes.verification;
    const stringify = (v) =>
      typeof v === "string" ? v : JSON.stringify(v, null, 2);
    const evidence = [
      ["checks", "Verification command output"],
      ["check_results", "Check results"],
      ["failed_checks", "Failed check count"],
      ["trigger_verdict", "GitHub write checks"],
      ["verdict", "GitHub write decision"],
      ["companion_branch", "Companion branch"],
      ["pr_head_refusal", "Pull request branch restriction"],
      ["auto_watch", "Automatic review follow-up"],
    ]
      .filter(([key]) => notes[key] !== undefined && notes[key] !== null)
      .map(
        ([key, label]) =>
          `<details class="source-context spaced-top"><summary>${esc(label)}</summary><div><pre>${esc(stringify(notes[key]))}</pre></div></details>`,
      )
      .join("");
    const createdPR = notes.created_pr?.number
      ? `<div class="detail-section"><h3>Created pull request</h3><a href="${targetURL(job.repo_full_name || job.repo, "fix-pr", notes.created_pr.number)}" target="_blank" rel="noopener noreferrer">Open pull request #${esc(notes.created_pr.number)} ${icon("external")}</a></div>`
      : "";
    openDialog(
      "Task details",
      "YOUR WORK, WITH THE DETAILS",
      `<div class="detail-top"><div><h3>${esc(notes.title || `${kindLabel(job.kind)}${job.number ? ` #${job.number}` : ""}`)}</h3><p><a href="${targetURL(job.repo_full_name || job.repo, job.kind, job.number)}" target="_blank" rel="noopener noreferrer">${esc(job.repo_full_name || job.repo)} #${esc(job.number)} ${icon("external")}</a></p></div>${jobBadge(job)}</div>${job.error ? `<div class="dialog-error" role="status">${esc(job.error)}</div>` : ""}${notes.result_export_error ? `<div class="banner banner-warning" role="status">${icon("alert")}<div><strong>Local results need attention</strong>${esc(stringify(notes.result_export_error))}${notes.worktree_path ? "<p>The temporary checkout is retained for recovery. Check the recorded worktree location before retrying an export or starting new work.</p>" : ""}</div></div>` : ""}<dl class="detail-grid"><div><dt>Task ID</dt><dd class="mono">${esc(id)}</dd></div><div><dt>Kind</dt><dd>${esc(kindLabel(job.kind))}</dd></div><div><dt>Created</dt><dd>${esc(date(job.created_at))}</dd></div><div><dt>Updated</dt><dd>${esc(date(job.updated_at))}</dd></div><div><dt>Model calls used</dt><dd>${number(job.model_calls_used)}</dd></div><div><dt>Results folder</dt><dd>${esc(output || "Not recorded for this older task")}</dd></div><div><dt>Member account</dt><dd>${esc(notes.member || notes.account || job.account || "Not recorded")}</dd></div><div><dt>Commits produced</dt><dd>${esc(list(job.produced_commits).join(", ") || "None recorded")}</dd></div><div><dt>Worker task state</dt><dd>${esc(labelState(job.state))}</dd></div><div><dt>Temporary checkout</dt><dd>${esc(notes.worktree_path || "Not recorded")}</dd></div></dl>${notes.worktree_cleanup ? `<div class="detail-section"><h3>Temporary checkout cleanup</h3><p>${esc(stringify(notes.worktree_cleanup))}</p></div>` : ""}${list(notes.export_skipped_files).length ? `<div class="detail-section"><h3>Files skipped while saving results</h3><pre>${esc(list(notes.export_skipped_files).map(stringify).join("\n"))}</pre></div>` : ""}${notes.agent_summary ? `<div class="detail-section"><h3>Result summary</h3><p>${esc(stringify(notes.agent_summary))}</p></div>` : ""}${notes.changed_files ? `<div class="detail-section"><h3>Changed files</h3><pre>${esc(list(notes.changed_files).map(stringify).join("\n"))}</pre></div>` : ""}${verification ? `<div class="detail-section"><h3>Verification</h3><pre>${esc(stringify(verification))}</pre>${notes.tested_commit ? `<p>Tested commit: <code>${esc(notes.tested_commit)}</code></p>` : ""}</div>` : ""}${createdPR}${evidence}<div class="detail-section"><h3>Task files</h3><p>Inspect the files recorded for this task. Large file previews may be shortened.</p><div class="spaced-top">${button("Browse task files", "job-files", "folder", "button-small", `data-id="${esc(id)}"`)}</div><div id="job-file-browser" class="spaced-top"></div></div>`,
      `${button("Close", "dialog-close")}<div class="footer-right">${button("Refresh task", "job-refresh", "refresh", "", `data-id="${esc(id)}"`)}<a class="button" href="/api/jobs/${encodeURIComponent(id)}/report" download>${icon("download")}Download report</a>${isActive(job) ? button("Stop task", "job-stop", "stop", "button-danger", `data-id="${esc(id)}"`) : canRetry(job) ? button("Retry task", "job-retry", "refresh", "button-primary", `data-id="${esc(id)}"`) : ""}</div>`,
      true,
    );
  }
  async function browseJobFiles(id, path = "", isFile = false) {
    const root = $("#job-file-browser");
    if (!root) return;
    root.innerHTML = loading("Loading task files");
    const fileDownload = (filePath, label = "Download file") =>
      `<a class="button button-small" href="/api/jobs/${encodeURIComponent(id)}/download?${query({ path: filePath })}" download>${icon("download")}${esc(label)}</a>`;
    try {
      const r = await api(`/api/jobs/files?${query({ job_id: id, path })}`);
      if (!root.isConnected) return;
      root.innerHTML = `<div class="folder-location">${path ? button("Root", "job-files", "back", "button-small", `data-id="${esc(id)}"`) : ""}<code>${esc(r.path || path || "Task root")}</code></div>${
        r.content !== undefined
          ? `<div class="file-content"><div class="heading-actions">${fileDownload(path)}</div><pre>${esc(r.content)}</pre>${r.truncated ? '<p class="field-hint">This preview is shortened. Download the file to read the complete content.</p>' : ""}</div>`
          : `<div class="file-list spaced-top">${
              list(r.items).length
                ? list(r.items)
                    .map(
                      (f, i) =>
                        `<div class="file-browser-row"><button class="folder-row" data-job-file="${i}">${icon(f.is_dir ? "folder" : "file")}<span>${esc(f.name)}</span>${f.is_dir ? icon("chevron") : `<small class="muted">${number(f.size)} bytes</small>`}</button>${f.is_dir ? "" : fileDownload(f.path, "Download")}</div>`,
                    )
                    .join("")
                : empty(
                    "folder",
                    "No files available",
                    "This task has no browsable files yet.",
                  )
            }</div>`
      }`;
      $$("[data-job-file]", root).forEach((el) =>
        el.addEventListener("click", () =>
          browseJobFiles(
            id,
            r.items[Number(el.dataset.jobFile)].path,
            !r.items[Number(el.dataset.jobFile)].is_dir,
          ),
        ),
      );
    } catch (error) {
      root.innerHTML =
        info(esc(error.message), "error") +
        (isFile
          ? `<div class="spaced-top">${fileDownload(path)}<p class="field-hint spaced-top">If a preview is unavailable, download the file to inspect it locally. Individual downloads are limited to 50 MB.</p></div>`
          : "");
    }
  }

  async function showRepo(existingName = "") {
    const revision = A.state.config_revision;
    const original = repos().find((r) => repoName(r) === existingName);
    const r = original || {
      owner: "",
      name: "",
      base_branch: "",
      checks: [],
      allow_unknown_rulesets: false,
    };
    openDialog(
      original ? "Edit repository" : "Add a repository",
      "MORE PROJECTS, ONE WORKSPACE",
      `<form id="repo-form"><div class="wizard-stack">${original ? "" : `<div class="field"><label for="repo-search">Find a repository on GitHub</label><div class="input-action"><input id="repo-search" placeholder="Search by owner or repository name" autocomplete="off"><button class="button" id="repo-search-button" type="button">${icon("search")}Search</button></div><span class="field-hint">Search uses the connected GitHub account. You can also enter the owner and name below.</span><div id="repo-search-results"></div></div>`}<div class="field-grid">${field("repo-owner", "Repository owner", r.owner, "GitHub user or organization.", "text", "required")}${field("repo-name", "Repository name", r.name, "The repository’s name.", "text", "required")}</div><div class="field"><label for="repo-branch">Base branch for issue implementations</label><div class="input-action"><select id="repo-branch" required>${r.base_branch ? `<option>${esc(r.base_branch)}</option>` : '<option value="">Load branches, then choose one…</option>'}</select><button id="load-branches" class="button" type="button">${icon("refresh")}Load branches</button></div><span class="field-hint">Issue implementations start from this branch. Pull request fixes use the pull request’s branch.</span></div><div class="field"><label for="check-preset">Add verification commands</label><div class="input-action"><select id="check-preset"><option value="">Choose a common check…</option><option value="npm test">Node.js · npm test</option><option value="npm run build">Node.js · npm run build</option><option value="npm run lint">Node.js · npm run lint</option><option value="python3 -m unittest discover -s tests">Python · unittest</option><option value="python3 -m pytest">Python · pytest</option><option value="cargo test">Rust · cargo test</option><option value="go test ./...">Go · go test ./...</option></select><button class="button" id="add-check" type="button">${icon("plus")}Add</button></div></div><div class="field"><label for="repo-checks">Verification commands (one per line)</label><textarea id="repo-checks" rows="4" spellcheck="false" placeholder="Enter commands that run in this repository">${esc(list(r.checks).join("\n"))}</textarea><span class="field-hint">Use the project’s real check commands. They run as shell commands in the task’s isolated checkout.</span></div><details><summary>Advanced repository option</summary><div><label class="checkbox-label"><input type="checkbox" id="repo-rulesets"${r.allow_unknown_rulesets ? " checked" : ""}><span>Allow publishing when GitHub cannot show its branch rules<small>Advanced exception: use only when a private repository’s plan prevents GitHub from returning its rulesets (branch restrictions). FatChin records the rules as unverified; it cannot prove those rules are safe.</small></span></label></div></details>${info("Repository changes are saved immediately and apply after restarting the worker. Adding a repository does not start a coding task.")}</div></form>`,
      `${button("Cancel", "dialog-close")}<button class="button button-primary" type="submit" form="repo-form">${icon("check")}${original ? "Save repository" : "Add repository"}</button>`,
    );
    const version = A.dialogVersion;
    async function branches() {
      const full = `${$("#repo-owner").value.trim()}/${$("#repo-name").value.trim()}`;
      const result = await api(`/api/github/branches?${query({ repo: full })}`);
      if (
        !currentDialog(version) ||
        `${$("#repo-owner").value.trim()}/${$("#repo-name").value.trim()}` !==
          full
      )
        return;
      $("#repo-branch").innerHTML =
        '<option value="">Choose a branch…</option>' +
        list(result.items)
          .map(
            (b) =>
              `<option value="${esc(b.name)}"${b.name === r.base_branch ? " selected" : ""}>${esc(b.name)}</option>`,
          )
          .join("");
    }
    async function searchRepos() {
      try {
        const q = $("#repo-search").value.trim();
        if (!q) throw new Error("Enter a repository or owner to search.");
        const target = $("#repo-search-results");
        target.innerHTML = loading("Searching GitHub");
        const result = await api(`/api/github/repositories?${query({ q })}`);
        if (!currentDialog(version)) return;
        if ($("#repo-search").value.trim() !== q) {
          target.innerHTML = info(
            "The search changed. Choose Search to load the matching repositories.",
          );
          return;
        }
        target.innerHTML = list(result.items).length
          ? `<div class="result-list">${result.items.map((item, i) => `<button class="result-item" type="button" data-repo-result="${i}">${icon("repo")}<span><strong>${esc(item.full_name)}</strong><small>${esc(item.description || (item.private ? "Private repository" : "Public repository"))}</small></span></button>`).join("")}</div>`
          : info("No accessible repositories match this search.");
        $$("[data-repo-result]", target).forEach((el) =>
          el.addEventListener("click", () => {
            const item = result.items[Number(el.dataset.repoResult)];
            const parts = item.full_name.split("/");
            $("#repo-owner").value = parts[0];
            $("#repo-name").value = parts[1];
            r.base_branch = item.default_branch;
            target.innerHTML = "";
            busy($("#load-branches"), async () => {
              try {
                await branches();
              } catch (e) {
                if (currentDialog(version)) dialogError(e);
              }
            });
          }),
        );
      } catch (error) {
        if (currentDialog(version)) dialogError(error);
      }
    }
    $("#repo-search-button")?.addEventListener("click", (e) =>
      busy(e.currentTarget, searchRepos),
    );
    $("#repo-search")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        busy($("#repo-search-button"), searchRepos);
      }
    });
    $("#load-branches").addEventListener("click", (e) =>
      busy(e.currentTarget, async () => {
        try {
          await branches();
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
        }
      }),
    );
    for (const input of [$("#repo-owner"), $("#repo-name")])
      input.addEventListener("input", () => {
        $("#repo-branch").innerHTML =
          '<option value="">Load branches for this repository…</option>';
      });
    $("#add-check").addEventListener("click", () => {
      const c = $("#check-preset").value;
      if (!c) return;
      const el = $("#repo-checks");
      const values = el.value
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean);
      if (!values.includes(c)) values.push(c);
      el.value = values.join("\n");
    });
    $("#repo-form").addEventListener("submit", (e) => {
      e.preventDefault();
      busy($('button[form="repo-form"]'), async () => {
        try {
          await api("/api/repos", {
            owner: $("#repo-owner").value.trim(),
            name: $("#repo-name").value.trim(),
            base_branch: $("#repo-branch").value,
            checks: $("#repo-checks")
              .value.split("\n")
              .map((c) => c.trim())
              .filter(Boolean),
            allow_unknown_rulesets: $("#repo-rulesets").checked,
            original_full_name: original ? repoName(original) : undefined,
            revision,
          });
          if (currentDialog(version)) closeDialog();
          await refresh();
          toast("Repository saved. Restart the worker to apply this change.");
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
          else reportError(error);
        }
      });
    });
  }

  function showWatch() {
    if (!repos().length) {
      showRepo();
      return;
    }
    const w = {
      repo: "",
      mode: "selected",
      number: "",
      target: null,
      account: "",
      output_dir: "",
    };
    openDialog(
      "Add a review watch",
      "KEEP THE CONVERSATION MOVING",
      `<form id="watch-form"><div class="wizard-stack"><div class="field"><label for="watch-repo">Repository</label><select id="watch-repo" required><option value="">Choose a repository…</option>${repos()
        .map((r) => `<option>${esc(repoName(r))}</option>`)
        .join(
          "",
        )}</select></div><div class="field"><label for="watch-mode">What to watch</label><select id="watch-mode"><option value="selected">One selected pull request</option><option value="all">All current and future open pull requests</option></select></div><div class="field" id="watch-target-field"><label for="watch-target">Pull request</label><select id="watch-target" required><option value="">Choose a repository first…</option></select><span class="field-hint">Open pull requests accessible to your GitHub account.</span></div>${destinationFields(w, "watch")}${info("The first check records existing feedback as its starting point. New feedback after that can queue work automatically. Use New task to address comments that already exist.")}<div class="effects"><strong>Automatic follow-up uses your model</strong><ul><li>Future review comments may start coding tasks without another confirmation.</li><li>Tasks can push commits, post comments, and resolve supported review threads.</li><li>The same local worker call controls apply, separately from your provider’s subscription allowance.</li><li>Results are saved to the folder you choose above.</li></ul></div><label class="checkbox-label"><input id="watch-confirm" type="checkbox" required><span>I understand that this watch may start model requests and GitHub updates automatically.</span></label></div></form>`,
      `${button("Cancel", "dialog-close")}<button class="button button-primary" type="submit" form="watch-form">${icon("watch")}Create watch</button>`,
    );
    const version = A.dialogVersion;
    let targets = [];
    let requestId = 0;
    $("#watch-repo").addEventListener("change", async (e) => {
      w.repo = e.target.value;
      w.accept_no_checks=false;
      w.number = "";
      w.target = null;
      targets = [];
      const requestedRepo = w.repo,
        request = ++requestId;
      const stillCurrent = () =>
        currentDialog(version) &&
        w.repo === requestedRepo &&
        requestId === request;
      const el = $("#watch-target");
      el.innerHTML = '<option value="">Loading open pull requests…</option>';
      try {
        if (!w.repo) {
          el.innerHTML = '<option value="">Choose a repository first…</option>';
          return;
        }
        const result = await api(
          `/api/github/targets?${query({ repo: requestedRepo, kind: "fix-pr", state: "open" })}`,
        );
        if (!el.isConnected || !stillCurrent()) return;
        targets = result.items || [];
        el.innerHTML =
          '<option value="">Choose a pull request…</option>' +
          targets
            .map(
              (t) =>
                `<option value="${t.number}">#${t.number} — ${esc(t.title)}</option>`,
            )
            .join("");
      } catch (error) {
        if (!stillCurrent()) return;
        el.innerHTML = '<option value="">Unable to load pull requests</option>';
        dialogError(error);
      }
    });
    $("#watch-mode").addEventListener("change", (e) => {
      w.mode = e.target.value;
      $("#watch-target-field").hidden = w.mode === "all";
      $("#watch-target").required = w.mode === "selected";
    });
    $("#watch-target").addEventListener("change", (e) => {
      w.number = e.target.value;
      w.target = targets.find((t) => String(t.number) === w.number);
    });
    bindDestination(w, "watch");
    $("#watch-form").addEventListener("submit", (e) => {
      e.preventDefault();
      busy($('button[form="watch-form"]'), async () => {
        try {
          validateDestination(w);
          await api("/api/watches", {
            repo: w.repo,
            mode: w.mode,
            number: w.mode === "selected" ? Number(w.number) : undefined,
            account: w.account,
            output_dir: w.output_dir,
          });
          const relevant = currentDialog(version);
          if (relevant) {
            closeDialog();
            location.hash = "watches";
          }
          await refresh(relevant);
          toast(
            "Review watch saved. New feedback will be checked while polling is enabled.",
          );
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
          else reportError(error);
        }
      });
    });
  }

  async function saveSettings(e) {
    e.preventDefault();
    const form = e.currentTarget;
    busy($('button[type="submit"]', form), async () => {
      let locked = [];
      try {
        const data = new FormData(form);
        const value = (name) => String(data.get(name) || "").trim();
        const n = (name) => {
          const v = Number(value(name));
          if (!Number.isInteger(v) || v < 1)
            throw new Error("Numeric limits must be positive whole numbers.");
          return v;
        };
        const users = value("allowed_command_users")
          .split(/[\s,]+/)
          .filter(Boolean);
        if (
          users.some(
            (u) => !/^\d+$/.test(u) || !Number.isSafeInteger(Number(u)),
          )
        )
          throw new Error(
            "Allowed GitHub user IDs must be whole numeric IDs, separated by lines or commas.",
          );
        const updated = {
          ...settings(),
          github_host: value("github_host"),
          rest_api_version: value("rest_api_version"),
          poll_interval_seconds: n("poll_interval_seconds"),
          command_prefix: value("command_prefix"),
          allowed_command_users: users.map(Number),
          max_tool_iterations: n("max_tool_iterations"),
          run_command_timeout_seconds: n("run_command_timeout_seconds"),
          run_command_output_cap: n("run_command_output_cap"),
          poll_repos: value("poll_repos") === "true",
          state_dir: value("state_dir"),
          worktree_root: value("worktree_root"),
          model: {
            ...(settings().model || {}),
            model_id: value("model_id"),
            base_url: value("base_url"),
            auth_mode: value("auth_mode"),
            broker_base_url: value("broker_base_url"),
            broker_consumer_id: value("broker_consumer_id"),
            broker_server_id: value("broker_server_id"),
            lease_ttl_seconds: n("lease_ttl_seconds"),
            disable_reasoning: value("disable_reasoning") === "true",
            request_timeout_seconds: n("request_timeout_seconds"),
            max_tokens: n("max_tokens"),
            max_calls_per_job: n("max_calls_per_job"),
            max_calls_per_day: n("max_calls_per_day"),
          },
        };
        locked = $$("input,select,textarea,button", form).map((element) => ({
          element,
          disabled: element.disabled,
        }));
        for (const { element } of locked) element.disabled = true;
        form.setAttribute("aria-busy", "true");
        await api("/api/settings", {
          settings: updated,
          revision: form.dataset.revision,
        });
        A.settingsDirty = false;
        await refresh();
        toast("Settings saved. Restart the worker to apply these changes.");
      } catch (error) {
        reportError(error);
      } finally {
        if (form.isConnected) {
          for (const { element, disabled } of locked)
            element.disabled = disabled;
          form.removeAttribute("aria-busy");
        }
      }
    });
  }
  async function diagnostics() {
    openDialog(
      "Setup checks",
      "CONNECTIONS & READINESS",
      loading("Checking your setup"),
      button("Close", "dialog-close"),
    );
    const version = A.dialogVersion;
    try {
      const result = await api("/api/diagnostics");
      if (!currentDialog(version)) return;
      A.diagnostics = result;
      $("#dialog-body").innerHTML =
        `<p class="dialog-copy">These checks inspect the worker’s setup without starting a coding task or sending a model request.</p>${
          list(result.checks)
            .map(
              (c) =>
                `<div class="diagnostic-row ${esc(c.status)}">${icon(c.status === "pass" ? "check-circle" : c.status === "warn" ? "alert" : "x")}<div><strong>${esc(c.name)}</strong><p>${esc(c.message)}</p>${c.action ? `<p>${esc(c.action)}</p>` : ""}</div></div>`,
            )
            .join("") || info("No setup checks were returned by the worker.")
        }`;
      $("#dialog-footer").innerHTML =
        `${button("Close", "dialog-close")}${button("Run again", "diagnostics", "refresh", "button-primary")}`;
    } catch (error) {
      if (currentDialog(version)) dialogError(error);
    }
  }
  function providerCheck() {
    confirmDialog(
      "Test the model connection?",
      "This sends exactly one small model request using the currently saved connection. Billing follows the selected provider or subscription. Save settings and restart the worker first if you changed the connection.",
      "Send one test request",
      async () => {
        const result = await api(
          "/api/provider-check",
          { confirmed: true },
          { timeout: 240000 },
        );
        if (!result.ok)
          throw new Error(result.output || "The model connection test failed.");
        toast(`Model connection succeeded. ${result.output || ""}`.trim());
      },
      {
        details: info(
          "No coding task is created. This connection test does not repeat automatically.",
        ),
      },
    );
  }
  function storeCredential() {
    openDialog(
      "Store a credential",
      "WRITE-ONLY SECRET STORAGE",
      `<form id="credential-form"><div class="wizard-stack"><p class="dialog-copy" style="margin:0">Credentials are saved in the worker’s private environment file with a descriptive record. Saved secrets are never returned to this browser.</p><div class="field"><label for="credential-kind">Credential type</label><select id="credential-kind"><option value="model">Dedicated provider API key</option><option value="broker">Company secret broker credential</option></select></div><div class="field"><label for="credential-value">New credential</label><input id="credential-value" type="password" autocomplete="new-password" required spellcheck="false"><span class="field-hint">Paste the application’s dedicated key. The saved value is never displayed.</span></div>${field("credential-supplier", "Supplied by", "", "The name recorded with this credential.", "text", "required")}${info("Storing a credential replaces that credential in the worker’s environment. Restart the worker after saving to use the new value. Keep an existing valid credential available until the new connection is verified.", "warning")}</div></form>`,
      `${button("Cancel", "dialog-close")}<button class="button button-primary" type="submit" form="credential-form">${icon("shield")}Store credential</button>`,
    );
    const version = A.dialogVersion;
    $("#credential-form").addEventListener("submit", (e) => {
      e.preventDefault();
      busy($('button[form="credential-form"]'), async () => {
        try {
          const secret = $("#credential-value");
          await api("/api/credentials", {
            kind: $("#credential-kind").value,
            value: secret.value,
            supplied_by: $("#credential-supplier").value.trim(),
          });
          secret.value = "";
          const relevant = currentDialog(version);
          if (relevant) closeDialog();
          await refresh(relevant);
          toast(
            "Credential saved. Restart the worker before testing the connection.",
          );
        } catch (error) {
          if (currentDialog(version)) dialogError(error);
          else reportError(error);
        }
      });
    });
  }
  async function githubConnect() {
    openDialog(
      "Connect GitHub",
      "AUTHORIZE THE WORKER",
      loading("Starting GitHub device connection"),
      button("Close", "dialog-close"),
    );
    const version = A.dialogVersion;
    try {
      const connection = await api("/api/github/connect", {});
      if (!currentDialog(version)) return;
      renderGitHubConnection(connection, version);
    } catch (error) {
      if (currentDialog(version)) dialogError(error);
    }
  }
  function renderGitHubConnection(connection, version) {
    if (!currentDialog(version)) return;
    const status = String(connection.status || "").toLowerCase();
    if (
      ["finished", "complete", "completed", "connected", "success"].includes(
        status,
      )
    ) {
      closeDialog();
      refresh();
      toast("GitHub connected. Your accessible repositories are ready.");
      return;
    }
    if (status === "error" || status === "failed") {
      dialogError(
        new Error(
          connection.error ||
            connection.message ||
            "GitHub connection failed. Try connecting again.",
        ),
      );
      return;
    }
    const uri =
      connection.verification_uri === "https://github.com/login/device"
        ? connection.verification_uri
        : "https://github.com/login/device";
    $("#dialog-body").innerHTML =
      `<p class="dialog-copy">Authorize the shared worker using GitHub’s device connection. Open GitHub, sign in to the account with repository access, and enter this one-time code.</p>${connection.code ? `<div class="selection-summary" style="justify-content:center"><strong class="mono" style="font-size:27px;letter-spacing:4px">${esc(connection.code)}</strong><button class="icon-button" id="copy-device-code" aria-label="Copy GitHub code">${icon("copy")}</button></div>` : loading("Waiting for a device code")}<div class="spaced-top" style="text-align:center"><a class="button button-primary" href="${uri}" target="_blank" rel="noopener noreferrer">Open GitHub ${icon("external")}</a></div><p class="field-hint spaced-top" style="text-align:center">This window checks for completion automatically. Closing it does not cancel GitHub’s code or revoke an authorization. Complete the connection on GitHub or let the code expire. Your GitHub password never enters this app.</p>`;
    $("#copy-device-code")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(connection.code);
        toast("GitHub code copied.");
      } catch {
        toast("Select the code and copy it manually.", true);
      }
    });
    A.githubTimer = setTimeout(async () => {
      try {
        const result = await api("/api/github/connect");
        if (currentDialog(version)) renderGitHubConnection(result, version);
      } catch (error) {
        if (currentDialog(version)) dialogError(error);
      }
    }, 4000);
  }

  async function handleAction(element) {
    const action = element.dataset.action;
    if (action.startsWith("fleet-")) return fleetAction(element);
    if (action.startsWith("learn-") || action.startsWith("explore-")) return learnAction(element);
    if (action === "refresh") return refresh();
    if (action === "dialog-close") return closeDialog();
    if (action === "folder-close") {
      folderDialog.close();
      A.folder = null;
      return;
    }
    if (action === "folder-retry") return loadFolders(A.folder?.path);
    if (action === "new-task") return newTask(element.dataset.repo);
    if (action === "wizard-next") return wizardNext();
    if (action === "wizard-back") {
      A.wizard.requestId++;
      A.wizard.loading = false;
      const flow = wizardFlow(A.wizard);
      const index = flow.indexOf(A.wizard.step);
      A.wizard.step = index > 0 ? flow[index - 1] : 0;
      return renderWizard();
    }
    if (action === "new-repo") return showRepo();
    if (action === "edit-repo") return showRepo(element.dataset.repo);
    if (action === "new-watch") return showWatch();
    if (action === "go-settings") {
      location.hash = "settings";
      return;
    }
    if (action === "authority-toggle") {
      const repo = element.dataset.repo;
      const direct = element.dataset.mode === "auto";
      return confirmDialog(
        direct ? "Post drafts without asking?" : "Ask before posting again?",
        direct
          ? `${repo}: drafts this worker prepares may be posted to GitHub without a second confirmation. Merging, deleting, closing, and releasing always ask.`
          : `${repo}: drafts wait for you, and nothing is posted until you choose Post.`,
        direct ? "Post directly" : "Ask me every time",
        async () => {
          await api("/api/authority", {
            repo,
            mode: direct ? "auto" : "ask",
          });
          toast(
            direct
              ? "Saved: this repository posts directly without a second confirmation."
              : "Saved: this repository asks you before posting again.",
          );
          await loadFence(true);
        },
      );
    }
    if (["draft-post", "draft-edit", "draft-discard"].includes(action)) {
      if (element.disabled) return;
      return draftAction(element.dataset.id, action.slice("draft-".length));
    }
    if (action === "add-connected-user")
      return busy(element, () => addCommandUser("", true));
    if (action === "remove-command-user") {
      const field = $("#allowed_command_users");
      if (!field) return;
      field.value = field.value
        .split(/[\s,]+/)
        .filter((id) => id && id !== element.dataset.id)
        .join("\n");
      field.dispatchEvent(new Event("input", { bubbles: true }));
      return loadCommandUsers();
    }
    if (action === "settings-reload") {
      if (!A.settingsDirty) return refresh();
      return confirmDialog(
        "Reload saved settings?",
        "Your unsaved edits in this browser will be discarded. The saved worker configuration will be loaded again.",
        "Reload saved settings",
        async () => {
          A.settingsDirty = false;
          await refresh();
        },
      );
    }
    if (action === "diagnostics") return diagnostics();
    if (action === "provider-check") return providerCheck();
    if (action === "store-credential") return storeCredential();
    if (action === "github-connect") return githubConnect();
    if (action === "job-detail") {
      const j = jobs().find((j) => j.job_id === element.dataset.id);
      if (j) return showJob(j);
      toast(
        "This task is no longer in the current snapshot. Refresh to reload it.",
        true,
      );
      return;
    }
    if (action === "job-files") return browseJobFiles(element.dataset.id);
    if (action === "job-refresh")
      return busy(element, async () => {
        const version = A.dialogVersion;
        const refreshed = await refresh(false);
        if (!currentDialog(version)) return;
        if (!refreshed) {
          dialogError(
            new Error(
              "Could not refresh this task. Previous details remain visible; check your connection and choose Refresh task to try again.",
            ),
          );
          return;
        }
        const current = jobs().find((j) => j.job_id === element.dataset.id);
        if (current) showJob(current);
        else
          toast(
            "This task is no longer available in the worker snapshot.",
            true,
          );
      });
    if (action === "job-stop" || action === "job-retry") {
      const id = element.dataset.id;
      const stop = action === "job-stop";
      return confirmDialog(
        stop ? "Stop this task?" : "Retry this task?",
        stop
          ? "The worker will request a graceful stop. A command or model request already in progress may finish first. Existing GitHub updates and task files are kept. Review watches remain enabled; remove the watch separately to prevent future automatic tasks."
          : "This queues another attempt with the same repository and destination. It can make additional model requests and push additional changes to GitHub.",
        stop ? "Request stop" : "Queue retry",
        async () => {
          await api(
            `/api/jobs/${encodeURIComponent(id)}/${stop ? "stop" : "retry"}`,
            {},
          );
          toast(
            stop
              ? "Stop requested. Check the task status for completion."
              : "Task retry queued.",
          );
        },
        { danger: stop },
      );
    }
    if (action === "remove-repo") {
      const repo = element.dataset.repo;
      return confirmDialog(
        "Remove this repository?",
        `${repo} will be removed from the saved configuration. History and files are kept. Restart the worker to apply the change; existing queued work may still run.`,
        "Remove repository",
        async () => {
          await api("/api/repos/remove", {
            repo,
            revision: A.state.config_revision,
          });
          toast(
            "Repository removed from saved configuration. Restart to apply.",
          );
        },
        { danger: true },
      );
    }
    if (action === "remove-watch") {
      const w = watches()[Number(element.dataset.index)];
      if (!w) return;
      return confirmDialog(
        "Remove this review watch?",
        `Future automatic follow-up will stop for ${w.repo || w.repo_full_name}${w.mode === "all" ? "" : ` #${w.number}`}. Already queued tasks and their results remain.`,
        "Remove watch",
        async () => {
          await api("/api/watches/remove", {
            repo: w.repo || w.repo_full_name,
            mode: w.mode || "selected",
            number: w.number,
          });
          toast("Review watch removed.");
        },
        { danger: true },
      );
    }
    if (action?.startsWith("service-")) {
      const command = action.slice(8);
      if (["pause", "resume", "start"].includes(command)) {
        return busy(element, async () => {
          await api("/api/service", { action: command });
          await refresh();
          toast(
            command === "pause"
              ? "Polling pause saved. Queued tasks continue."
              : command === "resume"
                ? "Polling resumed."
                : "Worker start requested. Status will update shortly.",
          );
        });
      }
      return confirmDialog(
        command === "stop" ? "Stop the worker?" : "Restart the worker?",
        command === "stop"
          ? "This stops the background service only when it is idle. Stop queued or running tasks first, then wait for the current step to finish. Saved records are kept. Start it again when ready."
          : "This restarts the background service to load saved settings only when it is idle. Stop queued or running tasks first, then wait for the current step to finish. Saved records are kept.",
        command === "stop" ? "Stop worker" : "Restart worker",
        async () => {
          await api("/api/service", { action: command });
          toast(
            command === "stop"
              ? "Worker stop requested."
              : "Worker restart requested. Status will update shortly.",
          );
        },
        { danger: command === "stop" },
      );
    }
  }

  document.addEventListener("click", (e) => {
    const nav = e.target.closest('a[href^="#"]');
    if (
      nav &&
      A.settingsDirty &&
      nav.hash &&
      nav.hash !== "#settings" &&
      nav.hash !== "#main"
    ) {
      e.preventDefault();
      const destination = nav.hash;
      confirmDialog(
        "Leave unsaved settings?",
        "Your edits in this browser have not been saved. Leaving this page discards those edits.",
        "Leave settings",
        async () => {
          A.settingsDirty = false;
          location.hash = destination;
        },
      );
      return;
    }
    const scroll = e.target.closest("[data-scroll]");
    if (scroll) {
      e.preventDefault();
      document
        .getElementById(scroll.dataset.scroll)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const element = e.target.closest("[data-action]");
    if (!element || element.disabled) return;
    e.preventDefault();
    Promise.resolve(handleAction(element)).catch(reportError);
  });
  $("#dialog-close").addEventListener("click", closeDialog);
  $("#folder-close").addEventListener("click", () => {
    folderDialog.close();
    A.folder = null;
  });
  dialog.addEventListener("cancel", () => {
    A.dialogVersion++;
    A.wizard = null;
    A.dialog = null;
    if (A.githubTimer) clearTimeout(A.githubTimer);
  });
  folderDialog.addEventListener("cancel", () => {
    A.folder = null;
  });
  $("#refresh-button").addEventListener("click", () =>
    A.settingsDirty
      ? handleAction({ dataset: { action: "settings-reload" } })
      : refresh(),
  );
  const phoneLayout = matchMedia("(max-width:720px)");
  const closeMobileNav = () => {
    const wasOpen = $("#sidebar").classList.contains("open");
    $("#sidebar").classList.remove("open");
    $("#sidebar").inert = phoneLayout.matches;
    $("#mobile-menu").setAttribute("aria-expanded", "false");
    $("#mobile-menu").setAttribute("aria-label", "Open navigation");
    $("#nav-scrim").hidden = true;
    main.inert = false;
    $(".topbar-actions").inert = false;
    $(".app-footer").inert = false;
    if (wasOpen && phoneLayout.matches) $("#mobile-menu").focus();
  };
  $("#sidebar").inert = phoneLayout.matches;
  phoneLayout.addEventListener("change", closeMobileNav);
  $("#mobile-menu").addEventListener("click", () => {
    if ($("#sidebar").classList.contains("open")) return closeMobileNav();
    $("#sidebar").inert = false;
    $("#sidebar").classList.add("open");
    $("#mobile-menu").setAttribute("aria-expanded", "true");
    $("#mobile-menu").setAttribute("aria-label", "Close navigation");
    $("#nav-scrim").hidden = false;
    main.inert = true;
    $(".topbar-actions").inert = true;
    $(".app-footer").inert = true;
    $('#sidebar a[aria-current="page"]')?.focus();
  });
  $("#nav-scrim").addEventListener("click", closeMobileNav);
  document.addEventListener("keydown", (e) => {
    const modal = folderDialog.open
      ? folderDialog
      : dialog.open
        ? dialog
        : null;
    if (e.key === "Tab" && modal) {
      const focusable = $$(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex="0"]',
        modal,
      ).filter((el) => el.getClientRects().length && !el.inert);
      const first = focusable[0],
        last = focusable.at(-1);
      if (!first) {
        e.preventDefault();
        return;
      }
      if (!modal.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
        return;
      }
      if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
        return;
      }
    }
    if (e.key === "Escape" && !modal) closeMobileNav();
    if (
      e.key === "Tab" &&
      phoneLayout.matches &&
      $("#sidebar").classList.contains("open")
    ) {
      const items = [$("#mobile-menu"), ...$$("#sidebar a[href]")];
      const current = items.indexOf(document.activeElement);
      e.preventDefault();
      items[
        (current + (e.shiftKey ? -1 : 1) + items.length) % items.length
      ].focus();
    }
    if (
      e.altKey &&
      !e.ctrlKey &&
      !e.metaKey &&
      !dialog.open &&
      !folderDialog.open &&
      !$("#sidebar").classList.contains("open")
    ) {
      if (e.key.toLowerCase() === "n") {
        e.preventDefault();
        newTask();
      }
      if (e.key.toLowerCase() === "r") {
        e.preventDefault();
        refresh();
      }
    }
  });
  window.addEventListener("hashchange", () => {
    const next = location.hash.slice(1);
    if (!views.some((v) => v[0] === next)) return;
    if (next === A.view) return;
    if (A.view === "settings" && A.settingsDirty) {
      history.replaceState(
        history.state,
        "",
        `${location.pathname}${location.search}#settings`,
      );
      confirmDialog(
        "Leave unsaved settings?",
        "Your edits in this browser have not been saved. Leaving this page discards those edits.",
        "Leave settings",
        async () => {
          A.settingsDirty = false;
          location.hash = next;
        },
      );
      return;
    }
    A.view = next;
    A.settingsDirty = false;
    renderNav();
    renderView();
    closeMobileNav();
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  });
  const initial = location.hash.slice(1);
  A.view = views.some((v) => v[0] === initial) ? initial : "overview";
  renderNav();
  loadTaskTypes().catch(() => {});
  refresh();
  setInterval(() => {
    if (
      document.visibilityState === "visible" &&
      !$("#sidebar").classList.contains("open") &&
      !dialog.open &&
      !folderDialog.open &&
      !document.activeElement?.matches("input,select,textarea")
    )
      refresh(A.view !== "settings" && A.view !== "learn" && A.view !== "fleet");
  }, 10000);
  setInterval(() => {
    if (A.view === "fleet" && document.visibilityState === "visible") loadFleet();
  }, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && A.view === "fleet") loadFleet();
    if (document.visibilityState === "visible")
      refresh(!dialog.open && A.view !== "settings" && A.view !== "learn" && A.view !== "fleet");
  });
  window.addEventListener("beforeunload", (event) => {
    if (A.settingsDirty || unsavedPractice()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();
