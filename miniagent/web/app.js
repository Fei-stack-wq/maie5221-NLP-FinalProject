"use strict";
// miniagent Studio — vanilla JS client. Polls /api/groups/<id> for new chat
// events (by seq) and live activity; mutations send the instance token.

const $ = (selector) => document.querySelector(selector);
const state = {
  config: null, templates: [], groups: [], current: null, group: null,
  outbox: new Map(), sendQueues: new Map(), after: 0, activityAfter: 0, live: null, timer: null, editing: null, rendered: new Set(),
};
let md = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const ICONS = {
  plus: "M12 5v14M5 12h14", spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z", chat: "M5 5h14v10H9l-4 4z",
  users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M17 3.5a4 4 0 0 1 0 7.5M22 21a7 7 0 0 0-4-6.3",
  list: "M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01", key: "M15 7a4 4 0 1 1-3.2 6.4L5 20H3v-2l1.5-1.5H6V15h1.5l1.9-1.9A4 4 0 0 1 15 7z",
  stop: "M7 7h10v10H7z", folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15.1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 4.6V4a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5.9z",
  dots: "M5 12h.01M12 12h.01M19 12h.01", down: "M12 5v14M6 13l6 6 6-6", x: "M6 6l12 12M18 6L6 18",
  clip: "M21 11.5l-8.6 8.6a5 5 0 0 1-7-7l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4", send: "M5 12h14M13 6l6 6-6 6",
  check: "M5 12l5 5L20 7", arrow: "M5 12h14M13 6l6 6-6 6", question: "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name] || ICONS.dots); svg.append(path);
  return svg;
}
function fillIcons(root = document) {
  for (const slot of root.querySelectorAll("[data-icon]")) {
    if (!slot.querySelector("svg")) slot.prepend(icon(slot.dataset.icon));
  }
}
// Seal inks: one family — cobalt, indigo, slate, celadon, gold — so a crowded room stays harmonious.
const AVATAR_COLORS = ["#22409c", "#3d4a8f", "#2f5d86", "#4e8a80", "#5a4b8a", "#a7802a", "#2d6f7c", "#43507a"];
function avatarColor(id) {
  let h = 0;
  for (const ch of String(id || "?")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function initials(name) {
  let text = String(name || "?").trim();
  // "场景一-软件开发组" → use the distinctive part after a short prefix.
  const parts = text.split(/\s*[-·—:：|｜]\s*/).filter(Boolean);
  if (parts.length > 1 && parts[0].length <= 4) text = parts[1];
  const cjk = text.match(/[\u4e00-\u9fff]/g);
  if (cjk) return cjk.slice(0, 2).join("");
  const words = text.split(/[\s_\-]+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : text.slice(0, 2)).toUpperCase();
}
function avatar(id, name, size = "") {
  // A small seal carries one character, as a real name-chop would.
  let mark = initials(name || id);
  if (size === "sm" && /^[\u4e00-\u9fff]{2}$/.test(mark)) mark = mark[0];
  const node = el("span", { class: "avatar" + (size ? " " + size : ""), title: name || id, "aria-hidden": "true" }, mark);
  node.style.setProperty("--c", avatarColor(id));
  return node;
}
function brandSeal() {
  const mark = el("span", { class: "brand-mark" });
  mark.innerHTML = $("#brand-seal").innerHTML;
  return mark;
}
function youAvatar(size = "") { return el("span", { class: "avatar you" + (size ? " " + size : ""), "aria-hidden": "true" }, "你"); }

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
    init.headers["Content-Type"] = "application/json";
  }
  if (init.method && init.method !== "GET") init.headers["X-Studio-Token"] = state.config.token;
  const response = await fetch("/api" + path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function memberName(id) {
  const member = state.group?.id === state.current && state.group.members.find((m) => m.id === id);
  return member ? `${member.name}` : id;
}

function fmtTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

const STATUS_LABEL = {
  dispatched: "已派工", completed: "完成", incomplete: "未完成", failed: "失败", cancelled: "已停止", max_steps: "步数用尽",
  stuck: "循环中止", budget: "预算用尽", error: "出错", queued: "排队中", interrupted: "已中断", running: "进行中",
};

function statusChip(status) {
  return el("span", { class: `status ${status}` }, STATUS_LABEL[status] || status);
}

function collapsible(node, text) {
  if ((text || "").length < 1400) return node;
  node.classList.add("collapsed");
  const toggle = el("button", { class: "link more", type: "button" }, "展开全文");
  toggle.addEventListener("click", () => {
    const collapsed = toggle.parentElement.classList.toggle("collapsed");
    toggle.textContent = collapsed ? "展开全文" : "收起";
  });
  node.append(toggle);
  return node;
}

function renderEvent(event) {
  const tid=event.task_id;
  let files=state.publications.get(tid);
  if (!tid && (event.kind==="room_reply" || event.kind==="user_message")) {
    files=new Map();
    for (const [taskId,publications] of state.publications) {
      for (const [path,change] of publications) {
        if (change.change==="deleted") continue;
        if (!files.has(path)) files.set(path,{versions:[]});
        files.get(path).versions.push({gid:state.current,tid:taskId,path,sha256:change.sha256,files:publications});
      }
    }
  }
  const scope={gid:state.current,tid,files};
  const markdown=text=>documentBody(text,scope);
  const who = (label, ...extra) => el("div", { class: "who" }, label, ...extra, el("span", {}, fmtTime(event.time)));
  const card = (cls, actorId, label, ...body) => el("div", { class: "msg with-avatar " + cls },
    actorId === "you" ? youAvatar() : avatar(actorId, memberName(actorId)), who(...label), ...body);
  if (event.kind !== "progress") state.worklog = null;
  switch (event.kind) {
    case "assistant_group_created":
    case "dispatch_sent":
    case "dispatch_finished":
      return dispatchNotice(event);
    case "session_created":
      return el("div",{class:"divider"},el("span",{},"会话已创建 · "+event.name));
    case "group_created":
      return el("div", { class: "divider" }, el("span", {}, `群聊已创建 · ${event.members.length} 位成员 · 负责人 ${memberName(event.lead)}`));
    case "group_updated": {
      const changes=[];
      if (event.joined?.length) changes.push(event.joined.join("、")+" 加入群聊");
      if (event.left?.length) changes.push(event.left.join("、")+" 已移出群聊");
      return el("div",{class:"divider"},el("span",{},changes.join(" · ") || "群聊设置已更新"));
    }
    case "room_reply":
      return card("", event.agent, [el("b",{},memberName(event.agent))], markdown(event.text));
    case "progress": {
      // Agents narrate as they work; fold a burst into one quiet, expandable log.
      const log = state.worklog;
      if (log && log.agent === event.agent && log.tid === tid && log.node.isConnected && log.node.nextSibling === state.feed?.root) {
        log.count++;
        log.latest.textContent = (event.text || "").replace(/\s+/g, " ");
        log.countLabel.textContent = log.count + " 条进展";
        log.list.append(el("li", {}, markdown(event.text)));
        return null;
      }
      const latest = el("span", { class: "latest" }, (event.text || "").replace(/\s+/g, " "));
      const countLabel = el("span", { class: "count" }, "1 条进展");
      const list = el("ol", {}, el("li", {}, markdown(event.text)));
      const details = el("details", {}, el("summary", {}, el("b", {}, memberName(event.agent)), latest, countLabel), list);
      const node = el("div", { class: "msg worklog with-avatar" }, avatar(event.agent, memberName(event.agent), "sm"), details);
      state.worklog = { agent: event.agent, tid, node, latest, countLabel, list, count: 1 };
      return node;
    }
    case "user_message":
      return el("div", { class: "msg user" },
        who(el("b", {}, event.during_task ? "你 · 任务进行中" : "你"),
          event.addressed ? el("span", {}, `→ @${event.addressed}`) : null),
        markdown(event.text), (event.attachments||[]).length ? el("div",{class:"chips"},...(event.attachments||[]).map(a=>el("span",{class:"chip"},icon("clip"),a.name))) : null);
    case "attachments":
      return el("div",{class:"msg user"},el("div",{class:"chips"},...(event.attachments||[]).map(a=>el("span",{class:"chip"},icon("clip"),a.name))));
    case "queued_message":
      return el("div",{class:"divider"},el("span",{},"已排队，当前任务结束后开始 · "+event.text));
    case "task_started": {
      const tools=toolWidget(tid);state.toolWidgets.set(tid,tools);
      return el("section",{class:"chat-run"},el("div",{class:"chat-run-head"},
        el("strong",{},avatar(event.agent, memberName(event.agent), "sm"), "任务开始 · "+memberName(event.agent)+" 负责"),button("查看任务树",()=>taskDetail(tid))),tools);
    }
    case "delegation": {
      const node = card("delegation", event.agent,
        [el("b", {}, memberName(event.agent)), el("span",{class:"arrow"},"→"), el("b", {}, memberName(event.assignee)), el("span", {}, "派工")],
        markdown(event.task),
        event.deliverables && event.deliverables.length
          ? el("div", { class: "chips", style: "margin-top:8px" }, event.deliverables.map((p) => el("span", { class: "chip" }, p))) : null);
      return collapsible(node, event.task);
    }
    case "report": {
      const level = event.status === "completed" ? "" : (event.status === "error" || event.status === "cancelled" ? "failed" : "incomplete");
      const node = card(`report ${level}`, event.agent,
        [el("b", {}, memberName(event.agent)), statusChip(event.status),
          el("span", {}, `${event.steps || 0} 步 · ${event.tool_calls || 0} 次工具调用`)],
        markdown(event.text), artifactCards(scope,event.changes));
      return collapsible(node, event.text);
    }
    case "question": {
      const node = card("question", event.agent, [el("b", {}, `${memberName(event.agent)} 想请你决定`)], markdown(event.question));
      const box = el("textarea", { placeholder: "直接在这里回答…", "aria-label": "回答这个问题" });
      const form = el("form", { class: "answer-box" }, box, el("div", { class: "actions" },
        el("button", { type: "submit", class: "primary" }, icon("send"), "回答"),
        button("让团队自己决定", () => answerQuestion(form, "Please decide as a team and state your assumptions."))));
      form.onsubmit = (e) => { e.preventDefault(); if (box.value.trim()) answerQuestion(form, box.value.trim()); };
      box.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } };
      if (event.qid) state.questionForms.set(event.qid, form);
      node.append(form);
      return node;
    }
    case "answer": {
      const form = event.qid && state.questionForms.get(event.qid);
      if (form) form.replaceWith(el("div", { class: "answered-note" }, "✓ 已回答"));
      return el("div", { class: "msg user" }, who(el("b", {}, `你的回答 → ${memberName(event.agent)}`)), markdown(event.text));
    }
    case "publish":
      return card("publication", event.agent, [el("b",{},memberName(event.agent)), el("span",{},"发布了文件")], artifactCards(scope,event.changes));
    case "final":
      return card("final", event.agent,
        [el("b", {}, `${memberName(event.agent)} · 交付说明`), statusChip(event.status)], markdown(event.text),
        el("div",{class:"actions"},el("button",{type:"button",class:"primary","data-accept-id":"accept-"+state.current+"-"+event.seq,onclick:async e=>{
          const button=e.currentTarget; button.disabled=true;
          const sent=await postChatMessage("我接受这次交付。",{clientId:button.dataset.acceptId});
          if (sent) button.textContent="已接受交付"; else button.disabled=false;
        }},icon("check"),"接受交付"),
          button("要求修改",()=>{ $("#input").value="请修改上一份交付：";$("#input").focus(); }),
          button("查看文件",()=>toggleFiles(true))));
    case "task_finished": {
      state.toolWidgets.get(tid)?.update(null,event.status);
      const m = event.metrics || {};
      const parts = [STATUS_LABEL[event.status] || event.status];
      if (m.duration_s !== undefined) parts.push(fmtDuration(m.duration_s));
      if (m.delegations !== undefined) parts.push(`${m.delegations} 次派工`);
      if (m.steps !== undefined) parts.push(`${m.steps} 步`);
      if (m.prompt_tokens !== undefined) parts.push(`${Math.round((m.prompt_tokens + m.completion_tokens) / 1000)}k tokens`);
      if (event.error) parts.push(event.error);
      return el("div", { class: "divider end" }, el("span", {}, "任务结束 · " + parts.join(" · ")));
    }
    case "system":
      return el("div", { class: "divider" }, el("span", {}, event.text));
    default:
      return null;
  }
}

