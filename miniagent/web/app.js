'use strict';
// Buildless local UI. All runtime-owned text passes through escapeHTML; no raw
// model HTML, remote resources or provider credentials enter this application.
const icons = {
 down:'M12 4v14m-6-6 6 6 6-6',
 messages:'M21 13a3 3 0 0 1-3 3H9l-6 4v-5a3 3 0 0 1-1-2V6a3 3 0 0 1 3-3h13a3 3 0 0 1 3 3Z',user:'M20 21v-2a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
 hash:'M10 3 7 21M17 3l-3 18M4 9h16M3 15h16',pin:'m15 3 6 6-3 1-3 4v3l-2 1-7-7 1-2h3l4-3ZM9 15l-6 6',pinFilled:'m15 3 6 6-3 1-3 4v3l-2 1-7-7 1-2h3l4-3ZM9 15l-6 6m6-12 6 6',
 plus:'M12 5v14M5 12h14',grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
 layers:'m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5M3 17l9 5 9-5',users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
 book:'M4 3h14a2 2 0 0 1 2 2v16H6a3 3 0 0 1-3-3V5a2 2 0 0 1 2-2M3 17h17M7 7h9M7 10h6',cpu:'M6 6h12v12H6zM9 9h6v6H9zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3',
 menu:'M4 6h16M4 12h16M4 18h16',workspace:'M3 4h18v16H3zM3 9h18M9 9v11',refresh:'M20 7a8 8 0 0 0-13-3L3 8m0-5v5h5M4 17a8 8 0 0 0 13 3l4-4m0 5v-5h-5',
 x:'m6 6 12 12M6 18 18 6',arrow:'M5 12h14m-5-5 5 5-5 5',left:'M19 12H5m5-5-5 5 5 5',chevron:'m9 5 7 7-7 7',check:'m5 12 4 4L19 6',circle:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
 checkcircle:'M22 11v1a10 10 0 1 1-5.9-9.1M22 4 12 14l-3-3',clock:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0M12 6v6l4 2',file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8M8 17h5',
 terminal:'m4 6 6 6-6 6M12 18h8',branch:'M6 3v12a4 4 0 1 0 4 4M18 3v5a7 7 0 0 1-7 7H6M9 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0M21 3a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
 tune:'M4 7h9M17 7h3M4 17h3M11 17h9M13 4v6M7 14v6',spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4M18 4h4',
 shield:'m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7Zm-4 9 3 3 5-6',download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',search:'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0m-2 5 6 6',
 bolt:'m13 2-9 12h7l-1 8 10-12h-7Z',folder:'M3 5h6l2 3h10v12H3z',info:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0M12 11v6M12 7v.1',link:'m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0'
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[name] || icons.circle}"></path></svg>`;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHTML;
const labels = {completed:'已完成',passed:'通过',succeeded:'已完成',running:'运行中',queued:'排队中',verifying:'验收中',created:'已创建',ready:'待执行',failed:'失败',error:'错误',cancelled:'已取消',cancel_requested:'正在取消',unverified:'未验证',interrupted:'待核实',blocked:'受阻',waiting_user:'等待回复',ended:'已结束'};
const modeLabels = {demo:'单 Agent 演示','team-demo':'协作演示',single:'单 Agent',team:'多 Agent 协作',cli:'CLI 运行',imported:'历史 CLI 运行'};
const state = {config:null,chats:null,runs:[],detail:null,route:'assistant',runId:null,tab:'overview',search:'',filter:'all',logFilter:'all',selectedFile:null,fileData:null,expanded:new Set(),logOpen:new Set(),polling:false,online:true,initialized:false,detailSignature:'',submission:null,cancelling:new Set(),downloads:new Set()};
let toastTimer;
const $ = selector => document.querySelector(selector);
const main = $('#main');
// Keep live views mounted. Polls reconcile existing nodes, so elapsed time and
// new log entries do not restart page animations or discard browser state.
function nodeKey(node){return node.nodeType===1?(node.getAttribute('data-key')||node.id||null):null;}
function sameNode(a,b){return a.nodeType===b.nodeType&&a.nodeName===b.nodeName&&nodeKey(a)===nodeKey(b);}
function reconcileNode(current,next){
 if(current.isEqualNode(next))return;
 if(current.nodeType===3||current.nodeType===8){if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;return;}
 if(current.nodeType!==1)return;
 // An expanded disclosure belongs to the reader, including acceptance receipts.
 const preserveOpen=current.tagName==='DETAILS';
 for(const attribute of [...current.attributes]){
  if(preserveOpen&&attribute.name==='open')continue;
  if(!next.hasAttribute(attribute.name))current.removeAttribute(attribute.name);
 }
 for(const attribute of [...next.attributes]){
  if(preserveOpen&&attribute.name==='open')continue;
  if(current.getAttribute(attribute.name)!==attribute.value)current.setAttribute(attribute.name,attribute.value);
 }
 reconcileChildren(current,next);
}
function reconcileChildren(current,next){
 const keyed=new Map([...current.childNodes].map(node=>[nodeKey(node),node]).filter(([key])=>key!==null));
 let cursor=current.firstChild;
 for(const wanted of [...next.childNodes]){
  const key=nodeKey(wanted);
  let node=key===null?(cursor&&sameNode(cursor,wanted)?cursor:null):keyed.get(key);
  if(node&&!sameNode(node,wanted))node=null;
  if(node){
   keyed.delete(key);
   if(node!==cursor)current.insertBefore(node,cursor);
   reconcileNode(node,wanted);
  }else{
   node=wanted.cloneNode(true);
   current.insertBefore(node,cursor);
  }
  cursor=node.nextSibling;
 }
 while(cursor){const unused=cursor;cursor=cursor.nextSibling;unused.remove();}
}
function patchHTML(container,html){
 if(!container)return;
 const template=document.createElement('template');
 template.innerHTML=html;
 reconcileChildren(container,template.content);
}
function setText(node,value){const text=String(value);if(node&&node.textContent!==text)node.textContent=text;}
function eventEntries(events){
 const occurrences=new Map();
 return events.map(event=>{
  // The trace window can slide; list position is not a persistent event identity.
  const encoded=JSON.stringify(event);let hash=2166136261;
  for(let i=0;i<encoded.length;i++)hash=Math.imul(hash^encoded.charCodeAt(i),16777619);
  const identity=`${event.ts||event.iso||''}:${event.kind||''}:${event.task_id||''}:${hash>>>0}`;
  const duplicate=occurrences.get(identity)||0;occurrences.set(identity,duplicate+1);
  return {event,key:`${identity}:${duplicate}`};
 });
}
function hydrateIcons(root=document){root.querySelectorAll('[data-icon]').forEach(n=>{n.innerHTML=icon(n.dataset.icon);n.removeAttribute('data-icon');});}
function stateIcon(status){return ['completed','passed','succeeded'].includes(status)?'checkcircle':['failed','error','cancelled'].includes(status)?'x':status==='running'||status==='verifying'?'bolt':'circle';}
function badge(status){return `<span class="badge ${e(status)}">${icon(stateIcon(status))}${e(labels[status] || status || '未知')}</span>`;}
function dateText(value){if(!value)return '时间未记录';const d=new Date(value);return Number.isNaN(d.getTime())?'时间未记录':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
function clockText(value){const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleTimeString('zh-CN',{hour12:false});}
function duration(value){return typeof value==='number'?value<1000?`${Math.round(value)} ms`:`${(value/1000).toFixed(1)} s`:'—';}
function sizeText(value){return value<1024?`${value} B`:`${(value/1024).toFixed(1)} KB`;}
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('#toast').hidden=true;},4000);}
async function api(path,options={}){const response=await fetch(path,{...options,headers:{'Accept':'application/json',...(options.body?{'Content-Type':'application/json','X-Studio-Token':state.config?.csrf_token || ''}:{}),...options.headers}});let data;try{data=await response.json();}catch{throw new Error('服务未返回可读取的数据，请检查本地服务。');}if(!response.ok){const err=new Error(typeof data.error==='string'?data.error:'请求失败，请稍后重试。');err.code=data.code;err.status=response.status;throw err;}return data;}
function setOnline(value){state.online=value;$('#connection-banner').hidden=value;patchHTML($('#server-status'),`<span class="dot"></span>${value?'本地服务':'重新连接中'}`);}
function mobileNav(){const open=$('#sidebar').classList.contains('open');$('#sidebar').inert=window.matchMedia('(max-width:650px)').matches&&!open;const button=$('[data-action="menu"]');button.setAttribute('aria-expanded',String(open));}
function updateSidebar(){mobileNav(); setText($('#nav-count'),state.runs.length);chatDirectory.sidebar();document.querySelectorAll('[data-nav]').forEach(n=>{const active=n.dataset.nav===state.route || (state.route==='detail'&&n.dataset.nav===(state.tab==='chat'?'chats':'runs'));n.classList.toggle('active',active);if(active)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});$('#breadcrumb-current').textContent=state.route==='assistant'?'统一对话':state.route==='detail'?'任务详情':state.route==='runs'?'全部任务':state.route==='chats'?'最近对话':state.route==='roles'?'角色库':'工作台';if(state.config){$('#provider-label').textContent=state.config.provider_ready?'DeepSeek 已配置':'离线模式就绪';$('#provider-caption').textContent=state.config.provider_ready?state.config.model:'无需 API key，即可体验';$('#provider-dot').classList.toggle('ready',state.config.provider_ready);}}
const runLimitFields = [
 {name:'max_steps',label:'执行轮数上限（0 为不限）',value:0,min:0,max:1000},
 {name:'max_total_tokens',label:'累计 token 上限（0 为不限）',value:0,min:0,max:100000000},
 {name:'max_seconds',label:'时间预算（秒）',value:3600,min:1,max:86400}
];
function runActive(run){return ['running','queued','verifying','waiting_user','cancel_requested'].includes(run?.status);}
function runStatus(run){return runActive(run)&&(run.cancel_requested||state.cancelling.has(run.id))?'cancel_requested':run.status;}
function limitFields(demo, values={}) {
 const inputs = runLimitFields.map(field => {
  const value = values[field.name] ?? state.config?.limits?.[field.name] ?? field.value;
  return `<label class="field-label">${field.label}<input type="number" name="${field.name}" value="${e(value)}" min="${field.min}" max="${field.max}" step="1" required ${demo?'disabled':''}></label>`;
 }).join('');
 return `<fieldset class="run-limits"><legend>本次执行预算</legend><div class="run-limit-grid">${inputs}</div>
  <p class="muted-note">轮数和累计 token 设为 0 时继续计量。时间预算仍生效。${demo?'':' <button type="button" class="text-button" data-action="unlimit-steps">不限轮数</button> <button type="button" class="text-button" data-action="unlimit-tokens">不限累计 token</button>'}</p></fieldset>`;
}
const chatDrafts = new Map();
const chatSubmissions = new Map();
const pendingChat = new Set();
const chatRoles = () => [...(state.config?.conversation_roles || []),
 ...(state.config?.custom_profiles || []).map(profile => ({id:profile.id,label:profile.name}))];
const roleLabel = id => state.detail?.conversation?.profiles?.find(p=>p.id===id)?.name || chatRoles().find(role => role.id === id)?.label || id;
function memberLink(thread,id) {
 const key=thread?.role_keys?.[id];
 const name=thread?.member_names?.[id] || thread?.profiles?.find(profile=>profile.id===id)?.name || chatRoles().find(role=>role.id===id)?.label || id;
 return key ? `<a class="member-profile" href="${roleLink(key)}">${e(name)}</a>` : e(name);
}
const chatMode = mode => mode === 'single' || mode === 'group';
const chatLink = id => '#run/' + encodeURIComponent(id) + '/chat';
const latestChatLink = id => chatLink(id) + '?latest=1';

const databaseFile = path => /\.(db|sqlite|sqlite3)$/i.test(path);
function fileURL(runId, path, endpoint) {
 return '/api/runs/' + encodeURIComponent(runId) + '/' + endpoint + '?' + new URLSearchParams({path});
}
function artifactLink(runId, file) {
 const path = file.path;
 const preview = /\.(?:html|png)$/i.test(path);
 const binary = databaseFile(path);
 const href = preview || binary ? fileURL(runId, path, preview ? 'preview' : 'download')
  : '#run/' + encodeURIComponent(runId) + '/artifacts?' + new URLSearchParams({path});
 const markdownFile = /\.md$/i.test(path);
 const attrs = markdownFile ? StudioMarkdown.attributes({run:runId, path, size:file.size, kind:file.kind}) : `href="${e(href)}"`;
 const action = preview ? '预览' : binary ? '下载' : '查看';
 return `<a class="turn-file" ${attrs} ${preview?'target="_blank" rel="noopener noreferrer"':binary?'download':''}
  title="${e(file.project_path||path)}${file.sha256?' · SHA-256 '+e(file.sha256):''}">${icon(binary?'download':preview?'link':'file')}<span>${e(file.project_path || path)}</span><small>${action} · ${e(sizeText(file.size))}</small></a>${window.StudioActivity?.preview(runId,file)||''}`;
}
function turnFiles(turn, collapsed=false) {
 const files = turn.artifacts || [];
 const delivered = turn.workspace_delivery;
 const status = delivered?.status==='published' ? `已写入本地工作区 · ${delivered.files.length} 个文件`
  : delivered?.status==='failed' ? delivered.message : '';
 if (!files.length && !status) return '';
 return `<details class="turn-files" ${!collapsed && files.length <= 5 ? 'open' : ''}><summary>本轮文件 · ${files.length}</summary>
  ${status ? `<p class="muted-note">${e(status)}</p>` : ''}
  <div>${files.map(file=>artifactLink(turn.run_id,file)).join('')}</div></details>`;
}

function roleOptions(ids, selected) {
 return ids.map(id => `<option value="${e(id)}" ${id === selected ? 'selected' : ''}>${e(roleLabel(id))}</option>`).join('');
}

function intentOptions(intent) {
 return `<option value="edit" ${intent === 'edit' ? 'selected' : ''}>执行或修改</option>
         <option value="question" ${intent === 'question' ? 'selected' : ''}>只读问答</option>`;
}

