// Independent North Star recovery checks. Every /api/* request is fulfilled locally; no live API is reached.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-northstar.mjs http://worker-address:18465 /home/you/gui-fixtures
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
    "Usage: node tests/gui-northstar.mjs <workspace-url> <output-folder>",
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

const assertions = [], requests = [], submissions = [], unexpected = [];
const originalEvidence = {summary:'Original evidence stays available',checks:[{command:'npm test',passed:false,exit:1,output_tail:'The project check did not pass.'}],receipt:'kept-publication-123'};
const account='example',repo='example/project',fleetId='fleet-northstar';
const fleet={id:fleetId,account,status:'stopped',output_dir:'/home/example/results',member_home:'/home/example',scope:'selected',repos:[repo],workers:1,deepseek_workers:0,dry_run:true,calls_used:4,max_calls_total:10,last_scan_at:'2026-11-08T20:00:00Z'};
const rec=(kind,title,next_step,actions=[],can_retry=false,needs_checks=false)=>({kind,title,explanation:title,next_step,actions,can_retry,needs_checks});
const items=[
 {id:'blocked-item',title:'A saved stopped-fleet failure',status:'blocked',repo,number:7,kind:'review_pr',fleet_id:fleetId,fleet_status:'stopped',error:'The earlier request failed.',result:structuredClone(originalEvidence),history:[{event:'original-attempt',error:'Original error'}],recovery:rec('fleet_stopped','This work is saved; its fleet is stopped','Resume the fleet when you are ready, then retry this item.',['resume','archive','open_details'])},
 {id:'scope-item',title:'An excluded project',status:'blocked',repo:'example/outside',number:2,kind:'review_pr',fleet_id:fleetId,fleet_status:'stopped',error:'Outside the current writable scope.',result:{summary:'Saved scope hold'},history:[],recovery:rec('scope','This project is outside your selection','Review the selected projects and GitHub access, or archive this item.',['review_scope','check_access','archive'])},
 {id:'done-item',title:'Finished review with saved evidence',status:'done',repo,number:8,kind:'review_pr',fleet_id:fleetId,fleet_status:'stopped',error:'',result:{summary:'Finished independently verified review'},readiness:{ready:false,reason:'One approval is still required.'},history:[],recovery:rec('merge_waiting','The review finished; an approval is still required','Open GitHub to review the remaining approval.',['open_details','open_github','archive'])},
 {id:'done-failed-item',title:'A finished record whose check failed',status:'done',repo,number:9,kind:'review_pr',fleet_id:fleetId,fleet_status:'stopped',result:structuredClone(originalEvidence),history:[],recovery:rec('checks','The project check needs attention','Read the check output; resume the fleet before retrying.',['configure_checks','resume','archive'],false,true)},
 {id:'archived-item',title:'Earlier archived result',status:'blocked',repo,number:10,kind:'review_pr',fleet_id:fleetId,fleet_status:'stopped',archived_at:'2026-11-08T21:00:00Z',archive_reason:'The user handled it.',result:{summary:'Archived original evidence'},history:[],recovery:rec('archived','Saved in history','Restore it for review without starting a new attempt.',['restore','open_details'])}
];
const jobs=[
 {job_id:'waiting-job',kind:'triage-issues',repo_full_name:repo,number:0,state:'waiting-on-you',created_at:'2026-11-08T21:00:00Z',updated_at:'2026-11-08T21:00:00Z',model_calls_used:1,error:'',notes:{title:'A proposal for your decision',member:account,output_dir:'/home/example/results',drafts:[{name:'one',title:'One proposed issue',body:'Exact words that must be reviewed before posting.'}]}},
 {job_id:'uncertain-job',kind:'triage-issues',repo_full_name:repo,number:0,state:'waiting-on-you',created_at:'2026-11-08T20:00:00Z',updated_at:'2026-11-08T20:00:00Z',model_calls_used:1,error:'',notes:{title:'A decision with an uncertain earlier post',member:account,output_dir:'/home/example/results',posting:'one',phase:'posting-unconfirmed',drafts:[{name:'one',title:'Earlier possible post',body:'Preserve the original proposed words.'}]}}
];
const state={version:'northstar-fixture',config_revision:'revision-1',service:{active:true,state:'active',polling_enabled:true,poll_interval_seconds:120,restart_required:false},github:{configured:true,login:'example',id:42},accounts:[{account,label:'Example member',path:'/home/example'}],repos:[{full_name:repo,owner:'example',name:'project',base_branch:'main',checks:['npm test']}],jobs,watches:[{repo,mode:'all',number:null,scope:'all',health:'needs-folder',status_reason:'This watch needs a results folder before it can start.',next_step:'Choose Edit watch and select your member account and results folder.'}],summary:{jobs_open:2,jobs_done_today:0,model_calls_today:2},settings:{github_host:'github.com',rest_api_version:'2022-11-28',poll_interval_seconds:120,command_prefix:'/agent',allowed_command_users:[42],state_dir:'/home/example/state',worktree_root:'/home/example/work',max_tool_iterations:40,run_command_timeout_seconds:600,run_command_output_cap:100000,poll_repos:true,model:{base_url:'http://127.0.0.1:9999',model_id:'glm-5.3-flash',auth_mode:'env',max_calls_per_job:60,max_calls_per_day:600,request_timeout_seconds:180,max_tokens:8192,broker_base_url:'http://127.0.0.1:9999',broker_consumer_id:'fixture',broker_server_id:'fixture',lease_ttl_seconds:900}}};
state.model=state.settings.model;
state.providers=[{id:'deepseek',available:true,credential_source:'broker-lease',credential_source_label:'Company broker',next_step:'Restart the worker after changing the selected connection.'}];
jobs[0].notes.recovery_copy_skipped=['.private-key','linked-folder'];
jobs.push({job_id:'older-failure',kind:'triage-issues',repo_full_name:repo,number:0,state:'failed',notes:{title:'Earlier failed request',member:account,output_dir:'/home/example/results',request:{repo,kind:'triage-issues',account,output_dir:'/home/example/results',values:{words:'Keep this exact earlier request.'}}},error:'The earlier connection failed.'});
state.watches[0]={...state.watches[0],enabled:false,health:'disabled',member:account,member_home:'/home/example',output_dir:'/home/example/results'};
function refreshProjection(){
 state.fleet_summary={fleets:[fleet],repositories:[],repository_count:0,configured_agents:{glm:0,deepseek:0},active_agents:{glm:0,deepseek:0},task_counts:{blocked:items.filter(i=>!i.archived_at&&i.status==='blocked').length,done:2},unresolved_count:items.filter(i=>!i.archived_at&&i.status==='blocked').length,recent_tasks:items.filter(i=>!i.archived_at),usage:{glm:0,deepseek:0}};
}
refreshProjection();
const fleetData=()=>({fleets:[fleet],items,ready:[],comments:[],workers:[],providers:[{id:'glm',model_id:'glm-5.3-flash',available:true},{id:'deepseek',model_id:'deepseek-flash',available:true}],supervisor:{status:'running',active:true},rate_limits:{},events:[{at:'2026-11-08T21:00:00Z',fleet_id:fleetId,repo,item_id:'blocked-item',message:'Needle: check failed on this selected project.'},{at:'2026-11-08T21:01:00Z',fleet_id:fleetId,repo:'example/outside',item_id:'scope-item',message:'Other project activity.'}],usage:{glm:{calls_today:1},deepseek:{calls_today:0}},history_count:items.length});
const lesson={id:'lesson_northstar_saved',account,output_dir:'/home/example/results',repo,ref:'main',path:'',mode:'guide',lesson:'git-basics',status:'failed',revision:3,step_index:0,step:{id:'move-one',title:'A practice move',intent:'Learn what changed.',action_kind:'git',command:['git','status'],explanation:'This reads the practice project.'},receipts:[{at:'2026-11-08T20:00:00Z',command:'git status',exit_code:1,output:'The recorded practice command failed.'}],practice:{filename:'lesson.txt',content:'Saved practice text.'},error:'The practice command failed. Read the saved output before starting a new lesson.'};
let failFolders=true,failPostCheck=false,failDraftLoad=false,delayItem=false;
function respond(p){
 const url=new URL(p.request.url),path=url.pathname,method=p.request.method;
 if(!path.startsWith('/api/'))return null;
 requests.push({method,path,query:url.search});
 let data={},status=200,body={};
 try {body=JSON.parse(p.request.postData||'{}');}catch{}
 if(method==='POST')submissions.push({path,body:structuredClone(body)});
 if(path==='/api/state')data=state;
 else if(path==='/api/fleet'&&method==='GET')data=fleetData();
 else if(path==='/api/fleet'&&method==='POST'){
  const item=items.find(i=>i.id===body.item_id);
  if(body.action==='archive'&&item){item.archived_at=new Date().toISOString();item.archive_reason=body.reason;item.history.push({event:'archived'});item.recovery=rec('archived','Saved in history','Restore it for review without starting work.',['restore']);}
  else if(body.action==='restore'&&item){delete item.archived_at;item.restore_hold=true;item.history.push({event:'restored'});item.recovery=rec('fleet_stopped','Restored for review; no work started','Resume the fleet before retrying.',['resume','archive']);}
  else if(body.action==='resume'){fleet.status='running';items.forEach(i=>i.fleet_status='running');}
  else if(body.action==='refresh'){}
  else {status=400;data={error:'Unexpected simulated fleet action',hint:'This test never performs live work.'};}
  refreshProjection();if(status===200)data={...fleetData(),hint:'Your choice was saved; all earlier evidence is kept.'};
 }
 else if(path==='/api/fleet/item'){const item=items.find(i=>i.id===url.searchParams.get('id'));if(item)data={item,fleet};else{status=404;data={error:'The requested test item does not exist.'};}}
 else if(path==='/api/fleet/files')data={items:[],message:'Read the complete saved evidence with Download report.'};
 else if(path==='/api/digest')data={done:[],waiting_on_you:{events:items.filter(i=>!i.archived_at&&i.status==='blocked').map(i=>({fleet_item:true,item_id:i.id,repo:i.repo,text:i.recovery.title})),drafts:jobs.filter(j=>j.state==='waiting-on-you')},watched:[]};
 else if(path==='/api/task-types')data={items:[{id:'triage-issues',label:'Turn your words into issues',help:'Prepare issue proposals for your review.',takes:'repo',profile:'draft',fields:[{name:'words',label:'Your words',required:true}]},{id:'explain-repo',label:'Explain this repository',help:'Read a project and save its explanation.',takes:'repo',profile:'read-only',fields:[{name:'question',label:'Your question',required:false}]}]};
 else if(path==='/api/explore/catalog')data={tools:[],clients:[],shortcuts:[],verified:'2026-11-08'};
 else if(path==='/api/learn/sessions')data={items:[{id:lesson.id,account,repo,lesson:lesson.lesson,status:'failed',readable:true,next_step:'Open this saved lesson, read its output, and download the transcript.'}]};
 else if(path==='/api/learn'){if(body.action==='destination'){lesson.output_dir=body.output_dir;lesson.revision++;}data={session:lesson};}
 else if(path==='/api/folders'){
  if(failFolders){status=403;data={error:'The selected folder cannot be read.',hint:'Return to your home folder and choose another folder.',actions_available:['diagnostics']};}
  else data=url.searchParams.get('path')==='/home/example/results'?{path:'/home/example/results',parent:'/home/example',writable:true,items:[]}:{path:'/home/example',parent:null,writable:true,items:[{name:'results',path:'/home/example/results',is_dir:true}]};
 }
 else if(path==='/api/watches/edit'){Object.assign(state.watches[0],body,{health:body.enabled?'not-checked':'disabled'});data={ok:true};}
 else if(path==='/api/service'){state.service.active=true;state.service.restart_required=false;data={ok:true};}
 else if(path==='/api/github/branches')data={items:[{name:'main'}]};
 else if(path==='/api/repos'){const full_name=body.owner+'/'+body.name;state.repos.push({...body,full_name});state.service.restart_required=true;data={repo:full_name,restart_required:true};}
 else if(path==='/api/provider-source'){state.providers[0].credential_source=body.source;state.providers[0].credential_source_label=body.source==='env'?'Dedicated DeepSeek key':'Company broker';state.service.restart_required=true;data={ok:true,hint:'Connection choice saved. Restart the idle worker, then test.'};}
 else if(path==='/api/credentials')data={ok:true};
 else if(path==='/api/fence')data={repos:[{repo,configured:true,protection:'safe',watch_state:'Needs a results folder',watch_next_step:'Edit watch to choose its results folder.',watches_armed:false},{repo:'example/fleet-only',configured:false,protection:'automatic',watches_armed:false}]};
 else if(path==='/api/authority')data={modes:{},grants:[]};
 else if(path==='/api/jobs/files'){status=404;data={error:'The saved task files are unavailable.',hint:'Save existing results to another folder or download the report.',actions_available:['save-results','download-report']};}
 else if(path==='/api/watch-destinations')data={items:[{account,output_dir:'/home/example/results',source:'Saved for this project’s watch'}]};
 else if(path==='/api/diagnostics')data={checks:[{name:'Recorded recovery fixture',status:'warn',message:'No live setup or model request was made.'}]};
 else if(path==='/api/github/user')data={login:'example',id:42};
 else if(path.startsWith('/api/jobs/')){
  const parts=path.split('/'),job=jobs.find(j=>j.job_id===parts[3]);
  if(!job){status=404;data={error:'Test job not found.'};}
  else if(parts[4]==='draft-action'){
   if(body.action==='post'){job.notes.posted=[{name:body.name}];job.state='done';data={job,hint:'The selected draft was posted in simulated data only.'};}
   else if(body.action==='discard'){job.state='stopped';data={job,hint:'The proposal was discarded in simulated data only.'};}
   else{data={job};}
  }else if(parts[4]==='recovery'&&failPostCheck){status=503;data={error:'The remote result could not be checked.',hint:'Keep this decision open and use Open setup checks before trying again.',actions_available:['diagnostics']};}
  else if(failDraftLoad){status=503;data={error:'The draft could not be loaded.',hint:'Open setup checks and try again.',actions_available:['diagnostics']};}
  else data={job};
 }
 else {unexpected.push({method,path});status=503;data={error:'Unimplemented test fixture route '+path,hint:'This request was intercepted; no live API was reached.'};}
 return {data,status};
}
const check=async(name,expression)=>{const result=await evaluate(expression);if(!result)throw new Error('Assertion failed: '+name);assertions.push({name,result:'passed'});console.log('PASS',name);};
const navigate=async hash=>{await evaluate(`location.hash=${JSON.stringify(hash)}`);await delay(250);};
const close=async()=>{if(await evaluate(`document.querySelector('#app-dialog').open`))await click('#dialog-close');};
async function saveReceipts(status,error){await writeFile(join(outputDir,'pass2-browser-receipts.json'),JSON.stringify({status,error:error?.message,assertions,requests,submissions,unexpected,errors,failed},null,2));}