async function answerQuestion(form, text) {
  for (const control of form.querySelectorAll("button,textarea")) control.disabled = true;
  if (!await postChatMessage(text)) {
    for (const control of form.querySelectorAll("button,textarea")) control.disabled = false;
  }
}

// ---------------------------------------------------------------- sidebar
let directoryRequest = 0;
async function loadGroups() {
  const request = ++directoryRequest;
  try {
    const rows = await api("/groups?view=conversations");
    if (request !== directoryRequest) return;
    state.groups = rows;
  } catch { return; }
  if (state.group && state.group.id===state.current) sessionControls(state.group);
  const list = $("#group-list");
  const signature = JSON.stringify([state.current, state.groups.map(g => [g.id, g.name, g.pinned, g.archived, g.running, g.waiting_for_you, Math.floor(g.last_activity || 0)])]);
  if (signature === state.groupSignature && list.childElementCount) return;  // no churn, no flicker
  state.groupSignature = signature;
  const activeTeam=state.groups.find(g=>g.id===state.current)?.team_id;
  const visible = state.groups.filter(g => !g.archived && g.kind !== "assistant" && g.id===g.team_id);
  const pinned = visible.filter(g => g.pinned);
  // Recent unpinned rooms (and anything that needs you) stay one click away.
  const recent = visible.filter(g => !g.pinned).sort((a, b) => b.last_activity - a.last_activity)
    .filter((g, i) => i < 5 || g.waiting_for_you || g.running || g.id === activeTeam);
  const row = (g) => el("li", {
    class: g.id === state.current ? "active" : "", onclick: () => openGroup(g.id), title: g.name,
  }, avatar(g.id, g.name, "sm square"), el("span", { class: "name" }, g.name),
    g.waiting_for_you ? el("span", { class: "pill waiting" }, "待你回复")
      : g.running ? el("span", { class: "pill running" }, "进行中") : null);
  function teamRows(g) {
    const rooms=state.groups.filter(r=>r.team_id===g.id && !r.archived);
    if (g.id!==activeTeam || rooms.length<2) return [row(g)];
    return [row(g),...rooms.map(r=>el("li",{class:"session-row"+(r.id===state.current?" active":""),
      onclick:()=>openGroup(r.id)},el("span",{class:"name"},r.session_name)))];
  }
  list.replaceChildren(...pinned.flatMap(teamRows));
  if (recent.length) list.append(el("li", { class: "empty-hint section" }, "最近"), ...recent.flatMap(teamRows));
  if (!pinned.length && !recent.length) list.append(el("li", { class: "empty-hint" }, "新建的群聊会出现在这里"));
}

