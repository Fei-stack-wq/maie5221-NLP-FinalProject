'use strict';

const studioAssistant = (() => {
 const examples = [
  {icon:'cpu', name:'创建角色', description:'培养一位长期助手', prompt:'创建一个名叫「电脑管家」的角色，负责电脑维护、诊断记录和后续回访。'},
  {icon:'users', name:'组建群聊', description:'让不同角色一起协作', prompt:'创建一个电脑维护群聊，安排负责诊断、修复和验证的角色，并选择一位负责人。'},
  {icon:'branch', name:'分派任务', description:'从这里跟进交付', prompt:'请找一位合适的现有角色检查当前项目的开发环境，写一份维护清单，并把结果带回这里。'}
 ];
 const indexedRoles = new Set();
 let detail=null, head=null, loaded=false, sending=false, reading=false, choosing=false;
 let epoch=0, loadError='', sendError='', request=null, draft='', workspace='';
 const active = () => Boolean(detail && runActive(detail.run));
 const onPage = () => state.route === 'assistant';
 const messages = () => detail?.conversation?.turns || [];
 const field = () => document.getElementById('assistant-message');

 function entityLinks(events) {
  const entities = new Map();
  for (const event of events || []) {
   const entity = event.entity;
   if (event.kind==='studio_entity' && entity?.id && ['role','group'].includes(entity.kind)) {
    entities.set(entity.kind+'/'+entity.id,entity);
   }
  }
  return [...entities.values()].map(entity => {
   const role = entity.kind==='role';
   const href = role ? roleLink(entity.id) : latestChatLink(entity.id);
   return `<a class="studio-entity" data-key="entity-${e(entity.kind)}-${e(entity.id)}" href="${e(href)}"><span class="entity-symbol">${icon(role?'cpu':'users')}</span><span><small>${role?'已创建角色':'已创建群聊'}</small><strong>${e(entity.name || entity.id)}</strong></span><span class="entity-open">${role?'查看角色':'进入群聊'} ${icon('arrow')}</span></a>`;
  }).join('');
 }

 function dispatchCard(dispatch) {
  const group = dispatch.target_kind==='group';
  const room = dispatch.conversation_id || dispatch.child_run_id || (group && dispatch.target_id);
  const name = dispatch.target_name || (group ? dispatch.target_id : roleLabel(dispatch.target_id));
  const status = dispatch.status==='unverified' ? '已结束 · 未独立验收' : labels[dispatch.status] || '等待状态';
  const questions = (dispatch.pending_questions || []).map(question =>
   `<div data-key="dispatch-question-${e(question.request_id)}">${humanQuestions({run:{id:dispatch.child_run_id},pending_questions:[question]})}</div>`
  ).join('');
  return `<section class="dispatch-card" data-key="dispatch-${e(dispatch.dispatch_id)}" aria-label="${e(name)}的任务">
   <div class="dispatch-heading"><span class="dispatch-recipient">${icon(group?'users':'cpu')}<strong>${e(name)}</strong></span>
    <span class="dispatch-status" role="status" data-status="${e(dispatch.status)}">${e(status)}</span>
    ${room ? `<a href="${e(latestChatLink(room))}">${group?'进入群聊':'进入私聊'} ${icon('chevron')}</a>` : ''}</div>
   <p class="dispatch-objective">${e(dispatch.objective)}</p>
   ${dispatch.error ? `<p class="dispatch-error">${e(dispatch.error)}</p>` : ''}
   ${dispatch.answer ? `<div class="dispatch-answer prose">${markdown(dispatch.answer)}</div>` : ''}
   ${dispatch.answer_truncated ? '<small class="assistant-note">此处显示答复节选，可进入对话查看全文。</small>' : ''}
   ${dispatch.child_run_id ? turnFiles({run_id:dispatch.child_run_id,artifacts:dispatch.artifacts}) + questions : ''}</section>`;
 }

 function turnRow(turn) {
  const pending = runActive(turn);
  const activity = window.StudioActivity?.renderTurn(turn,detail) || '';
  const entities = entityLinks(turn.events);
  const dispatches = (turn.dispatches || []).map(dispatchCard).join('');
  const error = turn.run_id === detail?.run.id ? detail.run.error : '';
  const failed = ['failed','error','cancelled','interrupted','blocked'].includes(turn.status);
  const waiting = turn.status==='waiting_user';
  const status = waiting ? '<p class="assistant-note">等待你的答复</p>' : pending ? '<p class="assistant-pending" role="status"><i></i>正在处理…</p>' :
   failed || (!turn.answer && !entities && !dispatches) ? `<p class="assistant-note">${e(labels[turn.status] || '本轮已结束')}${error ? ' · '+e(error) : ''} · <a href="#run/${encodeURIComponent(turn.run_id)}">查看运行记录</a></p>` : '';
  return `<article class="assistant-turn" data-key="assistant-turn-${e(turn.run_id)}">
   <div class="assistant-user"><span class="prompt-sign" aria-hidden="true">›</span><div><span class="sr-only">你</span><p>${e(turn.user)}</p></div></div>
   <div class="assistant-reply"><div class="assistant-byline"><span class="small-terminal" aria-hidden="true">${icon('terminal')}</span><strong>工作室助手</strong><a href="#run/${encodeURIComponent(turn.run_id)}" title="查看本轮运行详情">本轮详情 ${icon('chevron')}</a></div>
   ${activity}
   ${turn.answer ? `<div class="assistant-answer prose">${markdown(turn.answer)}</div>` : ''}
   ${turn.answer_truncated?'<small class="assistant-note">此处显示回复节选，完整记录见本轮详情。</small>':''}
   <div class="assistant-entities">${entities}</div>${dispatches}${turnFiles(turn)}${status}</div></article>`;
 }

 function quickActions() {
  return `<div class="assistant-examples" aria-label="快捷指令">${examples.map((example,i)=>
   `<button type="button" data-assist-example="${i}"><span class="example-icon">${icon(example.icon)}</span><span><strong>${example.name}</strong><small>${example.description}</small></span><span class="example-go" aria-hidden="true">↗</span></button>`).join('')}</div>`;
 }

 function composer() {
  const ready = Boolean(state.config?.provider_ready);
  const locked = !ready || sending || !loaded || active() || Boolean(loadError);
  const path = workspace || detail?.conversation?.workspace_path;
  const folder = path ? path.split(/[\\/]/).filter(Boolean).at(-1) || path : '选择工作区';
  const runningId = active() ? detail.run.id : null;
  return `<form class="assistant-composer" id="assistant-form" data-key="assistant-form">
   <label class="sr-only" for="assistant-message">给工作室助手发消息</label>
   <div class="assistant-input-row"><span class="prompt-sign" aria-hidden="true">›</span><textarea id="assistant-message" name="message" rows="2" maxlength="8000" placeholder="描述你的目标，也可以点名角色或群聊…" required></textarea></div>
   <div class="assistant-tools"><div class="assistant-context">
   <button type="button" class="assistant-folder" data-assist-pick aria-label="选择工作区" title="${e(path || '选择本机项目文件夹')}" ${choosing||sending||active()?'disabled':''}>${icon('folder')}<span>${e(choosing?'正在选择…':folder)}</span></button>
   ${ready && state.config.model ? `<span class="assistant-model" title="当前模型"><i></i>${e(state.config.model)}</span>`:''}
   </div><div class="assistant-send-tools">${runningId?'<span class="assistant-state">运行中</span>':'<span class="assistant-shortcut">Shift + Enter 换行</span>'}
   <button class="assistant-send" type="submit" aria-label="发送消息" title="发送消息（Enter）" ${locked || !draft.trim()?'disabled':''}>${icon('arrow')}</button></div></div>
   ${!ready?'<p class="assistant-config-note">发送消息需要配置 DeepSeek。<button type="button" data-action="connection">查看连接设置</button></p>':''}
   ${sendError?`<p class="assistant-error" role="alert">${e(sendError)}</p>`:''}</form>`;
 }

 function page() {
  const turns = messages();
  const hasHistory = turns.length>0;
  const loading = !loaded && !loadError;
  return `<section class="assistant-page ${hasHistory?'has-history':'is-new'}" data-key="assistant-page" aria-label="工作室助手">
   <div class="assistant-panel" data-key="assistant-panel">
   ${hasHistory ? `<header class="assistant-heading compact-heading"><span class="assistant-signature">Superagent</span><h1>统一对话</h1></header>` :
    `<header class="assistant-heading welcome-heading"><span class="assistant-identity"><span class="terminal-tile" aria-hidden="true">${icon('terminal')}</span><span class="assistant-signature">Superagent</span></span><h1>今天想完成什么？</h1><p>工作室助手可以直接帮你，也可以把任务交给合适的角色或群聊。进展和交付都会回到这里。</p></header>`}
   ${loadError?`<div class="assistant-load-error" role="status">${icon('info')}<span>${e(loadError)}</span><button type="button" data-assist-retry>重新连接</button></div>`:''}
   ${hasHistory ? `<div class="assistant-feed" data-key="assistant-feed" aria-label="对话记录">${turns.map(turnRow).join('')}${roomPanel.endMarker()}</div>`:''}
   ${detail?.pending_questions?.length ? `<div class="assistant-questions" data-key="assistant-questions">${humanQuestions(detail)}</div>` : ''}
   <div class="assistant-compose-area" data-key="assistant-compose-area">${hasHistory?roomPanel.jumpButton():''}${composer()}</div>
   ${!hasHistory ? quickActions():''}
   ${loading?'<p class="assistant-note assistant-loading" role="status">正在读取对话…</p>':''}
   </div></section>`;
 }

 function draw(follow=false) {
  if (!onPage()) return;
  const feed = document.querySelector('.assistant-feed');
  const pinned = !feed || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 64;
  render();
  const input = field();
  if (input && input.value!==draft) input.value=draft;
  const next = document.querySelector('.assistant-feed');
  if (next && (follow || pinned)) next.scrollTop=next.scrollHeight;
 }

 async function syncCreatedRoles(turns) {
  // Role pickers outside this chat read the bootstrap snapshot. Refresh it
  // from the authoritative index once a tool has actually created a new role.
  const created = turns.flatMap(turn=>(turn.events || []).filter(event=>event.kind==='studio_entity' && event.entity?.kind==='role')
   .map(event=>event.entity.id)).filter(id=>id && !indexedRoles.has(id));
  if (!created.length || !state.config) return;
  try {
   const data=await api('/api/profiles');
   if (!Array.isArray(data.profiles) || !state.config) return;
   state.config.custom_profiles=data.profiles;
   for (const id of created) indexedRoles.add(id);
  } catch { /* The next refresh or a visit to the role library can retry. */ }
 }

 async function refresh(force=false) {
  if (reading || sending || !onPage()) return;
  const token = epoch;
  reading=true;
  try {
   const pointer=await api('/api/superagent');
   const id=pointer.head_run_id || null;
   const pendingDispatch = messages().some(turn => turn.dispatches?.some(runActive));
   const fresh = id && (force || head!==id || !detail || !loaded || active() || pendingDispatch) ? await api('/api/runs/'+encodeURIComponent(id)) : id ? detail : null;
   if (token!==epoch) return;
   const changed = !loaded || Boolean(loadError) || head!==id || fresh!==detail;
   loaded=true; head=id; detail=fresh; loadError='';
   await syncCreatedRoles(messages());
   if (changed) draw();
  } catch(error) {
   if (token===epoch) { loadError=error.message; draw(); }
  } finally { reading=false; }
 }

 async function send(form) {
  if (sending || active() || !loaded || loadError || !state.config?.provider_ready) return;
  const message=form.elements.message.value.trim();
  if (!message) return;
  const path = workspace;
  if (!request || request.message!==message || request.workspace!==path) {
   request={id:crypto.randomUUID(),message,workspace:path,parent:head};
  }
  const payload={request_id:request.id,message,parent_run_id:request.parent};
  if (request.workspace) payload.workspace_path=request.workspace;
  epoch++; sending=true; sendError=''; draw();
  try {
   const data=await api('/api/superagent/messages',{method:'POST',body:JSON.stringify(payload)});
   if (draft.trim()===message) draft='';
   request=null; head=data.run.id;
   // Keep earlier turns and display the accepted message while the first poll arrives.
   const turns=messages().filter(turn=>turn.run_id!==head);
   detail={run:data.run,conversation:{...detail?.conversation,workspace_path:workspace || detail?.conversation?.workspace_path,
    turns:[...turns,{run_id:head,user:message,status:data.run.status,events:[],artifacts:[]}]}};
   workspace=''; loaded=false;
   draw(true);
  } catch(error) {
   sendError=error.message;
   // The server confirms stale heads with a conflict. Transport failures may
   // have accepted the turn; retry the same payload and idempotency key.
   if (error.status===409) request=null;
   if (error.code==='forbidden') state.config=null;
  } finally {
   sending=false;
   await refresh();
   draw();
  }
 }

 function enter() {
  if (onPage()) {
   const input=field();
   if (input) input.value=draft;
   const feed=document.querySelector('.assistant-feed');
   if (feed) feed.scrollTop=feed.scrollHeight;
  }
 }

 document.addEventListener('submit',event=>{
  if (event.target.id==='assistant-form') { event.preventDefault(); send(event.target); }
 });
 document.addEventListener('input',event=>{
  if (event.target.id!=='assistant-message') return;
  draft=event.target.value;
  const btn=event.target.form.querySelector('.assistant-send');
  btn.disabled=!draft.trim() || !state.config?.provider_ready || !loaded || Boolean(loadError) || sending || active();
 });
 document.addEventListener('keydown',event=>{
  if (event.target.id==='assistant-message' && event.key==='Enter' && !event.shiftKey && !event.isComposing && event.keyCode!==229) {
   event.preventDefault();event.target.form.requestSubmit();
  }
 });
 document.addEventListener('click',async event=>{
  const sample=event.target.closest('[data-assist-example]');
  if (sample) {
   draft=examples[Number(sample.dataset.assistExample)].prompt;sendError='';draw();field()?.focus();return;
  }
  if (event.target.closest('[data-assist-retry]')) { await refresh();return; }
  if (event.target.closest('[data-assist-pick]') && !choosing && !sending && !active()) {
   choosing=true;draw();
   try {
    const data=await api('/api/workspaces/pick',{method:'POST',body:'{}'});
    if (typeof data.path==='string' && data.path) workspace=data.path;
   } catch(error) { sendError=error.message; }
   finally { choosing=false;draw(); }
  }
 });
 return {page,refresh,enter};
})();