try{
 await Promise.all([send('Runtime.enable'),send('Page.enable'),send('Log.enable'),send('Network.enable')]);
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.method!=='Fetch.requestPaused')return;const p=m.params,result=respond(p);if(result){const fulfill=()=>send('Fetch.fulfillRequest',{requestId:p.requestId,responseCode:result.status,responseHeaders:[{name:'Content-Type',value:'application/json'}],body:Buffer.from(JSON.stringify(result.data)).toString('base64')});if(delayItem&&new URL(p.request.url).pathname==='/api/fleet/item')setTimeout(fulfill,800);else fulfill();}else if(!['GET','HEAD'].includes(p.request.method)){unexpected.push({method:p.request.method,url:p.request.url});send('Fetch.failRequest',{requestId:p.requestId,errorReason:'BlockedByClient'});}else send('Fetch.continueRequest',{requestId:p.requestId});});
 await send('Fetch.enable',{patterns:[{urlPattern:'*'}]});await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});await send('Page.navigate',{url:baseURL});await waitFor(`document.querySelector('h1')?.textContent==='Your work, moving forward.'`);
 await navigate('fleet');await waitFor(`document.querySelector('[data-fleet-item="scope-item"]')`);await click('[data-fleet-item="scope-item"] [data-action="fleet-open-item"]');await waitFor(`document.querySelector('#app-dialog').open`);await click('#dialog-body [data-action="recovery-fleet-scope"]');await delay(100);
 await check('Scope handoff leaves the project form operable after item details',`!document.querySelector('#app-dialog').open&&document.querySelector('#fleet-start-form').contains(document.activeElement)`);
 await fill('#fleet-search','nothing-matches-this');await check('An empty filtered queue offers Clear filters instead of starting a fleet',`document.querySelector('#fleet-monitor').innerText.includes('No matching')&&Boolean(document.querySelector('#fleet-monitor [data-action="recovery-filter-clear"]'))`);await click('[data-action="recovery-filter-clear"]');await check('Clear filters restores existing held work',`Boolean(document.querySelector('[data-fleet-item="scope-item"]'))`);
 await navigate('jobs');await click('[data-action="job-detail"][data-id="older-failure"]');await click('[data-action="recovery-job-edit"]');await check('Correcting an older request restores its original required words',`document.querySelector('#task-field-words').value==='Keep this exact earlier request.'`);
 await click('[data-action="new-repo"]');await waitFor(`document.querySelector('#repo-form')`);await close();await navigate('repos');await click('[data-action="new-repo"]');await fill('#repo-owner','example');await fill('#repo-name','independent-project');await click('#load-branches');await waitFor(`document.querySelector('#repo-branch option[value="main"]')`);await fill('#repo-branch','main','change');await fill('#repo-checks','npm test');await click('[form="repo-form"]');await delay(300);await check('Cancelling prerequisite setup does not hijack a later independent repository save',`!document.querySelector('#app-dialog').open`);
 await navigate('watches');await click('[data-action="recovery-watch-edit"]');await waitFor(`document.querySelector('#watch-edit-form')`);await check('Editing a stopped watch shows its disabled state explicitly',`Boolean(document.querySelector('#watch-edit-enabled'))&&!document.querySelector('#watch-edit-enabled').checked`);await click('[form="watch-edit-form"]');await waitFor(`!document.querySelector('#app-dialog').open`);if(submissions.at(-1).body.enabled!==false)throw new Error('A stopped watch was implicitly enabled');assertions.push({name:'Saving watch repairs preserves its stopped choice',result:'passed'});
 await navigate('settings');await check('Settings identifies the effective DeepSeek connection without claiming it was tested',`document.querySelector('#provider-source-form').closest('section').innerText.includes('Company broker')&&document.querySelector('#provider-source-form').closest('section').innerText.includes('does not prove')`);await fill('#deepseek-source','env','change');await click('#provider-source-form button[type="submit"]');await waitFor(`document.querySelector('#provider-source-status')?.innerText.includes('Restart')`);await check('Choosing a saved DeepSeek key updates the shown connection and explains restart',`document.querySelector('#provider-source-form').closest('section').innerText.includes('Dedicated DeepSeek key')`);await click('[data-action="store-deepseek"]');await check('DeepSeek credential entry defaults to the connection it will select',`document.querySelector('#credential-kind').value==='deepseek'`);await fill('#credential-value','fake-fixture-only');await fill('#credential-supplier','Fixture member');await click('[form="credential-form"]');await waitFor(`!document.querySelector('#app-dialog').open`);if(submissions.at(-1).body.provider_source!=='env')throw new Error('Stored DeepSeek key was not explicitly selected');assertions.push({name:'Saving a DeepSeek key explicitly selects it',result:'passed'});
 await navigate('fence');await waitFor(`document.querySelector('[data-action="recovery-watches"]')`);await check('Checks screen reports the blocked watch and hides unsupported fleet-only posting toggle',`document.querySelector('main').innerText.includes('Needs a results folder')&&!document.querySelector('[data-action="authority-toggle"][data-repo="example/fleet-only"]')`);await click('[data-action="recovery-watches"]');await check('Checks screen Review watches opens actual repair controls',`location.hash==='#watches'&&Boolean(document.querySelector('[data-action="recovery-watch-edit"]'))`);await navigate('fence');await click('[data-action="new-repo"][data-repo="example/fleet-only"]');await check('Fleet-only repository setup carries the correct project name',`document.querySelector('#repo-owner').value==='example'&&document.querySelector('#repo-name').value==='fleet-only'`);await close();
 await navigate('jobs');await click('[data-action="job-detail"][data-id="older-failure"]');await click('[data-action="job-files"]');await waitFor(`document.querySelector('#job-file-browser [data-action="recovery-job-export"]')`);await check('Missing terminal task files offer a working save path and report',`Boolean(document.querySelector('#job-file-browser a[download]'))`);await click('#job-file-browser [data-action="recovery-job-export"]');await check('Missing-file recovery opens the existing-results destination form',`Boolean(document.querySelector('#recovery-destination-form'))`);await close();
 await navigate('jobs');await click('[data-action="job-detail"][data-id="waiting-job"]');await check('Partial-copy omissions are readable beside the saved result',`document.querySelector('#dialog-body').innerText.includes('.private-key')&&document.querySelector('#dialog-body').innerText.includes('original location')`);failDraftLoad=true;await click('[data-action="draft-review"]');await waitFor(`document.querySelector('#dialog-body .dialog-error')`);await check('Draft loading failure gives recovery inside the active dialog',`document.querySelector('#dialog-body').innerText.includes('Open setup checks')`);await close();failDraftLoad=false;
 delayItem=true;await navigate('fleet');await click('[data-fleet-item="scope-item"] [data-action="fleet-open-item"]');await waitFor(`document.querySelector('#dialog-body')?.innerText.includes('Reading the result')`);await close();await navigate('jobs');await click('[data-action="new-task"]');await delay(1200);await check('A cancelled slow result cannot replace a newer task dialog',`Boolean(document.querySelector('#task-repo'))&&!document.querySelector('#dialog-body').innerText.includes('This project is outside your selection')`);await close();
 if(unexpected.length)throw new Error('Unexpected fixture requests '+JSON.stringify(unexpected));if(errors.some(e=>e.exception||e.exceptionDetails||e.text==='Uncaught'))throw new Error('Application runtime error');await saveReceipts('passed');console.log('COMPLETE',JSON.stringify({assertions:assertions.length,submissions:submissions.length}));
}catch(error){await saveReceipts('failed',error);await screenshot('pass2-failure').catch(()=>{});console.error(error.stack);process.exitCode=1;}
finally{ws.close();chrome.kill();await delay(400);await rm(profile,{recursive:true,force:true});}