// ---------------------------------------------------------------- room
function stopChatStream() {
  state.feed?.source?.close();
  state.feed = null;
}

function openChatStream(id) {
  stopChatStream();
  const timeline = $("#timeline");
  const root = el("div",{class:"chat-drafts"});
  timeline.append(root);
  const feed = {id, source:null, root, items:new Map(), committed:new Set(), finishedDrafts:new Set(), requested:0};
  state.feed = feed;
  for (const item of state.outbox.values()) showOutgoing(item);
  feed.onframe = (event) => {
    const frame = JSON.parse(event.data);
    const nearBottom = timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight < 160;
    if (feed.initial) {
      for (const draft of feed.items.values()) draft.removeAt = frame.after;
      feed.initial = false;
    }
    for (const patch of frame.drafts) {
      if (feed.finishedDrafts.has(patch.id)) continue;
      let draft = feed.items.get(patch.id);
      if (patch.agent) {
        if (draft) {
          // A reconnect starts with the full text of each still-active draft.
          delete draft.removeAt;
          draft.body.data="";
        } else {
          const box = el("div",{class:"chat-draft"});
          const local = state.outbox.get(patch.client_id);
          let requestNode;
          if (local) {
            local.status = ""; showOutgoing(local);
          } else if ((patch.request || patch.attachments.length) && !feed.committed.has(patch.client_id)) {
            requestNode = el("div",{class:"msg user"},el("div",{class:"who"},"你"),
              el("div",{class:"draft-text"},patch.request),
              patch.attachments.map(name=>el("span",{class:"chip"},name)));
            box.append(requestNode);
          }
          const body = document.createTextNode("");
          const phase = el("span",{class:"reply-phase",role:"status"});
          const bubble = el("div",{class:"msg with-avatar"},avatar(patch.agent,memberName(patch.agent)),el("div",{class:"who"},el("b",{"data-actor":patch.agent},memberName(patch.agent)),phase),
            el("div",{class:"draft-text"},body));
          box.append(bubble); root.append(box);
          draft = {box,bubble,body,phase,requestNode,clientId:patch.client_id}; feed.items.set(patch.id,draft);
        }
      }
      if (draft) {
        if (patch.text) draft.body.appendData(patch.text);
        draft.phase.textContent = patch.phase==="thinking" ? "正在思考…" : patch.phase==="processing" ? "正在处理…" : "";
      }
    }
    for (const key of frame.removed) {
      const draft=feed.items.get(key);
      if (draft) draft.removeAt=frame.after;
    }
    retireDrafts();
    renderWelcome();
    if (nearBottom) timeline.scrollTop=timeline.scrollHeight;
    if (frame.after > state.after && frame.after > feed.requested) {
      feed.requested=frame.after;
      poll();
    }
  };
  connectChatStream();
}

