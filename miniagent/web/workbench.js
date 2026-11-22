"use strict";

function fileIndex(changes) {
  return new Map(changes.map(c=>[c.path,c]));
}
function artifactCards(scope, changes) {
  if (!changes?.length) return null;
  return el("div",{class:"artifact-grid"},changes.map(c=>{
    const deleted=c.change==="deleted", path=c.path, ext=path.split(".").pop();
    const card=el("button",{type:"button",class:"artifact-card",disabled:deleted,
      title:path,"aria-label":path+(deleted?"（已删除）":"")},el("small",{},ext===path?"FILE":ext.toUpperCase()),
      el("span",{},el("strong",{},path),el("small",{},({added:"新增",modified:"修改",deleted:"删除"})[c.change]||c.change,
        Number.isFinite(c.size)?" · "+new Intl.NumberFormat().format(c.size)+" B":"")));
    if (!deleted) card.onclick=()=>openDocument({...fileRef(scope,path),sha256:c.sha256});
    return card;
  }));
}

function taskAddress(hash) {
  return /^(g-[a-f0-9]{12})\/task\/(t-[a-f0-9]{12})$/.exec(hash);
}
async function openTaskAddress(hash) {
  const [,gid,tid]=taskAddress(hash);
  await openGroup(gid);
  if (state.current!==gid) return;
  await taskDetail(tid);
  if (state.current===gid && $("#sheet").open) {
    history.replaceState(null,"","#"+hash);
    $("#sheet").addEventListener("close",()=>{
      if (location.hash==="#"+hash) history.replaceState(null,"","#"+gid);
    },{once:true});
  }
}
function dispatchLink(label, hash, open) {
  return el("a",{href:"#"+hash,onclick:event=>{
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    open().catch(showError);
  }},label);
}
function dispatchNotice(event) {
  const gid=event.target_group_id, tid=event.target_task_id;
  const links=el("div",{class:"dispatch-links"},dispatchLink(event.group_name,gid,()=>openGroup(gid)));
  if (tid) links.append(dispatchLink("查看任务",gid+"/task/"+tid,()=>taskDetail(tid)));
  const finished=event.kind==="dispatch_finished";
  const title=finished?"执行结果":tid?(event.queued?"任务已排队":"任务已派出"):
    (event.room_kind==="dm"?"私聊已创建":"群聊已创建");
  const card=el("article",{class:"msg dispatch-notice","data-notice":event.kind},
    el("div",{class:"who"},el("b",{},title),finished?statusChip(event.status):null,el("span",{},fmtTime(event.time))),links);
  if (!finished) return card;
  const changes=event.artifacts || [], scope={gid,tid,files:fileIndex(changes)};
  if (event.text) card.append(documentBody(event.text,scope));
  if (event.error || event.warning) card.append(el("p",{class:"error"},[event.error,event.warning].filter(Boolean).join(" · ")));
  if (event.evaluation?.total) card.append(el("p",{},"验收通过 "+event.evaluation.passed+" / "+event.evaluation.total));
  if (changes.length) card.append(artifactCards(scope,changes));
  return collapsible(card,event.text);
}

function taskTree(task, events, scope) {
  const root=el("div",{class:"tree-root"},el("strong",{},"@"+(task.primary||"—")),statusChip(task.status),el("p",{},task.text));
  const list=el("ol",{class:"branches"}), pending=new Map();
  const active=["running","queued"].includes(task.status);
  for (const e of events) {
    if (e.kind==="delegation") {
      const badge=el("small",{class:"tree-status"},active?"等待回报":"无回报记录");
      const panel=el("details",{class:"branch","data-assignment":e.seq},el("summary",{},
        el("span",{class:"tree-actor"},"@"+e.agent+" → @"+e.assignee),badge,el("span",{class:"tree-brief"},e.task)));
      if (e.deliverables?.length) panel.append(el("p",{},el("strong",{},"委托交付　"),e.deliverables.join("、")));
      list.append(el("li",{},panel));
      const queue=pending.get(e.assignee)||[]; queue.push({panel,badge}); pending.set(e.assignee,queue);
    } else if (e.kind==="report") {
      const branch=pending.get(e.agent)?.shift();
      if (!branch) continue;
      branch.badge.replaceWith(statusChip(e.status));
      branch.panel.append(el("p",{class:"muted"},"成员回报 · "+fmtTime(e.time)),
        documentBody(e.text,scope));
      const cards=artifactCards(scope,e.changes);
      if (cards) branch.panel.append(cards);
    }
  }
  return el("section",{class:"task-tree","aria-label":"任务树"},el("h4",{},"任务树"),root,list.children.length?list:null);
}

