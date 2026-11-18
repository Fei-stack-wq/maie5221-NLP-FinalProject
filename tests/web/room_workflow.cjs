// Run from the repository root with the project Python and Playwright Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { withFixture } = require('./browser_fixture.cjs');

async function room(page, url) {
  const requests = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && request.url() === url + 'api/runs') requests.push(request.postDataJSON());
  });
  await page.goto(url + '#chats');
  await page.getByText(/工作室角色 ·/).click();
  const profile = page.locator('.profile-form');
  await profile.locator('[name="id"]').fill('lead');
  await profile.locator('[name="name"]').fill('项目负责人');
  await profile.locator('[name="prompt"]').fill('负责协调并委派独立评审。');
  await profile.getByText('保存角色').click();
  await page.locator('.role-library code').getByText('lead', {exact: true}).waitFor();
  await page.getByText('新建群聊', {exact: true}).click();
  const form = page.locator('#dialog-form form');
  const picked = page.waitForResponse(r => r.url() === url + 'api/workspaces/pick');
  await form.getByRole('button', {name:'选择文件夹'}).click();
  const project = (await (await picked).json()).path;
  assert.equal(await form.locator('[name="workspace_path"]').inputValue(), project);
  for (const member of await form.locator('[name="member"]').all()) await member.setChecked(false);
  await form.locator('[name="member"][value="reviewer"]').check();
  await form.locator('[name="member"][value="lead"]').check();
  await form.locator('[name="recipient"]').selectOption('lead');
  assert.equal(await form.locator('[name="dispatch"]').inputValue(), 'team');
  await form.locator('[name="objective"]').fill('请协作写一份模拟说明。');
  await form.getByRole('button', {name:'发送'}).click();
  const human = page.locator('.human-form');
  await human.locator('textarea').fill('两年');
  await human.getByRole('button', {name:'答复并继续'}).click();
  const reply = page.locator('.reply-form');
  await reply.locator('textarea[name="message"]').fill('接着未完成的文件修订。');
  assert.equal(requests[0].mode, 'team');
  assert.equal(requests[0].workspace_path, project);
  assert.deepEqual(requests[0].conversation.members.sort(), ['lead','reviewer']);
  assert.equal(requests[0].conversation.profiles[0].prompt, '负责协调并委派独立评审。');
  await reply.getByText('本轮选项与预算').click();
  await reply.locator('[name="base"]').selectOption('failed');
  await reply.locator('[name="max_total_tokens"]').fill('420000');
  await reply.locator('[name="acceptance_files"]').fill('working.md');
  await reply.locator('[name="dispatch"]').selectOption('single');
  assert.equal(await reply.locator('[name="base"]').inputValue(), 'failed');
  assert.equal(await reply.locator('[name="max_total_tokens"]').inputValue(), '420000');
  const created = page.waitForResponse(r => r.url() === url + 'api/runs' && r.request().method() === 'POST');
  await reply.getByRole('button', {name:'发送'}).click();
  const response = await created;
  assert.equal(response.status(), 202);
  const latest = (await response.json()).run.id;
  await page.waitForURL('**/#run/' + latest + '/chat');
  await page.getByText('已接续并完成修订。').waitFor();
  assert.equal(requests[1].mode, 'single');
  assert.equal(requests[1].limits.max_total_tokens, 420000);
  assert.equal(requests[1].conversation.recover_failed, true);
  await page.locator('.thread-workspace code').getByText(project, {exact:true}).waitFor();
  const firstTurn = page.locator('.chat-turn').first();
  await firstTurn.getByText('已交接给负责人').waitFor();
  const receiptFile = firstTurn.locator('.member-files a').filter({hasText:'working.md'});
  await receiptFile.click();
  assert.match(await page.locator('.artifact-button.active').getAttribute('data-file'), /^handoffs\/[a-f0-9]{32}\/working\.md$/);
  await page.locator('.file-content.prose').getByText('Unpublished working version of the proposal.').waitFor();
  await page.goBack();
  await page.waitForURL('**/#run/' + latest + '/chat');
  const files = firstTurn.locator('.chat-agent > .turn-files');
  await files.evaluate(details => details.open = true);
  const dbLink = files.locator('a[download]').filter({hasText:'agents/lead/reservations.db'});
  const dbDownload = page.waitForEvent('download');
  await dbLink.click();
  const data = await fs.readFile(await (await dbDownload).path());
  assert.equal(data.subarray(0,16).toString(), 'SQLite format 3\0');
  const previewPage = page.waitForEvent('popup');
  await files.locator('a').filter({hasText:'agents/lead/a.html'}).click();
  const preview = await previewPage;
  await preview.frameLocator('iframe').getByRole('heading',{name:'Room fixture preview'}).waitFor();
  await preview.close();
  const screenshotPopup = page.waitForEvent('popup');
  await files.locator('a').filter({hasText:'agents/lead/room-screen.png'}).click();
  const screenshot = await screenshotPopup;
  await screenshot.locator('img').waitFor();
  assert.deepEqual(await screenshot.locator('img').evaluate(img => [img.naturalWidth, img.naturalHeight]), [41,29]);
  await screenshot.close();
  const historicLink = files.locator('a').filter({hasText:'agents/lead/working.md'});
  await historicLink.click();
  await page.locator('.file-content.prose').getByText('Unpublished working version of the proposal.').waitFor();
  assert.equal(await page.locator('.artifact-button.active').getAttribute('data-file'), 'agents/lead/working.md');
  assert.equal(await page.locator('.file-content').getByText('follow-up revision', {exact:false}).count(),0);
  await page.goBack();
  await page.waitForURL('**/#run/' + latest + '/chat');
  const delivery = await page.request.get(url + 'api/runs/' + latest + '/artifact?path=agents/lead/working.md');
  assert.match((await delivery.json()).content, /Unpublished working version[\s\S]*follow-up revision/);
  const exportLink = page.getByRole('link', {name:'导出完整会话'});
  const download = page.waitForEvent('download');
  await exportLink.click();
  assert.equal((await download).suggestedFilename(), latest + '-room.zip');
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
  for (const outcome of outcomes) if (outcome.status === 'rejected') throw outcome.reason;
})().catch(error => { console.error(error); process.exitCode = 1; });
