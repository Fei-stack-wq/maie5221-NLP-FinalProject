// Chat filenames are resolved against Studio's published metadata. Never fetch a path
// inferred from prose. Inside a receipt, the server checks its immutable manifest.
(() => {
  'use strict';
  const md = window.markdownit({html:false, linkify:false});
  const escape = md.utils.escapeHtml;
  const isMarkdown = path => /\.md$/i.test(path);
  const rootPattern = /^(?:handoffs\/[a-f0-9]{32}|(?:workspaces\/[^/]+\/)?agents\/[^/]+)\//;
  const projectPath = path => path.replace(rootPattern, '');
  const snapshot = path => /^handoffs\/[a-f0-9]{32}\//.test(path);
  const target = (run, file) => ({run, path:file.path, name:file.project_path || projectPath(file.path), size:file.size, kind:file.kind});
  const fallback = (run, path) => '#run/' + encodeURIComponent(run) + '/artifacts?' + new URLSearchParams({path});

  function attributes(ref) {
    return 'href="' + escape(fallback(ref.run, ref.path)) + '" data-md-run="' + escape(ref.run) +
      '" data-md-path="' + escape(ref.path) + '"' +
      (Number.isSafeInteger(ref.size) ? ' data-md-size="' + ref.size + '"' : '') + (ref.kind === 'diagnostic' ? ' data-md-diagnostic' : '');
  }

  function knownTurns() {
    return state.route === 'assistant' ? studioAssistant.turns() : state.detail?.conversation?.turns || [];
  }

  function groupFileIndex(row) {
    const turns = knownTurns();
    const horizon = turns.slice(0,turns.findIndex(turn => turn.run_id === row.parent_run_id) + 1);
    const ordinals = new Map(horizon.map((turn,index) => [turn.run_id, turn.ordinal || index + 1]));
    const candidates = new Map(), cited = new Map();
    function index(entry, byName) {
      const {run_id:run, path:name, ref:path} = entry;
      if (!ordinals.has(run) || typeof name !== 'string' || !isMarkdown(name) ||
          typeof path !== 'string' || !rootPattern.test(path) || projectPath(path) !== name) return;
      const ref = {run, path, name, ordinal:ordinals.get(run)};
      for (const key of [name, name.split('/').pop(), path]) {
        if (!byName.has(key)) byName.set(key, new Map());
        byName.get(key).set(run + '/' + path, ref);
      }
    }
    for (const turn of horizon) {
      if (turn.run_id === row.parent_run_id && row.after_turn === false) {
        for (const event of turn.events || []) {
          if (!event.handoff?.published || !event.at || event.at > row.at) continue;
          for (const file of event.artifacts || []) {
            index({run_id:turn.run_id,path:file.project_path,ref:file.path},candidates);
          }
        }
      } else {
        for (const item of turn.published_artifacts || []) index(item,candidates);
      }
    }
    for (const item of row.cited_artifacts || []) index(item,cited);
    const files = new Map();
    for (const [name, versions] of [...candidates, ...cited]) {
      const refs = [...versions.values()];
      files.set(name, refs.length === 1 ? refs[0] : {choices:refs});
    }
    return {files, names:[...files.keys()].sort((a,b) => b.length-a.length)};
  }

  function scope(turn, row) {
    const files = new Map();
    // A later group publication supersedes its predecessor. Files returned side
    // by side (different roles or child chats) have no such ordering.
    const addBatch = sources => {
      const batch = new Map();
      const place = (name, ref) => {
        const prior = batch.get(name);
        if (!batch.has(name) || (prior?.run === ref.run && prior.path === ref.path)) batch.set(name,ref);
        else batch.set(name,null);
      };
      for (const {run_id, artifacts} of sources) {
        for (const file of artifacts || []) {
          if (!run_id || typeof file.path !== 'string' || !isMarkdown(file.path) || !rootPattern.test(file.path)) continue;
          const ref = target(run_id, file);
          place(file.path,ref);
          place(ref.name,ref);
          if (typeof file.ref === 'string' && file.ref.startsWith('dispatch:')) place(file.ref,ref);
        }
      }
      for (const [key,value] of batch) files.set(key,value);
    };
    if (!turn) return files;
    const base = turn.base_run_id && knownTurns().find(t => t.run_id === turn.base_run_id);
    if (base) addBatch([base]);
    if (row?.kind === 'goal') return files;
    // A blocked writer may be describing a private revision of the same public name.
    if (row?.handoff?.published === false) {
      files.clear();
      addBatch([{run_id:turn.run_id, artifacts:row.artifacts}]);
      return files;
    }
    const events = turn.events || [];
    if (row && !events.includes(row)) return files;
    for (const event of events) {
      const listed = event.artifacts || [];
      addBatch([{run_id:turn.run_id, artifacts:event === row ? listed : listed.filter(file => file.kind !== 'diagnostic')}]);
      if (event === row) return files;
    }
    if (!row) {
      addBatch((turn.published_artifacts || []).map(item => ({
        run_id:item.run_id, artifacts:[{path:item.ref, project_path:item.path}]
      })));
      addBatch([turn,...(turn.dispatches || []).map(child => ({run_id:child.child_run_id, artifacts:child.artifacts}))]);
    }
    return files;
  }

  function relativePath(path, source = '') {
    if (!path || /^[a-z][\w+.-]*:|^\/|[\\?#\u0000-\u001f]/i.test(path)) return null;
    const parts = source.split('/').slice(0,-1);
    for (const part of path.split('/')) {
      if (part === '..') { if (!parts.length) return null; parts.pop(); }
      else if (part && part !== '.') parts.push(part);
    }
    return parts.length && parts.every(p => !p.startsWith('.')) ? parts.join('/') : null;
  }

  function resolve(raw, env, fromHref = false) {
    if (!raw) return null;
    let path = raw;
    if (fromHref) {
      try { path = decodeURIComponent(raw); } catch { return null; }
    }
    if (!isMarkdown(path)) return null;
    if (!env.source && path.startsWith('dispatch:')) return env.files.get(path) || null;
    const relative = relativePath(path, env.source?.name);
    if (!relative) return null;
    // In a document, resolve relative paths within that exact artifact root.
    // The API verifies every receipt path and digest, including unchanged siblings.
    if (env.source) {
      const prefix = env.source.path.match(rootPattern)?.[0];
      if (!prefix) return null;
      const nested = prefix + relative;
      if (env.source.kind === 'diagnostic' && nested !== env.source.path) return null;
      if (snapshot(env.source.path) && fromHref) return {run:env.source.run, path:nested, name:relative, kind:env.source.kind};
      const ref = env.files.get(nested);
      return ref?.run === env.source.run ? ref : null;
    }
    return env.files.get(relative) || null;
  }

  function linkOpen(ref, fragment = '') {
    if (ref.choices) return '<button type="button" class="md-file md-versions" title="选择文件版本" data-md-versions="' +
      escape(JSON.stringify(ref.choices)) + '"' + fragment + '>';
    return '<a class="md-file" ' + attributes(ref) + fragment + '>';
  }
  const linkTag = ref => ref.choices ? 'button' : 'a';

  function fileText(content, env) {
    if (env.inLink || !env.names.length) return escape(content);
    let html = '', offset = 0;
    const urls = [...content.matchAll(/(?:[a-z][a-z0-9+.-]*:\/\/|www\.)[^\s<>"`，。；！？、]+/gi)];
    for (const suffix of content.matchAll(/\.md(?![a-zA-Z0-9_/~-]|\.[a-zA-Z0-9])/gi)) {
      if (urls.some(m => m.index <= suffix.index && suffix.index < m.index + m[0].length)) continue;
      const end = suffix.index + 3;
      for (const name of env.names) {
        if (!content.endsWith(name,end)) continue;
        let start = end - name.length;
        if (start >= 2 && content.slice(start-2,start) === './') start -= 2;
        if (start < offset || /[a-zA-Z0-9_./%~-]/.test(content[start-1] || '')) continue;
        const path = content.slice(start,end);
        const ref = resolve(path, env);
        if (!ref && env.files.get(name) !== null) continue;
        html += escape(content.slice(offset,start)) + (ref ? linkOpen(ref) + escape(path) +
          '</' + linkTag(ref) + '>' : '<span class="md-unavailable" title="有多个同名附件，请在交付材料中选择">' + escape(path) + '</span>');
        offset = end;
        break;
      }
    }
    return html + escape(content.slice(offset));
  }

  md.renderer.rules.text = (tokens, i, options, env) => fileText(tokens[i].content, env);
  md.renderer.rules.code_inline = (tokens, i, options, env) => {
    const text = tokens[i].content;
    const code = '<code>' + escape(text) + '</code>';
    const ref = env.inLink ? null : resolve(text, env);
    if (ref) return linkOpen(ref) + code + '</' + linkTag(ref) + '>';
    const reason = env.files.get(text) === null ? '有多个同名附件，请在交付材料中选择' : '本条消息没有可打开的对应附件';
    return !env.inLink && isMarkdown(text) ? '<code title="' + reason + '">' + escape(text) + '</code>' : code;
  };
  md.renderer.rules.link_open = (tokens, i, options, env) => {
    const href = tokens[i].attrGet('href') || '';
    const [path, hash] = href.split('#', 2);
    let fragment = '';
    try { fragment = decodeURIComponent(hash || ''); } catch { /* malformed fragment */ }
    const ref = resolve(path, env, true);
    const bookmark = fragment && ' data-md-fragment="' + escape(fragment) + '"';
    env.inLink = 'a';
    if (ref) { env.inLink = linkTag(ref); return linkOpen(ref,bookmark); }
    if (!path && fragment && env.source) return '<a href="#' + escape(encodeURIComponent(fragment)) + '"' + bookmark + '>';
    if (/^https?:\/\//i.test(href)) return '<a href="' + escape(href) + '" target="_blank" rel="noopener noreferrer">';
    env.inLink = 'span';
    const reason = env.files.get(path) === null ? '有多个同名附件，请在交付材料中选择' : '此处没有可打开的交付附件';
    return '<span class="md-unavailable" title="' + reason + '">';
  };
  md.renderer.rules.link_close = (tokens, i, options, env) => {
    const tag = env.inLink;
    env.inLink = null;
    return '</' + tag + '>';
  };
  md.renderer.rules.image = (tokens, i, options, env, renderer) =>
    escape(renderer.renderInlineAsText(tokens[i].children || [], options, env));

  md.renderer.rules.heading_open = (tokens, i, options, env, renderer) => {
    if (env.source && tokens[i+1]?.type === 'inline') {
      const text = renderer.renderInlineAsText(tokens[i+1].children || [], options, env);
      const slug = text.trim().toLowerCase().replace(/[^\p{L}\p{M}\p{N}_\s-]/gu, '').replace(/\s+/g,'-');
      const count = env.slugs.get(slug) || 0;
      env.slugs.set(slug, count + 1);
      tokens[i].attrSet('data-md-heading', count ? slug + '-' + count : slug);
    }
    return renderer.renderToken(tokens, i, options);
  };

  const indices = new WeakMap();
  function fileIndex(turn, row) {
    if (!turn) return {files:new Map(), names:[]};
    let cache = indices.get(turn);
    if (!cache) { cache = new WeakMap(); indices.set(turn, cache); }
    const key = row || turn;
    if (cache.has(key)) return cache.get(key);
    if (row?.kind === 'group_message') {
      const index = groupFileIndex(row);
      cache.set(key,index);
      return index;
    }
    const files = scope(turn, row);
    const basenames = new Map();
    for (const [name, ref] of files) {
      if (!ref && !name.includes(':') && !rootPattern.test(name)) { basenames.set(name.split('/').pop(),null); continue; }
      if (!ref || name !== ref.name) continue;
      const base = name.split('/').pop();
      basenames.set(base, basenames.has(base) ? null : ref);
    }
    // Report.md is a useful shortcut only when it names one document.
    for (const [base,ref] of basenames) if (!files.has(base)) files.set(base,ref);
    const index = {files, names:[...files.keys()].sort((a,b) => b.length-a.length)};
    cache.set(key, index);
    return index;
  }

  function render(text, turn, row, inline = false, source) {
    const env = {...fileIndex(turn, row), source, slugs:new Map()};
    return inline ? md.renderInline(String(text ?? ''), env) : md.render(String(text ?? ''), env);
  }

  function documentTurn(run) {
    const turns = knownTurns();
    const owner = turns.find(t => t.run_id === run);
    if (owner) return owner;
    for (const turn of turns) {
      const child = turn.dispatches?.find(d => d.child_run_id === run);
      if (child) return {run_id:run, artifacts:child.artifacts};
    }
    return {run_id:run, artifacts:state.detail?.run?.id === run ? state.detail.artifacts : []};
  }

  function documentHTML(text, ref) {
    return render(text, documentTurn(ref.run), null, false, {...ref, name:projectPath(ref.path)});
  }

  const overDownloadLimit = size => size > 8 * 1024 ** 2;
  const attachmentPreviewBytes = 256 * 1024;
  const canPreviewAttachment = (name, size) => isMarkdown(name) && Number.isSafeInteger(size) &&
    size >= 0 && size <= attachmentPreviewBytes;

  async function attachmentText(url, controller) {
    const response = await fetch(url, {signal:controller.signal, redirect:'error'});
    if (!response.ok) throw new Error('读取附件失败（HTTP ' + response.status + '）。');
    const overflow = () => {
      controller.abort();
      return new Error('附件超过 256 KiB，请下载原文查看。');
    };
    if (Number(response.headers.get('Content-Length')) > attachmentPreviewBytes) throw overflow();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const chunks = [];
    let bytes = 0;
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > attachmentPreviewBytes) throw overflow();
        chunks.push(decoder.decode(value,{stream:true}));
      }
      chunks.push(decoder.decode());
      return chunks.join('');
    } finally {
      reader.releaseLock();
    }
  }

  function truncationNotice(size) {
    if (overDownloadLimit(size)) return '仅显示前 256 KiB。文件超过 8 MiB 下载上限，请在本机工作区打开全文。';
    if (!Number.isSafeInteger(size)) return '仅显示前 256 KiB。单文件最多可下载 8 MiB，更大文件请在本机工作区查看。';
    return '仅显示前 256 KiB，可下载原文查看其余内容。';
  }
  const dialog = document.getElementById('markdown-reader');
  const body = dialog.querySelector('article');
  const back = dialog.querySelector('[data-md-back]');
  function scrollToHeading(container, fragment) {
    const heading = [...(container?.querySelectorAll('[data-md-heading]') || [])]
      .find(h => h.dataset.mdHeading === fragment);
    heading?.scrollIntoView({block:'start'});
  }
  let stack = [], generation = 0, attachmentController;
  async function show(ref) {
    attachmentController?.abort();
    attachmentController = null;
    const request = ++generation;
    if (ref.choices) {
      dialog.querySelector('h2').textContent = ref.name;
      dialog.querySelector('.md-origin').textContent = '选择文件版本';
      dialog.querySelector('.md-path').hidden = true;
      dialog.querySelector('[download]').hidden = true;
      back.hidden = true;
      const fragment = ref.fragment ? ' data-md-fragment="' + escape(ref.fragment) + '"' : '';
      body.innerHTML = '<ul class="md-version-list">' + ref.choices.map(option =>
        '<li><a ' + attributes(option) + fragment + '>第 ' + escape(String(option.ordinal)) + ' 轮 · ' +
        escape(option.name) + '</a></li>').join('') + '</ul>';
      if (!dialog.open) dialog.showModal();
      return;
    }
    const name = projectPath(ref.path);
    dialog.querySelector('h2').textContent = name.split('/').pop();
    const pathLabel = dialog.querySelector('.md-path');
    pathLabel.textContent = name;
    pathLabel.hidden = !name.includes('/');
    dialog.querySelector('.md-origin').textContent = ref.attachmentURL ? '消息附件' : ref.kind === 'diagnostic' ? '诊断附件' : snapshot(ref.path) ? '交接时的版本' : '本轮工作区';
    dialog.querySelector('[download]').href = ref.attachmentURL || fileURL(ref.run, ref.path, 'download');
    dialog.querySelector('[download]').hidden = !ref.attachmentURL && overDownloadLimit(ref.size);
    back.hidden = stack.length < 2;
    body.textContent = '正在读取…';
    body.scrollTop = 0;
    if (!dialog.open) dialog.showModal();
    try {
      if (ref.attachmentURL) {
        attachmentController = new AbortController();
        const text = await attachmentText(ref.attachmentURL, attachmentController);
        if (request === generation && dialog.open) body.innerHTML = render(text, null, null, false, ref);
        return;
      }
      const data = await api(fileURL(ref.run, ref.path, 'artifact'));
      if (request !== generation || !dialog.open) return;
      body.innerHTML = (data.truncated ? '<p class="run-warning">' + truncationNotice(ref.size) + '</p>' : '') +
        documentHTML(data.content, ref);
      if (ref.fragment) scrollToHeading(body, ref.fragment);
    } catch (err) {
      if (request === generation && dialog.open) body.textContent = '无法打开这份文件。' + err.message;
    }
  }
  document.addEventListener('click', event => {
    const versions = event.target.closest?.('button[data-md-versions]');
    if (versions) {
      stack = [{name:versions.textContent, choices:JSON.parse(versions.dataset.mdVersions), fragment:versions.dataset.mdFragment}];
      show(stack[0]);
      return;
    }
    const link = event.target.closest?.('a[data-md-path], a[data-md-fragment]');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!link.dataset.mdPath) {
      scrollToHeading(link.closest('.prose'), link.dataset.mdFragment);
      return;
    }
    if (!dialog.open) stack = [];
    const ref = {run:link.dataset.mdRun, path:link.dataset.mdPath, size:Number(link.dataset.mdSize), fragment:link.dataset.mdFragment, kind:link.hasAttribute('data-md-diagnostic') ? 'diagnostic' : ''};
    if (stack.at(-1)?.path !== ref.path || stack.at(-1)?.run !== ref.run) stack.push(ref);
    show(ref);
  });
  back.addEventListener('click', () => { if (stack.length > 1) { stack.pop(); show(stack.at(-1)); } });
  dialog.querySelector('[data-md-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (!dialog.open) { generation++; attachmentController?.abort(); stack = []; } });
  window.addEventListener('hashchange', () => { if (dialog.open) dialog.close(); });
  function openAttachment(name, size, url) {
    if (!canPreviewAttachment(name, size)) return;
    const ref = {path:name, name, size, attachmentURL:url};
    stack = [ref];
    show(ref);
  }
  window.StudioMarkdown = {render, documentHTML, attributes, overDownloadLimit, truncationNotice, canPreviewAttachment, openAttachment};
})();
