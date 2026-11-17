const {chromium, request, expect}=require('playwright/test');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const base='http://127.0.0.1:20101', artifacts=process.env.MSGHUB_TEST_ARTIFACTS || '/tmp/msghub-browser';
  fs.mkdirSync(artifacts,{recursive:true});
  const api=await request.newContext({baseURL:base});
  const token=(await (await api.post('/api/admin/login',{data:{password:'acceptance-only'}})).json()).token;
  assert(token); const headers={Authorization:'Bearer '+token};
  async function call(method,url,data) {
    const r=await api[method](url,{headers,data}); assert(r.ok(),url+': '+r.status()+' '+await r.text()); return r.json();
  }
  const sites=[], contexts=[], errors=[];
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    for(const name of ['关联验收官网','关联验收商城']) sites.push(await call('post','/api/admin/sites',{name,require_contact:false,ai_enabled:false}));
    const fixture=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',...sites.map(s=>String(s.id))],{encoding:'utf8',input:`
import json, os, sqlite3, sys, secrets
from app.config import DB_PATH
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
with sqlite3.connect(DB_PATH) as db:
    convs=[]; mids=[]
    for i in range(2):
        v=db.execute('INSERT INTO visitors (token,name,contact) VALUES (?,?,?)',(secrets.token_hex(16),'关联访客'+str(i),'visitor'+str(i)+'@example.com')).lastrowid
        c=db.execute("INSERT INTO conversations (site_id,visitor_id,last_preview,unread) VALUES (?,?,?,1)",(int(sys.argv[i+1]),v,'聊天上下文 '+str(i))).lastrowid
        convs.append(c)
        db.execute("INSERT INTO chat_messages (conversation_id,sender,content,msg_type) VALUES (?,'visitor',?,'text')",(c,'聊天上下文 '+str(i)))
    for i in range(13):
        mids.append(db.execute('INSERT INTO messages (site_id,conversation_id,content,note) VALUES (?,?,?,?)',(int(sys.argv[1]),convs[0],'关联内容 '+str(i)+' <img src=x onerror="window.inboxXss=true">','私有处理记录')).lastrowid)
    for i in range(22):
        db.execute('INSERT INTO messages (site_id,content) VALUES (?,?)',(int(sys.argv[1]),'独立表单内容 '+str(i)))
    print(json.dumps({'convs':convs,'mids':mids}))
