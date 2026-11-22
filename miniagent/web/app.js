"use strict";
// miniagent Studio — vanilla JS client. Polls /api/groups/<id> for new chat
// events (by seq) and live activity; mutations send the instance token.

const $ = (selector) => document.querySelector(selector);
const state = {
  config: null, templates: [], groups: [], current: null, group: null,
  sending: new Set(), after: 0, activityAfter: 0, live: null, timer: null, editing: null, rendered: new Set(),
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
    node.classList.toggle("collapsed");
    toggle.textContent = node.classList.contains("collapsed") ? "展开全文" : "收起";
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
  switch (event.kind) {
    case "assistant_group_created":
    case "dispatch_sent":
    case "dispatch_finished":
      return dispatchNotice(event);
    case "group_created":
      return el("div", { class: "divider" }, el("span", {}, `群聊已创建 · ${event.members.length} 位成员 · 负责人 ${memberName(event.lead)}`));
    case "group_updated": {
      const changes=[];
      if (event.joined?.length) changes.push(event.joined.join("、")+" 加入群聊");
      if (event.left?.length) changes.push(event.left.join("、")+" 已移出群聊");
      return el("div",{class:"divider"},el("span",{},changes.join(" · ") || "群聊设置已更新"));
    }
    case "room_reply":
    case "progress":
      return el("div",{class:"msg"},who(el("b",{},memberName(event.agent))), markdown(event.text));
    case "user_message":
      return el("div", { class: "msg user" },
        who(el("b", {}, event.during_task ? "你（任务进行中）" : "你"),
          event.addressed ? el("span", {}, `→ @${event.addressed}`) : null),
        markdown(event.text), ...(event.attachments||[]).map(a=>el("span",{class:"chip"},a.name)));
    case "attachments":
      return el("div",{class:"msg user"},...(event.attachments||[]).map(a=>el("span",{class:"chip"},a.name)));
    case "queued_message":
      return el("div",{class:"divider"},el("span",{},"已排队 · "+event.text));
    case "task_started": {
      const tools=toolWidget(tid);state.toolWidgets.set(tid,tools);
      return el("section",{class:"chat-run"},el("div",{class:"chat-run-head"},
        el("strong",{},"任务开始 · "+memberName(event.agent)+" 负责"),button("查看任务树",()=>taskDetail(tid))),tools);
    }
    case "delegation": {
      const node = el("div", { class: "msg delegation" },
        who(el("b", {}, `${memberName(event.agent)} → ${memberName(event.assignee)}`), el("span", {}, "派工")),
        markdown(event.task),
        event.deliverables && event.deliverables.length
          ? el("div", { class: "chips", style: "margin-top:6px" }, event.deliverables.map((p) => el("span", { class: "chip" }, p))) : null);
      return collapsible(node, event.task);
    }
    case "report": {
      const level = event.status === "completed" ? "" : (event.status === "error" || event.status === "cancelled" ? "failed" : "incomplete");
      const node = el("div", { class: `msg report ${level}` },
        who(el("b", {}, memberName(event.agent)), statusChip(event.status),
          el("span", { class: "muted" }, `${event.steps || 0} 步 · ${event.tool_calls || 0} 次工具调用`)),
        markdown(event.text), artifactCards(scope,event.changes));
      return collapsible(node, event.text);
    }
    case "question":
      return el("div", { class: "msg question" }, who(el("b", {}, `${memberName(event.agent)} 向你提问`)), markdown(event.question));
    case "answer":
      return el("div", { class: "msg user" }, who(el("b", {}, `你的回答 → ${memberName(event.agent)}`)), markdown(event.text));
    case "publish":
      return el("div", { class: "msg publication" }, who(el("b",{},memberName(event.agent)),"文件发布"), artifactCards(scope,event.changes));
    case "final":
      return el("div", {class:"msg final"},
        who(el("b", {}, `${memberName(event.agent)} · 最终答复`), statusChip(event.status)), markdown(event.text),
        el("div",{class:"actions"},button("接受交付",()=>deliveryResponse("我接受这次交付。")),
          button("要求修改",()=>{ $("#input").value="请修改上一份交付：";$("#input").focus(); })));
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

// ---------------------------------------------------------------- sidebar
async function loadGroups() {
  try { state.groups = await api("/groups"); } catch { return; }
  const list = $("#group-list");
  list.replaceChildren(...state.groups.filter(g => g.pinned && !g.archived).map((g) => el("li", {
    class: g.id === state.current ? "active" : "", onclick: () => openGroup(g.id),
  }, el("span", { class: "name", title: g.name }, g.name),
    el("span", { class: `dot ${g.waiting_for_you ? "waiting" : g.running ? "running" : ""}`,
      title: g.waiting_for_you ? "等待你的回复" : g.running ? "进行中" : "" }))));
}

// ---------------------------------------------------------------- room
function stopChatStream() {
  state.feed?.source.close();
  state.feed = null;
}

function openChatStream(id) {
  stopChatStream();
  const timeline = $("#timeline");
  const root = el("div",{class:"chat-drafts"});
  timeline.append(root);
  const source = new EventSource("/api/groups/"+encodeURIComponent(id)+"/chat-stream");
  const feed = {source, root, items:new Map(), requested:0};
  state.feed = feed;
  let initial = true;
  source.onopen = () => { initial = true; };
  source.onmessage = (event) => {
    if (state.feed !== feed) return;
    const frame = JSON.parse(event.data);
    const nearBottom = timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight < 160;
    if (initial) {
      for (const draft of feed.items.values()) draft.removeAt = frame.after;
      initial = false;
    }
    for (const patch of frame.drafts) {
      let draft = feed.items.get(patch.id);
      if (patch.agent) {
        if (draft) draft.box.remove();
        const box = el("div",{class:"chat-draft"});
        if (patch.request || patch.attachments.length) {
          box.append(el("div",{class:"msg user"},el("div",{class:"who"},"你"),
            el("div",{class:"draft-text"},patch.request),
            patch.attachments.map(name=>el("span",{class:"chip"},name))));
        }
        const body = document.createTextNode("");
        const bubble = el("div",{class:"msg",hidden:true},el("div",{class:"who","data-actor":patch.agent},memberName(patch.agent)),
          el("div",{class:"draft-text"},body));
        box.append(bubble); root.append(box);
        draft = {box,bubble,body}; feed.items.set(patch.id,draft);
      }
      if (draft && patch.text) {
        draft.body.appendData(patch.text);
        draft.bubble.hidden = false;
      }
    }
    for (const key of frame.removed) {
      const draft=feed.items.get(key);
      if (draft) draft.removeAt=frame.after;
    }
    retireDrafts();
    if (nearBottom) timeline.scrollTop=timeline.scrollHeight;
    if (frame.after > state.after && frame.after > feed.requested) {
      feed.requested=frame.after;
      poll();
    }
  };
}

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
  $("#sending-status").hidden = !state.sending.has(id);
  if (state.current !== id) { state.runOptions = {}; $("#thinking").value = ""; $("#queue").checked = false; }
  $("#uploads").value = "";
  state.current = id; state.after = 0; state.activityAfter = 0; state.rendered = new Set();
  state.publications=new Map();state.toolWidgets=new Map();state.view="room";
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
  $("#room-name").textContent = g.name + (g.mode === "autonomous" ? "（自主模式）" : "");
  $("#member-panel").hidden = g.kind !== "group";
  $("#room-members").replaceChildren(...g.members.map((m) =>
    el("li", {title: m.prompt}, el("strong", {}, m.name), el("small", {}, `@${m.id}${m.id === g.lead ? " · 负责人" : ""}`))));
  const template = state.templates.find((t) => t.name === g.name);
  $("#task-buttons").replaceChildren(...(template ? template.tasks.map((t, i) =>
    el("button", { type: "button", title: t.title, onclick: () => { $("#input").value = t.text; $("#input").focus(); } }, `填入任务 ${i + 1}`)) : []));
  $("#stop").hidden = !g.running;
  $("#edit-group").disabled = g.kind === "assistant";
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
  $("#live-status").replaceChildren(el("span", { class: "spinner" }),
    `${active.map(memberName).join(" › ")} 正在工作${step}${doing}`);
  const banner = $("#question-banner");
  if (live.question) {
    banner.hidden = false;
    banner.replaceChildren(el("b", {}, `${memberName(live.question.agent)} 在等你回答：`), " ", live.question.question.slice(0, 300));
    banner.append(el("button",{type:"button", onclick:async()=>{await api(`/groups/${state.current}/messages`,{method:"POST",body:{text:"Please decide as a team and state your assumptions."}});poll();}}, "让团队决定"));
    $("#input").placeholder = "回复问题，也可以继续追问或交办新工作……";
  } else {
    banner.hidden = true;
    $("#input").placeholder = "询问进展、调整当前要求，或交办另一项任务……";
  }
}

async function deliveryResponse(text) {
  await api("/groups/"+state.current+"/messages",{method:"POST",body:{text}});
  await poll();
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
      if (event.task_id && event.changes?.length) {
        const files=new Map(state.publications.get(event.task_id));
        for (const c of event.changes) files.set(c.path,c);
        state.publications.set(event.task_id,files);
      }
      const node = renderEvent(event);
      if (node) timeline.insertBefore(node,feed.root);
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
    if (!data.live && state.live) loadGroups();
    state.live = data.live;
    renderLive(data.live);
    if (!data.live) $("#input").placeholder = "追问、讨论或交办工作。可用 @成员ID 点名。Enter 发送，Shift+Enter 换行。";
  } catch (error) {
    console.warn(error);
  }
  if (state.feed === feed) state.timer = setTimeout(poll, state.live ? 900 : 2500);
}

