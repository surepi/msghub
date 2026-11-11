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
 * 其他可选属性：data-title 标题、data-color 主题色、data-welcome 欢迎语
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
    ".mh-box{font:14px/1.5 system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2937;background:#fff;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;text-align:left}" +
    ".mh-box *{box-sizing:border-box}" +
    ".mh-fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;width:56px;height:56px;border-radius:50%;border:0;background:" + C + ";color:#fff;font-size:24px;line-height:56px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2);padding:0}" +
    ".mh-badge{position:absolute;top:-2px;right:-2px;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:#ef4444;color:#fff;font:600 12px/20px system-ui,sans-serif;display:none}" +
    ".mh-float{position:fixed;right:20px;bottom:88px;z-index:2147483000;width:360px;max-width:calc(100vw - 32px);box-shadow:0 10px 36px rgba(0,0,0,.2);display:none}" +
    "@media (max-width:480px){.mh-float{right:16px;left:16px;width:auto;bottom:84px}}" +
    // 对话
    ".mh-chat{display:flex;flex-direction:column;height:480px;max-height:calc(100vh - 120px);overflow:hidden}" +
    ".mh-inline.mh-chat{height:520px;max-width:520px}" +
    ".mh-head{background:" + C + ";color:#fff;padding:12px 16px;display:flex;align-items:center;gap:8px}" +
    ".mh-head b{font-size:15px;flex:1}.mh-head small{opacity:.85;font-size:12px}" +
    ".mh-list{flex:1;overflow-y:auto;padding:12px;background:#f7f8fa}" +
    ".mh-row{display:flex;margin:8px 0}.mh-row.me{justify-content:flex-end}.mh-row.sys{justify-content:center}" +
    ".mh-b{max-width:78%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e5e7eb}" +
    ".mh-row.me .mh-b{background:" + C + ";color:#fff;border-color:" + C + "}" +
    ".mh-row.sys .mh-b{background:none;border:0;color:#6b7280;font-size:12px;text-align:center;max-width:90%;padding:2px 8px}" +
    ".mh-t{display:block;font-size:11px;opacity:.6;margin-top:2px}" +
    ".mh-row.pending .mh-b{opacity:.6}.mh-row.failed .mh-b{background:#fee2e2;color:#991b1b;border-color:#fecaca}" +
    ".mh-foot{border-top:1px solid #e5e7eb;padding:8px;display:flex;gap:8px;align-items:flex-end;background:#fff}" +
    ".mh-foot textarea{flex:1;resize:none;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;font:inherit;color:inherit;height:40px;max-height:120px;background:#fff;margin:0}" +
    ".mh-btn{border:0;border-radius:8px;background:" + C + ";color:#fff;font:inherit;padding:0 16px;height:40px;cursor:pointer}" +
    ".mh-btn:disabled{opacity:.5;cursor:default}" +
    ".mh-net{font-size:12px;color:#b45309;background:#fef3c7;padding:4px 12px;display:none}" +
    // 表单
    ".mh-form{padding:16px;max-width:420px}.mh-form h3{margin:0 0 12px;font-size:16px}" +
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
    return { inline: false, badge: badge, close: function () { setOpen(false); } };
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
    var TOKEN_KEY = "msghub:" + cfg.siteKey;

    var box = el("div", "mh-box mh-chat");
    var head = el("div", "mh-head");
    var titleWrap = el("div");
    titleWrap.style.flex = "1";
    var title = el("b", null, cfg.title || "在线咨询");
    var sub = el("small", null, "");
    title.style.display = "block";
    titleWrap.appendChild(title); titleWrap.appendChild(sub);
    head.appendChild(titleWrap);
    var net = el("div", "mh-net", "连接中断，正在重连…");
    var list = el("div", "mh-list");
    var foot = el("div", "mh-foot");
    var input = el("textarea");
    input.placeholder = "输入消息，Enter 发送";
    input.maxLength = 5000;
    var send = el("button", "mh-btn", "发送");
    send.type = "button";
    foot.appendChild(input); foot.appendChild(send);
    box.appendChild(head); box.appendChild(net); box.appendChild(list); box.appendChild(foot);

    var ws = null, connected = false, retry = 0, retryTimer = null, pingTimer = null;
    var isOpen = false, unread = 0, seen = {}, pending = {}, ui = null;

    function fmtTime(s) { return s ? String(s).slice(11, 16) : ""; }
    function scroll() { list.scrollTop = list.scrollHeight; }
    function setSub(status) {
      sub.textContent = status === "closed" ? "对话已结束，发送消息可重新开始"
        : status === "active" ? "客服已接入" : status === "waiting" ? "已收到，客服会尽快回复" : "";
    }

    function bubble(sender, text, time) {
      var row = el("div", "mh-row " + (sender === "visitor" ? "me" : sender === "system" ? "sys" : "them"));
      var b = el("div", "mh-b", text);
      if (time && sender !== "system") b.appendChild(el("span", "mh-t", fmtTime(time)));
      row.appendChild(b);
      list.appendChild(row);
      return row;
    }
    function render(m) {
      if (seen[m.id]) return;
      seen[m.id] = 1;
      bubble(m.sender, m.content, m.created_at);
    }
    function welcome() { bubble("agent", cfg.welcome); }

    function bump() {
      if (isOpen || !ui || !ui.badge) return;
      unread++;
      ui.badge.textContent = unread > 99 ? "99+" : unread;
      ui.badge.style.display = "block";
    }

    function connect() {
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      clearTimeout(retryTimer);
      try { ws = new WebSocket(wsUrl.href); } catch (_) { return scheduleRetry(); }
      ws.onopen = function () {
        ws.send(JSON.stringify({ type: "hello", site_key: cfg.siteKey, token: store(TOKEN_KEY) || "", page_url: location.href }));
      };
      ws.onmessage = function (ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        if (d.type === "init") {
          connected = true; retry = 0;
          net.style.display = "none";
          list.textContent = ""; seen = {};
          welcome();
          (d.messages || []).forEach(render);
          Object.keys(pending).forEach(function (cid) { list.appendChild(pending[cid].row); });
          setSub(d.conversation && d.conversation.status);
          scroll();
          flushPending();
          clearInterval(pingTimer);
          pingTimer = setInterval(function () { if (ws.readyState === 1) ws.send('{"type":"ping"}'); }, 25000);
        } else if (d.type === "token") {
          store(TOKEN_KEY, d.token);
        } else if (d.type === "msg") {
          var p = d.client_id && pending[d.client_id];
          if (p) {
            seen[d.message.id] = 1;
            p.row.className = "mh-row me";
            p.row.firstChild.appendChild(el("span", "mh-t", fmtTime(d.message.created_at)));
            delete pending[d.client_id];
          } else {
            render(d.message);
            if (d.message.sender !== "visitor") bump();
          }
          scroll();
        } else if (d.type === "status") {
          setSub(d.status);
        } else if (d.type === "error") {
          var f = d.client_id && pending[d.client_id];
          if (f) { f.row.className = "mh-row me failed"; f.row.title = d.error; delete pending[d.client_id]; }
          bubble("system", d.error);
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
      Object.keys(pending).forEach(function (cid) {
        var p = pending[cid];
        if (!p.sent && connected) {
          ws.send(JSON.stringify({ type: "msg", content: p.text, client_id: cid, page_url: location.href }));
          p.sent = true;
        }
      });
    }

    function doSend() {
      var text = input.value.trim();
      if (!text) return;
      var cid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      pending[cid] = { text: text, row: bubble("visitor", text), sent: false };
      pending[cid].row.className = "mh-row me pending";
      input.value = "";
      scroll();
      if (connected) flushPending(); else connect();
    }
    send.onclick = doSend;
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        doSend();
      }
    });

    welcome();
    ready(function () {
      ui = mount(box, function (open) {
        isOpen = open;
        if (open) {
          unread = 0;
          if (ui && ui.badge) ui.badge.style.display = "none";
          connect();
          setTimeout(function () { scroll(); input.focus(); }, 0);
        }
      });
      if (ui.inline) { isOpen = true; connect(); }
      else if (store(TOKEN_KEY)) connect();  // 老访客：后台连着，客服回复时按钮上显示未读数
    });
  }

  if (cfg.mode === "form") formWidget(); else chatWidget();
})();
