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
  const site=await(await api.post('/api/admin/sites',{headers,data:{name:'分类验收',require_contact:false,ai_enabled:false}})).json();
  const tag=await(await api.post('/api/admin/tags',{headers,data:{name:'分类验收 <img src=x>'}})).json();
  assert(site.id&&tag.id);const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const f=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',String(site.id),String(tag.id)],{encoding:'utf8',input:`
import os,json,secrets,sys
from app import db,chat,triage,summaries
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
assert not os.environ.get('QQ_APP_ID') and not os.environ.get('QQ_APP_SECRET')
with db.get_db() as conn:
    vid=conn.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    cid=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),vid)).lastrowid
    vid=conn.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    empty=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),vid)).lastrowid
msg=chat.add_message(cid,'visitor','紧急故障 <img src=x onerror=window.triageXss=true>')
snap=summaries.snapshot(cid)
ev={'message_id':msg['id'],'quote':msg['content']}
suggestions=[{'id':int(sys.argv[2]),'name':'分类验收 <img src=x>',**ev}]
with db.get_db() as conn:
    aid=conn.execute("INSERT INTO conversation_triage (conversation_id,source_upto,status,model,category,category_evidence,priority,priority_evidence,tag_suggestions,sources,source_count) VALUES (?,?,'ready','fixture-model','support',?,'high',?,?,?,1)", (cid,msg['id'],json.dumps(ev),json.dumps(ev),json.dumps(suggestions),json.dumps(snap['sources']))).lastrowid
    jid=conn.execute("INSERT INTO qq_deliveries (conversation_id,recipient,kind,status,triage_id,summary) VALUES (?,'fake-agent','priority','cancelled',?,'紧急故障原话')",(cid,aid)).lastrowid
    conn.execute('UPDATE conversations SET auto_handled_upto=? WHERE id=?',(msg['id'],cid))
print(json.dumps({'cid':cid,'empty':empty,'aid':aid,'jid':jid}))
`}));
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
    const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);await expect(page.locator('#liveState')).toHaveText('● 实时连接正常');
    await expect(page.locator('.conv[data-id="'+f.cid+'"]')).toContainText('高优先级待处理');
    await expect(page.locator('.conv[data-id="'+f.cid+'"]')).toContainText('技术支持');
    await page.locator('.conv-filters summary').click();
    await page.locator('#cCategory').selectOption('support');await page.locator('#cPriority').selectOption('high');
    await expect(page.locator('#convList .conv')).toHaveCount(1);
    await page.locator('.conv[data-id="'+f.cid+'"]').click();
    await page.locator('#replyInput').fill('分类期间保留草稿');await page.locator('[data-act="triage"]').click();
    await expect(page.locator('#triageBody')).toContainText('AI 分类：技术支持');
    await expect(page.locator('#triageBody')).toContainText('分类验收 <img src=x>');
    assert.equal(await page.locator('#triageBody img').count(),0);assert.equal(await page.evaluate(()=>window.triageXss),undefined);
    await page.locator('[data-triage-apply]').click();
    await expect(page.locator('#triageBody')).toContainText('当前标签：分类验收 <img src=x>');
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]){
      await page.setViewportSize(size);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert(await page.locator('#triageDrawer .drawer-panel').evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.screenshot({path:path.join(artifacts,'triage-'+size.width+'.png'),fullPage:true});
    }
    await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:900});
    await expect(page.locator('#replyInput')).toHaveValue('分类期间保留草稿');
    await page.locator('[data-act="triage"]').click();await page.locator('[data-triage-dismiss]').click();
    await expect(page.locator('#triageBody')).toContainText('高（已撤销）');
    await page.keyboard.press('Escape');await expect(page.locator('#convList .conv')).toHaveCount(0);
    await page.locator('#clearConvFilters').click();
    await page.locator('.conv[data-id="'+f.empty+'"]').click();await page.locator('[data-act="triage"]').click();
    await expect(page.locator('#triageBody')).toContainText('暂无分类');
    await page.locator('[data-triage-generate]').click();await expect(page.locator('#triageBody')).toContainText('请开启全局和本站 AI');
    await page.locator('[data-triage-reload]').click();
    let release,begin;const held=new Promise(r=>release=r),started=new Promise(r=>begin=r);
    const result=await(await api.get('/api/admin/conversations/'+f.cid+'/triage',{headers})).json();
    await page.route('**/conversations/'+f.empty+'/triage',async route=>{
      if(route.request().method()!=='POST')return route.continue();begin();await held;await route.fulfill({json:result});
    });
    await page.locator('[data-triage-generate]').click();await started;
    await page.keyboard.press('Escape');await page.locator('.conv[data-id="'+f.cid+'"]').click();await page.locator('[data-act="triage"]').click();
    release();await page.unrouteAll({behavior:'wait'});await expect(page.locator('#triageBody')).toContainText('对话 #'+f.cid);
    await page.keyboard.press('Escape');
    await page.locator('nav [href="#sites"]').click();await page.locator('[data-qq=deliveries]').click();
    await page.locator('#qqDeliveryKind').selectOption('priority');
    await page.locator('[data-delivery-id="'+f.jid+'"]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('高优先级提醒');
    await page.locator('[data-delivery-triage]').click();await expect(page.locator('#triageBody')).toContainText('技术支持');
    await page.keyboard.press('Escape');await page.locator('nav [href="#ai"]').click();
    const form=page.locator('#aiForm');await form.locator('[name="triage_enabled"]').check();await form.locator('[name="auto_tags"]').check();
    await form.locator('button[type=submit]').click();
    await expect(form.locator('[name="triage_enabled"]')).toBeChecked();await expect(form.locator('[name="auto_tags"]')).toBeChecked();
    const settings=await(await api.get('/api/admin/ai/settings',{headers})).json();assert(settings.triage_enabled&&settings.auto_tags);
    assert.deepEqual(errors,[]);await ctx.close();
    console.log('PASS: classification/priority filters and badges, visitor evidence escaping, applying tags/dismissing priority, drafts, empty/error states, late-response isolation, QQ evidence link, settings toggles, desktop/mobile.');
  }finally{
    await browser.close();await api.put('/api/admin/ai/settings',{headers,data:{enabled:false,triage_enabled:false,auto_tags:false}});
    await api.delete('/api/admin/sites/'+site.id,{headers});await api.delete('/api/admin/tags/'+tag.id,{headers});await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
