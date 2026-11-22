/* Browser regression fixture: no provider, credentials, tools or writes.
 * Run python tests/web/serve_stability_fixture.py, open the printed URL,
 * and read the PASS/FAIL list. The real app.js and stylesheet are used.
 */
'use strict';
let fixturePolls=0,fixtureTrim=false,fixtureChat=false,fixtureStop=false;
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
const nativeReview = {target:'deck-task',reviewer:'reviewer',candidate_version:'d'.repeat(64)};
const assessment = {target:nativeReview.target,candidate_version:nativeReview.candidate_version,
 evaluator:'reviewer',attempt_id:'review-attempt',verdict:'changes_requested',results:[
  ...['SRC-1','SRC-2','SRC-4','SRC-6'].map(criterion_id=>({criterion_id,verdict:'passed',reason:'已逐项核对。'})),
  {criterion_id:'SRC-3',verdict:'changes_requested',cause:'product',action:'补上第 20 页的实际和预测标注。'},
  {criterion_id:'SRC-5',verdict:'changes_requested',cause:'product',action:'补上敏感性计算。'}]};
const reviewEvent = (signature,disposition='assessed')=>({kind:'member_report',role:'reviewer',task_id:'review-task',
 text:'仍未能签署结论。',disposition,published:false,handoff:{role:'reviewer',task_id:'review-task',published:false,
 native_review:{...nativeReview,assessment:signature}}});
const signedEvents = [reviewEvent(assessment),reviewEvent({...assessment,verdict:'inconclusive',
 results:[{criterion_id:'CHECK',verdict:'inconclusive',cause:'verification',action:'重跑失败的检查。'}]}),
 reviewEvent({...assessment,verdict:'passed',results:assessment.results.slice(0,4)})];
const publishedReview = reviewEvent(assessment,'published');
publishedReview.published = publishedReview.handoff.published = true;
signedEvents.push(publishedReview);
const historical = reviewEvent({...assessment,verdict:'passed',results:assessment.results.slice(0,4)},'blocked');
Object.assign(historical.handoff.native_review,{kind:'product',state:'blocked',author:'worker',private_candidate_task:nativeReview.target});
signedEvents.push(historical);
const unboundEvents = [null,{...assessment,evaluator:'worker'},{...assessment,target:'other-task'},
 {...assessment,candidate_version:'e'.repeat(64)}].map(s=>reviewEvent(s));