function connectChatStream() {
  const feed = state.feed;
  if (!feed || feed.source || document.hidden || !state.config?.chat_protocol) return;
  const source = feed.source = new EventSource("/api/groups/"+encodeURIComponent(feed.id)+"/chat-stream");
  source.onopen = () => { feed.initial=true; };
  source.onmessage = event => {
    if (state.feed===feed && feed.source===source) feed.onframe(event);
  };
}

function pauseChatStream() {
  if (state.feed) { state.feed.source?.close(); state.feed.source=null; }
  clearTimeout(state.timer);
}

document.addEventListener("visibilitychange",()=>{
  if (document.hidden) pauseChatStream();
  else { connectChatStream(); poll(); }
});
window.addEventListener("pagehide",pauseChatStream);
window.addEventListener("pageshow",connectChatStream);

function retireDrafts() {
  const feed=state.feed;
  if (!feed) return;
  for (const [id,draft] of feed.items) {
    if (draft.removeAt !== undefined && draft.removeAt <= state.after) {
      draft.box.remove(); feed.items.delete(id);
    }
  }
}

async function openGroup(id) {
  $("#directory").hidden = true;
  switchComposerRoom(id);
  state.group=null;
  $("#room-name").textContent=""; $("#room-members").replaceChildren(); $("#session-controls").hidden=true;
  $("#room-avatars").replaceChildren(); $("#member-panel").hidden=true; $("#workspace-choice").textContent="工作区";
  state.current = id; state.after = 0; state.activityAfter = 0; state.rendered = new Set();
  state.publications=new Map();state.toolWidgets=new Map();state.view="room";
  state.questionForms=new Map();state.worklog=null;setNav(null);
  $("#file-list").replaceChildren(); $("#preview").replaceChildren();
  state.steps = {}; state.stepsTask = null; state.lastActivity = null; state.live = null;
  $("#question-banner").hidden = $("#live").hidden = true;
  history.replaceState(null, "", "#" + id);
  $("#timeline").replaceChildren(); $("#activity").replaceChildren();
  openChatStream(id);
  $("#empty").hidden = true; $("#room").hidden = false;
  $("#files").hidden = true;
  await poll(true);
  loadGroups();
}

function renderHead() {
  const g = state.group;
  const workspace = $("#workspace-choice");
  workspace.title = g.workspace_path;
  workspace.textContent = "工作区 · "+(g.folder ? g.folder.split(/[\\/]/).filter(Boolean).pop() : "Studio 默认目录")+" ▾";
  $("#room-name").textContent = g.team_name || g.name;
  sessionControls(g);
  $("#room-mode").hidden = g.mode !== "autonomous";
  $("#room-avatars").replaceChildren(...g.members.slice(0, 6).map(m => avatar(m.id, m.name, "sm")));
  $("#member-panel").hidden = g.kind !== "group";
  const working = new Set(state.live?.active || []);
  $("#room-members").replaceChildren(...g.members.map((m) =>
    el("li", {title: m.prompt, class: working.has(m.id) ? "working" : ""}, avatar(m.id, m.name),
      el("span", {}, el("strong", {}, m.name), el("small", {}, `@${m.id}${m.id === g.lead ? " · 负责人" : ""}`)))));
  const template = state.templates.find((t) => t.name === g.name);
  const idle = !g.running;
  $("#task-buttons").replaceChildren(...(template && idle ? template.tasks.map((t, i) =>
    el("button", { type: "button", title: t.text, onclick: () => { $("#input").value = t.text; autoGrow(); $("#input").focus(); } },
      `任务 ${i + 1} · ${t.title.replace(/^.*?:\s*/, "").slice(0, 28)}`)) : []));
  $("#stop").hidden = !g.running;
  $("#queue-toggle").hidden = !g.running;
  if (!g.running) $("#queue").checked = false;
  $("#edit-group").disabled = g.kind === "assistant";
  $("#key-banner").hidden = Boolean(state.config?.has_key);
  setNav(g.kind === "assistant" ? "assistant-nav" : null);
}