function interactionOptions(selected='autonomous') {
 return `<option value="autonomous" ${selected==='autonomous'?'selected':''}>自动完成</option>
         <option value="interactive" ${selected==='interactive'?'selected':''}>允许中途询问</option>`;
}
function interactionField(selected='autonomous') {
 return `<label class="field-label">执行方式<select name="interaction_mode">${interactionOptions(selected)}</select></label>`;
}

function dispatchField(selected='team') {
 return `<label class="field-label">协作方式<select name="dispatch">
  <option value="team" ${selected==='team'?'selected':''}>由负责人分工</option>
  <option value="single" ${selected==='single'?'selected':''}>只请指定成员处理</option></select></label>`;
}
function ruleFields(value='') {
 return `<label class="field-label">交付检查（选填）<textarea name="acceptance_checks" rows="2"
  placeholder='[{"kind":"contains","path":"report.md","text":"调查方法"}]'>${e(value)}</textarea>
  <small>JSON 数组支持 contains、sha256、json_equals。留空时可在交付后人工评审。</small></label>`;
}
function sourceReviewField(value='', enabled=true) {
 return `<label class="field-label" ${enabled?'':'hidden'}>需要独立核查来源的文件（选填）
  <input name="required_source_review_paths" value="${e(value)}" ${enabled?'':'disabled'} placeholder="例如 report.md">
  <small>由另一位成员打开原始来源核查最终版本，多个路径用逗号分隔。</small></label>`;
}
function childFields(value='', group=true) {
 return `<label class="field-label">子任务检查（按${group?'角色 ID':'规划 ID'}）<textarea name="child_checks" rows="2"
  placeholder='{"qa":{"files":["assessment.md"],"checks":[]}}'>${e(value)}</textarea></label>`;
}
function readRules(value) {
 if (!value?.trim()) return [];
 const rules=JSON.parse(value);
 if (!Array.isArray(rules)) throw new Error('交付检查须为 JSON 数组。');
 return rules;
}

function newConversationFields(mode) {
 if (!chatMode(mode)) return '';
 const roles = chatRoles().map(role => role.id);
 const members = mode === 'group' ? roles.slice(0, 2) : roles;
 const groupFields = mode === 'group' ? `<fieldset class="member-picker"><legend>群成员</legend>
  <div>${roles.map(id => `<label><input name="member" type="checkbox" value="${e(id)}" ${members.includes(id) ? 'checked' : ''}>${e(roleLabel(id))}</label>`).join('')}</div></fieldset>` : '';
 return `<div class="chat-setup">${groupFields}${mode==="group"?dispatchField():""}
  <label class="field-label">${mode === 'group' ? '负责人或回复角色' : '私聊角色'}
   <select name="recipient" required>${roleOptions(members,members[0])}</select></label>
  <label class="field-label">消息类型<select name="intent">${intentOptions('edit')}</select></label></div>`;
}

function updateMemberPicker(form) {
 const members = new FormData(form).getAll('member');
 const recipient = form.elements.recipient;
 recipient.innerHTML = roleOptions(members, members.includes(recipient.value) ? recipient.value : members[0]);
}

function syncIntent(form) {
 const field = form.elements.acceptance_files;
 if (!field) return;
 field.disabled = form.elements.intent.value === 'question';
 field.closest('label').hidden = field.disabled;
 if (form.elements.dispatch) form.elements.dispatch.disabled=field.disabled;
 const source = form.elements.required_source_review_paths;
 if (source) {
  source.disabled = field.disabled || form.elements.dispatch?.value!=='team';
  source.closest('label').hidden = source.disabled;
 }
 if (form.elements.acceptance_checks) form.elements.acceptance_checks.disabled=field.disabled;
}

const roleView = {id:null, directory:null, detail:null, error:'', serial:0, kind:'memory', filter:'current',
 section:'tasks', taskFilter:'all', search:'', checkedAt:0, runSignature:'', incomplete:[]};
const knowledgeKinds = {memory:'记忆',skill:'技能'};
const knowledgeStates = {active:'已启用',candidate:'待确认',archived:'已归档',rejected:'已拒绝'};
const roleSourceNames = {saved:'个人角色',historical:'历史会话成员',builtin:'内置角色'};
const roleLink = role => '#roles/' + encodeURIComponent(role);
const profileURL = role => '/api/profiles/' + encodeURIComponent(role);
const entryId = row => row.id || row.entry_id;

function roleMark(profile) {
 const first = Array.from(profile.name.trim())[0] || '·';
 const tone = Array.from(profile.id).reduce((n,c)=>n+c.codePointAt(0),0) % 4;
 return `<span aria-hidden="true" class="role-mark tone-${tone}">${e(first)}</span>`;
}

function roleSections() {
 if (!roleView.directory) return roleView.error
  ? `<p class="error-panel" role="alert">${e(roleView.error)} <button data-role-action="reload">重试</button></p>`
  : '<p class="muted-note">正在读取角色…</p>';
 const query=roleView.search.trim().toLocaleLowerCase();
 const matches=roleView.directory.filter(role=>[role.name,role.id,role.prompt].some(value=>value.toLocaleLowerCase().includes(query)));
 if (!matches.length) return query?empty('没有匹配的角色','试试角色名称、ID 或职责。'):empty('还没有角色','创建一位助手，开始第一次对话。');
 return Object.entries(roleSourceNames).map(([source,label])=>{
  const profiles=matches.filter(role=>role.source===source);
  if (!profiles.length) return '';
  return `<section class="directory-section" aria-label="${label}"><h2>${label} <small>${profiles.length}</small></h2>
   <div class="role-cards">${profiles.map(p=>`<a class="role-card" data-key="role-${e(p.key)}" href="${roleLink(p.key)}">
    ${roleMark(p)}<div><h3>${e(p.name)}</h3><code>${e(p.id)}</code></div>
    <p>${e(p.prompt)}</p><span class="role-card-activity">${icon('users')}${p.conversation_count?e(p.conversation_count)+' 个对话':'尚无对话'}<i aria-hidden="true">${icon('arrow')}</i></span></a>`).join('')}</div></section>`;
 }).join('');
}

function roleLibrary() {
 return `<section class="page roles-page"><header class="page-top role-page-top"><h1>角色库</h1>
  <button type="button" class="primary" data-role-action="new-role">${icon('plus')}创建角色</button></header>
  <label class="directory-search">${icon('search')}<input type="search" id="role-search" aria-label="搜索角色" placeholder="按名称、ID 或职责搜索" value="${e(roleView.search)}"></label>
  ${roleView.incomplete.length?`<p class="run-warning">有 ${e(roleView.incomplete.length)} 次运行的角色资料暂时无法读取。</p>`:''}
  <div id="directory-sections">${roleSections()}</div></section>`;
}

function roleConversations(chats) {
 if (!chats.length) return '<p class="role-empty">这个角色还没有参加项目对话。</p>';
 return `<div class="role-conversations">${chats.map(c=>`<a data-key="role-conversation-${e(c.id)}" href="${latestChatLink(c.head_run_id)}">
   ${icon(c.kind==='dm'?'cpu':'users')}<span><strong>${e(c.title || '未命名对话')}</strong><small>${[c.kind==='dm'?'私聊':'群聊', c.status?labels[c.status] || c.status:'',c.turn_count?c.turn_count+' 轮':'', c.updated_at?dateText(c.updated_at):''].filter(Boolean).map(e).join(' · ')}</small></span>
   ${icon('chevron')}</a>`).join('')}</div>`;
}

function legacyConversations(rooms) {
 if (!rooms.length) return '';
 return `<section class="legacy-rooms"><h3>可关联的旧会话</h3>
  <p>关联后，从下一轮开始使用这个角色的记忆和技能。</p>
  ${rooms.map(c=>`<div class="legacy-room" data-key="legacy-${e(c.id)}">
    <a href="${latestChatLink(c.head_run_id)}">${e(c.title)}</a><small>${c.kind==='dm'?'私聊':'群聊'}</small>
    <button type="button" class="secondary" data-role-action="bind" data-room="${e(c.id)}" data-head="${e(c.head_run_id)}">关联已有会话</button>
   </div>`).join('')}</section>`;
}

function knowledgeCard(row, conversations, latest) {
 const key = e(entryId(row));
 const label = knowledgeKinds[row.kind];
 const project = conversations.find(c=>c.project===row.project || c.id===row.project);
 const scope = row.scope==='project' ? '项目 · '+(project?.title || row.project) : '角色通用';
 const change = (status,name) => `<button type="button" class="text-button" data-role-action="entry-status" data-entry="${key}" data-status="${status}">${name}</button>`;
 const sourceRun = row.origin_run || row.source_run;
 const older = latest && row.revision!==latest.revision;
 return `<article class="knowledge-card" data-key="entry-${key}" data-entry-id="${key}"><div class="knowledge-heading">
  <h3>${e(row.title || label)}</h3><span class="knowledge-state ${e(row.status)}">${e(knowledgeStates[row.status] || row.status)}</span></div>
  <p class="knowledge-text">${e(row.text)}</p>
  <div class="entry-meta"><span title="${e(row.project)}">${e(scope)}</span><span>v${e(row.revision)}</span><span>${(row.origin_source || row.source)==='agent'?'角色建议':'用户添加'}</span>
   ${row.active_revision && row.active_revision!==row.revision?`<span>正在使用 v${e(row.active_revision)}</span>`:''}
   ${sourceRun?`<a href="${chatLink(sourceRun)}">来源对话 ${icon('link')}</a>`:''}</div>
  <div class="entry-actions">${older?`<small>最新修订 v${e(latest.revision)} · ${e(knowledgeStates[latest.status])}</small>
   <button type="button" class="text-button" data-role-action="edit-active" data-entry="${key}">编辑当前版本</button>${change('archived','归档整个条目')}`:`
   ${row.status==='candidate'?change('active','确认启用')+change('rejected','拒绝'):''}
   ${['active','candidate'].includes(row.status)?`<button class="text-button" data-role-action="edit-entry" data-entry="${key}" aria-label="编辑${e(row.title || label)}">编辑</button>`:''}
   ${row.status==='active'?change('archived','归档'):row.status==='archived'?change('active','恢复使用'):''}`}
  </div></article>`;
}

function knowledgePanel(entries, activeEntries, conversations) {
 const kind = roleView.kind;
 const matching = entries.filter(row=>row.kind===kind);
 const filters = {current:'当前',candidate:'待确认',history:'已处理'};
 const visible = roleView.filter==='current' ? activeEntries.filter(row=>row.kind===kind)
   : matching.filter(row=>roleView.filter==='history'?['archived','rejected'].includes(row.status):row.status==='candidate');
 const selected = roleView.filter;
 return `<section class="panel knowledge-panel" aria-label="记忆与技能"><div class="knowledge-controls">
  <div class="role-switch" role="group" aria-label="经验类型">${Object.entries(knowledgeKinds).map(([key,name])=>
   `<button type="button" aria-pressed="${kind===key}" data-role-action="choose-kind" data-value="${key}">${icon(key==='memory'?'book':'layers')}${name}</button>`).join('')}</div>
  <button class="secondary" data-role-action="new-entry">${icon('plus')}添加${knowledgeKinds[kind]}</button></div>
  <div class="knowledge-filters" role="group" aria-label="经验状态">${Object.entries(filters).map(([key,name])=>
   `<button type="button" aria-pressed="${selected===key}" data-role-action="choose-filter" data-value="${key}">${name}${key==='candidate'?`<span>${matching.filter(x=>x.status==='candidate').length}</span>`:''}</button>`).join('')}</div>
  <div class="knowledge-list">${visible.length?visible.map(row=>knowledgeCard(row,conversations,matching.find(x=>entryId(x)===entryId(row)))).join(''):
    `<p class="role-empty">${selected==='candidate'?'暂时没有待确认的建议。':selected==='history'?'没有已归档或拒绝的条目。':kind==='memory'?'还没有已启用的记忆。可以添加偏好、约束或长期事实。':'还没有已启用的技能。可以添加可重复使用的工作步骤。'}</p>`}</div></section>`;
}

function roleTasks(tasks, incomplete) {
 const filter=roleView.taskFilter;
 const visible=tasks.filter(task=>filter==='all' || task.kind===filter);
 const filters=[['all','全部'],['dm','私聊'],['group','群聊']];
 if (tasks.some(t=>t.kind==='run')) filters.push(['run','其他运行']);
 const participation={solo:'独立执行',lead:'负责人',member:'协作成员'};
 const contexts={dm:'私聊',group:'群聊',run:'其他运行'};
 return `<section class="panel role-tasks" aria-label="分配的任务">
  <div class="role-task-filters" role="group" aria-label="按任务来源筛选">${filters.map(([key,label])=>
   `<button class="text-button" data-role-action="task-filter" data-value="${key}" aria-pressed="${key===filter}">${label} <small>${key==='all'?tasks.length:tasks.filter(task=>task.kind===key).length}</small></button>`).join('')}</div>
  ${incomplete?.length?`<p class="run-warning">有 ${e(incomplete.length)} 次运行的任务记录暂时无法读取。</p>`:''}
  ${visible.map(task=>`<a class="role-task-row" data-key="work-${e(task.run_id)}-${e(task.task_id)}"
     href="#run/${encodeURIComponent(task.run_id)}/overview?task=${encodeURIComponent(task.task_id)}">
    <span class="role-task-text"><strong>${e(task.objective)}</strong><small>${[contexts[task.kind],
     participation[task.participation],dateText(task.updated_at),task.conversation_title].filter(Boolean).map(e).join(' · ')}</small></span>
    ${badge(task.status)}${icon('chevron')}</a>`).join('') || '<p class="role-empty">暂时没有分配给这个角色的'+(contexts[filter] || '')+'任务。</p>'}</section>`;
}

