'use strict';
// Buildless local UI. All runtime-owned text passes through escapeHTML; no raw
// model HTML, remote resources or provider credentials enter this application.
const icons = {
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
const state = {config:null,chats:null,runs:[],detail:null,route:'home',runId:null,tab:'overview',search:'',filter:'all',logFilter:'all',selectedFile:null,fileData:null,expanded:new Set(),logOpen:new Set(),polling:false,online:true,initialized:false,detailSignature:'',submission:null,cancelling:new Set(),downloads:new Set()};
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
function updateSidebar(){mobileNav(); setText($('#nav-count'),state.runs.length);patchHTML($('#recent-nav'),state.runs.slice(0,4).map(r=>`<a class="recent-link" data-key="recent-${e(r.id)}" href="#run/${encodeURIComponent(r.id)}">${icon(stateIcon(r.status))}<span>${e(r.title)}</span></a>`).join('')||'<p class="side-empty">你的任务会显示在这里</p>');document.querySelectorAll('[data-nav]').forEach(n=>n.classList.toggle('active',n.dataset.nav===state.route || (state.route==='detail'&&n.dataset.nav===(state.tab==='chat'?'chats':'runs'))));$('#breadcrumb-current').textContent=state.route==='detail'?'任务详情':state.route==='runs'?'全部任务':state.route==='chats'?'对话':state.route==='roles'?'角色库':'工作台';if(state.config){$('#provider-label').textContent=state.config.provider_ready?'DeepSeek 已配置':'离线模式就绪';$('#provider-caption').textContent=state.config.provider_ready?state.config.model:'无需 API key，即可体验';$('#provider-dot').classList.toggle('ready',state.config.provider_ready);}}
const runLimitFields = [
 {name:'max_steps',label:'执行轮数上限',value:100,min:1,max:1000},
 {name:'max_total_tokens',label:'累计 token 预算',value:1000000,min:1,max:100000000},
 {name:'max_seconds',label:'时间预算（秒）',value:3600,min:1,max:86400}
];
function runActive(run){return ['running','queued','verifying','waiting_user','cancel_requested'].includes(run?.status);}
function runStatus(run){return runActive(run)&&(run.cancel_requested||state.cancelling.has(run.id))?'cancel_requested':run.status;}
function limitFields(demo, values={}){return `<fieldset class="run-limits"><legend>本次执行预算</legend><div class="run-limit-grid">${runLimitFields.map(field=>`<label class="field-label">${field.label}<input type="number" name="${field.name}" value="${e(values[field.name]??state.config?.limits?.[field.name]??field.value)}" min="${field.min}" max="${field.max}" step="1" required ${demo?'disabled':''}></label>`).join('')}</div><p class="muted-note">累计 token 包含多轮输入和输出，与单次输出上限、上下文窗口分别计量。多 Agent 的预算按各执行任务计量。</p></fieldset>`;}
const chatDrafts = new Map();
const chatSubmissions = new Map();
const pendingChat = new Set();
const chatRoles = () => [...(state.config?.conversation_roles || []),
 ...(state.config?.custom_profiles || []).map(profile => ({id:profile.id,label:profile.name}))];
const roleLabel = id => state.detail?.conversation?.profiles?.find(p=>p.id===id)?.name || chatRoles().find(role => role.id === id)?.label || id;
const chatMode = mode => mode === 'single' || mode === 'group';
const chatLink = id => '#run/' + encodeURIComponent(id) + '/chat';

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
 const action = preview ? '预览' : binary ? '下载' : '查看';
 return `<a class="turn-file" href="${e(href)}" ${preview?'target="_blank" rel="noopener noreferrer"':binary?'download':''}
  title="${e(file.project_path||path)}${file.sha256?' · SHA-256 '+e(file.sha256):''}">${icon(binary?'download':preview?'link':'file')}<span>${e(file.project_path || path)}</span><small>${action} · ${e(sizeText(file.size))}</small></a>`;
}
function turnFiles(turn) {
 const files = turn.artifacts || [];
 const delivered = turn.workspace_delivery;
 const status = delivered?.status==='published' ? `已写入本地工作区 · ${delivered.files.length} 个文件`
  : delivered?.status==='failed' ? delivered.message : '';
 if (!files.length && !status) return '';
 return `<details class="turn-files" ${files.length <= 5 ? 'open' : ''}><summary>本轮文件 · ${files.length}</summary>
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
 if (form.elements.acceptance_checks) form.elements.acceptance_checks.disabled=field.disabled;
}

function chatList(items) {
 if (items === null) return '<p class="muted-note">正在读取对话…</p>';
 if (!items.length) return empty('还没有对话','选择私聊角色，或邀请几位角色加入群聊。');
 return `<div class="run-list">${items.map(item => `<a class="run-row" data-key="chat-${e(item.id)}" href="${chatLink(item.head_run_id)}">
   <span class="run-icon">${icon(item.kind === 'group' ? 'users' : 'cpu')}</span>
   <div class="run-info"><div class="run-title">${e(item.title)}</div>
    <div class="run-meta"><span>${item.kind === 'group' ? '群聊' : '私聊'}</span><span>·</span>
     <span>${e(item.members.map(roleLabel).join('、'))}</span><span>·</span><span>${e(item.turn_count)} 轮</span></div></div>
   <span class="run-time">${e(dateText(item.updated_at))}</span><span class="chevron">${icon('chevron')}</span></a>`).join('')}</div>`;
}

function chatsPage() {
 return `<section class="page"><div class="page-top chat-page-top"><h1>对话</h1>
  <div class="chat-actions"><button class="secondary" data-action="new-group">${icon('users')}新建群聊</button>
  <button class="primary" data-action="new-dm">${icon('plus')}新建私聊</button></div></div>
  <a class="back-link" href="#roles">${icon('book')}管理工作室角色</a>
  <div id="chat-list">${chatList(state.chats)}</div></section>`;
}

const roleView = {id:null, detail:null, error:'', serial:0, kind:'memory', filter:'current'};
const knowledgeKinds = {memory:'记忆',skill:'技能'};
const knowledgeStates = {active:'已启用',candidate:'待确认',archived:'已归档',rejected:'已拒绝'};
const roleLink = role => '#roles/' + encodeURIComponent(role);
const profileURL = role => '/api/profiles/' + encodeURIComponent(role);
const entryId = row => row.id || row.entry_id;

function roleMark(profile) {
 const first = Array.from(profile.name.trim())[0] || '·';
 const tone = Array.from(profile.id).reduce((n,c)=>n+c.codePointAt(0),0) % 4;
 return `<span aria-hidden="true" class="role-mark tone-${tone}">${e(first)}</span>`;
}

function roleLibrary() {
 const profiles = state.config?.custom_profiles || [];
 const cards = profiles.map(p=>`<a class="role-card" href="${roleLink(p.id)}">
   ${roleMark(p)}<div><h2>${e(p.name)}</h2><code>${e(p.id)}</code></div>
   <p>${e(p.prompt)}</p><span class="card-open">打开角色 ${icon('arrow')}</span></a>`).join('');
 return `<section class="page roles-page"><header class="page-top role-page-top"><div><h1>角色库</h1>
   <p class="page-subtitle">管理长期协作的助手、项目对话和已确认的经验。</p></div>
   <button type="button" class="primary" data-role-action="new-role">${icon('plus')}创建角色</button></header>
   ${cards ? `<div class="role-cards">${cards}</div>` : empty('还没有自定义角色','创建一个角色，写下它的职责，就可以开始私聊。')}</section>`;
}

function roleConversations(chats) {
 if (!chats.length) return '<p class="role-empty">这个角色还没有参加项目对话。</p>';
 return `<div class="role-conversations">${chats.map(c=>`<a href="${chatLink(c.head_run_id)}">
   ${icon(c.kind==='dm'?'cpu':'users')}<span><strong>${e(c.title)}</strong><small>${[c.kind==='dm'?'私聊':'群聊', c.status?labels[c.status] || c.status:'',c.turn_count?c.turn_count+' 轮':'', c.updated_at?dateText(c.updated_at):''].filter(Boolean).map(e).join(' · ')}</small></span>
   ${icon('chevron')}</a>`).join('')}</div>`;
}

function legacyConversations(rooms) {
 if (!rooms.length) return '';
 return `<section class="legacy-rooms"><h3>可关联的旧会话</h3>
  <p>关联后，从下一轮开始使用这个角色的记忆和技能。</p>
  ${rooms.map(c=>`<div class="legacy-room" data-key="legacy-${e(c.id)}">
    <a href="${chatLink(c.head_run_id)}">${e(c.title)}</a><small>${c.kind==='dm'?'私聊':'群聊'}</small>
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

function rolePage() {
 if (!roleView.id) return roleLibrary();
 const back = `<a class="back-link" href="#roles">${icon('left')}返回角色库</a>`;
 if (!roleView.detail) return `<section class="page roles-page">${back}${roleView.error?`<p class="error-panel" role="alert">${e(roleView.error)} <button data-role-action="reload">重试</button></p>`:'<p class="muted-note">正在读取角色…</p>'}</section>`;
 const {role,entries,active_entries,conversations} = roleView.detail;
 const dm = conversations.find(c=>c.kind==='dm');
 const openDM = `<button type="button" class="${dm?'secondary':'primary'}" data-role-action="start-dm">${icon('plus')}新建私聊</button>`;
 return `<section class="page roles-page">${back}<header class="role-detail-head"><div class="role-title">${roleMark(role)}
  <div><h1>${e(role.name)}</h1><code>${e(role.id)}</code></div></div>
  <div class="chat-actions">${dm?`<a class="primary" href="${chatLink(dm.head_run_id)}">继续最近私聊 ${icon('arrow')}</a>`:''}${openDM}</div></header>
  <section class="role-brief"><div><h2>角色职责</h2><button class="text-button" data-role-action="edit-role">${icon('tune')}编辑角色</button></div><p>${e(role.prompt)}</p></section>
  <div class="role-columns">${knowledgePanel(entries,active_entries,conversations)}
   <section class="panel role-projects"><div class="panel-heading"><span>近期项目对话</span><small>${e(conversations.length)} 个</small></div>${roleConversations(conversations)}${legacyConversations(roleView.detail.unbound_conversations || [])}</section></div></section>`;
}

async function loadRole() {
 const id = roleView.id;
 const serial = ++roleView.serial;
 try {
  const data = await api(id ? profileURL(id)+'/knowledge' : '/api/profiles');
  if (state.route!=='roles' || serial!==roleView.serial) return;
  if (id) {
   roleView.detail=data;
   const profiles=state.config.custom_profiles || [];
   state.config.custom_profiles=[...profiles.filter(p=>p.id!==id),data.role];
  }
  else if (Array.isArray(data.profiles)) state.config.custom_profiles=data.profiles;
  roleView.error='';
 } catch (error) {
  if (state.route!=='roles' || serial!==roleView.serial) return;
  roleView.error=error.message;
  if (!id || roleView.detail) toast(error.message);
 }
 render();
}

function openRoleEditor(type, entry) {
 const profile = roleView.detail?.role;
 const profileForm = type==='new-role' || type==='edit-role';
 const editing = type==='edit-role' || type==='edit-entry';
 const kind = entry?.kind || roleView.kind;
 const title = profileForm ? (editing?'编辑角色':'创建角色') : (editing?'编辑':'添加')+knowledgeKinds[kind];
 const formRole = (profileForm && !editing) ? '' : profile.id;
 const profileFields = () => `<label class="field-label">角色名称<input name="name" required maxlength="100" value="${editing?e(profile.name):''}" placeholder="例如，电脑维护助手"></label>
   <label class="field-label">角色职责<textarea name="prompt" required maxlength="4000" rows="5" placeholder="它长期负责什么？哪些情况需要先和你确认？">${editing?e(profile.prompt):''}</textarea></label>
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

async function refreshChats() {
 try {
  const data = await api('/api/conversations');
  if (state.route !== 'chats') return;
  state.chats = data.conversations;
  patchHTML($('#chat-list'),chatList(state.chats));
 } catch (error) {
  if (state.route === 'chats') patchHTML($('#chat-list'),`<p class="error-panel">${e(error.message)}</p>`);
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
 const unavailable = chatTransitionNotice(thread,run);
 if (unavailable) return unavailable;
 const draft = chatDrafts.get(thread.id) || {};
 const last = thread.turns.at(-1);
 const recipient = draft.recipient || last?.recipient || thread.members[0];
 const status = pendingChat.has(thread.id);
 const intent = draft.intent || 'edit';
 const hasProvider = state.config?.provider_ready;
 return `<form class="reply-form" data-key="composer-${e(thread.id)}" data-thread="${e(thread.id)}" data-parent="${e(run.id)}">
   <fieldset ${status ? 'disabled' : ''}>
    <label class="composer-label" for="chat-reply">继续对话</label>
    <textarea id="chat-reply" name="message" maxlength="8000" required placeholder="提出问题，或描述下一步要修改的地方…">${e(draft.message || '')}</textarea>
    <div class="reply-options">
     ${thread.kind==='group' && intent==='edit'?dispatchField(draft.dispatch||last?.mode||'single'):''}
     <label class="field-label">回复角色<select name="recipient" ${thread.kind === 'dm' ? 'disabled' : ''}>${roleOptions(thread.members,recipient)}</select></label>
     <label class="field-label">消息类型<select name="intent">${intentOptions(intent)}</select></label>
     <label class="field-label" ${intent === 'question' ? 'hidden' : ''}>本轮验收文件（选填）
      <input name="acceptance_files" ${intent === 'question' ? 'disabled' : ''} placeholder="例如 snake/game.js" value="${e(draft.files || '')}"></label>
    </div>
    <details class="studio-disclosure"><summary>本轮选项与预算</summary>
     ${intent==='edit'?`<label class="field-label">项目文件<select name="base">
       <option value="published" ${draft.base==='published'?'selected':''}>沿用最近发布版本</option>
       ${run.status==='failed' && last?.intent==='edit'?`<option value="failed" ${draft.base==='failed'?'selected':''}>接着本轮未完成文件</option>`:''}
       <option value="empty" ${draft.base==='empty'?'selected':''}>新任务，使用空工作区</option></select></label>
       ${ruleFields(draft.rules||'')}${thread.kind==='group' && (draft.dispatch||last?.mode)==='team'?childFields(draft.children):''}`:''}
     ${limitFields(false,draft.limits || run.limits || {})}</details>
    <div class="reply-footer"><span class="muted-note">${hasProvider ? (intent === 'question' ? '仅查询项目文件与对话记录。' : '基于最近可用的项目版本修改，旧版可随时查看。') : '继续对话需要配置本机 DeepSeek API key。'}</span>
     <button type="submit" class="primary" ${!hasProvider || status ? 'disabled' : ''}>${status ? '正在发送…' : '发送'}${icon('arrow')}</button></div>
   </fieldset>${draft.error ? `<p class="form-error" role="alert">${e(draft.error)}</p>` : ''}</form>`;
}

function conversationPanel(detail) {
 const thread = detail.conversation;
 if (!thread) return empty('当前运行没有项目对话','可从“对话”发起私聊或群聊。');
 const latestVersion = thread.artifact_run_id;
 const history = thread.turns.map(turn => {
  const link = turn.run_id === detail.run.id
   ? '<button class="text-button" data-tab="overview">本轮详情</button>'
   : `<a class="text-button" href="#run/${encodeURIComponent(turn.run_id)}">查看本轮</a>`;
  const note = runActive(turn) ? '正在处理…' : '本轮没有保存回复正文，可在运行详情查看产物和记录。';
  return `<article class="chat-turn" data-key="turn-${e(turn.run_id)}">
   <div class="chat-user"><div class="speaker">你 <small>发给 ${e(roleLabel(turn.recipient))}</small></div>
   <p>${e(turn.user)}</p></div>
   ${roomMessages(turn.events||[],turn.run_id)}
   <div class="chat-agent"><div class="speaker">${icon('cpu')} ${e(roleLabel(turn.recipient))} ${link}</div>
    ${turn.answer ? `<div class="chat-answer prose">${markdown(turn.answer)}</div>` : `<p class="muted-note">${e(note)}</p>`}
    ${turn.answer_truncated ? '<small>这里只展示回复节选。</small>' : ''}${turnFiles(turn)}</div></article>`;
 }).join('');
 const versionLink = latestVersion === detail.run.id
  ? '<button class="text-button" data-tab="artifacts">查看当前产物</button>'
  : latestVersion ? `<a class="text-button" href="#run/${encodeURIComponent(latestVersion)}/artifacts">查看项目产物</a>` : '';
 return `<section class="chat-thread" data-key="thread-${e(thread.id)}">
   <header class="thread-head"><div><h2>${thread.kind === 'group' ? '项目群聊' : '项目私聊'}</h2>
    <p>${e(thread.members.map(roleLabel).join('、'))}</p>
    ${thread.workspace_path?`<p class="thread-workspace">${icon('folder')}工作区 <code>${e(thread.workspace_path)}</code></p>`:''}</div><div class="chat-actions">${versionLink}
   <a class="text-button" href="/api/runs/${encodeURIComponent(detail.run.id)}/room.zip" download>导出完整会话</a></div></header>
   <div class="chat-history">${history}</div>${conversationComposer(thread,detail.run)}</section>`;
}

function rememberReply(form) {
 const fields = form.elements;
 const draft = {message:fields.message.value,recipient:fields.recipient.value,intent:fields.intent.value,files:fields.acceptance_files.value,
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
 const payload = {mode:draft.intent==='edit'&&draft.dispatch==='team'?'team':'single',limits:draft.limits,objective:draft.message.trim(),conversation:{
  parent_run_id:form.dataset.parent,recipient:draft.recipient,intent:draft.intent
 }};
 if (draft.intent === 'edit') {
  payload.acceptance_files = parseFiles(draft.files);
  if (draft.base==='failed') payload.conversation.recover_failed=true;
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
  if (state.runId === form.dataset.parent) location.hash = chatLink(data.run.id);
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
   ${mode==='team' ? '<label class="field-label">调度方式<select name="scheduler"><option value="serial">串行 · 按依赖执行</option><option value="parallel">分层并发</option><option value="evented">事件驱动</option></select></label>' : ''}
   <label class="field-label">模型思考<select name="thinking" ${demo?'disabled':''}><option value="disabled">关闭</option>
     <option value="enabled" ${state.config?.thinking==='enabled'?'selected':''}>开启</option></select></label>
   ${(chat||mode==='team') ? '<label class="field-label">本轮验收文件（选填）<input name="acceptance_files" placeholder="例如 snake/index.html, snake/game.js"><small>填写工作区相对路径，多个文件用逗号分隔。</small></label>' : ''}
   ${!demo?ruleFields():''}
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
 if (run.mode==='single' && conversationKind) return conversationKind==='group' ? '群聊' : '私聊';
 return modeLabels[run.mode] || run.mode || '历史运行';
}
function runRow(r){return `<a class="run-row" data-key="run-${e(r.id)}" href="#run/${encodeURIComponent(r.id)}"><span class="run-icon">${icon(r.mode?.includes('team')?'branch':'file')}</span><div class="run-info"><div class="run-title">${e(r.title || '未命名任务')}</div><div class="run-meta"><span>${e(runModeLabel(r))}</span><span>·</span><span>${r.completed_count || 0}/${r.task_count || 0} 个任务完成</span><span>·</span><span>${r.artifact_count || 0} 个产物</span></div></div>${badge(runStatus(r))}<span class="run-time">${e(dateText(r.created_at))}</span><span class="chevron">${icon('chevron')}</span></a>`;}
function empty(title,description,action=''){return `<div class="empty-state"><div class="empty-illustration">${icon('layers')}</div><h3>${e(title)}</h3><p>${e(description)}</p>${action}</div>`;}
function home(){return `<section class="page home-page"><div class="home-heading"><div><h1>工作台</h1><p>发起任务，跟踪执行，检查结果。</p></div></div>${formHTML('home',state.config?.provider_ready?'single':'team-demo')}<section class="starter-section"><div class="section-kicker">试用示例</div><div class="starter-grid"><button class="starter" data-template="team-demo"><span class="starter-icon">${icon('branch')}</span><strong>协作整理笔记</strong></button><button class="starter" data-template="demo"><span class="starter-icon">${icon('terminal')}</span><strong>工具执行演示</strong></button><button class="starter" data-template="maintenance"><span class="starter-icon">${icon('cpu')}</span><strong>电脑维护计划</strong></button></div></section><section class="runs-section"><div class="section-header"><h2 class="section-title">最近任务 <span class="count-badge">${state.runs.length}</span></h2><a href="#runs" class="text-button">查看全部</a></div><div id="home-runs">${state.runs.length?`<div class="run-list">${state.runs.slice(0,5).map(runRow).join('')}</div>`:empty('还没有任务','运行上方演示，查看任务分工、工具调用和验收结果。')}</div></section></section>`;}
function filteredRuns(){const query=state.search.toLowerCase();return state.runs.filter(r=>(!query||`${r.title} ${r.id}`.toLowerCase().includes(query))&&(state.filter==='all'||(state.filter==='active'?['running','verifying','queued'].includes(r.status):r.status===state.filter)));}
function runList(){const runs=filteredRuns();return runs.length?`<div class="run-list">${runs.map(runRow).join('')}</div>`:empty(state.runs.length?'没有匹配的任务':'还没有任务记录',state.runs.length?'试试其他关键词或状态。':'从新建任务开始，运行记录会保存在这里。');}
function runsPage(){return `<section class="page"><div class="page-top"><div><span class="eyebrow">TASK HISTORY</span><h1>所有任务</h1><p class="page-subtitle">查看已保存的执行与验收记录。</p></div><button class="primary" data-action="new">${icon('plus')}新建任务</button></div><div class="filters"><input id="run-search" class="filter-input" placeholder="搜索任务名称…" aria-label="搜索任务" value="${e(state.search)}"><select id="run-filter" class="filter-select" aria-label="筛选任务状态">${[['all','所有状态'],['active','运行中'],['completed','已完成'],['unverified','未验证'],['failed','失败'],['interrupted','待核实']].map(([v,t])=>`<option value="${v}" ${state.filter===v?'selected':''}>${t}</option>`).join('')}</select></div><div id="all-runs">${runList()}</div></section>`;}
function taskNodes(tasks){const map=new Map(tasks.map(t=>[t.task_id,t]));const children=new Map();tasks.forEach(t=>{const parent=map.has(t.parent_id)?t.parent_id:null;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(t);});const seen=new Set();function walk(task,depth){if(seen.has(task.task_id))return '';seen.add(task.task_id);const expanded=state.expanded.has(task.task_id);return `<div data-key="task-${e(task.task_id)}" class="task-node ${depth?'child':''}"><button id="task-${e(task.task_id)}" class="task-button" data-task="${e(task.task_id)}" aria-expanded="${expanded}"><span class="task-state-icon">${icon(stateIcon(task.status))}</span><span class="task-text"><span class="task-name">${e(task.objective)}</span><span class="task-subline"><span>${e(task.assignee_profile_id || task.owner_profile_id || '—')}</span><span>·</span><span>${depth?'子任务':'整体目标'}</span>${task.deps?.length?`<span>· ${task.deps.length} 个前置任务</span>`:''}</span></span>${badge(task.status)}</button>${expanded?`<div class="task-inspector"><div class="inspector-grid"><span>任务身份</span><code>${e(task.task_id)}</code><span>执行尝试</span><code>${e(task.current_attempt_id||'尚未开始')}</code><span>状态版本</span><span>${e(task.revision)}</span>${task.deps?.length?`<span>依赖任务</span><code>${e(task.deps.join(', '))}</code>`:''}${task.reason?`<span>说明</span><span>${e(task.reason)}</span>`:''}</div></div>`:''}${(children.get(task.task_id)||[]).map(child=>walk(child,depth+1)).join('')}</div>`;}return (children.get(null)||[]).map(t=>walk(t,0)).join('')||empty('正在登记任务','任务领取后，依赖关系和执行状态会显示在这里。');}
function eventLabel(event){const names={step_start:'开始一轮决策与执行',plan_start:'正在请求任务规划',studio_configuration:'本次运行配置',studio_error:'工作室执行异常',case_registered:'任务已登记',task_start:'开始执行任务',step:'完成一轮决策与执行',tool_result:'工具返回结果',acceptance:'独立验收',task_end:'任务执行结束',orchestration_start:'开始规划与调度',orchestration_end:'整体目标执行结束',plan:'任务计划已生成',plan_usage:'规划器已返回',subtask_start:'开始执行子任务',subtask_end:'子任务执行结束',provider_error:'模型调用异常',task_registered:'子任务已登记',plan_degraded:'规划已降级',context_compacted:'上下文已整理',context_limit:'上下文容量不足',output_recovery:'正在恢复被截断的输出',output_recovery_exhausted:'输出恢复次数已用尽',budget_warning:'执行预算即将耗尽',cancel_observed:'已响应取消请求'};const detail=event.tool_name||event.tool||event.operation||event.agent||event.assignee||'';return `${names[event.kind]||event.kind||'执行事件'}${detail?' · '+detail:''}`;}
function summaryStats(detail){const s=detail.summary||{},r=detail.run;return `<div class="panel summary-panel"><h3>运行摘要</h3><div class="stat-line"><span>执行模式</span><strong>${e(runModeLabel(r, detail.conversation?.kind))}</strong></div><div class="stat-line"><span>完成任务</span><strong>${r.completed_count||0} / ${r.task_count||0}</strong></div><div class="stat-line"><span>运行耗时</span><strong>${duration(r.duration_ms)}</strong></div><div class="stat-line"><span>生成产物</span><strong>${detail.artifacts?.length||0} 个</strong></div><div class="stat-line"><span>${detail.summary_incomplete?'部分 token 记录':'已记录 token'}</span><strong>${Number(s.total_tokens||0).toLocaleString()}</strong></div><div class="summary-rule"></div><div class="summary-callout">${icon('shield')}${r.status==='completed'?'结果已通过独立验收。你可以在验收页查看检查项与证据。':endedWithoutChecks(detail)?'执行已结束。本轮未配置独立验收，可以查看产物或继续对话。':r.status==='unverified'?'独立验收尚未确认本轮结果，具体原因可在验收页查看。':r.status==='interrupted'?'当前服务未持有执行权，无法判断原执行者是否仍在运行。历史状态保留。':'每个任务的状态与验收记录会持续保存。'}</div><p class="muted-note">${r.mode?.includes('demo')?'离线演示的 token 为估算值。':'token 按已有记录汇总，不代表完整费用账单。'}</p></div>`;}
function overview(detail){const events=eventEntries(detail.events||[]).slice(-6).reverse();return `<div class="overview-grid"><div class="panel"><div class="panel-heading"><span>${icon('branch')}任务树</span><small>点击任务查看详情</small></div><div class="task-tree">${taskNodes(detail.tasks||[])}</div></div>${summaryStats(detail)}</div><div class="panel activity-panel"><div class="panel-heading"><span>${icon('clock')}最近动态</span><button class="text-button" data-tab="logs">查看完整日志 ${icon('arrow')}</button></div><div class="activity-list">${events.map(({event,key})=>`<div class="activity-row" data-key="${e(key)}"><span class="activity-time">${e(clockText(event.ts?event.ts*1000:event.iso))}</span>${icon(event.kind?.includes('end')?'check':'circle')}<span class="activity-label">${e(eventLabel(event))}</span></div>`).join('')||'<p class="muted-note">等待第一条执行记录…</p>'}</div></div>`;}
function logs(detail){const all=detail.events||[];const list=eventEntries(all).filter(({event})=>state.logFilter==='all'||(state.logFilter==='tools'?event.kind==='tool_result':state.logFilter==='acceptance'?event.kind==='acceptance':(event.kind?.includes('error')||['context_limit','output_recovery_exhausted'].includes(event.kind)||event.error_class||event.error||event.ok===false)));return `<div class="log-toolbar"><select id="log-filter" class="filter-select" aria-label="筛选日志">${[['all','全部事件'],['tools','工具调用'],['acceptance','验收事件'],['errors','错误事件']].map(([v,t])=>`<option value="${v}" ${state.logFilter===v?'selected':''}>${t}</option>`).join('')}</select><p>${detail.events_truncated?'仅展示最近部分日志 · ':''}${list.length} 条事件 · 点击展开详情</p></div>${list.length?`<div class="log-list">${list.map(({event,key})=>`<details class="log-entry ${event.error_class||event.ok===false||event.kind?.includes('error')||['context_limit','output_recovery_exhausted'].includes(event.kind)?'error':''}" data-key="${e(key)}" data-log="${e(key)}" ${state.logOpen.has(key)?'open':''}><summary><span class="activity-time">${e(clockText(event.ts?event.ts*1000:event.iso))}</span><span class="event-kind">${e(event.kind)}</span><span class="log-caption">${e(eventLabel(event))}${event.error||event.error_class||event.finish_reason==='length'?`<small>${e(event.finish_reason==='length'?'输出截断 (length) · '+event.completion_tokens+' token':event.error||event.preview||event.error_class)}</small>`:''}</span>${icon('chevron')}</summary><pre>${e(JSON.stringify(event,null,2))}</pre></details>`).join('')}</div>`:empty('这里还没有事件','执行日志会在任务运行时更新。')}`;}
function inlineMarkdown(text){return e(text).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');}
function markdown(source){const lines=String(source).split('\n');let result='',list=false,code=false,table=false,buffer=[];const closeList=()=>{if(list){result+='</ul>';list=false;}};const closeTable=()=>{if(table){result+='</tbody></table>';table=false;}};for(let i=0;i<lines.length;i++){const line=lines[i];if(line.startsWith('```')){closeList();closeTable();if(code){result+=`<pre><code>${e(buffer.join('\n'))}</code></pre>`;buffer=[];}code=!code;continue;}if(code){buffer.push(line);continue;}if(/^\|.+\|\s*$/.test(line)){closeList();const cells=line.trim().slice(1,-1).split('|').map(s=>s.trim());if(cells.every(c=>/^:?-+:?$/.test(c)))continue;if(!table){result+='<table><tbody>';table=true;}result+='<tr>'+cells.map(c=>`<td>${inlineMarkdown(c)}</td>`).join('')+'</tr>';continue;}closeTable();const heading=line.match(/^(#{1,3})\s+(.+)$/);if(heading){closeList();result+=`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`;continue;}if(/^[-*] /.test(line)){if(!list){result+='<ul>';list=true;}result+=`<li>${inlineMarkdown(line.slice(2))}</li>`;continue;}closeList();if(line.trim())result+=`<p>${inlineMarkdown(line)}</p>`;}closeList();closeTable();if(code)result+=`<pre>${e(buffer.join('\n'))}</pre>`;return result;}
function filePreview(){if(!state.selectedFile)return empty('选择一个产物','在左侧选择文件，预览实际生成的内容。');if(!state.fileData)return '<div class="initial-loading"><span class="spinner"></span>正在读取产物…</div>';if(state.fileData.error)return `<div class="error-panel">${e(state.fileData.error)}</div>`;const f=state.fileData;if(f.media_type==='image/png')return `<div class="file-header"><span>${e(f.path.split('/').pop())}</span><a class="text-button" href="${e(fileURL(state.runId,f.path,'download'))}" download>${icon('download')}下载截图</a></div><img class="project-screenshot" src="${e(fileURL(state.runId,f.path,'preview'))}" alt="${e(f.path)}">`;if(f.binary) return `<div class="file-header"><span>${e(f.path.split('/').pop())}</span><a class="text-button" href="${e(fileURL(state.runId,f.path,'download'))}" download>${icon('download')}下载数据库</a></div><p class="binary-note">SQLite 数据库，可下载后在本机打开。</p>`;return `<div class="file-header"><span>${e(f.path.split('/').pop())}</span>${f.path.toLowerCase().endsWith('.html')?`<a class="text-button" href="/api/runs/${encodeURIComponent(state.runId)}/preview?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener noreferrer">打开交互预览</a>`:''}<a class="text-button" href="${e(fileURL(state.runId,f.path,'download'))}" download>${icon('download')}下载文件</a></div>${f.truncated?'<div class="run-warning">仅预览前 256 KiB。下载可获取完整文件。</div>':''}<div class="file-content ${f.path.endsWith('.md')?'prose':''}">${f.path.endsWith('.md')?markdown(f.content):`<pre>${e(f.content)}</pre>`}</div>`;}
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
 const active=runActive(detail.run);
 const cancelling=runStatus(detail.run)==='cancel_requested';
 if(!issue&&!active)return '';
 const task=(detail.tasks||[]).find(t=>t.task_id===issue?.task_id);
 const verification={not_configured:'未配置',pending:'等待检查',not_recorded:'无记录',passed:'通过',failed:'未通过',error:'检查出错',unverified:'未验证'};
 const execution={running:'运行中',cancelled:'已取消',cancel_requested:'正在取消',failed:'失败',blocked:'依赖受阻',finished:'已结束',unknown:'待核实'};
 const activity=d.current_activity||[];
 const title=cancelling?'正在取消任务':active?(activity.length?activity.some(a=>a.phase==='planning')?'正在规划任务':'正在执行 Agent 步骤':'正在执行任务'):issue.title;
 return `<section class="diagnosis ${active?'running':issue?.severity==='warning'&&!endedWithoutChecks(detail)?'warning':''}" data-key="diagnosis"><div class="diagnosis-header"><h2>${e(title)}</h2><button class="secondary" data-action="download-diagnostics">${icon('download')}导出诊断</button></div>${active?`<p class="activity-live"><span class="dot"></span>${activity.length?activity.map(a=>`${e(({coordinate:'负责人分工',work:'执行任务',handoff:'成员交接',finalize:'负责人收尾',planning:'规划任务'})[a.phase]||a.task_id?.split(':').pop()||'整体目标')}${a.step?' 第 '+e(a.step)+' 轮':''}，已用时 ${duration(typeof a.started_ts==='number'?Date.now()-a.started_ts*1000:null)}`).join('；'):'任务状态会自动更新。'}</p>`:`<p>${task?`${e(task.assignee_profile_id||'Agent')}${issue.step?' 第 '+e(issue.step)+' 轮':''}：`:''}${e(issue.message)}</p>`}<div class="diagnosis-facts"><span>执行 <strong>${e(cancelling?'正在取消':execution[d.execution_status]||d.execution_status)}</strong></span><span>验收 <strong>${e(verification[d.acceptance_status]||d.acceptance_status)}</strong></span>${d.configuration?.max_steps!=null?`<span>轮数上限 <strong>${e(d.configuration.max_steps)}</strong></span>`:''}${d.configuration?.max_total_tokens!=null?`<span>累计 token 预算 <strong>${Number(d.configuration.max_total_tokens).toLocaleString()}</strong></span>`:''}${d.blocked_tasks?.length?`<span>后续受阻 <strong>${d.blocked_tasks.length} 个任务</strong></span>`:''}${issue?.completion_tokens!=null?`<span>本轮输出 <strong>${Number(issue.completion_tokens).toLocaleString()} token</strong></span>`:''}</div>${!active&&issue?.suggestion?`<p class="next-step">下一步：${e(issue.suggestion)}</p>`:''}${!active&&d.acceptance_status==='not_configured'&&issue?.code!=='no_acceptance'?'<p>此外，本次未配置独立验收，不能判定目标已达成。</p>':''}${detail.run.status==='unverified'&&d.execution_status==='failed'?'<p>旧版总状态保存为“未验证”；日志确认存在执行失败。原始记录保持不变。</p>':''}<details data-key="diagnostic-evidence"><summary>查看失败证据、阻塞链与运行配置</summary>${issue?.request_id?`<p>模型请求 <code>${e(issue.request_id)}</code></p>`:''}${issue?.finish_reason?`<p>结束原因 <code>${e(issue.finish_reason)}</code>；输出上限 ${e(issue.max_output_tokens??'旧记录未保存')}；耗时 ${duration(issue.latency_ms)}</p>`:''}${(d.blocked_tasks||[]).map(t=>`<p>${e(t.task_id?.split(':').pop())} / ${e(t.assignee)}：${e(t.reason)}</p>`).join('')}<p>${Object.keys(d.configuration||{}).length?`模型 ${e(d.configuration.model)}；thinking ${e(d.configuration.thinking)}；单次输出上限 ${e(d.configuration.max_output_tokens)} token；执行轮数上限 ${e(d.configuration.max_steps??'未记录')}；累计 token 预算 ${e(d.configuration.max_total_tokens??'未记录')}；任务时限 ${e(d.configuration.max_task_seconds??d.configuration.max_seconds??'未记录')} 秒。`:'旧运行没有保存配置快照；当前服务配置不代表当时配置。'}</p>${d.incomplete?'<p>日志已截断，此诊断只基于保留的记录。</p>':''}</details></section>`;
}
function roomMessages(events,runId) {
 const kinds={goal:'任务',assignment:'分工',member_report:'成员交付',runtime_reroute:'平台转交返工',human_question:'询问',human_answer:'用户答复',question_closed:'等待结束',human_review:'人工评审'};
 return events.map(row=>{
  const reserve = row.reserved;
  const handoff = row.handoff;
  const checks = handoff?.evidence?.check_signals || [];
  const receipt = handoff ? `<div class="handoff-receipt"><strong>${handoff.published?'已交接给负责人':'交接受阻'}</strong>
   <span>变更 ${e(handoff.changes_total??0)} 个文件</span>
   ${handoff.remaining_issue?`<p>${e(handoff.remaining_issue)}</p>`:''}
   ${checks.map(check=>`<p>${e(check)}</p>`).join('')}
   ${handoff.published && row.artifacts?.length ? `<details class="turn-files member-files" ${row.artifacts.length<=5?'open':''}>
    <summary>成员交付文件 · ${row.artifacts.length}</summary><div>${row.artifacts.map(file=>artifactLink(runId,file)).join('')}</div></details>` : ''}</div>` : '';
  return `<div class="room-event"><strong>${e(kinds[row.kind]||row.kind)} · ${e(row.role==='user'?'你':roleLabel(row.role))}</strong>${row.recipient?` → ${e(roleLabel(row.recipient))}`:''}
   <p>${e(row.text)}</p>${receipt}${reserve?`<small>后续委派与收尾预留 · ${e(reserve.steps_left)} 步 · ${e(Number(reserve.tokens_left).toLocaleString())} token</small>`:''}</div>`;
 }).join('');
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
  await refreshDetail();
 }catch(error){toast(error.message);}finally{button.disabled=false;}
}
function detailContent(){if(!state.detail)return '<div class="initial-loading"><span class="spinner"></span>正在读取任务…</div>';const d=state.detail,r=d.run;const tabs=[...(d.conversation?[['chat','users','项目对话',d.conversation.turns.length]]:[]),['overview','branch','任务概览',d.tasks?.length],['logs','terminal','执行日志',d.events?.length],['artifacts','folder','产物',d.artifacts?.length],['acceptance','shield','验收',d.acceptance?.length]];return `<section class="page"><div class="detail-top"><a class="back-link" href="#runs">${icon('left')}返回全部任务</a><div class="detail-heading"><h1>${e(r.title)}</h1><div class="detail-actions">${detailBadge(d)}${d.conversation&&state.tab!=='chat'?'<button class="secondary" data-tab="chat">进入对话</button>':''}${runActive(r)?`<button class="secondary" data-action="cancel-run" ${runStatus(r)==='cancel_requested'?'disabled':''}>${icon('x')}${runStatus(r)==='cancel_requested'?'正在取消…':'取消任务'}</button>`:''}</div></div><div class="detail-meta"><span>${icon('layers')}${e(runModeLabel(r, d.conversation?.kind))}</span><span>${icon('clock')}${e(dateText(r.created_at))}</span><span>${icon('file')}${d.artifacts?.length||0} 个产物</span></div></div>${runStatus(r)==='cancel_requested'?'<p class="run-warning" role="status">已请求停止后续执行；当前模型请求或工具调用可能需要先返回。保存的产物和日志会保留。</p>':''}${r.error&&!d.diagnostics?.issues?.length?`<div class="run-warning">${e(r.error)}</div>`:''}${r.status==='interrupted'?'<div class="run-warning">当前服务未持有这次执行。已保存的任务状态可查看；刷新不会自动继续执行或重放工具。</div>':''} ${humanQuestions(d)}${state.tab==='chat'&&endedWithoutChecks(d)?'':diagnosis(d)}<div class="tabbar" data-key="detail-tabs" role="tablist" aria-label="任务视图">${tabs.map(([id,i,label,count])=>`<button class="tab ${state.tab===id?'active':''}" data-tab="${id}" role="tab" aria-selected="${state.tab===id}" id="tab-${id}" aria-controls="detail-panel" tabindex="${state.tab===id?0:-1}">${icon(i)}${label}<span class="tab-count">${count||0}</span></button>`).join('')}</div><div id="detail-panel" role="tabpanel" aria-labelledby="tab-${state.tab}">${state.tab==='chat'?conversationPanel(d):state.tab==='overview'?overview(d):state.tab==='logs'?logs(d):state.tab==='artifacts'?artifacts(d):acceptance(d)+reviewForm(d)}</div></section>`;}
function render({reset=false}={}){const html=state.route==='home'?home():state.route==='runs'?runsPage():state.route==='chats'?chatsPage():state.route==='roles'?rolePage():detailContent();if(reset)main.innerHTML=html;else patchHTML(main,html);updateSidebar();}
function parseRoute() {
 const [hash, query=''] = location.hash.slice(1).split('?');
 if (hash.startsWith('run/')) {
  try {
   const [encoded, tab] = hash.slice(4).split('/');
   return {route:'detail', runId:decodeURIComponent(encoded), tab:['chat','artifacts'].includes(tab)?tab:'overview',
    file:tab==='artifacts' ? new URLSearchParams(query).get('path') : null};
  } catch { return {route:'runs',runId:null}; }
 }
 if (hash==='roles' || hash.startsWith('roles/')) {
  try {return {route:'roles',roleId:decodeURIComponent(hash.slice(6)) || null,runId:null};}
  catch {return {route:'roles',roleId:null,runId:null};}
 }
 return {route:['runs','chats'].includes(hash)?hash:'home',runId:null};
}
async function route(){const next=parseRoute();const changed=next.runId!==state.runId;
 if (state.route!==next.route || roleView.id!==(next.roleId || null)) {
  Object.assign(roleView,{id:next.roleId||null,detail:null,error:'',filter:'current',kind:'memory'});
  if ($('#role-dialog')?.open) $('#role-dialog').close();
 }
 state.route=next.route;state.runId=next.runId;if(changed){state.detail=null;state.detailSignature='';state.tab=next.tab||'overview';state.selectedFile=null;state.fileData=null;state.expanded.clear();state.logOpen.clear();}else if(next.tab){state.tab=next.tab;}$('#sidebar').classList.remove('open');render({reset:true});window.scrollTo({top:0,behavior:'instant'});if(state.route==='detail') {
  if(next.file) await selectFile(next.file);
  await refreshDetail();
  if(state.runId===next.runId && state.tab==='artifacts' && !state.selectedFile && state.detail?.artifacts?.length)
   await selectFile(state.detail.artifacts[0].path);
 }if(state.route==='chats')await refreshChats();if(state.route==='roles')await loadRole();}
async function refreshDetail(){const id=state.runId;if(!id)return;try{const detail=await api(`/api/runs/${encodeURIComponent(id)}`);if(state.runId!==id)return;const signature=JSON.stringify(detail);state.detail=detail;if(signature!==state.detailSignature){state.detailSignature=signature;render();if(state.tab==='artifacts'&&!state.selectedFile&&detail.artifacts?.length)selectFile(detail.artifacts[0].path);}setOnline(true);}catch(error){if(state.runId===id&&!state.detail)main.innerHTML=`<div class="page error-panel"><h2>暂时无法读取任务</h2><p>${e(error.message)}</p><a href="#runs" class="secondary">返回任务列表</a></div>`;if(!error.code)setOnline(false);}}
async function refresh(){if(state.polling)return;state.polling=true;try{if(!state.config)state.config=await api('/api/bootstrap');const data=await api('/api/runs');state.runs=data.runs||[];setOnline(true);if(!state.initialized){state.initialized=true;await route();}updateSidebar();if(state.route==='home'&&$('#home-runs')){patchHTML($('#home-runs'),state.runs.length?`<div class="run-list">${state.runs.slice(0,5).map(runRow).join('')}</div>`:empty('工作室已经准备好了','运行上方演示，你的第一个任务会出现在这里。'));const count=$('.section-title .count-badge');setText(count,state.runs.length);}if(state.route==='runs'&&$('#all-runs'))patchHTML($('#all-runs'),runList());if(state.route==='detail')await refreshDetail();if(state.route==='chats')await refreshChats();}catch{setOnline(false);}finally{state.polling=false;}}
function formTitle(mode){return mode==='group'?'新建群聊':mode==='single'?'新建私聊':'开始一个新任务';}
function openForm(mode=state.config?.provider_ready?'single':'team-demo',objective=''){ $('#dialog-title').textContent=formTitle(mode); $('#dialog-form').innerHTML=formHTML('dialog',mode,objective);$('#task-dialog').showModal();setTimeout(()=>$('#dialog-objective').focus(),0);}
function swapMode(form,mode,objective=''){const savedWorkspace=form.elements.workspace_path?.value||'';const savedLimits=runLimitFields.map(field=>[field.name,form.querySelector(`[name="${field.name}"]`)?.value]);const prefix=form.dataset.prefix;if(prefix==='dialog')$('#dialog-title').textContent=formTitle(mode);const advanced=!form.querySelector('.advanced').hidden;const custom=objective || (!form.elements.objective.readOnly?form.elements.objective.value:'');form.outerHTML=formHTML(prefix,mode,custom);const replacement=document.querySelector(`form[data-prefix="${prefix}"]`);if(replacement.elements.workspace_path)replacement.elements.workspace_path.value=savedWorkspace;for(const [name,value] of savedLimits){if(value!==undefined)replacement.querySelector(`[name="${name}"]`).value=value;}if(advanced){replacement.querySelector('.advanced').hidden=false;replacement.querySelector('[data-action="advanced"]').setAttribute('aria-expanded','true');}}
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
  if (mode === 'group' && members.length < 2) return showError('请选择至少两位群成员。');
  if (!recipient || !members.includes(recipient)) return showError('请选择本轮回复的群成员或私聊角色。');
  payload.conversation = {kind:mode==='group'?'group':'dm',members,recipient,intent};
  if (mode==='group' && intent==='edit' && fields.get('dispatch')==='team') payload.mode='team';
 }
 if ((chat||mode==='team') && fields.get('intent')!=='question') {
  payload.acceptance_files=parseFiles(fields.get('acceptance_files'));
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
async function selectFile(path){const runId=state.runId;state.selectedFile=path;state.fileData=null;render();if(databaseFile(path)){state.fileData={path,binary:true};render();return;}try{const data=await api(`/api/runs/${encodeURIComponent(runId)}/artifact?path=${encodeURIComponent(path)}`);if(state.runId!==runId||state.selectedFile!==path)return;state.fileData=data;}catch(error){if(state.runId!==runId||state.selectedFile!==path)return;state.fileData={error:error.message};}if(state.tab==='artifacts')render();}
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
 if(form?.matches('.reply-form')){rememberReply(form);if(event.target.name==='intent'||event.target.name==='dispatch')render();return;}if(event.target.name==='member')updateMemberPicker(form);if(event.target.name==='intent')syncIntent(form);if(event.target.name==='mode')swapMode(event.target.closest('form'),event.target.value);if(event.target.id==='run-filter'){state.filter=event.target.value;patchHTML($('#all-runs'),runList());}if(event.target.id==='log-filter'){state.logFilter=event.target.value;render();}});
document.addEventListener('input',event=>{const reply=event.target.closest('.reply-form');if(reply)rememberReply(reply);if(event.target.id==='run-search'){state.search=event.target.value;patchHTML($('#all-runs'),runList());}});
document.addEventListener('toggle',event=>{if(event.target.matches?.('[data-log]')){const key=event.target.dataset.log;if(event.target.open)state.logOpen.add(key);else state.logOpen.delete(key);}},true);
document.addEventListener('click',event=>{
 const roleButton=event.target.closest('[data-role-action]');
 if(roleButton){runRoleAction(roleButton);return;}
 const template=event.target.closest('[data-template]');if(template){const value=template.dataset.template,form=$('form[data-prefix="home"]');swapMode(form,value==='maintenance'?'single':value,value==='maintenance'?'生成一份电脑维护诊断计划 maintenance.md，包含信息收集、排查步骤和验证方法。':'');if(value==='maintenance'){const f=$('form[data-prefix="home"]');f.querySelector('[name="acceptance_files"]').value='maintenance.md';}$('#home-objective')?.focus();return;}
 const task=event.target.closest('[data-task]');if(task){const key=task.dataset.task;state.expanded.has(key)?state.expanded.delete(key):state.expanded.add(key);render();return;}
 const tab=event.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;render();if(state.tab==='artifacts'&&!state.selectedFile&&state.detail?.artifacts?.length)selectFile(state.detail.artifacts[0].path);return;}
 const file=event.target.closest('[data-file]');if(file){selectFile(file.dataset.file);return;}
 const action=event.target.closest('[data-action]')?.dataset.action;
 if(action==='pick-workspace')pickWorkspace(event.target.closest('button'));
 if(action==='new')openForm();
 if(action==='new-dm')openForm('single');
 if(action==='new-group')openForm('group');
 if(action==='close-dialog')$('#task-dialog').close();
 if(action==='close-info')$('#info-dialog').close();
 if(action==='connection')showConnection();
 if(action==='menu'){$('#sidebar').classList.toggle('open');mobileNav();}
 if(action==='refresh' && state.route==='roles')loadRole();
 if(action==='refresh' && state.route!=='roles')refresh().then(()=>toast(state.online?'任务状态已更新。':'服务未连接，请检查启动终端。'));
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
async function boot(){hydrateIcons();try{state.config=await api('/api/bootstrap');const data=await api('/api/runs');state.runs=data.runs||[];setOnline(true);state.initialized=true;await route();}catch(error){setOnline(false);main.innerHTML=`<div class="page error-panel"><h2>工作室暂时无法连接</h2><p>${e(error.message)}</p><button class="secondary" data-action="refresh">重新连接</button></div>`;}setInterval(async()=>{if(document.hidden)return;const missing=!state.config;await refresh();if(missing&&state.config)await route();},1800);}
boot();
