const assert = require('node:assert/strict');
const { withFixture } = require('./browser_fixture.cjs');

async function verify(page, url) {
  const source = 'studio-routing';
  const head = 'studio-later-message';
  const child = 'studio-maintenance';
  const dm = 'studio-caretaker-room';
  const group = 'group-maintenance';
  const file = {name:'maintenance.md', project_path:'docs/maintenance.md',
    path:'handoffs/0123456789abcdef0123456789abcdef/docs/maintenance.md', size:40};
  const dispatch = {
    dispatch_id:'check', target_kind:'role', target_id:'caretaker', target_name:'小维',
    objective:'检查电脑开发环境，交付维护清单', status:'queued',
  };
  const groupDispatch = {
    dispatch_id:'review', target_kind:'group', target_id:group, target_name:'维护复核组',
    objective:'复核维护清单', status:'failed', child_run_id:'studio-failed-review',
    conversation_id:group, error:'复核输入缺失 <input>', artifacts:[],
  };
  const errors = [], replies = [];
  let current = source, detailReads = 0;
  page.on('pageerror', error=>errors.push(error.message));
  const summary = (id, title, status='completed')=>({id, title, status, mode:'single'});
  await page.route('**/api/**', async route=>{
    const api = new URL(route.request().url());
    const ok = data=>route.fulfill({json:data});
    if (api.pathname==='/api/bootstrap') return ok({
      provider_ready:true, model:'fixture', csrf_token:'local', conversation_roles:[],
      custom_profiles:[{id:'caretaker',name:'小维'}],
    });
    if (api.pathname==='/api/runs') return ok({runs:[]});
    if (api.pathname==='/api/superagent') return ok({conversation_id:source,head_run_id:current});
    if (api.pathname==='/api/runs/'+current) {
      detailReads++;
      const turns = [{run_id:source,user:'检查项目',answer:'已安排检查。',status:'completed',dispatches:[dispatch,groupDispatch]}];
      if (current===head) turns.push({run_id:head,user:'刚才是谁在处理？',answer:'小维。',status:'completed'});
      return ok({run:summary(current,'工作室助手'),conversation:{superagent:true,turns}});
    }
    if (api.pathname==='/api/runs/'+child+'/questions/choice') {
      assert.equal(route.request().method(),'POST');
      replies.push(route.request().postDataJSON());
      Object.assign(dispatch,{status:'unverified',pending_questions:[],
        answer:'可用磁盘空间已核对。<input>',answer_truncated:true,artifacts:[file]});
      return ok({});
    }
    if (api.pathname==='/api/runs/'+child+'/excerpt') {
      assert.equal(api.searchParams.get('path'),file.path);
      return ok({content:'维护结果由小维回传。',truncated:false});
    }
    if (api.pathname==='/api/runs/'+dm || api.pathname==='/api/runs/'+child) {
      const turn = {run_id:child,recipient:'caretaker',user:dispatch.objective,answer:dispatch.answer,status:'unverified',mode:'single'};
      return ok({run:summary(api.pathname.split('/').pop(),'维护任务','unverified'),tasks:[],events:[],artifacts:[],
        diagnostics:{issues:[]},conversation:{id:dm,head_run_id:child,kind:'dm',
        recipient:'caretaker',members:['caretaker'],can_continue:true,turns:[turn]}});
    }
    return route.continue();
  });
  await page.goto(url+'#assistant');
  const sourceTurn=page.locator('[data-key="assistant-turn-'+source+'"]');
  const card=sourceTurn.getByRole('region',{name:'小维的任务'});
  const status=card.getByRole('status');
  const input=page.getByLabel('给工作室助手发消息');
  await status.getByText('排队中').waitFor();
  assert.equal(await card.getByRole('link').count(),0);
  assert(await input.isEnabled());
  const failed=sourceTurn.getByRole('region',{name:'维护复核组的任务'});
  assert.equal(await failed.getByRole('status').innerText(),'失败');
  assert.equal(await failed.locator('.dispatch-error').innerText(),'复核输入缺失 <input>');
  assert.equal(await failed.locator('input').count(),0);
  assert.equal(await failed.getByRole('link',{name:'进入群聊'}).getAttribute('href'),'#run/'+group+'/chat?latest=1');

  // Both assistant turns are already terminal. The older child continues.
  current=head;
  Object.assign(dispatch,{status:'running',child_run_id:child,conversation_id:dm});
  await page.getByText('刚才是谁在处理？',{exact:true}).waitFor();
  await status.getByText('运行中').waitFor();
  const stable=await card.elementHandle();
  await input.fill('后续工作草稿');
  const priorReads=detailReads;
  Object.assign(dispatch,{status:'waiting_user',pending_questions:[
    {request_id:'choice',role:'caretaker',text:'需要检查 Python 吗？'}]});
  const question=card.locator('form[data-question="choice"]');
  await question.waitFor();
  assert(detailReads>priorReads);
  assert(await card.evaluate((node,previous)=>node===previous,stable));
  const answer=question.locator('textarea');
  await answer.fill('需要，检查解释器路径。');
  await page.evaluate(()=>studioAssistant.refresh(true));
  assert.equal(await answer.inputValue(),'需要，检查解释器路径。');
  await question.getByRole('button',{name:'答复并继续'}).click();
  await status.getByText('已结束 · 未独立验收').waitFor();
  assert.deepEqual(replies,[{answer:'需要，检查解释器路径。'}]);
  assert.equal(await input.inputValue(),'后续工作草稿');
  assert.equal(await card.locator('.dispatch-answer input').count(),0);
  assert.equal(await card.locator('.dispatch-answer').innerText(),'可用磁盘空间已核对。<input>');
  await card.getByText(/此处显示答复节选/).waitFor();
  await card.locator('a.turn-file').getByText('docs/maintenance.md').waitFor();
  assert.equal(await card.locator('a.turn-file').getAttribute('href'),
    '#run/'+child+'/artifacts?'+new URLSearchParams({path:file.path}));
  await card.locator('details[data-activity-kind=file] summary').click();
  await card.getByText('维护结果由小维回传。').waitFor();
  await page.setViewportSize({width:375,height:780});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const finishedReads=detailReads;
  await page.evaluate(()=>studioAssistant.refresh());
  assert.equal(detailReads,finishedReads);
  await card.getByRole('link',{name:'进入私聊'}).click();
  await page.waitForURL('**/#run/'+child+'/chat?latest=1');
  await page.locator('.reply-form').waitFor();
  assert.deepEqual(errors,[]);
}

withFixture('serve_room_fixture',verify)
  .then(()=>console.log('Superagent dispatch replies, child links, questions and inline delivery passed.'))
  .catch(error=>{console.error(error);process.exitCode=1;});