unboundEvents.push(reviewEvent(assessment,'blocked'));
const completeThread = {
 id:'demo-project',kind:'group',lead:'worker',members:['worker','reviewer'],head_run_id:'fixture',
 artifact_run_id:'fixture',can_continue:true,
 turns:[{run_id:'fixture',user:'请制作网页。',recipient:'worker',answer:'已生成网页。',status:'unverified',intent:'edit',
  events:[{kind:'member_report',role:'worker',task_id:'dev-check',text:'[BLOCKED] 请复核重复预订。',disposition:'blocked',
   handoff:{role:'worker',task_id:'dev-check',published:false,remaining_issue:'[BLOCKED] 请复核重复预订。',
    reviewable_candidate:{task_id:'dev-check',candidate_version:'a'.repeat(64),files_total:5}}},
   {kind:'member_report',role:'reviewer',text:'**成果**：证据底座已保存到文件。',disposition:'blocked',artifacts:[],
   handoff:{published:false,remaining_issue:'项目快照发布失败：项目文件含可识别的服务凭据。「research/research_notes.md」'}},
   ...signedEvents,...unboundEvents]}]
};
window.fetch=async(path, options={})=>{
 let data;
 let ok=true;
 if(path==='/api/bootstrap') data={provider_ready:true,model:'offline fixture',thinking:'disabled',csrf_token:'fixture',conversation_roles:roles};
 else if(path==='/api/directory/chats') data={channels:[],recent:[{...completeThread,title:'测试项目',turn_count:1}]};
 else if(path==='/api/directory/channels' && options.method==='POST'){
  window.fixtureGroup=JSON.parse(options.body);data={channel:{id:'demo-project',head_run_id:'demo-project'}};
 }else if(path==='/api/groups/demo-project/messages' && options.method==='POST'){
  fixturePosts.push(JSON.parse(options.body));
  ok=false;data={error:'测试网络错误。',code:'fixture_error'};
 }else if(path==='/api/runs') data={runs:[fixtureRun]};
 else{
  data=fixtureDetail();
  if(fixtureChat){
   data.run.status='unverified';data.conversation=completeThread;
   data.diagnostics={execution_status:'finished',acceptance_status:'not_configured',issues:[{code:'no_acceptance'}]};
   if(fixtureStop){
    const [turn]=completeThread.turns;
    data.run.status='failed';
    data.conversation={...completeThread,turns:[
     {...turn,run_id:'earlier',events:turn.events.slice(0,2),answer:'',status:'failed'},
     {...turn,run_id:'no-receipt',events:[],error:'前一轮独立验收未通过。',answer:'',status:'failed'},
     {...turn,events:[],answer:'',status:'failed'}]};
    data.diagnostics={execution_status:'failed',acceptance_status:'failed',issues:[
     {severity:'warning',code:'output_truncated',message:'此前截断已恢复。'},
     {severity:'error',code:'provider_balance_exhausted',message:'DeepSeek 返回 HTTP 402（额度不足），本轮模型调用受阻。'}]};
   }
  }
 }
 return {ok,status:ok?200:503,json:async()=>data};
};
// Start on the deep link itself, as when a user opens a saved run. Assigning
// location.hash here also queues a second navigation after the app has booted.
history.replaceState(null, '', '#run/fixture');
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
  const [candidate,blocked,changes,unresolved,passed,published,legacy,...unbound]=document.querySelectorAll('.room-message[data-kind="member_report"]');
  check('frozen candidate shows its review option and retains original receipt',
   candidate.querySelector('.room-disposition').textContent==='候选已保存 · 可独立复核'&&
   candidate.querySelector('.room-line').textContent.includes('请复核重复预订')&&
   !candidate.querySelector('.room-line').textContent.includes('[BLOCKED]')&&
   candidate.querySelector('.room-material-body').textContent.includes('[BLOCKED]'));
  check('unpublished file without candidate retains real blockage',blocked.querySelector('.room-disposition').textContent==='交接受阻 · 平台回执');
  const blockedLine=blocked.querySelector('.room-line');
  check('blocked handoff leads with actual publication failure',blockedLine.textContent.includes('项目快照发布失败')&&!blockedLine.textContent.includes('证据底座已保存'));
  check('private file is plain text and original member claim stays in materials',!blockedLine.querySelector('[data-md-path]')&&blocked.querySelector('.room-material-body').textContent.includes('证据底座已保存'));
  const line=message=>message.querySelector('.room-line').textContent;
  const badge=message=>message.querySelector('.room-disposition').textContent;
  check('bound rejection leads with signed counts and both requested repairs',badge(changes)==='评审已签署 · 要求修改'&&
   ['4 项通过','SRC-3 待修改','补上第 20 页','SRC-5 待修改','敏感性计算'].every(word=>line(changes).includes(word))&&
   !line(changes).includes('未能签署'));
  const materials=changes.querySelector('.room-material');
  check('original member account and full findings are preserved in collapsed review material',!materials.open&&
   materials.querySelector('summary').textContent==='评审材料'&&materials.querySelector('.prose').textContent.includes('仍未能签署')&&
   materials.querySelectorAll('.room-review-findings li').length===6);
  check('inconclusive and passing signed reviews keep their distinct verdicts',badge(unresolved)==='评审已签署 · 暂未定论'&&
   line(unresolved).includes('CHECK 待补证')&&badge(passed)==='评审已签署 · 通过'&&line(passed).includes('4 项通过')&&!badge(passed).includes('交接'));
  check('publication is displayed separately when the signed event also published files',badge(published).includes('要求修改 · 本次交接已发布'));
  check('legacy blocked prose cannot erase a signed review of a bound frozen draft',
   badge(legacy)==='评审已签署 · 通过'&&line(legacy).includes('4 项通过')&&!badge(legacy).includes('已发布'));
  check('absent mismatched and unbound receipts never count as a signed assessment',unbound.length===5&&
   unbound.every(message=>!badge(message).includes('已签署')&&!line(message).includes('4 项通过'))&&
   unbound.slice(0,4).every(message=>line(message).includes('签署记录待核对')));
  const form=document.querySelector('.reply-form'),draft=form.elements.message;
  draft.value='请解释速度计算。';draft.dispatchEvent(new Event('input',{bubbles:true}));
  form.elements.intent.value='question';form.elements.intent.dispatchEvent(new Event('change',{bubbles:true}));
  form.elements.recipient.value='reviewer';form.elements.recipient.dispatchEvent(new Event('change',{bubbles:true}));
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
  check('question submits to a single chosen member',one.parent_run_id==='fixture'&&one.recipient==='reviewer'&&one.intent==='question'&&!one.options.acceptance_files);
  check('finished execution explicitly reports missing checks',document.querySelector('.detail-actions').textContent.includes('执行已结束 · 未配置验收'));
  fixtureStop=true;
  await waitFor(()=>document.querySelectorAll('.turn-system').length===3);
  const [past,ownMeta,current]=document.querySelectorAll('.turn-system');
  check('terminal provider error is identified as a Studio system record',current.dataset.origin==='studio'&&current.querySelector('.speaker').textContent.includes('系统记录')&&current.querySelector('.room-line').textContent.includes('HTTP 402'));
  check('old turn takes its own blocked receipt and never the selected run diagnosis',past.textContent.includes('项目快照发布失败')&&!past.textContent.includes('HTTP 402'));
  check('old turn without receipt uses its own saved error',ownMeta.textContent.includes('前一轮独立验收未通过')&&!ownMeta.textContent.includes('HTTP 402'));
  location.hash='chats';await waitFor(()=>document.querySelector('#chat-list a'));
  document.querySelector('[data-action="new-group"]').click();
  const group=document.querySelector('#dialog-form form');
  group.elements.objective.value='请检查游戏。';
  const reviewer=group.querySelector('[name="member"][value="reviewer"]');
  for(const box of group.querySelectorAll('[name=member]'))box.checked=false;reviewer.dispatchEvent(new Event('change',{bubbles:true}));
  group.requestSubmit();
  check('sending a task needs an available recipient',fixturePosts.length===2&&group.elements.recipient.validity.valueMissing);
  for(const box of group.querySelectorAll('[name=member]'))box.checked=true;reviewer.dispatchEvent(new Event('change',{bubbles:true}));
  group.elements.recipient.value='reviewer';group.requestSubmit();
  await waitFor(()=>fixturePosts.length===3);
  check('new group includes roster and explicit responder',window.fixtureGroup.members.length===2&&window.fixtureGroup.lead==='reviewer'&&fixturePosts[2].recipient==='reviewer'&&fixturePosts[2].parent_run_id==='demo-project'&&fixturePosts[2].intent==='auto');
    result.dataset.finished='true';result.dataset.passed=String(results.every(r=>r.passed));
 }catch(error){check(error.message,false);result.dataset.finished='true';result.dataset.passed='false';}
});