function renderLive(live) {
  const box = $("#live");
  if (!live) { box.hidden = true; $("#question-banner").hidden = true; return; }
  box.hidden = false;
  if (state.stepsTask !== live.task_id) { state.steps = {}; state.stepsTask = live.task_id; }
  const steps = state.steps;
  let last = null;
  for (const a of live.activity) {
    if (a.type === "step") steps[a.agent] = `${a.step}/${a.max_steps}`;
    if (["tool_call", "say", "delta", "question"].includes(a.type)) last = a;
  }
  state.lastActivity = state.lastActivity || {};
  if (last) state.lastActivity = last;
  const active = (live.active && live.active.length ? live.active : [live.primary]);
  const current = active[active.length - 1];
  const step = steps[current] ? ` · 第 ${steps[current]} 步` : "";
  let doing = "";
  const a = state.lastActivity;
  if (a && a.agent === current) {
    if (a.type === "tool_call") doing = ` · 调用 ${a.tool}`;
    else if (a.type === "delta" || a.type === "say") doing = " · " + (a.text || "").replace(/\s+/g, " ").slice(-90);
  }
  const [done, total] = (steps[current] || "").split("/").map(Number);
  const bar = el("span", { class: "steps", title: steps[current] ? `第 ${steps[current]} 步` : "" }, el("i"));
  bar.firstChild.style.width = total ? Math.min(100, Math.round(done / total * 100)) + "%" : "8%";
  $("#live-status").replaceChildren(live.question ? el("span", { class: "dot waiting" }) : el("span", { class: "spinner" }),
    ...active.slice(-2).map(id => avatar(id, memberName(id), "sm")),
    el("span", { class: "text" }, live.question ? `${memberName(live.question.agent)} 在等你回答` :
      `${active.map(memberName).join(" › ")} 正在工作${step}${doing}`), bar);
  const banner = $("#question-banner");
  if (live.question) {
    banner.hidden = false;
    banner.replaceChildren(el("b", {}, `${memberName(live.question.agent)} 在等你回答`),
      el("span", { class: "q" }, live.question.question.replace(/\s+/g, " ").slice(0, 200)),
      button("让团队自己决定", ()=>postChatMessage("Please decide as a team and state your assumptions.")));
    $("#input").placeholder = "在这里或问题卡片里回答，也可以继续追问……";
  } else {
    banner.hidden = true;
    $("#input").placeholder = "询问进展、补充要求，或勾选「排队为新任务」交办下一项工作……";
  }
}

function renderActivity(items) {
  const box = $("#activity");
  for (const a of items) {
    let line = null;
    const who = el("span", { class: "agent" }, `[${a.agent}] `);
    if (a.type === "tool_call") line = el("div", { class: "a" }, who, `→ ${a.tool} ${JSON.stringify(a.arguments).slice(0, 400)}`);
    else if (a.type === "tool_result") line = el("div", { class: "a" }, who, el("span", { class: a.ok ? "ok" : "err" }, a.ok ? "✓ " : "✗ "), (a.preview || "").slice(0, 500));
    else if (a.type === "say") line = el("div", { class: "a say" }, who, a.text);
    else if (a.type === "compaction") line = el("div", { class: "a" }, who, `上下文压缩（${a.method}）：${a.before_tokens} → ${a.after_tokens} tokens`);
    else if (a.type === "loop_nudge" || a.type === "loop_stuck") line = el("div", { class: "a err" }, who, `循环检测：${a.reason}`);
    else if (a.type === "end") line = el("div", { class: "a" }, who, `结束：${a.status}，${a.steps} 步`);
    if (line) box.append(line);
  }
  while (box.childElementCount > 600) box.firstElementChild.remove();
  if (!box.hidden) box.scrollTop = box.scrollHeight;
}

async function poll(first = false) {
  const feed = state.feed;
  if (!feed) return;
  if (feed.fetching) {
    feed.again = true;
    return feed.fetching;
  }
  feed.fetching = refreshRoom(feed, first);
  try { await feed.fetching; } finally {
    feed.fetching = null;
    if (state.feed === feed && feed.again) { feed.again=false; poll(); }
  }
}

async function refreshRoom(feed, first) {
  clearTimeout(state.timer);
  const id = state.current;
  if (!id) return;
  try {
    const data = await api(`/groups/${id}?after=${state.after}&activity_after=${state.activityAfter}`);
    if (state.feed !== feed) return;
    state.group = data.group;
    for (const label of feed.root.querySelectorAll("[data-actor]")) label.textContent=memberName(label.dataset.actor);
    renderHead();
    const timeline = $("#timeline");
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 160;
    for (const event of data.events) {
      state.after = Math.max(state.after, event.seq);
      if (state.rendered.has(event.seq)) continue;
      state.rendered.add(event.seq);
      const outgoing = event.client_id && reconcileOutgoing(feed,event.client_id);
      const draft = feed.items.get(event.draft_id);
      if (event.draft_id) feed.finishedDrafts.add(event.draft_id);
      if (event.task_id && event.changes?.length) {
        const files=new Map(state.publications.get(event.task_id));
        for (const c of event.changes) files.set(c.path,c);
        state.publications.set(event.task_id,files);
      }
      const node = renderEvent(event);
      const bubble = outgoing || draft?.bubble;
      if (node && bubble) {
        bubble.className = node.className + " settled";
        bubble.replaceChildren(...node.childNodes);
        timeline.insertBefore(bubble,feed.root);
        if (state.worklog?.node === node) state.worklog.node = bubble;
      } else if (node) timeline.insertBefore(node,feed.root);
      else if (outgoing) outgoing.remove();
      if (draft) { draft.box.remove(); feed.items.delete(event.draft_id); }
    }
    retireDrafts();
    if (data.events.length && (first || nearBottom)) timeline.scrollTop = timeline.scrollHeight;
    if (data.live) {
      const latest=data.live.activity.filter(e=>e.type==="tool_call" || e.type==="tool_result").pop();
      state.toolWidgets.get(data.live.task_id)?.update(latest);
      renderActivity(data.live.activity);
      state.activityAfter = data.live.activity_seq;
    } else {
      state.activityAfter = 0;
    }
    const newChannel = data.events.some(e => e.kind==="assistant_group_created" && e.room_kind==="group");
    if (newChannel || (!data.live && state.live)) loadGroups();
    state.live = data.live;
    renderLive(data.live);
    renderHead();
    renderWelcome();
    if (!data.live) $("#input").placeholder = state.group.kind === "assistant"
      ? "问工作室助手任何事，或请它组建团队、派发任务……"
      : "交办工作、追问进展，或回答团队的问题。用 @成员ID 点名某位成员。";
  } catch (error) {
    console.warn(error);
  }
  if (state.feed === feed && !document.hidden) state.timer = setTimeout(poll, state.live ? 900 : 2500);
}

