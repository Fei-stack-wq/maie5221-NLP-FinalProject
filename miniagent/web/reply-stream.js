'use strict';

// One subscription per visible run. Snapshots replace local drafts on every
// connection; committed answers and member handoffs belong to the room journal.
const studioReplyStreams = (() => {
 const channels = new Map();
 const paintedReplies = new WeakMap();

 function message(row,turn) {
  const user = row.kind==='user_steer';
  const receipt = user && (turn.steering || []).find(item=>item.request_id===row.request_id);
  const seen = receipt?.seen_by;
  const name = user ? '你' : turn.member_names?.[row.role] || roleLabel(row.role);
  const caption = user ? (seen ? '已送达 '+(turn.member_names?.[seen] || roleLabel(seen)) :
   runActive(turn) ? '等待角色接收' : '本轮未送达') : '进度';
  const text = receipt?.text ?? row.text;
  const files = user ? studioAttachments.history({...turn,message_attachments:row.message_attachments}) : '';
  return `<div class="chat-update ${user?'chat-update-user':''}" data-kind="${e(row.kind)}" data-key="update-${e(row.reply_id || row.request_id)}">
   <div class="chat-update-byline">${e(name)} · ${e(caption)}</div>${text?`<div class="prose">${markdown(text,turn,row)}</div>`:''}${files}</div>`;
 }

 function timeline(turn, withHistory=false) {
  const start=turn.events_start || 0, indices=turn.event_indices;
  const rows=withHistory && start ? [historyButton(turn.run_id,start,'查看更早消息')] : [];
  for (const [offset,row] of (turn.events || []).entries()) {
   const index=indices?.[offset] ?? start+offset;
   if (withHistory && offset && indices && index>indices[offset-1]+1)
    rows.push(historyButton(turn.run_id,index,'查看此处更早的消息'));
   if (row.kind!=='assistant_progress' && row.kind!=='user_steer') continue;
   rows.push(message(row,turn));
   if (withHistory && row.text_truncated) rows.push(historyButton(turn.run_id,index+1,'读取完整原文'));
  }
  return rows.join('');
 }

 function slot(turn, showSpeaker=true) {
  const id = turn.run_id || turn.id;
  if (!id || !runActive(turn) || turn.answer) return '';
  const archived = turn.archived_tasks || (turn.events || []).filter(row=>row.kind==='member_report' && row.task_id).map(row=>row.task_id);
  const committed = (turn.events || []).filter(row=>row.kind==='assistant_progress').map(row=>row.reply_id);
  return `<div class="reply-stream" data-key="reply-stream-${e(id)}" data-live-run="${e(id)}"
   data-archived="${e(JSON.stringify(archived))}" data-committed="${e(JSON.stringify(committed))}" data-speakers="${e(JSON.stringify(turn.member_names || {}))}"
   data-show-speaker="${showSpeaker}" aria-label="正在生成的回复"></div>`;
 }

 function valid(reply) {
  return reply && ['id','role','task_id'].every(key=>typeof reply[key]==='string' && reply[key])
   && typeof reply.text==='string';
 }

 function paint(channel) {
  const archivedOnAll = new Set(channel.replies.keys());
  for (const slot of channel.slots) {
   const feed = slot.closest('.assistant-feed, .chat-history');
   const innerFeed = feed?.classList.contains('assistant-feed');
   const bottom = feed?.querySelector('[data-chat-end]');
   const before = innerFeed ? feed.scrollHeight : bottom?.getBoundingClientRect().bottom;
   const pinned = innerFeed ? before-feed.scrollTop-feed.clientHeight<64
    : before>=0 && before<=innerHeight+8;
   const archived = new Set(JSON.parse(slot.dataset.archived));
   const committed = new Set(JSON.parse(slot.dataset.committed));
   const names = JSON.parse(slot.dataset.speakers);
   const previous = new Map([...slot.children].map(el=>[el.dataset.replyId,el]));
   let after = null;
   for (const reply of channel.replies.values()) {
    if (!reply.text || archived.has(reply.task_id) || committed.has(reply.id)) continue;
    archivedOnAll.delete(reply.id);
    let row = previous.get(reply.id);
    if (!row) {
     row = document.createElement('div');
     row.className = 'live-reply';
     row.dataset.replyId = reply.id;
     const header = document.createElement('div');
     header.className = 'live-reply-byline';
     row.append(header,document.createElement('div'));
     row.lastChild.className = 'live-reply-text';
     row.lastChild.append(document.createTextNode(''));
    }
    previous.delete(reply.id);
    const next = after ? after.nextElementSibling : slot.firstElementChild;
    if (row!==next) slot.insertBefore(row,next);
    after = row;
    const speaker = Object.hasOwn(names,reply.role) ? names[reply.role] : roleLabel(reply.role);
    setText(row.firstChild,slot.dataset.showSpeaker==='true' ? speaker+' · 正在回复' : '正在回复');
    const node = row.lastChild.firstChild;
    // Snapshot entries are replaced. A delta extends the same entry, so long
    // replies need only copy the newly visible suffix into the text node.
    if (paintedReplies.get(node)!==reply) {
     node.data=reply.text;
     paintedReplies.set(node,reply);
    } else if (node.length<reply.text.length) node.appendData(reply.text.slice(node.length));
   }
   for (const obsolete of previous.values()) obsolete.remove();
   if (pinned && innerFeed && feed.scrollHeight>before) feed.scrollTop=feed.scrollHeight;
   else if (pinned && !innerFeed && bottom.getBoundingClientRect().bottom>innerHeight+8)
    window.scrollBy(0,bottom.getBoundingClientRect().bottom-innerHeight);
  }
  if (channel.slots.length) {
   for (const id of archivedOnAll) channel.replies.delete(id);
  }
  roomPanel.scheduleScrollCheck();
 }

 function connect(id) {
  const source = new EventSource('/api/runs/'+encodeURIComponent(id)+'/reply-stream');
  const channel = {source,replies:new Map(),slots:[],frame:0,ready:false};
  channels.set(id,channel);
  function schedule() {
   if (!channel.frame) channel.frame=requestAnimationFrame(()=>{
    channel.frame=0;
    if (channels.get(id)===channel) paint(channel);
   });
  }
  function listen(kind,accept) {
   source.addEventListener(kind,event=>{
    if (channels.get(id)!==channel || !channel.source) return;
    let data;
    try { data=JSON.parse(event.data); } catch { return; }
    if (accept(data)) schedule();
   });
  }
  listen('snapshot',data=>{
   if (!Array.isArray(data?.replies)) return false;
   channel.replies=new Map(data.replies.filter(valid).map(reply=>[reply.id,reply]));
   channel.ready=true;
   return true;
  });
  listen('delta',data=>{
   if (!channel.ready || !valid(data)) return false;
   const reply=channel.replies.get(data.id);
   if (!reply) channel.replies.set(data.id,data);
   else if (reply.role===data.role && reply.task_id===data.task_id && reply.step===data.step) reply.text+=data.text;
   else return false;
   return true;
  });
  listen('remove',data=>channel.ready && channel.replies.delete(data?.id));
  function refreshCommitted() {
   if (state.route==='assistant') return studioAssistant.refresh(true);
   if (state.route==='detail') return refreshDetail();
  }
  listen('room',()=>{
   // The view may already be fetching, or temporarily offline. Retire the
   // preview only when paint sees the committed row in every visible slot.
   Promise.resolve(refreshCommitted()).then(schedule);
   return false;
  });
  source.onerror=()=>{ channel.ready=false; }; // EventSource reconnects and receives a fresh snapshot.
  source.addEventListener('done',()=>{
   if (channels.get(id)!==channel || !channel.source) return;
   source.close();
   channel.source=null;
   Promise.resolve(refreshCommitted()).then(()=>{
    channel.replies.clear();
    if (channels.get(id)===channel) schedule();
   });
  });
  return channel;
 }

 function close(id,channel) {
  channel.source?.close();
  if (channel.frame) cancelAnimationFrame(channel.frame);
  channels.delete(id);
 }

 function sync() {
  if (typeof EventSource==='undefined') return;
  const slots = document.hidden ? [] : [...document.querySelectorAll('[data-live-run]')];
  const wanted = new Set(slots.map(slot=>slot.dataset.liveRun));
  for (const [id,channel] of channels) {
   if (wanted.has(id)) channel.slots=[];
   else close(id,channel);
  }
  for (const slot of slots) {
   const id=slot.dataset.liveRun;
   (channels.get(id) || connect(id)).slots.push(slot);
  }
  for (const channel of channels.values()) paint(channel);
 }
 window.addEventListener('pagehide',()=>{ for (const [id,channel] of channels) close(id,channel); });
 window.addEventListener('pageshow',sync);
 document.addEventListener('visibilitychange',sync);
 return {slot,sync,message,timeline};
})();