async function send(event) {
  event.preventDefault();
  const input = $("#input");
  const text = input.value.trim();
  if ((!text && !$("#uploads").files.length) || !state.current || state.sending.has(state.current)) return;
  const gid = state.current;
  state.sending.add(gid);
  $("#sending-status").hidden = false;
  const errorBox = $("#composer-error");
  errorBox.hidden = true;
  try {
    const queue = $("#queue").checked;
    const options = {...state.runOptions};
    if ($("#thinking").value) options.thinking = $("#thinking").value;
    const uploads = await Promise.all(Array.from($("#uploads").files, uploadFile));
    await api(`/groups/${gid}/messages`, {method:"POST", body:{text, attachments:uploads, options, queue}});
    if (state.current !== gid) return;
    state.runOptions = {};
    $("#queue").checked = false; $("#uploads").value = "";
    if (input.value.trim() === text) input.value = "";
    poll();
    loadGroups();
  } catch (error) {
    if (state.current === gid) { errorBox.textContent = error.message; errorBox.hidden = false; }
  } finally {
    state.sending.delete(gid);
    if (state.current === gid) $("#sending-status").hidden = true;
  }
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
  list.replaceChildren(...data.files.map((f) => el("li", { "data-path": f.path, onclick: () => openFile(f.path) },
    el("span", {}, f.path), el("span", { class: "size" }, f.size > 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`))));
  if (!data.files.length) list.append(el("li", { class: "muted" }, "项目里还没有文件。"));
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
  $("#group-name").value = group ? group.name : "";
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
      ? await api(`/groups/${state.editing.id}`, { method: "PATCH", body })
      : await api("/groups", { method: "POST", body });
    $("#group-dialog").close();
    await loadGroups();
    openGroup(result.id);
  } catch (error) {
    $("#group-error").textContent = error.message; $("#group-error").hidden = false;
  }
}

// ---------------------------------------------------------------- boot
async function boot() {
  if (window.markdownit) md = window.markdownit({ html: false, linkify: true, breaks: true });
  state.config = await api("/bootstrap");
  state.templates = state.config.templates;
  setupDirectory();setupReader();
  $("#config").textContent = `模型 ${state.config.model} · 思考 ${state.config.thinking === "enabled" ? "开" : "关"}` +
    ` · 负责人 ≤${state.config.lead_max_steps} 步 · 成员 ≤${state.config.member_max_steps} 步` +
    (state.config.has_key ? "" : " · ⚠ 未配置 DEEPSEEK_API_KEY");
  $("#template").append(...state.templates.map((t) => el("option", { value: t.id }, t.name)));
  $("#template").addEventListener("change", applyTemplate);
  $("#new-group").addEventListener("click", () => openDialog());
  $("#edit-group").addEventListener("click", () => state.group && openDialog(state.group));
  $("#add-member").addEventListener("click", () => { $("#member-rows").append(memberRow()); });
  $("#cancel-group").addEventListener("click", () => $("#group-dialog").close());
  $("#group-form").addEventListener("submit", saveGroup);
  $("#composer").addEventListener("submit", send);
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) send(event);
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
  setInterval(loadGroups, 5000);
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
