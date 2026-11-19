'use strict';

const chatDirectory = (() => {
 let recent = null;
 let channels = [];
 let filter = 'all';
 let query = '';
 let loadError = '';
 let requestVersion = 0;
 let lastLoad = 0;
 let pending = null;

 const find = id => recent?.find(room => room.id === id);
 const chatURL = room => room.superagent ? '#assistant' : latestChatLink(room.head_run_id);
 const names = room => (room.member_names || room.members).join('、');
 const roomType = room => room.superagent ? '统一对话' : room.kind === 'group' ? '群聊' : '私聊';
 const title = room => room.superagent ? 'Superagent' : room.title || names(room);
 const filters = [['all','全部'],['dm','私聊'],['group','群聊']];

 function sidebar() {
  const activeRoom = state.route === 'detail' && state.tab === 'chat' && state.detail?.conversation?.id;
  const html = channels.map(room =>
   `<a class="channel-link" href="${e(chatURL(room))}" data-key="channel-${e(room.id)}"
       ${activeRoom === room.id ? 'aria-current="page"' : ''} title="${e(title(room))}">
      ${icon('hash')}<span>${e(title(room))}</span></a>`).join('');
  const empty = recent === null && !loadError ? '<p class="side-empty">正在读取群聊…</p>' :
   `<a class="channel-discover" href="#chats?kind=group">${icon('users')}从已有群聊中固定</a>`;
  patchHTML($('#channel-nav'), html || empty);
 }

 function managementButton(room, compact=false) {
  if (room?.kind !== 'group') return '';
  const label = room.pinned ? '管理固定群聊' : '固定到侧栏';
  return `<button type="button" class="${compact ? 'row-pin' : 'secondary'}" data-channel-action="manage"
   data-room="${e(room.id)}" title="${label}" aria-label="${label}">${icon(room.pinned ? 'pinFilled' : 'pin')}${compact ? '' : label}</button>`;
 }

 function list() {
  if (recent === null) return loadError ? `<p class="error-panel">${e(loadError)}</p>` : '<p class="muted-note">正在读取对话…</p>';
  const needle = query.trim().toLocaleLowerCase();
  const rooms = recent.filter(room => (filter === 'all' || (room.kind === filter && !room.superagent)) &&
   (!needle || [title(room), names(room), room.last_message || ''].some(value => value.toLocaleLowerCase().includes(needle))));
  if (!rooms.length) return empty(recent.length ? '没有匹配的对话' : '还没有对话',
   recent.length ? '可以切换类型或调整搜索词。' : '从新建私聊或新建群聊开始。');
  return '<div class="directory-chat-list">' + rooms.map(room => {
   const name = title(room);
   const snippet = (room.last_message || '').trim();
   return `<article class="directory-chat" data-key="chat-${e(room.id)}">
    <a class="directory-chat-link" href="${e(chatURL(room))}">
     <span class="chat-symbol ${room.superagent ? 'assistant' : e(room.kind)}">${icon(room.superagent ? 'terminal' : room.kind === 'group' ? 'hash' : 'user')}</span>
     <div class="chat-row-text"><strong title="${e(name)}">${e(name)}</strong>
      ${snippet && !name.startsWith(snippet) ? `<span class="last-message">${e(snippet)}</span>` : ''}
      <small>${e(roomType(room))}${!room.superagent ? ' · ' + e(names(room)) : ''} · ${room.turn_count ? e(room.turn_count) + ' 轮' : '等待第一条消息'}</small></div>
     <time datetime="${e(room.updated_at)}">${e(dateText(room.updated_at))}</time></a>${managementButton(room,true)}</article>`;
  }).join('') + '</div>';
 }

 function page() {
  return `<section class="page chats-directory"><header class="page-top chat-page-top"><h1>最近对话</h1>
    <div class="chat-actions"><button class="secondary" data-action="new-group">${icon('users')}新建群聊</button>
     <button class="primary" data-action="new-dm">${icon('plus')}新建私聊</button></div></header>
    <div class="directory-tools"><nav class="chat-filters" aria-label="对话类型">${filters.map(([id,label]) =>
     `<a href="${id === 'all' ? '#chats' : '#chats?kind='+id}" ${filter === id ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav>
     <label class="chat-search">${icon('search')}<input type="search" aria-label="搜索对话或成员" placeholder="搜索对话或成员" value="${e(query)}"></label></div>
    <div id="chat-list">${list()}</div></section>`;
 }

 function refresh(force=false) {
  if (!force && pending) return pending;
  if (!force && recent !== null && Date.now() - lastLoad < 5500) return Promise.resolve();
  const version = ++requestVersion;
  pending = (async () => {
   try {
    const result = await api('/api/directory/chats');
    if (version !== requestVersion) return;
    recent = result.recent;
    channels = result.channels;
    lastLoad = Date.now();
    loadError = '';
   } catch (error) {
    if (version !== requestVersion) return;
    loadError = error.message;
   } finally {
    if (version === requestVersion) {
     pending = null;
     sidebar();
     if (state.route === 'chats') patchHTML($('#chat-list'),list());
     if (state.route === 'detail') patchHTML($('#thread-pin'),managementButton(find(state.detail?.conversation?.id)));
    }
   }
  })();
  return pending;
 }

 function setFilter(kind) {
  filter = filters.some(([id]) => id === kind) ? kind : 'all';
 }

 function openEditor(room) {
  const create = !room;
  const roles = chatRoles();
  const pick = create ? `<fieldset class="member-picker"><legend>群成员</legend><div>${roles.map((role,i) =>
   `<label><input name="member" type="checkbox" value="${e(role.id)}" ${i<2 ? 'checked' : ''}>${e(role.label)}</label>`).join('')}</div></fieldset>
   <label class="field-label">负责人<select name="recipient" required></select></label>
   <div class="channel-workspace"><label class="field-label">本地工作区（选填）<input name="workspace_path" autocomplete="off" placeholder="项目文件夹的绝对路径"></label>
    <button type="button" class="secondary" data-action="pick-workspace">${icon('folder')}选择文件夹</button></div>` :
   `<p class="channel-members">${icon('users')}${e(names(room))}</p>`;
  $('#channel-dialog-title').textContent = create ? '新建固定群聊' : room.pinned ? '管理固定群聊' : '固定群聊';
  $('#channel-editor').innerHTML = `<form class="channel-form" data-room="${e(room?.id || '')}">
   <label class="field-label">群聊名称<input name="name" maxlength="100" required placeholder="例如：电脑维护组"
     value="${create ? '' : e(title(room).slice(0,100))}"></label>
   ${pick}<p class="form-error" role="alert" hidden></p>
   <div class="channel-form-actions">${room?.pinned ? '<button type="button" class="text-button" data-channel-action="unpin">取消固定</button>' : ''}
    <button type="submit" class="primary">${create ? '创建群聊' : room.pinned ? '保存群名' : '固定到侧栏'}</button></div></form>`;
  const form = $('#channel-editor form');
  if (create) updateChannelLead(form);
  $('#channel-dialog').showModal();
  form.elements.name.focus();
  if (!create) form.elements.name.select();
 }

 function updateChannelLead(form) {
  const ids = new FormData(form).getAll('member');
  const select = form.elements.recipient;
  const before = select.value;
  select.innerHTML = chatRoles().filter(role => ids.includes(role.id)).map(role =>
   `<option value="${e(role.id)}" ${role.id === before ? 'selected' : ''}>${e(role.label)}</option>`).join('');
 }

 async function save(form, unpin=false) {
  if (form.dataset.saving || (!unpin && !form.reportValidity())) return;
  const errorBox = form.querySelector('.form-error');
  errorBox.hidden = true;
  const data = new FormData(form);
  const room = form.dataset.room;
  const name = data.get('name').trim();
  let payload = unpin ? {pinned:false} : {pinned:true,name};
  try {
   if (!room) {
    const members = data.getAll('member');
    if (members.length < 2) throw new Error('请选择至少两位群成员。');
    payload = {name,members,lead:data.get('recipient')};
    const workspace = data.get('workspace_path').trim();
    if (workspace) payload.workspace_path = workspace;
    const fingerprint = JSON.stringify(payload);
    if (form.dataset.fingerprint !== fingerprint) {
     form.dataset.fingerprint = fingerprint;
     form.dataset.requestId = crypto.randomUUID();
    }
    payload.request_id = form.dataset.requestId;
   }
   form.dataset.saving = '1';
   const buttons = form.querySelectorAll('button');
   buttons.forEach(button => button.disabled = true);
   const path = '/api/directory/channels' + (room ? '/' + encodeURIComponent(room) : '');
   const {channel} = await api(path,{method:'POST',body:JSON.stringify(payload)});
   await refresh(true);
   if (form.isConnected && $('#channel-dialog').open) $('#channel-dialog').close();
   if (!room) location.hash = chatURL(channel);
   else if (state.route === 'detail' && state.detail?.conversation?.id === room) await refreshDetail();
   toast(!room ? '群聊已创建。' : unpin ? '已从侧栏移除，仍可在最近对话找到。' : '固定群聊已更新。');
  } catch (error) {
   errorBox.textContent = error.message;
   errorBox.hidden = false;
  } finally {
   delete form.dataset.saving;
   form.querySelectorAll('button').forEach(button => button.disabled = false);
  }
 }

 document.addEventListener('input', event => {
  if (event.target.matches('.chat-search input')) {
   query = event.target.value;
   patchHTML($('#chat-list'),list());
  }
 });
 document.addEventListener('click', event => {
  const button = event.target.closest('[data-channel-action]');
  if (!button) return;
  const action = button.dataset.channelAction;
  if (action === 'dismiss') $('#channel-dialog').close();
  if (action === 'create') openEditor(null);
  if (action === 'manage') {
   const room = find(button.dataset.room);
   if (room) openEditor(room);
  }
  if (action === 'unpin') save(button.closest('form'),true);
 });
 document.addEventListener('submit',event => {
  if (!event.target.matches('.channel-form')) return;
  event.preventDefault();
  save(event.target);
 });

 return {page, sidebar, refresh, find, setFilter, updateChannelLead, managementButton};
})();
