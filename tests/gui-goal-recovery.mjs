// Goal completion regressions. Every API request is intercepted; only static app assets reach the server.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-goal-recovery.mjs http://worker-address:18465 /home/you/gui-fixtures
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
    "Usage: node tests/gui-goal-recovery.mjs <workspace-url> <output-folder>",
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
const click = async (selector) => {
  await waitFor(`Boolean(document.querySelector(${JSON.stringify(selector)}))`);
  return evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
};
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

const assertions=[],failures=[],requests=[],submissions=[],unexpected=[];
let failResume=false,delayNextState=false;
const repo='example/project', fleetId='fleet-goal-recovery';
const fleet={id:fleetId,status:'stopped',account:'example',output_dir:'/home/example/results',member_home:'/home/example',scope:'selected',repos:[repo],workers:1,deepseek_workers:0,dry_run:false,calls_used:2,max_calls_total:20};
const originalResult={summary:'Keep the original result',checks:[{command:'npm test',passed:false,exit_code:1,output_tail:'Original failed-check output'}]};
const item={id:'failed-check-item',fleet_id:fleetId,fleet_status:'stopped',repo,number:7,kind:'review_pr',title:'A saved check failure',status:'blocked',error:'Project verification failed.',result:structuredClone(originalResult),history:[{event:'earlier-attempt'}],recovery:{kind:'checks',title:'The project check needs attention',explanation:'Read the command output.',next_step:'Save the correct command, then resume the stopped fleet when ready.',actions:['configure_checks','resume','archive'],needs_checks:true}};
const model={model_id:'glm-5.3-flash',auth_mode:'env',base_url:'http://127.0.0.1:9999',broker_base_url:'http://127.0.0.1:9999',broker_consumer_id:'fixture',broker_server_id:'fixture',max_calls_per_job:60,max_calls_per_day:600,max_tokens:8192,lease_ttl_seconds:900,request_timeout_seconds:180};
const state={version:'goal-fixture',config_revision:'1',service:{active:true,state:'active',polling_enabled:true,poll_interval_seconds:120},github:{configured:true,login:'example',id:42},accounts:[{account:'example',label:'Example member',path:'/home/example'}],repos:[{full_name:repo,owner:'example',name:'project',base_branch:'main',checks:['npm test']}],model,jobs:[],watches:[],summary:{jobs_open:0,jobs_done_today:0,model_calls_today:2},settings:{github_host:'github.com',rest_api_version:'2022-11-28',poll_interval_seconds:120,command_prefix:'/agent',allowed_command_users:[42],state_dir:'/home/example/state',worktree_root:'/home/example/work',max_tool_iterations:40,run_command_timeout_seconds:600,run_command_output_cap:100000,poll_repos:true,model}};
function projection(){state.fleet_summary={fleets:[fleet],repositories:[],repository_count:0,configured_agents:{glm:0,deepseek:0},active_agents:{glm:0,deepseek:0},task_counts:{blocked:1},unresolved_count:1,recent_tasks:[item],usage:{glm:0,deepseek:0}};}
function fleetData(){return {fleets:[fleet],items:[item],workers:[],ready:[],comments:[],providers:[{id:'glm',model_id:'glm-5.3-flash',available:true},{id:'deepseek',available:true}],usage:{glm:{calls_today:2},deepseek:{calls_today:0}},supervisor:{status:'running',active:true},events:[],rate_limits:{}};}
projection();
function route(p){
 const u=new URL(p.request.url),path=u.pathname,method=p.request.method;
 if(!path.startsWith('/api/'))return null;
 let body={};try{body=JSON.parse(p.request.postData||'{}');}catch{}
 requests.push({method,path,query:u.search});if(method==='POST')submissions.push({path,body:structuredClone(body)});
 let data={},status=200;
 if(path==='/api/state')data=state;
 else if(path==='/api/task-types')data={items:[]};
 else if(path==='/api/fleet'&&method==='GET')data=fleetData();
 else if(path==='/api/fleet'&&method==='POST'){
  if(body.action==='resume'&&failResume){status=503;data={error:'The saved fleet could not resume. Its previous state and work are kept.'};return {status,data};}
  if(body.action==='resume'){fleet.status='running';item.fleet_status='running';}
  else if(body.action==='configure_checks'){state.repos[0].checks=body.checks;item.history.push({event:'checks-saved',checks:body.checks});}
  else if(body.action!=='refresh'){unexpected.push({method,path,body});status=400;}
  projection();data={...fleetData(),hint:'Saved in simulated data only.'};
 }
 else if(path==='/api/fleet/item')data={item,fleet};
 else if(path==='/api/digest')data={done:[],waiting_on_you:{events:[],drafts:[]},watched:[]};
 else if(path==='/api/explore/catalog')data={tools:[],shortcuts:[],clients:[]};
 else if(path==='/api/learn/sessions')data={items:[]};
 else if(path==='/api/explore'){
  const ref=u.searchParams.get('ref')||'main',view=u.searchParams.get('view')||'tree',path=u.searchParams.get('path')||'';
  if(ref==='bad-version'){status=404;data={error:'That tag or commit could not be found. Check its spelling and choose another version.'};}
  else data={repository:{full_name:repo,default_branch:'main',ref,sha:(ref==='v1.0'?'b':'a').repeat(40),private:false},branches:[{name:'main'},{name:'feature'}],items:[{name:'src',path:'src',type:'dir'},{name:'README.md',path:'README.md',type:'file'}],file:view==='file'?{path,text:'Original working file context at '+ref,binary:false}:null,receipts:[{command:'gh api project',explanation:'Read project files only.'}],links:{github:'https://github.com/'+repo},history:[]};
 }
 else {unexpected.push({method,path});status=503;data={error:'Missing isolated test route '+path};}
 return {status,data};
}
const check=async(label,expression)=>{if(!await evaluate(expression))throw new Error(label);assertions.push(label);console.log('PASS',label);};
const countResume=()=>submissions.filter(s=>s.body.action==='resume').length;
const navigate=async hash=>{await evaluate(`location.hash=${JSON.stringify(hash)}`);await delay(150);};
const close=async()=>{if(await evaluate('document.querySelector("#app-dialog").open'))await click('#dialog-close');};
async function scenario(name,run){console.log('SCENARIO',name);try{await close();await run();}catch(error){failures.push({name,error:error.message});await screenshot('failure-'+name);}}

