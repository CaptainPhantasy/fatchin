// Read-only Chrome integration check. No npm dependencies; uses the host's installed Chrome.
// Usage: node tests/gui-browser.mjs http://worker-address:18465 /home/you/gui-receipts
import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile, mkdir, rm } from "node:fs/promises";
import { tmpdir, homedir } from "node:os";
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
    "Usage: node tests/gui-browser.mjs <workspace-url> <output-folder>",
  );
  process.exit(2);
}
await mkdir(outputDir, { recursive: true });
const installedState = await (
  await fetch(new URL("/api/state", baseURL))
).json();
const ownAccount = installedState.accounts.find((a) => a.path === homedir());
const candidateRepos = [...installedState.repos].sort(
  (a, b) => Number(!a.name.includes("demo")) - Number(!b.name.includes("demo")),
);
let scenario = null;
for (const r of candidateRepos.slice(0, 5)) {
  const repo = r.full_name || `${r.owner}/${r.name}`;
  const response = await (
    await fetch(
      new URL(
        "/api/github/targets?" +
          new URLSearchParams({ repo, kind: "implement-issue", state: "open" }),
        baseURL,
      ),
    )
  ).json();
  if (response.items?.length) {
    scenario = { repo, target: response.items[0] };
    break;
  }
}
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
    captureBeyondViewport: !/dialog|review|folder|diagnostic|navigation/.test(
      name,
    ),
  });
  await writeFile(
    join(outputDir, `${name}.png`),
    Buffer.from(r.data, "base64"),
  );
};
try {
  await Promise.all([
    send("Runtime.enable"),
    send("Page.enable"),
    send("Log.enable"),
    send("Network.enable"),
  ]);
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1050,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Page.navigate", { url: baseURL });
  await waitFor(
    `document.querySelector('h1')?.textContent==='Your work, moving forward.'`,
  );
  console.log(
    "Overview",
    await evaluate(
      `({title:document.title,metrics:[...document.querySelectorAll('.metric-value')].map(e=>e.textContent),horizontalOverflow:document.documentElement.scrollWidth>innerWidth})`,
    ),
  );
  await screenshot("overview-desktop");
  for (const name of ["jobs", "repos", "watches", "settings", "help"]) {
    await evaluate(`location.hash=${JSON.stringify(name)}`);
    await waitFor(
      `document.querySelector('#breadcrumb strong')?.textContent===${JSON.stringify({ jobs: "Tasks", repos: "Repositories", watches: "Review watches", settings: "Settings", help: "Help & getting started" }[name])}`,
    );
    await delay(100);
    console.log(
      "View",
      name,
      await evaluate(
        `({heading:document.querySelector('h1')?.textContent,overflow:document.documentElement.scrollWidth>innerWidth,buttons:document.querySelectorAll('main button').length})`,
      ),
    );
    if (["repos", "settings", "help"].includes(name))
      await screenshot(name + "-desktop");
  }
  await click('[data-action="diagnostics"]');
  await waitFor(`document.querySelectorAll('.diagnostic-row').length>0`);
  console.log(
    "Diagnostics",
    await evaluate(
      `[...document.querySelectorAll('.diagnostic-row')].map(e=>({status:e.className,text:e.textContent}))`,
    ),
  );
  await screenshot("diagnostics-desktop");
  await click("#dialog-close");
  await evaluate(`location.hash='settings'`);
  await waitFor(`document.querySelector('#command-user-lookup')`);
  await fill("#command-user-lookup", installedState.github.login);
  await click("#command-user-add");
  await waitFor(
    `document.querySelector('#command-user-feedback').textContent.includes(${JSON.stringify(installedState.github.login)})`,
  );
  console.log(
    "Username lookup",
    await evaluate(
      `({feedback:document.querySelector('#command-user-feedback').textContent,chips:document.querySelector('#command-user-list').textContent})`,
    ),
  );
  await click('[data-action="settings-reload"]');
  await click("#confirm-action");
  await waitFor(`!document.querySelector('#app-dialog').open`);
  if (scenario && ownAccount) {
    await click('.topbar [data-action="new-task"]');
    for (let i = 0; i < 10; i++) {
      await send("Input.dispatchKeyEvent", {
        type: "keyDown",
        key: "Tab",
        code: "Tab",
        windowsVirtualKeyCode: 9,
      });
      await send("Input.dispatchKeyEvent", {
        type: "keyUp",
        key: "Tab",
        code: "Tab",
        windowsVirtualKeyCode: 9,
      });
      if (
        !(await evaluate(
          `document.querySelector('#app-dialog').contains(document.activeElement)`,
        ))
      )
        throw Error("Native task dialog lost keyboard focus");
    }
    console.log(
      "Native dialog focus",
      "10 Tab presses remained within the dialog",
    );
    await fill("#task-repo", scenario.repo, "change");
    await click('input[value="implement-issue"]');
    await click('[data-action="wizard-next"]');
    await waitFor(
      `document.querySelector('#task-target')?.options.length>1 || document.querySelector('.dialog-error')`,
    );
    console.log(
      "Targets",
      await evaluate(
        `({options:[...document.querySelector('#task-target').options].map(e=>e.textContent),error:document.querySelector('.dialog-error')?.textContent})`,
      ),
    );
    const targetValue = await evaluate(
      `document.querySelector('#task-target').options[1]?.value`,
    );
    if (targetValue) {
      await fill("#task-target", targetValue, "change");
      await waitFor(
        `document.querySelector('.selection-summary strong')?.textContent.includes('#') || [...document.querySelectorAll('.selection-summary strong')].some(e=>e.textContent.includes('#'))`,
      );
      await click('[data-action="wizard-next"]');
      await waitFor(`document.querySelector('#task-account')`);
      await fill("#task-account", ownAccount.account, "change");
      await click("#task-browse");
      await waitFor(`document.querySelector('#folder-create-form')`);
      console.log(
        "Folder browser",
        await evaluate(
          `({location:document.querySelector('.folder-location code').textContent,folders:document.querySelectorAll('#folder-body .folder-row').length,writable:!document.querySelector('#folder-select').disabled})`,
        ),
      );
      assertHome: {
        const disabled = await evaluate(
          `document.querySelector('#folder-select').disabled`,
        );
        if (!disabled) throw new Error("Home folder must not be selectable");
        console.log("Account root correctly requires a project subfolder");
      }
      const folderOptions = await (
        await fetch(
          new URL(
            "/api/folders?" +
              new URLSearchParams({
                account: ownAccount.account,
                path: ownAccount.path,
              }),
            baseURL,
          ),
        )
      ).json();
      let chosenFolder = null;
      for (const entry of [...folderOptions.items].sort(
        (a, b) =>
          Number(a.name !== "Documents") - Number(b.name !== "Documents"),
      )) {
        const value = await (
          await fetch(
            new URL(
              "/api/folders?" +
                new URLSearchParams({
                  account: ownAccount.account,
                  path: entry.path,
                }),
              baseURL,
            ),
          )
        ).json();
        if (value.writable) {
          chosenFolder = entry;
          break;
        }
      }
      if (!chosenFolder)
        throw Error(
          "No existing writable project folder for the current account",
        );
      await evaluate(
        `(()=>{const row=[...document.querySelectorAll('#folder-body .folder-row')].find(e=>e.textContent.includes(${JSON.stringify(chosenFolder.name)}));if(!row)throw Error('Selected folder unavailable');row.click()})()`,
      );
      await waitFor(
        `document.querySelector('.folder-location code')?.textContent===${JSON.stringify(chosenFolder.path)}`,
      );
      await screenshot("folder-desktop");
      await click("#folder-select");
      await click('[data-action="wizard-next"]');
      await waitFor(`document.querySelector('#queue-task')`);
      console.log(
        "Task review",
        await evaluate(
          `({source:!!document.querySelector('.source-context'),checks:document.querySelector('.review-list').textContent,automaticWatch:document.querySelector('.effects').textContent.includes('watched automatically'),queueDisabled:document.querySelector('#queue-task').disabled})`,
        ),
      );
      await screenshot("task-review-desktop");
      await evaluate(
        `document.querySelector('#app-dialog').scrollTop=document.querySelector('#app-dialog').scrollHeight`,
      );
      await screenshot("task-review-confirmation-desktop");
    }
    await click("#dialog-close");
  } else
    console.log("SKIP Task review: no open issue or own account is available.");
  await evaluate(`location.hash='repos'`);
  await delay(200);
  await click('[data-action="new-repo"]');
  await fill("#repo-owner", installedState.repos[0].owner);
  await fill("#repo-name", installedState.repos[0].name);
  await click("#load-branches");
  await waitFor(
    `document.querySelector('#repo-branch').options.length>1 || document.querySelector('.dialog-error')`,
  );
  console.log(
    "Branches",
    await evaluate(
      `[...document.querySelector('#repo-branch').options].map(e=>e.textContent)`,
    ),
  );
  await screenshot("repository-dialog-desktop");
  await click("#dialog-close");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  for (const name of [
    "overview",
    "jobs",
    "repos",
    "watches",
    "settings",
    "help",
  ]) {
    await evaluate(`location.hash=${JSON.stringify(name)}`);
    await delay(250);
    console.log(
      "Mobile",
      name,
      await evaluate(
        `({overflow:document.documentElement.scrollWidth>innerWidth,width:innerWidth,scrollWidth:document.documentElement.scrollWidth})`,
      ),
    );
    await screenshot(name + "-mobile");
    if (["jobs", "watches"].includes(name))
      console.log(
        "OverflowRects",
        name,
        await evaluate(
          `({viewport:visualViewport.width,items:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>400&&e.getBoundingClientRect().width>400).map(e=>({tag:e.tagName,cls:e.className,w:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right,overflow:getComputedStyle(e).overflowX})).slice(0,20)})`,
        ),
      );
  }
  await click("#mobile-menu");
  await delay(300);
  console.log(
    "Mobile navigation",
    await evaluate(
      `({expanded:document.querySelector('#mobile-menu').getAttribute('aria-expanded'),sidebarVisible:document.querySelector('#sidebar').getBoundingClientRect().left===0})`,
    ),
  );
  await screenshot("navigation-mobile");
  for (let i = 0; i < 12; i++) {
    await send("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    if (
      !(await evaluate(
        `document.querySelector('#sidebar').contains(document.activeElement)||document.activeElement.id==='mobile-menu'`,
      ))
    )
      throw Error("Mobile navigation lost keyboard focus");
  }
  console.log(
    "Mobile drawer focus",
    "12 Tab presses remained within navigation",
  );
  await click("#nav-scrim");
  if (!(await evaluate(`document.querySelector('#sidebar').inert`)))
    throw Error("Hidden mobile navigation is still keyboard reachable");
  console.log("BrowserExceptions", JSON.stringify(errors));
  console.log("FailedResources", JSON.stringify(failed));
  await writeFile(
    join(outputDir, "browser-report.json"),
    JSON.stringify({ errors, failed }, null, 2),
  );
  if (errors.length || failed.length)
    throw new Error(
      `Browser verification failed: ${errors.length} exceptions/log errors; ${failed.length} failed resources. Inspect browser-report.json.`,
    );
} finally {
  await send("Browser.close").catch(() => {});
  ws.close();
  chrome.kill();
  await rm(profile, { recursive: true, force: true, maxRetries: 2 });
}
