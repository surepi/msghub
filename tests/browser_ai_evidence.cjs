const {chromium,request,expect}=require('playwright/test');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
  const base='http://127.0.0.1:20101',artifacts=process.env.MSGHUB_TEST_ARTIFACTS||'/tmp/msghub-browser';
  fs.mkdirSync(artifacts,{recursive:true});
  const api=await request.newContext({baseURL:base});
  const token=(await(await api.post('/api/admin/login',{data:{password:'acceptance-only'}})).json()).token;
  assert(token);const headers={Authorization:'Bearer '+token};
  const site=await(await api.post('/api/admin/sites',{headers,data:{name:'AI依据验收',require_contact:false,ai_enabled:false}})).json();
  assert(site.id);const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const f=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',String(site.id)],{encoding:'utf8',input:`
import os,json,secrets,sys
from app import db,chat,auto,qq_delivery
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
assert not os.environ.get('QQ_APP_ID') and not os.environ.get('QQ_APP_SECRET')
with db.get_db() as conn:
    vid=conn.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    cid=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),vid)).lastrowid
    mid=conn.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,'visitor','套餐 pricing')",(cid,)).lastrowid
    legacy=chat.add_message(cid,'ai','旧 AI 消息，没有快照',db=conn)['id']
    qid=conn.execute("INSERT INTO qa (site_id,question,answer) VALUES (?,'套餐 pricing','每月 100 元 <img src=x onerror=window.evidenceXss=true>')",(int(sys.argv[1]),)).lastrowid
    conn.execute("INSERT INTO kb_pages (site_id,url,title,text) VALUES (?,'https://example.com/pricing','套餐网页 <img src=x onerror=window.evidenceXss=true>','套餐 pricing 每月费用')",(int(sys.argv[1]),))
context,sources=auto.retrieve_sources(int(sys.argv[1]),'套餐 pricing')
# Also exercise unsafe URL rendering using an explicitly synthetic snapshot.
sources.append({'type':'page','scope':'本站','ref':len(sources)+1,'title':'危险链接作为文字','url':'javascript:window.evidenceXss=true','snippet':'仅展示文字快照','score':1})
with db.get_db() as conn:
    aid_msg=chat.add_message(cid,'ai','套餐每月 100 元。',db=conn)['id']
    aid=auto.save_decision(conn,cid,mid,'客服未及时回复','套餐 pricing','套餐每月 100 元。','answered',{'context':context,'sources':sources,'model':'fixture-model'},message_id=aid_msg)
    for i in range(11):
        vm=chat.add_message(cid,'visitor','测试额外问题 '+str(i),db=conn)['id']
        auto.save_decision(conn,cid,vm,'客服未及时回复','测试额外问题 '+str(i),'','api_error',{})
    vm=chat.add_message(cid,'visitor','我想找人工客服',db=conn)['id']
    inbox=auto.archive(conn,{'id':cid,'visitor_id':vid,'site_id':int(sys.argv[1]),'page_url':''},[{'content':'我想找人工客服','msg_type':'text'}],'访客要求人工')
    jid=conn.execute("INSERT INTO qq_deliveries (conversation_id,recipient,kind,status,reason,inbox_message_id) VALUES (?,'fake-agent','handoff','cancelled','模型判断访客要求人工',?)",(cid,inbox)).lastrowid
    hid=auto.save_decision(conn,cid,vm,'客服未及时回复','我想找人工客服','','visitor_requested',{'context':'','sources':[],'model':'fixture-model'},inbox_id=inbox,delivery_id=jid)
    conn.execute('UPDATE conversations SET auto_handled_upto=? WHERE id=?',(vm,cid))
    # Snapshots survive changes to the owning documents.
    conn.execute('DELETE FROM qa WHERE id=?',(qid,))
    conn.execute('DELETE FROM kb_pages WHERE site_id=?',(int(sys.argv[1]),))
