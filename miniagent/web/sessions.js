"use strict";

function sessionControls(group) {
  const controls = $("#session-controls");
  controls.hidden = group.kind !== "group";
  if (controls.hidden) return;
  const select = $("#room-session");
  const rooms = state.groups.filter(r => r.team_id === group.team_id && (!r.archived || r.id === group.id));
  if (!rooms.some(r => r.id === group.id)) rooms.push(group);
  rooms.sort((a,b) => (b.id === group.team_id) - (a.id === group.team_id) || a.created_at-b.created_at);
  const signature = JSON.stringify(rooms.map(r => [r.id,r.session_name]));
  if (select.dataset.rooms !== signature) {
    select.replaceChildren(...rooms.map(r => el("option",{value:r.id},r.session_name)));
    select.dataset.rooms = signature;
  }
  select.value = group.id;
  $("#new-session").disabled = group.archived;
}

function newSessionSheet() {
  const group = state.group;
  if (!group || group.id !== state.current) return;
  const name = editInput("",{maxlength:120,required:true,placeholder:"例如：官网改版"});
  const error = el("p",{class:"error",role:"alert",hidden:true});
  const create = button("创建会话",async () => {
    if (!name.reportValidity() || create.disabled) return;
    create.disabled = true;
    try {
      const room = await api("/groups/"+group.team_id+"/sessions",{method:"POST",body:{name:name.value.trim()}});
      $("#sheet").close();
      await openGroup(room.id);
    } catch (e) { error.textContent=e.message; error.hidden=false; }
    finally { create.disabled=false; }
  });
  sheet("新建会话",el("p",{class:"muted"},"沿用「"+group.team_name+"」的成员和负责人，使用独立的聊天与工作区。"),
    field("会话名称",name),create,error);
  name.onkeydown=e=>{if(e.key==="Enter" && !e.isComposing){e.preventDefault();create.click();}};
  name.focus();
}

const composerDrafts = new Map();
let composerRoom = null;
function switchComposerRoom(id) {
  if (id === composerRoom) return;
  const input = $("#input"), uploads = $("#uploads");
  if (composerRoom) {
    const draft = {text:input.value,files:Array.from(uploads.files),options:state.runOptions,
      thinking:$("#thinking").value,effort:$("#effort").value,queue:$("#queue").checked};
    if (draft.text || draft.files.length || draft.thinking || draft.effort || draft.queue || Object.keys(draft.options || {}).length)
      composerDrafts.set(composerRoom,draft);
    else composerDrafts.delete(composerRoom);
  }
  composerRoom=id;
  const next=composerDrafts.get(id);
  input.value=next?.text || ""; uploads.value="";
  if (next?.files.length) {
    const transfer=new DataTransfer();
    for (const file of next.files) transfer.items.add(file);
    uploads.files=transfer.files;
  }
  state.runOptions=next?.options || {};
  $("#thinking").value=next?.thinking || ""; $("#effort").value=next?.effort || ""; $("#queue").checked=Boolean(next?.queue);
  if (typeof syncEffort === "function") syncEffort();
  $("#composer-error").hidden=true;
  hideMentions(); renderUploads(); autoGrow();
}

let mentionIndex = 0;
function hideMentions() {
  const list=$("#mention-list"), input=$("#input");
  list.hidden=true; list.replaceChildren();
  input.setAttribute("aria-expanded","false");input.removeAttribute("aria-activedescendant");
}
function mentionToken(input) {
  if (input.selectionStart !== input.selectionEnd) return null;
  const prefix=input.value.slice(0,input.selectionStart);
  // Also support CJK text immediately before @ while leaving email addresses alone.
  const match=/(?:^|[^A-Za-z0-9_@])[@＠]([^\s@＠,:，：]*)$/u.exec(prefix);
  if (!match) return null;
  const end=input.selectionStart;
  return {start:end-match[1].length-1,end,query:match[1].toLowerCase()};
}
function pickMention(id) {
  const input=$("#input"), token=mentionToken(input);
  if (!token || !state.group?.members.some(m=>m.id===id)) {hideMentions();return;}
  input.setRangeText("@"+id+" ",token.start,token.end,"end");
  hideMentions(); input.focus(); autoGrow(); refreshMailComposer();
}
function highlightMention() {
  const list=$("#mention-list");
  for (const [i,option] of Array.from(list.children).entries())
    option.setAttribute("aria-selected",String(i===mentionIndex));
  const active=list.children[mentionIndex];
  if (active) {
    $("#input").setAttribute("aria-activedescendant",active.id);
    active.scrollIntoView({block:"nearest"});
  }
}
function showMentions() {
  const input=$("#input"), group=state.group, token=mentionToken(input);
  if (!token || group?.kind!=="group" || group.id!==state.current || document.activeElement!==input) {
    hideMentions();return;
  }
  const members=group.members.filter(m=>[m.id,m.name].some(s=>s.toLowerCase().includes(token.query)));
  if (!members.length) {hideMentions();return;}
  const list=$("#mention-list");
  list.replaceChildren(...members.map((m,i)=>el("button",{type:"button",role:"option",id:"mention-"+i,tabindex:-1,
    "data-member":m.id,onmousedown:e=>e.preventDefault(),onclick:()=>pickMention(m.id)},
    el("strong",{},m.name),el("small",{},"@"+m.id+(m.id===group.lead?" · 负责人":"")))));
  list.hidden=false;mentionIndex=0;input.setAttribute("aria-expanded","true");highlightMention();
}
function mentionKey(event) {
  const list=$("#mention-list");
  if (list.hidden || event.isComposing || event.keyCode===229) return false;
  if (event.key==="Escape") hideMentions();
  else if (event.key==="ArrowDown" || event.key==="ArrowUp") {
    mentionIndex=(mentionIndex+(event.key==="ArrowDown"?1:-1)+list.children.length)%list.children.length;
    highlightMention();
  } else if (event.key==="Tab" || (event.key==="Enter" && !event.shiftKey)) {
    pickMention(list.children[mentionIndex].dataset.member);
  } else return false;
  event.preventDefault();return true;
}

function setupSessions() {
  $("#room-session").onchange=e=>openGroup(e.target.value).catch(showError);
  $("#new-session").onclick=newSessionSheet;
  const input=$("#input");
  input.addEventListener("input",e=>{if(!e.isComposing) showMentions();});
  input.addEventListener("compositionend",showMentions);
  input.addEventListener("click",showMentions);
  input.addEventListener("blur",hideMentions);
  input.addEventListener("keyup",e=>{if (["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) showMentions();});
}