function showOutgoing(item) {
  const feed=state.feed;
  if (!feed || feed.id!==item.gid || !state.outbox.has(item.id)) return;
  if (!item.node?.isConnected) {
    item.feedback=el("div",{class:"outgoing-feedback",role:"status"});
    item.node=el("div",{class:item.legacy && !item.failed ? "outgoing" : "msg user outgoing"},el("div",{class:"who"},el("b",{},"你")),
      el("div",{class:"draft-text"},item.text),item.files.map(file=>el("span",{class:"chip"},file.name)),
      item.feedback);
    if (item.legacy && !item.failed) item.node.replaceChildren(item.feedback);
    feed.root.append(item.node);
  }
  item.feedback.replaceChildren(item.status);
  if (item.failed) {
    item.feedback.append(el("button",{class:"link",type:"button",onclick:()=>scheduleSend(item)},"重试"));
  }
  item.feedback.hidden=!item.status;
}

function reconcileOutgoing(feed,id) {
  feed.committed.add(id);
  const accept = $("#timeline").querySelector('[data-accept-id="'+CSS.escape(id)+'"]');
  if (accept) { accept.disabled=true; accept.textContent="已接受交付"; }
  const item=state.outbox.get(id);
  let node;
  if (item?.gid===feed.id) {
    node=item.node; state.outbox.delete(id);
  }
  for (const draft of feed.items.values()) {
    if (draft.clientId===id && draft.requestNode) {
      if (node) draft.requestNode.remove(); else node=draft.requestNode;
    }
  }
  return node;
}

function scheduleSend(item) {
  const previous=state.sendQueues.get(item.gid);
  if (item.legacy && item.failed) item.node?.remove();
  item.failed=false;
  item.status=previous ? "等待发送…" : item.files.length ? "正在上传附件…" : "正在发送…";
  showOutgoing(item);
  const delivery=(async()=>{
    if (previous) await previous;
    try {
      if (item.files.length) {
        item.status="正在上传附件…"; showOutgoing(item);
        for (let i=item.uploads.length;i<item.files.length;i++) item.uploads.push(await uploadFile(item.files[i],item.gid));
      }
      item.status=item.legacy ? "正在等待完整回复…" : "等待助手接收…"; showOutgoing(item);
      await api("/groups/"+item.gid+"/messages",{method:"POST",body:{client_id:item.id,text:item.text,
        attachments:item.uploads,options:item.options,queue:item.queue}});
      if (item.legacy && state.outbox.has(item.id)) { item.node?.remove(); state.outbox.delete(item.id); }
      item.status=""; showOutgoing(item);
      if (state.current===item.gid) await poll();
      else state.outbox.delete(item.id);  // The committed event will load with the room.
      loadGroups();
      return true;
    } catch (error) {
      if (item.legacy) item.node?.remove();
      item.failed=true; item.status=error.message || "发送失败";
      showOutgoing(item);
      return false;
    }
  })();
  state.sendQueues.set(item.gid,delivery);
  item.delivery=delivery;
  delivery.finally(()=>{
    if (state.sendQueues.get(item.gid)===delivery) state.sendQueues.delete(item.gid);
  });
  return delivery;
}

function postChatMessage(text,{files=[],options={},queue=false,clientId=crypto.randomUUID()}={}) {
  const gid=state.current;
  if (!gid || state.feed?.id!==gid) return Promise.resolve(false);
  const existing=state.outbox.get(clientId);
  if (existing && existing.gid===gid) return existing.failed ? scheduleSend(existing) : existing.delivery;
  const item={id:clientId,gid,text,files,uploads:[],options,queue,legacy:!state.config.chat_protocol};
  state.outbox.set(item.id,item);
  const delivery=scheduleSend(item);
  renderWelcome();
  const timeline=$("#timeline");
  timeline.scrollTop=timeline.scrollHeight;
  return delivery;
}

function send(event) {
  event.preventDefault();
  const input=$("#input"), files=Array.from($("#uploads").files);
  const text=input.value.trim(), gid=state.current;
  if ((!text && !files.length) || !gid || state.feed?.id!==gid) return;
  const options={...state.runOptions};
  if ($("#thinking").value) options.thinking=$("#thinking").value;
  const queue=$("#queue").checked;
  input.value=""; autoGrow();
  $("#uploads").value=""; renderUploads();
  $("#queue").checked=false; state.runOptions={};
  $("#composer-error").hidden=true;
  postChatMessage(text,{files,options,queue});
  input.focus();
}