// Each trace widget owns its cursor and nodes. Opening history only reads that task.
// Queue by actor and id: two model calls may reuse an id on different steps.
function toolWidget(tid) {
  const summary=el("summary",{},"工具调用"), list=el("div",{class:"tool-list"});
  const feedback=el("small",{class:"muted"}), widget=el("details",{class:"tool-widget"},summary,list,feedback);
  const pending=new Map();
  let cursor=0, count=0, busy=false, again=false, finished=false, endLabel="";
  function append(event) {
    const key=JSON.stringify([event.agent,event.call_id||event.tool]);
    const queue=pending.get(key)||[];
    const isResult=event.type==="tool_result";
    let row=isResult?queue.shift():null;
    if (!row) {
      const badge=el("small",{class:"tool-badge"});
      const detail=el("details",{class:"tool-row"},el("summary",{},el("span",{class:"tool-actor"},"@"+event.agent),
        el("code",{},event.tool||"工具"),badge));
      row={badge,detail}; list.append(detail); count++;
      if (!isResult) {
        queue.push(row); detail.append(el("small",{class:"muted"},"调用参数摘要"),el("pre",{},JSON.stringify(event.arguments??{},null,2)));
      }
    }
    pending.set(key,queue);
    row.badge.textContent=isResult?(event.ok?"完成":"失败"):"等待结果";
    row.badge.dataset.status=isResult?(event.ok?"ok":"error"):"pending";
    if (isResult) row.detail.append(el("small",{class:"muted"},"返回摘要"),el("pre",{class:"tool-output"},event.preview||"（无输出）"));
  }
  async function refresh() {
    if (!widget.open || !widget.isConnected) return;
    if (busy) { again=true; return; }
    if (finished) return;
    busy=true;
    try {
      if (!state.config.workbench_version) {
        list.replaceChildren();pending.clear();count=0;
        (await api("/tasks/"+tid)).activity.filter(e=>["tool_call","tool_result"].includes(e.type)).forEach(append);
      } else {
        let page;
        do {
          page=await api("/tasks/"+tid+"/tools?cursor="+cursor);
          if (!widget.isConnected) return;
          page.items.forEach(append);cursor=page.cursor;
        } while(page.more && widget.open);
        if (page.more) return;
      }
      summary.textContent="工具调用 · "+count+" 次";
      feedback.textContent=count?"":"暂无工具调用记录";
      if (endLabel && !again) {
        finished=true;
        for (const queue of pending.values()) for (const row of queue) row.badge.textContent="无结果记录";
      }
    } catch(error) { feedback.textContent=error.message; }
    finally {
      busy=false;
      if (again) { again=false;refresh(); }
    }
  }
  widget.ontoggle=refresh;
  widget.update=(last,status)=>{
    if (status) endLabel=status;
    if (!widget.open && last) summary.textContent="工具调用 · @"+last.agent+" · "+last.tool;
    if (widget.open) refresh();
  };
  return widget;
}

async function workbenchView() {
  const root=enterView("workbench");
  history.replaceState(null,"","#workbench");
  const view=el("section",{class:"workbench"}); root.append(view);
  const [tasks,rooms]=await Promise.all([api("/tasks"),api("/groups")]);
  if (!view.isConnected) return;
  const stats=el("div",{class:"bench-stats"}), list=el("section",{class:"bench-list"});
  const categories=[
    ["全部任务",()=>true],
    ["进行中",t=>t.status==="running" || t.status==="queued"],
    ["已完成",t=>t.status==="completed"],
    ["待回复",t=>t.status==="running" && rooms.some(g=>g.id===t.group_id && g.waiting_for_you)]
  ];
  function select(index) {
    [...stats.children].forEach((b,i)=>b.setAttribute("aria-pressed",i===index));
    const filtered=tasks.filter(categories[index][1]);
    list.replaceChildren(el("h3",{},index?categories[index][0]:"最近任务"));
    for (const t of filtered) {
      const date=new Date(t.created_at*1000);
      list.append(el("button",{type:"button",class:"bench-row",onclick:()=>taskDetail(t.id).catch(showError)},
        el("span",{class:"bench-row-main"},el("small",{},t.group_name+" · @"+(t.primary||"—")),el("strong",{},t.text)),
        statusChip(t.status),el("time",{},Number.isNaN(date.getTime())?"":date.toLocaleDateString())));
    }
    if (!filtered.length) list.append(el("p",{class:"muted"},"暂无"+categories[index][0]));
  }
  categories.forEach(([name,test],i)=>stats.append(el("button",{type:"button",onclick:()=>select(i)},
    el("span",{},name),el("strong",{},String(tasks.filter(test).length)))));
  const target=el("select",{"aria-label":"发送到会话",required:true},el("option",{value:""},"选择会话"),
    ...rooms.filter(g=>!g.archived).map(g=>el("option",{value:g.id},g.name)));
  const input=el("textarea",{rows:2,placeholder:"给哪个团队交办什么工作？","aria-label":"消息",required:true});
  const form=el("form",{class:"bench-compose"},el("h3",{},"发消息"),input,
    el("div",{class:"actions"},target,el("button",{type:"submit",class:"primary"},"发送到会话")));
  form.onsubmit=async e=>{
    e.preventDefault();
    const gid=target.value, text=input.value.trim();
    if (!gid || !text) return;
    try {
      await openGroup(gid);
      if (state.current!==gid) return;
      $("#input").value=text;
      $("#composer").requestSubmit();
    } catch(error) { showError(error); }
  };
  view.append(heading("工作台",button("刷新",workbenchView)),stats,form,list);
  select(0);
}

