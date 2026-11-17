const { chromium, request, expect } = require('playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = 'http://127.0.0.1:20101';
  const artifacts = process.env.MSGHUB_TEST_ARTIFACTS || '/tmp/msghub-browser';
  fs.mkdirSync(artifacts, {recursive:true});
  const api = await request.newContext({baseURL:base});
  const login = await api.post('/api/admin/login', {data:{password:'acceptance-only'}});
  const token = (await login.json()).token;
  assert(token);
  const headers = {Authorization:'Bearer '+token};
  async function call(method, url, data) {
    const res = await api[method](url, {headers,data});
    assert(res.ok(), url+': '+res.status()+' '+await res.text());
    return res.json();
  }
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  const contexts = [], visitors = [], sites = [], tags = [], errors = [];
  try {
    const site = await call('post','/api/admin/sites',{name:'标签验收',require_contact:false,ai_enabled:false});
    sites.push(site.id);
    const convs = [];
    for(let i=0;i<3;i++) {
      const v = await browser.newPage(); visitors.push(v);
      await v.goto(base+'/api/health');
      await v.evaluate(({base,key,i})=>new Promise((resolve,reject)=>{
        const ws=window.visitorSocket=new WebSocket(base.replace('http','ws')+'/api/chat/ws');
        window.visitorEvents=[];
        ws.onopen=()=>ws.send(JSON.stringify({type:'hello',site_key:key}));
        ws.onerror=reject;
        ws.onmessage=e=>{
          const d=JSON.parse(e.data); window.visitorEvents.push(d);
          if(d.type==='init') ws.send(JSON.stringify({type:'msg',content:'标签验收咨询 '+i}));
          if(d.type==='msg') resolve();
        };
      }),{base,key:site.site_key,i});
      convs.push((await call('get','/api/admin/conversations?site_id='+site.id)).items[0]);
    }
    async function adminPage() {
      const ctx=await browser.newContext({viewport:{width:1440,height:900}}); contexts.push(ctx);
      await ctx.addInitScript(token=>{
        localStorage.setItem('msghub_token',token);
        const NativeWebSocket=window.WebSocket;
        window.WebSocket=class extends NativeWebSocket {
          constructor(...args) {super(...args); if(String(args[0]).includes('/api/admin/ws')) window.adminSocket=this;}
        };
      },token);
      const p=await ctx.newPage();
      p.on('pageerror',e=>errors.push(e.message));
      await p.goto(base);
      await expect(p.locator('#liveState')).toHaveText('● 实时连接正常');
      return p;
    }
    const page=await adminPage(), observer=await adminPage();
    const row=(p,c)=>p.locator('.conv[data-id="'+c.id+'"]');
    const open=async(p,c)=>{
      await row(p,c).click();
      await expect(p.locator('#chatHead h3')).toContainText('#'+c.id+' ');
    };
    await row(page,convs[0]).locator('.conv-check').check();
    await expect(page.locator('#chatPane .chat-empty')).toBeVisible();
    await expect(page.locator('#convSelectionCount')).toHaveText('已选 1 条');
    await page.locator('#manageTags').click();
    const label='<b>重点客户</b>';
    await page.locator('#newTagName').fill(label);
    await page.locator('#tagCreateForm button').click();
    await expect(page.locator('#tagCatalog input').last()).toHaveValue(label);
    const tag=(await call('get','/api/admin/tags')).find(t=>t.name===label); tags.push(tag.id);
    await expect(observer.locator('#cTag option[value="'+tag.id+'"]')).toHaveText(label);
    await page.locator('#tagManager [data-close]').click();
    await page.locator('#convSelectAll').check();
    await expect(page.locator('#convSelectionCount')).toHaveText('已选 3 条');
    await page.locator('#batchTag').selectOption(String(tag.id));
    await page.locator('#tagBatchForm [value=add]').click();
    for(const c of convs) {
      await expect(row(page,c).locator('.conv-label')).toHaveText(label);
      await expect(row(observer,c).locator('.conv-label')).toHaveText(label);
    }
    assert.equal(await page.locator('.conv-label b').count(),0);
    await expect(page.locator('#tagBatchForm')).toBeHidden();
    const keep=await call('post','/api/admin/tags',{name:'售后跟进验收'}); tags.push(keep.id);
    await open(page,convs[0]); await open(observer,convs[0]);
    await page.locator('#addConvTag').selectOption(String(keep.id));
    await expect(observer.locator('[data-remove-tag="'+keep.id+'"]')).toBeVisible();
    await page.locator('#replyInput').fill('回复草稿保留');
    await page.locator('.conv-notes summary').click();
    await page.locator('#convNoteInput').fill('备注草稿保留');
    await page.locator('#cTag').selectOption(String(tag.id));
    await expect(page.locator('.conv')).toHaveCount(3);
    await row(page,convs[0]).locator('.conv-check').check();
    await page.locator('#batchTag').selectOption(String(tag.id));
    await page.locator('#tagBatchForm [value=remove]').click();
    await expect(row(page,convs[0])).toHaveCount(0);
    await expect(page.locator('.conv')).toHaveCount(2);
    await expect(page.locator('#conversationTags [data-remove-tag]')).toHaveCount(1);
    await expect(observer.locator('#conversationTags [data-remove-tag]')).toHaveCount(1);
    await expect(page.locator('#replyInput')).toHaveValue('回复草稿保留');
    await expect(page.locator('#convNoteInput')).toHaveValue('备注草稿保留');
    await page.locator('#manageTags').click();
    const edit=page.locator('.tag-edit[data-tag-id="'+keep.id+'"]');
    await edit.locator('input').fill('重新命名验收');
    await edit.locator('[type=submit]').click();
    await expect(observer.locator('#conversationTags')).toContainText('重新命名验收');
    await expect(page.locator('#replyInput')).toHaveValue('回复草稿保留');
    await expect(page.locator('#convNoteInput')).toHaveValue('备注草稿保留');
    page.once('dialog',d=>d.accept());
    await page.locator('[data-delete-tag="'+keep.id+'"]').click();
    await expect(observer.locator('#conversationTags [data-remove-tag]')).toHaveCount(0);
    await page.locator('#tagManager [data-close]').click();
    // Recover metadata missed during an admin WebSocket disconnection.
    await observer.evaluate(()=>window.adminSocket.close());
    await call('put','/api/admin/conversations/'+convs[0].id+'/tags/'+tag.id);
    await expect(observer.locator('#conversationTags [data-remove-tag="'+tag.id+'"]')).toBeVisible();
    await expect(row(page,convs[0])).toBeVisible();
    await page.locator('[data-remove-tag="'+tag.id+'"]').click();
    await expect(row(page,convs[0])).toHaveCount(0);
    // Removing the filtered tag from every remaining row yields a genuine empty state.
    await page.locator('#convSelectAll').check();
    await page.locator('#batchTag').selectOption(String(tag.id));
    await page.locator('#tagBatchForm [value=remove]').click();
    await expect(page.locator('.conv')).toHaveCount(0);
    await expect(page.locator('#convList')).toContainText('暂无符合条件的对话');
    await page.locator('#cTag').selectOption('');
    await expect(page.locator('.conv')).toHaveCount(3);
    // Long multi-label rows must fit both the sidebar and the small conversation pane.
    for(let i=0;i<3;i++) {
      const t=await call('post','/api/admin/tags',{name:'长标签手机布局验收'+i+'测试客户需求'}); tags.push(t.id);
      await call('put','/api/admin/conversations/'+convs[0].id+'/tags/'+t.id);
    }
    await expect(page.locator('#conversationTags [data-remove-tag]')).toHaveCount(3);
    await page.locator('.conv-notes summary').click();
    await expect(page.locator('#toast')).toBeHidden();
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]) {
      await page.setViewportSize(size);
      const layout=await page.evaluate(()=>({width:document.documentElement.scrollWidth,
        reply:document.querySelector('#replyBtn').getBoundingClientRect().bottom,
        messages:document.querySelector('#chatMsgs').getBoundingClientRect().height,height:innerHeight}));
      assert(layout.width<=size.width && layout.reply<=layout.height && layout.messages>50,
        JSON.stringify({size,layout}));
      await page.screenshot({path:path.join(artifacts,'tags-'+size.width+'.png'),fullPage:true});
    }
    await page.locator('[data-act=back]').click();
    await page.locator('#convSelectAll').check();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    for(const selector of ['#manageTags','#convSelectionCount','#tagBatchForm [value=remove]']) {
      const bounds=await page.locator(selector).boundingBox();
      assert(bounds.x>=0 && bounds.x+bounds.width<=320,selector+' clipped: '+JSON.stringify(bounds));
    }
    await page.screenshot({path:path.join(artifacts,'tags-mobile-list.png'),fullPage:true});
    await page.locator('#manageTags').click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(artifacts,'tags-mobile-manager.png'),fullPage:true});
    for(const visitor of visitors) {
      const events=await visitor.evaluate(()=>window.visitorEvents);
      assert(!JSON.stringify(events).includes('重点客户'));
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: tags catalog/escaping; single/bulk add/remove; filters/empty state; two-admin sync/reconnect; draft preservation; desktop/mobile layout.');
  } finally {
    for(const v of visitors) await v.close();
    for(const ctx of contexts) await ctx.close();
    for(const id of tags) await api.delete('/api/admin/tags/'+id,{headers});
    for(const id of sites) await api.delete('/api/admin/sites/'+id,{headers});
    await browser.close(); await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