`}));
    const [convA,convB]=fixture.convs, latest=fixture.mids.at(-1);
    async function admin() {
      const ctx=await browser.newContext({viewport:{width:1440,height:900}});contexts.push(ctx);
      await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
      const page=await ctx.newPage(); page.on('pageerror',e=>errors.push(e.message));
      await page.goto(base); await expect(page.locator('#liveState')).toHaveText('● 实时连接正常');return page;
    }
    const page=await admin(), observer=await admin();
    for(const p of [page,observer]) {
      await p.locator('.conv[data-id="'+convA+'"]').click();
      await expect(p.locator('#relatedCount')).toHaveText('13');
    }
    await page.locator('#replyInput').fill('关联跳转保留回复草稿');
    await page.locator('.conv-notes summary').click();
    await page.locator('#convNoteInput').fill('关联跳转保留会话备注');
    for(const p of [page,observer]) {
      await p.locator('[data-act=related]').click();
      await expect(p.locator('.related-message')).toHaveCount(10);
    }
    await page.locator('[data-open-message="'+latest+'"]').click();
    await expect(page.locator('#drawer')).toBeVisible();
    await expect(page.locator('#messageStatus')).toHaveText('已读');
    await expect(page.locator('#viewSourceConv')).toHaveText('查看对话 #'+convA);
    assert.equal(await page.locator('#drawerBody img').count(),0);
    assert.equal(await page.evaluate(()=>window.inboxXss),undefined);
    await page.locator('#noteInput').fill('尚未保存的留言备注');
    await page.locator('#backToRelated').click();
    await page.locator('[data-open-message="'+latest+'"]').click();
    await expect(page.locator('#noteInput')).toHaveValue('尚未保存的留言备注');
    await page.locator('#viewSourceConv').click();
    await expect(page.locator('#drawer')).toBeHidden();
    await expect(page.locator('#replyInput')).toHaveValue('关联跳转保留回复草稿');
    await expect(page.locator('#convNoteInput')).toHaveValue('关联跳转保留会话备注');
    await page.locator('[data-act=related]').click();
    await page.locator('[data-open-message="'+latest+'"]').click();
    await page.locator('#drawerBody [data-s=handled]').click();
    await expect(page.locator('#drawer')).toBeHidden();
    await expect(observer.locator('.related-message').filter({hasText:'留言 #'+latest}).locator('.tag')).toHaveText('已处理');
    assert.equal((await call('get','/api/admin/messages/'+latest)).note,'尚未保存的留言备注');
    assert.equal((await call('get','/api/admin/conversations/'+convA)).conversation.status,'waiting');
    await page.locator('[data-act=related]').click();
    await page.locator('#relatedPager [data-related-page="2"]').click();
    await expect(page.locator('.related-message')).toHaveCount(3);
    // Detail can be opened even when the message is outside the main inbox's current page.
    await page.locator('[data-open-message="'+fixture.mids[0]+'"]').click();
    await expect(page.locator('#drawerBody')).toContainText('关联内容 0');
    await page.locator('#backToRelated').click();
    await expect(page.locator('#relatedPager')).toContainText('2/2');
    await page.locator('#relatedDrawer [data-close]').click();
    await page.locator('.conv-notes summary').click();
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]) {
      await page.setViewportSize(size);
      const layout=await page.evaluate(()=>({width:document.documentElement.scrollWidth,
        reply:document.querySelector('#replyBtn').getBoundingClientRect().bottom,
        messages:document.querySelector('#chatMsgs').clientHeight,height:innerHeight,
        link:document.querySelector('[data-act=related]').getBoundingClientRect()}));
      assert(layout.width<=size.width && layout.reply<=size.height && layout.messages>50,JSON.stringify({size,layout}));
      await page.locator('[data-act=related]').click();
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(artifacts,'links-'+size.width+'.png'),fullPage:true});
      await page.locator('#relatedDrawer [data-close]').click();
    }
    await page.setViewportSize({width:1440,height:900});
    await page.locator('.conv[data-id="'+convB+'"]').click();
    await expect(page.locator('#relatedCount')).toHaveText('0');
    await page.locator('[data-act=related]').click();
    await expect(page.locator('#relatedList')).toContainText('此会话暂无关联留言');
    await page.locator('#relatedDrawer [data-close]').click();
    await page.locator('nav [href="#messages"]').click();
    await expect(page.locator('#msgBody tr[data-id]')).toHaveCount(20);
    await page.locator('#fQ').fill('独立表单内容'); await page.locator('#btnSearch').click();
    await page.locator('#msgBody tr[data-id]').first().click();
    await expect(page.locator('#drawerBody')).toContainText('独立留言（未关联会话）');
    await expect(page.locator('#viewSourceConv')).toHaveCount(0);
    await page.locator('#drawer [data-close]').click();
    await page.locator('#fQ').fill('关联内容 12'); await page.locator('#btnSearch').click();
    await page.locator('#msgBody tr[data-id="'+latest+'"]').click();
    await page.locator('#viewSourceConv').click();
    await expect(page.locator('#chatHead h3')).toContainText('#'+convA+' ');
    await expect(page.locator('#drawer')).toBeHidden();
    await expect(page.locator('#relatedCount')).toHaveText('13');
    // A delayed detail request must not reopen a drawer after Escape.
    await page.locator('[data-act=related]').click();
    let release, started; const held=new Promise(r=>release=r),begun=new Promise(r=>started=r);
    const detailUrl='**/api/admin/messages/'+latest;
    await page.route(detailUrl,async route=>{const response=await route.fetch();started();await held;await route.fulfill({response});});
    await page.locator('[data-open-message="'+latest+'"]').click(); await begun;
    await page.keyboard.press('Escape'); release(); await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('#drawer')).toBeHidden();
    // Remote changes update both the association count and visible related list.
    await call('delete','/api/admin/messages/'+latest);
    await expect(page.locator('#relatedCount')).toHaveText('12');
    await expect(observer.locator('#relatedCount')).toHaveText('12');
    await expect(observer.locator('[data-open-message="'+latest+'"]')).toHaveCount(0);
    await call('delete','/api/admin/conversations/'+convA);
    await page.locator('nav [href="#messages"]').click();
    await page.locator('#fQ').fill('关联内容 0'); await page.locator('#btnSearch').click();
    await page.locator('#msgBody tr[data-id="'+fixture.mids[0]+'"]').click();
    await expect(page.locator('#drawerBody')).toContainText('已删除或不可用，留言仍保留');
    await expect(page.locator('#viewSourceConv')).toHaveCount(0);
    await page.setViewportSize({width:320,height:568});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(artifacts,'links-orphan-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: bidirectional links; related pagination/detail outside inbox page; drafts; mark/read/handle; two-admin sync; independent/orphan states; late response isolation; desktop/mobile.');
  } finally {
    for(const ctx of contexts) await ctx.close();
    await browser.close();
    for(const site of sites) await api.delete('/api/admin/sites/'+site.id,{headers});
    await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
