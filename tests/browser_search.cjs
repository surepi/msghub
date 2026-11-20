const {chromium, request, expect} = require('playwright/test');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async()=>{
  const base='http://127.0.0.1:20101';
  const artifacts=process.env.MSGHUB_TEST_ARTIFACTS || '/tmp/msghub-browser';
  fs.mkdirSync(artifacts,{recursive:true});
  const api=await request.newContext({baseURL:base});
  const token=(await (await api.post('/api/admin/login',{data:{password:'acceptance-only'}})).json()).token;
  assert(token);
  const headers={Authorization:'Bearer '+token};
  async function call(method,url,data) {
    const res=await api[method](url,{headers,data});
    assert(res.ok(),url+': '+res.status()+' '+await res.text()); return res.json();
  }
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const sites=[], tags=[], errors=[];
  try {
    for(const name of ['搜索验收官网','搜索验收商城']) sites.push(await call('post','/api/admin/sites',{name,require_contact:false,ai_enabled:false}));
    // Seed only the named disposable acceptance container, never a production database.
    const fixture=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',...sites.map(s=>String(s.id))],{encoding:'utf8',input:`
import json, os, sqlite3, sys, secrets
from app.config import DB_PATH
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
with sqlite3.connect(DB_PATH) as db:
    convs=[]; target=None
    for i in range(35):
        site_id=int(sys.argv[1 if i<34 else 2])
        visitor_token=secrets.token_hex(16)
        if i==0: target_token=visitor_token
        v=db.execute('INSERT INTO visitors (site_id,token,name,contact) VALUES (?,?,?,?)',(site_id,visitor_token,'分页客户'+str(i),'customer'+str(i)+'@example.com')).lastrowid
        c=db.execute("INSERT INTO conversations (site_id,visitor_id,last_preview,last_msg_at,unread) VALUES (?,?,?,'2026-02-11 12:00:00',1)",(site_id,v,'最新消息不含关键词')).lastrowid
        convs.append(c)
        for j in range(620 if i==0 else 2):
            text='<img src=x onerror="window.searchXss=true"> QuotedNeedle '+('后文'*100) if i==0 and j==80 else '普通消息 '+str(j)
            stamp='2026-01-10 23:59:59' if i==0 and j==80 else '2026-02-11 12:00:00'
            mid=db.execute("INSERT INTO chat_messages (conversation_id,sender,content,created_at,msg_type) VALUES (?,'visitor',?,?,'text')",(c,text,stamp)).lastrowid
            if i==0 and j==80: target=mid
        # These are historical search fixtures, already handled by the unattended flow.
        # Otherwise its 20-second scan adds a system notice during pagination checks.
        db.execute('UPDATE conversations SET auto_handled_upto=? WHERE id=?',(mid,c))
    print(json.dumps({'convs':convs,'target':target,'token':target_token}))
` }));
    const targetConv=fixture.convs[0];
    const tag=await call('post','/api/admin/tags',{name:'搜索组合验收'}); tags.push(tag.id);
    await call('put','/api/admin/conversations/'+targetConv+'/tags/'+tag.id);
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(token=>localStorage.setItem('msghub_token',token),token);
    const page=await ctx.newPage(); page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);
    const sendVisitor=content=>page.evaluate(({siteKey, token, content})=>new Promise((resolve, reject)=>{
      const ws=new WebSocket(location.origin.replace(/^http/, "ws")+"/api/chat/ws");
      const timer=setTimeout(()=>{ws.close();reject(new Error("Visitor fixture timed out"));}, 4000);
      ws.onopen=()=>ws.send(JSON.stringify({type:"hello", site_key:siteKey, token}));
      ws.onmessage=event=>{
        const data=JSON.parse(event.data);
        if(data.type==="init") ws.send(JSON.stringify({type:"msg", content, client_id:"read-fixture"}));
        if(data.type==="error" || data.client_id==="read-fixture") {
          clearTimeout(timer); ws.close();
          if(data.type==="error") reject(new Error(data.error));
          else resolve(data.message.id);
        }
      };
    }), {siteKey:sites[0].site_key, token:fixture.token, content});
    const unread=async()=>(await call('get','/api/admin/conversations/'+targetConv)).conversation.unread;
    await expect(page.locator('#liveState')).toHaveText('● 实时连接正常');
    await expect(page.locator('.conv')).toHaveCount(30);
    await page.locator('.conv-check').first().check();
    await page.locator('#convNext').click();
    await expect(page.locator('.conv')).toHaveCount(5);
    await expect(page.locator('#convPageInfo')).toHaveText('2/2 · 共 35 条');
    await expect(page.locator('#convSelectionCount')).toHaveText('已选 0 条');
    // Reordering events must preserve the current page and its correct size.
    await call('post','/api/admin/conversations/'+targetConv+'/messages',{content:'分页期间的新消息'});
    await expect(page.locator('#convPageInfo')).toHaveText('2/2 · 共 35 条');
    await expect(page.locator('.conv')).toHaveCount(5);
    await page.locator('#cQ').fill('QuotedNeedle');
    await expect(page.locator('.conv')).toHaveCount(1);
    await expect(page.locator('#convPageInfo')).toHaveText('1/1 · 共 1 条');
    await expect(page.locator('.conv-match')).toContainText('QuotedNeedle');
    assert.equal(await page.locator('.conv-match img').count(),0);
    assert.equal(await page.evaluate(()=>window.searchXss),undefined);
    await page.locator('.conv-filters summary').click();
    await page.locator('#cSite').selectOption(String(sites[0].id));
    await page.locator('#cFrom').fill('2026-01-10'); await page.locator('#cFrom').dispatchEvent('change');
    await page.locator('#cTo').fill('2026-01-10'); await page.locator('#cTo').dispatchEvent('change');
    await page.locator('#cTag').selectOption(String(tag.id));
    await expect(page.locator('.conv-match')).toBeVisible();
    await page.locator('.conv-match').click();
    await expect(page.locator('#chatMsgs .search-target')).toHaveAttribute('data-message-id',String(fixture.target));
    const onScreen=await page.locator('#chatMsgs .search-target').evaluate(el=>{
      const a=el.getBoundingClientRect(), b=el.parentElement.getBoundingClientRect(); return a.top>=b.top && a.top<b.bottom;
    }); assert(onScreen,'Target must be scrolled into the message viewport');
    await expect(page.locator('#jumpLatest')).toBeVisible();
    await expect(page.locator('#loadOlder')).toBeVisible();
    await page.locator('#replyInput').fill('搜索期间回复草稿');
    await page.locator('.conv-notes summary').click(); await page.locator('#convNoteInput').fill('搜索期间备注草稿');
    await page.locator('#loadOlder').click();
    await expect(page.locator('#loadOlder')).toBeHidden();
    await expect(page.locator('#replyInput')).toHaveValue('搜索期间回复草稿');
    await page.locator('#loadNewer').click();
    await expect(page.locator('#chatMsgs .msg')).toHaveCount(230);
    const before=await page.locator('#chatMsgs .msg').count();
    await sendVisitor('历史窗口外的新消息');
    await expect.poll(unread).toBe(1);
    await expect(page.locator('.conv .unread')).toHaveText('1');
    await expect(page.locator('#chatMsgs .msg')).toHaveCount(before);
    await page.locator('#jumpLatest').click();
    await expect(page.locator('#chatMsgs')).toContainText('历史窗口外的新消息');
    await expect(page.locator('#jumpLatest')).toBeHidden();
    await expect.poll(unread).toBe(0);
    await expect(page.locator('#convNoteInput')).toHaveValue('搜索期间备注草稿');
    await page.locator('#chatMsgs').evaluate(box=>box.scrollTop=0);
    await sendVisitor('滚动区下方的新消息');
    await expect(page.locator('#chatMsgs')).toContainText('滚动区下方的新消息');
    await expect.poll(unread).toBe(1);
    await page.locator('#chatMsgs').evaluate(box=>box.scrollTop=box.scrollHeight);
    await expect.poll(unread).toBe(0);
    // A message arriving after the latest-history snapshot must survive its delayed response.
    await page.locator('.conv-match').click();
    let releaseLatest, latestStarted;
    const heldLatest=new Promise(r=>releaseLatest=r), beganLatest=new Promise(r=>latestStarted=r);
    const historyUrl='**/api/admin/conversations/'+targetConv+'/messages?limit=100';
    await page.route(historyUrl,async route=>{
      const response=await route.fetch(); latestStarted(); await heldLatest; await route.fulfill({response});
    });
    await page.locator('#jumpLatest').click(); await beganLatest;
    await call('post','/api/admin/conversations/'+targetConv+'/messages',{content:'快照之后到达的新消息'});
    releaseLatest();
    await expect(page.locator('#chatMsgs')).toContainText('快照之后到达的新消息');
    await page.unroute(historyUrl);
    // Another site and an out-of-range day correctly exclude the history hit.
    await page.locator('#cSite').selectOption(String(sites[1].id));
    await expect(page.locator('.conv')).toHaveCount(0);
    await page.locator('#cSite').selectOption(String(sites[0].id));
    await page.locator('#cTo').fill('2026-01-11'); await page.locator('#cTo').dispatchEvent('change');
    await page.locator('#cFrom').fill('2026-01-11'); await page.locator('#cFrom').dispatchEvent('change');
    await expect(page.locator('.conv')).toHaveCount(0);
    await page.locator('#cFrom').fill('2026-01-10'); await page.locator('#cFrom').dispatchEvent('change');
    await expect(page.locator('.conv')).toHaveCount(1);
    // A slow obsolete search must not replace a newer query.
    let release, started;
    const held=new Promise(r=>release=r), begun=new Promise(r=>started=r);
    await page.route('**/api/admin/conversations?**',async route=>{
      if(new URL(route.request().url()).searchParams.get('q')==='no-results-slow') {
        const response=await route.fetch(); started(); await held; await route.fulfill({response});
      } else await route.continue();
    });
    await page.locator('#cQ').fill('no-results-slow'); await begun;
    await page.locator('#cQ').fill('QuotedNeedle');
    await expect(page.locator('.conv')).toHaveCount(1);
    release();
    await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('.conv')).toHaveCount(1);
    // A late history response from the previous conversation cannot replace a new pane.
    await page.locator('.conv-match').click();
    let releaseHistory, historyStarted;
    const heldHistory=new Promise(r=>releaseHistory=r), beganHistory=new Promise(r=>historyStarted=r);
    await page.route(historyUrl,async route=>{
      const response=await route.fetch(); historyStarted(); await heldHistory; await route.fulfill({response});
    });
    await page.locator('#jumpLatest').click(); await beganHistory;
    await page.locator('#clearConvFilters').click();
    await page.locator('#cQ').fill('#'+fixture.convs[1]);
    await expect(page.locator('.conv')).toHaveCount(1);
    await page.locator('.conv[data-id="'+fixture.convs[1]+'"]').click();
    await expect(page.locator('#chatHead h3')).toContainText('#'+fixture.convs[1]+' ');
    releaseHistory(); await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('#chatMsgs .msg')).toHaveCount(2);
    await page.locator('#cQ').fill('QuotedNeedle');
    await expect(page.locator('.conv')).toHaveCount(1);
    // History controls and the filter/pager fit small phones as well as desktop.
    await page.locator('.conv-match').click();
    await expect(page.locator('.search-target')).toBeVisible();
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]) {
      await page.setViewportSize(size);
      if(size.width<=760) await page.locator('[data-act=back]').click();
      await page.locator('.conv-match').click();
      await expect(page.locator('#chatMsgs .search-target')).toBeVisible();
      await expect.poll(async()=>page.locator('#chatMsgs .search-target').evaluate(el=>{
        const a=el.getBoundingClientRect(),b=el.parentElement.getBoundingClientRect();return a.top>=b.top && a.top<b.bottom;
      }),{message:'Hit must be visible at '+size.width}).toBe(true);
      const layout=await page.evaluate(()=>({width:document.documentElement.scrollWidth,
        reply:document.querySelector('#replyBtn').getBoundingClientRect().bottom,
        messages:document.querySelector('#chatMsgs').clientHeight,height:innerHeight}));
      assert(layout.width<=size.width && layout.reply<=size.height && layout.messages>50,JSON.stringify({size,layout}));
      await page.screenshot({path:path.join(artifacts,'search-'+size.width+'.png'),fullPage:true});
    }
    await page.locator('[data-act=back]').click();
    await page.locator('.conv-filters summary').click();
    for(const selector of ['#convNext','#manageTags']) {
      const b=await page.locator(selector).boundingBox(); assert(b.x>=0 && b.x+b.width<=320,selector);
    }
    await page.screenshot({path:path.join(artifacts,'search-mobile-list.png'),fullPage:true});
    await page.locator('.conv-filters summary').click();
    await page.screenshot({path:path.join(artifacts,'search-mobile-filters.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: history search/snippet/XSS; site/tag/date filters; pagination/realtime; old-message context/cursors/latest; drafts; stale search response; mobile layout.');
  } finally {
    await browser.close();
    for(const id of tags) await api.delete('/api/admin/tags/'+id,{headers});
    for(const site of sites) await api.delete('/api/admin/sites/'+site.id,{headers});
    await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