// ---------------------------------------------------------------- files
async function toggleFiles(force) {
  const panel = $("#files");
  const show = force === undefined ? panel.hidden : force;
  panel.hidden = !show;
  if (!show) return;
  $("#assessor-zip").href = `/api/groups/${state.current}/export.zip`;
  $("#download-zip").href = `/api/groups/${state.current}/archive.zip`;
  const gid=state.current;
  const data = await api(`/groups/${gid}/files`);
  if (state.current!==gid || panel.hidden) return;
  const list = $("#file-list");
  const row = (f) => el("li", { "data-path": f.path, onclick: () => openFile(f.path) },
    el("span", { class: "name" }, icon(/\.(md|txt)$/i.test(f.path) ? "list" : "folder"), el("span", {}, f.path)),
    el("span", { class: "size" }, f.size > 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`));
  const main = data.files.filter(f => !f.path.startsWith("sources/")), sources = data.files.filter(f => f.path.startsWith("sources/"));
  list.replaceChildren(...main.map(row));
  if (sources.length) list.append(el("li", { class: "group" }, `资料来源存档 · ${sources.length}`), ...sources.map(row));
  if (!data.files.length) list.append(el("li", { class: "muted" }, "项目里还没有文件。团队交付后会出现在这里。"));
}

async function openFile(path) {
  const gid=state.current;
  path = path.replace(/^\.\//,"");
  if ($("#files").hidden) await toggleFiles(true);
  if (gid!==state.current) return;
  for (const li of document.querySelectorAll("#file-list li")) li.classList.toggle("active", li.dataset.path === path);
  if (/\.(md|markdown)$/i.test(path)) { openDocument({gid:state.current,path});return; }
  const preview = $("#preview");
  const previewUrl = `http://127.0.0.1:${state.config.preview_port}/${state.current}/${path.split("/").map(encodeURIComponent).join("/")}`;
  const head = el("div", { class: "chips", style: "margin-bottom:8px" }, el("b", {}, path), " ",
    el("a", { href: previewUrl, target: "_blank", rel: "noopener" }, "在新标签页打开"));
  const lower = path.toLowerCase();
  if (/\.(png|jpe?g|gif|webp|svg)$/.test(lower)) {
    preview.replaceChildren(head, el("img", { src: previewUrl, alt: path }));
    return;
  }
  if (/\.html?$/.test(lower)) {
    preview.replaceChildren(head, el("iframe", { src: previewUrl, title: path }));
    return;
  }
  const response = await fetch(`/api/groups/${state.current}/files/${path.split("/").map(encodeURIComponent).join("/")}`);
  const text = await response.text();
  if (gid!==state.current) return;
  preview.replaceChildren(head, el("pre", {}, text.length > 400000 ? text.slice(0, 400000) + "\n…" : text));
}

// ---------------------------------------------------------------- group dialog
function memberRow(member = { id: "", name: "", prompt: "" }) {
  const row = el("div", { class: "member-row" },
    el("input", { placeholder: "ID，如 dev", value: member.id, class: "m-id", required: true, pattern: "[A-Za-z][A-Za-z0-9_\\-]{0,47}" }),
    el("input", { placeholder: "名称，如 Software Developer", value: member.name, class: "m-name", required: true }),
    el("button", { type: "button", class: "link", onclick: () => { row.remove(); refreshLead(); } }, "移除"),
    el("textarea", { placeholder: "职责与角色提示词", class: "m-prompt", readonly:state.boundRoles?.has(member.id) }, member.prompt),
    el("input",{placeholder:"本群补充职责（可选）",class:"m-note",value:member.note||""}));
  row.querySelector(".m-id").addEventListener("input", refreshLead);
  return row;
}

function readMembers() {
  return [...document.querySelectorAll(".member-row")].map((row) => ({
    id: row.querySelector(".m-id").value.trim(),
    name: row.querySelector(".m-name").value.trim(),
    prompt: row.querySelector(".m-prompt").value.trim(), note:row.querySelector(".m-note").value.trim(),
  }));
}

function refreshLead(selected) {
  const select = $("#group-lead");
  const current = selected || select.value;
  const members = readMembers().filter((m) => m.id);
  select.replaceChildren(...members.map((m) => el("option", { value: m.id }, `${m.name || m.id} (@${m.id})`)));
  if (members.some((m) => m.id === current)) select.value = current;
}

function openDialog(group = null) {
  state.boundRoles = new Set(group?.role_ids || []);
  fillLibrary().catch(showError);
  state.editing = group;
  $("#group-dialog-title").textContent = group ? "编辑群聊" : "新建群聊";
  $("#save-group").textContent = group ? "保存" : "创建";
  $("#template").parentElement.hidden = Boolean(group);
  $("#group-name").value = group ? group.team_name || group.name : "";
  $("#group-mode").value = group ? group.mode : "interactive";
  $("#member-rows").replaceChildren(...(group ? group.members : [{ id: "lead", name: "Team Lead", prompt: "" }]).map(memberRow));
  refreshLead(group ? group.lead : "lead");
  $("#group-error").hidden = true;
  $("#template").value = "";
  $("#group-dialog").showModal();
}

function applyTemplate() {
  const template = state.templates.find((t) => t.id === $("#template").value);
  if (!template) return;
  $("#group-name").value = template.name;
  $("#member-rows").replaceChildren(...template.members.map(memberRow));
  refreshLead(template.lead);
}

async function saveGroup(event) {
  event.preventDefault();
  const body = { name: $("#group-name").value.trim(), mode: $("#group-mode").value, members: readMembers(), lead: $("#group-lead").value };
  try {
    body.role_ids = body.members.filter(m=>state.boundRoles.has(m.id)).map(m=>m.id);

    const result = state.editing
      ? await api(`/groups/${state.editing.team_id || state.editing.id}`, { method: "PATCH", body })
      : await api("/groups", { method: "POST", body });
    $("#group-dialog").close();
    await loadGroups();
    openGroup(state.editing?.id || result.id);
  } catch (error) {
    $("#group-error").textContent = error.message; $("#group-error").hidden = false;
  }
}

// ---------------------------------------------------------------- comfort helpers
function setNav(id) {
  for (const b of document.querySelectorAll("#sections button")) {
    if (b.id === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
}

function updateConnection() {
  const ready = Boolean(state.config?.has_key);
  $("#conn-dot").classList.toggle("ready", ready);
  $("#conn-title").textContent = ready ? "DeepSeek 已连接" : "填写 API key";
  $("#conn-detail").textContent = ready
    ? `${state.config.model} · 思考${state.config.thinking === "enabled" ? "开" : "关"}`
    : "配置后团队才能开始工作";
  const banner = $("#key-banner");
  if (banner) banner.hidden = ready || !state.current;
}

// A room with nothing but its creation notice gets a friendly start card.
function renderWelcome() {
  const timeline = $("#timeline"), g = state.group;
  const existing = timeline.querySelector(".welcome");
  const substantive = [...timeline.children].some(n => !n.classList.contains("divider") && !n.classList.contains("chat-drafts") && !n.classList.contains("welcome"));
  if (substantive || state.feed?.root.childElementCount || !g || state.live) { existing?.remove(); return; }
  if (existing) return;
  const assistant = g.kind === "assistant";
  const lead = g.members.find(m => m.id === g.lead);
  const card = el("div", { class: "welcome" },
    assistant ? brandSeal()
      : el("div", { class: "avatar-stack" }, ...g.members.slice(0, 6).map(m => avatar(m.id, m.name))),
    el("h3", {}, assistant ? "你好，我是工作室助手" : `欢迎来到「${g.name}」`),
    el("p", {}, assistant
      ? "告诉我你想完成什么。简单的问题我直接回答；需要团队时，我会建群、找合适的角色并把任务派出去。"
      : `把任务发到群里，${lead ? lead.name : "负责人"}会拆分工作、委派给合适的成员，并在需要时向你确认。`),
    assistant ? null : el("div", { class: "roster" }, ...g.members.map(m => el("span", { class: "chip" + (m.id === g.lead ? " lead" : "") }, m.name, " @" + m.id))),
    el("div", { class: "tips" },
      el("div", {}, el("b", {}, "说清目标"), "要做什么、给谁用、什么算完成。"),
      el("div", {}, el("b", {}, "随时插话"), "任务进行中也能补充要求或追问进展。"),
      el("div", {}, el("b", {}, "看交付"), "完成后点「文件」预览和下载成果。")));
  timeline.insertBefore(card, state.feed?.root || null);
}

function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}

function renderUploads() {
  const files = Array.from($("#uploads").files || []);
  $("#attachment-chips").replaceChildren(...files.map((f, i) => el("span", { class: "chip" }, icon("clip"), f.name,
    el("button", { type: "button", "aria-label": "移除 " + f.name, onclick: () => {
      const keep = new DataTransfer();
      Array.from($("#uploads").files).forEach((x, j) => { if (j !== i) keep.items.add(x); });
      $("#uploads").files = keep.files; renderUploads();
    } }, "×"))));
}

function setupComposer() {
  const input = $("#input"), timeline = $("#timeline"), bottom = $("#bottom");
  input.addEventListener("input", autoGrow);
  $("#uploads").addEventListener("change", renderUploads);
  $("#key-banner-open").addEventListener("click", () => connectionSheet().catch(showError));
  timeline.addEventListener("scroll", () => {
    bottom.hidden = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 240;
  });
  bottom.addEventListener("click", () => { timeline.scrollTo({ top: timeline.scrollHeight, behavior: "smooth" }); });
  // Dropping files anywhere in the room attaches them.
  const room = $("#room");
  room.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); });
  room.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    const merged = new DataTransfer();
    [...$("#uploads").files, ...e.dataTransfer.files].forEach(f => merged.items.add(f));
    $("#uploads").files = merged.files; renderUploads(); input.focus();
  });
}