let taskViewRequest=0;
async function taskDetail(id) {
  const request=++taskViewRequest, current=state.current, view=state.view;
  let d=await api("/tasks/"+id+"?view=summary");
  if (request!==taskViewRequest || current!==state.current || view!==state.view) return;
  let t=d.task, selected="概览";
  const scope={gid:t.group_id,tid:id,files:fileIndex(d.changes)}, body=el("div",{class:"task-content"});
  const tabs=el("nav",{class:"task-tabs","aria-label":"任务详情"});
  async function render(name) {
    selected=name;
    body.replaceChildren();
    for (const b of tabs.children) b.setAttribute("aria-pressed",b.textContent===name);
    if (name==="概览") {
      body.append(taskTree(t,d.events,scope));
      if (t.final_answer) body.append(el("h4",{},"最终答复"),documentBody(t.final_answer,scope));
      if (t.evaluation) body.append(el("p",{},t.evaluation.total?"验收 "+t.evaluation.passed+"/"+t.evaluation.total:"未指定验收条件"));
      for (const c of t.evaluation?.checks || []) body.append(el("p",{},(c.passed?"通过":"未通过")+" · "+c.kind+" · "+(c.path||c.command)+(c.exit_code==null?"":" · exit "+c.exit_code)));
      for (const warning of [...(t.folder_warnings||[]),t.delivery_warning,t.error].filter(Boolean)) body.append(el("p",{},warning));
    } else if (name==="产物") {
      if (d.has_snapshot) body.append(el("a",{href:"/api/tasks/"+id+"/artifacts.zip",download:id+".zip"},"下载本任务快照"));
      body.append(artifactCards(scope,[...scope.files.values()])||el("p",{class:"muted"},"本任务没有发布文件。"));
    } else if (name==="轨迹") {
      const widget=toolWidget(id); body.append(widget);widget.open=true;
      if (!["running","queued"].includes(t.status)) widget.update(null,t.status);
    } else if (name==="对话") {
      for (const e of d.events) body.append(el("p",{},[e.agent||e.kind,e.text||e.task||e.question||e.status].filter(Boolean).join(" · ")));
    } else {
      const pane=el("div",{},"正在读取诊断…");body.append(pane);
      const detail=await api("/tasks/"+id);
      pane.replaceChildren(...detail.diagnostics.map(e=>el("pre",{},JSON.stringify(e,null,2))));
      if (!detail.diagnostics.length) pane.textContent="没有诊断记录";
    }
  }
  for (const name of ["概览","对话","轨迹","产物","诊断"]) tabs.append(button(name,()=>render(name)));
  let status=statusChip(t.status);
  sheet("任务详情",el("div",{class:"task-meta"},status,el("code",{},id),
    button("打开会话",()=>{$("#sheet").close();return openGroup(t.group_id);}),
    button("刷新记录",()=>taskDetail(id))),tabs,body);
  await render("概览");
  const visible=()=>body.isConnected && $("#sheet").open && request===taskViewRequest;
  async function update() {
    if (!visible()) return;
    try {
      const next=await api("/tasks/"+id+"?view=summary");
      if (!visible()) return;
      const changed=next.task.status!==t.status || next.events.length!==d.events.length ||
        next.has_snapshot!==d.has_snapshot || next.changes.length!==d.changes.length;
      d=next;t=d.task;scope.files=fileIndex(d.changes);
      if (changed) {
        const chip=statusChip(t.status);status.replaceWith(chip);status=chip;
        if (selected!=="轨迹") {
          const expanded=[...body.querySelectorAll(".branch[open]")].map(n=>n.dataset.assignment);
          await render(selected);
          for (const node of body.querySelectorAll(".branch")) node.open=expanded.includes(node.dataset.assignment);
        }
      }
      const ended=!["queued","running"].includes(t.status);
      body.querySelector(".tool-widget")?.update(null,ended?t.status:null);
      if (ended) return;
    } catch(error) { if (visible()) showError(error); }
    if (visible()) setTimeout(update,2000);
  }
  if (["queued","running"].includes(t.status)) setTimeout(update,2000);
}
