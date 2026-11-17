// Disposable DB fixtures only; no production credentials and no QQ network calls.
const {chromium,request,expect}=require('playwright/test');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
  const base='http://127.0.0.1:20101',artifacts=process.env.MSGHUB_TEST_ARTIFACTS||'/tmp/msghub-browser';
  fs.mkdirSync(artifacts,{recursive:true});
  const api=await request.newContext({baseURL:base});
  const token=(await(await api.post('/api/admin/login',{data:{password:'acceptance-only'}})).json()).token;
  assert(token);const headers={Authorization:'Bearer '+token};
  const site=await(await api.post('/api/admin/sites',{headers,data:{name:'转人工验收',require_contact:false,ai_enabled:false}})).json();
  assert(site.id);
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const fixture=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',String(site.id)],{encoding:'utf8',input:`
import json,os,secrets,sys
from unittest.mock import patch
from app import db,chat,auto,qq,qq_delivery
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
assert not os.environ.get('QQ_APP_ID') and not os.environ.get('QQ_APP_SECRET')
items=[]
for state in ['failed','uncertain']:
    with db.get_db() as conn:
        vid=conn.execute('INSERT INTO visitors (token,name,contact) VALUES (?,?,?)',(secrets.token_hex(16),'待接管访客','customer@example.com')).lastrowid
        cid=conn.execute('INSERT INTO conversations (site_id,visitor_id,page_url) VALUES (?,?,?)',(int(sys.argv[1]),vid,'https://example.com/pricing')).lastrowid
        mid=conn.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,'visitor',?)",(cid,'想咨询套餐 <img src=x onerror="window.handoffXss=true">')).lastrowid
        conn.execute('UPDATE conversations SET auto_handled_upto=? WHERE id=?',(mid,cid))
        conn.execute("INSERT INTO conversation_notes (conversation_id,content) VALUES (?,'仅内部可见的备注')",(cid,))
    conv=chat.conv_summary(cid);msgs=auto._unhandled(cid)
    with db.get_db() as conn,patch.object(qq,'ENABLED',True):
        msgs=db.rows(conn.execute('SELECT * FROM chat_messages WHERE id=?',(mid,)))
        inbox=auto.archive(conn,conv,msgs,'客服忙碌；AI 自动应答未启用')
        conn.execute("UPDATE qq_agent SET openid='fake-test-agent' WHERE id=1")
        jid=qq_delivery.enqueue_handoff(conn,conv,msgs,'客服忙碌；AI 自动应答未启用',inbox)
        assert qq_delivery.enqueue_handoff(conn,conv,msgs,'客服忙碌',inbox)==jid
        conn.execute('UPDATE qq_deliveries SET status=?,attempts=1,last_error=? WHERE id=?',(state,'模拟发送失败' if state=='failed' else '模拟结果未确认',jid))
        conn.execute('UPDATE qq_delivery_parts SET status=?,attempts=1 WHERE delivery_id=?',(state,jid))
    items.append({'cid':cid,'mid':mid,'jid':jid,'inbox':inbox})
# Leave the running app disabled and unbound; fixtures can never trigger a send.
qq.update_agent(openid='')
print(json.dumps(items))
`}));
    const [failed,uncertain]=fixture;
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
    const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);
    await page.locator('.conv[data-id="'+failed.cid+'"]').click();
    await page.locator('#replyInput').fill('保留待接管会话的回复草稿');
    await page.locator('nav [href="#sites"]').click();
    async function openRecords(kind='handoff',status=''){
      await page.locator('[data-qq=deliveries]').click();
      await page.locator('#qqDeliveryKind').selectOption(kind);
      await page.locator('#qqDeliveryStatus').selectOption(status);
      await page.locator('#qqDeliveryFilters button').click();
    }
    await openRecords();
    await expect(page.locator('#qqDeliveryList .qq-delivery')).toHaveCount(2);
    await page.locator('#qqDeliveryKind').selectOption('visitor');
    await page.locator('#qqDeliveryFilters button').click();
    await expect(page.locator('#qqDeliveryList')).toContainText('暂无符合条件');
    await page.locator('#qqDeliveryKind').selectOption('handoff');
    await page.locator('#qqDeliveryStatus').selectOption('failed');
    await page.locator('#qqDeliveryFilters button').click();
    await expect(page.locator('#qqDeliveryList .qq-delivery')).toHaveCount(1);
    await page.locator('[data-delivery-id="'+failed.jid+'"]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('客服忙碌；AI 自动应答未启用');
    await expect(page.locator('#qqDeliveryDetail')).toContainText('访客需求（原话摘录，非 AI 总结）');
    await expect(page.locator('#qqDeliveryDetail')).toContainText('想咨询套餐 <img');
    await expect(page.locator('#qqDeliveryDetail')).not.toContainText('仅内部可见的备注');
    assert.equal(await page.locator('#qqDeliveryDetail img').count(),0);
    assert.equal(await page.evaluate(()=>window.handoffXss),undefined);
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]){
      await page.setViewportSize(size);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert(await page.locator('#qqDeliveryDrawer .drawer-panel').evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.screenshot({path:path.join(artifacts,'handoff-'+size.width+'.png'),fullPage:true});
    }
    await page.locator('[data-delivery-conv]').click();
    await expect(page.locator('#qqDeliveryDrawer')).toBeHidden();
    await expect(page.locator('#chatHead h3')).toContainText('#'+failed.cid+' ');
    await expect(page.locator('#replyInput')).toHaveValue('保留待接管会话的回复草稿');
    await page.setViewportSize({width:1440,height:900});
    await page.locator('nav [href="#sites"]').click();
    await openRecords('handoff','failed');
    await page.locator('[data-delivery-id="'+failed.jid+'"]').click();
    await page.locator('[data-delivery-inbox]').click();
    await expect(page.locator('#drawer')).toBeVisible();
    await expect(page.locator('#drawerBody')).toContainText('想咨询套餐');
    await page.locator('#drawer [data-close]').click();
    await api.delete('/api/admin/messages/'+failed.inbox,{headers});
    await openRecords('handoff','failed');
    await page.locator('[data-delivery-id="'+failed.jid+'"]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('来源留言已删除');
    await expect(page.locator('[data-delivery-inbox]')).toHaveCount(0);
    await page.locator('#qqDeliveryDrawer [data-close]').click();
    await openRecords('handoff','uncertain');
    await page.locator('[data-delivery-id="'+uncertain.jid+'"]').click();
    page.once('dialog',dialog=>dialog.accept());
    await page.locator('[data-delivery-confirm]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('客服人工确认收到');
    await expect(page.locator('[data-delivery-confirm]')).toHaveCount(0);
    assert.deepEqual(errors,[]);
    await ctx.close();
    console.log('PASS: handoff type/status filters, reasons and excerpt escaping, source conversation/inbox navigation, draft preservation, deleted inbox, manual confirmation, desktop/mobile layout.');
  } finally {
    await browser.close();await api.delete('/api/admin/sites/'+site.id,{headers});await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