function rolePage() {
 if (!roleView.id) return roleLibrary();
 const back = `<a class="back-link" href="#roles">${icon('left')}返回角色库</a>`;
 if (!roleView.detail) return `<section class="page roles-page">${back}${roleView.error?`<p class="error-panel" role="alert">${e(roleView.error)} <button data-role-action="reload">重试</button></p>`:'<p class="muted-note">正在读取角色…</p>'}</section>`;
 const {role,tasks,conversations,entries,active_entries,unbound_conversations,incomplete_run_ids}=roleView.detail;
 const dm=conversations.find(c=>c.kind==='dm');
 const personal=role.source==='saved', historical=role.source==='historical';
 const lastGroup=historical && !dm && conversations.find(room=>room.kind==='group');
 const openDM=historical?'':`<button type="button" class="${dm?'secondary':'primary'}" data-role-action="start-dm">${icon('plus')}新建私聊</button>`;
 const sections=[['tasks','分配的任务',tasks.length],['chats','参与的对话',conversations.length]];
 if (personal) sections.push(['knowledge','记忆与技能',null]);
 let section;
 if (roleView.section==='chats') section=`<section class="panel role-projects">${roleConversations(conversations)}${legacyConversations(unbound_conversations || [])}</section>`;
 else if (personal && roleView.section==='knowledge') section=knowledgePanel(entries,active_entries,conversations);
 else section=roleTasks(tasks,incomplete_run_ids);
 return `<section class="page roles-page">${back}<header class="role-detail-head"><div class="role-title">${roleMark(role)}
  <div><h1>${e(role.name)}</h1><div class="role-identity"><span>${e(roleSourceNames[role.source])}</span><code>${e(role.id)}</code></div></div></div>
  <div class="chat-actions">${dm?`<a class="primary" href="${latestChatLink(dm.head_run_id)}">继续最近私聊 ${icon('arrow')}</a>`:''}${openDM}
   ${lastGroup?`<a class="primary" href="${latestChatLink(lastGroup.head_run_id)}">回到最近群聊 ${icon('arrow')}</a>`:''}
   ${historical?`<button type="button" class="secondary" data-role-action="save-historical">${icon('plus')}保存为个人角色</button>`:''}</div></header>
  <section class="role-brief"><div><h2>角色职责</h2>${personal?`<button class="text-button" data-role-action="edit-role">${icon('tune')}编辑角色</button>`:''}</div>
   <p>${e(role.prompt)}</p></section>
  <div class="role-sections" role="group" aria-label="角色详情">${sections.map(([key,label,count])=>
   `<button data-role-action="role-section" data-value="${key}" aria-pressed="${roleView.section===key}">${label}${count!==null?`<small>${count}</small>`:''}</button>`).join('')}</div>
  ${section}</section>`;
}

function roleRunSignature() {
 return state.runs.map(r=>[r.id,r.status,r.task_count,r.completed_count].join('/')).join(',');
}

async function refreshRoleIfNeeded() {
 if (state.route!=='roles' || !roleView.checkedAt) return;
 const age=Date.now()-roleView.checkedAt;
 // Active assignees may advance within a turn without changing the summary
 // counts. Idle directory changes in another tab also become visible.
 if (roleView.runSignature!==roleRunSignature() || age>=(state.runs.some(runActive)?6000:30000)) {
  await loadRole();
 }
}

async function loadRole() {
 roleView.runSignature=roleRunSignature();
 roleView.checkedAt=Date.now();
 const id=roleView.id;
 const serial=++roleView.serial;
 try {
  const data=await api(id ? '/api/directory/roles/'+encodeURIComponent(id) : '/api/directory/roles');
  if (state.route!=='roles' || serial!==roleView.serial) return;
  if (id && data.role.source==='saved') {
   const library=await api(profileURL(data.role.id)+'/knowledge');
   if (state.route!=='roles' || serial!==roleView.serial) return;
   const projects=new Map(library.conversations.map(c=>[c.id,c.project]));
   roleView.detail={...data,entries:library.entries,active_entries:library.active_entries,
    unbound_conversations:library.unbound_conversations,conversations:data.conversations.map(c=>({...c,project:projects.get(c.id)}))};
   const profiles=state.config.custom_profiles || [];
   state.config.custom_profiles=[...profiles.filter(p=>p.id!==id),library.role];
  } else if (id) roleView.detail=data;
  else {
   roleView.directory=data.roles;
   roleView.incomplete=data.incomplete_run_ids || [];
   // Task forms accept saved IDs only. Historic snapshots can share an ID with
   // a different personal role and must never silently bind to that identity.
   state.config.custom_profiles=data.roles.filter(role=>role.source==='saved').map(({id,name,prompt})=>({id,name,prompt}));
  }
  roleView.error='';
 } catch (error) {
  if (state.route!=='roles' || serial!==roleView.serial) return;
  roleView.error=error.message;
  if (roleView.detail || (!id && roleView.directory)) toast(error.message);
 }
 render();
}

function openRoleEditor(type, entry) {
 const profile = roleView.detail?.role;
 const copy=type==='new-role' && entry?.source==='historical' ? entry : null;
 const profileForm = type==='new-role' || type==='edit-role';
 const editing = type==='edit-role' || type==='edit-entry';
 const kind = entry?.kind || roleView.kind;
 const title = profileForm ? (editing?'编辑角色':'创建角色') : (editing?'编辑':'添加')+knowledgeKinds[kind];
 const formRole = (profileForm && !editing) ? '' : profile.id;
 const profileFields = () => `<label class="field-label">角色名称<input name="name" required maxlength="100" value="${e(editing?profile.name:copy?.name)}" placeholder="例如，电脑维护助手"></label>
   <label class="field-label">角色职责<textarea name="prompt" required maxlength="4000" rows="5" placeholder="它长期负责什么？哪些情况需要先和你确认？">${e(editing?profile.prompt:copy?.prompt)}</textarea></label>
   ${editing?`<div class="entry-meta">固定角色 ID <code>${e(profile.id)}</code></div>`:
    '<details class="role-id-option"><summary>自定义角色 ID（选填）</summary><label class="field-label">角色 ID<input name="id" maxlength="48" pattern="[A-Za-z][A-Za-z0-9_\\-]{0,47}" placeholder="留空以自动生成"><small>以字母开头，可使用字母、数字、下划线和连字符。</small></label></details>'}`;
 const knowledgeFields = () => {
  const projects = new Map((roleView.detail.conversations || []).map(c=>[c.project,c.title]).filter(([key])=>key));
  if (entry?.project && !projects.has(entry.project)) projects.set(entry.project,entry.project);
  return `<label class="field-label">标题（选填）<input name="title" value="${e(entry?.title)}" maxlength="160" placeholder="${kind==='memory'?'例如，软件安装偏好':'例如，检查磁盘空间'}"></label>
   <label class="field-label">${kind==='memory'?'记忆内容':'操作步骤'}<textarea name="text" required maxlength="8000" rows="6" placeholder="${kind==='memory'?'记录角色以后需要遵循的信息。':'写下适用条件、操作顺序和检查结果的方法。'}">${e(entry?.text)}</textarea></label>
   <label class="field-label">适用范围<select name="scope" ${editing?'disabled':''}><option value="role">角色通用 · 跨项目使用</option>
   <option value="project" ${entry?.scope==='project'?'selected':''} ${projects.size?'':'disabled'}>仅指定项目</option></select></label>
   <label class="field-label" data-project-choice ${entry?.scope==='project'?'':'hidden'}>项目<select name="project" ${editing?'disabled':''}>
    ${[...projects].map(([id,name])=>`<option value="${e(id)}" ${entry?.project===id?'selected':''}>${e(name)} · ${e(id)}</option>`).join('')}</select></label>`;
 };
 $('#role-dialog-title').textContent=title;
 $('#role-editor').innerHTML=`<form class="role-editor" data-type="${type}" data-role="${e(formRole)}" data-kind="${e(kind)}"
    data-entry="${e(entry && entryId(entry))}" data-revision="${e(entry?.revision)}">
   ${profileForm?profileFields():knowledgeFields()}<p class="form-error" role="alert" hidden></p>
   <div class="editor-actions"><button type="button" class="secondary" data-role-action="dismiss">取消</button>
    <button type="submit" class="primary">${profileForm?'保存角色':entry?.status==='candidate'?'保存并启用':editing?'保存修改':'保存'+knowledgeKinds[kind]}</button></div></form>`;
 if (copy) {
  const existing=chatRoles().some(r=>r.id.toLowerCase()===copy.id.toLowerCase());
  const editor=$('#role-editor form');
  if (!existing) editor.elements.id.value=copy.id;
  const note=document.createElement('p');
  note.className='muted-note';
  note.textContent=(existing?'原 ID 已被占用，将为个人角色分配独立 ID。':'保存后可从个人角色页开始私聊。')+'历史对话保留原成员记录。';
  editor.querySelector('.editor-actions').before(note);
 }
 $('#role-dialog').showModal();
 if (type==='edit-role') $('#role-editor form').previousProfile={name:profile.name,prompt:profile.prompt};
 $('#role-editor input:not([disabled])')?.focus();
}

async function saveRoleEditor(form) {
 const submit = form.querySelector('[type="submit"]');
 if (submit.disabled || !form.reportValidity()) return;
 const {type,role,kind,entry,revision} = form.dataset;
 const fields = form.elements;
 const isProfile = type.endsWith('role');
 let path, payload;
 if (isProfile) {
  payload={name:fields.name.value.trim(),prompt:fields.prompt.value.trim()};
  path=type==='new-role'?'/api/profiles':profileURL(role)+'/edit';
  if (!role) payload.id=fields.id.value.trim() || 'assistant-'+crypto.randomUUID().slice(0,8);
  else payload.previous=form.previousProfile;
 } else {
  path=profileURL(role)+'/knowledge';
  payload={kind,title:fields.title.value.trim(),text:fields.text.value.trim(),scope:fields.scope.value};
  if (payload.scope==='project') payload.project=fields.project.value;
  if (entry) payload.entry_id=entry;
 }
 if (revision) payload.expected_revision=Number(revision);
 const errorBox=form.querySelector('.form-error');
 errorBox.hidden=true;
 submit.disabled=true;
 try {
  const result = await api(path,{method:'POST',body:JSON.stringify(payload)});
  $('#role-dialog').close();
  if (isProfile) {
   const saved=result.profile;
   state.config.custom_profiles=[...(state.config.custom_profiles||[]).filter(p=>p.id!==saved.id),saved];
   if (roleView.id!==saved.id || state.route!=='roles') location.hash=roleLink(saved.id);
   else await loadRole();
  } else if (state.route==='roles' && roleView.id===role) await loadRole();
  toast(isProfile?'角色已保存。':'已保存。');
 } catch (error) {
  if (error.status===409 && role && state.route==='roles' && role===roleView.id) await loadRole();
  errorBox.textContent=error.message+(error.status===409 && (revision || type==='edit-role')?' 请保留修改内容，关闭窗口后重新打开最新版本。':'');
  errorBox.hidden=false;
 } finally {submit.disabled=false;}
}

async function runRoleAction(button) {
 const action=button.dataset.roleAction;
 if (action==='dismiss') {$('#role-dialog').close();return;}
 if (action==='reload') {await loadRole();return;}
 if (action==='role-section' || action==='task-filter') {
  if (action==='role-section') roleView.section=button.dataset.value;
  else roleView.taskFilter=button.dataset.value;
  render();return;
 }
 if (action==='save-historical') {openRoleEditor('new-role',roleView.detail.role);return;}
 if (action==='new-role' || action==='edit-role' || action==='new-entry') {openRoleEditor(action);return;}
 if (action==='choose-kind' || action==='choose-filter') {
  if (action==='choose-kind') roleView.kind=button.dataset.value;
  else roleView.filter=button.dataset.value;
  render();return;
 }
 if (action==='start-dm') {
  openForm('single');
  $('#dialog-form [name="recipient"]').value=roleView.id;
  return;
 }
 if (action==='bind' && !button.disabled) {
  const id=roleView.id;
  button.disabled=true;
  try {
   await api(profileURL(id)+'/bind',{method:'POST',body:JSON.stringify({
    room_id:button.dataset.room,expected_head:button.dataset.head
   })});
   if (state.route==='roles' && roleView.id===id) await loadRole();
   toast('会话已关联，下一轮将使用这个角色的记忆和技能。');
  } catch (error) {
   if (state.route==='roles' && roleView.id===id) await loadRole();
   toast(error.message);
  } finally {button.disabled=false;}
  return;
 }
 const row=roleView.detail?.entries?.find(item=>entryId(item)===button.dataset.entry);
 if (!row) return;
 if (action==='edit-active') {
  const active=roleView.detail.active_entries.find(x=>entryId(x)===entryId(row));
  if (active) openRoleEditor('edit-entry',{...active,revision:row.revision});
  return;
 }
 if (action==='edit-entry') {openRoleEditor(action,row);return;}
 if (action==='entry-status' && !button.disabled) {
  const id=roleView.id;
  button.disabled=true;
  try {
   await api(profileURL(id)+'/knowledge/'+encodeURIComponent(entryId(row))+'/status',
    {method:'POST',body:JSON.stringify({status:button.dataset.status,expected_revision:row.revision})});
   if (state.route==='roles' && roleView.id===id) await loadRole();
   toast('已更新条目。');
  } catch (error) {
   if (error.status===409 && state.route==='roles' && roleView.id===id) await loadRole();
   toast(error.message);
  } finally {button.disabled=false;}
 }
}

function chatTransitionNotice(thread, run) {
 if (thread.head_run_id !== run.id) {
  return `<div class="chat-notice"><span>你正在查看较早的一轮。</span>
   <a class="secondary" href="${chatLink(thread.head_run_id)}">继续最新对话 ${icon('arrow')}</a></div>`;
 }
 if (run.status==='waiting_user') return '';
 if (runActive(run)) return '<p class="chat-notice" role="status">当前角色正在执行。结束后可在这里继续对话。</p>';
 if (!thread.can_continue) return '<p class="chat-notice">当前运行的执行状态有待核实，暂时无法继续。</p>';
 return '';
}

