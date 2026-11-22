const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { withFixture } = require('./browser_fixture.cjs');

withFixture('serve_markdown_fixture', async (page, url) => {
  const errors = [];
  const remote = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (!r.url().startsWith(new URL(url).origin)) remote.push(r.url()); });
  await page.goto(url);
  const first = page.locator('.chat-turn').first();
  const reader = page.locator('#markdown-reader');
  const preview = first.getByRole('button',{name:'预览 用户笔记.md'});
  const download = first.getByRole('link',{name:'下载 用户笔记.md'});
  await preview.click();
  await reader.getByRole('heading',{name:'Uploaded note'}).waitFor();
  assert.equal(await reader.locator('strong').innerText(),'原始记录');
  assert.equal(await reader.locator('img,script').count(),0);
  assert.equal(await reader.getByRole('link',{name:'下载原文'}).getAttribute('href'),await download.getAttribute('href'));
  await page.keyboard.press('Escape');
  assert.equal(await first.getByRole('button',{name:'预览 large-input.md'}).count(),0);
  assert(await first.getByRole('link',{name:'下载 large-input.md'}).isVisible());
  const rawDownload = page.waitForEvent('download');
  await download.click();
  assert((await fs.readFile(await (await rawDownload).path(),'utf8')).includes('<script>window.bad = true</script>'));
  // Even a stream without Content-Length must respect the preview cap.
  await page.evaluate(path => {
    const fetch = window.fetch;
    window.fetch = (input, options) => {
      if (input !== path) return fetch(input,options);
      window.fetch = fetch;
      return Promise.resolve(new Response(new ReadableStream({start(stream) {
        stream.enqueue(new Uint8Array(256 * 1024));
        stream.enqueue(new Uint8Array(1));
        stream.close();
      }})));
    };
  },await download.getAttribute('href'));
  await preview.click();
  await reader.getByText(/超过 256 KiB/).waitFor();
  assert.equal(await reader.locator('article h1').count(),0);
  await page.keyboard.press('Escape');
  const receipt = first.locator('[data-kind="member_report"]').filter({hasText:'评审报告'});
  const line = receipt.locator('.room-line');
  // Explicit link, inline code and bare path each resolve to the same receipt.
  const links = line.locator('a[data-md-path]');
  await links.first().waitFor();
  assert.equal(await links.count(), 3);
  assert.equal(new Set(await links.evaluateAll(nodes => nodes.map(n => n.dataset.mdPath))).size, 1);
  await links.first().click();
  await reader.getByRole('heading', {name:'Handoff edition'}).waitFor();
  assert.equal(await reader.getByRole('columnheader').count(), 2);
  assert.equal(await reader.locator('ol li').count(), 2);
  assert.equal(await reader.locator('blockquote').count(), 1);
  assert.equal(await reader.locator('pre code').innerText(), '<sample>\n');
  assert.equal(await reader.locator('strong').innerText(), 'Ready');
  assert.equal(await reader.getByRole('link',{name:'Source'}).getAttribute('rel'), 'noopener noreferrer');
  assert.equal(await reader.locator('img,script,a[href*="bootstrap"],a[href*="Outside"]').count(), 0);
  await reader.getByRole('link',{name:'Appendix'}).click();
  await reader.getByRole('heading',{name:'Original appendix'}).waitFor();
  assert.equal(await reader.getByRole('heading',{name:'Notes'}).getAttribute('data-md-heading'), 'notes');
  await reader.getByRole('button',{name:'返回'}).click();
  await reader.getByRole('heading',{name:'Handoff edition'}).waitFor();
  // Return to the report while a slower appendix request is still in flight.
  let reached, resume;
  const waiting = new Promise(resolve => reached = resolve);
  const release = new Promise(resolve => resume = resolve);
  const appendixResponse = response => response.url().includes('/artifact?') &&
    new URL(response.url()).searchParams.get('path')?.endsWith('/Appendix.md');
  await page.route('**/artifact?**', async route => {
    if (!appendixResponse(route.request())) return route.continue();
    const response = await route.fetch();
    reached();
    await release;
    await route.fulfill({response});
  });
  await reader.getByRole('link',{name:'Appendix'}).click();
  await waiting;
  await reader.getByRole('button',{name:'返回'}).click();
  await reader.getByRole('heading',{name:'Handoff edition'}).waitFor();
  const lateResponse = page.waitForResponse(appendixResponse);
  resume();
  await (await lateResponse).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal(await reader.getByRole('heading',{name:'Handoff edition'}).isVisible(),true);
  await page.keyboard.press('Escape');
  await reader.waitFor({state:'hidden'});
  assert.equal(await links.first().evaluate(a => a === document.activeElement), true);
  assert.equal(page.url(), url);
  const blocked = first.locator('[data-kind="member_report"]').filter({hasText:'暂存'});
  assert.equal(await blocked.locator('a[data-md-path]').count(), 0);
  const answer = first.locator('.chat-answer');
  assert.equal(await answer.locator('a').filter({hasText:/^ledger\.md$/}).count(),0);
  assert.match(await answer.getByText('ledger.md',{exact:true}).getAttribute('title'), /同名/);
  // The query parameter of a search URL should remain part of that URL's text.
  assert.equal(await answer.locator('a').filter({hasText:/^Report\.md$/}).count(),0);
  await answer.getByRole('link',{name:'agents/worker/reviewer/ledger.md'}).click();
  await reader.getByRole('heading',{name:'reviewer ledger'}).waitFor();
  await page.keyboard.press('Escape');
  await answer.getByRole('link',{name:'Long.md'}).click();
  await reader.getByText(/仅显示前 256 KiB，可下载/).waitFor();
  assert(!(await reader.innerText()).includes('LAST LINE'));
  const downloading = page.waitForEvent('download');
  await reader.getByRole('link',{name:'下载原文'}).click();
  assert((await fs.readFile(await (await downloading).path(),'utf8')).endsWith('LAST LINE'));
  await page.keyboard.press('Escape');
  await answer.getByRole('link',{name:'Giant.md'}).click();
  await reader.getByText(/超过 8 MiB 下载上限/).waitFor();
  assert.equal(await reader.getByRole('link',{name:'下载原文'}).count(),0);
  await page.keyboard.press('Escape');
  const routes = await page.evaluate(() => {
    const shared = 'docs/Decision.md';
    const doc = id => ({path:'handoffs/'+id.repeat(32)+'/'+shared,project_path:shared,ref:'dispatch:'+id+'/'+shared});
    const turn = {run_id:'parent',dispatches:[{child_run_id:'child-a',artifacts:[doc('a')]},
      {child_run_id:'child-b',artifacts:[doc('b')]}]};
    const element = document.createElement('div');
    element.innerHTML = StudioMarkdown.render('见 docs/Decision.md 和 `dispatch:a/docs/Decision.md`。',turn);
    return [...element.querySelectorAll('a')].map(a => [a.textContent,a.dataset.mdRun]);
  });
  assert.deepEqual(routes,[['dispatch:a/docs/Decision.md','child-a']]);
  const user = page.locator('.chat-turn').last().locator('.chat-user a[data-md-path]');
  await user.click();
  await reader.getByRole('heading',{name:'Published edition'}).waitFor();
  await page.setViewportSize({width:375,height:750});
  const bounds = await reader.boundingBox();
  assert(bounds.width <= 375 && bounds.x >= 0 && bounds.x + bounds.width <= 375);
  await reader.getByRole('button',{name:'关闭文件预览'}).click();
  await first.getByRole('link',{name:'查看本轮',exact:true}).click();
  await page.locator('#tab-artifacts').click();
  const selectedFile = page.locator('.artifact-preview');
  await page.locator('button[data-file$="/Giant.md"]').click();
  await selectedFile.getByText(/超过 8 MiB 下载上限/).waitFor();
  const long = page.locator('button[data-file$="/Long.md"]');
  let oldArrived, releaseOld;
  const ready = new Promise(resolve => oldArrived = resolve);
  const unblock = new Promise(resolve => releaseOld = resolve);
  const isLong = response => response.url().includes('/artifact?') && new URL(response.url()).searchParams.get('path')?.endsWith('/Long.md');
  let once = false;
  await page.route('**/artifact?**',async route => {
    if (once || !isLong(route.request())) return route.fallback();
    once = true;
    oldArrived();
    await unblock;
    return route.fulfill({json:{path:'agents/worker/Long.md',content:'# Earlier revision',truncated:false}});
  });
  await long.click();
  await ready;
  await page.locator('button[data-file$="/Giant.md"]').click();
  await long.click();
  await selectedFile.getByRole('heading',{name:'Long bulletin'}).waitFor();
  const late = page.waitForResponse(isLong);
  releaseOld();
  await (await late).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal(await selectedFile.getByRole('heading',{name:'Long bulletin'}).isVisible(),true);
  // The head can be a cancelled false start. Open both real artifact versions
  // through the API, including an unchanged file absent from a turn's delta.
  const latestRun = url.split('#run/')[1].split('/')[0];
  const detailPath = '**/api/runs/'+latestRun;
  const versions = async route => {
    const response = await route.fetch(), detail = await response.json();
    const thread = detail.conversation, [earlier,later] = thread.turns;
    const path = 'docs/Report.md';
    const old = {run_id:earlier.run_id,path,ref:'agents/worker/'+path};
    const revised = {run_id:later.run_id,path,ref:'handoffs/'+'b'.repeat(32)+'/'+path};
    const inherited = {run_id:earlier.run_id,path:'Appendix.md',ref:'handoffs/'+'a'.repeat(32)+'/Appendix.md'};
    earlier.published_artifacts = [old,inherited];
    later.published_artifacts = [revised];
    thread.turns.push({run_id:'cancelled-head',user:'旧误派',status:'cancelled',events:[],artifacts:[],published_artifacts:[]});
    const row = {kind:'group_message',parent_run_id:'cancelled-head',disposition:'reply',role:'worker',
      question:'这份文件如何使用？',after_turn:true,at:'2027-01-01T00:00:00Z'};
    thread.messages = [
      {...row,request_id:'pinned-ref',text:'第一版是 `Report.md`，附件是 Appendix.md。',cited_artifacts:[old]},
      {...row,request_id:'ambiguous-ref',text:'两次交付均有 [报告](docs/Report.md) 和 `Report.md`。'},
    ];
    await route.fulfill({json:detail});
  };
  await page.route(detailPath,versions);
  await page.goto(url);
  await page.reload();
  const pinned = page.locator('[data-key="group-reply-pinned-ref"]');
  await pinned.getByRole('link',{name:'Report.md',exact:true}).click();
  await reader.getByRole('heading',{name:'Published edition'}).waitFor();
  await page.keyboard.press('Escape');
  await pinned.getByRole('link',{name:'Appendix.md',exact:true}).click();
  await reader.getByRole('heading',{name:'Original appendix'}).waitFor();
  await page.keyboard.press('Escape');
  const multiple = page.locator('[data-key="group-reply-ambiguous-ref"]');
  assert.equal(await multiple.getByRole('link').count(),0);
  await multiple.getByRole('button',{name:'报告',exact:true}).click();
  const oldVersion = reader.getByRole('link',{name:'第 1 轮 · docs/Report.md'});
  const newVersion = reader.getByRole('link',{name:'第 2 轮 · docs/Report.md'});
  await oldVersion.click();
  await reader.getByRole('heading',{name:'Published edition'}).waitFor();
  await reader.getByRole('button',{name:'返回'}).click();
  await newVersion.click();
  await reader.getByRole('heading',{name:'Revised publication'}).waitFor();
  await page.keyboard.press('Escape');
  await page.unroute(detailPath,versions);
  // A process started before the upload route existed still serves text chat.
  await page.route('**/attachments.js', route=>route.fulfill({status:404,body:''}));
  await page.goto(url);
  await page.reload();
  const reply = page.locator('.reply-form');
  await reply.locator('textarea[name=message]').fill('继续之前的纯文本任务');
  assert.equal(await reply.locator('[data-pick-uploads]').count(),0);
  await page.route('**/api/groups/*/messages', route => route.request().method()==='POST'
    ? route.fulfill({status:400,json:{error:'已捕获请求'}}) : route.fallback());
  const outgoing = page.waitForRequest(r=>r.method()==='POST' && new URL(r.url()).pathname.endsWith('/messages'));
  await reply.locator('[type=submit]').click();
  const payload = (await outgoing).postDataJSON();
  assert.equal(payload.text,'继续之前的纯文本任务');
  assert.equal(Object.hasOwn(payload.options,'attachments'),false);
  assert.deepEqual(errors, []);
  assert.deepEqual(remote, []);
  console.log('Chat Markdown, publication history, nested links and mobile reader passed.');
}).catch(error => { console.error(error); process.exitCode=1; });
