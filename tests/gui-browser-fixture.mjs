// Isolated browser submission/error tests. CDP fulfills EVERY /api/* request; no live writes.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-browser-fixture.mjs http://worker-address:18465 /home/you/gui-fixtures
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
    "Usage: node tests/gui-browser-fixture.mjs <workspace-url> <output-folder>",
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
const taskTypeCatalog = [
  {
    id: "explain-repo",
    label: "Explain this repository",
    help: "Writes a plain-words tour of the repository into your folder.",
    takes: "repo",
    profile: "read-only",
    fields: [{ name: "focus", label: "What to focus on", required: false }],
  },
  {
    id: "fix-pr",
    label: "Fix a pull request",
    help: "Reads review comments, fixes the code, and proves it with the repository's checks.",
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
  {
    id: "summarise-pr",
    label: "Summarise a pull request",
    help: "Writes a short summary of a pull request into your folder.",
    takes: "pr",
    profile: "read-only",
    fields: [],
  },
];
let authorityMode = "ask";
let decisionKind = "draft-pr-reply";
const decisionDrafts = [
  { name: "one", title: "First draft", body: "First exact words." },
  { name: "two", title: "Second draft", body: "Second exact words." },
];
const writes = [];
let providerCalls = 0;
let stateFailure = false;
let staleSettings = false;
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
    if (stateFailure) {
      status = 503;
      result = { error: "Fixture connection unavailable" };
    } else result = fixture;
  } else if (path === "/api/github/user")
    result = {
      login: url.searchParams.get("login") || "connected-fixture",
      id: url.searchParams.get("login") ? 123456 : 196844434,
    };
  else if (path === "/api/github/targets") result = { items: [targetItem] };
  else if (path === "/api/github/target") result = { item: targetItem };
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
    result = { path: data.parent + "/" + data.name };
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
    fixture.repos.push({ ...data, full_name: data.owner + "/" + data.name });
    fixture.service.restart_required = true;
    result = { ok: true, restart_required: true };
  } else if (path === "/api/repos/remove") {
    fixture.repos = fixture.repos.filter((r) => r.full_name !== data.repo);
    result = { ok: true };
  } else if (path === "/api/diagnostics")
    result = {
      checks: [
        { name: "Fixture setup", status: "pass", message: "No model request." },
      ],
    };
  else if (path === "/api/provider-check") {
    providerCalls++;
    result = { ok: true, output: "Fixture model response." };
  } else if (path === "/api/credentials")
    result = {
      ok: true,
      record: { kind: data.kind, supplied_by: data.supplied_by },
    };
  else if (path === "/api/service") {
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
      status: "waiting",
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
  } else if (path === "/api/task-types") result = { items: taskTypeCatalog };
  else if (path === "/api/jobs/draft-job-1" && method === "GET")
    result = { job: { job_id: "draft-job-1", kind: decisionKind,
      repo_full_name: "example/fixture-project", state: "waiting-on-you",
      notes: { drafts: decisionDrafts, values: {source: "develop", target: "main"} } } };
  else if (path === "/api/authority" && method === "GET")
    result = {
      modes: { "example/fixture-project": authorityMode },
      grants: [],
      explanation:
        "Drafts wait for you unless you choose to post directly for a repository. Merging, deleting, closing and releasing always ask.",
    };
  else if (path === "/api/authority") {
    authorityMode = data.mode;
    result = { repo: data.repo, mode: data.mode };
  } else if (path === "/api/fence")
    result = {
      repos: [
        {
          repo: "example/fixture-project",
          checks_configured: 1,
          protection: "safe",
          watches_armed: true,
          post_authority: authorityMode,
          base_branch: "main",
          rulesets_readable: "unknown",
        },
        {
          repo: "example/unchecked-project",
          checks_configured: 0,
          protection: "no-checks",
          watches_armed: false,
          post_authority: authorityMode,
          base_branch: "main",
          rulesets_readable: "unknown",
        },
      ],
      copilot_runs_cancelled: 2,
      model_calls_today: 14,
      actions_runs_started_by_worker: 0,
      note: "The worker starts no GitHub Actions runs and cancels Copilot reviews on its own pushes.",
      hint: "This fence is a read-only summary; nothing here changes a repository.",
    };
  else if (path === "/api/digest")
    result = {
      done: [
        {
          kind: "done",
          at: new Date(Date.now() - 3600000).toISOString(),
          job_id: "fixture-job-1",
          repo: "example/fixture-project",
          number: 77,
          task: "implement-issue",
          text: "implement-issue example/fixture-project#77 finished",
        },
      ],
      waiting_on_you: {
        events: [
          {
            kind: "waiting-on-you",
            at: new Date(Date.now() - 600000).toISOString(),
            job_id: "draft-job-1",
            repo: "example/fixture-project",
            number: 12,
            task: "draft-pr-reply",
            text: "A draft for example/fixture-project#12 is ready for your decision",
          },
        ],
        drafts: [
          {
            job_id: "draft-job-1",
            kind: "draft-pr-reply",
            repo_full_name: "example/fixture-project",
            number: 12,
            state: "waiting-on-you",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            model_calls_used: 3,
            notes: { title: "Reply to fixture reviewer" },
          },
        ],
      },
      watched: [
        { kind: "watch", repo: "example/fixture-project", number: "77", mode: "selected" },
        { kind: "watch", repo: "example/fixture-project", number: "*", mode: "all" },
      ],
    };
  else if (path.endsWith("/draft-action") && path.startsWith("/api/jobs/")) {
    status = 200;
    result = { ok: true, hint: "Posted. The words are now on GitHub." };
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
    route(msg.params.request)
      .then(({ status, result }) =>
        send("Fetch.fulfillRequest", {
          requestId: msg.params.requestId,
          responseCode: status,
          responseHeaders: [
            { name: "Content-Type", value: "application/json" },
          ],
          body: Buffer.from(JSON.stringify(result)).toString("base64"),
        }),
      )
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
    width: 1365,
    height: 950,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Page.navigate", { url: baseURL });
  await waitFor(
    `document.querySelector('h1')?.textContent==='Your work, moving forward.'`,
  );
  await screenshot("01-overview");
  await evaluate(`location.hash='fleet'`);
  await waitFor(`document.querySelector('#fleet-start-form')`);
  await screenshot("02-fleet-dry-run");
  await evaluate(`location.hash='overview'`);
  await waitFor(
    `document.querySelector('h1')?.textContent==='Your work, moving forward.'`,
  );
  assert(
    await evaluate(
      `document.querySelector('.empty-state h3')?.textContent==='A fresh start'`,
    ),
    "Empty activity is explained without fake records",
  );
  assert(providerCalls === 0, "Loading workspace makes no model request");
  await click('.topbar [data-action="new-task"]');
  await waitFor(`document.querySelector('input[value="implement-issue"]')`);
  assert(
    await evaluate(
      `[...document.querySelectorAll('.choice-card strong')].map(e=>e.textContent).join('|')===${JSON.stringify(
        taskTypeCatalog.map((item) => item.label).join("|"),
      )}`,
    ),
    "Wizard cards are rendered from the worker's task list, label and order included",
  );
  assert(
    await evaluate(
      `[...document.querySelectorAll('.choice-card p')].map(e=>e.textContent).join('|')===${JSON.stringify(
        taskTypeCatalog.map((item) => item.help).join("|"),
      )}`,
    ),
    "Every wizard card carries the worker's plain-words help",
  );
  await fill("#task-repo", "example/fixture-project", "change");
  await click('input[value="implement-issue"]');
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#task-target').options.length>1`);
  await fill("#task-target", "77", "change");
  await waitFor(
    `document.querySelector('.selection-summary strong')&&[...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#77'))`,
  );
  await click('[data-action="wizard-next"]');
  await fill("#task-account", "example", "change");
  assert(
    await evaluate(`document.querySelector('#task-destination').value===''`),
    "Destination is never selected automatically",
  );
  assert(
    writes.filter((w) => w.path === "/api/folders").length === 0,
    "Folder creation is not implicit",
  );
  await click("#task-browse");
  await waitFor(`document.querySelector('#folder-create-form')`);
  await fill("#folder-new-name", "fixture-output");
  await click("#folder-create-form button");
  await waitFor(
    `document.querySelector('.folder-location code')?.textContent==='/home/example/fixture-output'`,
  );
  await click("#folder-select");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#queue-task')`);
  assert(
    await evaluate(
      `document.querySelector('.source-context').textContent.includes('Required behavior')&&!document.querySelector('.source-context script')&&!globalThis.badSource`,
    ),
    "Full issue source is visible and unsafe HTML is escaped",
  );
  assert(
    await evaluate(
      `document.querySelector('.effects').textContent.includes('watched automatically')&&document.querySelector('.effects').textContent.includes('configured model connection')&&document.querySelector('.review-list').textContent.includes('subscription allowance')&&!document.querySelector('.effects').textContent.includes('incur charges')`,
    ),
    "Final review discloses automatic future model work",
  );
  await click("#queue-task");
  await modal("Task details");
  await waitFor(`document.querySelector('[data-action="job-stop"]')`);
  const jobPost = writes.find((w) => w.path === "/api/jobs");
  assert(
    jobPost.data.output_dir === "/home/example/fixture-output" &&
      jobPost.data.account === "example" &&
      jobPost.data.number === 77,
    "Queue submission includes explicit member, target, and real chosen path",
  );
  assert(
    !(await evaluate(`!!document.querySelector('[data-action="job-retry"]')`)),
    "Received task offers Stop rather than Retry",
  );
  await click('[data-action="job-files"]');
  await waitFor(`document.querySelector('[data-job-file="0"]')`);
  assert(
    await evaluate(
      `document.querySelector('#job-file-browser a[download]').href.endsWith('/api/jobs/fixture-job-1/download?path=report.json')`,
    ),
    "Individual result files offer correctly scoped download links",
  );
  await click('[data-job-file="0"]');
  await waitFor(`document.querySelector('.file-content pre')`);
  assert(
    await evaluate(
      `document.querySelector('.file-content a[download]')?.textContent.includes('Download file')`,
    ),
    "Text preview includes Download file",
  );
  await click('#job-file-browser [data-action="job-files"]');
  await waitFor(`document.querySelector('[data-job-file="1"]')`);
  await click('[data-job-file="1"]');
  await waitFor(
    `document.querySelector('#job-file-browser .inline-notice.error')`,
  );
  assert(
    await evaluate(
      `document.querySelector('#job-file-browser a[download]').href.endsWith('path=image.png')`,
    ),
    "Binary preview failure retains a file download path",
  );
  await click('[data-action="job-stop"]');
  await modal("Stop this task?");
  assert(
    await evaluate(
      `document.querySelector('#dialog-body').textContent.includes('Review watches remain enabled')`,
    ),
    "Stop explains that watches remain enabled",
  );
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  await click('[data-action="job-detail"]');
  await modal("Task details");
  assert(
    await evaluate(`!!document.querySelector('[data-action="job-retry"]')`),
    "Stopped task offers a deliberate retry",
  );
  assert(
    await evaluate(
      `document.querySelector('#dialog-body').textContent.includes('Local results need attention')&&document.querySelector('#dialog-body').textContent.includes('Verification command output')&&document.querySelector('#dialog-body').textContent.includes('Open pull request #88')`,
    ),
    "Task details surface export failures, check output, and created pull request",
  );
  await click('[data-action="job-retry"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  await evaluate(`location.hash='watches'`);
  await delay(100);
  await click('[data-action="new-watch"]');
  await fill("#watch-repo", "example/fixture-project", "change");
  await fill("#watch-mode", "all", "change");
  await fill("#watch-account", "example", "change");
  await click("#watch-browse");
  await waitFor(`document.querySelector('#folder-select')`);
  await click('[data-folder-index="0"]');
  await waitFor(
    `document.querySelector('.folder-location code')?.textContent==='/home/example/results'`,
  );
  await click("#folder-select");
  await click("#watch-confirm");
  await click('button[form="watch-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  const watchPost = writes.find((w) => w.path === "/api/watches");
  assert(
    watchPost.data.mode === "all" &&
      watchPost.data.output_dir === "/home/example/results" &&
      watchPost.data.account === "example",
    "All-PR watch submission includes explicit output contract",
  );
  await evaluate(`location.hash='settings'`);
  await delay(100);
  await screenshot("03-settings");
  await fill("#command-user-lookup", "fixture-user");
  await click("#command-user-add");
  await waitFor(
    `document.querySelector('#command-user-feedback').textContent.includes('was added')`,
  );
  assert(
    await evaluate(
      `document.querySelector('#allowed_command_users').value.includes('123456')`,
    ),
    "Familiar username lookup adds the permanent GitHub account ID",
  );
  await click('[data-action="add-connected-user"]');
  await waitFor(
    `document.querySelector('#command-user-feedback').textContent.includes('connected-fixture')`,
  );
  assert(
    await evaluate(
      `document.querySelector('#allowed_command_users').value.split('\\n').filter(v=>v==='196844434').length===1`,
    ),
    "Add connected account prevents duplicate numeric IDs",
  );
  await fill("#model_id", "fixture-new-model");
  fixture.config_revision = "revision-2";
  await evaluate(`document.dispatchEvent(new Event('visibilitychange'))`);
  await delay(200);
  await click('#settings-form button[type="submit"]');
  await waitFor(`document.querySelector('.toast.error')`);
  const stalePost = writes.filter((w) => w.path === "/api/settings").at(-1);
  assert(
    stalePost.data.revision === "revision-1",
    "Settings save uses edit-start revision despite background refresh",
  );
  assert(
    await evaluate(
      `document.querySelector('#model_id').value==='fixture-new-model'`,
    ),
    "Stale save preserves unsaved edits for review",
  );
  await click('[data-action="settings-reload"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  assert(
    await evaluate(
      `document.querySelector('#settings-form').dataset.revision==='revision-2'`,
    ),
    "Explicit Reload saved gets current configuration revision",
  );
  await fill("#model_id", "fixture-valid-model");
  await click('#settings-form button[type="submit"]');
  await delay(300);
  assert(
    writes.filter((w) => w.path === "/api/settings").at(-1).data.revision ===
      "revision-2",
    "Fresh settings save submits current revision",
  );
  assert(
    await evaluate(
      `document.querySelector('.banner-warning')?.textContent.includes('restart')`,
    ),
    "Saved configuration clearly awaits restart",
  );
  await click('[data-action="diagnostics"]');
  await waitFor(`document.querySelector('.diagnostic-row')`);
  assert(providerCalls === 0, "Setup checks use no model call");
  await click("#dialog-close");
  await click('[data-action="provider-check"]');
  assert(
    providerCalls === 0 &&
      (await evaluate(
        `document.querySelector('#dialog-body').textContent.includes('Billing follows the selected provider or subscription')&&!document.querySelector('#dialog-body').textContent.includes('may charge')`,
      )),
    "Opening provider test confirmation uses no model call",
  );
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  assert(
    providerCalls === 1,
    "Explicit provider confirmation sends exactly one request",
  );
  await click('[data-action="store-credential"]');
  await fill("#credential-value", "fixture-secret-value");
  await fill("#credential-supplier", "UI fixture");
  await click('button[form="credential-form"]');
  await waitFor(`!document.querySelector('#app-dialog').open`);
  assert(
    !(await evaluate(
      `document.body.textContent.includes('fixture-secret-value')`,
    )),
    "Stored credential is never displayed back to the browser",
  );
  await click('[data-action="github-connect"]');
  await waitFor(`document.querySelector('#copy-device-code')`);
  assert(
    await evaluate(
      `document.querySelector('#dialog-body').textContent.includes('Closing it does not cancel')`,
    ),
    "Device-flow close behavior is explicit",
  );
  await click("#dialog-close");
  await fill("#model_id", "pending-user-edit");
  await click('a.nav-link[href="#jobs"]');
  await modal("Leave unsaved settings?");
  await click('[data-action="dialog-close"]');
  assert(
    await evaluate(
      `location.hash==='#settings'&&document.querySelector('#model_id').value==='pending-user-edit'`,
    ),
    "Canceling navigation keeps unsaved settings",
  );
  await click('[data-action="settings-reload"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  stateFailure = true;
  await send("Page.reload", { ignoreCache: true });
  await waitFor(
    `document.querySelector('.empty-state h3')?.textContent==='Your worker is unavailable'`,
  );
  assert(
    await evaluate(
      `document.querySelector('[data-action="refresh"]')?.textContent==='Try again'`,
    ),
    "Initial connection failure has a retry path",
  );
  stateFailure = false;
  await click('[data-action="refresh"]');
  await waitFor(`document.querySelector('#settings-form')`);
  assert(
    await evaluate(`!!document.querySelector('#settings-form')`),
    "Retry recovers to requested page",
  );
  // A repository-wide task skips the item picker and never sends a number.
  await evaluate(`location.hash='overview'`);
  await delay(120);
  await click('.topbar [data-action="new-task"]');
  await waitFor(`document.querySelector('input[value="explain-repo"]')`);
  await fill("#task-repo", "example/fixture-project", "change");
  await click('input[value="explain-repo"]');
  await waitFor(`document.querySelector('#task-field-focus')`);
  assert(
    await evaluate(
      `document.querySelector('.choice-card.selected strong').textContent==='Explain this repository'&&document.querySelector('#task-field-focus').labels[0].textContent.includes('What to focus on')&&document.querySelector('#task-field-focus').labels[0].textContent.includes('optional')`,
    ),
    "Repository-wide task shows its declared field and marks it optional",
  );
  await fill("#task-field-focus", "the verification commands");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#task-account')`);
  assert(
    await evaluate(
      `!document.querySelector('#task-target')&&document.querySelectorAll('.step').length===3`,
    ),
    "Repository-wide task skips the work-item step entirely",
  );
  await fill("#task-account", "example", "change");
  await click("#task-browse");
  await waitFor(`document.querySelector('[data-folder-index="0"]')`);
  await click('[data-folder-index="0"]');
  await waitFor(
    `document.querySelector('#folder-select')&&!document.querySelector('#folder-select').disabled`,
  );
  await click("#folder-select");
  await click('[data-action="wizard-next"]');
  await waitFor(`document.querySelector('#queue-task')`);
  assert(
    await evaluate(
      `document.querySelector('.review-list').textContent.includes('Explain this repository')&&document.querySelector('.review-list').textContent.includes('the verification commands')&&!document.querySelector('.review-list').textContent.includes('Pull request')`,
    ),
    "Review lists the worker's own label and the answers given, with no invented item",
  );
  await click("#queue-task");
  await modal("Task details");
  const repoTaskPost = writes.filter((w) => w.path === "/api/jobs").at(-1);
  assert(
    !("number" in repoTaskPost.data) &&
      repoTaskPost.data.focus === "the verification commands" &&
      repoTaskPost.data.kind === "explain-repo" &&
      repoTaskPost.data.output_dir === "/home/example/results",
    "Repository-wide submission omits the number and sends declared field values",
  );
  await click("#dialog-close");
  // The doorbell holds three piles and refuses to pretend posting exists.
  await evaluate(`location.hash='doorbell'`);
  await waitFor(`document.querySelector('.doorbell-row')`);
  assert(
    await evaluate(
      `[...document.querySelectorAll('.panel')].map(p=>p.querySelector('h2')?.textContent).join('|')==='Done|Waiting on you|Watched'`,
    ),
    "Doorbell shows Done, Waiting on you, and Watched piles",
  );
  assert(
    await evaluate(
      `document.querySelector('.doorbell-row p').textContent==='implement-issue example/fixture-project#77 finished'`,
    ),
    "Doorbell rows are one plain sentence from the worker's record",
  );
  assert(
    await evaluate(
      `(()=>{const b=document.querySelector('[data-action="draft-post"]');return !b.disabled&&b.getAttribute('aria-describedby')&&(()=>{const text=document.getElementById(b.getAttribute('aria-describedby')).textContent;return /Post.*sends/.test(text)&&/Edit.*wording/.test(text)&&/Discard.*remaining drafts/.test(text)&&/Previously posted.*remain/.test(text)})()})()`,
    ),
    "Draft buttons are live and state plainly what each choice does",
  );
  assert(
    await evaluate(
      `[...document.querySelectorAll('.doorbell-row p')].some(p=>p.textContent==='Watching every open pull request for example/fixture-project.')`,
    ),
    "Watched pile states each watch in plain words",
  );
  await evaluate(
    `document.querySelector('[data-action="draft-post"]').click()`,
  );
  await waitFor(`document.querySelector('#draft-choice')`);
  await fill("#draft-choice", "1", "change");
  await evaluate(`document.querySelector('button[form="draft-decision-form"]').click()`);
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert(
    writes.some((w) => w.path === "/api/jobs/draft-job-1/draft-action" && w.data.name === "two"),
    "Multi-draft posting sends the selected second draft instead of defaulting to the first",
  );
  await evaluate(`document.querySelector('[data-action="draft-edit"]').click()`);
  await waitFor(`document.querySelector('#draft-body')`);
  await fill("#draft-choice", "1", "change");
  await fill("#draft-body", "Revised second draft.");
  await evaluate(`document.querySelector('button[form="draft-decision-form"]').click()`);
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert(writes.at(-1).data.action === "edit" && writes.at(-1).data.name === "two" && writes.at(-1).data.body === "Revised second draft.",
    "Edit sends the selected draft name and actual edited body without posting");
  // An explicit confirmation must precede the only merge-capable task.
  decisionKind = "sync-branch";
  taskTypeCatalog.push({id: decisionKind, label: "Bring a branch up to date", profile: "write", post_confirm_required: true, takes: "repo", fields: []});
  decisionDrafts.splice(1);
  await evaluate(`location.reload()`);
  await delay(300);
  await waitFor(`document.querySelector('[data-action="draft-post"]')`);
  const postsBefore = writes.filter((w) => w.path.endsWith('/draft-action')).length;
  await evaluate(`document.querySelector('[data-action="draft-post"]').click()`);
  await waitFor(`document.querySelector('#confirm-action')`);
  assert(writes.filter((w) => w.path.endsWith('/draft-action')).length === postsBefore,
    "A confirmation-required task cannot publish merely by choosing Post");
  await evaluate(`document.querySelector('#confirm-action').click()`);
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert(writes.at(-1).data.confirm === true && writes.at(-1).data.name === "one",
    "The separate confirmation sends confirm true with the exact selected draft");
  // The safety fence names what could land unverified and who may post.
  await evaluate(`location.hash='fence'`);
  await waitFor(`document.querySelector('.fence-repo')`);
  assert(
    await evaluate(
      `(()=>{const e=document.querySelector('.fence-danger .inline-notice.error');return !!e&&/No check commands/.test(e.textContent)&&/before.*publish/.test(e.textContent)})()`,
    ),
    "A repository without checks is shown red with the unverified-changes sentence",
  );
  assert(
    await evaluate(
      `document.querySelector('.panel').textContent.includes('Model calls used today')&&document.querySelector('.panel').textContent.includes('14')&&document.querySelector('.fence-repo').textContent.includes('Watching for feedback')`,
    ),
    "Fence states today's model calls and whether review follow-up is armed",
  );
  assert(
    await evaluate(
      `[...document.querySelectorAll('.toast')].some(t=>t.textContent.includes('read-only summary'))`,
    ),
    "A successful reply's hint is shown without blocking anything",
  );
  await click('[data-action="authority-toggle"]');
  await modal("Post drafts without asking?");
  assert(
    await evaluate(
      `document.querySelector('#dialog-body').textContent.includes('always ask')&&document.querySelector('#dialog-body').textContent.includes('example/fixture-project')`,
    ),
    "Changing posting permission names the repository and the standing guarantees",
  );
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  await waitFor(
    `[...document.querySelectorAll('.fence-repo')].some(r=>r.textContent.includes('Posts directly'))`,
  );
  assert(
    writes.some(
      (w) =>
        w.path === "/api/authority" &&
        w.data.repo === "example/fixture-project" &&
        w.data.mode === "auto",
    ),
    "The posting toggle saves the chosen mode through /api/authority",
  );
  console.log("BrowserDiagnosticErrors", JSON.stringify(errors));
  // The draft-action refusal is the one deliberate 404: Phase 0 has no posting
  // endpoint, and the page must show the worker's hint rather than fail silently.
  const draftRefusal = (e) =>
    e.source === "network" &&
    /status of 404\b/.test(e.text || "") &&
    (e.url || "").endsWith("/api/jobs/draft-job-1/draft-action");
  assert(
    errors.filter(
      (e) =>
        !draftRefusal(e) &&
        (e.source !== "network" ||
          !/status of (400|409|503)\b/.test(e.text || "")),
    ).length === 0,
    "No JavaScript exceptions in functional fixture flows",
  );
  console.log(
    "MutationCounts",
    writes.reduce((r, w) => ((r[w.path] = (r[w.path] || 0) + 1), r), {}),
  );
  await writeFile(
    join(outputDir, "fixture-report.json"),
    JSON.stringify(
      {
        checks: "passed",
        providerCalls,
        writePaths: writes.map((w) => w.path),
        errors,
      },
      null,
      2,
    ),
  );
} finally {
  await send("Browser.close").catch(() => {});
  ws.close();
  chrome.kill();
  await rm(profile, { recursive: true, force: true, maxRetries: 2 });
}
