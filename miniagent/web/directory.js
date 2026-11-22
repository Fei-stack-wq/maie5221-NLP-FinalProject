"use strict";

function showError(error) {
  const notice = $("#notice");
  notice.hidden = false;
  notice.textContent = error.message || String(error);
  clearTimeout(state.noticeTimer);
  state.noticeTimer = setTimeout(() => notice.hidden = true, 7000);
}
function button(label, action) {
  return el("button", {type:"button", onclick:() => Promise.resolve().then(action).catch(showError)}, label);
}
function heading(title, ...actions) {
  const subtitle = typeof actions[0] === "string" ? actions.shift() : "";
  return el("header", {class:"view-head"}, el("div",{},el("h2",{},title), subtitle ? el("p",{},subtitle) : null),
    el("div",{class:"actions"},...actions));
}
function relativeTime(seconds) {
  if (!seconds) return "";
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
  return new Date(seconds * 1000).toLocaleDateString();
}
function sheet(title, ...elements) {
  $("#sheet-content").replaceChildren(el("h3",{},title),...elements);
  if (!$("#sheet").open) $("#sheet").showModal();
}
function field(label, input) { return el("label",{},label,input); }
function editInput(value="", options={}) { return el("input", {value, ...options}); }
function enterView(title) {
  stopChatStream();
  clearTimeout(state.timer);
  state.current = null;
  state.view = title;
  history.replaceState(null, "", "#" + title);
  setNav(title + "-nav");
  $("#member-panel").hidden = $("#empty").hidden = $("#room").hidden = $("#files").hidden = true;
  const root = $("#directory");
  root.hidden = false; root.replaceChildren();
  return root;
}
async function fillLibrary() {
  const roles = await api("/roles");
  $("#library-member").replaceChildren(el("option",{value:""},"选择角色"),
    ...roles.map(r=>el("option",{value:r.id},r.name)));
  $("#library-member").onchange = (event) => {
    const role = roles.find(r=>r.id===event.target.value);
    if (role && !readMembers().some(m=>m.id===role.id)) {
      state.boundRoles.add(role.id);
      $("#member-rows").append(memberRow(role));
      refreshLead();
    }
    event.target.value = "";
  };
}
async function uploadFile(file, roomId) {
  if (file.size > 20 * 1024 * 1024) throw Error("附件超过 20 MiB");
  const data = await new Promise((resolve,reject)=>{
    const reader = new FileReader();
    reader.onload=()=>resolve(String(reader.result).split(",")[1]);
    reader.onerror=reject; reader.readAsDataURL(file);
  });
  return api("/groups/"+roomId+"/uploads",{method:"POST",body:{name:file.name,data}});
}
async function roomsView() {
  const root = enterView("rooms"), rooms = await api("/groups?view=conversations");
  if (state.view!=="rooms" || state.current) return;
  const search = editInput("",{placeholder:"搜索群聊或私聊","aria-label":"搜索会话",type:"search"});
  const kinds = ["全部","群聊","私聊","已归档"];
  let kind = "全部";
  const segmented = el("div",{class:"segmented",role:"group","aria-label":"会话类型"});
  const list = el("div",{class:"card-grid"});
  function draw() {
    for (const b of segmented.children) b.setAttribute("aria-pressed", b.textContent===kind);
    const visible = rooms.filter(g=>g.kind!=="assistant" && g.archived===(kind==="已归档")
      && (!["私聊","群聊"].includes(kind) || g.kind===(kind==="私聊"?"dm":"group"))
      && (g.team_name+" "+g.name).toLowerCase().includes(search.value.toLowerCase())).sort((a,b)=>b.last_activity-a.last_activity);
    list.replaceChildren(...visible.map(g=>el("button",{type:"button",class:"card",onclick:()=>openGroup(g.id).catch(showError)},
      el("div",{class:"card-top"},avatar(g.id,g.name,"square"),el("strong",{},g.team_id!==g.id?g.team_name+" / "+g.name:g.name),
        g.waiting_for_you?el("span",{class:"status interrupted"},"待你回复"):g.running?statusChip("running"):null),
      el("div",{class:"card-meta"},el("span",{class:"kind"+(g.kind==="dm"?" dm":"")},g.kind==="dm"?"私聊":"群聊"),
        el("span",{class:"avatar-stack"},...(g.members||[]).slice(0,5).map(m=>avatar(m.id,m.name,"sm"))),
        el("span",{},relativeTime(g.last_activity))),
      g.last_status?el("div",{class:"card-meta"},"上次任务",statusChip(g.last_status),`共 ${g.tasks} 项`):el("p",{},"还没有任务"))));
    if (!visible.length) list.append(el("div",{class:"empty-state"},"没有匹配的会话"));
  }
  for (const k of kinds) segmented.append(el("button",{type:"button",onclick:()=>{kind=k;draw();}},k));
  search.oninput = draw;
  root.append(heading("最近对话","所有群聊和私聊，按最近活动排序。",el("button",{type:"button",class:"primary",onclick:()=>openDialog()},icon("plus"),"新建群聊")),
    el("div",{class:"toolbar"},search,segmented),list);draw();
}
function roleForm(role) {
  const id=editInput(role?.id||""), name=editInput(role?.name||""), prompt=el("textarea",{rows:5},role?.prompt||"");
  id.disabled=Boolean(role);
  sheet(role?"编辑角色":"创建角色",field("角色 ID",id),field("名称",name),field("职责",prompt),button("保存",async()=>{
    await api(role?"/roles/"+role.id:"/roles",{method:role?"PATCH":"POST",body:{id:id.value,name:name.value,prompt:prompt.value}});
    $("#sheet").close(); await rolesView();
  }));
}
async function rolesView() {
  const root=enterView("roles"), roles=await api("/roles");
  if (state.view!=="roles" || state.current) return;
  const grid=el("div",{class:"card-grid"},...roles.map(r=>el("button",{type:"button",class:"card","aria-label":r.name+" · @"+r.id,onclick:()=>roleDetail(r).catch(showError)},
    el("div",{class:"card-top"},avatar(r.id,r.name,"lg"),el("span",{style:"display:grid;min-width:0"},el("strong",{},r.name),el("small",{class:"muted"},"@"+r.id))),
    el("p",{},r.prompt||"（未填写职责）"))));
  root.append(heading("角色库","可重复使用的角色。建群时从这里添加，角色的记忆和技能会跟着走。",
    el("button",{type:"button",class:"primary",onclick:()=>roleForm()},icon("plus"),"创建角色")),
    roles.length?grid:el("div",{class:"empty-state"},"还没有保存的角色。创建一个，之后建群时可以直接选用。"));
}
async function roleDetail(role) {
  const [history,tasks]=await Promise.all([api("/roles/"+role.id+"/knowledge"),api("/tasks")]);
  const latest=new Map(history.map(r=>[r.id,r]));
  const tasksForRole=tasks.filter(t=>t.role_ids?.includes(role.id) && (t.primary===role.id || t.assignments?.some(a=>a.agent===role.id)));
  const draft = (entry) => {
    const kind = el("select",{},el("option",{value:"memory"},"记忆"),el("option",{value:"skill"},"技能"));
    const body = el("textarea",{rows:6},entry?.text || "");
    kind.value = entry?.kind || "skill";
    kind.disabled = Boolean(entry);
    sheet(entry ? "编辑知识" : "添加角色知识",field("类型",kind),field("内容",body),
      el("p",{class:"muted"},entry ? entry.scope : "保存为角色通用知识草稿，可在角色页批准。"),
      button(entry ? "保存并批准" : "保存草稿",async()=>{
        if (entry) await change(entry,"approved",undefined,body.value);
        else {
          await api("/roles/"+role.id+"/knowledge",{method:"POST",body:{text:body.value,kind:kind.value}});
          await roleDetail(role);
        }
      }));
  };
  const change=async(row,status,revision,text)=>{
    await api("/roles/"+role.id+"/knowledge/"+row.id+"/status",{method:"POST",body:{status,revision,text}});
    await roleDetail(role);
  };
  sheet(role.name,el("p",{},role.prompt),button("编辑角色",()=>roleForm(role)),button("新建私聊",async()=>{
    const group=await api("/groups",{method:"POST",body:{name:role.name,kind:"dm",lead:role.id,role_ids:[role.id]}});
    $("#sheet").close();await openGroup(group.id);
  }),el("h4",{},"分配的任务"),...tasksForRole.map(t=>button(t.group_name+" · "+t.text,()=>taskDetail(t.id))),
    el("h4",{},"记忆与技能"),button("添加知识",()=>draft()),...Array.from(latest.values()).map(r=>el("article",{},el("p",{},r.kind+" · "+r.status+" · "+r.scope),r.text,button("编辑内容",()=>draft(r)),
    ...["approved","rejected","archived"].map((v,i)=>button(["批准","拒绝","归档"][i],()=>change(r,v))),
    ...history.filter(v=>v.id===r.id&&v.revision<r.revision).map(v=>button("恢复版本 "+v.revision,()=>change(r,"restore",v.revision))))));
}
async function tasksView() {
  const root=enterView("tasks"), rows=await api("/tasks");
  if (state.view!=="tasks" || state.current) return;
  const list=el("section",{class:"bench-list"});
  for (const t of rows) {
    list.append(el("button",{type:"button",class:"bench-row","aria-label":(STATUS_LABEL[t.status]||t.status)+" · "+t.group_name+" · "+t.text,onclick:()=>taskDetail(t.id).catch(showError)},
      avatar(t.primary||t.group_id,t.primary||t.group_name,"sm"),
      el("span",{class:"bench-row-main"},el("small",{},t.group_name+" · @"+(t.primary||"—")),el("strong",{},t.text)),
      statusChip(t.status),el("time",{},relativeTime(t.created_at))));
  }
  root.append(heading("任务记录","每个任务的派工、产物和轨迹。"),rows.length?list:el("div",{class:"empty-state"},"还没有任务。到任意群聊里发布第一项工作吧。"));
}
async function connectionSheet() {
  const ready=Boolean(state.config.has_key);
  const key=editInput("",{type:"password",autocomplete:"new-password",placeholder:"sk-…",spellcheck:"false"});
  const toggle=el("button",{type:"button",onclick:()=>{const show=key.type==="password";key.type=show?"text":"password";toggle.textContent=show?"隐藏":"显示";}},"显示");
  const message=el("p",{class:"sheet-note",role:"status"});
  const save=el("button",{type:"button",class:"primary",onclick:async()=>{
    if (!key.value.trim()) { message.textContent="请先粘贴 API key。"; return; }
    save.disabled=true; message.textContent="正在向 DeepSeek 验证（不消耗 token）…";
    try {
      const result=await api("/settings/api-key",{method:"POST",body:{api_key:key.value}});
      state.config.has_key=result.has_key; key.value=""; updateConnection();
      if (state.group) renderHead();
      message.textContent=result.verified?"✓ 已验证并保存，新任务立即生效。":"已保存，但暂时没能确认连接；第一次运行任务时会再验证。";
      status.className="key-state ready"; status.lastChild.textContent="DeepSeek 已配置 · "+state.config.model;
    } catch(error) { message.textContent=error.message; }
    finally { save.disabled=false; }
  }},icon("check"),"验证并保存");
  const status=el("div",{class:"key-state"+(ready?" ready":"")},el("span",{class:"conn-dot"+(ready?" ready":"")}),
    el("span",{},ready?"DeepSeek 已配置 · "+state.config.model:"尚未配置 DeepSeek API key"));
  sheet("模型连接",status,field(ready?"更换 API key":"DeepSeek API key",el("div",{class:"key-row"},key,toggle)),save,message,
    el("p",{class:"sheet-note"},"密钥只保存在这台电脑的 .env 文件里，网页不会再显示完整内容。可在 platform.deepseek.com 创建。"));
  key.focus();
}
function workspaceSheet() {
  const g=state.group;
  if (!g || g.id !== state.current) return;
  const path=editInput(g.folder||"",{placeholder:"输入本机项目文件夹的绝对路径"});
  const error=el("p",{class:"error",role:"alert",hidden:true});
  const manual=el("details",{},el("summary",{},"手动输入路径"),field("文件夹路径",path));
  const actions=el("div",{class:"workspace-actions"});
  let busy=false;
  const current=()=>state.current===g.id && error.isConnected && $("#sheet").open;
  async function apply(getPath) {
    if (busy) return;
    busy=true; error.hidden=true;
    const controls=[...actions.querySelectorAll("button"),...manual.querySelectorAll("button")];
    controls.forEach(node=>{node.disabled=true;});
    try {
      const folder=await getPath();
      if (folder===null || !current()) return;
      const updated=await api("/groups/"+g.id,{method:"PATCH",body:{folder}});
      if (current()) {
        state.group=updated; renderHead(); $("#sheet").close(); poll();
      }
    } catch (err) {
      if (current()) {error.textContent=err.message; error.hidden=false; manual.open=true;}
    } finally {
      busy=false; controls.forEach(node=>{node.disabled=false;});
    }
  }
  actions.append(button("选择本地文件夹…",()=>apply(async()=>{
    const result=await api("/folders/pick",{method:"POST",body:{}});
    return result.path || null;
  })));
  if (g.folder) actions.append(button("使用 Studio 默认目录",()=>apply(()=>"")));
  manual.append(button("使用此路径",()=>apply(()=>{
    if (!path.value.trim()) throw Error("请输入本机文件夹的绝对路径。");
    return path.value;
  })));
  const help=g.running ? "当前任务结束后可以切换工作区。" :
    g.kind==="assistant" ? "聊天可直接查看所选目录和文本文件。新建群聊或私聊会沿用此目录。" :
    "聊天可直接查看所选目录和文本文件。执行任务时读取工作副本，交付后写回修改。";
  sheet("会话工作区",el("small",{class:"muted"},g.folder?"当前本地目录":"当前 Studio 默认目录"),
    el("code",{class:"workspace-path"},g.workspace_path),el("p",{class:"muted"},help),
    ...(g.running?[]:[actions,manual,error]));
}

