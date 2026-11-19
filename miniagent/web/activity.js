// Public conversation activity. Each disclosure uses native keyboard interaction.
(() => {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[char]);
  const labels = {
    list_dir:'列出目录', read_file:'读取文件', read_lines:'读取指定行', find_files:'查找文件',
    search_files:'搜索文件', write_file:'写入文件', edit_file:'编辑文件', append_file:'追加文件',
    delete_file:'删除文件', run_shell:'执行命令', run_python:'运行 Python', run_browser:'检查网页',
    http_get:'打开来源', search_sources:'搜索来源', submit_review:'提交评审',
    list_review_evidence:'查询评审证据', delegate_task:'委派任务', delegate:'委派任务',
    ask_human:'请求用户答复', human:'请求用户答复', list_studio_roles:'查看角色',
    create_studio_role:'创建角色', create_studio_group:'创建群聊'
  };
  const states = {running:'执行中', succeeded:'完成', failed:'失败', interrupted:'已中断'};
  const minutes = ms => ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(1) + ' s';
  const cache = new Map();

  function render(activity) {
    const calls = activity?.calls;
    if (!Array.isArray(calls) || !calls.length) return '';
    const recent = calls.at(-1);
    const current = calls.find(call => call.status === 'running') || recent;
    const rows = calls.map(call => {
      const state = Object.hasOwn(states, call.status) ? call.status : 'interrupted';
      const title = labels[call.tool] || call.tool;
      const result = [call.error, call.output].filter(Boolean).map(escape).join(' · ');
      return `<details class="activity-call" data-key="activity-${escape(call.id)}">
        <summary><span class="activity-indicator" data-status="${state}" aria-hidden="true"></span>
        <span class="activity-title">${escape(title)} ${call.summary ? `<code>${escape(call.summary)}</code>` : ''}</span>
        <span class="activity-state">${escape(states[state])}</span></summary>
        <div class="activity-info"><code>${escape(call.tool)}</code>
          ${call.role ? `<span>${escape(typeof roleLabel === 'function' ? roleLabel(call.role) : call.role)}</span>` : ''}
          ${Number.isFinite(call.duration_ms) ? `<span>${escape(minutes(call.duration_ms))}</span>` : ''}</div>
        ${result ? `<pre class="activity-result">${result}</pre>` : ''}</details>`;
    }).join('');
    return `<details class="activity-group" data-key="activity-list" ${calls.length < 5 ? 'open' : ''}>
      <summary><span>工具调用 <b>${activity.truncated ? '最近 ' : ''}${calls.length}</b></span>
        <span class="activity-latest">${escape(labels[current.tool] || current.tool)} · ${escape(states[current.status] || '')}</span></summary>
      <div class="activity-calls">${rows}</div></details>`;
  }

  function block(kind, runId, path, label) {
    const key = JSON.stringify([kind, runId, path]);
    return `<details class="activity-lazy ${kind === 'file' ? 'activity-file-preview' : ''}" data-key="${escape(key)}"
      data-activity-kind="${kind}" data-run="${escape(runId)}" data-path="${escape(path)}">
        <summary>${escape(label)}</summary><div class="activity-body">${cache.get(key)?.html || ''}</div></details>`;
  }

  function renderTurn(turn, detail) {
    if (turn.run_id === detail?.run?.id) return render(detail.activity);
    return block('history', turn.run_id, '', '查看本轮工具调用');
  }

  function preview(runId, file) {
    const path = file?.path;
    if (typeof path !== 'string') return '';
    const name = file.name || path.split('/').pop();
    if (/\.png$/i.test(path)) {
      return `<details class="activity-lazy activity-file-preview" data-key="${escape(JSON.stringify(['image', runId, path]))}"
        data-activity-kind="image" data-run="${escape(runId)}" data-path="${escape(path)}">
        <summary>预览截图 ${escape(name)}</summary><div class="activity-image-body"></div></details>`;
    }
    if (!/\.(md|txt|csv|tsv|rst|json|log)$/i.test(path)) return '';
    return block('file', runId, path, '预览 ' + name);
  }

  function loadImage(details) {
    const host = details.querySelector('.activity-image-body');
    // The chat reconciles its light DOM on each poll. Each opened disclosure keeps
    // its own loaded image without giving closed copies or future views a src.
    if (host.shadowRoot) return;
    const root = host.attachShadow({mode:'open'});
    const url = fileURL(details.dataset.run, details.dataset.path, 'preview');
    function show() {
      const image = new Image();
      image.setAttribute('part', 'thumbnail');
      image.alt = '截图 ' + details.dataset.path.split('/').pop();
      image.decoding = 'async';
      image.addEventListener('error', () => {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.textContent = '截图加载失败 · 重试';
        retry.setAttribute('part', 'retry');
        retry.addEventListener('click', show, {once:true});
        root.replaceChildren(retry);
      }, {once:true});
      root.replaceChildren(image);
      image.src = url;
    }
    show();
  }

  async function load(details, retry = false) {
    const {activityKind, run, path} = details.dataset;
    const key = JSON.stringify([activityKind, run, path]);
    if (!retry && cache.has(key)) return;
    const entry = {html:'<p class="activity-note" role="status">正在加载…</p>'};
    // Entries only contain excerpts. Bound history when a studio stays open for days.
    if (cache.size >= 80) cache.delete(cache.keys().next().value);
    cache.set(key, entry);
    details.querySelector('.activity-body').innerHTML = entry.html;
    try {
      const base = '/api/runs/' + encodeURIComponent(run);
      const url = activityKind === 'history' ? base + '/activity' :
        base + '/excerpt?' + new URLSearchParams({path});
      const response = await fetch(url, {headers:{Accept:'application/json'}});
      if (!response.ok) {
        if (response.status === 415 && activityKind === 'file') {
          const failure = await response.json();
          entry.html = `<p class="activity-note">${escape(failure.error)}</p>`;
        } else throw Error();
      } else {
        const data = await response.json();
        if (activityKind === 'history') {
          entry.html = render(data) || '<p class="activity-note">本轮没有工具调用记录。</p>';
        } else {
          if (typeof data.content !== 'string') throw Error();
          const lines = data.content.split('\n');
          const excerpt = lines.slice(0, 12).join('\n').slice(0, 1200);
          const clipped = data.truncated || lines.length > 12 || excerpt.length < data.content.length;
          entry.html = `<pre class="activity-preview-text">${escape(excerpt || '（空文件）')}</pre>${clipped ?
            '<p class="activity-note">显示内容节选，可从文件链接查看全文。</p>' : ''}`;
        }
      }
    } catch {
      entry.html = '<button type="button" class="text-button" data-activity-retry>加载失败 · 重试</button>';
    }
    if (cache.get(key) !== entry) return;
    // Navigation may replace a view while its request is in flight.
    document.querySelectorAll('.activity-lazy').forEach(node => {
      if (node.dataset.key === key) node.querySelector('.activity-body').innerHTML = entry.html;
    });
  }

  document.addEventListener('toggle', event => {
    const node = event.target;
    if (!node.matches?.('details.activity-lazy') || !node.open) return;
    if (node.dataset.activityKind === 'image') loadImage(node);
    else load(node);
  }, true);
  document.addEventListener('click', event => {
    const retry = event.target.closest?.('[data-activity-retry]');
    if (retry) load(retry.closest('.activity-lazy'), true);
  });
  window.StudioActivity = {render, renderTurn, preview};
})();