function conversationComposer(thread,run) {
 if (run.status==='waiting_user' && thread.head_run_id===run.id) return '';
 if (thread.kind==='group' && !thread.members.length && thread.head_run_id===run.id && !runActive(run)) return '<p class="chat-notice">添加群成员后即可发送消息。<button type="button" class="text-button" data-edit-members>添加成员</button></p>';
 const unavailable = chatTransitionNotice(thread,run);
 if (unavailable) return unavailable;
 const draft = chatDrafts.get(thread.id) || {};
 const last = thread.turns.at(-1);
 const dispatch = draft.dispatch || (thread.kind==='group' ? 'team' : 'single');
 const status = pendingChat.has(thread.id);
 const intent = draft.intent || 'edit';
 const teamLead = dispatch==='team' && intent==='edit' ? thread.lead : null;
 const recipient = teamLead || [draft.recipient,last?.recipient,thread.recipient].find(id=>thread.members.includes(id)) || thread.members[0];
 const hasProvider = state.config?.provider_ready;
 return `<form class="reply-form" data-key="composer-${e(thread.id)}" data-thread="${e(thread.id)}" data-parent="${e(run.id)}">
   <fieldset ${status ? 'disabled' : ''}>
    <label class="composer-label" for="chat-reply">继续对话</label>
    <textarea id="chat-reply" name="message" maxlength="8000" required placeholder="${thread.kind==='group'?'发送群消息，或在开头 @角色 点名处理…':'提出问题，或描述下一步要修改的地方…'}">${e(draft.message || '')}</textarea>
    <div class="reply-options">
     ${thread.kind==='group' && intent==='edit'?dispatchField(dispatch):''}
     <label class="field-label"><span id="reply-recipient-label">${teamLead?'负责人':'回复角色'}</span>
      <select name="recipient" aria-labelledby="reply-recipient-label" ${teamLead || thread.kind==='dm' ? 'disabled' : ''}>
      ${roleOptions(teamLead?[teamLead]:thread.members,recipient)}</select></label>
     <label class="field-label">消息类型<select name="intent">${intentOptions(intent)}</select></label>
     <label class="field-label" ${intent === 'question' ? 'hidden' : ''}>本轮验收文件（选填）
      <input name="acceptance_files" ${intent === 'question' ? 'disabled' : ''} placeholder="例如 snake/game.js" value="${e(draft.files || '')}"></label>
    </div>
    <details class="studio-disclosure"><summary>本轮选项与预算</summary>
     ${interactionField(draft.interactionMode || (last ? run.interaction_mode || 'interactive' : 'autonomous'))}
     ${intent==='edit'?`<label class="field-label">项目文件<select name="base">
       <option value="published" ${draft.base==='published'?'selected':''}>沿用最近发布版本</option>
       ${run.status==='failed' && last?.intent==='edit'?`<option value="failed" ${draft.base==='failed'?'selected':''}>接着本轮未完成文件</option>`:''}
       ${(thread.recovery_candidates || []).map(c => {
         const key = 'checkpoint:' + c.task_id;
         return `<option value="${e(key)}" ${draft.base===key?'selected':''}>成员草稿 · ${e(roleLabel(c.role))} · ${e(c.objective)} (${e(c.task_id.slice(-8))})</option>`;
       }).join('')}
       <option value="empty" ${draft.base==='empty'?'selected':''}>新任务，使用空工作区</option></select></label>
       ${ruleFields(draft.rules||'')}${thread.kind==='group'?sourceReviewField(draft.sourcePaths,dispatch==='team'):''}${thread.kind==='group' && dispatch==='team'?childFields(draft.children):''}`:''}
     ${limitFields(false,draft.limits || run.limits || {})}</details>
    <div class="reply-footer"><span class="muted-note">${hasProvider ? (intent === 'question' ? '仅查询项目文件与对话记录。' : '基于最近可用的项目版本修改，旧版可随时查看。') : '继续对话需要配置本机 DeepSeek API key。'}</span>
     <button type="submit" class="primary" ${!hasProvider || status ? 'disabled' : ''}>${status ? '正在发送…' : '发送'}${icon('arrow')}</button></div>
   </fieldset>${draft.error ? `<p class="form-error" role="alert">${e(draft.error)}</p>` : ''}</form>`;
}

function conversationPanel(detail) {
 const thread = detail.conversation;
 if (!thread) return empty('当前运行没有项目对话','可从“对话”发起私聊或群聊。');
 if (historyThread !== thread.id) {
  roomHistory.clear();
  historyThread = thread.id;
 }
 const group = thread.kind==='group';
 const latestVersion = thread.artifact_run_id;
 const history = thread.turns.map(entry => {
  const turn = roomPage(entry,detail);
  const link = turn.run_id === detail.run.id
   ? '<button class="text-button" data-tab="overview">本轮详情</button>'
   : `<a class="text-button" href="#run/${encodeURIComponent(turn.run_id)}">查看本轮</a>`;
  return `<article class="chat-turn" data-key="turn-${e(turn.run_id)}">
   <div class="chat-user"><div class="speaker">你 <small>发给 ${e(turn.member_names?.[turn.recipient] || roleLabel(turn.recipient))}</small></div>
   <p>${inlineMarkdown(turn.user,turn,{kind:'goal'})}</p></div>
   ${roomMessages(turn,detail,group)}
   <div class="chat-agent">${turn.answer ? `<div class="speaker">${group?roomAvatar(turn,turn.recipient):icon('cpu')} ${memberLink(turn.role_keys?turn:thread,turn.recipient)} ${link}</div>
    ${roomReply(turn,group)}` : roomStatus(turn,detail,link)}${turnFiles(turn,group)}</div></article>`;
 }).join('');
 const versionLink = latestVersion === detail.run.id
  ? '<button class="text-button" data-tab="artifacts">查看当前产物</button>'
  : latestVersion ? `<a class="text-button" href="#run/${encodeURIComponent(latestVersion)}/artifacts">查看项目产物</a>` : '';
 return `<div class="${group?'group-chat-layout':'private-chat-layout'}"><section class="chat-thread" data-key="thread-${e(thread.id)}">
   <header class="thread-head"><div><h2>${thread.kind === 'group' ? '项目群聊' : '项目私聊'}</h2>
    ${group?'':`<p>${thread.members.map(id=>memberLink(thread,id)).join('、')}</p>`}
    ${thread.workspace_path?`<p class="thread-workspace">${icon('folder')}工作区 <code>${e(thread.workspace_path)}</code></p>`:''}</div><div class="chat-actions">${versionLink}
   <a class="text-button" href="/api/runs/${encodeURIComponent(detail.run.id)}/room.zip" download>导出完整会话</a></div></header>
   <div class="chat-history">${history || (group?'<p class="room-first-message">还没有群消息。</p>':'')}${history?roomPanel.endMarker():''}</div>${history?roomPanel.jumpButton():''}${conversationComposer(thread,detail.run)}</section>
   ${group?roomPanel.rail(thread,thread.head_run_id===detail.run.id && !runActive(detail.run) && thread.can_continue):''}</div>`;
}

function rememberReply(form) {
 const fields = form.elements;
 const draft = {interactionMode:fields.interaction_mode.value,message:fields.message.value,recipient:fields.recipient.value,intent:fields.intent.value,files:fields.acceptance_files.value,
  sourcePaths:fields.required_source_review_paths?.value ?? chatDrafts.get(form.dataset.thread)?.sourcePaths,
  rules:fields.acceptance_checks?.value,children:fields.child_checks?.value,base:fields.base?.value,dispatch:fields.dispatch?.value,
  limits:Object.fromEntries(runLimitFields.map(k=>[k.name, Number(fields[k.name].value)]))};
 chatDrafts.set(form.dataset.thread,draft);
 return draft;
}

async function sendReply(form) {
 const id = form.dataset.thread;
 if (pendingChat.has(id) || !form.reportValidity()) return;
 const draft = rememberReply(form);
 if (!draft.message.trim()) {
  draft.error = '请输入消息内容。';
  render();
  return;
 }
 const payload = {interaction_mode:draft.interactionMode,mode:draft.intent==='edit'&&draft.dispatch==='team'?'team':'single',limits:draft.limits,objective:draft.message.trim(),conversation:{
  parent_run_id:form.dataset.parent,recipient:draft.recipient,intent:draft.intent
 }};
 if (draft.intent === 'edit') {
  payload.acceptance_files = parseFiles(draft.files);
  if (payload.mode==='team' && draft.sourcePaths?.trim()) payload.required_source_review_paths=parseFiles(draft.sourcePaths);
  if (draft.base==='failed') payload.conversation.recover_failed=true;
  if (draft.base?.startsWith('checkpoint:')) {
   payload.conversation.recover_failed=true;
   payload.conversation.recover_task_id=draft.base.slice('checkpoint:'.length);
  }
  if (draft.base==='empty') payload.conversation.inherit_files=false;
  try { if(draft.rules?.trim()) payload.acceptance_checks=readRules(draft.rules);
   if(payload.mode==='team'&&draft.children?.trim()) payload.child_checks=JSON.parse(draft.children); }
  catch(error){draft.error=error.message;render();return;}
 }
 const fingerprint = JSON.stringify(payload);
 let submission = chatSubmissions.get(id);
 if (submission?.fingerprint !== fingerprint) {
  submission = {fingerprint,id:crypto.randomUUID()};
  chatSubmissions.set(id,submission);
 }
 payload.request_id = submission.id;
 pendingChat.add(id);
 render();
 try {
  const data = await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});
  chatDrafts.delete(id);
  chatSubmissions.delete(id);
  if (state.runId === form.dataset.parent) {
   location.hash = state.followLatest ? latestChatLink(data.run.id) : chatLink(data.run.id);
  }
  toast('消息已发送。');
 } catch(error) {
  draft.error = error.message;
  if (error.code === 'forbidden') state.config = null;
  await refresh();
 } finally {
  pendingChat.delete(id);
  if (state.detail?.conversation?.id === id) render();
 }
}

function parseFiles(value) {
 return String(value || '').split(/[,，]/).map(path => path.trim()).filter(Boolean);
}

function endedWithoutChecks(detail) {
 return detail.run.status === 'unverified'
  && detail.diagnostics?.execution_status === 'finished'
  && detail.diagnostics?.acceptance_status === 'not_configured';
}

function detailBadge(detail) {
 if (endedWithoutChecks(detail)) return '<span class="badge ended">执行已结束 · 未配置验收</span>';
 return badge(runStatus(detail.run));
}

