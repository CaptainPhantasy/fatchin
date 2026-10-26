// Independent browser edge and command tests. All API calls are intercepted. CDP fulfills EVERY /api/* request; no live writes.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-browser-edges.mjs http://worker-address:18465 /home/you/gui-fixtures
import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
const baseURL = process.argv[2];
const outputDir = process.argv[3];
if (!outputDir) {
  console.error(
    "Error: No destination folder specified. Supply an output path within your user space.",
  );
  process.exit(2);
}
if (!baseURL) {
  console.error(
    "Usage: node tests/gui-browser-edges.mjs <workspace-url> <output-folder>",
  );
  process.exit(2);
}
await mkdir(outputDir, { recursive: true });
const profile = await mkdtemp(join(tmpdir(), "gha-ui-chrome-"));
const chrome = spawn(
  "/usr/bin/google-chrome",
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] },
);
let chromeError = "";
chrome.stderr.on("data", (d) => {
  chromeError += d;
});
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
let port;
for (let i = 0; i < 100; i++) {
  try {
    port = (await readFile(join(profile, "DevToolsActivePort"), "utf8")).split(
      "\n",
    )[0];
    break;
  } catch {
    await delay(100);
  }
}
if (!port) throw new Error(chromeError);
const target = await (
  await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {
    method: "PUT",
  })
).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r, j) => {
  ws.onopen = r;
  ws.onerror = j;
});
let id = 0;
const pending = new Map();
const errors = [];
const failed = [];
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
    else p.resolve(msg.result);
  } else if (msg.method === "Runtime.exceptionThrown")
    errors.push(msg.params.exceptionDetails);
  else if (
    msg.method === "Log.entryAdded" &&
    msg.params.entry.level === "error"
  )
    errors.push(msg.params.entry);
  else if (msg.method === "Network.loadingFailed") failed.push(msg.params);
};
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const key = ++id;
    pending.set(key, { resolve, reject });
    ws.send(JSON.stringify({ id: key, method, params }));
  });
const evaluate = async (expression) => {
  const out = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (out.exceptionDetails)
    throw new Error(JSON.stringify(out.exceptionDetails));
  return out.result.value;
};
const click = (selector) =>
  evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
const fill = (selector, value, event = "input") =>
  evaluate(
    `(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));})()`,
  );
const waitFor = async (expression) => {
  for (let i = 0; i < 120; i++) {
    if (await evaluate(expression)) return;
    await delay(250);
  }
  throw new Error("Timed out: " + expression);
};
const screenshot = async (name) => {
  await delay(300);
  const r = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(
    join(outputDir, `${name}.png`),
    Buffer.from(r.data, "base64"),
  );
};
const fixture = {
  version: "fixture",
  config_revision: "revision-1",
  service: {
    active: true,
    state: "active",
    poll_interval_seconds: 120,
    polling_enabled: true,
    restart_required: false,
  },
  github: { configured: true, login: "fixture-connected", id: 196844434 },
  accounts: [{ account: "example", label: "example", path: "/home/example" }],
  repos: [
    {
      owner: "example",
      name: "fixture-project",
      full_name: "example/fixture-project",
      base_branch: "main",
      checks: ["npm test"],
    },
  ],
  jobs: [],
  watches: [],
  summary: { jobs_open: 0, jobs_done_today: 0, model_calls_today: 0 },
  settings: {
    github_host: "github.com",
    rest_api_version: "2022-11-28",
    poll_interval_seconds: 120,
    command_prefix: "/agent",
    allowed_command_users: [196844434],
    state_dir: "/home/example/worker-state",
    worktree_root: "/home/example/worker-work",
    max_tool_iterations: 40,
    run_command_timeout_seconds: 600,
    run_command_output_cap: 100000,
    poll_repos: true,
    model: {
      base_url: "http://127.0.0.1:9999/model",
      model_id: "fixture-model",
      auth_mode: "env",
      broker_base_url: "http://127.0.0.1:9999",
      broker_consumer_id: "fixture",
      broker_server_id: "fixture",
      lease_ttl_seconds: 900,
      request_timeout_seconds: 180,
      max_tokens: 8192,
      max_calls_per_job: 60,
      max_calls_per_day: 600,
      disable_reasoning: false,
    },
  },
};
fixture.model = fixture.settings.model;
const writes = [];
let providerCalls = 0;
let stateFailure = false;
let staleSettings = false;
let settingsDelay = 0;
let branchesDelay = 0;
let lookupDelay = 0;
const targetDelay = new Map();
const requestDelay = new Map();
let providerFailure = false,
  credentialFailure = false,
  diagnosticsFailure = false,
  githubStatus = "waiting";
