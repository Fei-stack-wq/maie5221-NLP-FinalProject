// Test the real web assets with deterministic HTTP replies, including a lost
// POST response and another tab advancing the shared conversation.
const assert = require('node:assert/strict');
const { withFixture } = require('./browser_fixture.cjs');

async function verify(page,url) {
 const requests=[], errors=[];
 const model='deepseek-flash';
 const group='studio-empty-room', profile='caretaker', initial='studio-home-1';
 const names=['电脑管家','验证员'];
 const groupThread={id:group,title:'电脑维护组',kind:'group',members:[profile,'reviewer'],lead:'reviewer',recipient:'reviewer',
  profiles:[{id:profile,name:names[0]},{id:'reviewer',name:names[1]}],head_run_id:group,can_continue:true,turns:[]};
 let head=null, status='running', workspace='', answer=null;
 const turns=[];
 const summary=(id,status)=>({id,title:'工作室助手',mode:'single',status,created_at:'2026-11-19T02:00:00Z'});
 page.on('pageerror',err=>errors.push(err.message));
 await page.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  const ok=data=>route.fulfill({json:data});
  if(path==='/api/bootstrap')return ok({provider_ready:true,model,csrf_token:'local',conversation_roles:[],custom_profiles:[]});
  if(path==='/api/profiles')return ok({profiles:[{id:profile,name:names[0],prompt:'负责本机维护。'}]});
  if(path==='/api/runs'&&route.request().method()==='POST') {
   const body=route.request().postDataJSON();
   requests.push(body);
   if(body.conversation.parent_run_id!==groupThread.head_run_id) {
    return route.fulfill({status:409,json:{code:'thread_conflict',error:'群里已有新消息'}});
   }
   const runId='group-message-'+(groupThread.turns.length+1);
   groupThread.turns.push({run_id:runId,user:body.objective,mode:body.mode,recipient:body.conversation.recipient,
    intent:body.conversation.intent,status:'completed',answer:'诊断已记录。',events:[],artifacts:[]});
   groupThread.head_run_id=runId;
   return route.fulfill({status:202,json:{run:summary(runId,'completed')}});
  }
  if(path==='/api/runs')return ok({runs:[]});
  if(path==='/api/superagent')return ok({conversation_id:head?initial:null,head_run_id:head});
  if(path==='/api/superagent/messages') {
   const data=route.request().postDataJSON();requests.push(data);
   if(requests.length===1)return route.abort();
   head=initial;workspace=data.workspace_path || workspace;
   if(!turns.length)turns.push({run_id:head,user:data.message,recipient:'superagent',events:[],artifacts:[]});
   return route.fulfill({status:202,json:{run:summary(head,status)}});
  }
  if(path==='/api/runs/'+initial) {
   const turn={...turns[0],status};
   if(status==='completed')Object.assign(turn,{answer:'**已创建** 一个角色和一个项目群。',events:[
    {kind:'studio_entity',entity:{kind:'role',id:profile,name:names[0]}},
    {kind:'studio_entity',entity:{kind:'group',id:group,name:'电脑维护组'}},
   ],artifacts:[{path:'notes.md',size:12}]});
   return ok({run:summary(head,status),activity:{calls:[],truncated:false},
    pending_questions:status==='waiting_user'?[{request_id:'info',role:'superagent',text:'工作目录用哪一个？'}]:[],
    conversation:{superagent:true,workspace_path:workspace,head_run_id:head,turns:[turn]}});
  }
  if(path==='/api/runs/'+initial+'/questions/info') {
   answer=route.request().postDataJSON().answer;status='completed';return ok({});
  }
  const roomRun=groupThread.turns.find(turn=>path==='/api/runs/'+turn.run_id);
  if(path==='/api/runs/'+group || roomRun) {
   const run=roomRun ? {...summary(roomRun.run_id,roomRun.status),title:roomRun.user,mode:roomRun.mode}
    : {...summary(group,'ready'),mode:'room',title:groupThread.title};
   return ok({run,conversation:groupThread,tasks:[],artifacts:[],events:[],diagnostics:{issues:[]}});
  }
  if(path==='/api/workspaces/pick')return ok({path:workspace?'/tmp/next-project':'/tmp/project'});
  if(path==='/api/conversations')return ok({conversations:[]});
  return route.continue();
 });
 await page.goto(url);
 const input=page.getByLabel('给工作室助手发消息');
 const send=page.getByRole('button',{name:'发送消息'});
 await page.getByRole('heading',{name:'今天想完成什么？'}).waitFor();
 assert(await send.isDisabled());
 await page.getByRole('button',{name:/创建角色.*培养/}).click();
 assert.match(await input.inputValue(),/创建一个/);
 assert.equal(requests.length,0);
 await page.getByRole('button',{name:'选择工作区'}).click();
 await page.getByText('project',{exact:true}).waitFor();
 await input.fill('新建电脑维护组。');
 await input.press('Shift+Enter');await input.press('End');await input.type('继续。');
 assert.equal(requests.length,0);
 await input.dispatchEvent('keydown',{key:'Enter',isComposing:true});
 assert.equal(requests.length,0);
 await input.press('Enter');
 await page.getByRole('alert').waitFor();
 assert.match(await input.inputValue(),/继续。/);
 await send.click();
 await page.locator('.assistant-user').waitFor();
 assert.equal(requests[0].request_id,requests[1].request_id);
 assert.equal(requests[1].workspace_path,'/tmp/project');
 assert.equal(requests[1].parent_run_id,null);
 const pendingInput=await input.elementHandle();
 await input.fill('下一步检查磁盘');
 await input.focus();
 await page.waitForTimeout(2300);
 assert(await input.evaluate((n,other)=>n===other,pendingInput));
 assert.equal(await input.inputValue(),'下一步检查磁盘');
 assert(await send.isDisabled());
 status='waiting_user';
 const question=page.locator('.assistant-questions form');
 await question.locator('textarea').fill('使用当前目录。');
 await question.getByRole('button',{name:'答复并继续'}).click();
 const groupLink=page.locator('.studio-entity').filter({hasText:'电脑维护组'});
 await groupLink.waitFor();
 assert.equal(answer,'使用当前目录。');
 assert.equal(await page.locator('.studio-entity').filter({hasText:'电脑管家'}).getAttribute('href'),'#roles/'+profile);
 assert.equal(await groupLink.getAttribute('href'),'#run/'+group+'/chat?latest=1');
 assert.equal(await input.inputValue(),'下一步检查磁盘');
 await page.getByRole('button',{name:/新建任务/}).click();
 await page.locator('#task-dialog [name=recipient] option[value="caretaker"]').waitFor({state:'attached'});
 await page.getByRole('button',{name:'关闭新建任务'}).click();
 await page.getByRole('button',{name:'选择工作区'}).click();
 await page.getByText('next-project',{exact:true}).waitFor();
 await groupLink.click();
 const groupForm=page.locator('.reply-form');
 await groupForm.waitFor();
 assert.equal(await groupForm.locator('[name=recipient]').inputValue(),'reviewer');
 assert.equal(await groupForm.locator('[name=dispatch]').inputValue(),'team');
 assert(await groupForm.getByLabel('负责人',{exact:true}).isDisabled());
 const namedGroup=page.getByRole('heading',{level:1,name:'电脑维护组',exact:true});
 const lead=page.locator('.thread-head').getByText('负责人 · 验证员',{exact:true});
 await namedGroup.waitFor();
 await lead.waitFor();
 await groupForm.locator('[name=message]').fill('开始诊断');
 await groupForm.getByRole('button',{name:'发送'}).click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-1');
 assert.equal(requests.at(-1).conversation.parent_run_id,group);
 assert.equal(requests.at(-1).mode,'team');
 await groupForm.locator('[name=dispatch]').selectOption('single');
 await groupForm.locator('[name=recipient]').selectOption(profile);
 await groupForm.locator('[name=message]').fill('检查本机磁盘');
 await groupForm.getByRole('button',{name:'发送'}).click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-2');
 assert.equal(requests.at(-1).conversation.parent_run_id,'group-message-1');
 assert.equal(requests.at(-1).conversation.recipient,profile);
 await page.goto(url+'#assistant');
 await groupLink.click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-2');
 await namedGroup.waitFor();
 await lead.waitFor();
 assert.equal(await groupForm.locator('[name=recipient]').inputValue(),profile);
 assert.equal(await page.locator('.chat-turn').count(),2);
 await page.goBack();
 await groupLink.waitFor();
 await groupLink.click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-2');
 await groupForm.getByLabel('协作方式').selectOption('team');
 const fixedRecipient=groupForm.getByLabel('负责人',{exact:true});
 assert(await fixedRecipient.isDisabled());
 assert.equal(await fixedRecipient.inputValue(),'reviewer');
 await groupForm.locator('[name=message]').fill('请团队复核磁盘诊断');
 await groupForm.getByRole('button',{name:'发送'}).click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-3');
 assert.equal(requests.at(-1).conversation.parent_run_id,'group-message-2');
 assert.equal(requests.at(-1).mode,'team');
 assert.equal(requests.at(-1).conversation.recipient,'reviewer');
 await groupForm.getByLabel('消息类型').selectOption('question');
 const directRecipient=groupForm.getByLabel('回复角色',{exact:true});
 assert(await directRecipient.isEnabled());
 await directRecipient.selectOption(profile);
 await groupForm.locator('[name=message]').fill('目前的诊断结论是什么？');
 await groupForm.getByRole('button',{name:'发送'}).click();
 await page.waitForFunction(()=>document.querySelector('.reply-form')?.dataset.parent==='group-message-4');
 assert.equal(requests.at(-1).mode,'single');
 assert.equal(requests.at(-1).conversation.intent,'question');
 assert.equal(requests.at(-1).conversation.recipient,profile);
 await page.goto(url+'#run/group-message-1/chat');
 await page.getByText('你正在查看较早的一轮。').waitFor();
 assert.equal(await groupForm.count(),0);
 await page.getByRole('tab',{name:/任务概览/}).click();
 await page.getByRole('heading',{level:1,name:'开始诊断',exact:true}).waitFor();
 await page.goto(url+'#assistant');
 await groupLink.waitFor();
 assert.equal(await page.locator('.assistant-turn').count(),1);
 await page.setViewportSize({width:375,height:720});
 await page.waitForFunction(()=>document.querySelector('#sidebar').getBoundingClientRect().right<=1);
 assert(await input.isVisible());
 const bounds=await page.evaluate(()=>({page:document.documentElement.scrollWidth,viewport:window.innerWidth,
  form:document.querySelector('#assistant-form').getBoundingClientRect().bottom,height:window.innerHeight}));
 assert(bounds.page<=bounds.viewport && bounds.form<=bounds.height+1,JSON.stringify(bounds));
 await page.getByRole('button',{name:'展开导航'}).click();
 await page.getByRole('link',{name:'工作台',exact:true}).click();
 await page.waitForURL('**/#home');
 await page.getByRole('button',{name:'展开导航'}).click();
 await page.getByRole('link',{name:/统一对话/}).click();
 await groupLink.waitFor();
 assert.deepEqual(errors,[]);
}
withFixture('serve_room_fixture',verify).then(()=>console.log('Superagent UI passed')).catch(error=>{console.error(error);process.exitCode=1;});