function formHTML(prefix='home',mode='team-demo',objective='') {
 const demo = mode==='demo' || mode==='team-demo';
 const chat = chatMode(mode);
 const goal = demo ? (mode==='demo' ? '生成笔记并验证工具执行' : '整理笔记并写摘要') : objective;
 const options = [
  ['single','私聊 · 指定角色'], ['group','群聊 · 指定成员'],
  ['team','多 Agent 自动分工（实验）'], ['demo','离线单 Agent 演示'], ['team-demo','离线协作演示']
 ];
 const note = demo ? '离线演示使用预设模型回复和真实工具。'
  : !state.config?.provider_ready ? 'DeepSeek 尚未配置。可先运行离线演示。'
  : mode==='team' ? '使用独立任务图调度。需要持续讨论时，可在群聊里选择由负责人分工。'
  : mode==='group' ? '可由负责人分工，也可以点名一位成员回复。'
  : '私聊记录和各轮产物保存在本机。';
 return `<form class="task-form" data-prefix="${prefix}"><div class="composer">
  ${newConversationFields(mode)}
  <label class="composer-label" for="${prefix}-objective">${chat ? '首条消息' : '任务目标'}</label>
  <textarea id="${prefix}-objective" name="objective" placeholder="描述你想解决的问题或要完成的任务…" ${demo?'readonly':''} maxlength="8000">${e(goal)}</textarea>
  ${chat || mode==='demo' ? `<div class="workspace-choice"><label class="field-label" for="${prefix}-workspace">本地工作区（选填）</label>
   <div><input id="${prefix}-workspace" name="workspace_path" placeholder="项目文件夹的绝对路径" autocomplete="off" spellcheck="false">
    <button type="button" class="secondary" data-action="pick-workspace">${icon('folder')}选择文件夹</button></div>
   <small>已有文件会作为项目输入，成功交付的修改会写回这里。</small></div>` : ''}
  <div class="advanced" id="${prefix}-advanced" hidden>
   ${!demo ? interactionField() : ''}
   ${mode==='team' ? '<label class="field-label">调度方式<select name="scheduler"><option value="serial">串行 · 按依赖执行</option><option value="parallel">分层并发</option><option value="evented">事件驱动</option></select></label>' : ''}
   <label class="field-label">模型思考<select name="thinking" ${demo?'disabled':''}><option value="disabled">关闭</option>
     <option value="enabled" ${state.config?.thinking==='enabled'?'selected':''}>开启</option></select></label>
   ${(chat||mode==='team') ? '<label class="field-label">本轮验收文件（选填）<input name="acceptance_files" placeholder="例如 snake/index.html, snake/game.js"><small>填写工作区相对路径，多个文件用逗号分隔。</small></label>' : ''}
   ${!demo?ruleFields():''}
   ${mode==='group'?sourceReviewField():''}
   ${mode==='team'||mode==='group'?childFields('',mode==='group'):''}
   ${limitFields(demo)}
  </div>
  <div class="composer-footer"><div class="composer-controls">
   <label class="mode-control">${icon(mode==='group'?'users':demo?'bolt':'cpu')}
    <select name="mode" aria-label="执行模式">${options.map(([value,label])=>`<option value="${value}" ${value===mode?'selected':''}>${label}</option>`).join('')}</select></label>
   <button type="button" class="text-button" data-action="advanced" aria-expanded="false" aria-controls="${prefix}-advanced">${icon('tune')}选项</button>
  </div><button class="primary" type="submit">${demo?'运行演示':chat?'发送':'开始任务'}${icon('arrow')}</button></div>
  <p class="form-error" role="alert" hidden></p></div><p class="form-note">${icon('shield')}<span>${note}</span></p></form>`;
}
function runModeLabel(run, conversationKind=run.conversation_kind) {
 if (run.mode==='room' && conversationKind==='group') return '项目群聊';
 if (run.mode==='single' && conversationKind) return conversationKind==='group' ? '群聊' : '私聊';
 return modeLabels[run.mode] || run.mode || '历史运行';
}
function runRow(r){return `<a class="run-row" data-key="run-${e(r.id)}" href="#run/${encodeURIComponent(r.id)}"><span class="run-icon">${icon(r.mode?.includes('team')?'branch':'file')}</span><div class="run-info"><div class="run-title">${e(r.title || '未命名任务')}</div><div class="run-meta"><span>${e(runModeLabel(r))}</span><span>·</span><span>${r.completed_count || 0}/${r.task_count || 0} 个任务完成</span><span>·</span><span>${r.artifact_count || 0} 个产物</span></div></div>${badge(runStatus(r))}<span class="run-time">${e(dateText(r.created_at))}</span><span class="chevron">${icon('chevron')}</span></a>`;}
function empty(title,description,action=''){return `<div class="empty-state"><div class="empty-illustration">${icon('layers')}</div><h3>${e(title)}</h3><p>${e(description)}</p>${action}</div>`;}
function home(){return `<section class="page home-page"><div class="home-heading"><div><h1>工作台</h1><p>发起任务，跟踪执行，检查结果。</p></div></div>${formHTML('home',state.config?.provider_ready?'single':'team-demo')}<section class="starter-section"><div class="section-kicker">试用示例</div><div class="starter-grid"><button class="starter" data-template="team-demo"><span class="starter-icon">${icon('branch')}</span><strong>协作整理笔记</strong></button><button class="starter" data-template="demo"><span class="starter-icon">${icon('terminal')}</span><strong>工具执行演示</strong></button><button class="starter" data-template="maintenance"><span class="starter-icon">${icon('cpu')}</span><strong>电脑维护计划</strong></button></div></section><section class="runs-section"><div class="section-header"><h2 class="section-title">最近任务 <span class="count-badge">${state.runs.length}</span></h2><a href="#runs" class="text-button">查看全部</a></div><div id="home-runs">${state.runs.length?`<div class="run-list">${state.runs.slice(0,5).map(runRow).join('')}</div>`:empty('还没有任务','运行上方演示，查看任务分工、工具调用和验收结果。')}</div></section></section>`;}
function filteredRuns(){const query=state.search.toLowerCase();return state.runs.filter(r=>(!query||`${r.title} ${r.id}`.toLowerCase().includes(query))&&(state.filter==='all'||(state.filter==='active'?['running','verifying','queued'].includes(r.status):r.status===state.filter)));}
function runList(){const runs=filteredRuns();return runs.length?`<div class="run-list">${runs.map(runRow).join('')}</div>`:empty(state.runs.length?'没有匹配的任务':'还没有任务记录',state.runs.length?'试试其他关键词或状态。':'从新建任务开始，运行记录会保存在这里。');}
function runsPage(){return `<section class="page"><div class="page-top"><div><span class="eyebrow">TASK HISTORY</span><h1>所有任务</h1><p class="page-subtitle">查看已保存的执行与验收记录。</p></div><button class="primary" data-action="new">${icon('plus')}新建任务</button></div><div class="filters"><input id="run-search" class="filter-input" placeholder="搜索任务名称…" aria-label="搜索任务" value="${e(state.search)}"><select id="run-filter" class="filter-select" aria-label="筛选任务状态">${[['all','所有状态'],['active','运行中'],['completed','已完成'],['unverified','未验证'],['failed','失败'],['interrupted','待核实']].map(([v,t])=>`<option value="${v}" ${state.filter===v?'selected':''}>${t}</option>`).join('')}</select></div><div id="all-runs">${runList()}</div></section>`;}
function taskNodes(tasks){const map=new Map(tasks.map(t=>[t.task_id,t]));const children=new Map();tasks.forEach(t=>{const parent=map.has(t.parent_id)?t.parent_id:null;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(t);});const seen=new Set();function walk(task,depth){if(seen.has(task.task_id))return '';seen.add(task.task_id);const expanded=state.expanded.has(task.task_id);return `<div data-key="task-${e(task.task_id)}" class="task-node ${depth?'child':''}"><button id="task-${e(task.task_id)}" class="task-button" data-task="${e(task.task_id)}" aria-expanded="${expanded}"><span class="task-state-icon">${icon(stateIcon(task.status))}</span><span class="task-text"><span class="task-name">${e(task.objective)}</span><span class="task-subline"><span>${e(roleLabel(task.assignee_profile_id || task.owner_profile_id) || '—')}</span><span>·</span><span>${depth?'子任务':'整体目标'}</span>${task.deps?.length?`<span>· ${task.deps.length} 个前置任务</span>`:''}</span></span>${badge(task.status)}</button>${expanded?`<div class="task-inspector"><div class="inspector-grid"><span>执行角色</span><span>${memberLink(state.detail?.conversation,task.assignee_profile_id || task.owner_profile_id) || '—'}</span><span>任务身份</span><code>${e(task.task_id)}</code><span>执行尝试</span><code>${e(task.current_attempt_id||'尚未开始')}</code><span>状态版本</span><span>${e(task.revision)}</span>${task.deps?.length?`<span>依赖任务</span><code>${e(task.deps.join(', '))}</code>`:''}${task.reason?`<span>说明</span><span>${e(task.reason)}</span>`:''}</div></div>`:''}${(children.get(task.task_id)||[]).map(child=>walk(child,depth+1)).join('')}</div>`;}return (children.get(null)||[]).map(t=>walk(t,0)).join('')||empty('正在登记任务','任务领取后，依赖关系和执行状态会显示在这里。');}
function eventLabel(event){const names={step_start:'开始一轮决策与执行',plan_start:'正在请求任务规划',studio_configuration:'本次运行配置',studio_error:'工作室执行异常',case_registered:'任务已登记',task_start:'开始执行任务',step:'完成一轮决策与执行',tool_result:'工具返回结果',acceptance:'独立验收',task_end:'任务执行结束',orchestration_start:'开始规划与调度',orchestration_end:'整体目标执行结束',plan:'任务计划已生成',plan_usage:'规划器已返回',subtask_start:'开始执行子任务',subtask_end:'子任务执行结束',provider_error:'模型调用异常',task_registered:'子任务已登记',plan_degraded:'规划已降级',compaction_usage:'语义摘要调用与用量',context_compacted:'上下文已整理',context_limit:'上下文容量不足',output_recovery:'正在恢复被截断的输出',output_recovery_exhausted:'输出恢复次数已用尽',budget_warning:'执行预算即将耗尽',cancel_observed:'已响应取消请求'};const detail=event.tool_name||event.tool||event.operation||event.agent||event.assignee||'';return `${names[event.kind]||event.kind||'执行事件'}${detail?' · '+detail:''}`;}
function summaryStats(detail){const s=detail.summary||{},r=detail.run;return `<div class="panel summary-panel"><h3>运行摘要</h3><div class="stat-line"><span>执行模式</span><strong>${e(runModeLabel(r, detail.conversation?.kind))}</strong></div><div class="stat-line"><span>完成任务</span><strong>${r.completed_count||0} / ${r.task_count||0}</strong></div><div class="stat-line"><span>运行耗时</span><strong>${duration(r.duration_ms)}</strong></div><div class="stat-line"><span>生成产物</span><strong>${detail.artifacts?.length||0} 个</strong></div><div class="stat-line"><span>${detail.summary_incomplete?'部分 token 记录':'已记录 token'}</span><strong>${Number(s.total_tokens||0).toLocaleString()}</strong></div><div class="summary-rule"></div><div class="summary-callout">${icon('shield')}${r.status==='completed'?'结果已通过独立验收。你可以在验收页查看检查项与证据。':endedWithoutChecks(detail)?'执行已结束。本轮未配置独立验收，可以查看产物或继续对话。':r.status==='unverified'?'独立验收尚未确认本轮结果，具体原因可在验收页查看。':r.status==='interrupted'?'当前服务未持有执行权，无法判断原执行者是否仍在运行。历史状态保留。':'每个任务的状态与验收记录会持续保存。'}</div><p class="muted-note">${r.mode?.includes('demo')?'离线演示的 token 为估算值。':'token 按已有记录汇总，不代表完整费用账单。'}</p></div>`;}
function overview(detail){const events=eventEntries(detail.events||[]).slice(-6).reverse();return `<div class="overview-grid"><div class="panel"><div class="panel-heading"><span>${icon('branch')}任务树</span><small>点击任务查看详情</small></div><div class="task-tree">${taskNodes(detail.tasks||[])}</div></div>${summaryStats(detail)}</div><div class="panel activity-panel"><div class="panel-heading"><span>${icon('clock')}最近动态</span><button class="text-button" data-tab="logs">查看完整日志 ${icon('arrow')}</button></div><div class="activity-list">${events.map(({event,key})=>`<div class="activity-row" data-key="${e(key)}"><span class="activity-time">${e(clockText(event.ts?event.ts*1000:event.iso))}</span>${icon(event.kind?.includes('end')?'check':'circle')}<span class="activity-label">${e(eventLabel(event))}</span></div>`).join('')||'<p class="muted-note">等待第一条执行记录…</p>'}</div></div>`;}
function logs(detail){const all=detail.events||[];const list=eventEntries(all).filter(({event})=>state.logFilter==='all'||(state.logFilter==='tools'?event.kind==='tool_result':state.logFilter==='acceptance'?event.kind==='acceptance':(event.kind?.includes('error')||['context_limit','output_recovery_exhausted'].includes(event.kind)||event.error_class||event.error||event.ok===false)));return `<div class="log-toolbar"><select id="log-filter" class="filter-select" aria-label="筛选日志">${[['all','全部事件'],['tools','工具调用'],['acceptance','验收事件'],['errors','错误事件']].map(([v,t])=>`<option value="${v}" ${state.logFilter===v?'selected':''}>${t}</option>`).join('')}</select><p>${detail.events_truncated?'仅展示最近部分日志 · ':''}${list.length} 条事件 · 点击展开详情</p></div>${list.length?`<div class="log-list">${list.map(({event,key})=>`<details class="log-entry ${event.error_class||event.ok===false||event.kind?.includes('error')||['context_limit','output_recovery_exhausted'].includes(event.kind)?'error':''}" data-key="${e(key)}" data-log="${e(key)}" ${state.logOpen.has(key)?'open':''}><summary><span class="activity-time">${e(clockText(event.ts?event.ts*1000:event.iso))}</span><span class="event-kind">${e(event.kind)}</span><span class="log-caption">${e(eventLabel(event))}${event.error||event.error_class||event.finish_reason==='length'?`<small>${e(event.finish_reason==='length'?'输出截断 (length) · '+event.completion_tokens+' token':event.error||event.preview||event.error_class)}</small>`:''}</span>${icon('chevron')}</summary><pre>${e(JSON.stringify(event,null,2))}</pre></details>`).join('')}</div>`:empty('这里还没有事件','执行日志会在任务运行时更新。')}`;}
function inlineMarkdown(source, turn, row) { return StudioMarkdown.render(source, turn, row, true); }
function markdown(source, turn, row) { return StudioMarkdown.render(source, turn, row); }
function filePreview() {
 if (!state.selectedFile) return empty('选择一个产物','在左侧选择文件，预览实际生成的内容。');
 const f=state.fileData;
 if (!f) return '<div class="initial-loading"><span class="spinner"></span>正在读取产物…</div>';
 if (f.error) return `<div class="error-panel">${e(f.error)}</div>`;
 const run=state.runId, name=e(f.path.split('/').pop());
 const size=state.detail?.artifacts?.find(file=>file.path===f.path)?.size;
 const download=label=>StudioMarkdown.overDownloadLimit(size)?'':
  `<a class="text-button" href="${e(fileURL(run,f.path,'download'))}" download>${icon('download')}${label}</a>`;
 if (f.media_type==='image/png') return `<div class="file-header"><span>${name}</span>${download('下载截图')}</div>
  <img class="project-screenshot" src="${e(fileURL(run,f.path,'preview'))}" alt="${e(f.path)}">`;
 if (f.binary) return `<div class="file-header"><span>${name}</span>${download('下载数据库')}</div>
  <p class="binary-note">SQLite 数据库，可在本机打开。</p>`;
 const preview=/\.html$/i.test(f.path)?`<a class="text-button" href="${e(fileURL(run,f.path,'preview'))}" target="_blank" rel="noopener noreferrer">打开交互预览</a>`:'';
 const md=/\.md$/i.test(f.path);
 return `<div class="file-header"><span>${name}</span>${preview}${download('下载文件')}</div>
  ${f.truncated?`<div class="run-warning">${StudioMarkdown.truncationNotice(size)}</div>`:''}
  <div class="file-content ${md?'prose':''}">${md?StudioMarkdown.documentHTML(f.content, {run, path:f.path}):`<pre>${e(f.content)}</pre>`}</div>`;
}
function artifacts(detail) {
 let files = detail.artifacts || [];
 if (state.selectedFile?.startsWith('handoffs/')) {
  const receipts = (detail.room_events || []).flatMap(row => row.artifacts || []);
  const selected = receipts.find(file => file.path === state.selectedFile);
  files = [selected || {path: state.selectedFile, name: state.selectedFile.split('/').pop()}, ...files];
 }
 if(!files.length)return empty('还没有生成产物','Agent 生成的项目文件会出现在这里。');return `<div class="artifact-toolbar"><p>${runActive(detail.run)?'任务结束后可打包下载全部产物。':'ZIP 保留相对路径；单个文件最多 8 MiB，总计最多 20 MiB。超出时可从本机工作区读取。'}</p>${!runActive(detail.run)?`<button class="secondary" data-action="download-artifacts" ${state.downloads.has(detail.run.id)?'disabled':''}>${icon('download')}${state.downloads.has(detail.run.id)?'正在打包…':'下载全部产物 ZIP'}</button>`:''}</div><div class="artifact-layout"><div class="artifact-list">${files.map(file=>`<button id="file-${e(file.path)}" class="artifact-button ${file.path===state.selectedFile?'active':''}" data-file="${e(file.path)}">${icon('file')}<span><strong>${e(file.name||file.path.split('/').pop())}</strong><small>${sizeText(file.size||0)}</small></span></button>`).join('')}</div><div class="artifact-preview" id="artifact-preview">${filePreview()}</div></div>`;}
