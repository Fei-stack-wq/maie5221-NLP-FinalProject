"use strict";

function refreshMailComposer(live=state.live) {
  const bar=$("#mail-routing"), toggle=$("#interrupt-mail");
  const available=live?.mailboxes && !live.pausing && !$("#queue").checked && state.group?.kind!=="assistant";
  bar.hidden=!available;
  if (!available || bar.dataset.task!==live?.task_id || !live.interrupts) toggle.checked=false;
  bar.dataset.task=live?.task_id || "";
  if (!available) return;
  let rest=$("#input").value.trimStart();
  const recipients=[];
  const aliases=state.group.members.flatMap(m=>[m.id,m.name].map(name=>({id:m.id,name:"@"+name})))
    .sort((a,b)=>b.name.length-a.name.length);
  while (rest.startsWith("@")) {
    const match=aliases.find(a=>rest.toLowerCase().startsWith(a.name.toLowerCase()) &&
      (!rest[a.name.length] || /[\s,:：，]/u.test(rest[a.name.length])));
    if (!match) break;
    if (!recipients.includes(match.id)) recipients.push(match.id);
    rest=rest.slice(match.name.length).replace(/^[\s,:：，]+/u,"");
  }
  $("#mail-recipient").textContent=recipients.length ? "发给 "+recipients.map(id=>"@"+id).join("、") : "发给全体执行成员";
  toggle.parentElement.hidden=!live.interrupts;
}

function mailActor(id) {
  return ({user:"你",studio:"工作室","*":"全体执行成员"})[id] || memberName(id);
}

function mailEvent(event,scope) {
  const key=id=>"mail-"+event.task_id+"-"+id;
  const who=id=>id==="user" || id==="studio" || id==="*" ? mailActor(id) : "@"+mailActor(id);
  if (event.kind==="mail_receipt") {
    const host=document.getElementById(key(event.mail_id));
    if (!host) return null;
    const text=event.interrupted ? "已打断 " : "已送达 ";
    host.querySelector(".mail-receipts")?.append(el("span",{},text+who(event.agent)));
    return null;
  }
  const recipients=(event.recipients || []).map(who).join("、");
  const kind=event.mail_kind, sender=who(event.sender);
  const names={steer:"修改要求",checkin:"请负责人决定",decision:"负责人决定",teammate_question:"提问",
               teammate_answer:"答复",note:"留言",notice:"工作提醒"};
  const meta=el("small",{class:"mail-receipts","aria-label":"送达情况"});
  const head=el("div",{class:"who"},el("b",{},sender),el("span",{},"→ "+recipients),
    el("span",{},names[kind] || "消息"),el("span",{},fmtTime(event.time)));
  const inner=el("div",{id:key(event.id),class:"mail-content"},head,meta);
  if (kind==="steer" && event.sender==="user") {
    const bubble=document.getElementById("steer-"+event.task_id+"-"+event.in_reply_to);
    if (bubble) {
      inner.replaceChildren(el("small",{},recipients+(event.urgent?" · 请求打断":" · 下一步接收")),meta);
      bubble.append(inner);
      return null;
    }
  }
  const content=documentBody(event.summary || event.text || "",scope);
  const parent=event.reply_to && document.getElementById(key(event.reply_to));
  if (kind==="decision" && parent && ("decided_for" in event || event.status==="unavailable")) {
    const actions={unavailable:"未获得负责人决定",continue:"继续",adjust:"调整要求",stop_and_report:"立即交回报告",hold:"稍后处理",
                   forward:"转达",forward_urgent:"立即转达",stop_member:"请成员收尾",reply:"已回复"};
    const decision=el("div",{id:key(event.id),class:"mail-decision"},
      el("b",{},sender+" · "+(actions[event.action]||"决定")));
    // The actual forwarded message and public reply already have their own bubbles.
    if (!["reply","forward","forward_urgent","stop_member"].includes(event.action)) decision.append(content);
    decision.append(meta); parent.append(decision);
    return null;
  }
  if (kind==="teammate_answer" && parent?.querySelector("details")) {
    const detail=parent.querySelector("details");
    detail.append(el("b",{},sender+(event.ok===false?" · 未取得答复":" 答复")),content);
    if (event.source) detail.append(el("small",{class:"muted"},event.source));
    // Keep a receiver for the answer's own read receipt without repeating the question.
    const answer=el("div",{id:key(event.id)},meta);
    detail.append(answer);
    return null;
  }
  if (kind==="teammate_question") {
    inner.insertBefore(el("details",{},el("summary",{},(event.text||"队友问题").split("\n")[0].slice(0,120)),content),meta);
  } else {
    inner.insertBefore(content,meta);
    if (kind==="checkin" && event.decision) inner.insertBefore(el("p",{},event.decision),meta);
    if (event.options?.length) inner.insertBefore(el("ul",{},...event.options.map(s=>el("li",{},s))),meta);
    if (event.step) inner.insertBefore(el("small",{},"计划步骤 "+event.step),meta);
  }
  return el("article",{class:"msg team-mail with-avatar","data-kind":kind},
    event.sender==="user"?youAvatar():avatar(event.sender,mailActor(event.sender),"sm"),inner);
}
