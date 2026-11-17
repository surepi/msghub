const { chromium, request } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = 'http://127.0.0.1:20101';
  const artifacts = process.env.MSGHUB_TEST_ARTIFACTS || '/tmp/msghub-browser';
  fs.mkdirSync(artifacts, {recursive:true});
  const api = await request.newContext({ baseURL: base });
  const login = await api.post('/api/admin/login', {data:{password:'acceptance-only'}});
  const token = (await login.json()).token;
  assert(token);
  const headers = {Authorization:'Bearer ' + token};
  async function call(method, path, data) {
    const r = await api[method](path, {headers, data});
    assert(r.ok(), path + ': ' + r.status() + ' ' + await r.text());
    return r.json();
  }
  const browser = await chromium.launch({headless:true, args:['--no-sandbox']});
  const errors = [];
  const ctx = await browser.newContext({viewport:{width:1440,height:900}});
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  const sites = [];
  const visitors = [];
  const replies = [];
  try {
    for (const name of ['验收官网', '验收商城']) {
      sites.push(await call('post','/api/admin/sites',{name, require_contact:false, ai_enabled:false}));
    }
    const convs = [];
    for (let i=0; i<sites.length; i++) {
      const v = await browser.newPage();
      visitors.push(v);
      await v.goto(base+'/api/health');
      await v.evaluate(({base, key, text}) => new Promise((resolve,reject) => {
        const ws = window.visitorSocket = new WebSocket(base.replace('http','ws')+'/api/chat/ws');
        window.visitorEvents = [];
        ws.onopen = () => ws.send(JSON.stringify({type:'hello',site_key:key}));
        ws.onerror = reject;
        ws.onmessage = e => {
          const d=JSON.parse(e.data);
          window.visitorEvents.push(d);
          if(d.type==='init') ws.send(JSON.stringify({type:'msg',content:text}));
          if(d.type==='msg') resolve();
        };
      }), {base, key:sites[i].site_key, text:'咨询问题 '+i});
      const listing = await call('get','/api/admin/conversations?site_id='+sites[i].id);
      convs.push(listing.items[0]);
    }
    await page.goto(base);
    await page.locator('#adminPassword').fill('acceptance-only');
    await page.locator('#loginForm button[type=submit]').click();
    await page.locator('#app').waitFor({state:'visible'});
    await page.locator('[href="#replies"]').click();
    await page.locator('#replyNew').click();
    await page.locator('#quickReplyForm [name=title]').fill('通用问候');
    await page.locator('#quickReplyForm [name=content]').fill('您好，请问有什么可以帮您？');
    await page.locator('#quickReplyForm button[type=submit]').click();
    await page.locator('.quick-reply').filter({hasText:'通用问候'}).waitFor();
    const global = (await call('get','/api/admin/quick-replies')).find(x=>x.title==='通用问候');
    replies.push(global.id);
    const scoped = await call('post','/api/admin/quick-replies',{title:'官网专用',content:'官网回复',site_id:sites[0].id});
    replies.push(scoped.id);
    const other = await call('post','/api/admin/quick-replies',{title:'商城专用',content:'商城回复',site_id:sites[1].id});
    replies.push(other.id);
    await page.reload();
    await page.locator('.quick-reply').filter({hasText:'通用问候'}).waitFor();
    await page.locator('.quick-reply').filter({hasText:'通用问候'}).locator('[data-qr=edit]').click();
    await page.locator('#quickReplyForm [name=content]').fill('您好，很高兴为您服务。');
    await page.locator('#quickReplyForm button[type=submit]').click();
    await page.locator('.quick-reply').filter({hasText:'您好，很高兴为您服务。'}).waitFor();
    await page.locator('#replyScope').selectOption('global');
    await page.waitForFunction(()=>document.querySelectorAll('.quick-reply').length===1);
    await page.locator('[href="#chats"]').click();
    const selectConv = async (c) => {
      await page.locator('.conv[data-id="'+c.id+'"]').click();
      await page.waitForFunction(id=>document.querySelector('#chatHead h3')?.textContent.startsWith('#'+id+' '), c.id);
    };
    await selectConv(convs[0]);
    await page.waitForFunction(()=>document.querySelector('#replyPicker').options.length===3);
    const labels = await page.locator('#replyPicker option').allTextContents();
    assert(labels.some(x=>x.includes('官网专用')));
    assert(!labels.some(x=>x.includes('商城专用')));
    const before = await call('get','/api/admin/conversations/'+convs[0].id);
    await page.locator('#replyPicker').selectOption(String(global.id));
    assert.equal(await page.locator('#replyInput').inputValue(),'您好，很高兴为您服务。');
    assert.deepEqual((await call('get','/api/admin/conversations/'+convs[0].id)).messages,before.messages);
    await page.locator('.conv-notes summary').click();
    const privateText = '仅内部记录 <img src=x onerror="window.noteXss=true">';
    await page.locator('#convNoteInput').fill(privateText);
    await page.locator('#noteForm button').click();
    await page.locator('.internal-note p').filter({hasText:privateText}).waitFor();
    assert.equal(await page.evaluate(()=>window.noteXss),undefined);
    assert.equal(await page.locator('#convNotes img').count(),0);
    assert(!JSON.stringify(await visitors[0].evaluate(()=>window.visitorEvents)).includes('仅内部记录'));
    await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});
    await page.reload();
    await selectConv(convs[0]);
    await page.locator('.conv-notes summary').click();
    await page.locator('.internal-note p').filter({hasText:privateText}).waitFor();
    // Hold the note response while changing conversations.
    let releaseNote;
    const heldNote = new Promise(r=>releaseNote=r);
    let noteRequested;
    const noteStarted = new Promise(r=>noteRequested=r);
    const notePath = '**/api/admin/conversations/'+convs[0].id+'/notes';
    await page.route(notePath,async route=>{
      const response=await route.fetch();
      noteRequested();
      await heldNote;
      await route.fulfill({response});
    });
    await page.locator('#convNoteInput').fill('切换会话期间保存');
    await page.locator('#noteForm button').click();
    await noteStarted;
    await selectConv(convs[1]);
    releaseNote();
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#convNotes .internal-note').count(),0);
    await page.unroute(notePath);
    // The same guarantee applies to ordinary replies.
    await selectConv(convs[0]);
    let releaseReply, replyRequested;
    const heldReply = new Promise(r=>releaseReply=r);
    const replyStarted = new Promise(r=>replyRequested=r);
    const replyPath = '**/api/admin/conversations/'+convs[0].id+'/messages';
    await page.route(replyPath,async route=>{
      const response=await route.fetch();
      replyRequested();
      await heldReply;
      await route.fulfill({response});
    });
    await page.locator('#replyInput').fill('给官网访客的专属回复');
    await page.locator('#replyBtn').click();
    await replyStarted;
    await selectConv(convs[1]);
    releaseReply();
    await page.waitForTimeout(150);
    assert(!(await page.locator('#chatMsgs').textContent()).includes('给官网访客的专属回复'));
    await page.unroute(replyPath);
    // Navigation and composer stay within desktop and phone viewports.
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]) {
      await page.setViewportSize(size);
      await page.waitForTimeout(50);
      const layout=await page.evaluate(()=>({
        width:document.documentElement.scrollWidth,
        reply:document.querySelector('#replyBtn').getBoundingClientRect().bottom,
        height:innerHeight,
        messages:document.querySelector('#chatMsgs').getBoundingClientRect().height,
      }));
      assert(layout.width<=size.width,JSON.stringify({size,layout}));
      assert(layout.reply<=layout.height,JSON.stringify({size,layout}));
      assert(layout.messages>60,JSON.stringify({size,layout}));
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});
    for(const tab of ['messages','sites','ai','replies']) {
      const link=page.locator('nav [href="#'+tab+'"]');
      await link.scrollIntoViewIfNeeded();
      await link.click();
      await page.locator('#view-'+tab).waitFor({state:'visible'});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow in '+tab);
    }
    // Widget stays closed on first load and opens inside a phone viewport.
    await page.goto(base+'/api/health');
    await page.evaluate(({base,key})=>{
      const s=document.createElement('script');
      s.src=base+'/embed.js';
      s.setAttribute('data-site-key',key);
      document.body.appendChild(s);
    },{base,key:sites[0].site_key});
    await page.locator('.mh-fab').waitFor();
    assert.equal(await page.locator('.mh-float').isVisible(),false);
    await page.locator('.mh-fab').click();
    await page.locator('.mh-float').waitFor({state:'visible'});
    const widget=await page.locator('.mh-float').boundingBox();
    assert(widget.x>=0 && widget.x+widget.width<=390);
    assert(widget.y>=0 && widget.y+widget.height<=844);
    await page.screenshot({path:path.join(artifacts,'widget.png'),fullPage:true});
    await page.goto(base);
    await page.emulateMedia({colorScheme:'dark'});
    await page.locator('#app').waitFor({state:'visible'});
    await page.locator('#logout').click();
    await page.locator('#login').waitFor({state:'visible'});
    assert.equal(await page.locator('#chatPane').textContent(),'↗让对话从这里继续选择一条会话，查看访客需求并开始回复。');
    await page.screenshot({path:path.join(artifacts,'login-dark.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: template CRUD/scope/insertion; note persistence/privacy/escaping; delayed response isolation; desktop/mobile layout; widget.');
  } finally {
    for (const v of visitors) await v.close();
    for (const id of replies) await api.delete('/api/admin/quick-replies/'+id,{headers});
    for (const s of sites) await api.delete('/api/admin/sites/'+s.id,{headers});
    await browser.close();
    await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1});