function acceptance(detail){const reports=detail.acceptance||[];const passed=reports.filter(r=>r.report?.verdict==='passed').length;return `<div class="report-intro">${icon('shield')}<div><h3>${endedWithoutChecks(detail)?'本轮未配置独立验收':reports.length?`${passed} / ${reports.length} 份报告通过验收`:'等待独立验收'}</h3><p>成功由检查结果和证据共同确认。模型的完成答复不会直接计作成功。</p></div></div>${reports.map(item=>{const task=(detail.tasks||[]).find(t=>t.task_id===item.task_id),r=item.report||{};return `<article class="acceptance-card" data-key="acceptance-${e(r.report_id||item.task_id)}"><div class="acceptance-title"><span>${e(task?.objective||item.task_id)}</span>${badge(r.verdict)}</div><div class="acceptance-body">${r.reason?`<p>${e(r.reason)}</p>`:''}${(r.checks||[]).map(check=>{const criterion=item.spec?.criteria?.find(c=>c.id===check.criterion_id);return `<div class="check-row">${badge(check.verdict)}<span><strong>${e(criterion?.description||check.criterion_id)}</strong><small>${e(check.reason)}</small></span></div>`;}).join('')||'<p>尚无可执行检查结果。</p>'}<p>报告 ${e(r.report_id||'—')}</p></div><details><summary>查看检查规则、报告与证据</summary><pre>${e(JSON.stringify(item,null,2))}</pre></details></article>`;}).join('')||empty('尚无验收报告','运行中任务需等待检查；未配置验收的任务会保留为未验证。')}${detail.report?`<div class="section-header runs-section"><h3>完整运行报告</h3><button class="secondary" data-action="download-report">${icon('download')}保存 Markdown</button></div><div class="panel file-content prose">${markdown(detail.report)}</div>`:''}`;}
function diagnosis(detail){
 const d=detail.diagnostics;if(!d)return '';
 const issue=d.issues?.find(item=>item.severity==='error')||d.issues?.[0];
 const providerBlocked=issue?.code==='provider_balance_exhausted'&&!issue.resolved;
 const active=runActive(detail.run)&&!providerBlocked;
 const cancelling=runStatus(detail.run)==='cancel_requested';
 if(!issue&&!active)return '';
 const task=(detail.tasks||[]).find(t=>t.task_id===issue?.task_id);
 const verification={not_configured:'未配置',pending:'等待检查',not_recorded:'无记录',passed:'通过',failed:'未通过',error:'检查出错',unverified:'未验证'};
 const execution={running:'运行中',cancelled:'已取消',cancel_requested:'正在取消',failed:'失败',blocked:'依赖受阻',finished:'已结束',unknown:'待核实'};
 const activity=d.current_activity||[];
 const title=cancelling?'正在取消任务':active?(activity.length?activity.some(a=>a.phase==='planning')?'正在规划任务':'正在执行 Agent 步骤':'正在执行任务'):issue.title;
 return `<section class="diagnosis ${active?'running':issue?.severity==='warning'&&!endedWithoutChecks(detail)?'warning':''}" data-key="diagnosis"><div class="diagnosis-header"><h2>${e(title)}</h2><button class="secondary" data-action="download-diagnostics">${icon('download')}导出诊断</button></div>${active?`<p class="activity-live"><span class="dot"></span>${activity.length?activity.map(a=>`${e(({coordinate:'负责人分工',work:'执行任务',deliver:'写入分工文件',handoff:'成员交接',finalize:'负责人收尾',planning:'规划任务'})[a.phase]||a.task_id?.split(':').pop()||'整体目标')}${a.step?' 第 '+e(a.step)+' 轮':''}，已用时 ${duration(typeof a.started_ts==='number'?Date.now()-a.started_ts*1000:null)}`).join('；'):'任务状态会自动更新。'}</p>`:`<p>${task?`${e(task.assignee_profile_id||'Agent')}${issue.step?' 第 '+e(issue.step)+' 轮':''}：`:''}${e(issue.message)}</p>`}<div class="diagnosis-facts"><span>执行 <strong>${e(cancelling?'正在取消':execution[d.execution_status]||d.execution_status)}</strong></span><span>验收 <strong>${e(verification[d.acceptance_status]||d.acceptance_status)}</strong></span>${d.configuration?.max_steps!=null?`<span>轮数上限 <strong>${e(d.configuration.max_steps===0?'不限':d.configuration.max_steps)}</strong></span>`:''}${d.configuration?.max_total_tokens!=null?`<span>累计 token 预算 <strong>${d.configuration.max_total_tokens===0?'不限':Number(d.configuration.max_total_tokens).toLocaleString()}</strong></span>`:''}${d.blocked_tasks?.length?`<span>后续受阻 <strong>${d.blocked_tasks.length} 个任务</strong></span>`:''}${issue?.completion_tokens!=null?`<span>本轮输出 <strong>${Number(issue.completion_tokens).toLocaleString()} token</strong></span>`:''}</div>${!active&&issue?.suggestion?`<p class="next-step">下一步：${e(issue.suggestion)}</p>`:''}${!active&&d.acceptance_status==='not_configured'&&issue?.code!=='no_acceptance'?'<p>此外，本次未配置独立验收，不能判定目标已达成。</p>':''}${detail.run.status==='unverified'&&d.execution_status==='failed'?'<p>旧版总状态保存为“未验证”；日志确认存在执行失败。原始记录保持不变。</p>':''}<details data-key="diagnostic-evidence"><summary>查看失败证据、阻塞链与运行配置</summary>${issue?.request_id?`<p>模型请求 <code>${e(issue.request_id)}</code></p>`:''}${issue?.finish_reason?`<p>结束原因 <code>${e(issue.finish_reason)}</code>；输出上限 ${e(issue.max_output_tokens??'旧记录未保存')}；耗时 ${duration(issue.latency_ms)}</p>`:''}${(d.blocked_tasks||[]).map(t=>`<p>${e(t.task_id?.split(':').pop())} / ${e(t.assignee)}：${e(t.reason)}</p>`).join('')}<p>${Object.keys(d.configuration||{}).length?`模型 ${e(d.configuration.model)}；thinking ${e(d.configuration.thinking)}；单次输出上限 ${e(d.configuration.max_output_tokens)} token；执行轮数上限 ${e(d.configuration.max_steps===0?'不限':d.configuration.max_steps??'未记录')}；累计 token 预算 ${e(d.configuration.max_total_tokens??'未记录')}；任务时限 ${e(d.configuration.max_task_seconds??d.configuration.max_seconds??'未记录')} 秒。`:'旧运行没有保存配置快照；当前服务配置不代表当时配置。'}</p>${d.incomplete?'<p>日志已截断，此诊断只基于保留的记录。</p>':''}</details></section>`;
}
const roomHistory = new Map();
let historyThread;

function roomPage(turn, detail) {
 if (turn.run_id === detail.run.id && Array.isArray(detail.room_events))
  return {...turn, events:detail.room_events, events_start:0};
 const saved = roomHistory.get(turn.run_id);
 if (!saved?.events.size) return turn;
 // A historical turn can receive more journal entries after we fetch a page.
 // Merge by absolute position and leave explicit gaps between disjoint pages.
 const rows = new Map(saved.events);
 (turn.events || []).forEach((row,i)=>{
  const id = (turn.events_start || 0)+i;
  if (!rows.has(id) || !row.text_truncated) rows.set(id,row);
 });
 const ids = [...rows.keys()].sort((a,b)=>a-b);
 return {...turn,events:ids.map(id=>rows.get(id)),event_indices:ids,events_start:ids[0] || 0};
}

function historyState(id) {
 let saved = roomHistory.get(id);
 if (!saved) {
  saved = {events:new Map(),pending:new Set(),errors:new Map()};
  roomHistory.set(id,saved);
 }
 return saved;
}

function historyButton(runId, before, label) {
 const saved = roomHistory.get(runId);
 const pending = saved?.pending.has(before);
 const error = saved?.errors.get(before);
 return `<button type="button" class="text-button room-load" data-room-history="${e(runId)}"
  data-before="${e(before)}" ${pending?'disabled':''}>${e(pending?'正在读取…':label)}</button>${error?`<small role="alert">${e(error)}</small>`:''}`;
}

async function loadRoomPage(button) {
 const id = button.dataset.roomHistory;
 const before = button.dataset.before==='answer'?'answer':Number(button.dataset.before);
 const saved = historyState(id);
 if (saved.pending.has(before)) return;
 saved.pending.add(before);
 saved.errors.delete(before);
 render();
 try {
  const url = '/api/runs/'+encodeURIComponent(id);
  if (before==='answer') saved.answer = (await api(url+'/room-answer')).answer;
  else {
   const page = await api(url+'/room-events?before='+before);
   page.events.forEach((row,i)=>saved.events.set(page.start+i,row));
  }
 } catch (err) { saved.errors.set(before,err.message); }
 finally {
  saved.pending.delete(before);
  if (state.detail?.conversation?.id===historyThread && roomHistory.get(id)===saved) render();
 }
}

