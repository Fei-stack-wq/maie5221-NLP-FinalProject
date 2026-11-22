// Run from the repository root with the project Python and Playwright Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { withFixture } = require('./browser_fixture.cjs');

// Exercise the actual form and JSON request without consuming the scripted
// provider responses reserved for the full conversation below.
async function checkSourceReviewRequest(page, url, form, requests) {
  const field = form.locator('[name="required_source_review_paths"]');
  await field.fill('findings.md, brief.html');
  const endpoint = url + 'api/groups/*/messages';
  const intercept = async route => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 422, json: {error: 'Source review form inspected'} });
    } else {
      await route.fallback();
    }
  };
  await page.route(endpoint, intercept);
  try {
    await form.getByRole('button', {name:'发送'}).click();
    await form.getByText('Source review form inspected').waitFor();
    assert.deepEqual(requests.pop().options.required_source_review_paths, ['findings.md', 'brief.html']);
  } finally {
    await page.unroute(endpoint, intercept);
  }
}

async function room(page, url) {
  const requests = [];
  const channels = [];
  const screenshotRequests = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method()==='POST' && request.url().endsWith('/messages')) requests.push(request.postDataJSON());
    if (request.method()==='POST' && request.url().endsWith('/api/directory/channels')) channels.push(request.postDataJSON());
    const requested = new URL(request.url());
    if (requested.pathname.endsWith('/preview') && requested.searchParams.get('path')?.endsWith('room-screen.png')) {
      screenshotRequests.push(request.url());
    }
  });
  await page.goto(url + '#chats');
  await page.getByRole('link',{name:'角色库',exact:true}).click();
  await page.getByRole('button',{name:'创建角色'}).click();
  const profile = page.locator('.role-editor');
  await profile.getByText('自定义角色 ID（选填）').click();
  await profile.locator('[name="id"]').fill('lead');
  await profile.locator('[name="name"]').fill('项目负责人');
  await profile.locator('[name="prompt"]').fill('负责协调并委派独立评审。');
  await profile.getByText('保存角色').click();
  await page.getByRole('heading',{name:'项目负责人'}).waitFor();
  await page.getByRole('link',{name:'最近对话',exact:true}).click();
  await page.getByText('新建群聊', {exact: true}).click();
  const form = page.locator('#dialog-form form');
  const picked = page.waitForResponse(r => r.url() === url + 'api/workspaces/pick');
  await form.getByRole('button', {name:'选择文件夹'}).click();
  const project = (await (await picked).json()).path;
  assert.equal(await form.locator('[name="workspace_path"]').inputValue(), project);
  for (const member of await form.locator('[name="member"]').all()) await member.setChecked(false);
  await form.locator('[name="member"][value="reviewer"]').check();
  await form.locator('[name="member"][value="worker"]').check();
  await form.locator('[name="member"][value="lead"]').check();
  await form.locator('[name="recipient"]').selectOption('lead');
  assert.equal(await form.locator('[name="dispatch"]').inputValue(), 'team');
  const thinking = form.getByRole('combobox',{name:'本轮模型思考'});
  assert(await thinking.isVisible());
  assert.equal(await thinking.inputValue(), 'enabled');
  await thinking.selectOption('disabled');
  await form.getByRole('button',{name:'选项',exact:true}).click();
  assert.equal(await form.locator('[name="interaction_mode"]').inputValue(), 'autonomous');
  await form.locator('[name="interaction_mode"]').selectOption('interactive');
  assert.equal(await form.locator('[name="max_total_tokens"]').inputValue(), '0');
  assert.equal(await form.locator('[name="max_steps"]').inputValue(), '0');
  await form.locator('[name="max_steps"]').fill('37');
  await form.locator('[name="max_total_tokens"]').fill('1000000');
  assert.equal(await form.locator('[name="intent"]').inputValue(),'auto');
  await form.locator('[name="intent"]').selectOption('work');
  await form.locator('[name="objective"]').fill('请协作写一份模拟说明。');
  await checkSourceReviewRequest(page, url, form, requests);
  await form.locator('[name="required_source_review_paths"]').fill('');
  await form.getByRole('button', {name:'发送'}).click();
  const human = page.locator('.human-form');
  const underway = page.locator('.chat-turn').first();
  const liveAssignment = underway.locator('.room-message[data-kind="assignment"]');
  const liveMaterial = liveAssignment.locator('details.room-material');
  await liveMaterial.locator(':scope > summary').click();
  assert.equal(await liveMaterial.locator('.prose').getByText(/材料末尾。/).isVisible(), true);
  assert.equal(await underway.locator('[data-kind="member_report"]').count(), 0);
  await page.waitForResponse(response => /\/api\/runs\/[^/?]+$/.test(new URL(response.url()).pathname));
  assert.equal(await liveMaterial.evaluate(node=>node.open), true);
  await human.locator('textarea').fill('两年');
  await human.getByRole('button', {name:'答复并继续'}).click();
  const reply = page.locator('.reply-form');
  await reply.locator('textarea[name="message"]').fill('接着未完成的文件修订。');
  assert.equal(requests[0].options.mode, 'team');
  assert.equal(requests[0].options.thinking, 'disabled');
  assert.equal(await reply.getByRole('combobox',{name:'本轮模型思考'}).inputValue(), 'enabled');
  assert.equal(requests[0].options.interaction_mode, 'interactive');
  assert.equal(channels[0].workspace_path, project);
  assert.equal(channels[0].pinned, false);
  const directory = await (await page.request.get(url+'api/directory/chats')).json();
  assert.equal(directory.channels.length,0);
  assert.equal(directory.recent.length,1);
  assert.deepEqual(channels[0].members.sort(), ['lead','reviewer','worker']);
  assert.equal(channels[0].profiles, undefined);
  const detail=await (await page.request.get(url+'api/runs/'+new URL(page.url()).hash.split('/')[1])).json();
  assert.equal(detail.conversation.profiles.find(p=>p.id==='lead').prompt, '负责协调并委派独立评审。');
  await page.waitForFunction(()=>document.querySelector('.reply-form [name=intent] option[value=work]'));
  await reply.locator('[name=intent]').selectOption('work');
  await reply.getByText('本轮选项与预算').click();
  await reply.locator('[name="base"]').selectOption('failed');
  assert.equal(requests[0].options.limits.max_total_tokens, 1000000);
  assert.equal(await reply.locator('[name="max_total_tokens"]').inputValue(), '1000000');
  assert.equal(await reply.locator('[name="max_steps"]').inputValue(), '37');
  await reply.getByRole('button',{name:'不限轮数'}).click();
  await reply.getByRole('button',{name:'不限累计 token'}).click();
  await reply.locator('[name="acceptance_files"]').fill('working.md, room-screen.png');
  await reply.locator('[name="message"]').fill('请继续修订。');
  await checkSourceReviewRequest(page, url, reply, requests);
  await reply.locator('[name="dispatch"]').selectOption('single');
  assert.equal(await reply.locator('[name="required_source_review_paths"]').isVisible(), false);
  assert.equal(await reply.locator('[name="base"]').inputValue(), 'failed');
  assert.equal(await reply.locator('[name="max_total_tokens"]').inputValue(), '0');
  const created = page.waitForResponse(r => r.url().endsWith('/messages') && r.request().method() === 'POST');
  await reply.getByRole('button', {name:'发送'}).click();
  const response = await created;
  assert.ok(response.ok());
  const latest = (await response.json()).run.id;
  await page.waitForURL('**/#run/' + latest + '/chat');
  await page.getByText('已接续并完成修订。').waitFor();
  assert.equal(requests[1].options.mode, 'single');
  assert.equal(requests[1].options.required_source_review_paths, undefined);
  assert.equal(requests[1].options.limits.max_steps, 0);
  assert.equal(requests[1].options.limits.max_total_tokens, 0);
  assert.equal(requests[1].options.conversation.recover_failed, true);
  await page.locator('.thread-workspace code').getByText(project, {exact:true}).waitFor();
  // Ordinary free-form group messages use the room-scoped model without another work turn.
  const composer = page.locator('.reply-form');
  await composer.locator('textarea[name="message"]').fill('给我讲讲刚刚交过来的东西');
  const answered = page.waitForResponse(r => r.url().endsWith('/messages') && r.request().method() === 'POST');
  await composer.getByRole('button', {name:'发送'}).click();
  const snapshot = await (await answered).json();
  assert.equal(snapshot.message.disposition,'reply');
  assert.equal(snapshot.message.parent_run_id,latest);
  const recap = page.locator('[data-kind="group_message"]').filter({hasText:'这次交付了 working.md 和截图。'});
  await recap.waitFor();
  await recap.getByRole('button',{name:'working.md'}).click();
  const versions=page.locator('#markdown-reader');
  assert.equal(await versions.getByRole('link',{name:'第 2 轮 · working.md'}).getAttribute('data-md-run'),latest);
  await versions.getByRole('link',{name:'第 1 轮 · working.md'}).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.chat-turn').count(), 2);
  const firstTurn = page.locator('.chat-turn').first();
  const assignment = firstTurn.locator('.room-message[data-kind="assignment"]');
  assert.equal(await assignment.locator('.speaker a').innerText(), '项目负责人');
  const assignmentLine = await assignment.locator('.room-line').innerText();
  assert.match(assignmentLine, /^@[^@]+问清时间范围，再把结果记下来。$/);
  assert.equal(await page.locator('.chat-update-user').getByText('请协作写一份模拟说明。',{exact:true}).count(),1);
  assert.equal(await firstTurn.locator('.chat-user').count(),0);
  assert.equal(await firstTurn.locator('[data-kind="goal"]').count(), 0);
  const material = assignment.locator('details.room-material');
  assert.equal(await material.evaluate(node=>node.open), false);
  await material.locator(':scope > summary').click();
  await material.locator('.prose').getByText(/材料末尾。/).waitFor();
  assert.equal(await material.evaluate(node=>node.open), true);
  const report = firstTurn.locator('.room-message[data-kind="member_report"]');
  assert.match(await report.locator('.room-mention').innerText(), /@项目负责人/);
  assert.match(await report.locator('.room-line').innerText(), /已记录两年期限。/);
  assert.equal(await report.locator('.room-disposition').innerText(), '已交接');
  const reportDetails = report.locator('details.room-material');
  assert.equal(await reportDetails.evaluate(node=>node.open), false);
  assert.equal(await firstTurn.locator('details.room-records').evaluate(node=>node.open), false);
  await reportDetails.locator(':scope > summary').click();
  const receiptFile = firstTurn.locator('.member-files a').filter({hasText:'working.md'});
  assert.match(await receiptFile.getAttribute('data-md-path'), /^handoffs\/[a-f0-9]{32}\/working\.md$/);
  await receiptFile.click();
  const reader = page.getByRole('dialog', {name:'working.md'});
  await reader.getByText('Unpublished working version of the proposal.').waitFor();
  await reader.getByRole('button', {name:'关闭文件预览'}).click();
  await page.waitForURL('**/#run/' + latest + '/chat');
  const files = firstTurn.locator('.chat-agent > .turn-files');
  await files.evaluate(details => details.open = true);
  assert.deepEqual(await files.locator('a.turn-file span').allTextContents(), ['agents/lead/working.md']);
  const chatURL = page.url();
  await page.getByRole('tab', {name:/产物/}).click();
  await page.locator('[data-artifact-turn]').selectOption(detail.run.id);
  const projectFiles = page.locator('details.artifact-project').filter({hasText:'其他项目文件'});
  await projectFiles.locator('summary').click();
  await projectFiles.getByRole('button', {name:/reservations.db/}).click();
  const [dbDownload] = await Promise.all([
    page.waitForEvent('download'), page.getByRole('link', {name:'下载数据库'}).click()
  ]);
  const data = await fs.readFile(await dbDownload.path());
  assert.equal(data.subarray(0,16).toString(), 'SQLite format 3\0');
  await projectFiles.getByRole('button', {name:/a\.html/}).click();
  const [preview] = await Promise.all([
    page.waitForEvent('popup'), page.getByRole('link', {name:'打开交互预览'}).click()
  ]);
  await preview.frameLocator('iframe').getByRole('heading',{name:'Room fixture preview'}).waitFor();
  await preview.close();
  await projectFiles.getByRole('button', {name:/room-screen.png/}).click();
  const projectScreenshot = page.getByRole('img', {name:'agents/lead/room-screen.png'});
  await projectScreenshot.evaluate(image=>image.decode());
  assert.deepEqual(await projectScreenshot.evaluate(img=>[img.naturalWidth,img.naturalHeight]),[41,29]);
  assert.equal(screenshotRequests.length,1);
  assert(new URL(screenshotRequests[0]).pathname.includes('/'+detail.run.id+'/'));

  // The follow-up explicitly requested the screenshot. Only that turn offers the lazy chat thumbnail.
  await page.goto(chatURL);
  const deliveryFiles = page.locator('.chat-turn').last().locator('.chat-agent > .turn-files');
  await deliveryFiles.locator(':scope > summary').click();
  const disclosure = deliveryFiles.locator('details[data-activity-kind="image"]');
  const summary = disclosure.locator('summary');
  await summary.waitFor();
  assert.equal(await disclosure.locator('img').count(), 0);
  const screenshotURL = new URL(await deliveryFiles.locator('a').filter({hasText:'agents/lead/room-screen.png'}).getAttribute('href'), url).href;
  const deliveryScreenshotRequests = () => screenshotRequests.filter(source => source === screenshotURL);
  assert.deepEqual(deliveryScreenshotRequests(), []);
  assert.equal(await deliveryFiles.locator('details[data-path$=".html"]').count(), 0);
  await page.route(screenshotURL, route => route.fulfill({status:403, body:'未获授权'}), {times:1});
  await summary.click();
  const retry = disclosure.getByRole('button', {name:'截图加载失败 · 重试'});
  await retry.click();
  const thumbnail = disclosure.getByRole('img', {name:'截图 room-screen.png'});
  await thumbnail.evaluate(image => image.decode());
  assert.deepEqual(await thumbnail.evaluate(img => [img.naturalWidth, img.naturalHeight]), [41,29]);
  assert.deepEqual(deliveryScreenshotRequests(), [screenshotURL, screenshotURL]);
  await summary.click();
  await summary.click();
  assert.equal(await thumbnail.isVisible(), true);
  assert.equal(deliveryScreenshotRequests().length, 2);
  const previousSize = page.viewportSize();
  await page.setViewportSize({width:375, height:800});
  const thumbnailBounds = await thumbnail.boundingBox();
  assert(0 < thumbnailBounds.width && thumbnailBounds.x >= 0 && thumbnailBounds.x + thumbnailBounds.width <= 375);
  await page.setViewportSize(previousSize);
  const screenshotPopup = page.waitForEvent('popup');
  await deliveryFiles.locator('a').filter({hasText:'agents/lead/room-screen.png'}).click();
  const screenshot = await screenshotPopup;
  await screenshot.locator('img').waitFor();
  assert.deepEqual(await screenshot.locator('img').evaluate(img => [img.naturalWidth, img.naturalHeight]), [41,29]);
  await screenshot.close();
  await files.locator(':scope > summary').click();
  const historicLink = files.locator('a').filter({hasText:'agents/lead/working.md'});
  await historicLink.click();
  assert.equal(await historicLink.getAttribute('data-md-path'), 'agents/lead/working.md');
  await reader.getByText('Unpublished working version of the proposal.').waitFor();
  assert.equal(await reader.getByText('follow-up revision', {exact:false}).count(),0);
  await reader.getByRole('button', {name:'关闭文件预览'}).click();
  await page.waitForURL('**/#run/' + latest + '/chat');
  const delivery = await page.request.get(url + 'api/runs/' + latest + '/artifact?path=agents/lead/working.md');
  assert.match((await delivery.json()).content, /Unpublished working version[\s\S]*follow-up revision/);
  const exportLink = page.getByRole('link', {name:'导出完整会话'});
  const download = page.waitForEvent('download');
  await exportLink.click();
  assert.equal((await download).suggestedFilename(), latest + '-room.zip');
  // Old material is open when a burst of later notes pushes it beyond the preview window.
  const token = (await (await page.request.get(url+'api/bootstrap')).json()).csrf_token;
  const origin = new URL(url).origin;
  await material.locator(':scope > summary').click();
  const audit = url+'api/runs/'+detail.run.id+'/review';
  for (let i=0; i<51; i++) {
    const response = await page.request.post(audit, {headers:{Origin:origin,'X-Studio-Token':token},
      data:{verdict:'passed',note:'追加复查记录 '+i}});
    assert.equal(response.status(),200);
  }
  const gap = firstTurn.getByRole('button',{name:'查看此处更早的消息'});
  await gap.waitFor();
  assert.equal(await material.evaluate(node=>node.open),true);
  await gap.click();
  await firstTurn.getByText('追加复查记录 15',{exact:true}).waitFor();
  await gap.click();
  await firstTurn.getByText('追加复查记录 0',{exact:true}).waitFor();
  assert.equal(await firstTurn.locator('[data-kind="human_review"]').count(),51);
  assert.equal(await material.evaluate(node=>node.open),true);
  assert.deepEqual(errors, []);
  console.log('Persistent role, team assignment, human reply, failed workspace recovery and budget, download passed.');
}

async function stability(page, url) {
  await page.goto(url + '#run/fixture');
  const results = page.locator('#regression-results[data-finished="true"]');
  await results.waitFor({timeout:35000});
  assert.equal(await results.getAttribute('data-passed'), 'true', await results.textContent());
  console.log((await results.textContent()).split('\n').length + ' existing Studio browser regressions passed.');
}

(async () => {
  const outcomes = await Promise.allSettled([
    withFixture('serve_stability_fixture', stability, ['--port', '0']),
    withFixture('serve_room_fixture', room)
  ]);
  const failures = outcomes.filter(outcome=>outcome.status==='rejected');
  if (failures.length) throw new AggregateError(failures.map(outcome=>outcome.reason),'Room browser regressions failed');
})().catch(error => { console.error(error); process.exitCode = 1; });
