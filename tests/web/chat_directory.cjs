// Exercise the user-facing navigation against real room snapshots and Studio's HTTP API.
const assert = require('node:assert/strict');
const { withFixture } = require('./browser_fixture.cjs');

async function verify(page, url) {
 const errors = [];
 page.on('pageerror', error => errors.push(error.message));
 const nav = page.getByRole('navigation',{name:'固定群聊',exact:true});
 const dialog = page.locator('#channel-dialog');
 const groups = page.locator('.directory-chat');
 const getDirectory = async () => (await page.request.get(url+'api/directory/chats')).json();
 await page.goto(url+'#chats');
 await page.getByRole('heading',{name:'最近对话'}).waitFor();
 await nav.getByRole('link',{name:'从已有群聊中固定'}).click();
 const history = groups.filter({hasText:'复核机房告警'});
 await history.waitFor();
 assert.equal(await groups.count(),1);
 const original = (await getDirectory()).recent.find(room=>room.title==='复核机房告警');
 await history.getByRole('button',{name:'固定到侧栏'}).click();
 await dialog.getByLabel('群聊名称').fill('机房运维组');
 await dialog.getByRole('button',{name:'固定到侧栏'}).click();
 await nav.getByRole('link',{name:'机房运维组'}).click();
 await page.getByRole('heading',{name:'机房运维组',exact:true}).waitFor();
 await page.locator('.thread-head').getByRole('link',{name:'值班工程师'}).click();
 await page.getByRole('heading',{name:'值班工程师'}).waitFor();
 await page.locator('.role-task-row').getByText('复核机房告警',{exact:true}).click();
 await page.locator('.task-button[aria-expanded=true]').waitFor();
 assert.ok(page.url().includes(original.head_run_id));
 // Name changes and unpinning preserve the room ID, turns and member work.
 await nav.getByRole('link',{name:'机房运维组'}).click();
 await page.getByRole('button',{name:'管理固定群聊'}).click();
 await dialog.getByLabel('群聊名称').fill('维护频道');
 await dialog.getByRole('button',{name:'保存群名'}).click();
 await nav.getByRole('link',{name:'维护频道'}).waitFor();
 await page.reload();
 await nav.getByRole('link',{name:'维护频道'}).waitFor();
 await page.getByRole('button',{name:'管理固定群聊'}).click();
 await dialog.getByRole('button',{name:'取消固定'}).click();
 await nav.getByRole('link',{name:'从已有群聊中固定'}).waitFor();
 const renamed = (await getDirectory()).recent.find(room=>room.id===original.id);
 assert.equal(renamed.title,'维护频道');
 assert.equal(renamed.head_run_id,original.head_run_id);
 assert.equal(renamed.turn_count,original.turn_count);
 assert.equal(renamed.pinned,false);
 // A named channel exists before any task. A missing second member leaves the form open.
 await page.setViewportSize({width:390,height:800});
 await page.getByRole('button',{name:'展开导航'}).click();
 await page.getByRole('button',{name:'新建固定群聊',exact:true}).click();
 await dialog.getByLabel('群聊名称').fill('维护新群');
 for (const entry of await dialog.locator('[name="member"]').all()) await entry.uncheck();
 await dialog.locator('[value="worker"][name="member"]').check();
 await dialog.getByRole('button',{name:'创建群聊'}).click();
 await dialog.getByRole('alert').getByText('请选择至少两位群成员。').waitFor();
 await dialog.locator('[value="reviewer"][name="member"]').check();
 await dialog.getByLabel('负责人').selectOption('reviewer');
 await dialog.getByRole('button',{name:'创建群聊'}).click();
 await page.getByRole('heading',{name:'维护新群',exact:true}).waitFor();
 const result = await getDirectory();
 assert.equal(result.channels.length,1);
 const created = result.channels[0];
 assert.equal(created.turn_count,0);
 assert.equal(created.head_run_id,created.id);
 assert.deepEqual(created.members.sort(),['reviewer','worker']);
 assert.equal(await page.locator('.reply-form textarea[name=message]').isVisible(),true);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.goto(url+'#chats');
 await page.getByRole('link',{name:'私聊',exact:true}).click();
 await page.locator('.chat-filters [aria-current=page]').getByText('私聊',{exact:true}).waitFor();
 await groups.filter({hasText:'维护新群'}).waitFor({state:'detached'});
 await page.getByRole('link',{name:'全部',exact:true}).click();
 await page.locator('.chat-filters [aria-current=page]').getByText('全部',{exact:true}).waitFor();
 await page.getByRole('searchbox',{name:'搜索对话或成员'}).fill('维护频道');
 await groups.getByText('维护频道',{exact:true}).waitFor();
 assert.equal(await groups.count(),1);
 assert.deepEqual(errors,[]);
 console.log('Recent conversations, exact task links, renaming, persistent pinning, empty channels, member validation and mobile navigation passed.');
}

withFixture('serve_role_fixture',verify).catch(error=>{console.error(error);process.exitCode=1;});