print(json.dumps({'cid':cid,'aid':aid,'message':aid_msg,'legacy':legacy,'hid':hid,'jid':jid}))
`}));
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
    const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);await expect(page.locator('#liveState')).toHaveText('● 实时连接正常');
    await page.locator('.conv[data-id="'+f.cid+'"]').click();
    await page.locator('#replyInput').fill('查看依据期间保留回复草稿');
    await page.locator('[data-act="ai-evidence"]').click();
    await expect(page.locator('#aiDecisionBody [data-ai-detail]')).toHaveCount(10);
    await expect(page.locator('#aiDecisionBody .pager')).toContainText('1/2');
    await page.locator('[data-ai-page="2"]').click();
    await expect(page.locator('#aiDecisionBody [data-ai-detail]')).toHaveCount(3);
    await page.locator('[data-ai-detail="'+f.aid+'"]').click();
    await expect(page.locator('#aiDecisionBody')).toContainText('每月 100 元 <img');
    await expect(page.locator('#aiDecisionBody')).toContainText('https://example.com/pricing');
    await expect(page.locator('#aiDecisionBody a[href="https://example.com/pricing"]')).toHaveCount(1);
    await expect(page.locator('#aiDecisionBody a[href^="javascript:"]')).toHaveCount(0);
    await expect(page.locator('#aiDecisionBody img')).toHaveCount(0);
    assert.equal(await page.evaluate(()=>window.evidenceXss),undefined);
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]){
      await page.setViewportSize(size);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert(await page.locator('#aiDecisionDrawer .drawer-panel').evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.screenshot({path:path.join(artifacts,'ai-evidence-'+size.width+'.png'),fullPage:true});
    }
    await page.keyboard.press('Escape');
    await expect(page.locator('#replyInput')).toHaveValue('查看依据期间保留回复草稿');
    await page.locator('[data-ai-message="'+f.message+'"]').click();
    await expect(page.locator('#aiDecisionBody')).toContainText('记录 #'+f.aid+' · AI 已回答');
    await page.keyboard.press('Escape');
    await page.locator('[data-ai-message="'+f.legacy+'"]').click();
    await expect(page.locator('#aiDecisionBody')).toContainText('此条历史消息或通知尚未保存资料快照');
    await page.locator('[data-ai-all]').click();
    await page.locator('[data-ai-detail="'+f.hid+'"]').click();
    await expect(page.locator('#aiDecisionBody')).toContainText('模型判断访客要求人工');
    await expect(page.locator('#aiDecisionBody')).toContainText('转人工分类由模型返回');
    await expect(page.locator('#aiDecisionBody')).toContainText('未检索到可提供的匹配资料');
    // A held detail response must not overwrite a newly reopened list.
    await page.locator('[data-ai-back]').click();
    let release,begin;const held=new Promise(r=>release=r),started=new Promise(r=>begin=r);
    await page.route('**/ai-decisions/'+f.hid,async route=>{const response=await route.fetch();begin();await held;await route.fulfill({response});});
    await page.locator('[data-ai-detail="'+f.hid+'"]').click();await started;
    await page.keyboard.press('Escape');
    await page.locator('[data-act="ai-evidence"]').click();
    release();await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('#aiDecisionBody .pager')).toContainText('1/2');
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1440,height:900});
    await page.locator('nav [href="#sites"]').click();await page.locator('[data-qq=deliveries]').click();
    await page.locator('[data-delivery-id="'+f.jid+'"]').click();
    await page.locator('[data-delivery-evidence]').click();
    await expect(page.locator('#qqDeliveryDrawer')).toBeHidden();
    await expect(page.locator('#aiDecisionBody')).toContainText('记录 #'+f.hid+' · 转人工');
    await page.keyboard.press('Escape');
    // The test panel uses the same structured rendering; mock the HTTP response only.
    await page.locator('nav [href="#ai"]').click();
    await page.route('**/api/admin/ai/test',route=>route.fulfill({json:{answer:'',handoff:true,reason:'模型判断资料不足',model:'fake-model',context:'[资料1] 快照',sources:[{type:'qa',id:1,ref:1,title:'测试资料 <img src=x>',scope:'本站',snippet:'问：套餐\n答：100 元'}]}}));
    await page.locator('#aiTestQ').fill('套餐价格');await page.locator('#aiTestBtn').click();
    await expect(page.locator('#aiTestOut')).toContainText('模型判断资料不足');
    await expect(page.locator('#aiTestOut')).toContainText('测试资料 <img src=x>');
    assert.equal(await page.locator('#aiTestOut img').count(),0);
    assert.deepEqual(errors,[]);await ctx.close();
    console.log('PASS: AI evidence pagination/message/delivery scopes, immutable QA/page snapshots, escaped content and safe URLs, no-source/legacy states, drafts, late-response isolation, AI test sources, desktop/mobile.');
  } finally {
    await browser.close();await api.delete('/api/admin/sites/'+site.id,{headers});await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