const networkFailures = new Set();
let badStateJSON = false;
const checks = [];
const check = (condition, name) => {
  checks.push({ name, pass: !!condition });
  console.log(condition ? "PASS" : "FAIL", name);
};
fixture.repos.push({
  owner: "example",
  name: "second-project",
  full_name: "example/second-project",
  base_branch: "release",
  checks: ["npm test"],
});
const targetItem = {
  number: 77,
  title: "Fixture issue with complete source",
  state: "open",
  body: "Required behavior: preserve every input.\n<script>globalThis.badSource=true</script>",
  comments: [
    {
      author: { login: "fixture-reviewer" },
      body: "Cover the empty input edge case.",
    },
  ],
};
async function route(request) {
  const url = new URL(request.url),
    path = url.pathname;
  const method = request.method;
  await delay(requestDelay.get(path) || 0);
  if (path === "/api/github/targets")
    await delay(targetDelay.get(url.searchParams.get("repo")) || 0);
  if (path === "/api/github/branches") await delay(branchesDelay);
  if (path === "/api/github/target") await delay(lookupDelay);
  if (path === "/api/settings" && method === "POST") await delay(settingsDelay);
  let data;
  try {
    data = JSON.parse(request.postData || "{}");
  } catch {
    data = {};
  }
  if (method === "POST") writes.push({ path, data });
  let status = 200,
    result = {};
  if (path === "/api/state") {
    if (badStateJSON) return { status: 200, rawBody: "invalid-json" };
    if (stateFailure) {
      status = 503;
      result = { error: "Fixture connection unavailable" };
    } else result = fixture;
  } else if (path === "/api/github/user")
    result = {
      login: url.searchParams.get("login") || "connected-fixture",
      id: url.searchParams.get("login") ? 123456 : 196844434,
    };
  else if (path === "/api/github/targets")
    result = {
      items: [
        {
          ...targetItem,
          number:
            url.searchParams.get("repo") === "example/second-project" ? 88 : 77,
          title:
            url.searchParams.get("repo") === "example/second-project"
              ? "Second project issue"
              : "First project issue",
        },
      ],
    };
  else if (path === "/api/github/target")
    result = {
      item: {
        ...targetItem,
        number: Number(url.searchParams.get("number") || 77),
        title:
          url.searchParams.get("repo") === "example/second-project"
            ? "Second project issue"
            : "First project issue",
      },
    };
  else if (path === "/api/github/repositories")
    result = {
      items: [
        {
          full_name: "fixture/new-project",
          name: "new-project",
          description: "Fixture repository",
          default_branch: "main",
        },
      ],
    };
  else if (path === "/api/github/branches")
    result = { items: [{ name: "main" }, { name: "release" }] };
  else if (path === "/api/folders" && method === "GET") {
    const path = url.searchParams.get("path") || "/home/example";
    result = {
      path,
      parent: path === "/home/example" ? null : "/home/example",
      items:
        path === "/home/example"
          ? [{ name: "results", path: "/home/example/results", is_dir: true }]
          : [],
      writable: true,
    };
  } else if (path === "/api/folders") {
    if (data.name.includes("/") || data.name === "..") {
      status = 400;
      result = { error: "Folder name must be one directory name." };
    } else result = { path: data.parent + "/" + data.name };
  } else if (path === "/api/jobs") {
    const job = {
      job_id: "fixture-job-1",
      kind: data.kind,
      repo_full_name: data.repo,
      number: data.number,
      state: "received",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      model_calls_used: 0,
      notes: {
        title: targetItem.title,
        output_dir: data.output_dir,
        member: data.account,
      },
    };
    fixture.jobs = [job];
    result = { job };
  } else if (path.endsWith("/stop") && path.startsWith("/api/jobs/")) {
    fixture.jobs[0].state = "stopped";
    fixture.jobs[0].notes.result_export_error = "Fixture output access changed";
    fixture.jobs[0].notes.worktree_path = "/tmp/fixture-checkout";
    fixture.jobs[0].notes.worktree_cleanup = "Retained for recovery";
    fixture.jobs[0].notes.checks = [
      { command: "npm test", exit_code: 0, output: "passed" },
    ];
    fixture.jobs[0].notes.verification = "Checks passed";
    fixture.jobs[0].notes.created_pr = { number: 88 };
    result = { job: fixture.jobs[0] };
  } else if (path.endsWith("/retry") && path.startsWith("/api/jobs/")) {
    fixture.jobs[0].state = "received";
    result = { job: fixture.jobs[0] };
  } else if (path === "/api/watches") {
    fixture.watches.push(data);
    result = { ok: true };
  } else if (path === "/api/watches/remove") {
    fixture.watches = [];
    result = { ok: true };
  } else if (path === "/api/settings") {
    if (staleSettings || data.revision !== fixture.config_revision) {
      status = 409;
      result = {
        error:
          "Configuration changed in another browser. Reload saved settings before trying again.",
      };
    } else {
      fixture.settings = data.settings;
      fixture.model = data.settings.model;
      fixture.config_revision = "revision-3";
      fixture.service.restart_required = true;
      result = { ok: true, restart_required: true };
    }
  } else if (path === "/api/repos") {
    const replacement = { ...data, full_name: data.owner + "/" + data.name };
    const at = fixture.repos.findIndex(
      (r) => r.full_name === data.original_full_name,
    );
    if (at >= 0) fixture.repos[at] = replacement;
    else fixture.repos.push(replacement);
    fixture.service.restart_required = true;
    result = { ok: true, restart_required: true };
  } else if (path === "/api/repos/remove") {
    fixture.repos = fixture.repos.filter((r) => r.full_name !== data.repo);
    result = { ok: true };
  } else if (path === "/api/diagnostics") {
    if (diagnosticsFailure) {
      status = 503;
      result = { error: "Fixture diagnostics unavailable" };
    } else
      result = {
        checks: [
          {
            name: "Fixture setup",
            status: "pass",
            message: "No model request.",
          },
        ],
      };
  } else if (path === "/api/provider-check") {
    providerCalls++;
    result = {
      ok: !providerFailure,
      output: providerFailure
        ? "Fixture model unavailable"
        : "Fixture model response.",
    };
  } else if (path === "/api/credentials") {
    if (credentialFailure) {
      status = 503;
      result = { error: "Fixture credential storage unavailable" };
    } else
      result = {
        ok: true,
        record: { kind: data.kind, supplied_by: data.supplied_by },
      };
  } else if (path === "/api/service") {
    if (data.action === "pause") fixture.service.polling_enabled = false;
    if (data.action === "resume") fixture.service.polling_enabled = true;
    if (data.action === "stop") fixture.service.active = false;
    if (data.action === "start" || data.action === "restart") {
      fixture.service.active = true;
      fixture.service.restart_required = false;
    }
    result = { ok: true };
  } else if (path === "/api/github/connect")
    result = {
      status: githubStatus,
      code: "TEST-CODE",
      verification_uri: "https://github.com/login/device",
    };
  else if (path === "/api/jobs/files") {
    if (url.searchParams.get("path") === "report.json")
      result = { path: "report.json", content: '{"fixture":true}' };
    else if (url.searchParams.get("path") === "image.png") {
      status = 400;
      result = {
        error: "Binary file has no text preview. Download it instead.",
      };
    } else
      result = {
        path: "",
        items: [
          { name: "report.json", path: "report.json", is_dir: false, size: 15 },
          { name: "image.png", path: "image.png", is_dir: false, size: 32 },
        ],
      };
  } else if (path === "/api/task-types")
    result = {
      items: [
        {
          id: "fix-pr",
          label: "Fix a pull request",
          help: "Reads review comments and proves the fixes with the repository's checks.",
          takes: "pr",
          profile: "review-fix",
          fields: [],
        },
        {
          id: "implement-issue",
          label: "Implement an issue",
          help: "Turns an issue into working code and opens a pull request.",
          takes: "issue",
          profile: "implement",
          fields: [],
        },
      ],
    };
  else if (path === "/api/authority" && method === "GET")
    result = {
      modes: { "example/fixture-project": "ask" },
      grants: [],
      explanation:
        "Drafts wait for you unless you choose to post directly for a repository. Merging, deleting, closing and releasing always ask.",
    };
  else if (path === "/api/authority") result = { repo: data.repo, mode: data.mode };
  else if (path === "/api/fence")
    result = {
      repos: [
        {
          repo: "example/fixture-project",
          checks_configured: 1,
          protection: "safe",
          watches_armed: false,
          post_authority: "ask",
          base_branch: "main",
          rulesets_readable: "unknown",
        },
      ],
      copilot_runs_cancelled: 0,
      model_calls_today: 0,
      actions_runs_started_by_worker: 0,
      note: "The worker starts no GitHub Actions runs and cancels Copilot reviews on its own pushes.",
    };
  else if (path === "/api/digest")
    result = {
      done: [],
      waiting_on_you: { events: [], drafts: [] },
      watched: [],
    };
  else if (path.endsWith("/draft-action") && path.startsWith("/api/jobs/")) {
    status = 404;
    result = {
      error: "This operation was not found.",
      hint: "Posted. The words are now on GitHub.",
    };
  } else {
    status = 404;
    result = { error: "Unhandled fixture route " + path };
  }
  return { status, result };
}
const originalHandler = ws.onmessage;
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.method === "Fetch.requestPaused") {
    (async () => {
      const request = msg.params.request;
      const path = new URL(request.url).pathname;
      if (networkFailures.has(path)) {
        networkFailures.delete(path);
        await send("Fetch.failRequest", {
          requestId: msg.params.requestId,
          errorReason: "ConnectionReset",
        });
        return null;
      }
      return route(request);
    })()
      .then((response) => {
        if (!response) return;
        const { status, result, rawBody } = response;
        return send("Fetch.fulfillRequest", {
          requestId: msg.params.requestId,
          responseCode: status,
          responseHeaders: [
            { name: "Content-Type", value: "application/json" },
          ],
          body: Buffer.from(
            rawBody === undefined ? JSON.stringify(result) : rawBody,
          ).toString("base64"),
        });
      })
      .catch((e) => console.error("Fixture route failed", e));
  } else originalHandler(event);
};
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
  console.log("PASS", message);
};
const modal = (title) =>
  waitFor(
    `document.querySelector('#dialog-title').textContent===${JSON.stringify(title)}&&document.querySelector('#app-dialog').open`,
  );
