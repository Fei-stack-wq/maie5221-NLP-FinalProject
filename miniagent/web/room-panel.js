'use strict';

const roomPanel = (() => {
 let expandedRoom = null;
 let editor = null;
 let opening = 0;
 let frame = 0;
 let observedFeed = null;
 const resize = new ResizeObserver(scheduleScrollCheck);
 const dialog = () => document.getElementById('membership-dialog');
 const endpoint = id => '/api/directory/channels/' + encodeURIComponent(id) + '/members';

 function avatar(id,name) {
  const marker = Array.from((name || id || '').trim())[0] || '@';
  const tone = Array.from(id || '').reduce((n,c)=>(n+c.codePointAt(0))%3,0);
  return `<span class="roster-avatar" data-tone="${tone}" aria-hidden="true">${e(marker)}</span>`;
 }

 function rail(thread, composeAvailable) {
  const members = thread.members;
  const rows = members.map(id => {
   const name = thread.profiles?.find(profile => profile.id === id)?.name || roleLabel(id);
   return `<li data-key="room-person-${e(id)}">${avatar(id,name)}
    <div class="roster-identity"><strong>${memberLink(thread,id)}</strong><small>@${e(id)}</small></div>
    ${id === thread.lead ? '<small class="roster-lead">负责人</small>' : ''}
    ${composeAvailable ? `<button class="mention-member" type="button" data-mention-id="${e(id)}" aria-label="点名 ${e(name)}" title="点名 ${e(name)}">@</button>` : ''}</li>`;
  }).join('');
  const open = expandedRoom === thread.id;
  return `<aside class="room-rail${open?' mobile-open':''}" aria-label="当前群成员" data-key="members-${e(thread.id)}">
   <header><h3>群成员 <span>${members.length}</span></h3>
    <button type="button" class="roster-edit" data-edit-members aria-label="管理群成员" title="添加或移除成员">${icon('users')}管理</button>
    <button type="button" class="roster-toggle" data-toggle-members aria-controls="current-members" aria-expanded="${open}" aria-label="${open?'收起':'展开'}成员列表">${icon('chevron')}</button></header>
   <div id="current-members">${rows ? `<ul>${rows}</ul>` : '<p>群里还没有成员。</p>'}</div></aside>`;
 }

 function fields(snapshot, catalog) {
  const ids = new Map(snapshot.members.map(id => [id, snapshot.member_names[id] || id]));
  for (const role of catalog) if (!ids.has(role.id)) ids.set(role.id, role.label);
  return `<form class="membership-form"><label class="roster-search">${icon('search')}<input type="search" aria-label="搜索可邀请角色" placeholder="搜索角色名或 ID"></label>
   <fieldset class="roster-choices"><legend class="sr-only">选择群成员</legend>
    ${Array.from(ids, ([id,name]) => `<label data-search-text="${e((name+' '+id).toLocaleLowerCase())}"><input type="checkbox" name="room_member" value="${e(id)}" ${snapshot.members.includes(id)?'checked':''}>
     <span>${e(name)}</span><small>@${e(id)}</small></label>`).join('')}</fieldset>
   <p class="roster-no-match" hidden>没有匹配的角色。</p>
   <label class="field-label">群负责人<select name="lead"></select></label>
   <p class="muted-note">成员变更从下一条群消息生效。正在执行的分工沿用原成员。</p>
   <p class="form-error" role="alert" hidden></p>
   <footer><button type="button" class="secondary" data-dismiss-members>取消</button><button type="submit" class="primary">保存成员</button></footer></form>`;
 }

 function setLeader(form, preferred) {
  const selected = Array.from(form.querySelectorAll('[name=room_member]:checked'));
  const select = form.elements.lead;
  const lead = selected.some(input => input.value === preferred) ? preferred : selected[0]?.value;
  select.innerHTML = selected.length ? selected.map(input => {
   const name = input.closest('label').querySelector('span').textContent;
   return `<option value="${e(input.value)}"${input.value === lead?' selected':''}>${e(name)}</option>`;
  }).join('') : '<option value="">暂无成员</option>';
  select.disabled = !selected.length;
 }

 async function loadEditor(room, notice='') {
  const token = ++opening;
  const box = document.getElementById('membership-content');
  editor = null;
  box.innerHTML = '<p class="muted-note" role="status">正在读取成员…</p>';
  if (!dialog().open) dialog().showModal();
  try {
   const [snapshot, config] = await Promise.all([api(endpoint(room)), api('/api/bootstrap')]);
   if (token !== opening || !dialog().open || state.detail?.conversation?.id !== room) return;
   state.config = config;
   box.innerHTML = fields(snapshot, chatRoles());
   const form = box.querySelector('form');
   setLeader(form, snapshot.lead);
   editor = {room, revision:snapshot.revision};
   if (notice) showFormError(form, notice);
   form.querySelector('[type=search]').focus();
  } catch (error) {
   if (token === opening && dialog().open) box.innerHTML = `<p class="form-error" role="alert">${e(error.message)}</p><button type="button" class="secondary" data-edit-members>重新读取成员</button>`;
  }
 }

 function showFormError(form, message) {
  const node = form.querySelector('.form-error');
  node.textContent = message;
  node.hidden = false;
 }

 async function save(form) {
  const draft = editor;
  if (!draft || form.dataset.saving) return;
  const data = new FormData(form);
  const payload = {revision:draft.revision, members:data.getAll('room_member'), lead:data.get('lead') || null};
  form.dataset.saving = '1';
  for (const control of form.elements) control.disabled = true;
  try {
   await api(endpoint(draft.room), {method:'POST', body:JSON.stringify(payload)});
   if (editor === draft && dialog().open) dialog().close();
   await Promise.all([chatDirectory.refresh(true),refreshDetail()]);
   toast('群成员已更新。');
  } catch (error) {
   if (editor !== draft || !dialog().open) return;
   if (error.code === 'stale_membership') {
    await loadEditor(draft.room,'群成员已被更新，已载入最新名单。请核对后保存。');
    return;
   }
   delete form.dataset.saving;
   for (const control of form.elements) control.disabled = false;
   setLeader(form,payload.lead);
   showFormError(form,error.message);
  }
 }

 function mention(id) {
  const form = document.querySelector('.reply-form');
  if (!form || !state.detail?.conversation?.members.includes(id)) return;
  const input = form.elements.message;
  const token = '@'+id;
  const addressed = input.value.trimStart().match(/^(?:@[^\s,，:：]+[\s,，:：]*)+/u)?.[0] || '';
  if (!addressed.split(/[\s,，:：]+/u).includes(token)) {
   input.setRangeText(token+' ', 0, 0, 'end');
  }
  if (form.elements.dispatch) form.elements.dispatch.value='team';
  rememberReply(form);
  render();
  document.getElementById('chat-reply')?.focus();
 }

 const jumpButton = () => '<button type="button" class="jump-latest" aria-label="跳到最新消息" title="跳到最新消息（End）" data-jump-latest hidden>'+icon('down')+'<span>最新消息</span></button>';
 const endMarker = () => '<span class="chat-end" data-chat-end tabindex="-1" aria-label="最新消息末尾"></span>';
 function feed() {return document.querySelector('.assistant-feed, .chat-thread .chat-history');}
 function scheduleScrollCheck() {
  if (frame) return;
  frame = requestAnimationFrame(() => {
   frame = 0;
   const active = feed();
   if (active !== observedFeed) {
    resize.disconnect();
    observedFeed = active;
    if (active) resize.observe(active);
   }
   const button = document.querySelector('[data-jump-latest]');
   const end = active?.querySelector('[data-chat-end]');
   if (!button || !end) return;
   const bottom = active.classList.contains('assistant-feed') ? active.getBoundingClientRect().bottom : window.innerHeight;
   button.hidden = end.getBoundingClientRect().bottom <= bottom + 8;
  });
 }
 function jump() {
  const end = feed()?.querySelector('[data-chat-end]');
  if (!end) return;
  end.focus({preventScroll:true});
  end.scrollIntoView({block:'end',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
 }

 document.addEventListener('scroll',scheduleScrollCheck,{capture:true,passive:true});
 window.addEventListener('resize',scheduleScrollCheck);
 window.addEventListener('hashchange', () => { if (dialog().open) dialog().close(); });
 document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.hasAttribute('data-edit-members')) {
   const room = state.detail?.conversation?.id;
   if (room) loadEditor(room);
  } else if (button.hasAttribute('data-dismiss-members')) dialog().close();
  else if (button.hasAttribute('data-toggle-members')) {
   const room = state.detail.conversation.id;
   expandedRoom = expandedRoom === room ? null : room;
   render();
  } else if (button.dataset.mentionId) mention(button.dataset.mentionId);
  else if (button.hasAttribute('data-jump-latest')) jump();
 });
 document.addEventListener('input',event => {
  if (!event.target.matches('.roster-search input')) return;
  const form = event.target.form;
  const query = event.target.value.trim().toLocaleLowerCase();
  const choices = Array.from(form.querySelectorAll('label[data-search-text]'));
  for (const choice of choices) choice.hidden = !choice.dataset.searchText.includes(query);
  form.querySelector('.roster-no-match').hidden = choices.some(choice => !choice.hidden);
 });
 document.addEventListener('change',event => {
  if (event.target.name === 'room_member') setLeader(event.target.form,event.target.form.elements.lead.value);
 });
 document.addEventListener('submit',event => {
  if (event.target.matches('.membership-form')) {event.preventDefault(); save(event.target);}
 });
 document.addEventListener('keydown',event => {
  if (event.key!=='End' || event.defaultPrevented || event.shiftKey || event.altKey || document.querySelector('dialog[open]')) return;
  if (event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable=false])') || !feed()?.querySelector('[data-chat-end]')) return;
  event.preventDefault();
  jump();
 });
 dialog().addEventListener('close', () => {opening++; editor=null;});
 return {rail,avatar,jumpButton,endMarker,scheduleScrollCheck};
})();