try{
 await Promise.all([send('Runtime.enable'),send('Page.enable'),send('Network.enable')]);
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.method!=='Fetch.requestPaused')return;const p=m.params,r=route(p);if(r){const reply={requestId:p.requestId,responseCode:r.status,responseHeaders:[{name:'Content-Type',value:'application/json'}],body:Buffer.from(JSON.stringify(r.data)).toString('base64')};if(delayNextState&&new URL(p.request.url).pathname==='/api/state'){delayNextState=false;setTimeout(()=>send('Fetch.fulfillRequest',reply),850);}else send('Fetch.fulfillRequest',reply);}else if(!['GET','HEAD'].includes(p.request.method)){unexpected.push({method:p.request.method,url:p.request.url});send('Fetch.failRequest',{requestId:p.requestId,errorReason:'BlockedByClient'});}else send('Fetch.continueRequest',{requestId:p.requestId});});
 await send('Fetch.enable',{patterns:[{urlPattern:'*'}]});await send('Emulation.setDeviceMetricsOverride',{width:1400,height:1050,deviceScaleFactor:1,mobile:false});await send('Page.navigate',{url:baseURL});await waitFor('!!document.querySelector("h1")');
 await scenario('settings-stopped-fleet',async()=>{
  await navigate('settings');await waitFor('document.body.textContent.includes("Account-wide fleet controls")');
  await check('Stopped fleet in Settings offers Resume and omits Pause and Stop',`!!document.querySelector('main [data-action="recovery-review-resume"]')&&!document.querySelector('main [data-action="fleet-pause"]')&&!document.querySelector('main [data-action="fleet-stop"]')`);
  const before=countResume();await click('main [data-action="recovery-review-resume"]');
  await waitFor('!!document.querySelector("#confirm-action")');
  await check('Resume names the saved scope and possible model requests and GitHub changes before submission',`document.querySelector('#dialog-body').textContent.includes(${JSON.stringify(repo)})&&/example/.test(document.querySelector('#dialog-body').textContent)&&/model/i.test(document.querySelector('#dialog-body').textContent)&&/GitHub|publish/i.test(document.querySelector('#dialog-body').textContent)`);
  if(countResume()!==before)throw new Error('Resume sent a request before confirmation');assertions.push('Resume sends no request before a person confirms');
  await close();await click('main [data-action="recovery-review-resume"]');await click('#confirm-action');await waitFor('!document.querySelector("#app-dialog").open');
  if(countResume()!==before+1||fleet.status!=='running')throw new Error('Confirmed Settings Resume did not resume the same saved fleet');assertions.push('Confirmed Settings Resume resumes the saved fleet once');await screenshot('01-settings-resumed');
 });
 await scenario('stopped-check-command',async()=>{
  fleet.status='stopped';item.fleet_status='stopped';projection();await navigate('overview');await click('#refresh-button');await navigate('fleet');await waitFor('!!document.querySelector("[data-fleet-item=failed-check-item]")');await click('[data-action="fleet-refresh"]');await waitFor('document.querySelector("#fleet-controls").textContent.includes("Resume fleet")');
  await click('[data-fleet-item="failed-check-item"] [data-action="fleet-configure-checks"]');
  await check('Stopped-fleet check editor explains that saving will not start the fleet',`/stopped|paused/i.test(document.querySelector('#dialog-body').textContent)&&!/resume automatically/i.test(document.querySelector('#dialog-body').textContent)`);
  await check('Stopped-fleet check editor labels its button as a save without continue',`!/continue/i.test(document.querySelector('[form="fleet-checks-form"]').textContent)`);
  const before=countResume();await fill('#fleet-checks-command','npm run verify');await click('[form="fleet-checks-form"]');
  await waitFor('!document.querySelector("#fleet-checks-form")');
  if(countResume()!==before||fleet.status!=='stopped')throw new Error('Saving checks silently resumed stopped work');assertions.push('Saving verification command leaves the fleet stopped');
  await waitFor('!!document.querySelector("#app-dialog[open] [data-action=recovery-review-resume]")');
  await click('#app-dialog [data-action="recovery-review-resume"]');await waitFor('!!document.querySelector("#confirm-action")');
  if(countResume()!==before)throw new Error('Post-save Resume bypassed confirmation');
  await click('#confirm-action');await waitFor('!document.querySelector("#app-dialog").open');
  if(countResume()!==before+1||JSON.stringify(item.result)!==JSON.stringify(originalResult))throw new Error('Explicit post-save Resume failed or original evidence changed');assertions.push('Post-save Resume requires confirmation and keeps earlier failed-check evidence');await screenshot('02-checks-resumed');
 });
 await scenario('paused-resume-failure-and-budget',async()=>{
  fleet.status='paused';item.fleet_status='paused';projection();await navigate('settings');await send('Page.reload',{ignoreCache:true});
  await waitFor('!!document.querySelector("main [data-action=recovery-review-resume]")');
  await check('Paused Settings fleet offers Resume and Stop with no Pause',`!!document.querySelector('main [data-action="fleet-stop"]')&&!document.querySelector('main [data-action="fleet-pause"]')`);
  failResume=true;const before=countResume();await click('main [data-action="recovery-review-resume"]');await click('#confirm-action');
  await waitFor('document.querySelector("#app-dialog .dialog-error")?.textContent.includes("could not resume")');
  await delay(300);
  if(countResume()!==before+1||fleet.status!=='paused')throw new Error('Failed resume retried automatically or lost paused state');assertions.push('Failed resume keeps an inline error and sends no automatic retry');
  failResume=false;await click('#confirm-action');await waitFor('!document.querySelector("#app-dialog").open');
  if(countResume()!==before+2||fleet.status!=='running')throw new Error('Person could not retry the failed resume once');assertions.push('Person can explicitly retry failed resume without leaving its confirmation');
  fleet.status='paused';item.fleet_status='paused';fleet.calls_used=20;projection();await send('Page.reload',{ignoreCache:true});
  await waitFor('!!document.querySelector("main [data-action=recovery-budget]")');
  await check('Exhausted Settings fleet offers an explicit budget decision',`!document.querySelector('main [data-action="recovery-review-resume"]')&&document.querySelector('main [data-action="recovery-budget"]').textContent.includes('additional call budget')`);
  const beforeBudget=countResume();await click('main [data-action="recovery-budget"]');await waitFor('!!document.querySelector("#budget-form")');
  await check('Budget dialog explains extra allowance and asks before resuming',`/used/i.test(document.querySelector('#dialog-body').textContent)&&/Additional calls/i.test(document.querySelector('#dialog-body').textContent)&&/Authorize.*resume/i.test(document.querySelector('#dialog-footer').textContent)`);
  if(countResume()!==beforeBudget)throw new Error('Opening budget dialog resumed work');assertions.push('Opening exhausted-budget recovery sends no Resume request');
  await close();fleet.calls_used=2;projection();
 });
 await scenario('fresh-decision-and-cancelled-read',async()=>{
  fleet.status='paused';item.fleet_status='paused';fleet.calls_used=2;fleet.max_calls_total=20;fleet.repos=[repo];projection();
  await navigate('fleet');await send('Page.reload',{ignoreCache:true});
  await waitFor('document.querySelector("#fleet-controls")?.textContent.includes("Resume fleet")');
  // The browser now has an old fleet snapshot. Only the fake backend advances.
  fleet.repos=['example/latest-project-selection'];fleet.calls_used=17;projection();
  await navigate('settings');
  const before=submissions.length;
  await click('main [data-action="recovery-review-resume"]');await waitFor('!!document.querySelector("#confirm-action")');
  await check('Resume decision reads the latest saved project selection instead of cached fleet data',`document.querySelector('#dialog-body').textContent.includes('example/latest-project-selection')&&!document.querySelector('#dialog-body').textContent.includes('example/project')`);
  await close();
  if(submissions.length!==before)throw new Error('Cancelling fresh resume decision sent a mutation');assertions.push('Cancelling a fresh resume decision sends no mutation');
  fleet.calls_used=35;fleet.max_calls_total=35;projection();await click('#refresh-button');
  await waitFor('!!document.querySelector("main [data-action=recovery-budget]")');
  await click('main [data-action="recovery-budget"]');await waitFor('!!document.querySelector("#budget-form")');
  await check('Budget decision uses latest usage and allowance after an older fleet view',`/35 of 35/.test(document.querySelector('#dialog-body').textContent)&&!/2 of 20/.test(document.querySelector('#dialog-body').textContent)`);
  await screenshot('05-fresh-budget-decision');await close();
  if(submissions.length!==before)throw new Error('Cancelling fresh budget decision sent a mutation');assertions.push('Cancelling a fresh budget decision sends no mutation');
  delayNextState=true;await click('main [data-action="recovery-budget"]');
  await waitFor('document.querySelector("#dialog-title").textContent.includes("Read saved fleet settings")');
  await close();await click('[data-action="store-deepseek"]');await waitFor('!!document.querySelector("#credential-form")');
  await fill('#credential-supplier','Unsubmitted form kept after cancelled read');await delay(1100);
  await check('Cancelled delayed fleet read cannot replace a newer dialog or discard its entered text',`!!document.querySelector('#credential-form')&&document.querySelector('#credential-supplier').value==='Unsubmitted form kept after cancelled read'&&!document.querySelector('#budget-form')`);
  if(submissions.length!==before)throw new Error('Cancelled delayed decision sent a mutation');assertions.push('Cancelled delayed read sends no mutation');
  await close();fleet.repos=[repo];fleet.calls_used=2;fleet.max_calls_total=20;projection();
 });
 await scenario('explorer-version-recovery',async()=>{
  await navigate('learn');await fill('#explore-repo',repo,'change');await click('#explore-form button[type="submit"]');await waitFor('!!document.querySelector("[data-action=explore-file]")');
  await click('[data-action="explore-file"][data-index="1"]');await waitFor('!!document.querySelector(".explore-source")');
  await evaluate('document.querySelector(".explore-version-tools").open=true');await fill('#explore-version','bad-version');await click('[data-action="explore-version"]');
  await waitFor('document.querySelector("#explore-panel").textContent.includes("could not be found")');
  await check('Invalid version preserves the loaded project and working file context',`document.querySelector('#explore-repo').value===${JSON.stringify(repo)}&&document.querySelector('.explore-source')?.textContent.includes('Original working file context at main')&&!document.querySelector('#explore-panel').textContent.includes('Get to know a project')`);
  await check('Invalid version remains editable and shows the previous working branch',`document.querySelector('#explore-version')?.value==='bad-version'&&document.querySelector('#explore-ref')?.value==='main'&&document.querySelector('.explore-version-tools').open`);
  await check('Invalid version offers both retry and return choices',`/Retry this request/i.test(document.querySelector('#explore-panel').textContent)&&/Return to previous version/i.test(document.querySelector('#explore-panel').textContent)`);
  const badReads=requests.filter(r=>r.path==='/api/explore'&&r.query.includes('bad-version')).length;
  await click('[data-action="explore-retry"]');await waitFor('!!document.querySelector("#explore-recovery")');
  if(requests.filter(r=>r.path==='/api/explore'&&r.query.includes('bad-version')).length!==badReads+1)throw new Error('Retry did not repeat the named failed read exactly once');
  await check('Retry repeats only the failed read and keeps working context on another failure',`document.querySelector('#explore-version').value==='bad-version'&&document.querySelector('.explore-source')?.textContent.includes('Original working file context at main')`);
  await screenshot('03-invalid-version-keeps-context');
  await fill('#explore-version','v1.0');await click('[data-action="explore-version"]');await waitFor('document.querySelector("#explore-ref")?.value==="v1.0"');
  await check('Corrected version opens successfully without choosing the project again',`document.querySelector('#explore-repo').value===${JSON.stringify(repo)}&&!document.querySelector('#explore-panel').textContent.includes('could not be found')`);
  await evaluate('document.querySelector(".explore-version-tools").open=true');await fill('#explore-version','bad-version');await click('[data-action="explore-version"]');await waitFor('document.querySelector("#explore-panel").textContent.includes("could not be found")');
  const readsBeforeReturn=requests.filter(r=>r.path==='/api/explore').length;
  await evaluate(`[...document.querySelectorAll('#explore-panel button')].find(el=>/Return to previous version/i.test(el.textContent)).click()`);
  await waitFor('!document.querySelector("#explore-panel").textContent.includes("could not be found")');
  await check('Return to previous version restores the last successful version',`document.querySelector('#explore-ref').value==='v1.0'&&document.querySelector('#explore-repo').value===${JSON.stringify(repo)}`);
  if(requests.filter(r=>r.path==='/api/explore').length!==readsBeforeReturn)throw new Error('Return unexpectedly sent a request');assertions.push('Return uses the saved view without another network request');
  await screenshot('04-returned-working-version');
 });
 if(errors.length)failures.push({name:'javascript',errors});if(unexpected.length)failures.push({name:'unexpected-requests',unexpected});
}catch(error){failures.push({name:'harness',error:error.stack});}
finally{
 await writeFile(join(outputDir,'goal-recovery-results.json'),JSON.stringify({passed:!failures.length,assertions,failures,requests,submissions,errors,unexpected},null,2));
 ws.close();chrome.kill('SIGTERM');await delay(200);await rm(profile,{recursive:true,force:true});
}
console.log(JSON.stringify({assertions:assertions.length,failures},null,2));if(failures.length)process.exitCode=1;
