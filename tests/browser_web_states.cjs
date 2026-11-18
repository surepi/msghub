const { chromium, firefox, webkit, request, expect } = require("playwright/test");
const assert = require("node:assert/strict");

const base = "http://127.0.0.1:20101";
const engine = { chromium, firefox, webkit }[process.env.MSGHUB_BROWSER || "chromium"];

async function mountWidget(page, key, form = false) {
  await page.goto(base);
  await page.evaluate(({ key, form }) => {
    const host = document.createElement("div");
    host.id = "widgetTest";
    document.body.appendChild(host);
    const script = document.createElement("script");
    script.src = "/embed.js";
    script.dataset.siteKey = key;
    if (form) {
      script.dataset.mode = "form";
      script.dataset.target = "#widgetTest";
    }
    document.head.appendChild(script);
  }, { key, form });
}

(async () => {
  const api = await request.newContext({ baseURL: base });
  const response = await api.post("/api/admin/login", { data: { password: "acceptance-only" } });
  assert(response.ok());
  const { token } = await response.json();
  const headers = { Authorization: "Bearer " + token };
  const site = await (await api.post("/api/admin/sites", {
    headers, data: { name: "网页状态验收", require_contact: false, ai_enabled: false }
  })).json();
  assert(site.id && site.site_key);
  const browser = await engine.launch({ headless: true });
  const errors = [];
  try {
    const admin = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    admin.on("pageerror", error => errors.push(error.message));
    await admin.addInitScript(token => {
      localStorage.setItem("msghub_token", token);
      const NativeSocket = window.WebSocket;
      window.WebSocket = class extends NativeSocket {
        constructor(url) {
          super(url);
          if (url.endsWith("/api/admin/ws")) window.adminSocket = this;
        }
      };
    }, token);

    let releaseStats;
    const waitStats = new Promise(resolve => { releaseStats = resolve; });
    await admin.route("**/api/admin/stats", async route => {
      await waitStats;
      await route.fulfill({ status: 503, json: { detail: "测试：统计暂不可用" } });
    });
    await admin.goto(base);
    await expect(admin.locator("#stats")).toHaveAttribute("aria-busy", "true");
    await expect(admin.locator("#liveState")).toHaveText("● 实时连接正常");
    releaseStats();
    await expect(admin.locator("#stats")).toContainText("统计加载失败");
    await admin.unroute("**/api/admin/stats");
    await admin.locator("#stats button").click();
    await expect(admin.locator("#stats")).toContainText("待回复对话");

    const visitor = await browser.newPage({ viewport: { width: 320, height: 568 } });
    visitor.on("pageerror", error => errors.push(error.message));
    // Drop the delivery receipt without forwarding this particular message to the server.
    await visitor.addInitScript(() => {
      const NativeSocket = window.WebSocket;
      window.WebSocket = class extends NativeSocket {
        send(data) {
          if (JSON.parse(data).content === "送达未确认的测试消息") this.close();
          else super.send(data);
        }
      };
    });
    await mountWidget(visitor, site.site_key);
    const fab = visitor.locator(".mh-fab");
    await expect(fab).toHaveAttribute("aria-expanded", "false");
    await fab.click();
    await expect(fab).toHaveAttribute("aria-expanded", "true");
    const composer = visitor.locator(".mh-foot textarea");
    await expect(visitor.locator(".mh-foot .mh-btn")).toBeEnabled();
    await expect(composer).not.toBeFocused();
    await composer.fill("访客首条验收消息");
    await visitor.locator(".mh-foot .mh-btn").click();

    const conversation = admin.locator(".conv").filter({ hasText: "访客首条验收消息" });
    await conversation.click();
    await expect(admin.locator("#chatMsgs")).toContainText("访客首条验收消息");
    const id = await conversation.getAttribute("data-id");
    await admin.locator("#replyInput").fill("管理员草稿");
    await admin.evaluate(() => window.adminSocket.close());
    await expect(admin.locator("#chatLinkState")).toContainText("正在重连", { timeout: 2500 });
    const missedMessage = await api.post(`/api/admin/conversations/${id}/messages`, {
      headers, data: { content: "断线期间的回复" }
    });
    assert(missedMessage.ok());
    await expect(admin.locator("#chatMsgs")).toContainText("断线期间的回复");
    await expect(admin.locator("#chatMsgs .msg").filter({ hasText: "断线期间的回复" })).toHaveCount(1);
    await expect(admin.locator("#replyInput")).toHaveValue("管理员草稿");
    await admin.setViewportSize({ width: 390, height: 844 });
    await admin.evaluate(() => {
      Object.defineProperty(window.visualViewport, "height", { configurable: true, value: 330 });
      window.visualViewport.dispatchEvent(new Event("resize"));
    });
    await expect(admin.locator("body")).toHaveClass(/keyboard-open/);
    assert((await admin.locator("#replyBtn").boundingBox()).y + 30 <= 330);
    await admin.evaluate(() => {
      delete window.visualViewport.height;
      window.visualViewport.dispatchEvent(new Event("resize"));
    });

    await composer.fill("送达未确认的测试消息");
    await visitor.locator(".mh-foot .mh-btn").click();
    const failed = visitor.locator(".mh-row.failed");
    await expect(failed).toContainText("送达未确认");
    await expect(visitor.locator(".mh-foot .mh-btn")).toBeEnabled();
    await expect(failed).toContainText("送达未确认的测试消息");
    await failed.getByRole("button", { name: "取回文字" }).click();
    await expect(composer).toHaveValue("送达未确认的测试消息");
    assert.equal((await (await api.get(`/api/admin/conversations/${id}/messages`, { headers })).json()).items
      .filter(msg => msg.content === "送达未确认的测试消息").length, 0);

    await visitor.route("**/api/chat/upload", route => route.fulfill({ status: 503, json: { detail: "测试：上传暂不可用" } }));
    await visitor.locator(".mh-foot input[type=file]").setInputFiles({
      name: "acceptance.png", mimeType: "image/png", buffer: Buffer.from("fake image")
    });
    await expect(visitor.locator(".mh-row.failed").last()).toContainText("图片发送失败");
    await expect(composer).toHaveValue("送达未确认的测试消息");
    await visitor.evaluate(() => {
      Object.defineProperty(window.visualViewport, "height", { configurable: true, value: 300 });
      window.visualViewport.dispatchEvent(new Event("resize"));
    });
    await expect(visitor.locator(".mh-chat")).toHaveClass(/mh-keyboard/);
    const box = await visitor.locator(".mh-chat").boundingBox();
    assert(box.y >= 0 && box.y + box.height <= 300);
    await composer.press("Escape");
    await expect(fab).toHaveAttribute("aria-expanded", "false");

    const submitted = await api.post("/api/public/messages", {
      data: { site_key: site.site_key, name: "验收访客", content: "窄屏留言正文" }
    });
    assert(submitted.ok());
    await admin.setViewportSize({ width: 320, height: 568 });
    await admin.route("**/api/admin/messages?**", route => route.fulfill({ status: 503, json: { detail: "测试：留言暂不可用" } }));
    await admin.locator('nav [href="#messages"]').click();
    await expect(admin.locator("#msgBody")).toContainText("留言加载失败");
    await admin.unroute("**/api/admin/messages?**");
    await admin.locator("#msgBody button").click();
    const message = admin.locator("#msgBody tr[data-id]").filter({ hasText: "窄屏留言正文" });
    await expect(message).toBeVisible();
    for (const width of [320, 390]) {
      await admin.setViewportSize({ width, height: 568 });
      assert(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert(await admin.locator(".table-wrap").evaluate(element => element.scrollWidth <= element.clientWidth));
    }
    await message.click();
    await expect(admin.locator("#drawerBody")).toContainText("窄屏留言正文");

    const formPage = await browser.newPage();
    await formPage.route("**/api/public/messages", route => route.fulfill({ status: 502, body: "unavailable" }));
    await mountWidget(formPage, site.site_key, true);
    await formPage.locator(".mh-form textarea").fill("提交失败时保留文字");
    await formPage.locator(".mh-form button").click();
    await expect(formPage.locator(".mh-tip")).toContainText("服务暂不可用");
    await expect(formPage.locator(".mh-form textarea")).toHaveValue("提交失败时保留文字");
    await expect(formPage.locator(".mh-form button")).toBeEnabled();
    const changed = await api.put("/api/admin/sites/" + site.id, {
      headers, data: { name: site.name, require_contact: true, ai_enabled: false }
    });
    assert(changed.ok());
    const prePage = await browser.newPage({ viewport: { width: 320, height: 568 } });
    await mountWidget(prePage, site.site_key);
    await prePage.locator(".mh-fab").click();
    const profile = prePage.locator(".mh-pre");
    await expect(profile).toBeVisible();
    await expect(profile.locator("[name=name]")).not.toBeFocused();
    await profile.locator("[name=name]").fill("验收访客");
    await profile.locator("[name=contact]").fill("acceptance@example.com");
    await profile.getByRole("button").click();
    await expect(profile).toBeHidden();
    await expect(prePage.locator(".mh-foot .mh-btn")).toBeEnabled();
    const assistantPage = await browser.newPage({ viewport: { width: 320, height: 568 } });
    assistantPage.on("pageerror", error => errors.push(error.message));
    let assistantSocket;
    await assistantPage.routeWebSocket("**/api/chat/ws", socket => {
      assistantSocket = socket;
      socket.onMessage(raw => {
        const msg = JSON.parse(raw);
        if (msg.type === "hello") {
          socket.send(JSON.stringify({
            type: "init",
            site: { ai_available: true, online: false, offline_text: "非工作时间", require_contact: false },
            conversation: null, messages: []
          }));
        } else if (msg.type === "msg") {
          socket.send(JSON.stringify({ type: "msg", client_id: msg.client_id,
            message: { id: 1, sender: "visitor", content: msg.content } }));
          socket.send(JSON.stringify({ type: "ai_status", thinking: true }));
        }
      });
    });
    await mountWidget(assistantPage, site.site_key);
    await assistantPage.locator(".mh-fab").click();
    await expect(assistantPage.locator(".mh-head small")).toContainText("AI 助手可接待");
    await expect(assistantPage.locator(".mh-off")).toBeHidden();
    await assistantPage.locator(".mh-foot textarea").fill("你好");
    await assistantPage.locator(".mh-foot .mh-btn").click();
    await expect(assistantPage.locator(".mh-thinking")).toBeVisible();
    assistantSocket.send(JSON.stringify({ type: "ai_status", thinking: false, handoff: true }));
    await expect(assistantPage.locator(".mh-thinking")).toBeHidden();
    await expect(assistantPage.locator(".mh-head small")).toContainText("等待人工客服");
    assert.deepEqual(errors, []);
    console.log("PASS: independent loading/retry, admin reconnection and draft retention, widget uncertain delivery/upload/form errors, narrow inbox layout");
  } finally {
    await browser.close();
    await api.delete("/api/admin/sites/" + site.id, { headers });
    await api.dispose();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