const key = async (keyName, modifiers = 0) => {
  await send("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: keyName,
    code: keyName,
    modifiers,
    windowsVirtualKeyCode:
      keyName === "Tab" ? 9 : keyName === "Escape" ? 27 : 0,
  });
  await send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: keyName,
    code: keyName,
    modifiers,
    windowsVirtualKeyCode:
      keyName === "Tab" ? 9 : keyName === "Escape" ? 27 : 0,
  });
};
const view = async (name) => {
  await evaluate(`location.hash=${JSON.stringify(name)}`);
  await delay(150);
};
const close = async () => {
  if (await evaluate(`document.querySelector('#folder-dialog').open`))
    await click("#folder-close");
  if (await evaluate(`document.querySelector('#app-dialog').open`))
    await click("#dialog-close");
};
const cleanSettings = async () => {
  await close();
  if (await evaluate(`!!document.querySelector('#settings-form')`)) {
    await click('[data-action="settings-reload"]');
    await delay(100);
    if (await evaluate(`document.querySelector('#app-dialog').open`))
      await click("#confirm-action");
    await delay(150);
  }
};
let saveSerial = 10;
const baseRoute = route;
route = async (request) => {
  const result = await baseRoute(request);
  if (
    new URL(request.url).pathname === "/api/settings" &&
    request.method === "POST" &&
    result.status === 200
  )
    fixture.config_revision = `revision-${++saveSerial}`;
  return result;
};
async function secondTab() {
  const target = await (
    await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {
      method: "PUT",
    })
  ).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let nextId = 0;
  const requests = new Map();
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++nextId;
      requests.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const p = requests.get(message.id);
      requests.delete(message.id);
      if (message.error) p.reject(new Error(JSON.stringify(message.error)));
      else p.resolve(message.result);
    } else if (message.method === "Fetch.requestPaused") {
      route(message.params.request).then(({ status, result }) =>
        call("Fetch.fulfillRequest", {
          requestId: message.params.requestId,
          responseCode: status,
          responseHeaders: [
            { name: "Content-Type", value: "application/json" },
          ],
          body: Buffer.from(JSON.stringify(result)).toString("base64"),
        }),
      );
    } else if (message.method === "Runtime.exceptionThrown")
      errors.push({ ...message.params.exceptionDetails, tab: "second" });
  };
  const run = async (expression) => {
    const r = await call("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  await Promise.all([
    call("Runtime.enable"),
    call("Page.enable"),
    call("Fetch.enable", {
      patterns: [{ urlPattern: "*/api/*", requestStage: "Request" }],
    }),
  ]);
  await call("Page.navigate", { url: baseURL + "/#settings" });
  for (let i = 0; i < 100; i++) {
    if (await run(`!!document.querySelector('#settings-form')`)) break;
    await delay(50);
  }
  return {
    run,
    close: async () => {
      await call("Page.close");
      socket.close();
    },
  };
}
try {
  await Promise.all([
    send("Runtime.enable"),
    send("Page.enable"),
    send("Log.enable"),
    send("Network.enable"),
  ]);
  await send("Fetch.enable", {
    patterns: [{ urlPattern: "*/api/*", requestStage: "Request" }],
  });
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Page.navigate", { url: baseURL });
  await waitFor(
    `document.querySelector('h1')?.textContent==='Your work, moving forward.'`,
  );
  // Route transitions must never silently discard an edited settings form.
  await view("settings");
  await fill("#model_id", "unsaved-hash-change");
  await evaluate(`location.hash='jobs'`);
  await delay(150);
  check(
    await evaluate(
      `document.querySelector('#dialog-title').textContent==='Leave unsaved settings?'&&document.querySelector('#app-dialog').open&&document.querySelector('#model_id')?.value==='unsaved-hash-change'`,
    ),
    "Direct hash navigation protects unsaved settings",
  );
  await close();
  await cleanSettings();
  await view("overview");
  await view("settings");
  await fill("#model_id", "unsaved-browser-back");
  await evaluate(`history.back()`);
  await delay(150);
  check(
    await evaluate(
      `document.querySelector('#dialog-title').textContent==='Leave unsaved settings?'&&document.querySelector('#app-dialog').open&&document.querySelector('#model_id')?.value==='unsaved-browser-back'`,
    ),
    "Browser Back protects unsaved settings",
  );
  await close();
  await cleanSettings();
  await view("settings");
  await fill("#model_id", "unsaved-skip-link");
  await click(".skip-link");
  await evaluate(`history.back()`);
  await delay(150);
  check(
    await evaluate(
      `document.querySelector('#model_id')?.value==='unsaved-skip-link'`,
    ),
    "Skip-to-content and return to current route preserve unsaved edits",
  );
  await close();
  await cleanSettings();
  await view("settings");
  await fill("#model_id", "unsaved-alt-refresh");
  await key("r", 1);
  await delay(100);
  check(
    await evaluate(
      `document.querySelector('#model_id')?.value==='unsaved-alt-refresh'`,
    ),
    "Keyboard refresh keeps unsaved settings",
  );
  await cleanSettings();
  // Repositories and every service command, against isolated request fixtures.
  await view("repos");
  await click('[data-action="new-repo"]');
  await fill("#repo-search", "new-project");
  await click("#repo-search-button");
  await waitFor(`document.querySelector('[data-repo-result]')`);
  await click("[data-repo-result]");
  await waitFor(`document.querySelector('#repo-branch').options.length>1`);
  await fill("#check-preset", "npm test", "change");
  await click("#add-check");
  await click("#add-check");
  check(
    await evaluate(`document.querySelector('#repo-checks').value==='npm test'`),
    "Check presets avoid duplicate commands",
  );
  await click('button[form="repo-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    fixture.repos.some((r) => r.full_name === "fixture/new-project"),
    "Repository search, branch selection, and save use the real form contract",
  );
  await click('[data-action="edit-repo"][data-repo="fixture/new-project"]');
  await fill("#repo-checks", "npm test\nnpm run build");
  await click('button[form="repo-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    fixture.repos.find((r) => r.full_name === "fixture/new-project")?.checks
      .length === 2,
    "Repository edit preserves the intended repository and verification commands",
  );
  await click('[data-action="remove-repo"][data-repo="fixture/new-project"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    !fixture.repos.some((r) => r.full_name === "fixture/new-project"),
    "Repository removal confirms before submitting",
  );
  await view("settings");
  for (const action of ["pause", "resume", "restart", "stop", "start"]) {
    await click(`[data-action="service-${action}"]`);
    if (["restart", "stop"].includes(action)) await click("#confirm-action");
    await delay(180);
    check(
      writes.some((w) => w.path === "/api/service" && w.data.action === action),
      `Worker ${action} submits its intended service command`,
    );
  }
  // Slow responses must stay with the repository and dialog that requested them.
  await view("watches");
  await click('[data-action="new-watch"]');
  targetDelay.set("example/fixture-project", 300);
  await fill("#watch-repo", "example/fixture-project", "change");
  await fill("#watch-repo", "example/second-project", "change");
  await delay(450);
  check(
    await evaluate(
      `document.querySelector('#watch-target').options[1]?.textContent.includes('Second project')`,
    ),
    "Slow watch lookup cannot replace another repository’s pull requests",
  );
  await close();
  await click('.topbar [data-action="new-task"]');
  await fill("#task-repo", "example/fixture-project", "change");
  await click('[data-action="wizard-next"]');
  await click('[data-action="wizard-back"]');
  await fill("#task-repo", "example/second-project", "change");
  await click('[data-action="wizard-next"]');
  await delay(450);
  check(
    await evaluate(
      `document.querySelector('#task-target').options[1]?.textContent.includes('Second project')`,
    ),
    "Going Back during loading cannot mix task targets across repositories",
  );
  await close();
  targetDelay.clear();
  await view("repos");
  await click('[data-action="new-repo"]');
  await fill("#repo-owner", "example");
  await fill("#repo-name", "fixture-project");
  branchesDelay = 300;
  await click("#load-branches");
  await click("#dialog-close");
  await click('.topbar [data-action="new-task"]');
  await delay(450);
  check(
    await evaluate(
      `document.querySelector('#dialog-title').textContent==='Create a task'&&!document.querySelector('.dialog-error')`,
    ),
    "A closed branch lookup cannot show errors in a newer dialog",
  );
  await close();
  branchesDelay = 0;
  // In-flight saves must protect edits; two actual browser tabs must preserve revisions.
  await view("settings");
  settingsDelay = 350;
  await fill("#model_id", "save-in-flight");
  await click('#settings-form button[type="submit"]');
  await delay(80);
  check(
    await evaluate(`document.querySelector('#model_id').disabled`),
    "Settings cannot be edited while their save is in flight",
  );
  await delay(450);
  settingsDelay = 0;
  await fill("#model_id", "first-tab-unsaved");
  const firstRevision = await evaluate(
    `document.querySelector('#settings-form').dataset.revision`,
  );
  const other = await secondTab();
  await other.run(
    `(()=>{const el=document.querySelector('#model_id');el.value='second-tab-saved';el.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#settings-form button[type="submit"]').click()})()`,
  );
  await delay(300);
  await evaluate(`document.dispatchEvent(new Event('visibilitychange'))`);
  await delay(150);
  await click('#settings-form button[type="submit"]');
  await delay(200);
  check(
    writes.filter((w) => w.path === "/api/settings").at(-1).data.revision ===
      firstRevision &&
      fixture.model.model_id === "second-tab-saved" &&
      (await evaluate(
        `document.querySelector('#model_id').value==='first-tab-unsaved'`,
      )),
    "Two browser tabs cannot overwrite a newer configuration with stale edits",
  );
  await other.close();
  await cleanSettings();
  // Refreshing task details must not present stale data as a fresh successful read.
  fixture.jobs = [
    {
      job_id: "edge-job",
      kind: "fix-pr",
      repo_full_name: "example/fixture-project",
      number: 77,
      state: "done",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      model_calls_used: 0,
      notes: {
        title: "Original task detail",
        member: "example",
        output_dir: "/home/example/results",
      },
    },
  ];
  await view("jobs");
  await click("#refresh-button");
  await delay(180);
  await click('[data-action="job-detail"]');
  stateFailure = true;
  await click('[data-action="job-refresh"]');
  await delay(180);
  check(
    await evaluate(
      `document.querySelector('#app-dialog').open&&document.querySelector('#dialog-body .dialog-error')?.textContent.includes('refresh')`,
    ),
    "Failed task refresh shows failure instead of silently reopening cached data",
  );
  stateFailure = false;
  fixture.jobs[0].notes.title = "Fresh task detail";
  await click('[data-action="job-refresh"]');
  await delay(180);
  check(
    await evaluate(
      `document.querySelector('.detail-top h3').textContent==='Fresh task detail'`,
    ),
    "Explicit task refresh updates the displayed task snapshot",
  );
  await close();
  // Remaining public commands and recovery branches stay within their own flow.
  await view("watches");
  await click('[data-action="new-watch"]');
  await fill("#watch-repo", "example/fixture-project", "change");
  await waitFor(`document.querySelector('#watch-target').options.length>1`);
  await fill("#watch-target", "77", "change");
  await fill("#watch-account", "example", "change");
  await click("#watch-browse");
  await waitFor(`document.querySelector('[data-folder-index="0"]')`);
  await click('[data-folder-index="0"]');
  await waitFor(
    `document.querySelector('#folder-select')&&!document.querySelector('#folder-select').disabled`,
  );
  await click("#folder-select");
  await click("#watch-confirm");
  await click('button[form="watch-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    writes.filter((w) => w.path === "/api/watches").at(-1).data.mode ===
      "selected" &&
      writes.filter((w) => w.path === "/api/watches").at(-1).data.number === 77,
    "Selected-PR watch submits the chosen pull request and explicit destination",
  );
  await click('[data-action="remove-watch"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    fixture.watches.length === 0,
    "Review watch removal stops the selected watch without removing task history",
  );
  await view("settings");
  providerFailure = true;
  await click('[data-action="provider-check"]');
  await click("#confirm-action");
  await waitFor(
    `document.querySelector('.dialog-error')?.textContent.includes('unavailable')`,
  );
  const failedProviderCalls = providerCalls;
  await delay(250);
  check(
    providerCalls === failedProviderCalls &&
      (await evaluate(`!document.querySelector('#confirm-action').disabled`)),
    "Failed provider check stops after one request and offers explicit retry",
  );
  providerFailure = false;
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  check(
    providerCalls === failedProviderCalls + 1,
    "Provider-check retry sends exactly one additional explicit request",
  );
  credentialFailure = true;
  await click('[data-action="store-credential"]');
  await fill("#credential-value", "edge-secret");
  await fill("#credential-supplier", "Browser edge test");
  await click('button[form="credential-form"]');
  await waitFor(
    `document.querySelector('.dialog-error')?.textContent.includes('unavailable')`,
  );
  check(
    await evaluate(
      `document.querySelector('#credential-value').type==='password'&&!document.querySelector('#dialog-body').textContent.includes('edge-secret')`,
    ),
    "Credential storage failure never exposes the secret as page text",
  );
  credentialFailure = false;
  await click('button[form="credential-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  requestDelay.set("/api/credentials", 300);
  await click('[data-action="store-credential"]');
  await fill("#credential-value", "edge-secret-two");
  await fill("#credential-supplier", "Browser edge test");
  await click('button[form="credential-form"]');
  await click("#dialog-close");
  await click('.topbar [data-action="new-task"]');
  await delay(450);
  check(
    await evaluate(
      `document.querySelector('#app-dialog').open&&document.querySelector('#dialog-title').textContent==='Create a task'`,
    ),
    "Completing a closed credential form cannot dismiss a newer task dialog",
  );
  await close();
  requestDelay.delete("/api/credentials");
  diagnosticsFailure = true;
  await click('[data-action="diagnostics"]');
  await waitFor(`document.querySelector('.dialog-error')`);
  check(
    await evaluate(
      `document.querySelector('.dialog-error').textContent.includes('diagnostics unavailable')`,
    ),
    "Diagnostics failure is visible without a model request",
  );
  await close();
  diagnosticsFailure = false;
  await click('[data-action="diagnostics"]');
  await waitFor(`document.querySelector('.diagnostic-row.pass')`);
  check(true, "Diagnostics can be reopened successfully after a failure");
  await close();
  githubStatus = "waiting";
  await click('[data-action="github-connect"]');
  await waitFor(`document.querySelector('#copy-device-code')`);
  githubStatus = "connected";
  for (let i = 0; i < 30; i++) {
    if (await evaluate(`!document.querySelector('#app-dialog').open`)) break;
    await delay(200);
  }
  check(
    await evaluate(`!document.querySelector('#app-dialog').open`),
    "GitHub device-flow polling recognizes a completed connection",
  );
  githubStatus = "error";
  await click('[data-action="github-connect"]');
  await waitFor(`document.querySelector('.dialog-error')`);
  check(
    await evaluate(
      `document.querySelector('.dialog-error').textContent.includes('connection failed')`,
    ),
    "GitHub connection errors stay visible with a close-and-retry path",
  );
  await close();
  githubStatus = "waiting";
  await click('.topbar [data-action="new-task"]');
  await fill("#task-repo", "example/fixture-project", "change");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#task-target').options.length>1`);
  await fill("#task-target", "77", "change");
  await waitFor(
    `[...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#77'))`,
  );
  await click('[data-action="wizard-next"]');
  await fill("#task-account", "example", "change");
  await click("#task-browse");
  await waitFor(`document.querySelector('#folder-create-form')`);
  await fill("#folder-new-name", "../outside");
  await click("#folder-create-form button");
  await waitFor(`document.querySelector('#folder-body .dialog-error')`);
  check(
    await evaluate(
      `document.querySelector('#folder-body .dialog-error').textContent.includes('directory name')&&document.querySelector('#task-destination').value===''`,
    ),
    "Invalid folder creation preserves the unset output destination",
  );
  requestDelay.set("/api/folders", 300);
  await fill("#folder-new-name", "created-before-close");
  await click("#folder-create-form button");
  await click("#folder-close");
  await click("#task-browse");
  await delay(700);
  check(
    await evaluate(
      `document.querySelector('#folder-dialog').open&&document.querySelector('#folder-body .folder-location code').textContent==='/home/example'`,
    ),
    "Finishing an old folder creation cannot steer a reopened folder browser",
  );
  await close();
  requestDelay.delete("/api/folders");
  // Keyboard Escape closes only the topmost dialog and navigation remains usable.
  await click('.topbar [data-action="new-task"]');
  await fill("#task-repo", "example/fixture-project", "change");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#task-target').options.length>1`);
  await fill("#task-target", "77", "change");
  await waitFor(
    `document.querySelector('#task-number').value==='77'&&[...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#77'))`,
  );
  await click('[data-action="wizard-next"]');
  await fill("#task-account", "example", "change");
  await click("#task-browse");
  await waitFor(`document.querySelector('#folder-select')`);
  await key("Escape");
  await delay(120);
  check(
    await evaluate(
      `!document.querySelector('#folder-dialog').open&&document.querySelector('#app-dialog').open&&document.querySelector('#task-account').value==='example'`,
    ),
    "Escape closes the folder dialog without losing its parent task",
  );
  await key("Escape");
  await delay(100);
  check(
    await evaluate(`!document.querySelector('#app-dialog').open`),
    "Escape closes the task dialog",
  );
  // Failed transport, unreadable JSON, and genuine client timeout all have recovery paths.
  badStateJSON = true;
  await click("#refresh-button");
  await delay(120);
  check(
    await evaluate(
      `[...document.querySelectorAll('.toast.error')].some(e=>e.textContent.includes('unreadable response'))`,
    ),
    "Unreadable state responses are reported without inventing empty state",
  );
  badStateJSON = false;
  await click("#refresh-button");
  await delay(120);
  await click('.topbar [data-action="new-task"]');
  await fill("#task-repo", "example/fixture-project", "change");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#task-target').options.length>1`);
  networkFailures.add("/api/github/target");
  await fill("#task-number", "99");
  await click("#lookup-target");
  await delay(180);
  check(
    await evaluate(
      `document.querySelector('.dialog-error')?.textContent.includes('Cannot reach')`,
    ),
    "Connection reset during target lookup is explained",
  );
  await click("#lookup-target");
  await delay(150);
  check(
    await evaluate(
      `[...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#99'))`,
    ),
    "A failed lookup can be retried without losing repository context",
  );
  lookupDelay = 31000;
  await fill("#task-number", "100");
  await click("#lookup-target");
  for (let i = 0; i < 140; i++) {
    if (
      await evaluate(
        `document.querySelector('.dialog-error')?.textContent.includes('too long')`,
      )
    )
      break;
    await delay(250);
  }
  check(
    await evaluate(
      `document.querySelector('.dialog-error')?.textContent.includes('too long')`,
    ),
    "A genuine 30-second lookup timeout is reported and stops loading",
  );
  lookupDelay = 0;
  await click("#lookup-target");
  await delay(180);
  check(
    await evaluate(
      `[...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#100'))`,
    ),
    "Timed-out lookup can recover through an explicit retry",
  );
  await close();
  // Reflow at 320/390 pixels, desktop, and the equivalent of 200% desktop zoom.
  for (const width of [320, 390, 720, 1440]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height: 900,
      deviceScaleFactor: width === 720 ? 2 : 1,
      mobile: width < 721,
    });
    for (const name of [
      "overview",
      "jobs",
      "doorbell",
      "repos",
      "watches",
      "fence",
      "settings",
      "help",
    ]) {
      await view(name);
      if (name === "doorbell")
        await waitFor(
          `document.querySelector('.doorbell-row')||document.querySelector('.empty-state')`,
        );
      if (name === "fence") await waitFor(`document.querySelector('.fence-repo')`);
      check(
        await evaluate(
          `document.documentElement.scrollWidth<=${width}&&document.body.getBoundingClientRect().width<=${width}`,
        ),
        `${name} fits ${width}px${width === 720 ? " (200% desktop zoom equivalent)" : ""}`,
      );
    }
    if (width <= 390) {
      await click("#mobile-menu");
      await delay(250);
      await key("Escape");
      await delay(100);
      check(
        await evaluate(
          `document.querySelector('#sidebar').inert&&document.querySelector('#mobile-menu').getAttribute('aria-expanded')==='false'&&!document.querySelector('main').inert`,
        ),
        `Mobile Escape restores focus and interaction at ${width}px`,
      );
      await click('.topbar [data-action="new-task"]');
      check(
        await evaluate(
          `document.querySelector('#app-dialog').getBoundingClientRect().width<=${width}&&document.querySelector('#app-dialog').scrollWidth<=document.querySelector('#app-dialog').clientWidth`,
        ),
        `Task dialog fits ${width}px`,
      );
      await close();
    }
  }
  await send("Page.bringToFront");
  const firstUpdate = await evaluate(
    `document.querySelector('#last-updated').textContent`,
  );
  await evaluate(
    `document.querySelector('#navigation a[href="#repos"]').focus()`,
  );
  await delay(11000);
  check(
    await evaluate(
      `document.activeElement===document.querySelector('#navigation a[href="#repos"]')&&document.querySelector('#last-updated').textContent!==${JSON.stringify(firstUpdate)}`,
    ),
    "Periodic refresh preserves a focused desktop navigation link",
  );
  const secondUpdate = await evaluate(
    `document.querySelector('#last-updated').textContent`,
  );
  await evaluate(
    `document.querySelectorAll('main details')[1].open=true;document.querySelector('main [data-action="diagnostics"]').focus()`,
  );
  await delay(11000);
  check(
    await evaluate(
      `document.activeElement===document.querySelector('main [data-action="diagnostics"]')&&document.querySelector('#last-updated').textContent!==${JSON.stringify(secondUpdate)}`,
    ),
    "Periodic refresh preserves a focused main action button",
  );
  check(
    await evaluate(`document.querySelectorAll('main details')[1].open`),
    "Periodic refresh preserves the help section being read",
  );
  check(
    errors.every(
      (error) =>
        error.source === "network" &&
        /400|409|503|ERR_CONNECTION_RESET|ERR_ABORTED/.test(error.text || ""),
    ),
    "No unexpected JavaScript or rendering exceptions in edge cases",
  );
  await writeFile(
    join(outputDir, "edge-report.json"),
    JSON.stringify(
      {
        checks,
        failed: checks.filter((c) => !c.pass),
        errors,
        syntheticWritePaths: writes.map((w) => w.path),
      },
      null,
      2,
    ),
  );
  console.log(
    "EdgeSummary",
    checks.filter((c) => c.pass).length,
    "passed;",
    checks.filter((c) => !c.pass).length,
    "failed",
  );
  if (checks.some((c) => !c.pass)) process.exitCode = 1;
} finally {
  await send("Browser.close").catch(() => {});
  ws.close();
  chrome.kill();
  await rm(profile, { recursive: true, force: true, maxRetries: 2 });
}