// ---------------------------------------------------------------- boot
async function boot() {
  if (window.markdownit) md = window.markdownit({ html: false, linkify: true, breaks: true });
  state.config = await api("/bootstrap");
  state.templates = state.config.templates;
  if (!state.config.chat_protocol) {
    $("#composer").prepend(el("div",{class:"muted small",role:"status"},
      "当前 Studio 服务只支持整段回复。重启这个实例并刷新页面可开启流式回复。"));
  }
  setupDirectory();setupReader();fillIcons();updateConnection();
  setupComposer(); setupSessions();
  $("#config").textContent = `负责人 ≤${state.config.lead_max_steps} 步 · 成员 ≤${state.config.member_max_steps} 步`;
  $("#template").append(...state.templates.map((t) => el("option", { value: t.id }, t.name)));
  $("#template").addEventListener("change", applyTemplate);
  $("#new-group").addEventListener("click", () => openDialog());
  $("#edit-group").addEventListener("click", () => state.group && openDialog(state.group));
  $("#add-member").addEventListener("click", () => { $("#member-rows").append(memberRow()); });
  $("#cancel-group").addEventListener("click", () => $("#group-dialog").close());
  $("#group-form").addEventListener("submit", saveGroup);
  $("#composer").addEventListener("submit", send);
  $("#input").addEventListener("keydown", (event) => {
    if (mentionKey(event)) return;
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode!==229) send(event);
  });
  $("#toggle-files").addEventListener("click", () => toggleFiles());
  $("#close-files").addEventListener("click", () => toggleFiles(false));
  $("#toggle-activity").addEventListener("click", () => {
    const box = $("#activity"); box.hidden = !box.hidden; box.scrollTop = box.scrollHeight;
  });
  $("#stop").addEventListener("click", async () => {
    if (state.current && confirm("停止当前任务？已发布的文件会保留。")) {
      await api(`/groups/${state.current}/cancel`, { method: "POST", body: {} });
      poll();
    }
  });
  await loadGroups();
  setInterval(()=>{ if (!document.hidden) loadGroups(); }, 5000);
  const hash = location.hash.slice(1);
  if (taskAddress(hash)) await openTaskAddress(hash);
  else if (Object.hasOwn(directoryRoutes,hash)) await directoryRoutes[hash]();
  else if (hash && state.groups.some((g) => g.id === hash)) await openGroup(hash);
  else await openGroup((await api("/assistant")).id);
}

const directoryRoutes={workbench:()=>workbenchView(),rooms:()=>roomsView(),roles:()=>rolesView(),tasks:()=>tasksView()};
window.addEventListener("hashchange", () => {
  const hash = location.hash.slice(1);
  if (taskAddress(hash)) openTaskAddress(hash).catch(showError);
  else if (Object.hasOwn(directoryRoutes,hash)) directoryRoutes[hash]().catch(showError);
  else if (hash && state.groups.some(g=>g.id===hash) && hash!==state.current) openGroup(hash);
});
document.addEventListener("DOMContentLoaded", () => boot().catch(showError));
