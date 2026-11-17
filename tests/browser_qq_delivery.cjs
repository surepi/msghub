// Isolated fixtures and UI-only mocked retry responses; never calls a real QQ API.
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
  const site=await (await api.post('/api/admin/sites',{headers,data:{name:'QQ记录验收 <img src=x onerror="window.qqXss=true">',require_contact:false,ai_enabled:false}})).json();
  assert(site.id);
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const errors=[];
  try {
    const fixture=JSON.parse(execFileSync('docker',['exec','-i','msghub-acceptance','python','-',String(site.id)],{encoding:'utf8',input:`
import json, os, sqlite3, sys, secrets
from app.config import DB_PATH
assert os.environ['DATA_DIR']=='/tmp/acceptance' and os.environ['ADMIN_PASSWORD']=='acceptance-only'
assert not os.environ.get('QQ_APP_ID') and not os.environ.get('QQ_APP_SECRET')
with sqlite3.connect(DB_PATH) as db:
    v=db.execute('INSERT INTO visitors (token) VALUES (?)',(secrets.token_hex(16),)).lastrowid
    c=db.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(int(sys.argv[1]),v)).lastrowid
    jobs={}
    for i in range(25):
        status='uncertain' if i==24 else 'failed' if i==23 else 'cancelled' if i==22 else 'sent'
        jid=db.execute('INSERT INTO qq_deliveries (conversation_id,recipient,status,attempts,last_error) VALUES (?,?,?,?,?)',(c,'private-test-openid',status,2,'模拟失败 <img src=x onerror="window.qqXss=true">' if status=='failed' else '')).lastrowid
        mid=db.execute("INSERT INTO chat_messages (conversation_id,sender,content,qq_state) VALUES (?,'visitor',?,?)",(c,'来源内容 <script>window.qqXss=true</script> '+str(i),'sent' if status=='sent' else 'pending')).lastrowid
        db.execute('INSERT INTO qq_delivery_messages (delivery_id,message_id) VALUES (?,?)',(jid,mid))
        db.execute("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content,status,attempts,qq_message_id) VALUES (?,0,'text',?,?,2,?)",(jid,'分段内容 <img src=x onerror="window.qqXss=true">',status if status in ('uncertain','failed','sent') else 'pending','fake-id' if status=='sent' else ''))
        jobs[status]=jid
    print(json.dumps({'cid':c,**jobs}))
`}));
    const ctx=await browser.newContext({viewport:{width:1440,height:900}});
    await ctx.addInitScript(t=>localStorage.setItem('msghub_token',t),token);
    const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/#sites');
    await page.locator('[data-qq=deliveries]').click();
    await expect(page.locator('#qqDeliveryList .qq-delivery')).toHaveCount(20);
    await expect(page.locator('#qqDeliveryPager')).toContainText('1/2');
    await page.locator('[data-delivery-page="2"]').click();
    await expect(page.locator('#qqDeliveryList .qq-delivery')).toHaveCount(5);
    await page.locator('#qqDeliveryStatus').selectOption('failed');
    await page.locator('#qqDeliveryFilters button').click();
    await expect(page.locator('#qqDeliveryList .qq-delivery')).toHaveCount(1);
    await page.locator('[data-delivery-id="'+fixture.failed+'"]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('模拟失败');
    assert.equal(await page.locator('#qqDeliveryDrawer img, #qqDeliveryDrawer script').count(),0);
    assert.equal(await page.evaluate(()=>window.qqXss),undefined);
    await page.locator('[data-delivery-retry]').click();
    await expect(page.locator('#toast')).toContainText('QQ 机器人未配置');
    await expect(page.locator('[data-delivery-retry]')).toBeEnabled();
    // Hold a synthetic retry, then refresh the detail: both actions must remain disabled.
    let release, begun;const held=new Promise(r=>release=r),started=new Promise(r=>begun=r);
    await page.route('**/api/admin/qq/deliveries/'+fixture.failed+'/retry',async route=>{
      begun();await held;await route.fulfill({json:{ok:true,status:'failed'}});
    });
    await page.locator('[data-delivery-retry]').click();await started;
    await page.locator('[data-delivery-back]').click();
    await page.locator('[data-delivery-id="'+fixture.failed+'"]').click();
    await expect(page.locator('[data-delivery-retry]')).toBeDisabled();
    release();await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('[data-delivery-retry]')).toBeEnabled();
    await page.locator('[data-delivery-back]').click();
    await page.locator('#qqDeliveryStatus').selectOption('uncertain');
    await page.locator('#qqDeliveryFilters button').click();
    await page.locator('[data-delivery-id="'+fixture.uncertain+'"]').click();
    page.once('dialog',d=>d.dismiss());
    await page.locator('[data-delivery-retry]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('结果未确认');
    for(const size of [{width:1440,height:900},{width:390,height:844},{width:320,height:568}]) {
      await page.setViewportSize(size);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const panel=page.locator('#qqDeliveryDrawer .drawer-panel');
      assert(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.screenshot({path:path.join(artifacts,'qq-delivery-'+size.width+'.png'),fullPage:true});
    }
    page.once('dialog',d=>d.accept());
    await page.locator('[data-delivery-confirm]').click();
    await expect(page.locator('#qqDeliveryDetail')).toContainText('客服人工确认收到');
    await expect(page.locator('[data-delivery-retry]')).toHaveCount(0);
    const detail=await (await api.get('/api/admin/qq/deliveries/'+fixture.uncertain,{headers})).json();
    assert.equal(detail.delivery.status,'sent');
    assert.equal(detail.parts[0].qq_message_id,'');
    assert(!JSON.stringify(detail).includes('private-test-openid'));
    await page.locator('[data-delivery-back]').click();
    await page.locator('#qqDeliveryStatus').selectOption('cancelled');
    await page.locator('#qqDeliveryFilters button').click();
    await page.locator('[data-delivery-id="'+fixture.cancelled+'"]').click();
    await expect(page.locator('[data-delivery-retry], [data-delivery-confirm]')).toHaveCount(0);
    // A detail response arriving after close/reopen must not replace the new list.
    await page.locator('[data-delivery-back]').click();
    let lateRelease,lateStart;const lateHeld=new Promise(r=>lateRelease=r),lateStarted=new Promise(r=>lateStart=r);
    await page.route('**/api/admin/qq/deliveries/'+fixture.cancelled,async route=>{
      const response=await route.fetch();lateStart();await lateHeld;await route.fulfill({response});
    });
    await page.locator('[data-delivery-id="'+fixture.cancelled+'"]').click();await lateStarted;
    await page.keyboard.press('Escape');
    await page.locator('[data-qq=deliveries]').click();
    lateRelease();await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('#qqDeliveryListView')).toBeVisible();
    await expect(page.locator('#qqDeliveryDetail')).toBeHidden();
    await page.locator('#qqDeliveryStatus').selectOption('');
    await page.locator('#qqDeliveryConv').fill('999999');
    await page.locator('#qqDeliveryFilters button').click();
    await expect(page.locator('#qqDeliveryList')).toContainText('暂无符合条件');
    assert.deepEqual(errors,[]);
    await ctx.close();
    console.log('PASS: QQ records pagination/filter/detail, escaping, disabled-QQ failure, retry action locking, uncertainty dialog and real manual confirmation, cancelled state, late-response isolation, desktop/mobile.');
  } finally {
    await browser.close();
    await api.delete('/api/admin/sites/'+site.id,{headers});await api.dispose();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
