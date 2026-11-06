() => {
 const issues={overlaps:[],clipped:[],contrast:[],borders:[],emoji:[],overflow:[],tapTargets:[]};
 const visible=e=>{for(let p=e.parentElement;p;p=p.parentElement){if(p.matches('details:not([open])')&&!p.querySelector(':scope > summary')?.contains(e))return false;}const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&r.right>0&&r.left<innerWidth&&s.visibility==='visible'&&s.display!=='none'&&!e.closest('[inert],[hidden]')};
 const name=e=>(e.id?'#'+e.id:e.tagName.toLowerCase()+'.'+String(e.className).replaceAll(' ','.'))+' '+(e.innerText||e.value||'').trim().slice(0,80);
 const controls='button,a,input,select,[role="tab"],.badge,.pill';
 const sections='main,header,footer,article,.dashboard-grid,.quick-actions,.metrics,.workspace-fleet-status,.fleet-window-filter,.fleet-tabs,.fleet-explainer,section,.panel,.metric,.repo-card,.fleet-slot,.fleet-provider-group,.help-card,.quick-action,.learn-session,.explore-tool';
 function rect(e,shadow=false){const r=e.getBoundingClientRect();let p=0;if(shadow){const s=getComputedStyle(e).boxShadow;if(s!=='none'&&!s.includes('inset')) {const nums=s.replace(/rgba?\([^)]+\)/g,'').match(/-?[\d.]+px/g)||[];p=Math.max(0,...nums.map(x=>Math.abs(parseFloat(x))));}}const style=getComputedStyle(e);if(style.outlineStyle!=='none')p=Math.max(p,parseFloat(style.outlineWidth)+Math.max(0,parseFloat(style.outlineOffset)));return {left:r.left-p,right:r.right+p,top:r.top-p,bottom:r.bottom+p};}
 for(const selector of [controls,sections]) {
  for(const parent of document.querySelectorAll('body *')) {
   const children=[...parent.children].filter(e=>e.matches(selector)&&visible(e));
   for(let i=0;i<children.length;i++)for(let j=i+1;j<children.length;j++){
    const a=rect(children[i],true),b=rect(children[j],true);
    const w=Math.min(a.right,b.right)-Math.max(a.left,b.left),h=Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top);
    if(w>0&&h>0)issues.overlaps.push({a:name(children[i]),b:name(children[j]),area:w*h});
   }
  }
 }
 for(const e of document.querySelectorAll('button,a[role="tab"],[role="tab"],.badge,.pill,h1,h2,h3,h4')){
  if(!visible(e))continue; const s=getComputedStyle(e),r=e.getBoundingClientRect();
  if(e.tagName==='BUTTON'&&e.disabled)continue;
  if(s.textOverflow!=='ellipsis'&&(e.scrollWidth>e.clientWidth+1||e.scrollHeight>e.clientHeight+1))issues.clipped.push({el:name(e),reason:'content dimensions',scroll:[e.scrollWidth,e.scrollHeight],client:[e.clientWidth,e.clientHeight]});
  // Check each text line against overflow clipping ancestors, not just page width.
  const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);let t;
  while(t=walker.nextNode()){
   if(!t.textContent.trim())continue;const range=document.createRange();range.selectNodeContents(t);
   for(const line of range.getClientRects())for(let p=e;p&&p!==document.body;p=p.parentElement){
    const ps=getComputedStyle(p);if(!/(hidden|clip)/.test(ps.overflowX+' '+ps.overflowY)||ps.textOverflow==='ellipsis')continue;
    const q=p.getBoundingClientRect();if(line.left<q.left-1||line.right>q.right+1||line.top<q.top-1||line.bottom>q.bottom+1)issues.clipped.push({el:name(e),container:name(p),reason:'clipped text line'});
   }
  }
 }
 const rgb=s=>{const n=s.match(/[\d.]+/g)||[];return [+(n[0]||0),+(n[1]||0),+(n[2]||0),n.length>3?+n[3]:1]};
 const blend=(f,b)=>[...f.slice(0,3).map((v,i)=>v*f[3]+b[i]*(1-f[3])),1];
 const bg=e=>{let layers=[];for(let p=e;p;p=p.parentElement)layers.push(rgb(getComputedStyle(p).backgroundColor));return layers.reverse().reduce((b,f)=>blend(f,b),[255,255,255,1])};
 const lum=c=>c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
 const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
 for(const e of document.querySelectorAll('body *')) {
  if(!visible(e)||e.disabled||e.closest('svg')||!Array.from(e.childNodes).some(n=>n.nodeType===3&&n.textContent.trim()))continue;
  const s=getComputedStyle(e);if(Number(s.opacity)<1)continue;
  const background=bg(e),foreground=blend(rgb(s.color),background),contrast=ratio(foreground,background),large=parseFloat(s.fontSize)>=24||(parseFloat(s.fontSize)>=18.66&&parseInt(s.fontWeight)>=700),min=large?3:4.5;
  if(contrast<min-.01)issues.contrast.push({el:name(e),ratio:+contrast.toFixed(2),min,color:s.color,bg:background});
 }
 for(const e of document.querySelectorAll('button,input:not([type="checkbox"]):not([type="radio"]),select,textarea')){
  if(!visible(e)||e.disabled)continue;const s=getComputedStyle(e);if(parseFloat(s.borderTopWidth)===0)continue;
  // Filled buttons need a contrasting silhouette, outlined fields need a contrasting edge.
  const outside=bg(e.parentElement),fill=bg(e),border=blend(rgb(s.borderTopColor),outside);
  const value=Math.max(ratio(border,outside),ratio(fill,outside));
  if(value<2.99&&s.borderTopStyle!=='none')issues.borders.push({el:name(e),ratio:+value.toFixed(2)});
 }
 if(/[\p{Extended_Pictographic}]/u.test(document.body.innerText))issues.emoji.push('Pictographic character in rendered copy');
 if(document.documentElement.scrollWidth>innerWidth)issues.overflow.push([document.documentElement.scrollWidth,innerWidth]);
 for(const e of document.querySelectorAll('button,select,textarea,input:not([type="checkbox"]):not([type="radio"])')) {
  if(!visible(e)||e.disabled)continue;
  const r=e.getBoundingClientRect();
  if(r.width<44||r.height<44)issues.tapTargets.push({el:name(e),width:r.width,height:r.height});
 }
 return issues;
}
