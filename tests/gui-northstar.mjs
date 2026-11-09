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
function refreshProjection(){
 state.fleet_summary={fleets:[fleet],repositories:[],repository_count:0,configured_agents:{glm:0,deepseek:0},active_agents:{glm:0,deepseek:0},task_counts:{blocked:items.filter(i=>!i.archived_at&&i.status==='blocked').length,done:2},unresolved_count:items.filter(i=>!i.archived_at&&i.status==='blocked').length,recent_tasks:items.filter(i=>!i.archived_at),usage:{glm:0,deepseek:0}};
}
refreshProjection();
const fleetData=()=>({fleets:[fleet],items,ready:[],comments:[],workers:[],providers:[{id:'glm',model_id:'glm-5.3-flash',available:true},{id:'deepseek',model_id:'deepseek-flash',available:true}],supervisor:{status:'running',active:true},rate_limits:{},events:[{at:'2026-11-08T21:00:00Z',fleet_id:fleetId,repo,item_id:'blocked-item',message:'Needle: check failed on this selected project.'},{at:'2026-11-08T21:01:00Z',fleet_id:fleetId,repo:'example/outside',item_id:'scope-item',message:'Other project activity.'}],usage:{glm:{calls_today:1},deepseek:{calls_today:0}},history_count:items.length});
const lesson={id:'lesson_northstar_saved',account,output_dir:'/home/example/results',repo,ref:'main',path:'',mode:'guide',lesson:'git-basics',status:'failed',revision:3,step_index:0,step:{id:'move-one',title:'A practice move',intent:'Learn what changed.',action_kind:'git',command:['git','status'],explanation:'This reads the practice project.'},receipts:[{at:'2026-11-08T20:00:00Z',command:'git status',exit_code:1,output:'The recorded practice command failed.'}],practice:{filename:'lesson.txt',content:'Saved practice text.'},error:'The practice command failed. Read the saved output before starting a new lesson.'};
let failFolders=true,failPostCheck=false;
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
 else if(path==='/api/service'){state.service.active=true;state.service.restart_required=false;data={ok:true};}
 else if(path==='/api/github/branches')data={items:[{name:'main'}]};
 else if(path==='/api/repos'){const full_name=body.owner+'/'+body.name;state.repos.push({...body,full_name});state.service.restart_required=true;data={repo:full_name,restart_required:true};}
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
  else data={job};
 }
 else {unexpected.push({method,path});status=503;data={error:'Unimplemented test fixture route '+path,hint:'This request was intercepted; no live API was reached.'};}
 return {data,status};
}
const check=async(name,expression)=>{const result=await evaluate(expression);if(!result)throw new Error('Assertion failed: '+name);assertions.push({name,result:'passed'});console.log('PASS',name);};
const navigate=async hash=>{await evaluate(`location.hash=${JSON.stringify(hash)}`);await delay(250);};
const close=async()=>{if(await evaluate(`document.querySelector('#app-dialog').open`))await click('#dialog-close');};
async function saveReceipts(status,error){await writeFile(join(outputDir,'northstar-browser-receipts.json'),JSON.stringify({status,error:error?.message,assertions,requests,submissions,unexpected,errors,failed},null,2));}
try{
 await Promise.all([send('Runtime.enable'),send('Page.enable'),send('Log.enable'),send('Network.enable')]);
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.method!=='Fetch.requestPaused')return;const p=m.params,result=respond(p);if(result){send('Fetch.fulfillRequest',{requestId:p.requestId,responseCode:result.status,responseHeaders:[{name:'Content-Type',value:'application/json'}],body:Buffer.from(JSON.stringify(result.data)).toString('base64')}).catch(e=>errors.push(e.message));}else if(!['GET','HEAD'].includes(p.request.method)){unexpected.push({method:p.request.method,url:p.request.url});send('Fetch.failRequest',{requestId:p.requestId,errorReason:'BlockedByClient'});}else send('Fetch.continueRequest',{requestId:p.requestId});});
 await send('Fetch.enable',{patterns:[{urlPattern:'*'}]});
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
 await send('Page.navigate',{url:baseURL});
 await waitFor(`document.querySelector('h1')?.textContent==='Your work, moving forward.'`);
 await check('Stopped-fleet unresolved work remains visible on Overview',`document.querySelector('main').innerText.includes('A saved stopped-fleet failure')`);
 await click('[data-action="fleet-open-item"][data-id="done-item"]');await waitFor(`document.querySelector('#dialog-title').textContent==='Finished review with saved evidence'`);
 await check('Finished-item handoff opens exact saved item instead of an empty queue',`document.querySelector('#dialog-body').innerText.includes('One approval is still required.')`);await close();
 await navigate('fleet');await waitFor(`document.querySelector('[data-fleet-item="blocked-item"]')`);
 await check('Stopped item offers Resume and hides invalid Retry',`Boolean(document.querySelector('[data-fleet-item="blocked-item"] [data-action="recovery-fleet-resume"]'))&&!document.querySelector('[data-fleet-item="blocked-item"] [data-action="recovery-fleet-retry"]')`);
 await check('Out-of-scope row offers scope repair without a retry button',`Boolean(document.querySelector('[data-fleet-item="scope-item"] [data-action="recovery-fleet-scope"]'))&&!document.querySelector('[data-fleet-item="scope-item"] [data-action="recovery-fleet-retry"]')`);
 await click('[data-fleet-item="blocked-item"] [data-action="recovery-fleet-archive"]');await waitFor(`document.querySelector('#confirm-action')`);await check('Archive explains that evidence and GitHub changes stay intact',`document.querySelector('#dialog-body').innerText.includes('published-change records remain')`);await click('#confirm-action');await waitFor(`!document.querySelector('#app-dialog').open`);
 await check('Archived item leaves working queue',`!document.querySelector('[data-fleet-item="blocked-item"]')`);
 if(items[0].result.receipt!=='kept-publication-123'||items[0].result.summary!==originalEvidence.summary)throw new Error('Archive fixture lost original evidence');assertions.push({name:'Archive preserves original evidence and receipt',result:'passed'});
 await click('[data-action="fleet-tab"][data-tab="archived"]');await waitFor(`document.querySelector('[data-fleet-item="blocked-item"] [data-action="recovery-fleet-restore"]')`);
 await click('[data-fleet-item="blocked-item"] [data-action="recovery-fleet-restore"]');await waitFor(`document.querySelector('#confirm-action')`);await click('#confirm-action');await waitFor(`!document.querySelector('#app-dialog').open`);
 await check('Restore leaves item held without starting it',`!document.querySelector('[data-fleet-item="blocked-item"]')`);
 if(items[0].status!=='blocked'||!items[0].restore_hold||submissions.some(s=>s.path==='/api/fleet'&&['retry','resume'].includes(s.body.action)))throw new Error('Restoring started work');assertions.push({name:'Restore does not dispatch or retry',result:'passed'});
 await click('[data-action="fleet-tab"][data-tab="history"]');await check('History exposes finished reviews',`Boolean(document.querySelector('[data-fleet-item="done-item"]'))`);
 await click('[data-action="fleet-tab"][data-tab="activity"]');await fill('#fleet-search','Needle');await check('Activity search filters recorded events',`document.querySelectorAll('.fleet-events li').length===1&&document.querySelector('.fleet-events').innerText.includes('Needle')`);await fill('#fleet-search','');await fill('#fleet-repo-filter','example/outside','change');await check('Activity repository filter selects only that project',`document.querySelectorAll('.fleet-events li').length===1&&document.querySelector('.fleet-events').innerText.includes('Other project')`);
 await navigate('jobs');await click('[data-action="job-detail"][data-id="waiting-job"]');await waitFor(`document.querySelector('#dialog-body [data-action="draft-review"]')`);
 await check('Waiting task details contain review edit and discard controls',`['draft-review','draft-edit','draft-discard'].every(a=>document.querySelector('#dialog-body [data-action="'+a+'"]'))`);
 await check('Repository-wide task links to project rather than issue zero',`![...document.querySelectorAll('#dialog-body a')].some(a=>a.href.includes('/issues/0')||a.href.includes('/pull/0'))`);
 await click('#dialog-body [data-action="draft-review"]');await waitFor(`document.querySelector('#draft-body')`);await check('Single draft is previewed verbatim before posting',`document.querySelector('#draft-body').value==='Exact words that must be reviewed before posting.'`);
 if(submissions.some(s=>s.path.endsWith('/draft-action')))throw new Error('Draft preview posted before final decision');assertions.push({name:'Preview performs no publication',result:'passed'});
 await click('[form="draft-decision-form"]');await waitFor(`!document.querySelector('#app-dialog').open`);
 if(submissions.filter(s=>s.path.endsWith('/draft-action')&&s.body.action==='post').length!==1)throw new Error('Final draft decision did not produce exactly one simulated post');assertions.push({name:'Final decision posts exactly selected draft in fixture only',result:'passed'});
 await click('#refresh-button');await delay(150);await click('[data-action="job-detail"][data-id="uncertain-job"]');await waitFor(`document.querySelector('[data-action="recovery-post-check"]')`);
 await check('Uncertain posting hides ordinary publishing and offers reconciliation',`!document.querySelector('#dialog-body [data-action="draft-review"]')&&!document.querySelector('#dialog-body [data-action="draft-post"]')&&Boolean(document.querySelector('[data-action="recovery-post-retire"]'))`);
 failPostCheck=true;await click('[data-action="recovery-post-check"]');await waitFor(`document.querySelector('#dialog-body .dialog-error')`);await check('Errors display the supplied recovery hint and action inside the active dialog',`document.querySelector('#dialog-body .dialog-error').innerText.includes('Keep this decision open')&&Boolean(document.querySelector('#dialog-body .dialog-error [data-action="diagnostics"]'))`);await delay(15100);await check('Error guidance persists until the person dismisses it',`Boolean(document.querySelector('#dialog-body .dialog-error'))`);
 const diagnosticPoint=await evaluate(`(()=>{const b=document.querySelector('#dialog-body .dialog-error [data-action="diagnostics"]');b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
 await send('Input.dispatchMouseEvent',{type:'mousePressed',...diagnosticPoint,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...diagnosticPoint,button:'left',clickCount:1});
 await waitFor(`document.querySelector('#dialog-title').textContent==='Setup checks'&&document.querySelector('.diagnostic-row')`);await check('Recovery action is operable by pointer inside a native modal',`document.querySelector('#dialog-body').innerText.includes('No live setup or model request was made.')`);await close();
 await navigate('watches');await check('Folderless watch names missing setup and offers Edit watch',`document.querySelector('main').innerText.includes('Choose a results folder')&&Boolean(document.querySelector('[data-action="recovery-watch-edit"]'))`);
 await click('[data-action="recovery-watch-edit"]');await waitFor(`document.querySelector('#watch-edit-account')`);await fill('#watch-edit-account',account,'change');await click('#watch-edit-browse');await waitFor(`document.querySelector('#folder-body [data-action="recovery-folder-home"]')`);
 await check('Failed folder browser keeps recovery hint and a route home',`document.querySelector('#folder-body').innerText.includes('Return to your home folder')`);failFolders=false;await click('#folder-body [data-action="recovery-folder-home"]');await waitFor(`document.querySelector('#folder-body').innerText.includes('/home/example')&&document.querySelector('#folder-select')`);await check('Return home actually reloads available folders',`document.querySelector('#folder-body').innerText.includes('results')`);await click('#folder-close');await close();
 await navigate('learn');await click('[data-action="explore-tab"][data-tab="lesson"]');await waitFor(`document.querySelector('#saved-lessons-account')`);await fill('#saved-lessons-account',account,'change');await click('[data-action="recovery-lessons"]');await waitFor(`document.querySelector('[data-action="recovery-lesson-open"]')`);await check('Readable failed lessons can be found after losing browser storage',`document.querySelector('.saved-lessons').innerText.includes('failed')||document.querySelector('.saved-lessons').innerText.includes('Failed')`);await click('[data-action="recovery-lesson-open"]');await waitFor(`document.querySelector('.learn-session')`);await check('Saved failed lesson opens retained output and transcript recovery',`document.querySelector('.learn-session').innerText.includes('The recorded practice command failed.')&&Boolean(document.querySelector('[data-action="recovery-lesson-destination"]'))&&Boolean(document.querySelector('[data-lesson-download]'))`);

 // A stopped worker and a missing repository are prerequisites, not reasons to lose a request.
 state.service.active=false;await navigate('overview');await click('#refresh-button');await delay(150);await click('[data-action="new-task"]');await waitFor(`document.querySelector('#task-repo')`);
 await fill('#task-repo',repo,'change');await evaluate(`document.querySelector('input[name="task-kind"][value="explain-repo"]').checked=true;document.querySelector('input[name="task-kind"][value="explain-repo"]').dispatchEvent(new Event('change',{bubbles:true}))`);
 await fill('#task-field-question','Keep my question through setup.');await click('[data-action="wizard-next"]');await fill('#task-account',account,'change');await click('#task-browse');await waitFor(`document.querySelector('[data-folder-index="0"]')`);await click('[data-folder-index="0"]');await waitFor(`document.querySelector('#folder-select:not([disabled])')`);await click('#folder-select');await click('[data-action="wizard-next"]');
 await check('Stopped-worker review keeps the request and provides Start here',`document.querySelector('#queue-task').disabled&&Boolean(document.querySelector('[data-action="recovery-wizard-start"]'))&&document.querySelector('#dialog-body').innerText.includes('Keep my question through setup.')`);
 await click('[data-action="recovery-wizard-start"]');await waitFor(`!document.querySelector('#queue-task').disabled`);await check('Starting the worker preserves the exact request and destination',`document.querySelector('#dialog-body').innerText.includes('Keep my question through setup.')&&document.querySelector('#dialog-body').innerText.includes('/home/example/results')`);await close();
 await click('[data-action="new-task"]');await check('Reopening task creation restores the saved unfinished request',`document.querySelector('#task-field-question').value==='Keep my question through setup.'`);await click('[data-action="new-repo"]');await waitFor(`document.querySelector('#repo-form')`);await fill('#repo-owner',account);await fill('#repo-name','another');await click('#load-branches');await waitFor(`document.querySelector('#repo-branch option[value="main"]')`);await fill('#repo-branch','main','change');await fill('#repo-checks','npm test');await click('[form="repo-form"]');await waitFor(`document.querySelector('#task-repo')?.value==='example/another'`);
 await check('Adding a repository returns to the saved task and question',`document.querySelector('#task-field-question').value==='Keep my question through setup.'`);await click('[data-action="wizard-next"]');await click('[data-action="wizard-next"]');await waitFor(`document.querySelector('[data-action="recovery-wizard-restart"]')`);await click('[data-action="recovery-wizard-restart"]');await waitFor(`!document.querySelector('#queue-task').disabled`);await check('Applying repository settings keeps the request ready for a separate Queue decision',`document.querySelector('#dialog-body').innerText.includes('Keep my question through setup.')&&!document.querySelector('#queue-task').disabled`);await close();
 await navigate('learn');await click('[data-action="recovery-lesson-destination"]');await waitFor(`document.querySelector('#recover-account')`);await click('#recover-browse');await waitFor(`document.querySelector('#folder-select:not([disabled])')`);await click('#folder-select');await click('[form="recovery-destination-form"]');await waitFor(`!document.querySelector('#app-dialog').open`);await check('Lesson destination recovery keeps the failed lesson and saved text',`document.querySelector('.learn-session').innerText.includes('The recorded practice command failed.')&&Boolean(document.querySelector('[data-lesson-download]'))`);
 await navigate('settings');await check('Both fleet providers have named connection tests',`Boolean(document.querySelector('[data-action="recovery-provider-glm"]'))&&Boolean(document.querySelector('[data-action="recovery-provider-deepseek"]'))`);await click('[data-action="store-credential"]');await check('DeepSeek has its own named credential destination',`Boolean(document.querySelector('#credential-kind option[value="deepseek"]'))`);await close();


 await navigate('overview');await click('[data-action="new-task"]');await waitFor(`document.querySelector('[data-action="recovery-fresh-task"]')`);await click('[data-action="recovery-fresh-task"]');await check('Starting a different request explains exactly what will be cleared',`document.querySelector('#dialog-body').innerText.includes('Existing tasks, their history, result files and GitHub changes stay exactly as they are.')`);await click('#confirm-action');await waitFor(`document.querySelector('#task-field-words')`);await check('Fresh request clears only entered details and preserves existing saved jobs',`document.querySelector('#task-field-words').value===''&&document.querySelector('#task-repo').value===''`);if(jobs.length!==2)throw new Error('Starting a fresh request changed existing jobs');await close();

 if(unexpected.length)throw new Error('Unexpected fixture requests: '+JSON.stringify(unexpected));
 if(errors.some(e=>e.exception||e.exceptionDetails||e.text==='Uncaught'))throw new Error('Browser runtime errors occurred');
 await screenshot('northstar-saved-lesson');await saveReceipts('passed');console.log('COMPLETE',JSON.stringify({assertions:assertions.length,requests:requests.length,simulatedSubmissions:submissions.length}));
}catch(error){await saveReceipts('failed',error);try{await screenshot('northstar-failure');}catch{};console.error(error.stack);process.exitCode=1;
}finally{ws.close();chrome.kill();await delay(400);await rm(profile,{recursive:true,force:true});}