function roomOptions() {
  const g=state.group;
  const patch=async body=>{await api("/groups/"+g.id,{method:"PATCH",body});$("#sheet").close();await openGroup(g.id);};
  sheet("会话管理",el("p",{},g.members.map(m=>m.name+" (@"+m.id+")").join(" · ")),
    ...(g.team_id===g.id?[button(g.pinned?"取消固定":"固定到侧栏",()=>patch({pinned:!g.pinned}))]:[]),
    button(g.kind==="group" && g.id===g.team_id ? (g.archived?"恢复群组":"归档群组") : (g.archived?"恢复会话":"归档会话"),()=>patch({archived:!g.archived})),
    button("会话工作区",workspaceSheet),
    button("移到废纸篓",async()=>{if(confirm(g.kind==="group" && g.id===g.team_id ? "将整个群组及其全部会话移到本地废纸篓？" : "将此会话移到本地废纸篓？")){await api("/groups/"+g.id,{method:"DELETE"});$("#sheet").close();await roomsView();}}));
}
function runOptions() {
  const opts=state.runOptions||{};
  const mode=el("select",{},...["interactive","autonomous"].map(x=>el("option",{value:x},x==="interactive"?"交互":"自主")));
  mode.value=opts.interaction_mode||state.group.mode;
  const keys=["member_max_steps","lead_max_steps","max_delegations"];
  const inputs=keys.map(k=>editInput(opts[k]??state.config[k],{type:"number",min:k==="max_delegations"?0:1,max:k==="max_delegations"?200:1000}));
  const checks=el("textarea",{rows:3,placeholder:"[]"},JSON.stringify(opts.checks||[]));
  sheet("下一项任务",field("工作方式",mode),...inputs.map((v,i)=>field(["成员步数","负责人步数","委派数"][i],v)),
    field("验收检查（JSON 数组）",checks),button("应用",()=>{
      state.runOptions={interaction_mode:mode.value,checks:JSON.parse(checks.value||"[]")};
      keys.forEach((k,i)=>state.runOptions[k]=Number(inputs[i].value));$("#sheet").close();
    }));
}
function setupDirectory() {
  const actions={"workbench-nav":workbenchView,"assistant-nav":async()=>openGroup((await api("/assistant")).id),"rooms-nav":roomsView,"roles-nav":rolesView,
    "tasks-nav":tasksView,"connection":connectionSheet,"sheet-close":()=>$("#sheet").close(),
    "manage-members":()=>$("#edit-group").click(),"room-options":roomOptions,"workspace-choice":workspaceSheet,"task-options":runOptions,"bottom":()=>$("#timeline").scrollTop=$("#timeline").scrollHeight};
  for(const [id,fn] of Object.entries(actions)) $("#"+id).onclick=()=>Promise.resolve().then(fn).catch(showError);
  $("#sheet-content").onsubmit=e=>e.preventDefault();
}
