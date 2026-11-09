// Real HTTP control plane and files; no interception of API requests.
import {spawn} from 'node:child_process';
import {readFile, writeFile, mkdir, mkdtemp, rm, rmdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const output = process.argv[2];
if (!output) throw new Error('Supply an audit output folder inside your account.');
await mkdir(output,{recursive:true});
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
let serverLog='',chromeLog='',ws,chrome;
const profile=await mkdtemp(join(tmpdir(),'fatchin-watch-chrome-'));
const server=spawn('python3',['tests/watch-flow-server.py'],{cwd:root,env:{...process.env,PYTHONPATH:'src:.'},stdio:['ignore','pipe','pipe']});
server.stderr.on('data',d=>serverLog+=d);
const ready=await new Promise((resolve,reject)=>{
  let buffer=''; server.stdout.on('data',d=>{buffer+=d;const line=buffer.split('\n')[0];if(buffer.includes('\n')){try{resolve(JSON.parse(line));}catch(e){reject(e);}}});
  server.on('exit',code=>reject(new Error(`Temporary test server exited ${code}: ${serverLog}`)));
});
const assertions=[],requests=[],responses=[],errors=[];
let failure;
const assert=(value,label)=>{if(!value)throw new Error(label);assertions.push(label);};
const watches=async()=>JSON.parse(await readFile(ready.watch_file,'utf8'));
try {
  chrome=spawn('/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe']});
  chrome.stderr.on('data',d=>chromeLog+=d);
  let port;
  for(let i=0;i<100;i++){try{port=(await readFile(join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0];break;}catch{await pause(100);}}
  if(!port)throw new Error(chromeLog);
  const target=await(await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0;const pending=new Map();
  ws.onmessage=event=>{const message=JSON.parse(event.data);if(message.id){const waiter=pending.get(message.id);pending.delete(message.id);message.error?waiter.reject(new Error(JSON.stringify(message.error))):waiter.resolve(message.result);}
    else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);
    else if(message.method==='Network.requestWillBeSent'&&message.params.request.url.includes('/api/'))requests.push(message.params.request);
    else if(message.method==='Network.responseReceived'&&message.params.response.url.includes('/api/'))responses.push({url:message.params.response.url,status:message.params.response.status});};
  const send=(method,params={})=>new Promise((resolve,reject)=>{const key=++id;pending.set(key,{resolve,reject});ws.send(JSON.stringify({id:key,method,params}));});
  const evaluate=async expression=>{const reply=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(reply.exceptionDetails)throw new Error(JSON.stringify(reply.exceptionDetails));return reply.result.value;};
  const wait=async expression=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await pause(100);}throw new Error('Timed out: '+expression);};
  const click=async selector=>{await wait(`!!document.querySelector(${JSON.stringify(selector)})`);await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);};
  const fill=async(selector,value,event='input')=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));})()`);
  const screenshot=async name=>{await pause(120);const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});await writeFile(join(output,`${name}.png`),Buffer.from(shot.data,'base64'));};
  const close=async()=>{await click('#dialog-close');await wait('!document.querySelector("#app-dialog").open');};
  const selectSaved=async(prefix,path)=>{await wait(`[...document.querySelectorAll('#${prefix}-saved-destinations article')].some(el=>el.textContent.includes(${JSON.stringify(path)}))`);await evaluate(`[...document.querySelectorAll('#${prefix}-saved-destinations article')].find(el=>el.textContent.includes(${JSON.stringify(path)})).querySelector('button').click()`);};
  const openEdit=async(repo,mode='all',number='')=>{await click(`[data-action="recovery-watch-edit"][data-key="${repo}|${mode}|${number}"]`);await wait('!!document.querySelector("#watch-edit-form")');};
  const submitEdit=async()=>{await click('[form="watch-edit-form"]');await wait('!document.querySelector("#app-dialog").open');};
  const newWatch=async repo=>{await click('[data-action="new-watch"]');await fill('#watch-repo',repo,'change');await fill('#watch-mode','all','change');await wait('document.querySelector("#watch-saved-destinations").textContent.includes("Choose where")');};
  await send('Page.enable');await send('Runtime.enable');await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1365,height:1000,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url:ready.url+'/#repos'});
  await click('[data-action="new-repo"]');
  await fill('#repo-owner','owner');await fill('#repo-name','new-project');
  await click('#load-branches');await wait('document.querySelector("#repo-branch").options.length > 1');
  await fill('#repo-branch','main','change');await fill('#repo-checks','python3 -m unittest');
  await click('[form="repo-form"]');
  await wait('!!document.querySelector("#watch-edit-form")');
  assert(await evaluate('document.querySelector("#dialog-title").textContent.includes("Finish setting up")'),'Adding repository opens inactive watch setup automatically');
  const state=await(await fetch(ready.url+'/api/state')).json();
  assert(JSON.stringify(state.settings.fleet_policy)===JSON.stringify(ready.policy),'Repository save succeeds with fleet_policy and preserves its nested values');
  assert(!(await watches())['owner/new-project']['*'].enabled,'New repository has a stopped pending watch before any destination decision');
  assert(await evaluate('!document.querySelector("#watch-edit-enabled").checked && !document.querySelector("#watch-edit-destination").value'),'Pending setup chooses neither automatic work nor a destination');
  for(const [name,path] of [['earlier task',ready.task_output],['saved fleet',ready.fleet_output],['other watch',ready.other_output]]){
    await wait(`document.querySelector('#watch-edit-saved-destinations').textContent.includes(${JSON.stringify(path)})`);
    assert(true,`Valid ${name} destination appears as an explicit choice`);
  }
  await screenshot('01-pending-watch-saved-choices');
  await selectSaved('watch-edit',ready.task_output);
  assert(await evaluate(`document.querySelector('#watch-edit-account').value==='alice' && document.querySelector('#watch-edit-destination').value===${JSON.stringify(ready.task_output)} && !document.querySelector('#watch-edit-enabled').checked`),'Selecting a saved folder fills the form without enabling automation');
  await submitEdit();
  let saved=(await watches())['owner/new-project']['*'];
  assert(saved.output_dir===ready.task_output&&!saved.enabled&&!saved.setup_required,'Saving setup persists the explicit folder and keeps automation stopped');
  assert(saved.watch_history.some(row=>row.previous?.setup_required),'Saving setup retains the original pending watch in history');
  await openEdit('owner/new-project');
  assert(await evaluate(`document.querySelector('#watch-edit-destination').value===${JSON.stringify(ready.task_output)} && !document.querySelector('#watch-edit-enabled').checked`),'Reopened setup remembers saved folder and stopped choice');
  await click('#watch-edit-enabled');await submitEdit();
  assert((await watches())['owner/new-project']['*'].enabled,'Automation changes only after explicitly enabling and saving');

  await newWatch('owner/new-watch');
  await selectSaved('watch',ready.other_output);
  assert(await evaluate('!document.querySelector("#watch-confirm").checked'),'New watch starts with automatic-work confirmation unchecked');
  const beforePosts=requests.filter(r=>r.method==='POST'&&r.url.endsWith('/api/watches')).length;
  await click('[form="watch-form"]');await pause(150);
  assert(requests.filter(r=>r.method==='POST'&&r.url.endsWith('/api/watches')).length===beforePosts,'Creating a watch without required confirmation sends no request');
  await click('#watch-confirm');await click('[form="watch-form"]');await wait('!document.querySelector("#app-dialog").open');
  saved=(await watches())['owner/new-watch']['*'];
  assert(saved.output_dir===ready.other_output&&saved.member==='alice'&&saved.enabled,'New watch persists explicitly chosen saved folder and account');
  await openEdit('owner/new-watch');
  assert(await evaluate(`document.querySelector('#watch-edit-destination').value===${JSON.stringify(ready.other_output)} && document.querySelector('#watch-edit-enabled').checked`),'Newly created watch reopens with the same enabled choice and folder');
  await close();

  await click('[data-action="new-watch"]');await fill('#watch-repo','owner/repo','change');
  await wait('document.querySelector("#watch-target").options.length > 1');
  await fill('#watch-target','7','change');await selectSaved('watch',ready.other_output);
  await click('#watch-confirm');await click('[form="watch-form"]');await wait('!document.querySelector("#app-dialog").open');
  saved=(await watches())['owner/repo']['7'];
  assert(saved.output_dir===ready.other_output&&saved.last_review_comment_id===30,'Default one-pull-request flow saves destination and records existing feedback as starting point');
  await openEdit('owner/repo','selected','7');
  assert(await evaluate(`document.querySelector('#watch-edit-number').value==='7'&&document.querySelector('#watch-edit-destination').value===${JSON.stringify(ready.other_output)}`),'One-pull-request watch reopens with its target and saved folder');
  await close();

  await newWatch('owner/browse-only');await fill('#watch-account','bob','change');
  await wait('document.querySelector("#watch-saved-destinations").textContent.includes("No usable saved folder")');
  assert(await evaluate('document.querySelector("#watch-saved-destinations").textContent.includes("Browse folders")'),'No saved folder explains the account and folder-browser next step');
  await click('#watch-browse');await wait('document.querySelector("#folder-dialog").open && !!document.querySelector(".folder-row")');
  await evaluate(`[...document.querySelectorAll('.folder-row')].find(el=>el.textContent.includes('manual-results')).click()`);
  await wait(`document.querySelector('#folder-body').textContent.includes(${JSON.stringify(ready.bob_output)})`);
  await click('#folder-select');await click('#watch-confirm');await click('[form="watch-form"]');await wait('!document.querySelector("#app-dialog").open');
  saved=(await watches())['owner/browse-only']['*'];
  assert(saved.member==='bob'&&saved.output_dir===ready.bob_output&&saved.enabled,'Account plus real folder browser creates a usable watch with no saved suggestion');

  await openEdit('owner/stale');await selectSaved('watch-edit',ready.stale_output);await click('#watch-edit-enabled');
  const staleBefore=(await watches())['owner/stale']['*'];
  await rmdir(ready.stale_output);
  await click('[form="watch-edit-form"]');
  await wait('document.querySelector("#app-dialog").open && document.querySelector(".dialog-error")?.textContent.includes("does not exist")');
  assert(await evaluate(`document.querySelector('#watch-edit-account').value==='alice'&&document.querySelector('#watch-edit-destination').value===${JSON.stringify(ready.stale_output)}&&document.querySelector('#watch-edit-enabled').checked`),'Deleted suggested folder reports its error in place and preserves entered choices');
  assert(JSON.stringify((await watches())['owner/stale']['*'])===JSON.stringify(staleBefore),'Invalid suggested folder cannot overwrite saved watch or feedback cursor');
  await screenshot('02-stale-folder-preserves-form');
  await selectSaved('watch-edit',ready.other_output);await submitEdit();
  saved=(await watches())['owner/stale']['*'];
  assert(saved.output_dir===ready.other_output&&saved.last_review_comment_id===73&&saved.watch_history.some(row=>row.receipt==='stale-watch-history-kept'),'Choosing another valid folder repairs the watch while preserving feedback and history');
  await screenshot('03-completed-watch-list');
  const queue=JSON.parse(await readFile(ready.queue_file,'utf8'));
  assert(Object.keys(queue.jobs).length===2,'All setup flows preserve earlier runs and start no background coding tasks');
  assert(errors.length===0,'Real browser reports no JavaScript exceptions');
  await writeFile(join(output,'saved-watches.json'),JSON.stringify(await watches(),null,2));
}catch(error){failure=error;}
finally{
  await writeFile(join(output,'results.json'),JSON.stringify({passed:!failure,assertions,failure:failure?.stack,errors,requests:requests.map(r=>({method:r.method,url:r.url,body:r.postData})),responses,fixture:{...ready},verified_at:new Date().toISOString()},null,2));
  await writeFile(join(output,'server.log'),serverLog);
  if(ws)ws.close();chrome?.kill('SIGTERM');server.kill('SIGTERM');
  await pause(150);await rm(profile,{recursive:true,force:true});
}
if(failure){console.error(failure);process.exitCode=1;}else console.log(`${assertions.length} real-backend browser assertions passed; receipts: ${output}`);
