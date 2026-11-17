/*!
 * MsgHub 网站挂件
 *
 * 在线对话（默认）：
 *   <script src="https://你的域名/embed.js" data-site-key="sk_xxx"></script>
 *   → 右下角悬浮按钮，点开即可和客服实时对话
 *
 * 嵌入到页面某个位置：加 data-target="#容器选择器"
 * 旧版留言表单：加 data-mode="form"
 *
 * 其他可选属性：data-title 标题、data-color 主题色、data-welcome 欢迎语（后台设置了欢迎语时以后台为准）
 */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script) return;
  var cfg = {
    siteKey: script.getAttribute("data-site-key"),
    target: script.getAttribute("data-target"),
    mode: script.getAttribute("data-mode") || "chat",
    color: script.getAttribute("data-color") || "#2563eb",
    title: script.getAttribute("data-title"),
    welcome: script.getAttribute("data-welcome") || "您好，请问有什么可以帮您？",
  };
  if (!cfg.siteKey) { console.warn("[MsgHub] 缺少 data-site-key"); return; }
  var base = new URL(script.src);
  var C = cfg.color;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, val);
    } catch (_) { return null; }
  }

  var css =
    ".mh-box{font:14px/1.55 system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1d2926;background:#fff;border:1px solid #dfe7e2;border-radius:8px;box-sizing:border-box;text-align:left;box-shadow:0 12px 36px rgba(19,42,36,.14)}" +
    ".mh-box *{box-sizing:border-box}" +
    ".mh-fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;width:56px;height:56px;border-radius:50%;border:0;background:" + C + ";color:#fff;font-size:24px;line-height:56px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2);padding:0}" +
    ".mh-badge{position:absolute;top:-2px;right:-2px;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:#ef4444;color:#fff;font:600 12px/20px system-ui,sans-serif;display:none}" +
    ".mh-float{position:fixed;right:20px;bottom:88px;z-index:2147483000;width:380px;max-width:calc(100vw - 32px);display:none}" +
    "@media (max-width:480px){.mh-float{right:12px;left:12px;width:auto;bottom:78px;max-width:none}}" +
    // 对话
    ".mh-chat{display:flex;flex-direction:column;height:540px;max-height:calc(100vh - 112px);overflow:hidden}" +
    ".mh-inline.mh-chat{height:560px;max-width:560px;max-height:78vh}" +
    "@media(max-width:480px){.mh-chat{height:min(620px,calc(100dvh - 98px));max-height:calc(100dvh - 98px)}}" +
    ".mh-head{background:" + C + ";color:#fff;padding:14px 17px;box-shadow:0 2px 8px rgba(0,0,0,.08)}" +
    ".mh-head b{display:block;font-size:15px;line-height:1.4}.mh-head small{opacity:.92;font-size:12px}" +
    ".mh-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px;background:#9ca3af}.mh-dot.on{background:#4ade80}" +
    ".mh-list{flex:1;overflow-y:auto;padding:14px;background:#f4f7f5;overscroll-behavior:contain}" +
    ".mh-row{display:flex;flex-direction:column;align-items:flex-start;margin:8px 0}.mh-row.me{align-items:flex-end}.mh-row.sys{align-items:center}" +
    ".mh-who{font-size:11px;color:#6b7280;margin:0 4px 2px}" +
    ".mh-b{max-width:82%;padding:9px 12px;border-radius:8px;white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e1e8e4}" +
    ".mh-row.me .mh-b{background:" + C + ";color:#fff;border-color:" + C + "}" +
    ".mh-row.ai .mh-b{background:#eaf5f2;border-color:#cce5dd}" +
    ".mh-row.sys .mh-b{background:none;border:0;color:#6b7280;font-size:12px;text-align:center;max-width:90%;padding:2px 8px}" +
    ".mh-b img{display:block;max-width:100%;max-height:220px;border-radius:6px;cursor:zoom-in}" +
    ".mh-row.me .mh-b.mh-img,.mh-b.mh-img{padding:4px;background:#fff;border-color:#e5e7eb;color:#6b7280}" +
    ".mh-t{display:block;font-size:11px;opacity:.6;margin-top:2px}" +
    ".mh-row.pending .mh-b{opacity:.6}.mh-row.failed .mh-b{background:#fee2e2;color:#991b1b;border-color:#fecaca}" +
    ".mh-foot{border-top:1px solid #e1e8e4;padding:10px;display:flex;gap:7px;align-items:flex-end;background:#fff}" +
    ".mh-foot textarea{flex:1;min-width:0;resize:vertical;border:1px solid #d1dbd6;border-radius:7px;padding:9px 10px;font:inherit;color:inherit;height:42px;max-height:120px;background:#fff;margin:0}" +
    ".mh-btn{border:0;border-radius:7px;background:" + C + ";color:#fff;font:600 14px/1 system-ui,sans-serif;padding:0 16px;height:42px;cursor:pointer}" +
    ".mh-btn:disabled{opacity:.5;cursor:default}" +
    ".mh-icon{border:1px solid #d1dbd6;background:#fff;border-radius:7px;width:42px;height:42px;flex:none;cursor:pointer;font-size:18px;padding:0;color:#465750}" +
    ".mh-icon:focus-visible,.mh-btn:focus-visible,.mh-fab:focus-visible{outline:3px solid #9fe2d7;outline-offset:2px}" +
    ".mh-net{font-size:12px;color:#8b4f0a;background:#fff4d6;padding:7px 12px;display:none}" +
    ".mh-off{font-size:12px;color:#72521d;background:#fff9e8;border-bottom:1px solid #f1dfb3;padding:8px 12px;display:none}" +
    // 资料表单
    ".mh-pre{position:absolute;left:0;right:0;bottom:0;top:0;background:#fff;display:none;flex-direction:column;justify-content:center;padding:clamp(20px,6vw,32px);gap:11px}" +
    ".mh-pre p{margin:0 0 4px;color:#4b5563;font-size:13px}" +
    ".mh-pre input{width:100%;min-height:42px;padding:9px 10px;border:1px solid #d1dbd6;border-radius:7px;font:inherit;color:inherit;background:#fff}" +
    ".mh-pre .mh-err{color:#dc2626;font-size:12px;min-height:16px}" +
    // 表单模式
    ".mh-form{padding:18px;max-width:420px;box-shadow:none}.mh-form h3{margin:0 0 12px;font-size:16px}" +
    ".mh-form input,.mh-form textarea{width:100%;margin:0 0 10px;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px;font:inherit;color:inherit;background:#fff}" +
    ".mh-form textarea{min-height:100px;resize:vertical}.mh-form .mh-btn{width:100%}" +
    ".mh-tip{margin-top:8px;font-size:13px}.mh-hp{position:absolute!important;left:-9999px!important}";
  var st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);

  // ---------------- 挂载：内嵌或悬浮 ----------------
  function mount(box, onToggle) {
    var host = cfg.target && document.querySelector(cfg.target);
    if (host) { box.classList.add("mh-inline"); host.appendChild(box); return { inline: true }; }
    box.classList.add("mh-float");
    box.style.display = "none";   // 初始收起（.mh-chat 的 display:flex 会覆盖 .mh-float 的 display:none）
    var fab = el("button", "mh-fab", "✉");
    fab.type = "button";
    fab.title = cfg.title || "在线咨询";
    var badge = el("span", "mh-badge");
    fab.appendChild(badge);
    function setOpen(open) {
      box.style.display = open ? (box.classList.contains("mh-chat") ? "flex" : "block") : "none";
      fab.textContent = open ? "×" : "✉";
      fab.appendChild(badge);
      onToggle && onToggle(open);
    }
    fab.onclick = function () { setOpen(box.style.display === "none" || !box.style.display); };
    document.body.appendChild(box);
    document.body.appendChild(fab);
    return { inline: false, badge: badge };
  }

  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  // ---------------- 旧版：留言表单 ----------------
  function formWidget() {
    var api = new URL("/api/public/messages", base).href;
    var box = el("form", "mh-box mh-form");
    box.innerHTML =
      "<h3></h3>" +
      '<input name="name" placeholder="您的称呼" maxlength="100">' +
      '<input name="contact" placeholder="联系方式（邮箱 / 电话）" maxlength="200">' +
      '<textarea name="content" placeholder="请输入留言内容" maxlength="5000" required></textarea>' +
      '<input name="_hp" class="mh-hp" tabindex="-1" autocomplete="off">' +
      '<button type="submit" class="mh-btn">提交留言</button><div class="mh-tip"></div>';
    box.querySelector("h3").textContent = cfg.title || "给我们留言";
    var tip = box.querySelector(".mh-tip"), btn = box.querySelector("button");
    box.addEventListener("submit", function (e) {
      e.preventDefault();
      var body = { site_key: cfg.siteKey, page_url: location.href };
      new FormData(box).forEach(function (v, k) { body[k] = v; });
      btn.disabled = true;
      tip.style.color = "#6b7280"; tip.textContent = "提交中…";
      fetch(api, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.ok) { box.reset(); tip.style.color = "#16a34a"; tip.textContent = "提交成功，我们会尽快联系您！"; }
          else { tip.style.color = "#dc2626"; tip.textContent = r.error || r.detail || "提交失败"; }
        })
        .catch(function () { tip.style.color = "#dc2626"; tip.textContent = "网络错误，请稍后再试"; })
        .finally(function () { btn.disabled = false; });
    });
    ready(function () { mount(box); });
  }

  // ---------------- 在线对话 ----------------
  function chatWidget() {
    var wsUrl = new URL("/api/chat/ws", base);
    wsUrl.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    var uploadUrl = new URL("/api/chat/upload", base).href;
    var TOKEN_KEY = "msghub:" + cfg.siteKey;

    var box = el("div", "mh-box mh-chat");
    var head = el("div", "mh-head");
    var title = el("b", null, cfg.title || "在线咨询");
    var sub = el("small");
    var dot = el("span", "mh-dot");
    var subText = el("span", null, "");
    sub.appendChild(dot); sub.appendChild(subText);
    head.appendChild(title); head.appendChild(sub);
    var net = el("div", "mh-net", "连接中断，正在重连…");
    var off = el("div", "mh-off");
    var list = el("div", "mh-list");
    var foot = el("div", "mh-foot");
    var picBtn = el("button", "mh-icon", "🖼");
    picBtn.type = "button"; picBtn.title = "发送图片";
    var picInput = el("input");
    picInput.type = "file"; picInput.accept = "image/png,image/jpeg,image/gif,image/webp"; picInput.style.display = "none";
    var input = el("textarea");
    input.placeholder = "输入消息，Enter 发送";
    input.maxLength = 5000;
    var send = el("button", "mh-btn", "发送");
    send.type = "button";
    foot.appendChild(picBtn); foot.appendChild(picInput); foot.appendChild(input); foot.appendChild(send);

    // 对话前填写资料
    var pre = el("form", "mh-pre");
    pre.innerHTML = '<p>开始咨询前，请留下您的称呼和联系方式，方便客服及时联系您。</p>' +
      '<input name="name" placeholder="您的称呼" maxlength="100" autocomplete="name">' +
      '<input name="contact" placeholder="手机号或邮箱" maxlength="200" autocomplete="tel">' +
      '<div class="mh-err"></div><button type="submit" class="mh-btn">开始咨询</button>';
    var preErr = pre.querySelector(".mh-err");

    var body = el("div");
    body.style.cssText = "flex:1;display:flex;flex-direction:column;position:relative;min-height:0";
    body.appendChild(off); body.appendChild(list); body.appendChild(pre);
    box.appendChild(head); box.appendChild(net); box.appendChild(body); box.appendChild(foot);

    var ws = null, connected = false, retry = 0, retryTimer = null, pingTimer = null;
    var isOpen = false, unread = 0, seen = {}, pending = {}, ui = null;
    var site = null, profileOk = false;

    function fmtTime(s) { return s ? String(s).slice(11, 16) : ""; }
    function scroll() { list.scrollTop = list.scrollHeight; }
    function abs(path) { return new URL(path, base).href; }
    function setSub(status) {
      var online = site && site.online;
      dot.className = "mh-dot" + (online ? " on" : "");
      subText.textContent = status === "closed" ? "对话已结束，发送消息可重新开始"
        : status === "active" ? "客服已接入" : online ? "客服在线" : "客服不在线，可先留言";
    }

    function bubble(sender, msgType, content, time) {
      var cls = sender === "visitor" ? "me" : sender === "system" ? "sys" : sender === "ai" ? "ai" : "them";
      var row = el("div", "mh-row " + cls);
      if (sender === "ai") row.appendChild(el("div", "mh-who", "AI 助手"));
      var b = el("div", "mh-b" + (msgType === "image" ? " mh-img" : ""));
      if (msgType === "image") {
        var img = el("img");
        img.src = content.indexOf("blob:") === 0 ? content : abs(content);
        img.alt = "图片";
        img.onload = scroll;
        img.onclick = function () { window.open(img.src, "_blank", "noopener"); };
        b.appendChild(img);
      } else {
        b.textContent = content;
      }
      if (time && sender !== "system") b.appendChild(el("span", "mh-t", fmtTime(time)));
      row.appendChild(b);
      list.appendChild(row);
      return row;
    }
    function render(m) {
      if (seen[m.id]) return;
      seen[m.id] = 1;
      bubble(m.sender, m.msg_type, m.content, m.created_at);
    }

    function showPre(show) {
      pre.style.display = show ? "flex" : "none";
      input.disabled = picBtn.disabled = send.disabled = show;
      if (show) setTimeout(function () { pre.querySelector("[name=name]").focus(); }, 0);
    }

    function bump() {
      if (isOpen || !ui || !ui.badge) return;
      unread++;
      ui.badge.textContent = unread > 99 ? "99+" : unread;
      ui.badge.style.display = "block";
    }

    function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

    function connect() {
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      clearTimeout(retryTimer);
      try { ws = new WebSocket(wsUrl.href); } catch (_) { return scheduleRetry(); }
      ws.onopen = function () {
        wsSend({ type: "hello", site_key: cfg.siteKey, token: store(TOKEN_KEY) || "", page_url: location.href });
      };
      ws.onmessage = function (ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        if (d.type === "init") {
          connected = true; retry = 0;
          site = d.site || {};
          profileOk = !site.require_contact || !!(d.profile && d.profile.name && d.profile.contact);
          net.style.display = "none";
          off.textContent = site.offline_text || "";
          off.style.display = site.online ? "none" : "block";
          list.textContent = ""; seen = {};
          bubble("agent", "text", (site.welcome && site.welcome !== "您好，请问有什么可以帮您？") ? site.welcome : cfg.welcome);
          (d.messages || []).forEach(render);
          Object.keys(pending).forEach(function (cid) { list.appendChild(pending[cid].row); });
          setSub(d.conversation && d.conversation.status);
          showPre(!profileOk);
          scroll();
          flushPending();
          clearInterval(pingTimer);
          pingTimer = setInterval(function () { wsSend({ type: "ping" }); }, 25000);
        } else if (d.type === "token") {
          store(TOKEN_KEY, d.token);
        } else if (d.type === "profile_ok") {
          profileOk = true;
          showPre(false);
          input.focus();
          flushPending();
        } else if (d.type === "msg") {
          var p = d.client_id && pending[d.client_id];
          if (p) {
            seen[d.message.id] = 1;
            p.row.className = "mh-row me";
            p.row.lastChild.appendChild(el("span", "mh-t", fmtTime(d.message.created_at)));
            delete pending[d.client_id];
          } else {
            render(d.message);
            if (d.message.sender !== "visitor") bump();
          }
          scroll();
        } else if (d.type === "status") {
          setSub(d.status);
        } else if (d.type === "error") {
          if (pre.style.display === "flex" && !d.client_id) { preErr.textContent = d.error; return; }
          if (d.need_profile) { profileOk = false; showPre(true); }
          var f = d.client_id && pending[d.client_id];
          if (f) {
            if (d.need_profile) { f.sent = false; return; }   // 填完资料后自动重发
            f.row.className = "mh-row me failed"; f.row.title = d.error; delete pending[d.client_id];
          }
          bubble("system", "text", d.error);
          scroll();
        }
      };
      ws.onclose = function (ev) {
        connected = false;
        clearInterval(pingTimer);
        // 已发出但未确认的消息无法判断是否送达，标记为失败让访客知道
        Object.keys(pending).forEach(function (cid) {
          var p = pending[cid];
          if (p.sent) { p.row.className = "mh-row me failed"; p.row.title = "可能未发送成功"; delete pending[cid]; }
        });
        if (ev.code === 4403) { net.textContent = "该网站的在线客服暂不可用"; net.style.display = "block"; return; }
        if (isOpen || store(TOKEN_KEY)) { net.style.display = "block"; scheduleRetry(); }
      };
    }
    function scheduleRetry() {
      clearTimeout(retryTimer);
      var delay = Math.min(30000, 1000 * Math.pow(2, retry++));
      retryTimer = setTimeout(connect, delay);
    }

    function flushPending() {
      if (!connected || !profileOk) return;
      Object.keys(pending).forEach(function (cid) {
        var p = pending[cid];
        if (!p.sent && p.content) {
          wsSend({ type: "msg", msg_type: p.msgType, content: p.content, client_id: cid, page_url: location.href });
          p.sent = true;
        }
      });
    }

    function newCid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

    function doSend() {
      var text = input.value.trim();
      if (!text) return;
      var cid = newCid();
      pending[cid] = { msgType: "text", content: text, row: bubble("visitor", "text", text), sent: false };
      pending[cid].row.className = "mh-row me pending";
      input.value = "";
      scroll();
      if (connected) flushPending(); else connect();
    }

    function sendImage(file) {
      if (!file || !/^image\//.test(file.type)) return;
      if (file.size > 5 * 1024 * 1024) { bubble("system", "text", "图片不能超过 5MB"); scroll(); return; }
      var token = store(TOKEN_KEY);
      if (!token) { bubble("system", "text", "请先发送一条文字消息，再发送图片"); scroll(); return; }
      var cid = newCid();
      var p = pending[cid] = { msgType: "image", content: "", row: bubble("visitor", "image", URL.createObjectURL(file)), sent: false };
      p.row.className = "mh-row me pending";
      scroll();
      var fd = new FormData();
      fd.append("site_key", cfg.siteKey);
      fd.append("token", token);
      fd.append("file", file);
      fetch(uploadUrl, { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (!r.url) throw new Error(r.error || "上传失败");
          p.content = r.url;
          if (connected) flushPending(); else connect();
        })
        .catch(function (err) {
          p.row.className = "mh-row me failed"; p.row.title = err.message;
          delete pending[cid];
          bubble("system", "text", "图片发送失败：" + err.message);
          scroll();
        });
    }

    send.onclick = doSend;
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        doSend();
      }
    });
    input.addEventListener("paste", function (e) {
      var items = (e.clipboardData && e.clipboardData.files) || [];
      if (items.length && /^image\//.test(items[0].type)) { e.preventDefault(); sendImage(items[0]); }
    });
    picBtn.onclick = function () { picInput.click(); };
    picInput.onchange = function () { sendImage(picInput.files[0]); picInput.value = ""; };
    pre.addEventListener("submit", function (e) {
      e.preventDefault();
      preErr.textContent = "";
      var name = pre.querySelector("[name=name]").value.trim();
      var contact = pre.querySelector("[name=contact]").value.trim();
      if (!name) { preErr.textContent = "请填写称呼"; return; }
      if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(contact) && (contact.replace(/\D/g, "").length < 7 || !/^\+?[\d\- ()]+$/.test(contact))) {
        preErr.textContent = "请填写正确的手机号或邮箱"; return;
      }
      if (!connected) { preErr.textContent = "正在连接，请稍候再试"; connect(); return; }
      wsSend({ type: "profile", name: name, contact: contact });
    });

    bubble("agent", "text", cfg.welcome);
    ready(function () {
      ui = mount(box, function (open) {
        isOpen = open;
        if (open) {
          unread = 0;
          if (ui && ui.badge) ui.badge.style.display = "none";
          connect();
          setTimeout(function () { scroll(); if (profileOk) input.focus(); }, 0);
        }
      });
      if (ui.inline) { isOpen = true; connect(); }
      else if (store(TOKEN_KEY)) connect();  // 老访客：后台连着，客服回复时按钮上显示未读数
    });
  }

  if (cfg.mode === "form") formWidget(); else chatWidget();
})();
