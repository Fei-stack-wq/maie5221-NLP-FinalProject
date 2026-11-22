"use strict";

// Relative paths and reader history are scoped to one project or one task publication.
function relativeFile(raw, source = "") {
  let path;
  try { path = decodeURIComponent(raw); } catch { return null; }
  if (!path || /^(?:[a-z][\w+.-]*:|\/)|[\\?#\u0000-\u001f]/i.test(path)) return null;
  const parts = source.split("/").slice(0, -1);
  for (const part of path.split("/")) {
    if (part === "..") { if (!parts.length) return null; parts.pop(); }
    else if (part && part !== ".") parts.push(part);
  }
  return parts.length ? parts.join("/") : null;
}
function fileURL(ref) {
  const base = ref.tid ? "/api/tasks/" + ref.tid : "/api/groups/" + ref.gid;
  return base + "/files/" + ref.path.split("/").map(encodeURIComponent).join("/") +
    (ref.sha256 ? "?sha256=" + encodeURIComponent(ref.sha256) : "");
}
function fileRef(scope, path) {
  const file=scope.files?.get(path);
  if (file?.versions) return file.versions.length===1 ? file.versions[0] : {gid:scope.gid,path,versions:file.versions};
  return {gid:scope.gid, tid:scope.tid, path, files:scope.files, sha256:file?.sha256};
}
function jumpHeading(root, fragment) {
  let slug;
  try { slug = decodeURIComponent(fragment); } catch { return; }
  Array.from(root.querySelectorAll("[data-heading]")).find(n=>n.dataset.heading===slug)?.scrollIntoView({block:"start"});
}
function documentBody(text, scope = {}) {
  const box = el("div", {class:"body"});
  if (!md) { box.textContent=text; return box; }
  box.innerHTML = md.render(text || "");
  const used = new Set();
  for (const h of box.querySelectorAll("h1,h2,h3,h4,h5,h6")) {
    const base = h.textContent.trim().toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu,"").replace(/\s+/g,"-");
    let slug=base, i=1;
    while (used.has(slug)) slug=base+"-"+i++;
    h.dataset.heading=slug; used.add(slug);
  }
  for (const a of box.querySelectorAll("a[href]")) {
    const href=a.getAttribute("href");
    if (/^(https?:|mailto:)/i.test(href)) { a.target="_blank"; a.rel="noopener noreferrer"; continue; }
    const [raw, fragment] = href.split("#",2), path=relativeFile(raw,scope.path);
    if (!raw && fragment) a.onclick=e=>{e.preventDefault();jumpHeading(box,fragment);};
    else if ((scope.tid || scope.gid) && path) a.onclick=e=>{
      e.preventDefault(); openDocument({...fileRef(scope,path),fragment});
    };
    else a.removeAttribute("href");
  }
  for (const img of box.querySelectorAll("img[data-source]")) {
    const path = relativeFile(img.dataset.source,scope.path);
    if (path && (scope.tid || scope.gid) && /\.(png|jpe?g|gif|webp)$/i.test(path))
      img.src=fileURL(fileRef(scope,path));
    else img.replaceWith(el("span",{class:"muted"},img.alt || "图片"));
  }
  if (scope.files?.size) {
    const walker=document.createTreeWalker(box,NodeFilter.SHOW_TEXT), texts=[];
    for (let n; (n=walker.nextNode());) if (!n.parentElement.closest("a,pre")) texts.push(n);
    const paths=[...scope.files.keys()].filter(p=>/\.(md|markdown)$/i.test(p)).sort((a,b)=>b.length-a.length);
    if (paths.length) {
      const escaped=paths.map(p=>p.replace(/[.*+?^{}$()|[\]\\]/g,"\\$&"));
      const pattern=new RegExp("(?<![\\w/.-])(?:"+escaped.join("|")+")(?![\\w/-])","gu");
      for (const n of texts) {
        const chunks=[]; let start=0;
        for (const match of n.data.matchAll(pattern)) {
          const ref=fileRef(scope,match[0]);
          chunks.push(n.data.slice(start,match.index),el("a",{href:fileURL(ref),onclick:e=>{
            e.preventDefault();openDocument(ref);
          }},match[0])); start=match.index+match[0].length;
        }
        if (chunks.length) n.replaceWith(...chunks,n.data.slice(start));
      }
    }
  }
  return box;
}

const readerHistory=[];
let readerRequest;
function openDocument(ref) {
  const dialog=$("#document-reader");
  if (!dialog.open) { readerHistory.length=0; dialog.showModal(); }
  readerHistory.push(ref);
  displayDocument();
}
async function displayDocument() {
  readerRequest?.abort();
  const request=new AbortController(); readerRequest=request;
  const ref=readerHistory[readerHistory.length-1], body=$("#reader-body"), raw=$("#reader-download");
  $("#reader-title").textContent=ref.path;
  $("#reader-origin").textContent=ref.tid ? "任务交付 · "+ref.tid : "会话工作区";
  $("#reader-back").hidden=readerHistory.length<2;
  raw.hidden=true;
  if (ref.versions) {
    $("#reader-origin").textContent="选择交付版本";
    body.replaceChildren(...ref.versions.map(version=>button("打开任务 "+version.tid,()=>openDocument(version))));
    return;
  }
  raw.href=fileURL(ref); raw.download=ref.path.split("/").pop();
  body.replaceChildren(el("p",{class:"muted"},"正在读取…")); body.scrollTop=0;
  try {
    if (ref.tid && !state.config.workbench_version) throw Error("当前服务尚未加载任务版本阅读接口。");
    const response=await fetch(raw.href,{signal:request.signal});
    if (!response.ok) throw Error(response.status===404?"此文件的发布版本不可用。":"文件读取失败 ("+response.status+")");
    raw.hidden=false;
    if (/\.(png|jpe?g|gif|webp)$/i.test(ref.path)) {
      body.replaceChildren(el("img",{src:raw.href,alt:ref.path})); return;
    }
    if (!/\.(md|markdown|txt|log|csv|tsv|json|jsonl|ya?ml|toml|ini|html?|css|jsx?|tsx?|py|sh|sql|xml|svg|c|cpp|h)$/i.test(ref.path)) {
      body.replaceChildren(el("p",{},"可下载原文件查看。")); return;
    }
    const content=await response.text();
    if (request.signal.aborted) return;
    body.replaceChildren(/\.(md|markdown)$/i.test(ref.path) ? documentBody(content,ref) : el("pre",{},content));
    if (ref.fragment) jumpHeading(body,ref.fragment);
  } catch(error) {
    if (!request.signal.aborted) body.replaceChildren(el("p",{class:"error"},error.message));
  }
}
function setupReader() {
  $("#reader-back").onclick=()=>{readerHistory.pop();displayDocument();};
  $("#reader-close").onclick=()=>$("#document-reader").close();
  $("#document-reader").onclose=()=>{readerRequest?.abort();readerHistory.length=0;};
  if (md) md.renderer.rules.image=(tokens,idx)=>{
    const t=tokens[idx], esc=md.utils.escapeHtml;
    return '<img data-source="'+esc(t.attrGet("src")||"")+'" alt="'+esc(t.content)+'">';
  };
}
