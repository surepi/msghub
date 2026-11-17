const {chromium,request,expect}=require('playwright/test');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
  const base='http://127.0.0.1:20101',artifacts='/tmp/msghub-browser';
  fs.mkdirSync(artifacts,{recursive:true});
  const api=await request.newContext({baseURL:base});
  const token=(await(await api.post('/api/admin/login',{data:{password:'acceptance-only'}})).json()).token;
  assert(token);const headers={Authorization:'Bearer '+token};
  const site=await(await api.post('/api/admin/sites',{headers,data:{name:'总结验收',require_contact:false,ai_enabled:false}})).json();
  assert(site.id);const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const f=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',String(site.id)],{encoding:'utf8',input:`
import os,json,secrets,sys
from app import db,chat,summaries,auto
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
assert not os.environ.get('QQ_APP_ID') and not os.environ.get('QQ_APP_SECRET')
with db.get_db() as conn:
    vid=conn.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    cid=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),vid)).lastrowid
    vid=conn.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    empty=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),vid)).lastrowid
msg=chat.add_message(cid,'visitor','需要报价，QQ 12345678 <img src=x onerror=window.summaryXss=true>')
snap=summaries.snapshot(cid)
payload={**snap,'conversation_id':cid,'model':'fixture-model','status':'ready','error_code':'','summary':'客户需要报价。<img src=x onerror=window.summaryXss=true>','needs':[{'value':'报价 <img src=x>','quote':'需要报价','message_id':msg['id']}],'contacts':[{'value':'12345678','quote':'QQ 12345678','message_id':msg['id']}]}
with db.get_db() as conn:
    item=summaries.save(conn,payload)
    jid=conn.execute("INSERT INTO qq_deliveries (conversation_id,recipient,kind,status,summary_id) VALUES (?,'fake-agent','handoff','cancelled',?)",(cid,item['id'])).lastrowid
chat.add_message(cid,'visitor','新增需求')
snap=summaries.snapshot(cid)
with db.get_db() as conn:
    failed=summaries.save(conn,{**snap,'conversation_id':cid,'model':'fixture-model','status':'error','error_code':'api_error','summary':'','needs':[],'contacts':[]})
    conn.execute('UPDATE conversations SET auto_handled_upto=? WHERE id=?',(snap['source_upto'],cid))
print(json.dumps({'cid':cid,'empty':empty,'sid':item['id'],'failed':failed['id'],'jid':jid}))
`}));
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
    const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);await expect(page.locator('#liveState')).toHaveText('● 实时连接正常');
    await page.locator('.conv[data-id="'+f.cid+'"]').click();
    await page.locator('#replyInput').fill('总结期间保留回复草稿');
    await page.locator('[data-act="summary"]').click();
    await expect(page.locator('#summaryBody')).toContainText('AI 服务暂不可用');
    await page.locator('#summaryHistory').selectOption(String(f.sid));
    await expect(page.locator('#summaryBody')).toContainText('客户需要报价。<img');
    await expect(page.locator('#summaryBody')).toContainText('QQ 12345678');
    await expect(page.locator('#summaryBody')).toContainText('此快照之后有新消息');
    assert.equal(await page.locator('#summaryBody img').count(),0);
    assert.equal(await page.evaluate(()=>window.summaryXss),undefined);
    await page.locator('#summaryBody details summary').click();
    await expect(page.locator('#summaryBody details')).toContainText('访客');
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]){
      await page.setViewportSize(size);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert(await page.locator('#summaryDrawer .drawer-panel').evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.screenshot({path:path.join(artifacts,'summary-'+size.width+'.png'),fullPage:true});
    }
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1440,height:900});
    await expect(page.locator('#replyInput')).toHaveValue('总结期间保留回复草稿');
    await page.locator('.conv[data-id="'+f.empty+'"]').click();await page.locator('[data-act="summary"]').click();
    await expect(page.locator('#summaryBody')).toContainText('尚无会话总结');
    await page.locator('[data-summary-generate]').click();
    await expect(page.locator('#summaryBody')).toContainText('请开启全局和本站 AI');
    await page.locator('[data-summary-reload]').click();await expect(page.locator('#summaryBody')).toContainText('尚无会话总结');
    // Held POST response cannot overwrite another conversation after closing/reopening.
    let release,begin;const held=new Promise(r=>release=r),started=new Promise(r=>begin=r);
    const result=await(await api.get('/api/admin/conversations/'+f.cid+'/summary?summary_id='+f.sid,{headers})).json();
    await page.route('**/conversations/'+f.empty+'/summary',async route=>{
      if(route.request().method()!=='POST') return route.continue();
      begin();await held;await route.fulfill({json:result});
    });
    await page.locator('[data-summary-generate]').click();await started;
    await page.keyboard.press('Escape');await page.locator('.conv[data-id="'+f.cid+'"]').click();await page.locator('[data-act="summary"]').click();
    release();await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('#summaryBody')).toContainText('AI 服务暂不可用');
    // Mock generation response; real serialization/extraction is tested with a local HTTP model server.
    await page.route('**/conversations/'+f.cid+'/summary',route=>route.request().method()==='POST'?route.fulfill({json:result}):route.continue());
    await page.locator('[data-summary-generate]').click();
    await expect(page.locator('#summaryBody')).toContainText('客户需要报价。<img');
    await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:900});
    await page.locator('nav [href="#sites"]').click();await page.locator('[data-qq=deliveries]').click();
    await page.locator('[data-delivery-id="'+f.jid+'"]').click();await page.locator('[data-delivery-summary]').click();
    await expect(page.locator('#qqDeliveryDrawer')).toBeHidden();
    await expect(page.locator('#summaryBody')).toContainText('客户需要报价。<img');
    await expect(page.locator('#summaryHistory')).toHaveValue(String(f.sid));
    assert.deepEqual(errors,[]);await ctx.close();
    console.log('PASS: summary snapshots/history/stale/error/empty states, visitor quotes and escaping, draft preservation, generation and late-response isolation, QQ snapshot link, desktop/mobile.');
  }finally{await browser.close();await api.delete('/api/admin/sites/'+site.id,{headers});await api.dispose();}
})().catch(e=>{console.error(e);process.exitCode=1;});