function roomLine(source) {
 const text = String(source || '').trim();
 if (text.length<=145 && !text.includes('\n')) return text;
 const lines = text.split(/\n+/).map(line=>line.trim()).filter(Boolean);
 const summaryHeading = lines.findIndex(line=>/^#{1,6}\s+(一句话(?:成果|交接|总结)|摘要|结论|summary)(?:[：:]|$)/i.test(line));
 const paragraphs = summaryHeading>=0 ? lines.slice(summaryHeading+1) : lines;
 const first = paragraphs.find(line=>!/^#{1,6}\s|^\x60\x60\x60|^\|/.test(line)) || lines[0] || '';
 const clean = first.replace(/^#{1,6}\s+|^[-*]\s+/, '');
 const sentence = clean.match(/^.{1,144}?[。！？]|^.{1,144}?[.!?](?=\s|$)/u)?.[0];
 return sentence || (clean.length>145?Array.from(clean).slice(0,140).join('')+'…':clean);
}

function roomAvatar(turn,id) {
 return roomPanel.avatar(id,id==='user'?'你':id==='studio'?'Studio':turn.member_names?.[id] || roleLabel(id));
}

function roomName(turn,id) {
 return id==='user'?'你':id==='studio'?'Studio':memberLink(turn,id);
}

function roomMessage(row,index,turn) {
 const isUser = row.role==='user';
 const assignment = row.kind==='assignment';
 const report = row.kind==='member_report';
 const recipient = row.recipient || (report ? turn.recipient : row.kind==='human_question'?'user':'');
 const source = String(row.text || '');
 const handoff = row.handoff;
 const reason = handoff?.remaining_issue;
 const disposition = row.disposition || (handoff?.published ? 'published' : reason ? 'blocked' : handoff ? 'unpublished' : '');
 const blocked = report && disposition==='blocked';
 const candidate = handoff?.reviewable_candidate;
 const savedCandidate = blocked && handoff?.published===false && handoff.role===row.role
  && row.task_id && candidate?.task_id===row.task_id && candidate.task_id===handoff.task_id
  && /^[a-f0-9]{64}$/.test(candidate.candidate_version) && candidate.files_total>0;
 const issue = savedCandidate ? (reason || source).replace(/^\s*\[BLOCKED\]\s*(?:[—:：-]\s*)?/u,'') : reason;
 const summary = isUser ? source : blocked ? roomLine(issue || '交接受阻') : roomLine(row.chat_message || source);
 const names = recipient ? [recipient,turn.member_names?.[recipient],roleLabel(recipient)].filter(Boolean) : [];
 const addressed = names.find(name=>summary.startsWith('@'+name)
  && (!summary[name.length+1] || /[\s，,:：、。!！]/u.test(summary[name.length+1])));
 const body = addressed && !isUser ? summary.slice(addressed.length+1).trimStart() : summary;
 const mention = recipient && !(addressed && isUser) ? `<span class="room-mention">@${roomName(turn,recipient)}</span> ` : '';
 const files = row.artifacts || [];
 const receipt = row.kind==='human_review' ? ({passed:'人工评审通过',failed:'人工评审未通过'})[row.verdict]
  : savedCandidate ? '候选已保存 · 可独立复核'
  : ({published:'已交接',assessed:'已提交评审',awaiting_review:'等待独立复核',blocked:'交接受阻 · 平台回执',unpublished:'尚未发布'})[disposition];
 const paths = (row.deliverable_paths || []).map(path=>`<span>${inlineMarkdown(path,turn,row)}</span>`).join(' ');
 const full = summary!==source || row.text_truncated;
 const hasMaterials = assignment || report || full && !isUser;
 const label = assignment?'派工材料':report?'交付材料':'完整消息';
 const more = row.text_truncated?historyButton(turn.run_id,index+1,'读取完整原文'):'';
 const checks = handoff?.evidence?.check_signals || [];
 const materials = hasMaterials ? `<details class="room-material" data-key="materials"><summary>${label}${files.length?` · ${files.length} 个文件`:''}</summary>
  <div class="room-material-body">${source && (full || assignment || report)?`<div class="prose">${markdown(source,turn,row)}</div>`:''}${more}
  ${row.expectation?`<p><b>验收要求</b> ${inlineMarkdown(row.expectation,turn,row)}</p>`:''}
  ${paths?`<p><b>交付路径</b> ${paths}</p>`:''}
  ${handoff?.remaining_issue?`<p><b>待处理</b> ${inlineMarkdown(handoff.remaining_issue,turn,row)}</p>`:''}
  ${checks.length?`<ul>${checks.map(check=>`<li>${e(check)}</li>`).join('')}</ul>`:''}
  ${files.length?`<div class="member-files">${files.map(file=>artifactLink(turn.run_id,file)).join('')}</div>`:''}
  <details class="room-raw" data-key="raw"><summary>原始事件</summary><pre>${e(JSON.stringify(row,null,2))}</pre></details></div></details>`:'';
 return `<div class="room-message" data-kind="${e(row.kind)}" data-key="message-${index}">
  ${roomAvatar(turn,row.role)}<div class="speaker">${roomName(turn,row.role)}${row.at?`<time datetime="${e(row.at)}" title="${e(row.at)}">${e(clockText(row.at).slice(0,5))}</time>`:''}</div>
  <p class="room-line">${mention}${inlineMarkdown(body,turn,row)}</p>
  ${receipt?`<small class="room-disposition">${e(receipt)}</small>`:''}${materials}</div>`;
}

function roomMessages(turn,detail,group) {
 const events = turn.events || [];
 const start = turn.events_start || 0;
 const conversation = [], records = [];
 for (const [offset,row] of events.entries()) {
  const index = turn.event_indices?.[offset] ?? start+offset;
  if (offset && turn.event_indices && index>turn.event_indices[offset-1]+1)
   conversation.push(historyButton(turn.run_id,index,'查看此处更早的消息'));
  // The goal is already displayed as the user's message at the top of the turn.
  if (row.kind==='goal') continue;
  if (['assignment','member_report','human_question','human_answer','human_review'].includes(row.kind)) {
   conversation.push(roomMessage(row,index,turn));
  } else {
   records.push(`<details data-key="record-${index}"><summary>${e(row.kind)} · ${e(roomLine(row.text))}</summary>
    <pre>${e(JSON.stringify(row,null,2))}</pre>${row.text_truncated?historyButton(turn.run_id,index+1,'读取完整原文'):''}</details>`);
  }
 }
 const activity = window.StudioActivity?.renderTurn(turn,detail) || '';
 const diagnostics = records.length || group && activity ? `<details class="room-records" data-key="room-records">
  <summary>运行记录</summary>${records.join('')}${group?activity:''}</details>`:'';
 return (start?historyButton(turn.run_id,start,'查看更早消息'):'') + conversation.join('') + diagnostics + (group?'':activity);
}

function roomStatus(turn,detail,link) {
 if (runActive(turn)) return `<div class="turn-status"><span>${turn.status==='waiting_user'?'等待你的答复…':'正在处理…'}</span>${link}</div>`;
 const current = turn.run_id===detail.run.id;
 const failed = ['failed','error','blocked'].includes(turn.status);
 let message;
 if (failed) {
  const issues = current ? (detail.diagnostics?.issues || []).filter(issue=>issue.severity==='error' && issue.message) : [];
  const specific = issues.find(issue=>!['execution_failed','studio_error'].includes(issue.code));
  // A later assignment/receipt supersedes an older failed handoff as a possible cause.
  const last = (turn.events || []).findLast(row=>['assignment','member_report','direct_delivery'].includes(row.kind));
  const handoff = last?.kind==='member_report' && (last.disposition==='blocked' || (!last.disposition && last.handoff?.published===false))
   ? last.handoff?.remaining_issue : '';
  const cause = specific?.message || handoff || issues[0]?.message || turn.error || (current && detail.run.error);
  message = cause ? roomLine(cause).split(/(?<=[。！？])/u)[0] : '本轮执行失败，未收到负责人的最终答复。';
 } else {
  message = ({cancelled:'本轮已取消。',interrupted:'本轮执行状态待核实。',unverified:'本轮已结束，验收状态为未验证。',
   completed:'本轮已完成。'})[turn.status] || '本轮没有保存最终答复。';
 }
 return `<div class="turn-system" data-origin="studio"><div class="speaker">Studio <small>系统记录</small> ${link}</div>
  <p class="room-line">${e(message)}</p></div>`;
}

function roomReply(turn,group) {
 const loaded = roomHistory.get(turn.run_id)?.answer;
 const answer = loaded ?? turn.answer;
 if (!answer) return '';
 const line = roomLine(answer);
 const excerpt = loaded==null && turn.answer_truncated ? historyButton(turn.run_id,'answer','读取完整回复') : '';
 if (!group || line===answer) return `<div class="chat-answer prose">${markdown(answer,turn)}</div>${excerpt}`;
 return `<p class="room-line">${inlineMarkdown(line,turn)}</p><details class="room-material" data-key="final-answer">
  <summary>完整回复</summary><div class="room-material-body prose">${markdown(answer,turn)}${excerpt}</div></details>`;
}

function humanQuestions(detail) {
 return (detail.pending_questions||[]).map(q=>`<form class="human-form" data-question="${e(q.request_id)}" data-run="${e(detail.run.id)}">
  <label class="field-label"><strong>${e(roleLabel(q.role))} 正在等你回答</strong><span>${e(q.text)}</span>
  <textarea name="answer" required maxlength="8000" rows="2"></textarea></label><button class="primary">答复并继续</button></form>`).join('');
}
function reviewForm(detail) {
 const reviews=(detail.room_events||[]).filter(row=>row.kind==='human_review');
 if (runActive(detail.run) || detail.run.status==='interrupted') return '';
 return `<details class="studio-disclosure"><summary>人工评审 · ${reviews.length} 条</summary>
  ${reviews.map(r=>`<p><strong>${r.verdict==='passed'?'通过':'未通过'}</strong> · ${e(r.text)}</p>`).join('')}
  <form class="review-form" data-run="${e(detail.run.id)}"><label class="field-label">结论<select name="verdict"><option value="passed">通过</option><option value="failed">未通过</option></select></label>
  <label class="field-label">检验过程<textarea name="note" required maxlength="8000" rows="2"></textarea></label><button class="secondary">记录评审</button></form></details>`;
}
async function submitDiscussion(form) {
 const button=form.querySelector('button');
 if(button.disabled || !form.reportValidity())return;
 const path='/api/runs/'+encodeURIComponent(form.dataset.run)+(form.matches('.human-form')?'/questions/'+encodeURIComponent(form.dataset.question):'/review');
 const body=Object.fromEntries(new FormData(form));
 button.disabled=true;
 try {
  await api(path,{method:'POST',body:JSON.stringify(body)});
  form.reset();
  if(state.route==='assistant')await studioAssistant.refresh(true);else await refreshDetail();
 }catch(error){toast(error.message);}finally{button.disabled=false;}
}
function chatDiagnostics(detail) {
 if (endedWithoutChecks(detail)) return '';
 const content = diagnosis(detail);
 if (!content) return '';
 const issue = detail.diagnostics?.issues?.find(item => !item.resolved && item.severity === 'error');
 const label = issue?.title || '查看运行状态';
 return `<details class="chat-diagnostics" data-key="chat-diagnostics"><summary>${e(label)}</summary>${content}</details>`;
}

function detailContent(){if(!state.detail)return '<div class="initial-loading"><span class="spinner"></span>正在读取任务…</div>';const d=state.detail,r=d.run;
 const room=chatDirectory.find(d.conversation?.id);
 const title=state.tab==='chat' && d.conversation?.kind==='group' && (room?.title || d.conversation.title) || r.title;
 const back = state.tab==='chat' ? ['#chats','返回最近对话'] : ['#runs','返回全部任务'];
 const tabs=[...(d.conversation?[['chat','users','项目对话',d.conversation.turns.length]]:[]),['overview','branch','任务概览',d.tasks?.length],['logs','terminal','执行日志',d.events?.length],['artifacts','folder','产物',d.artifacts?.length],['acceptance','shield','验收',d.acceptance?.length]];return `<section class="page ${state.tab==='chat'?'conversation-page':''}"><div class="detail-top"><a class="back-link" href="${back[0]}">${icon('left')}${back[1]}</a><div class="detail-heading"><h1>${e(title)}</h1><div class="detail-actions"><span id="thread-pin">${chatDirectory.managementButton(room)}</span>${detailBadge(d)}${d.conversation&&state.tab!=='chat'?'<button class="secondary" data-tab="chat">进入对话</button>':''}${runActive(r)?`<button class="secondary" data-action="cancel-run" ${runStatus(r)==='cancel_requested'?'disabled':''}>${icon('x')}${runStatus(r)==='cancel_requested'?'正在取消…':'取消任务'}</button>`:''}</div></div><div class="detail-meta"><span>${icon('layers')}${e(runModeLabel(r, d.conversation?.kind))}</span><span>${icon('clock')}${e(dateText(r.created_at))}</span><span>${icon('file')}${d.artifacts?.length||0} 个产物</span></div></div>${runStatus(r)==='cancel_requested'?'<p class="run-warning" role="status">已请求停止后续执行；当前模型请求或工具调用可能需要先返回。保存的产物和日志会保留。</p>':''}${r.error&&!d.diagnostics?.issues?.length?`<div class="run-warning">${e(r.error)}</div>`:''}${r.status==='interrupted'?'<div class="run-warning">当前服务未持有这次执行。已保存的任务状态可查看；刷新不会自动继续执行或重放工具。</div>':''} ${humanQuestions(d)}${state.tab==='chat'?chatDiagnostics(d):diagnosis(d)}<div class="tabbar" data-key="detail-tabs" role="tablist" aria-label="任务视图">${tabs.map(([id,i,label,count])=>`<button class="tab ${state.tab===id?'active':''}" data-tab="${id}" role="tab" aria-selected="${state.tab===id}" id="tab-${id}" aria-controls="detail-panel" tabindex="${state.tab===id?0:-1}">${icon(i)}${label}<span class="tab-count">${count||0}</span></button>`).join('')}</div><div id="detail-panel" role="tabpanel" aria-labelledby="tab-${state.tab}">${state.tab==='chat'?conversationPanel(d):state.tab==='overview'?overview(d):state.tab==='logs'?logs(d):state.tab==='artifacts'?artifacts(d):acceptance(d)+reviewForm(d)}</div></section>`;}
function render({reset=false}={}){document.body.classList.toggle('assistant-view',state.route==='assistant');const html=state.route==='assistant'?studioAssistant.page():state.route==='home'?home():state.route==='runs'?runsPage():state.route==='chats'?chatDirectory.page():state.route==='roles'?rolePage():detailContent();if(reset)main.innerHTML=html;else patchHTML(main,html);updateSidebar();roomPanel.scheduleScrollCheck();}
function parseRoute() {
 const [hash, query=''] = location.hash.slice(1).split('?');
 if (hash.startsWith('run/')) {
  try {
   const [encoded, tab] = hash.slice(4).split('/');
   return {route:'detail', runId:decodeURIComponent(encoded), tab:['chat','artifacts'].includes(tab)?tab:'overview',
    followLatest:tab==='chat' && new URLSearchParams(query).get('latest')==='1',
    task:tab==='overview' ? new URLSearchParams(query).get('task') : null,
    file:tab==='artifacts' ? new URLSearchParams(query).get('path') : null};
  } catch { return {route:'runs',runId:null}; }
 }
 if (hash==='roles' || hash.startsWith('roles/')) {
  try {return {route:'roles',roleId:decodeURIComponent(hash.slice(6)) || null,runId:null};}
  catch {return {route:'roles',roleId:null,runId:null};}
 }
 if(hash==='chats') chatDirectory.setFilter(new URLSearchParams(query).get('kind'));
 return {route:['runs','chats','home'].includes(hash)?hash:'assistant',runId:null};
}
async function route(){const next=parseRoute();const changed=next.runId!==state.runId;
 if (state.route!==next.route || roleView.id!==(next.roleId || null)) {
  Object.assign(roleView,{id:next.roleId||null,detail:null,error:'',filter:'current',kind:'memory',section:'tasks',taskFilter:'all'});
  if ($('#role-dialog')?.open) $('#role-dialog').close();
 }
 state.followLatest=Boolean(next.followLatest);
 state.route=next.route;state.runId=next.runId;if(changed){state.detail=null;state.detailSignature='';state.tab=next.tab||'overview';state.selectedFile=null;state.fileData=null;state.expanded.clear();state.logOpen.clear();}else if(next.tab){state.tab=next.tab;}$('#sidebar').classList.remove('open');render({reset:true});window.scrollTo({top:0,behavior:'instant'});if(state.route==='detail') {
  if(next.file) await selectFile(next.file);
  if (next.task) {
   state.expanded.add(next.task);
   if (state.detail) render();
  }
  await refreshDetail();
  if (next.task && state.runId===next.runId && state.tab==='overview') {
   const target=document.getElementById('task-'+next.task);
   if (target) {target.focus({preventScroll:true});target.scrollIntoView({block:'center'});}
  }
  if(state.runId===next.runId && state.tab==='artifacts' && !state.selectedFile && state.detail?.artifacts?.length)
   await selectFile(state.detail.artifacts[0].path);
 }if(state.route==='assistant'){studioAssistant.enter();await studioAssistant.refresh();}if(state.route==='chats')await chatDirectory.refresh(true);if(state.route==='roles')await loadRole();}
async function refreshDetail() {
 const id=state.runId;
 if (!id) return;
 try {
  const detail=await api(`/api/runs/${encodeURIComponent(id)}`);
  if (state.runId!==id) return;
  const thread=detail.conversation;
  if (state.tab==='chat' && thread?.superagent) {
   location.replace('#assistant');
   return;
  }
  if (state.tab==='chat' && state.followLatest && thread?.head_run_id && thread.head_run_id!==id) {
   location.replace(latestChatLink(thread.head_run_id));
   return;
  }
  const signature=JSON.stringify(detail);
  state.detail=detail;
  if (signature!==state.detailSignature) {
   state.detailSignature=signature;
   render();
   if (state.tab==='artifacts' && !state.selectedFile && detail.artifacts?.length) selectFile(detail.artifacts[0].path);
  }
  setOnline(true);
 } catch(error) {
  if (state.runId===id && !state.detail) main.innerHTML=`<div class="page error-panel"><h2>暂时无法读取任务</h2><p>${e(error.message)}</p><a href="#runs" class="secondary">返回任务列表</a></div>`;
  if (!error.code) setOnline(false);
 }
}
async function refresh(){if(state.polling)return;state.polling=true;try{if(!state.config)state.config=await api('/api/bootstrap');const data=await api('/api/runs');state.runs=data.runs||[];setOnline(true);if(!state.initialized){state.initialized=true;await route();}updateSidebar();if(state.route==='home'&&$('#home-runs')){patchHTML($('#home-runs'),state.runs.length?`<div class="run-list">${state.runs.slice(0,5).map(runRow).join('')}</div>`:empty('工作室已经准备好了','运行上方演示，你的第一个任务会出现在这里。'));const count=$('.section-title .count-badge');setText(count,state.runs.length);}if(state.route==='runs'&&$('#all-runs'))patchHTML($('#all-runs'),runList());if(state.route==='detail')await refreshDetail();if(state.route==='assistant')await studioAssistant.refresh();await chatDirectory.refresh();await refreshRoleIfNeeded();}catch{setOnline(false);}finally{state.polling=false;}}
function formTitle(mode){return mode==='group'?'新建群聊':mode==='single'?'新建私聊':'开始一个新任务';}
function openForm(mode=state.config?.provider_ready?'single':'team-demo',objective=''){ $('#dialog-title').textContent=formTitle(mode); $('#dialog-form').innerHTML=formHTML('dialog',mode,objective);$('#task-dialog').showModal();setTimeout(()=>$('#dialog-objective').focus(),0);}
function swapMode(form, mode, objective='') {
 const {prefix} = form.dataset;
 const saved = new FormData(form);
 const advanced = !form.querySelector('.advanced').hidden;
 const prompt = objective || (form.elements.objective.readOnly ? '' : form.elements.objective.value);
 if (prefix === 'dialog') $('#dialog-title').textContent = formTitle(mode);
 form.outerHTML = formHTML(prefix, mode, prompt);
 const replacement = document.querySelector(`form[data-prefix="${prefix}"]`);
 for (const name of ['workspace_path', 'interaction_mode', ...runLimitFields.map(field => field.name)]) {
  if (saved.has(name) && replacement.elements[name]) replacement.elements[name].value = saved.get(name);
 }
 if (advanced) {
  replacement.querySelector('.advanced').hidden = false;
  replacement.querySelector('[data-action="advanced"]').setAttribute('aria-expanded','true');
 }
}
async function pickWorkspace(button) {
 const form = button.closest('form');
 const input = form.elements.workspace_path;
 const error = form.querySelector('.form-error');
 button.disabled = true;
 error.hidden = true;
 try {
  const {path} = await api('/api/workspaces/pick', {method:'POST',body:'{}'});
  if (typeof path === 'string') input.value = path;
 } catch (problem) {
  error.textContent = problem.message;
  error.hidden = false;
  input.focus();
 } finally { button.disabled = false; }
}

async function submit(form) {
 const button = form.querySelector('[type="submit"]');
 if (button.disabled || !form.reportValidity()) return;
 const fields = new FormData(form);
 const mode = fields.get('mode');
 const chat = chatMode(mode);
 const payload = {mode:chat?'single':mode,objective:String(fields.get('objective')||'').trim(),
  scheduler:fields.get('scheduler')||'serial',thinking:fields.get('thinking')||'disabled'};
 const workspace = String(fields.get('workspace_path') || '').trim();
 if (workspace) payload.workspace_path = workspace;
 const errorBox = form.querySelector('.form-error');
 function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
 }
 errorBox.hidden = true;
 if (chat || mode === 'team') {
  payload.interaction_mode = fields.get('interaction_mode');
  if (!state.config?.provider_ready) return showError('请先配置本机 DeepSeek API key 并重启服务，或使用离线演示。');
  if (!payload.objective) return showError('请填写消息内容。');
  payload.limits={};
  for (const field of runLimitFields) {
   const value = Number(fields.get(field.name));
   if (!Number.isInteger(value)||value<field.min||value>field.max) return showError(`${field.label}须为 ${field.min}–${field.max} 之间的整数。`);
   payload.limits[field.name]=value;
  }
 }
 if (chat) {
  const recipient = fields.get('recipient');
  const intent = fields.get('intent');
  const members = mode === 'group' ? fields.getAll('member') : [recipient];
  if (mode === 'group' && !members.length) return showError('请先选择本轮处理消息的群成员。');
  if (!recipient || !members.includes(recipient)) return showError('请选择本轮回复的群成员或私聊角色。');
  payload.conversation = {kind:mode==='group'?'group':'dm',members,recipient,intent};
  if (mode==='group' && intent==='edit' && fields.get('dispatch')==='team') payload.mode='team';
 }
 if ((chat||mode==='team') && fields.get('intent')!=='question') {
  payload.acceptance_files=parseFiles(fields.get('acceptance_files'));
  if (payload.mode==='team' && fields.get('required_source_review_paths')?.trim()) {
   payload.required_source_review_paths=parseFiles(fields.get('required_source_review_paths'));
  }
  try {
   if (fields.get('acceptance_checks')?.trim()) payload.acceptance_checks=readRules(fields.get('acceptance_checks'));
   if (payload.mode==='team' && fields.get('child_checks')?.trim()) payload.child_checks=JSON.parse(fields.get('child_checks'));
  } catch(error){return showError(error.message);}
 }
 const fingerprint = JSON.stringify(payload);
 if (state.submission?.fingerprint!==fingerprint) state.submission={fingerprint,id:crypto.randomUUID()};
 payload.request_id=state.submission.id;
 button.disabled=true;
 const text=button.innerHTML;
 button.innerHTML='<span class="spinner"></span>正在发送';
 try {
  const response=await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});
  state.submission=null;
  if ($('#task-dialog').open) $('#task-dialog').close();
  location.hash = chat ? chatLink(response.run.id) : '#run/'+encodeURIComponent(response.run.id);
  toast(chat?'消息已发送。':'任务已发起。');
  await refresh();
 } catch(error) {
  showError(error.message);
  if (error.code==='forbidden') {state.config=null;await refresh();}
 } finally {
  if (button.isConnected) {button.disabled=false;button.innerHTML=text;}
 }
}
let fileRequest=0;
async function selectFile(path) {
 const run=state.runId, request=++fileRequest;
 state.selectedFile=path;
 state.fileData=null;
 render();
 if (databaseFile(path)) { state.fileData={path,binary:true}; render(); return; }
 let data;
 try { data=await api(fileURL(run,path,'artifact')); }
 catch(error) { data={error:error.message}; }
 if (request!==fileRequest || state.runId!==run || state.selectedFile!==path) return;
 state.fileData=data;
 if (state.tab==='artifacts') render();
}
function download(name,content,type='text/plain'){const url=URL.createObjectURL(new Blob([content],{type:`${type};charset=utf-8`}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function cancelRun(){
 const id=state.runId;if(!id||!state.detail||!runActive(state.detail.run)||runStatus(state.detail.run)==='cancel_requested')return;
 state.cancelling.add(id);render();
 try{const response=await api(`/api/runs/${encodeURIComponent(id)}/cancel`,{method:'POST',body:'{}'});if(state.runId===id&&state.detail){state.detail.run=response.run;render();}toast(response.run.cancel_requested?'取消请求已记录，等待当前调用返回。':'任务已结束，状态已更新。');await refresh();}
 catch(error){toast(error.message);}
 finally{state.cancelling.delete(id);if(state.runId===id)render();}
}
async function downloadArtifacts(){
 const id=state.runId;if(!id||state.downloads.has(id))return;
 state.downloads.add(id);render();
 try{
  const response=await fetch(`/api/runs/${encodeURIComponent(id)}/artifacts.zip`,{headers:{Accept:'application/zip'}});
  if(!response.ok){let detail;try{detail=await response.json();}catch{}throw new Error(detail?.error||(response.status===413?'产物超出 ZIP 下载范围，请从本机工作区读取。':'产物下载失败，请稍后重试。'));}
  const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=`${id}-artifacts.zip`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('产物 ZIP 已下载，包含文件的相对路径。');
 }catch(error){toast(error.message);}
 finally{state.downloads.delete(id);if(state.runId===id)render();}
}
function showConnection(){const ready=state.config?.provider_ready;$('#connection-info').innerHTML=`<p>${ready?'DeepSeek 已从本机环境或 .env 读取配置。密钥不会发送到网页。':'离线演示已可用。使用真实模型前，在仓库根目录的 .env 中配置 DEEPSEEK_API_KEY，然后重启本地服务。'}</p><p>当前模型：<code>${e(state.config?.model||'尚未读取')}</code></p><p>单次输出上限：<code>${e(state.config?.max_output_tokens??'未知')} token</code>。可在 .env 中通过 DEEPSEEK_MAX_TOKENS 调整，重启服务后生效。</p><p>任务与文件保存在启动时选择的运行目录。可在角色库管理自定义角色。</p>`;$('#info-dialog').showModal();}
document.addEventListener('submit',event=>{if(event.target.matches('.role-editor')){event.preventDefault();saveRoleEditor(event.target);return;}if(event.target.matches('.human-form,.review-form')){event.preventDefault();submitDiscussion(event.target);return;}if(event.target.matches('.reply-form')){event.preventDefault();sendReply(event.target);return;}if(event.target.matches('.task-form')){event.preventDefault();submit(event.target);}});
document.addEventListener('change',event=>{const form=event.target.closest('form');
 if(form?.matches('.role-editor') && event.target.name==='scope'){
  form.querySelector('[data-project-choice]').hidden=event.target.value!=='project';return;
 }
 if(form?.matches('.reply-form')){rememberReply(form);if(event.target.name==='intent'||event.target.name==='dispatch')render();return;}if(event.target.name==='member'){if(form.matches('.channel-form'))chatDirectory.updateChannelLead(form);else updateMemberPicker(form);}if(event.target.name==='intent'||event.target.name==='dispatch')syncIntent(form);if(event.target.name==='mode')swapMode(event.target.closest('form'),event.target.value);if(event.target.id==='run-filter'){state.filter=event.target.value;patchHTML($('#all-runs'),runList());}if(event.target.id==='log-filter'){state.logFilter=event.target.value;render();}});
document.addEventListener('input',event=>{
 if(event.target.id==='role-search'){roleView.search=event.target.value;patchHTML($('#directory-sections'),roleSections());return;}
 const reply=event.target.closest('.reply-form');if(reply)rememberReply(reply);if(event.target.id==='run-search'){state.search=event.target.value;patchHTML($('#all-runs'),runList());}});
document.addEventListener('toggle',event=>{
 const disclosure = event.target;
 if (!disclosure.open || !disclosure.matches?.('.room-material')) return;
 const button = disclosure.querySelector('button[data-room-history]');
 if (button && !button.disabled && !disclosure.querySelector('[role="alert"]')) loadRoomPage(button);
},true);
document.addEventListener('toggle',event=>{if(event.target.matches?.('[data-log]')){const key=event.target.dataset.log;if(event.target.open)state.logOpen.add(key);else state.logOpen.delete(key);}},true);
document.addEventListener('click',event=>{
 const historyButton = event.target.closest('[data-room-history]');
 if(historyButton){loadRoomPage(historyButton);return;}
 if(event.target.closest('.skip-link')){event.preventDefault();main.focus();return;}
 const roleButton=event.target.closest('[data-role-action]');
 if(roleButton){runRoleAction(roleButton);return;}
 const template=event.target.closest('[data-template]');if(template){const value=template.dataset.template,form=$('form[data-prefix="home"]');swapMode(form,value==='maintenance'?'single':value,value==='maintenance'?'生成一份电脑维护诊断计划 maintenance.md，包含信息收集、排查步骤和验证方法。':'');if(value==='maintenance'){const f=$('form[data-prefix="home"]');f.querySelector('[name="acceptance_files"]').value='maintenance.md';}$('#home-objective')?.focus();return;}
 const task=event.target.closest('[data-task]');if(task){const key=task.dataset.task;state.expanded.has(key)?state.expanded.delete(key):state.expanded.add(key);render();return;}
 const tab=event.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;render();if(state.tab==='artifacts'&&!state.selectedFile&&state.detail?.artifacts?.length)selectFile(state.detail.artifacts[0].path);return;}
 const file=event.target.closest('[data-file]');if(file){selectFile(file.dataset.file);return;}
 const action=event.target.closest('[data-action]')?.dataset.action;
 if(action==='unlimit-steps'||action==='unlimit-tokens') {
  const input=event.target.closest('fieldset').querySelector(action==='unlimit-steps'?'[name="max_steps"]':'[name="max_total_tokens"]');
  input.value='0';
  input.dispatchEvent(new Event('input',{bubbles:true}));
 }
 if(action==='pick-workspace')pickWorkspace(event.target.closest('button'));
 if(action==='new')openForm();
 if(action==='new-dm')openForm('single');
 if(action==='new-group')openForm('group');
 if(action==='close-dialog')$('#task-dialog').close();
 if(action==='close-info')$('#info-dialog').close();
 if(action==='connection')showConnection();
 if(action==='menu'){$('#sidebar').classList.toggle('open');mobileNav();}
 if(action==='refresh' && state.route==='roles')loadRole();
 if(action==='refresh' && state.route==='assistant')studioAssistant.refresh(true);
 if(action==='refresh' && !['roles','assistant'].includes(state.route))refresh().then(()=>toast(state.online?'任务状态已更新。':'服务未连接，请检查启动终端。'));
 if(action==='advanced'){const button=event.target.closest('button'),panel=button.closest('form').querySelector('.advanced');panel.hidden=!panel.hidden;button.setAttribute('aria-expanded',String(!panel.hidden));}
 if(action==='cancel-run')cancelRun();
 if(action==='download-artifacts')downloadArtifacts();
 if(action==='download-file'&&state.fileData?.content!==undefined)download(state.selectedFile.split('/').pop(),state.fileData.content);
 if(action==='download-diagnostics'&&state.detail)download(`${state.runId}-diagnostics.json`,JSON.stringify({run:state.detail.run,diagnostics:state.detail.diagnostics,tasks:state.detail.tasks,events:state.detail.events,events_truncated:state.detail.events_truncated},null,2),'application/json');
 if(action==='download-report'&&state.detail?.report)download('report.md',state.detail.report,'text/markdown');
 if(!event.target.closest('.sidebar')&&!event.target.closest('[data-action="menu"]')){$('#sidebar').classList.remove('open');mobileNav();}
});
document.addEventListener('keydown',event=>{const editing=event.target.matches('input,textarea,select,[contenteditable="true"]');if(event.key==='n'&&!editing&&!event.metaKey&&!event.ctrlKey&&!document.querySelector('dialog[open]')){event.preventDefault();openForm();}if(event.key==='Escape'){$('#sidebar').classList.remove('open');mobileNav();}if((event.metaKey||event.ctrlKey)&&event.key==='Enter'&&event.target.closest('.reply-form')){event.preventDefault();sendReply(event.target.closest('.reply-form'));}if((event.metaKey||event.ctrlKey)&&event.key==='Enter'&&event.target.closest('.task-form')){event.preventDefault();submit(event.target.closest('.task-form'));}if(event.target.matches('[role="tab"]')&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const tabs=[...document.querySelectorAll('.tabbar [role="tab"]')],index=tabs.indexOf(event.target),next=tabs[(index+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length];state.tab=next.dataset.tab;render();$(`#tab-${state.tab}`).focus();if(state.tab==='artifacts'&&!state.selectedFile&&state.detail?.artifacts?.length)selectFile(state.detail.artifacts[0].path);}});
window.addEventListener('hashchange',route);window.matchMedia('(max-width:650px)').addEventListener('change',mobileNav);
async function boot(){hydrateIcons();try{state.config=await api('/api/bootstrap');const data=await api('/api/runs');state.runs=data.runs||[];setOnline(true);state.initialized=true;await Promise.all([route(),chatDirectory.refresh()]);}catch(error){setOnline(false);main.innerHTML=`<div class="page error-panel"><h2>工作室暂时无法连接</h2><p>${e(error.message)}</p><button class="secondary" data-action="refresh">重新连接</button></div>`;}setInterval(async()=>{if(document.hidden)return;const missing=!state.config;await refresh();if(missing&&state.config)await route();},1800);}
boot();
