/* Browser regression fixture: no provider, credentials, tools or writes.
 * Run python tests/web/serve_stability_fixture.py, open the printed URL,
 * and read the PASS/FAIL list. The real app.js and stylesheet are used.
 */
'use strict';
let fixturePolls=0,fixtureTrim=false,fixtureChat=false;
const fixturePosts=[];
const fixtureEvents=Array.from({length:40},(_,i)=>({kind:'step',ts:1700000000+i,task_id:'fixture:child',step:i+1,message:`Event ${i+1}: immutable execution evidence for selection testing.`}));
const fixtureRun={id:'fixture',title:'持续运行：界面稳定性回归',mode:'single',status:'running',created_at:'2026-11-17T12:00:00Z',task_count:2,completed_count:0,artifact_count:0,duration_ms:0};
const fixtureDetail=()=>{
 fixturePolls++;
 fixtureEvents.push({kind:'step',ts:1700000100+fixturePolls,task_id:'fixture:child',step:40+fixturePolls,message:'A new event arrived while the user was reading.'});
 if(fixtureTrim)fixtureEvents.shift();
 return {run:{...fixtureRun,duration_ms:fixturePolls*1800},tasks:[{task_id:'fixture',objective:'持续运行的任务',status:'running',revision:fixturePolls},{task_id:'fixture:child',parent_id:'fixture',objective:'子任务执行中',status:'running',revision:fixturePolls}],events:fixtureEvents.slice(),artifacts:[],acceptance:[{task_id:'fixture',report:{report_id:'fixture-report',verdict:'unverified',reason:'Fixture only; no execution or acceptance is claimed.'}}],summary:{total_tokens:fixturePolls*10}};
};
const roles = [{id:'worker',label:'通用助手'},{id:'reviewer',label:'评审员'}];
const completeThread = {
 id:'demo-project',kind:'group',members:['worker','reviewer'],head_run_id:'fixture',
 artifact_run_id:'fixture',can_continue:true,
 turns:[{run_id:'fixture',user:'请制作网页。',recipient:'worker',answer:'已生成网页。',status:'unverified',intent:'edit'}]
};
window.fetch=async(path, options={})=>{
 let data;
 let ok=true;
 if(path==='/api/bootstrap') data={provider_ready:true,model:'offline fixture',thinking:'disabled',csrf_token:'fixture',conversation_roles:roles};
 else if(path==='/api/conversations') data={conversations:[{...completeThread,title:'测试项目',turn_count:1}]};
 else if(path==='/api/runs' && options.method==='POST'){
  fixturePosts.push(JSON.parse(options.body));
  ok=false;data={error:'测试网络错误。',code:'fixture_error'};
 }else if(path==='/api/runs') data={runs:[fixtureRun]};
 else{
  data=fixtureDetail();
  if(fixtureChat){
   data.run.status='unverified';data.conversation=completeThread;
   data.diagnostics={execution_status:'finished',acceptance_status:'not_configured',issues:[{code:'no_acceptance'}]};
  }
 }
 return {ok,status:ok?200:503,json:async()=>data};
};
location.hash='run/fixture';
window.addEventListener('load',async()=>{
 const result=document.createElement('pre');result.id='regression-results';result.setAttribute('role','status');result.style.cssText='position:fixed;bottom:0;right:0;z-index:9999;background:#fff;color:#111;padding:16px;border:1px solid #888;max-width:580px;font:13px/1.5 monospace;white-space:pre-wrap';document.body.append(result);
 const results=[];
 const check=(name,passed)=>{results.push({name,passed});result.textContent=results.map(r=>`${r.passed?'PASS':'FAIL'} ${r.name}`).join('\n');};
 const waitFor=async predicate=>{const until=Date.now()+10000;while(!predicate()){if(Date.now()>until)throw Error('Timed out waiting for fixture poll');await new Promise(resolve=>setTimeout(resolve,50));}};
 try{
  await waitFor(()=>document.querySelector('#tab-logs'));
  document.querySelector('#tab-logs').click();
  const page=document.querySelector('#main > .page');
  const entry=document.querySelectorAll('.log-entry')[20];
  entry.querySelector('summary').click();
  const filter=document.querySelector('#log-filter');filter.focus({preventScroll:true});
  entry.scrollIntoView({block:'center'});
  const text=entry.querySelector('pre').firstChild;
  const range=document.createRange();range.setStart(text,0);range.setEnd(text,Math.min(28,text.length));
  const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
  const selectionText=selection.toString(),scroll=window.scrollY,initialPolls=fixturePolls,key=entry.dataset.key;
  await waitFor(()=>fixturePolls>=initialPolls+2);
  check('page identity survives two changing polls',document.querySelector('#main > .page')===page);
  check('existing log node stays mounted',Array.from(document.querySelectorAll('.log-entry')).find(n=>n.dataset.key===key)===entry);
  check('expanded log stays open',entry.open);
  check('focused filter stays focused',document.activeElement===filter);
  check('selected evidence text stays selected',window.getSelection().toString()===selectionText&&selection.anchorNode===text);
  check('reading position stays stable',Math.abs(window.scrollY-scroll)<=2);
  check('page entrance animation is not restarted',page.getAnimations().every(animation=>animation.playState!=='running'));
  fixtureTrim=true;
  const beforeTrim=fixturePolls;
  await waitFor(()=>fixturePolls>beforeTrim);
  check('rolling trace window retains the same expanded event',Array.from(document.querySelectorAll('.log-entry')).find(n=>n.dataset.key===key)===entry&&entry.open);
  document.querySelector('#tab-acceptance').click();
  const receipt=document.querySelector('.acceptance-card details');receipt.querySelector('summary').click();
  const beforeReceipt=fixturePolls;
  await waitFor(()=>fixturePolls>beforeReceipt);
  check('acceptance evidence stays expanded after polling',document.querySelector('.acceptance-card details')===receipt&&receipt.open);
  fixtureChat=true;
  await waitFor(()=>document.querySelector('#tab-chat'));
  document.querySelector('#tab-chat').click();
  const form=document.querySelector('.reply-form'),draft=form.elements.message;
  draft.value='请解释速度计算。';draft.dispatchEvent(new Event('input',{bubbles:true}));
  form.elements.recipient.value='reviewer';form.elements.recipient.dispatchEvent(new Event('change',{bubbles:true}));
  form.elements.intent.value='question';form.elements.intent.dispatchEvent(new Event('change',{bubbles:true}));
  draft.focus();const beforeDraft=fixturePolls;
  await waitFor(()=>fixturePolls>beforeDraft);
  check('reply draft, recipient and focus survive polling',document.activeElement===draft&&draft.value==='请解释速度计算。'&&form.elements.recipient.value==='reviewer');
  check('question hides file acceptance field',form.elements.acceptance_files.disabled&&form.elements.acceptance_files.closest('label').hidden);
  form.requestSubmit();
  await waitFor(()=>document.querySelector('.reply-form .form-error'));
  form.requestSubmit();
  await waitFor(()=>fixturePosts.length===2&&!form.querySelector('fieldset').disabled);
  const [one,two]=fixturePosts;
  check('failed send retains draft and idempotency key',draft.value==='请解释速度计算。'&&one.request_id===two.request_id);
  check('question submits to a single chosen member',one.conversation.parent_run_id==='fixture'&&one.conversation.recipient==='reviewer'&&one.conversation.intent==='question'&&!one.acceptance_files);
  check('finished execution explicitly reports missing checks',document.querySelector('.detail-actions').textContent.includes('执行已结束 · 未配置验收'));
  location.hash='chats';await waitFor(()=>document.querySelector('#chat-list a'));
  document.querySelector('[data-action="new-group"]').click();
  const group=document.querySelector('#dialog-form form');
  group.elements.objective.value='请检查游戏。';
  const reviewer=group.querySelector('[name="member"][value="reviewer"]');
  reviewer.checked=false;reviewer.dispatchEvent(new Event('change',{bubbles:true}));
  group.requestSubmit();
  check('group requires two members before dispatch',fixturePosts.length===2&&!group.querySelector('.form-error').hidden);
  reviewer.checked=true;reviewer.dispatchEvent(new Event('change',{bubbles:true}));
  group.elements.recipient.value='reviewer';group.requestSubmit();
  await waitFor(()=>fixturePosts.length===3);
  check('new group includes roster and explicit responder',fixturePosts[2].conversation.kind==='group'&&fixturePosts[2].conversation.members.length===2&&fixturePosts[2].conversation.recipient==='reviewer');
    result.dataset.finished='true';result.dataset.passed=String(results.every(r=>r.passed));
 }catch(error){check(error.message,false);result.dataset.finished='true';result.dataset.passed='false';}
});
