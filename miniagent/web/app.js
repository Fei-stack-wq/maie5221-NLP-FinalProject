"use strict";
// miniagent Studio — vanilla JS client. Polls /api/groups/<id> for new chat
// events (by seq) and live activity; all mutating calls send X-Studio: 1.

const $ = (selector) => document.querySelector(selector);
const state = {
  config: null, templates: [], groups: [], current: null, group: null,
  after: 0, activityAfter: 0, live: null, timer: null, editing: null, rendered: new Set(),
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
  if (init.method && init.method !== "GET") init.headers["X-Studio"] = "1";
  const response = await fetch("/api" + path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function markdown(text) {
  const box = el("div", { class: "body" });
  if (md) box.innerHTML = md.render(text || "");
  else box.textContent = text || "";
  for (const link of box.querySelectorAll("a")) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
  return box;
}

function memberName(id) {
  const member = state.group && state.group.members.find((m) => m.id === id);
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
  completed: "完成", incomplete: "未完成", failed: "失败", cancelled: "已停止", max_steps: "步数用尽",
  stuck: "循环中止", budget: "预算用尽", error: "出错", running: "进行中",
};

function statusChip(status) {
  return el("span", { class: `status ${status}` }, STATUS_LABEL[status] || status);
}

function fileChips(changes) {
  if (!changes || !changes.length) return null;
  return el("div", { class: "chips", style: "margin-top:8px" }, changes.slice(0, 40).map((c) =>
    el("span", {
      class: `chip file ${c.change}`, title: c.change,
      onclick: () => c.change !== "deleted" && openFile(c.path),
    }, c.path)));
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
  const who = (label, extra) => el("div", { class: "who" }, label, extra || null, el("span", {}, fmtTime(event.time)));
  switch (event.kind) {
    case "group_created":
      return el("div", { class: "divider" }, el("span", {}, `群聊已创建 · ${event.members.length} 位成员 · 负责人 ${memberName(event.lead)}`));
    case "group_updated":
      return el("div", { class: "divider" }, el("span", {}, "群聊设置已更新"));
    case "user_message":
      return el("div", { class: "msg user" },
        who(el("b", {}, event.during_task ? "你（任务进行中）" : "你"),
          event.addressed ? el("span", {}, `→ @${event.addressed}`) : null),
        markdown(event.text));
    case "task_started":
      return el("div", { class: "divider" }, el("span", {}, `任务开始 · ${memberName(event.agent)} 负责`));
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
        markdown(event.text), fileChips(event.changes));
      return collapsible(node, event.text);
    }
    case "question":
      return el("div", { class: "msg question" }, who(el("b", {}, `${memberName(event.agent)} 向你提问`)), markdown(event.question));
    case "answer":
      return el("div", { class: "msg user" }, who(el("b", {}, `你的回答 → ${memberName(event.agent)}`)), markdown(event.text));
    case "publish":
      return el("div", { class: "divider" }, el("span", {}, `${memberName(event.agent)} 发布了 ${event.changes.length} 个文件改动`));
    case "final":
      return el("div", { class: "msg final" },
        who(el("b", {}, `${memberName(event.agent)} · 最终答复`), statusChip(event.status)), markdown(event.text));
    case "task_finished": {
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
  list.replaceChildren(...state.groups.map((g) => el("li", {
    class: g.id === state.current ? "active" : "", onclick: () => openGroup(g.id),
  }, el("span", { class: "name", title: g.name }, g.name),
    el("span", { class: `dot ${g.waiting_for_you ? "waiting" : g.running ? "running" : ""}`,
      title: g.waiting_for_you ? "等待你的回复" : g.running ? "进行中" : "" }))));
}

// ---------------------------------------------------------------- room
async function openGroup(id) {
  state.current = id; state.after = 0; state.activityAfter = 0; state.rendered = new Set();
  state.steps = {}; state.stepsTask = null; state.lastActivity = null;
  history.replaceState(null, "", "#" + id);
  $("#timeline").replaceChildren(); $("#activity").replaceChildren();
  $("#empty").hidden = true; $("#room").hidden = false;
  $("#files").hidden = true;
  await poll(true);
  loadGroups();
}

function renderHead() {
  const g = state.group;
  $("#room-name").textContent = g.name + (g.mode === "autonomous" ? "（自主模式）" : "");
  $("#room-members").replaceChildren(...g.members.map((m) =>
    el("span", { class: `chip ${m.id === g.lead ? "lead" : ""}`, title: m.prompt }, `${m.name} · @${m.id}${m.id === g.lead ? " · 负责人" : ""}`)));
  const template = state.templates.find((t) => t.name === g.name);
  $("#task-buttons").replaceChildren(...(template ? template.tasks.map((t, i) =>
    el("button", { type: "button", title: t.title, onclick: () => { $("#input").value = t.text; $("#input").focus(); } }, `填入任务 ${i + 1}`)) : []));
  $("#stop").hidden = !g.running;
  $("#edit-group").disabled = g.running;
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
    $("#input").placeholder = "输入你的回答……";
  } else {
    banner.hidden = true;
    $("#input").placeholder = "任务进行中：这里的消息会在成员下一步时送达。也可以点“停止”。";
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
  clearTimeout(state.timer);
  const id = state.current;
  if (!id) return;
  try {
    const data = await api(`/groups/${id}?after=${state.after}&activity_after=${state.activityAfter}`);
    if (id !== state.current) return;
    state.group = data.group;
    renderHead();
    const timeline = $("#timeline");
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 160;
    for (const event of data.events) {
      state.after = Math.max(state.after, event.seq);
      if (state.rendered.has(event.seq)) continue;
      state.rendered.add(event.seq);
      const node = renderEvent(event);
      if (node) timeline.append(node);
    }
    if (data.events.length && (first || nearBottom)) timeline.scrollTop = timeline.scrollHeight;
    if (data.live) {
      renderActivity(data.live.activity);
      state.activityAfter = data.live.activity_seq;
    } else {
      state.activityAfter = 0;
    }
    if (!data.live && state.live) loadGroups();
    state.live = data.live;
    renderLive(data.live);
    if (!data.live) $("#input").placeholder = "发布任务或回复群里的问题。以 @成员ID 开头可直接点名某位成员。Enter 发送，Shift+Enter 换行。";
  } catch (error) {
    console.warn(error);
  }
  state.timer = setTimeout(poll, state.live ? 900 : 2500);
}

async function send(event) {
  event.preventDefault();
  const input = $("#input");
  const text = input.value.trim();
  if (!text || !state.current) return;
  const errorBox = $("#composer-error");
  errorBox.hidden = true;
  try {
    await api(`/groups/${state.current}/messages`, { method: "POST", body: { text } });
    input.value = "";
    poll();
    loadGroups();
  } catch (error) {
    errorBox.textContent = error.message; errorBox.hidden = false;
  }
}

// ---------------------------------------------------------------- files
async function toggleFiles(force) {
  const panel = $("#files");
  const show = force === undefined ? panel.hidden : force;
  panel.hidden = !show;
  if (!show) return;
  $("#download-zip").href = `/api/groups/${state.current}/archive.zip`;
  const data = await api(`/groups/${state.current}/files`);
  const list = $("#file-list");
  list.replaceChildren(...data.files.map((f) => el("li", { "data-path": f.path, onclick: () => openFile(f.path) },
    el("span", {}, f.path), el("span", { class: "size" }, f.size > 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`))));
  if (!data.files.length) list.append(el("li", { class: "muted" }, "项目里还没有文件。"));
}

async function openFile(path) {
  if ($("#files").hidden) await toggleFiles(true);
  for (const li of document.querySelectorAll("#file-list li")) li.classList.toggle("active", li.dataset.path === path);
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
  preview.replaceChildren(head, lower.endsWith(".md") ? markdown(text) : el("pre", {}, text.length > 400000 ? text.slice(0, 400000) + "\n…" : text));
}

// ---------------------------------------------------------------- group dialog
function memberRow(member = { id: "", name: "", prompt: "" }) {
  const row = el("div", { class: "member-row" },
    el("input", { placeholder: "ID，如 dev", value: member.id, class: "m-id", required: true, pattern: "[A-Za-z][A-Za-z0-9_-]{0,47}" }),
    el("input", { placeholder: "名称，如 Software Developer", value: member.name, class: "m-name", required: true }),
    el("button", { type: "button", class: "link", onclick: () => { row.remove(); refreshLead(); } }, "移除"),
    el("textarea", { placeholder: "职责与角色提示词", class: "m-prompt" }, member.prompt));
  row.querySelector(".m-id").addEventListener("input", refreshLead);
  return row;
}

function readMembers() {
  return [...document.querySelectorAll(".member-row")].map((row) => ({
    id: row.querySelector(".m-id").value.trim(),
    name: row.querySelector(".m-name").value.trim(),
    prompt: row.querySelector(".m-prompt").value.trim(),
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
  state.config = await api("/config");
  state.templates = await api("/templates");
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
  if (hash && state.groups.some((g) => g.id === hash)) openGroup(hash);
}

window.addEventListener("hashchange", () => {
  const hash = location.hash.slice(1);
  if (hash && hash !== state.current) openGroup(hash);
});
document.addEventListener("DOMContentLoaded", boot);
