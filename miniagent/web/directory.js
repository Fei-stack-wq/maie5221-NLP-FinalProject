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
  return el("header", {class:"view-head"}, el("h2",{},title), el("div",{class:"actions"},...actions));
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
async function uploadFile(file) {
  if (file.size > 20 * 1024 * 1024) throw Error("附件超过 20 MiB");
  const data = await new Promise((resolve,reject)=>{
    const reader = new FileReader();
    reader.onload=()=>resolve(String(reader.result).split(",")[1]);
    reader.onerror=reject; reader.readAsDataURL(file);
  });
  return api("/uploads",{method:"POST",body:{name:file.name,data}});
}
async function roomsView() {
  const root = enterView("rooms"), rooms = await api("/groups");
  if (state.view!=="rooms" || state.current) return;
  const search = editInput("",{placeholder:"搜索会话","aria-label":"搜索会话",type:"search"});
  const filter = el("select",{"aria-label":"会话类型"},...["全部","群聊","私聊","已归档"].map(x=>el("option",{},x)));
  const list = el("div",{class:"cards"});
  function draw() {
    const visible = rooms.filter(g=>g.kind!=="assistant" && g.archived===(filter.value==="已归档")
      && (!["私聊","群聊"].includes(filter.value) || g.kind===(filter.value==="私聊"?"dm":"group"))
      && g.name.toLowerCase().includes(search.value.toLowerCase())).sort((a,b)=>b.last_activity-a.last_activity);
    list.replaceChildren(...visible.map(g=>button(g.name+" · "+(g.kind==="dm"?"私聊":"群聊"),()=>openGroup(g.id))));
    if (!visible.length) list.append(el("p",{class:"muted"},"没有匹配的会话"));
  }
  search.oninput = filter.onchange = draw;
  root.append(heading("最近对话",button("新建群聊",()=>openDialog())),field("查找",search),filter,list);draw();
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
  root.append(heading("角色库",button("创建角色",()=>roleForm())),...roles.map(r=>button(r.name+" · @"+r.id,()=>roleDetail(r))));
  if (!roles.length) root.append(el("p",{class:"muted"},"还没有保存的角色"));
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
  root.append(heading("任务记录"),...rows.map(t=>button((STATUS_LABEL[t.status]||t.status)+" · "+t.group_name+" · "+t.text,()=>taskDetail(t.id))));
  if (!rows.length) root.append(el("p",{class:"muted"},"还没有任务"));
}
async function connectionSheet() {
  const key=editInput("",{type:"password",autocomplete:"new-password"});
  sheet("模型连接",el("p",{},state.config.model+" · "+(state.config.has_key?"已配置密钥":"尚未配置密钥")),field("DeepSeek API key",key),
    button("验证并保存",async()=>{
      const result=await api("/settings/api-key",{method:"POST",body:{api_key:key.value}});
      state.config.has_key=result.has_key;key.value="";
      $("#config").textContent=state.config.model+" · 已配置密钥";
      sheet("模型连接",el("p",{},result.verified?"验证通过":"密钥已保存，暂未确认连接"));
    }));
}
function roomOptions() {
  const g=state.group, path=editInput(g.folder||"");
  const patch=async body=>{await api("/groups/"+g.id,{method:"PATCH",body});$("#sheet").close();await openGroup(g.id);};
  sheet("会话管理",el("p",{},g.members.map(m=>m.name+" (@"+m.id+")").join(" · ")),
    button(g.pinned?"取消固定":"固定到侧栏",()=>patch({pinned:!g.pinned})),
    button(g.archived?"恢复会话":"归档会话",()=>patch({archived:!g.archived})),
    field("本机工作区",path),button("选择文件夹",async()=>{const r=await api("/folders/pick",{method:"POST",body:{}});if(r.path)path.value=r.path;}),
    button("保存工作区",()=>patch({folder:path.value})),
    button("移到废纸篓",async()=>{if(confirm("将此会话移到本地废纸篓？")){await api("/groups/"+g.id,{method:"DELETE"});$("#sheet").close();await roomsView();}}));
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
    "manage-members":()=>$("#edit-group").click(),"room-options":roomOptions,"task-options":runOptions,"bottom":()=>$("#timeline").scrollTop=$("#timeline").scrollHeight};
  for(const [id,fn] of Object.entries(actions)) $("#"+id).onclick=()=>Promise.resolve().then(fn).catch(showError);
  $("#sheet-content").onsubmit=e=>e.preventDefault();
}
