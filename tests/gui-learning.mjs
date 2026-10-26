// Isolated exploration and learning-interface regressions. CDP fulfills EVERY /api/* request; no live writes.
// Requires the real static GUI HTTP service and installed host Chrome, no npm dependencies.
// Usage: node tests/gui-learning.mjs http://worker-address:18465 /home/you/gui-fixtures
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
    "Usage: node tests/gui-learning.mjs <workspace-url> <output-folder>",
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
// This fixture proves browser pacing and rendering, not Git or provider behavior.
// Every API call is intercepted before navigation; no repository or model is touched.
const fixture={
  version:"learning-ui-fixture",config_revision:"r1",
  service:{active:true,polling_enabled:true,restart_required:false},
  github:{configured:true,login:"example"},
  accounts:[{account:"example",path:"/home/example"}],
  repos:[{full_name:"example/project",owner:"example",name:"project",base_branch:"main",checks:["python -m unittest"]}],
  jobs:[],watches:[],summary:{},settings:{model:{model_id:"fixture-model",max_calls_per_job:10,max_calls_per_day:100}}
};
const types=[
  {id:"fix-pr",label:"Fix a pull request",help:"Fix review feedback.",takes:"pr",profile:"review-fix",fields:[]},
  {id:"write-docs",label:"Write the documentation",help:"Prepare documentation.",takes:"repo",profile:"draft",fields:[{name:"focus",label:"Focus",required:false}]},
  {id:"triage-issues",label:"Turn your words into issues",help:"Prepare issues.",takes:"repo",profile:"draft",fields:[{name:"words",label:"Your words",required:true}]},
  {id:"new-project",label:"Start a new project",help:"Prepare a new private project.",takes:"none",profile:"high-stakes",fields:[{name:"name",label:"Project name",required:true},{name:"idea",label:"Your idea",required:true}]},
  {id:"cleanup-merged",label:"Tidy up after a merge",help:"Finish merged work.",takes:"pr",profile:"high-stakes",allows_closed:true,fields:[]},
];
const source="https://docs.github.com/en/get-started/accessibility/keyboard-shortcuts";
const catalog={
  verified:"2026-10-25",
  tools:["diagram","ingest","editor","viewer","wiki","mcp","source","archive"].map((id)=>({id,title:id,purpose:"Understand this project.",when:"You want to read its code.",how:"Open the selected view.",limits:"Read its documented limits.",source})),
  shortcuts:[{key:".",action:"Open the browser editor",where:"On GitHub, outside text fields."},{key:"y",action:"Pin the file link",where:"In GitHub file view."}],
  clients:[{id:"cursor",label:"Cursor"},{id:"vscode",label:"VS Code"}],
};
const writes=[],reads=[];
let session=null,sessionNumber=0,privateRepo=false,narrationFailure=false,staleNext=false,slowNarration=false,largePreview=false;
function prepared(index){
  const edit=index===1;
  return {id:"step-"+index,title:edit?"Edit a practice file":"Inspect the practice project",intent:edit?"Try a small text change.":"See what Git reports.",command:edit?"Write lesson.txt":"git status --short",explanation:"This prepared action stays in the disposable project.",memory_aid:"Status shows the state.",narration:"",narration_source:"",action_kind:edit?"edit":"git",instruction:"Read the explanation before continuing.",preview_content:"A prepared practice sentence.\n"};
}
function explorer(url){
  const view=url.searchParams.get("view")||"tree",path=url.searchParams.get("path")||"";
  const repo="example/project",sha="a".repeat(40);
  return {repository:{full_name:repo,private:privateRepo,default_branch:"main",ref:url.searchParams.get("ref")||"main",sha},branches:[{name:"main",sha},{name:"feature",sha:"b".repeat(40)}],
    items:[{name:"README.md",path:"README.md",type:"file"},{name:'<img src=x onerror="globalThis.badSource=true">',path:"odd.txt",type:"file"}],
    file:view==="file"?{path,text:largePreview?'':'<script>globalThis.badSource=true</script>\n# Project',binary:false,truncated:true}:null,
    history:view==="history"?[{sha,message:"Add the initial project",author:"Example",date:"2026-10-25T00:00:00Z"}]:[],
    comparison:view==="compare"?{base:"main",head:"feature",merge_base:sha,files:[{filename:"README.md",status:"modified",additions:1,deletions:0}],commits:[],truncated:false}:null,
    links:{github:"https://github.com/"+repo,editor:"https://github.dev/"+repo,viewer:"https://github1s.com/"+repo,diagram:"https://gitdiagram.com/"+repo,ingest:"https://gitingest.com/"+repo,wiki:"https://deepwiki.com/"+repo,mcp:"https://gitmcp.io/"+repo,source:"https://github.com/"+repo+"/blob/"+sha+"/README.md?plain=1",raw:"https://github.com/"+repo+"/raw/"+sha+"/README.md",permalink:"https://github.com/"+repo+"/blob/"+sha+"/README.md",blame:"https://github.com/"+repo+"/blame/"+sha+"/README.md",archive:"https://github.com/"+repo+"/archive/"+sha+".zip",avatar:"https://github.com/example.png"},
    mcp:{cursor:{mcpServers:{project:{url:"https://gitmcp.io/"+repo}}},vscode:{servers:{project:{type:"http",url:"https://gitmcp.io/"+repo}}}},
    receipts:[{command:"gh api repos/example/project/contents",explanation:"Read GitHub file information.",git_equivalent:"git ls-tree HEAD"}],truncated:false};
}
async function route(request){
  const url=new URL(request.url),path=url.pathname,method=request.method;
  const data=JSON.parse(request.postData||"{}");
  if(method==="POST")writes.push({path,data});else reads.push(url.pathname+url.search);
  if(path==="/api/state")return {status:200,result:fixture};
  if(path==="/api/task-types")return {status:200,result:{items:types}};
  if(path==="/api/explore/catalog")return {status:200,result:catalog};
  if(path==="/api/explore")return {status:200,result:explorer(url)};
  if(path==="/api/folders")return {status:200,result:{path:"/home/example/lessons",parent:"/home/example",items:[],writable:true}};
  if(path==="/api/github/targets")return {status:200,result:{items:[{number:12,title:"Finished change",state:"closed"}]}};
  if(path==="/api/github/target")return {status:200,result:{item:{number:12,title:"Finished change",state:"MERGED",body:"Ready to tidy"}}};
  if(path==="/api/jobs")return {status:200,result:{ok:true}};
  if(path==="/api/learn"&&method==="GET")return {status:200,result:{session:structuredClone(session)}};
  if(path==="/api/learn"){
    if(data.action==="start"){
      session={id:"session-"+(++sessionNumber),version:1,revision:1,mode:data.mode,lesson:data.lesson,repo:data.repo,ref:data.ref,path:data.path,account:data.account,output_dir:data.output_dir,status:"ready",step_index:0,step:prepared(0),practice:{filename:"lesson.txt",content:"",dirty:false},receipts:[],answer:"",error:""};
      return {status:200,result:{session:structuredClone(session)}};
    }
    if(data.revision!==session.revision||data.id!==session.id)return {status:409,result:{error:"Session changed. Reload session before continuing."}};
    if(staleNext&&data.action==="next"){staleNext=false;session.revision++;return {status:409,result:{error:"Session changed in another tab. Reload session."}};}
    session.revision++;
    if(data.action==="narrate"){
      if(narrationFailure&&!data.builtin){narrationFailure=false;return {status:503,result:{error:"The model could not explain this move. Retry or select the built-in guide."}};}
      session.step.narration='Read **this carefully**. Use '+String.fromCharCode(96)+'git status'+String.fromCharCode(96)+'. <script>globalThis.badNarration=true</script>';
      session.step.narration_source=data.builtin?"built-in":"model";
      if(slowNarration&&!data.builtin){
        slowNarration=false;const reply=structuredClone(session);await delay(900);
        return {status:200,result:{session:reply}};
      }
    }else if(["run","perform","edit"].includes(data.action)){
      if(data.action==="edit")session.practice.content=data.content;
      else if(session.step.action_kind==="edit")session.practice.content=session.step.preview_content;
      session.status="completed";
      session.receipts.push({step_id:session.step.id,command:session.step.command,output:"Completed safely. <script>globalThis.badReceipt=true</script>",exit_code:0,at:"2026-10-25T00:00:00Z",observed:true});
    }else if(data.action==="next"){
      session.step_index++;session.step=prepared(session.step_index);session.status=session.step_index>=3?"finished":"ready";
    }else if(data.action==="pause"){session.resume_status=session.status;session.status="paused";}
    else if(data.action==="resume")session.status=session.resume_status||"ready";
    else if(data.action==="question")session.answer="Add stages a change; commit saves it. <script>globalThis.badAnswer=true</script>";
    else if(data.action==="export")session.export={path:session.output_dir+"/lesson.md"};
    else if(data.action==="end")session.status="ended";
    return {status:200,result:{session:structuredClone(session)}};
  }
  return {status:404,result:{error:"Unhandled fixture route "+path}};
}
const originalHandler=ws.onmessage;
ws.onmessage=(event)=>{
  const msg=JSON.parse(event.data);
  if(msg.method==="Fetch.requestPaused"){
    route(msg.params.request).then(({status,result})=>send("Fetch.fulfillRequest",{
      requestId:msg.params.requestId,responseCode:status,
      responseHeaders:[{name:"Content-Type",value:"application/json"}],
      body:Buffer.from(JSON.stringify(result)).toString("base64"),
    })).catch((e)=>console.error("Fixture route failed",e));
  }else originalHandler(event);
};
const checks=[];
const assert=(ok,message)=>{if(!ok)throw new Error(message);checks.push(message);console.log("PASS",message);};
const submit=(selector)=>evaluate(`document.querySelector(${JSON.stringify(selector)}).requestSubmit()`);
const goTab=(id)=>click('[data-action="explore-tab"][data-tab="'+id+'"]');
const present=(selector)=>waitFor(`!!document.querySelector(${JSON.stringify(selector)})`);
const done=()=>waitFor("document.querySelector('[data-action=\"learn-next\"]')&&!document.querySelector('[data-action=\"learn-next\"]').disabled");
try{
  await Promise.all([send("Runtime.enable"),send("Page.enable"),send("Log.enable"),send("Network.enable")]);
  await send("Fetch.enable",{patterns:[{urlPattern:"*/api/*",requestStage:"Request"}]});
  await send("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await send("Page.navigate",{url:baseURL.replace(/\/$/,"")+"/#learn"});
  await present("#explore-form");
  const recoveryOnly=process.argv.includes("--focus=edit-recovery");
  if(recoveryOnly){
    session={id:"recovery-session",version:1,revision:1,mode:"guide",lesson:"git-basics",repo:"example/project",ref:"main",path:"",account:"example",output_dir:"/home/example/lessons",status:"ready",step_index:1,step:prepared(1),practice:{filename:"lesson.txt",content:"Saved text",dirty:false},receipts:[
      {step_id:"earlier-diff",command:"git diff --no-index --no-ext-diff --no-textconv -- /dev/null lesson.txt",output:"+New text",exit_code:1,at:"2026-10-25T00:00:00Z",observed:true},
      {step_id:"interrupted",command:"git commit -m Practice",output:"The connection ended",exit_code:null,at:"2026-10-25T00:00:00Z",observed:false}
    ],answer:"",error:""};
    session.step.narration="Use the practice editor.";session.step.narration_source="built-in";
    await evaluate("localStorage.setItem('fatchin-learning-session','recovery-session');globalThis.oldLearningDocument=true");
    await send("Page.reload");await waitFor("!globalThis.oldLearningDocument&&!!document.querySelector('#learn-practice-content')");
    const practiceText="My unsaved sentence."+String.fromCharCode(10);
    await fill("#learn-practice-content",practiceText);
    await click('[data-action="learn-export"]');
    assert(!writes.some((r)=>r.data.action==="export")&&await evaluate(`document.querySelector('#learn-practice-content').value===${JSON.stringify(practiceText)}`),"Export refuses unsaved browser text and preserves it");
    await click('[data-action="learn-restore"]');await present("#confirm-action");await click('[data-action="dialog-close"]');
    assert(await evaluate(`document.querySelector('#learn-practice-content').value===${JSON.stringify(practiceText)}`),"Cancelling Reload preserves unsaved browser text");
    assert(await evaluate("!window.dispatchEvent(new Event('beforeunload',{cancelable:true}))"),"Leaving the page warns when practice text is unsaved");
    await click('[data-action="learn-restore"]');await present("#confirm-action");await click("#confirm-action");
    await waitFor("document.querySelector('#learn-practice-content')?.value==='Saved text'");
    assert(await evaluate("document.querySelector('#learn-practice-content').value==='Saved text'"),"Confirmed Reload explicitly replaces the draft with the saved file");
    await fill("#learn-practice-content",practiceText);

    assert(await evaluate("document.querySelector('.learn-receipts').textContent.includes('Differences found')&&document.querySelector('.learn-receipts').textContent.includes('Outcome not confirmed')&&!document.querySelector('.learn-receipts').textContent.includes('You reported this completed')"),"Expected diff results and uncertain outcomes are labeled accurately");
    await click('[data-action="learn-edit"]');await done();
    assert(session.practice.content===practiceText,"The prepared Save action keeps the member’s exact multiline text");
    // Explicitly reload and accept replacement after editing again.
    await click('[data-action="learn-next"]');await present('[data-action="learn-narrate"]');
    await goTab("browse");
    await fill("#explore-custom","example/project");await submit("#explore-form");await present('[data-action="explore-file"]');
    largePreview=true;
    await click('[data-action="explore-file"][data-index="0"]');await present(".explore-source");
    assert(await evaluate("document.querySelector('.explore-source').textContent.includes('Preview unavailable')&&!document.querySelector('.explore-source').textContent.includes('file is empty')"),"An unavailable large-file preview is not described as an empty file");
    await writeFile(join(outputDir,"learning-recovery-report.json"),JSON.stringify({status:"passed",checks,errors},null,2));
    console.log("Focused learning recovery checks passed:",checks.length);
  }else{
  await delay(100);
  assert(!writes.length&&!reads.some((r)=>r.startsWith("/api/explore?")),"Opening the workspace starts no model call, repository read or lesson action");
  await fill("#explore-custom","https://github.com/example/project");
  await submit("#explore-form");await present('[data-action="explore-file"]');
  assert(reads.some((r)=>r.includes("repo=example%2Fproject")),"Repository URL becomes a validated owner/name query");
  await click('[data-action="explore-file"][data-index="0"]');await present(".explore-source");
  assert(await evaluate("document.querySelector('.explore-source').textContent.includes('<script>')&&!globalThis.badSource"),"Source is escaped, visible and never executed");
  await click('[data-action="explore-view"][data-view="history"]');await present(".explore-history");
  assert(await evaluate("document.querySelector('.explore-history a[href$=\".patch\"]')!==null"),"Saved changes expose real commit patch links");
  await click('[data-action="explore-view"][data-view="compare"]');await submit("#explore-compare-form");await present(".explore-compare");
  assert(await evaluate("document.querySelector('.explore-compare').textContent.includes('README.md')"),"Branch comparison explains and displays changed files");
  await goTab("tools");await present("#explore-mcp-config");
  await fill("#explore-mcp-client","vscode","change");
  assert(await evaluate("document.querySelector('#explore-mcp-config').textContent.includes('servers')&&!document.querySelector('#explore-mcp-config').textContent.includes('mcpServers')"),"MCP configuration changes to the selected client");
  privateRepo=true;await submit("#explore-form");
  await waitFor("Array.from(document.querySelectorAll('.badge')).some((e)=>e.textContent==='Private project')");
  assert(await evaluate("!document.querySelector('a[href^=\"https://gitdiagram.com/\"]')&&!document.querySelector('a[href^=\"https://gitmcp.io/\"]')&&!!document.querySelector('a[href^=\"https://github.dev/\"]')"),"Private project suppresses third-party links while official GitHub remains usable");
  await goTab("lesson");await present("#learn-start-form");
  await fill("#learn-account","example","change");
  await click("#learn-browse");await present("#folder-select");await click("#folder-select");
  await evaluate("(()=>{const f=document.querySelector('#learn-start-form');f.requestSubmit();f.requestSubmit();})()");
  await present('[data-action="learn-narrate"]');
  assert(writes.filter((r)=>r.path==="/api/learn"&&r.data.action==="start").length===1&&/^[a-f0-9-]{36}$/.test(writes.find((r)=>r.data.action==="start").data.request_id),"Repeated Start clicks create one session with a durable request ID");
  assert(await evaluate("!document.querySelector('[data-action=\"learn-perform\"]')&&!document.querySelector('[data-action=\"learn-run\"]')"),"Action is unavailable until its explanation is requested and shown");
  narrationFailure=true;await click('[data-action="learn-narrate"]');
  await waitFor("document.querySelector('[role=alert]')?.textContent.includes('could not explain')");
  assert(!writes.some((r)=>["run","perform","edit"].includes(r.data.action)),"A provider failure explains recovery and never silently runs a command");
  await click('[data-action="learn-builtin"]');await present('[data-action="learn-perform"]');
  assert(await evaluate("document.querySelector('.learn-narration').textContent.includes('Built-in explanation')&&!!document.querySelector('.learn-narration strong')&&document.querySelector('.learn-narration code')?.textContent==='git status'&&!globalThis.badNarration"),"Built-in narration is explicitly labeled and model text stays escaped");
  await click('[data-action="learn-perform"]');await done();
  const actionCount=writes.filter((r)=>["run","perform","edit"].includes(r.data.action)).length;
  await delay(350);
  assert(writes.filter((r)=>["run","perform","edit"].includes(r.data.action)).length===actionCount&&session.step_index===0,"Completed actions wait for an explicit Next move");
  staleNext=true;await click('[data-action="learn-next"]');
  await waitFor("document.querySelector('[role=alert]')?.textContent.includes('another tab')");
  assert(session.step_index===0,"A stale revision is explained without advancing the lesson");
  await click('[data-action="learn-next"]');await present('[data-action="learn-narrate"]');
  await click('[data-action="learn-builtin"]');await present('[data-action="learn-edit"]');
  const practiceText="My own practice sentence."+String.fromCharCode(10);
  await fill("#learn-practice-content",practiceText);
  const exportCountBeforeEdit=writes.filter((r)=>r.data.action==="export").length;
  await click('[data-action="learn-export"]');
  assert(writes.filter((r)=>r.data.action==="export").length===exportCountBeforeEdit&&await evaluate(`document.querySelector("#learn-practice-content").value===${JSON.stringify(practiceText)}`),"Export asks the member to save typed practice text and preserves it");
  await click('[data-action="learn-restore"]');await present("#confirm-action");
  await click('[data-action="dialog-close"]');
  assert(await evaluate(`document.querySelector("#learn-practice-content").value===${JSON.stringify(practiceText)}`),"Cancelling Reload keeps the member’s unsaved practice text");

  await click('[data-action="learn-edit"]');await done();
  assert(session.practice.content===practiceText&&writes.some((r)=>r.data.action==="edit"&&r.data.step_id==="step-1"),"Guide me saves the member’s prepared practice edit with its step and revision");
  await click('[data-action="learn-next"]');await present('[data-action="learn-narrate"]');
  slowNarration=true;await click('[data-action="learn-narrate"]');await delay(80);
  await click('[data-action="learn-pause"]');await present('[data-action="learn-resume"]');await delay(1000);
  assert(await evaluate("!!document.querySelector('[data-action=\"learn-resume\"]')&&!document.querySelector('[data-action=\"learn-perform\"]')"),"Pause remains available during narration and a late reply cannot overwrite it");
  await click('[data-action="learn-resume"]');await present('[data-action="learn-perform"]');
  await fill("#learn-question","Why is add different from commit?");
  await submit("#learn-question-form");await waitFor("document.querySelector('.learn-session').textContent.includes('Add stages a change')");
  assert(await evaluate("!globalThis.badAnswer&&!globalThis.badReceipt"),"Question answers and action receipts render as text");
  await click('[data-action="learn-handoff"][data-kind="write-docs"]');await present("#task-repo");
  assert(await evaluate("document.querySelectorAll('input[name=\"task-kind\"]').length===2&&document.querySelector('input[name=\"task-kind\"]:checked').value==='write-docs'"),"A learning handoff offers only reviewed documentation or issue drafts");
  await click('[data-action="wizard-next"]');await present("#task-account");
  assert(await evaluate("document.querySelector('#task-account').disabled&&document.querySelector('#task-browse').disabled"),"Handoff keeps the lesson’s account and results folder");
  await click('[data-action="wizard-next"]');await present("#queue-task");await click("#queue-task");
  await waitFor("!document.querySelector('#app-dialog').open");
  assert(writes.some((r)=>r.path==="/api/jobs"&&r.data.learning_session_id==="session-1"&&r.data.output_dir==="/home/example/lessons"),"The ordinary task queue receives the learning session identity and reviewed destination");
  await evaluate("location.hash='learn'");await present('[data-action="learn-export"]');
  await click('[data-action="learn-export"]');await delay(100);
  await click('[data-action="learn-end"]');await present("#confirm-action");await click("#confirm-action");
  await present('[data-action="learn-new"]');
  assert(session.status==="ended"&&writes.some((r)=>r.data.action==="export"),"Export and End are separate explicit actions with a practice-file warning");
  await click('[data-action="learn-new"]');await present("#learn-start-form");
  await click('input[name="learn-mode"][value="show"]');await submit("#learn-start-form");
  await present('[data-action="learn-narrate"]');
  await click('[data-action="learn-narrate"]');await present('[data-action="learn-run"]');
  await click('[data-action="learn-run"]');await done();
  assert(session.mode==="show"&&session.receipts.length===1,"Show me performs exactly one narrated action");
  await evaluate("globalThis.oldLearningDocument=true");
  await send("Page.reload");await waitFor("!globalThis.oldLearningDocument&&!!document.querySelector(\'[data-action=\"learn-next\"]\')");
  assert(await evaluate("document.querySelector('.learn-session').textContent.includes('Step completed')&&!!document.querySelector('[data-action=\"learn-next\"]')"),"Reload resumes the saved session at the same deliberate stopping point");
  await screenshot("learning-desktop");
  await send("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(150);
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector('.learn-file-editor')!==null"),"The mobile workspace fits its viewport with a visible practice-file inspector");
  await screenshot("learning-mobile");
  await click("#explore-tab-shortcuts");
  await send("Input.dispatchKeyEvent",{type:"keyDown",key:"Home",code:"Home"});
  await send("Input.dispatchKeyEvent",{type:"keyUp",key:"Home",code:"Home"});
  assert(await evaluate("document.activeElement?.id==='explore-tab-browse'"),"Tab navigation supports keyboard arrow, Home and End controls");
  await click('[data-action="new-task"]');await present('input[name="task-kind"][value="new-project"]');
  await click('input[name="task-kind"][value="new-project"]');
  await fill("#task-field-name","practice-project");await fill("#task-field-idea","A small private project");
  await click('[data-action="wizard-next"]');await present("#task-account");
  assert(await evaluate("!document.querySelector('#task-target')"),"New-project setup needs neither an existing repository nor a fabricated issue");
  await click("#dialog-close");
  await click('[data-action="new-task"]');await present("#task-repo");
  await fill("#task-repo","example/project","change");
  await click('input[name="task-kind"][value="cleanup-merged"]');
  await click('[data-action="wizard-next"]');await present("#task-target");
  await waitFor("document.querySelector('#task-target').options.length>1");
  await fill("#task-target","12","change");
  await waitFor("Array.from(document.querySelectorAll('.selection-summary')).some((e)=>e.textContent.includes('Finished change'))");
  await click('[data-action="wizard-next"]');await present("#task-account");
  assert(reads.some((r)=>r.includes('kind=cleanup-merged')&&r.includes('state=closed')),"Finished-work tasks can select an already merged pull request and continue");
  await click("#dialog-close");
  console.log("BrowserDiagnosticErrors",JSON.stringify(errors));
  assert(errors.filter((e)=>e.source!=="network"||!/status of (409|503)\b/.test(e.text||"")).length===0,"No unexpected JavaScript or network errors in the learning flows");
  await writeFile(join(outputDir,"learning-report.json"),JSON.stringify({status:"passed",checks,writes:writes.map((r)=>({path:r.path,action:r.data.action,mode:r.data.mode})),errors},null,2));
  console.log("Learning UI checks passed:",checks.length);
  }
}finally{
  await send("Browser.close").catch(()=>{});
  ws.close();chrome.kill();await rm(profile,{recursive:true,force:true,maxRetries:2});
}
