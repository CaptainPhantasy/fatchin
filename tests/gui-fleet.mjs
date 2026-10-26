// Isolated agent fleet interface regressions. CDP fulfills EVERY /api/* request; no live writes.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-fleet.mjs http://worker-address:18465 /home/you/gui-fixtures
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
    "Usage: node tests/gui-fleet.mjs <workspace-url> <output-folder>",
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
  await evaluate("document.querySelectorAll('#toast-region button').forEach((button)=>button.click());document.activeElement?.blur();window.scrollTo(0,0)");
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
// EVERY API route below is fulfilled locally; provider and GitHub mutations never leave this browser.
const fixture = {
  version:"fleet-ui-fixture",config_revision:"r1",service:{active:true,polling_enabled:true,restart_required:false},
  github:{configured:true,login:"example",id:123},accounts:[{account:"example",path:"/home/example"}],
  repos:[{full_name:"example/alpha",owner:"example",name:"alpha",base_branch:"main",checks:["python -m unittest"]}],
  jobs:[],watches:[],summary:{},settings:{model:{model_id:"your-model"}},
};
let snapshot = {version:1,max_workers:20,max_workers_per_provider:10,github:fixture.github,
  providers:[{id:"glm",label:"GLM",model_id:"your-model",available:true},{id:"deepseek",label:"DeepSeek",model_id:"DeepSeekv4.1flash1017",available:true}],
  fleets:[],workers:[],items:[],ready:[],comments:[],events:[],supervisor:{active:true},usage:{glm:{calls_today:4},deepseek:{calls_today:3}}};
const reads=[],writes=[];
let outage=false,retryFailure=false,slowRefresh=false;
let coverageCatalog=null;
async function route(request) {
  const url=new URL(request.url), path=url.pathname;
  const data=request.postData?JSON.parse(request.postData):null;
  if(data) writes.push({path,data}); else reads.push(path+url.search);
  if(path==="/api/state"){
    if(coverageCatalog)fixture.fleet_summary={fleets:snapshot.fleets,repositories:coverageCatalog,repository_count:coverageCatalog.length,
      configured_agents:{glm:10,deepseek:10},active_agents:{glm:10,deepseek:10},task_counts:{running:20,queued:12000,blocked:1},
      recent_tasks:[{id:"cross-screen",repo:"example/project-255",kind:"review_code",title:"Cross-screen task",status:"running",provider:"deepseek",slot:11}],usage:{glm:7,deepseek:9}};
    return{status:200,result:fixture};
  }
  if(path==="/api/digest")return{status:200,result:{done:[],waiting_on_you:{events:[],drafts:[]},watched:(coverageCatalog||[]).map((r)=>({...r,mode:"all"}))}};
  if(path==="/api/fence")return{status:200,result:{repos:(coverageCatalog||[]).map((r)=>({...r,protection:"automatic",checks_configured:0,watches_armed:true,post_authority:"ask",rulesets_readable:"unknown"}))}};
  if(path==="/api/authority")return{status:200,result:{}};
  if(path==="/api/explore/catalog")return{status:200,result:{}};
  if(path==="/api/task-types")return{status:200,result:{items:[]}};
  if(path==="/api/folders")return{status:200,result:{path:"/home/example/fleet-reports",parent:"/home/example",writable:true,items:[]}};
  if(path==="/api/github/repositories")return{status:200,result:{items:[{full_name:"example/alpha",description:"The main product"},{full_name:"example/beta",description:"Shared services <script>globalThis.badRepo=true</script>"}]}};
  if(path==="/api/fleet") {
    if(!data){if(outage)return{status:503,result:{error:"Fleet connection unavailable. Try again."}};return{status:200,result:structuredClone(snapshot)};}
    if(data.action==="start"){
      snapshot.fleets=[{...data,id:"fleet-1",github_id:123,github_login:"example",status:"running",created_at:"2026-10-25T01:00:00Z"}];
    }else if(data.action==="pause")snapshot.fleets[0].status="paused";
    else if(data.action==="resume")snapshot.fleets[0].status="running";
    else if(data.action==="stop")snapshot.fleets[0].status="stopped";
    else if(data.action==="retry"&&retryFailure){retryFailure=false;return{status:503,result:{error:"The retry could not be confirmed. Refresh status before retrying."}};}
    if(data.action==="refresh"&&slowRefresh){slowRefresh=false;await delay(500);}
    return{status:200,result:structuredClone(snapshot)};
  }
  return{status:404,result:{error:"Unhandled fixture route "+path}};
}
const originalHandler=ws.onmessage;
ws.onmessage=(event)=>{
  const message=JSON.parse(event.data);
  if(message.method==="Fetch.requestPaused"){
    route(message.params.request).then(({status,result})=>send("Fetch.fulfillRequest",{requestId:message.params.requestId,responseCode:status,responseHeaders:[{name:"Content-Type",value:"application/json"}],body:Buffer.from(JSON.stringify(result)).toString("base64")})).catch((error)=>console.error("Fixture route failed",error));
  }else originalHandler(event);
};
const checks=[];
function assert(value,message){if(!value)throw new Error(message);checks.push(message);console.log("PASS",message);}
const present=(selector)=>waitFor(`!!document.querySelector(${JSON.stringify(selector)})`);
const submit=()=>evaluate("document.querySelector('#fleet-start-form').requestSubmit()");
const tab=(name)=>click('[data-action="fleet-tab"][data-tab="'+name+'"]');
const refreshFleet=async()=>{const before=writes.length;await click('[data-action="fleet-refresh"]');await waitFor("!document.querySelector('[data-action=\"fleet-refresh\"]').disabled");assert(writes.length===before+1&&writes.at(-1).data.action==="refresh","Explicit refresh requests one scan");};
function populate(){
  const common={fleet_id:"fleet-1",repo:"example/alpha",created_at:"2026-10-25T01:30:00Z",updated_at:"2026-10-25T02:00:00Z",head_sha:"a".repeat(40),base_sha:"b".repeat(40)};
  snapshot.items=[
    {...common,id:"fix-1",kind:"fix_comment",number:72,title:"Keep a comment fix within its requested files",url:"https://github.com/example/alpha/pull/72#discussion_r9",status:"running",author_slot:1,slot:1,source_comment_url:"https://github.com/example/alpha/pull/72#discussion_r9",result:{summary:"Applying the specific correction, then running the saved project checks.",fix_pr:{url:"https://github.com/example/alpha/pull/78"},review_url:"https://github.com/example/alpha/pull/78#pullrequestreview-7",reviewer_slot:11}},
    {...common,id:"review-1",kind:"review_pr",number:74,title:"Simplify project onboarding",url:"https://github.com/example/alpha/pull/74",status:"running",slot:11,result:{summary:"Reading the proposed changes and current checks."}},
    {...common,id:"held-1",repo:"example/beta",kind:"fix_comment",number:6,title:"Verify the backup restore path",url:"https://github.com/example/beta/pull/6",status:"blocked",error:"The project check failed.",result:{checks:[{command:"python3 -m unittest",passed:false,exit:1,stage:"dependencies",output_tail:"ModuleNotFoundError: No module named requests. <script>globalThis.badCheck=true</script>"}],next_action:"Install the documented dependency, then retry this item."}},
    {...common,id:"ready-1",kind:"review_pr",number:70,title:"Improve the folder selection experience",url:"https://github.com/example/alpha/pull/70",status:"ready",result:{summary:"Current changes reviewed. GitHub reports successful checks and merge conditions.",checks:[{command:"python3 -m unittest",passed:true,exit:0,output_tail:"OK"}]}},
  ];
  snapshot.ready=[snapshot.items[3]];
  snapshot.comments=[snapshot.items[0],snapshot.items[2]];
  snapshot.workers=[{slot:1,provider:"glm",fleet_id:"fleet-1",item_id:"fix-1",status:"running",stage:"Checking the correction",restarts:0},{slot:11,provider:"deepseek",fleet_id:"fleet-1",item_id:"review-1",status:"running",stage:"Reading pull request changes",restarts:1},{slot:3,fleet_id:"fleet-1",status:"recovering",stage:"Resetting after an interrupted task",restarts:1}];
  snapshot.events=[{at:"2026-10-25T02:00:00Z",fleet_id:"fleet-1",slot:3,message:"A fresh worker replaced an interrupted task. Its saved progress is being checked before retrying."}];
}
try {
  await Promise.all([send("Runtime.enable"),send("Page.enable"),send("Log.enable"),send("Network.enable")]);
  await send("Fetch.enable",{patterns:[{urlPattern:"*/api/*",requestStage:"Request"}]});
  await send("Emulation.setDeviceMetricsOverride",{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  await send("Page.navigate",{url:baseURL.replace(/\/$/,"")+"/#fleet"});
  await present("#fleet-start-form");await waitFor("document.querySelectorAll('.fleet-slot').length===20");
  assert(!writes.length&&!reads.some((r)=>r.startsWith('/api/github/repositories')),"Opening Fleet reads status without starting agents or searching repositories");
  assert(await evaluate("document.querySelector('#fleet-scope').value==='all'&&document.querySelector('#fleet-workers').value==='10'&&document.querySelector('#fleet-deepseek-workers').value==='10'"),"All repositories and both ten-agent pools are the defaults");
  assert(await evaluate("document.querySelectorAll('.fleet-provider-group').length===2&&document.querySelector('.fleet-identity').textContent.includes('Connected as example')"),"Twenty logical slots are grouped by provider with the actual connected GitHub identity");
  await fill("#fleet-account","example","change");await click('[data-action="fleet-browse"]');await present("#folder-select");await click("#folder-select");
  await evaluate("(()=>{const form=document.querySelector('#fleet-start-form');form.requestSubmit();form.requestSubmit()})()");
  await present('[data-action="fleet-pause"]');
  const start=writes.filter((r)=>r.data.action==="start");
  assert(start.length===1&&start[0].data.workers===10&&start[0].data.deepseek_workers===10&&start[0].data.scope==="all"&&start[0].data.repos.length===0&&start[0].data.output_dir==="/home/example/fleet-reports","One Start dispatches both pools across all projects with the chosen destination and no single-repository requirement");
  populate();await refreshFleet();
  assert(await evaluate("document.querySelectorAll('.fleet-slot').length===20&&document.querySelector('.fleet-provider-group').textContent.includes('Checking the correction')&&document.querySelectorAll('.fleet-provider-group')[1].textContent.includes('Reading pull request changes')&&document.querySelector('.fleet-slots').textContent.includes('automatic resets')"),"Live cards show current tasks and automatic recovery in the correct provider groups");
  await tab("comments");
  assert(await evaluate("!!document.querySelector('#fleet-panel a[href*=\"discussion_r9\"]')&&!!document.querySelector('#fleet-panel a[href$=\"/pull/78\"]')&&!!document.querySelector('#fleet-panel a[href*=\"pullrequestreview-7\"]')&&document.querySelector('#fleet-panel').textContent.includes('confirmed merged')"),"Open comments links source feedback, correction and independent review without declaring an unmerged fix closed");
  await click('[data-fleet-item="held-1"] .fleet-check-details > summary');
  assert(await evaluate("document.querySelector('[data-fleet-item=\"held-1\"] .fleet-check-details').open&&document.querySelector('[data-fleet-item=\"held-1\"]').textContent.includes('Failed · exit 1')&&document.querySelector('[data-fleet-item=\"held-1\"]').textContent.includes('ModuleNotFoundError')&&document.querySelector('[data-fleet-item=\"held-1\"]').textContent.includes('Install the documented dependency')&&!globalThis.badCheck"),"Real check results expose the failed command, exit status, exact safe output and a useful next step");
  await click('[data-action="fleet-configure-checks"]');await present("#fleet-checks-form");
  await fill("#fleet-checks-command","python -m unittest\nnpm test");
  await evaluate("document.querySelector('#fleet-checks-form').requestSubmit()");await waitFor("!document.querySelector('#app-dialog').open");
  assert(writes.at(-1).data.action==="configure_checks"&&writes.at(-1).data.repo==="example/beta"&&writes.at(-1).data.checks.length===2,"A blocked item can save trusted project checks inline and request automatic continuation");
  retryFailure=true;await click('[data-action="fleet-retry"]');await waitFor("document.querySelector('#fleet-monitor [role=alert]')?.textContent.includes('could not be confirmed')");
  assert(writes.filter((r)=>r.data.action==="retry").length===1,"An uncertain retry failure is shown without automatically repeating the action");
  await refreshFleet();await tab("ready");
  assert(await evaluate("document.querySelector('#fleet-panel a[href$=\"/pull/70\"]')?.target==='_blank'&&document.querySelector('#fleet-panel').textContent.includes('delete its branch')&&document.querySelector('#fleet-panel').textContent.includes('required approvals')"),"Ready to merge opens the exact GitHub item and explains checks, approvals, merging and branch deletion");
  await fill("#fleet-search","no matching item");assert(await evaluate("document.querySelector('#fleet-panel').textContent.includes('No matching merge candidates')"),"Window search filters merge candidates without losing the typed search");
  await fill("#fleet-search","");await fill("#fleet-repo-filter","example/beta","change");
  assert(await evaluate("!document.querySelector('#fleet-panel a[href$=\"/pull/70\"]')"),"Repository filtering narrows the window independently of the fleet scope");
  await fill("#fleet-repo-filter","all","change");
  snapshot.ready=[];snapshot.supervisor.scan_partial=true;snapshot.supervisor.scan_error="GitHub could not return all repository checks.";
  await refreshFleet();
  assert(await evaluate("!document.querySelector('#fleet-panel a[href$=\"/pull/70\"]')&&document.querySelector('#fleet-monitor').textContent.includes('Missing items are not treated as closed')"),"A partial scan withdraws stale merge readiness and explains what could not be verified");
  snapshot.supervisor={active:true};populate();await refreshFleet();
  outage=true;await delay(5400);await waitFor("document.querySelector('#fleet-monitor [role=alert]')?.textContent.includes('connection unavailable')");
  assert(await evaluate("document.querySelector('#fleet-panel').textContent.includes('Last recorded merge candidates')&&document.querySelector('#fleet-monitor').textContent.includes('merge readiness cannot be confirmed')"),"An outage retains last-known evidence with a visible stale-readiness warning");
  outage=false;await delay(5400);await waitFor("!document.querySelector('#fleet-monitor [role=alert]')");
  await fill("#fleet-search","folder");await evaluate("document.querySelector('#fleet-search').focus();document.querySelector('#fleet-search').setSelectionRange(2,2)");await delay(5400);
  assert(await evaluate("document.activeElement?.id==='fleet-search'&&document.activeElement.value==='folder'&&document.activeElement.selectionStart===2"),"Background refresh keeps search text, keyboard focus and caret position");
  await fill("#fleet-search","");
  snapshot.ready.push({...snapshot.ready[0],id:"unsafe",title:'<img src=x onerror="globalThis.badFleet=true">',url:"javascript:globalThis.badFleet=true",result:{summary:'<script>globalThis.badFleet=true</script>',fix_pr:{url:"https://github.com.evil.example/pull/2"}}});
  await refreshFleet();
  assert(await evaluate("!globalThis.badFleet&&!document.querySelector('#fleet-panel img')&&!document.querySelector('#fleet-panel a[href^=\"javascript:\"]')&&!document.querySelector('#fleet-panel a[href*=\"evil.example\"]')&&document.querySelector('#fleet-panel').textContent.includes('<script>')"),"Untrusted titles, comments and external links cannot execute markup or impersonate GitHub");
  populate();await refreshFleet();
  await click('[data-action="fleet-pause"]');await present('[data-action="fleet-resume"]');await click('[data-action="fleet-resume"]');await present('[data-action="fleet-pause"]');
  await click('[data-action="fleet-stop"]');await waitFor("!document.querySelector('#fleet-start').disabled");
  assert(writes.filter((r)=>["pause","resume","stop"].includes(r.data.action)).map((r)=>r.data.action).join(',')==="pause,resume,stop","Pause, Resume and Stop each send one explicit action for the selected fleet");
  await fill("#fleet-scope","selected","change");await fill("#fleet-repo-query","example");await click('[data-action="fleet-search-repos"]');await present('[data-fleet-repo="example/beta"]');
  await click('[data-fleet-repo="example/alpha"]');await click('[data-fleet-repo="example/beta"]');
  await fill("#fleet-workers","0","change");await fill("#fleet-deepseek-workers","0","change");
  const before=writes.length;await submit();
  assert(writes.length===before&&await evaluate("document.querySelector('#fleet-monitor').textContent.includes('Choose at least one agent')"),"Both pools at zero are refused before sending a dispatch");
  await fill("#fleet-deepseek-workers","10","change");await submit();await present('[data-action="fleet-pause"]');
  assert(writes.at(-1).data.action==="start"&&writes.at(-1).data.workers===0&&writes.at(-1).data.deepseek_workers===10&&writes.at(-1).data.repos.length===2&&!await evaluate("globalThis.badRepo"),"A DeepSeek-only fleet can select multiple projects, with search descriptions escaped");
  await click('[data-action="fleet-stop"]');await waitFor("!document.querySelector('#fleet-start').disabled");
  snapshot.providers[1].available=false;snapshot.providers[1].error="The configured DeepSeek endpoint is unavailable.";
  await refreshFleet();const beforeUnavailable=writes.length;await submit();
  assert(writes.length===beforeUnavailable&&await evaluate("document.querySelector('#fleet-monitor').textContent.includes('DeepSeek endpoint is unavailable')"),"An unavailable provider is explained and never silently replaced");
  snapshot.providers[1].available=true;delete snapshot.providers[1].error;await refreshFleet();
  await fill("#fleet-scope","all","change");await fill("#fleet-workers","10","change");await submit();await present('[data-action="fleet-pause"]');populate();await refreshFleet();await tab("agents");
  await screenshot("fleet-desktop");await tab("ready");await screenshot("fleet-ready-desktop");await tab("comments");await screenshot("fleet-comments-desktop");
  await send("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});await tab("agents");await delay(150);
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+1&&getComputedStyle(document.querySelector('.fleet-slot-footer')).fontSize==='13px'"),"Mobile Fleet fits the viewport and keeps secondary task text readable");
  await screenshot("fleet-mobile");await tab("ready");await screenshot("fleet-ready-mobile");
  await click("#fleet-tab-ready");await evaluate("document.querySelector('#fleet-tab-ready').focus()");await send("Input.dispatchKeyEvent",{type:"keyDown",key:"End",code:"End"});await send("Input.dispatchKeyEvent",{type:"keyUp",key:"End",code:"End"});
  assert(await evaluate("document.activeElement?.id==='fleet-tab-activity'&&document.querySelector('#fleet-panel').textContent.includes('fresh worker replaced')"),"Fleet windows support keyboard navigation and show automatic-recovery history");
  // A saved account-wide fleet must be visible beyond the new Fleet screen.
  coverageCatalog=Array.from({length:256},(_,i)=>({repo:`example/project-${String(i).padStart(3,"0")}`,status:"watching",tasks:{queued:5},account:"example",output_dir:"/home/example/fleet-reports"}));
  fixture.accounts.unshift({account:"another-member",path:"/home/another-member"});
  await send("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await send("Page.reload",{ignoreCache:true});await present("#fleet-account");await waitFor("document.querySelector('#fleet-account')?.value==='example'");
  assert(await evaluate("document.querySelector('#fleet-destination').value==='/home/example/fleet-reports'&&!!document.querySelector('[data-action=\"fleet-pause\"]')"),"Reopening restores the running fleet and its saved folder even when another member is listed first");
  const largeQueue=Array.from({length:12000},(_,i)=>({...snapshot.items[0],id:`large-${i}`,kind:"review_code",status:"queued",title:`Queued file ${i}`}));
  snapshot.items=largeQueue;await refreshFleet();await tab("agents");
  assert(await evaluate("document.querySelectorAll('#fleet-panel [data-fleet-item]').length===200&&document.querySelector('#fleet-panel').textContent.includes('12,000')"),"Large account queues render a bounded page while retaining the complete queue");
  await click('[data-action="fleet-show-more"]');
  assert(await evaluate("document.querySelectorAll('#fleet-panel [data-fleet-item]').length===400"),"Show more exposes further queued work without losing the full count");
  snapshot.items.push({...snapshot.items[0],id:"cross-screen",repo:"example/project-255",title:"Cross-screen task",status:"running",provider:"deepseek",slot:11});
  for(const view of ["overview","jobs","repos","watches","doorbell","settings","fence","learn","help"]){
    await evaluate(`location.hash=${JSON.stringify(view)}`);await present("#workspace-fleet-status");
    assert(await evaluate("document.querySelector('#workspace-fleet-status').textContent.includes('256 repositories')&&document.querySelector('#workspace-fleet-status').textContent.includes('DeepSeek')&&document.querySelector('#workspace-fleet-status').textContent.includes('GLM')"),`${view} shows the same full repository coverage and both live pools`);
    if(["repos","watches"].includes(view)){
      await present("#coverage-search");await fill("#coverage-search","project-255");
      assert(await evaluate("document.querySelector('.fleet-coverage').textContent.includes('1 of 256')&&document.querySelector('.coverage-scroll tbody').textContent.includes('project-255')"),`${view} searches all discovered repositories rather than the manual shortlist`);
      await fill("#coverage-search","");
    }
    if(view==="jobs"){
      assert(await evaluate("document.querySelector('#main').textContent.includes('Cross-screen task')&&document.querySelector('#main').textContent.includes('DeepSeek')"),"Tasks includes real fleet assignments alongside individual tasks");
      await click('[data-action="fleet-open-item"]');await waitFor("document.querySelector('#fleet-panel [data-fleet-item=\"cross-screen\"]')?.textContent.includes('Cross-screen task')");
      assert(await evaluate("document.querySelector('#fleet-search').value==='Cross-screen task'&&document.querySelector('#fleet-repo-filter').value==='example/project-255'"),"A task opens its matching fleet evidence and controls instead of a generic queue");
    }
    if(view==="settings"){
      assert(await evaluate("!!document.querySelector('[data-action=\"fleet-pause\"]')&&document.querySelector('#main').textContent.includes('Account-wide fleet controls')"),"Settings connects controls for both active agent pools");
      await click('[data-action="fleet-pause"]');await present('[data-action="fleet-resume"]');await click('[data-action="fleet-resume"]');await present('[data-action="fleet-pause"]');
      assert(writes.at(-1).data.action==='resume'&&writes.at(-2).data.action==='pause',"Settings pause and resume control the same saved fleet and refresh shared status");
    }
  }
  await evaluate("location.hash='watches'");await present("#coverage-search");await screenshot("watched-repositories-desktop");
  await send("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(150);
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+1"),"Full repository coverage and shared pool status fit the mobile viewport");
  await screenshot("watched-repositories-mobile");
  assert(errors.filter((e)=>e.source!=="network"||!/status of 503\b/.test(e.text||"")).length===0,"No unexpected browser errors occurred in fleet workflows");
  await writeFile(join(outputDir,"fleet-report.json"),JSON.stringify({status:"passed",checks,actions:writes.map((r)=>({path:r.path,action:r.data.action})),errors},null,2));
  console.log("Fleet UI checks passed:",checks.length);
}finally {
  await send("Browser.close").catch(()=>{});ws.close();chrome.kill();await rm(profile,{recursive:true,force:true,maxRetries:2});
}
