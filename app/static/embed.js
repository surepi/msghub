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
 * 其他可选属性：data-lang 语言（zh / en，默认跟随页面）、data-title 标题、data-color 主题色、
 * data-welcome 欢迎语（后台设置了欢迎语时以后台为准）
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
    welcome: script.getAttribute("data-welcome"),
  };
  if (!cfg.siteKey) { console.warn("[MsgHub] 缺少 data-site-key"); return; }
  var base = new URL(script.src);
  var C = cfg.color;

  var copy = {
    title: ["在线咨询", "Live chat"],
    openChat: ["打开咨询窗口", "Open live chat"],
    closeChat: ["关闭咨询窗口", "Close live chat"],
    openForm: ["打开留言表单", "Open message form"],
    closeForm: ["关闭留言表单", "Close message form"],
    welcome: ["您好，请问有什么可以帮您？", "Hello! How can we help you?"],
    offlineBanner: ["客服当前不在线，您可以先留言，我们上班后会尽快通过您留下的联系方式联系您。",
      "Our team is offline. Leave a message and we'll get back to you during business hours."],
    formTitle: ["给我们留言", "Leave us a message"],
    name: ["您的称呼", "Your name"],
    formContact: ["联系方式（邮箱 / 电话）", "Email or phone number"],
    formMessage: ["请输入留言内容", "Your message"],
    submit: ["提交留言", "Submit message"],
    submitting: ["提交中…", "Submitting…"],
    submitted: ["提交成功，我们会尽快联系您！", "Message received. We'll get back to you soon!"],
    submitFailed: ["提交失败", "Couldn't submit your message"],
    unavailable: ["服务暂不可用，请稍后重试", "Service temporarily unavailable. Please try again later."],
    requestTimeout: ["请求超时，请稍后重试", "The request timed out. Please try again."],
    networkError: ["网络连接失败，请检查网络后重试", "Check your internet connection and try again."],
    reconnect: ["重新连接", "Reconnect"],
    thinking: ["AI 助手正在查看您的消息…", "The AI assistant is reviewing your message…"],
    uploadImage: ["上传图片", "Upload image"],
    composer: ["输入消息…", "Message…"],
    messageLabel: ["输入消息", "Type a message"],
    composerHint: ["按 Enter 发送，Shift+Enter 换行", "Enter to send, Shift+Enter for a new line"],
    send: ["发送", "Send"],
    profileIntro: ["开始咨询前，请留下您的称呼和联系方式，方便客服及时联系您。",
      "Please leave your name and contact details so our team can follow up."],
    profileContact: ["手机号或邮箱", "Phone number or email"],
    startChat: ["开始咨询", "Start chat"],
    connecting: ["正在连接客服…", "Connecting to support…"],
    closed: ["对话已结束，发送消息可重新开始", "Chat ended. Send a message to start again."],
    active: ["客服已接入", "An agent has joined"],
    waiting: ["等待人工客服", "Waiting for an agent"],
    aiAvailable: ["AI 助手可接待", "AI assistant available"],
    online: ["客服在线", "Support is online"],
    offline: ["客服不在线，可先留言", "Support is offline. You can leave a message."],
    assistant: ["AI 助手", "AI assistant"],
    image: ["图片", "Image"],
    imageBroken: ["图片无法加载", "Image could not be loaded"],
    restore: ["取回文字", "Restore text"],
    draftTooLong: ["输入框字数不足，请先编辑当前草稿", "Edit your draft to make room for this message."],
    imageUnsent: ["图片未发送", "Image not sent"],
    disconnected: ["网络已断开，联网后将自动重连", "You're offline. We'll reconnect when you're back online."],
    reconnecting: ["连接中断，正在重连…", "Connection lost. Reconnecting…"],
    sendAfterProfile: ["填写资料后发送", "Complete your contact details to send"],
    profileInterrupted: ["连接中断，恢复后可再次提交", "Connection lost. Try again after reconnecting."],
    deliveryUnconfirmed: ["送达未确认，请先查看聊天记录", "Delivery unconfirmed. Check the chat before resending."],
    chatUnavailable: ["该网站的在线客服暂不可用", "Live chat is unavailable on this site."],
    sending: ["发送中…", "Sending…"],
    imageTooLarge: ["图片不能超过 5MB", "Images must be 5 MB or smaller."],
    sendTextFirst: ["请先发送一条文字消息，再发送图片", "Send a text message before uploading an image."],
    uploading: ["正在上传图片…", "Uploading image…"],
    uploadFailed: ["上传失败", "Upload failed"],
    sendWhenConnected: ["等待连接后发送…", "Waiting to reconnect before sending…"],
    imageSendFailed: ["图片发送失败", "Couldn't send the image"],
    nameRequired: ["请填写称呼", "Enter your name."],
    invalidContact: ["请填写正确的手机号或邮箱", "Enter a valid phone number or email address."],
    waitForConnection: ["正在连接，请稍候再试", "Connecting. Please try again shortly."],
    saving: ["正在保存…", "Saving…"],
    saveUnconfirmed: ["保存结果未确认，请稍后重试", "We couldn't confirm your details were saved. Please try again."],
  };

  // The public API and saved system notices provide these messages as text.
  var serverCopy = new Map([
    copy.offlineBanner, copy.nameRequired, copy.imageTooLarge,
    ["协议错误", "Chat protocol error."],
    ["握手超时", "The chat connection timed out."],
    ["站点不存在或已停用", "This site is unavailable or disabled."],
    ["来源域名不被允许", "This domain isn't allowed to use live chat."],
    ["站点不存在或来源不被允许", "This site isn't allowed to use live chat."],
    ["请填写正确的电话或邮箱", copy.invalidContact[1]],
    ["请先填写称呼和联系方式", "Enter your name and contact details first."],
    ["操作过于频繁，请稍后再试", "Too many attempts. Please try again later."],
    ["请求过于频繁，请稍后再试", "Too many requests. Please try again later."],
    ["发送过于频繁，请稍后再试", "You're sending messages too quickly. Try again shortly."],
    ["提交过于频繁，请稍后再试", "Too many submissions. Please try again later."],
    ["上传过于频繁，请稍后再试", "Too many uploads. Please try again later."],
    ["请求体超过大小限制", "The request is too large."],
    ["JSON 格式错误", "Invalid request format."],
    ["缺少 site_key", "The site key is missing."],
    ["留言内容不能为空", "Enter a message."],
    ["留言内容不能超过 5000 字", "Your message must be 5,000 characters or fewer."],
    ["单条消息不能超过 5000 字", "Your message must be 5,000 characters or fewer."],
    ["图片无效，请重新上传", "Invalid image. Please upload it again."],
    ["请先开始对话", "Start a chat before uploading an image."],
    ["文件为空", "The file is empty."],
    ["只支持 PNG / JPG / GIF / WebP 图片", "Use a PNG, JPEG, GIF, or WebP image."],
    ["对话已结束。如还有问题，直接发送消息即可重新联系客服。", "This chat has ended. Send a message if you need more help."],
    ["已为您转人工，客服可以在这里回复。您也可以留下联系方式，方便后续联系。",
      "We've passed your message to our support team. An agent can reply here, or you can leave your contact details for a follow-up."],
    ["客服暂时没能及时回复，已为您记录留言，我们会尽快通过您留下的联系方式联系您。",
      "Our team couldn't respond right away. We've saved your message and will get back to you as soon as we can."],
  ].map(function (pair) { return [pair[0], pair]; }));

  var page = document.documentElement;
  var fixedLang = supportedLang(script.getAttribute("data-lang"));
  var lang = fixedLang || supportedLang(page.lang) || "zh";
  var bindings = new WeakMap();

  function supportedLang(value) {
    var primary = (value || "").trim().toLowerCase().split("-")[0];
    return primary === "zh" || primary === "en" ? primary : null;
  }
  function translate(message) {
    return Array.isArray(message) ? message[lang === "en" ? 1 : 0] : message;
  }
  function writeLabel(node, message, attribute) {
    if (attribute) node.setAttribute(attribute, translate(message));
    else node.textContent = translate(message);
  }
  function label(node, message, attribute = "") {
    var labels = bindings.get(node);
    if (Array.isArray(message)) {
      if (!labels) bindings.set(node, labels = new Map());
      labels.set(attribute, message);
    } else {
      labels?.delete(attribute);
    }
    writeLabel(node, message, attribute);
  }
  function watchLanguage(box, launcher) {
    box.lang = lang;
    if (launcher) launcher.lang = lang;
    if (fixedLang) return;
    function update() {
      var next = supportedLang(page.lang) || "zh";
      if (next === lang) return;
      lang = box.lang = next;
      if (launcher) launcher.lang = next;
      function refresh(node) {
        bindings.get(node)?.forEach(function (message, attribute) { writeLabel(node, message, attribute); });
      }
      refresh(box);
      box.querySelectorAll("*").forEach(refresh);
      if (launcher) refresh(launcher);
    }
    update();
    new MutationObserver(update).observe(page, { attributes: true, attributeFilter: ["lang"] });
  }
  function serverMessage(message, fallback) {
    return typeof message === "string" && message ? serverCopy.get(message) || message : fallback;
  }
  function responseError(result, fallback) {
    return serverMessage(result.error, serverMessage(result.detail, fallback));
  }
  function widgetError(message) {
    var error = new Error(translate(message));
    error.widgetCopy = message;
    return error;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) label(e, text);
    return e;
  }
  function glyph() {
    var icon = el("span", "mh-glyph");
    icon.setAttribute("aria-hidden", "true");
    return icon;
  }
  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, val);
    } catch (_) { return null; }
  }

  async function post(url, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 20000);
    try {
      var response = await fetch(url, Object.assign({ signal: controller.signal }, options));
      var result = await response.json().catch(function () { return {}; });
      if (!response.ok) throw widgetError(responseError(result, copy.unavailable));
      return result;
    } catch (err) {
      if (controller.signal.aborted) throw widgetError(copy.requestTimeout);
      if (err instanceof TypeError) throw widgetError(copy.networkError);
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  var css =
    ".mh-box{font:14px/1.55 system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1d2926;background:#fff;border:1px solid #dfe7e2;border-radius:8px;box-sizing:border-box;text-align:left;overflow-wrap:anywhere;box-shadow:0 12px 36px rgba(19,42,36,.14)}" +
    ".mh-box *{box-sizing:border-box}" +
    ".mh-fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;width:56px;height:56px;border-radius:50%;border:0;background:" + C + ";color:#fff;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2);padding:0}" +
    ".mh-fab,.mh-icon{display:grid;place-items:center}" +
    ".mh-glyph{position:relative;display:block;width:20px;height:20px;pointer-events:none}" +
    ".mh-glyph::before,.mh-glyph::after{position:absolute;box-sizing:border-box;content:''}" +
    ".mh-glyph-chat::before{inset:1px 0 4px;border:2px solid currentColor;border-radius:5px}" +
    ".mh-glyph-chat::after{left:4px;bottom:2px;width:6px;height:6px;border-left:2px solid currentColor;border-bottom:2px solid currentColor;transform:skewY(-35deg);background:" + C + "}" +
    ".mh-glyph-mail::before{inset:2px 0;border:2px solid currentColor;border-radius:4px}" +
    ".mh-glyph-mail::after{left:5px;top:1px;width:10px;height:10px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(45deg)}" +
    ".mh-glyph-close::before,.mh-glyph-close::after{left:2px;top:9px;width:16px;height:2px;border-radius:1px;background:currentColor;transform:rotate(45deg)}" +
    ".mh-glyph-close::after{transform:rotate(-45deg)}" +
    ".mh-glyph-upload{background:linear-gradient(currentColor,currentColor) center 3px/2px 10px no-repeat}" +
    ".mh-glyph-upload::before{inset:11px 1px 1px;border:2px solid currentColor;border-top:0;border-radius:0 0 3px 3px}" +
    ".mh-glyph-upload::after{top:3px;left:6px;width:8px;height:8px;border-top:2px solid currentColor;border-left:2px solid currentColor;transform:rotate(45deg)}" +
    ".mh-badge{position:absolute;top:-2px;right:-2px;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:#ef4444;color:#fff;font:600 12px/20px system-ui,sans-serif;display:none}" +
    ".mh-float{position:fixed;right:20px;bottom:88px;z-index:2147483000;width:380px;max-width:calc(100vw - 32px);display:none}" +
    "@media (max-width:480px){.mh-float{right:12px;left:12px;width:auto;bottom:78px;max-width:none}}" +
    // 对话
    ".mh-chat{display:flex;flex-direction:column;height:540px;max-height:calc(100vh - 112px);overflow:hidden}" +
    ".mh-inline.mh-chat{height:560px;max-width:560px;max-height:78vh}" +
    "@media(max-width:480px){.mh-chat{height:min(620px,calc(100dvh - 98px));max-height:calc(100dvh - 98px)}.mh-float.mh-box:focus-within textarea,.mh-float.mh-box:focus-within input:not([type=file]){font-size:16px}}" +
    ".mh-float.mh-box.mh-keyboard{bottom:calc(var(--mh-keyboard-inset) + 8px);max-height:calc(var(--mh-visible-height) - 16px)}" +
    ".mh-form.mh-float{max-height:calc(100dvh - 100px);overflow:auto;overscroll-behavior:contain}" +
    ".mh-head{background:" + C + ";color:#fff;padding:14px 17px;max-height:40%;overflow:auto;box-shadow:0 2px 8px rgba(0,0,0,.08)}" +
    ".mh-head b{display:block;font-size:15px;line-height:1.4}.mh-head small{opacity:.92;font-size:12px}" +
    ".mh-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px;background:#9ca3af}.mh-dot.on{background:#4ade80}" +
    ".mh-list{flex:1;min-height:0;overflow-y:auto;padding:14px;background:#f4f7f5;overscroll-behavior:contain}" +
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
    ".mh-delivery{color:#6b7280;font-size:12px;margin:3px 2px;text-align:right;overflow-wrap:anywhere}" +
    ".mh-delivery button,.mh-net button{border:1px solid currentColor;border-radius:5px;color:inherit;background:transparent;font:inherit;padding:4px 8px;max-width:100%;white-space:normal;overflow-wrap:anywhere;cursor:pointer}" +
    ".mh-delivery button{margin-inline-start:6px;max-width:calc(100% - 6px)}" +
    ".mh-net{flex-shrink:0;max-height:35%;overflow:auto}.mh-head,.mh-foot{flex-shrink:0}" +
    ".mh-foot{border-top:1px solid #e1e8e4;padding:10px;display:flex;gap:7px;align-items:flex-end;background:#fff}" +
    ".mh-foot textarea{flex:1;min-width:0;resize:vertical;border:1px solid #d1dbd6;border-radius:7px;padding:9px 10px;font:inherit;color:inherit;height:42px;max-height:120px;background:#fff;margin:0}" +
    ".mh-btn{border:0;border-radius:7px;background:" + C + ";color:#fff;font:600 14px/1.3 system-ui,sans-serif;padding:8px 16px;min-height:42px;white-space:normal;overflow-wrap:anywhere;cursor:pointer}" +
    ".mh-foot .mh-btn{flex:none;padding-inline:clamp(10px,3vw,16px);white-space:nowrap}" +
    ".mh-btn:disabled{opacity:.5;cursor:default}" +
    ".mh-icon{border:1px solid #d1dbd6;background:#fff;border-radius:7px;width:42px;height:42px;flex:none;cursor:pointer;padding:0;color:#465750}" +
    ".mh-icon:disabled{opacity:.5;cursor:default}" +
    ".mh-icon:focus-visible,.mh-btn:focus-visible,.mh-fab:focus-visible{outline:3px solid #9fe2d7;outline-offset:2px}" +
    ".mh-net{font-size:12px;color:#8b4f0a;background:#fff4d6;padding:7px 12px;display:none;align-items:center;flex-wrap:wrap;gap:4px 8px}" +
    ".mh-net span{min-width:0;flex:1 1 12em}.mh-net button[hidden]{display:none}" +
    ".mh-thinking{flex:none;padding:5px 12px;color:#52665c;background:#f4f7f5;font-size:12px}.mh-thinking[hidden]{display:none}" +
    ".mh-off{font-size:12px;color:#72521d;background:#fff9e8;border-bottom:1px solid #f1dfb3;padding:8px 12px;display:none;max-height:45%;overflow:auto}" +
    // 资料表单
    ".mh-pre{position:absolute;left:0;right:0;bottom:0;top:0;background:#fff;display:none;flex-direction:column;overflow:auto;overscroll-behavior:contain;padding:clamp(16px,5vw,28px);gap:11px}" +
    ".mh-pre>*{flex-shrink:0}.mh-pre p{margin:auto 0 4px;color:#4b5563;font-size:13px}" +
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
    if (host) {
      watchLanguage(box);
      box.classList.add("mh-inline"); host.appendChild(box); return { inline: true };
    }
    box.classList.add("mh-float");
    box.style.display = "none";   // 初始收起（.mh-chat 的 display:flex 会覆盖 .mh-float 的 display:none）
    var fab = el("button", "mh-fab");
    fab.type = "button";
    var isForm = cfg.mode === "form";
    var launcherIcon = glyph();
    watchLanguage(box, fab);
    var badge = el("span", "mh-badge");
    fab.append(launcherIcon, badge);
    function updateLauncher(open) {
      launcherIcon.className = "mh-glyph mh-glyph-" + (open ? "close" : isForm ? "mail" : "chat");
      var action = open ? (isForm ? copy.closeForm : copy.closeChat) : cfg.title || (isForm ? copy.openForm : copy.openChat);
      fab.setAttribute("aria-expanded", String(open));
      label(fab, action, "aria-label");
      label(fab, action, "title");
    }
    function setOpen(open) {
      box.style.display = open ? (box.classList.contains("mh-chat") ? "flex" : "block") : "none";
      updateLauncher(open);
      fitViewport();
      onToggle && onToggle(open);
    }
    function fitViewport() {
      var viewport = window.visualViewport;
      if (!viewport) return;
      var inset = document.documentElement.clientHeight - viewport.height - viewport.offsetTop;
      var keyboard = box.style.display !== "none" && viewport.scale < 1.1 && inset > 100;
      box.classList.toggle("mh-keyboard", keyboard);
      if (keyboard) {
        box.style.setProperty("--mh-keyboard-inset", inset + "px");
        box.style.setProperty("--mh-visible-height", viewport.height + "px");
      }
    }
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", fitViewport);
      window.visualViewport.addEventListener("scroll", fitViewport);
    }
    updateLauncher(false);
    fab.onclick = function () { setOpen(box.style.display === "none" || !box.style.display); };
    box.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !event.isComposing) { setOpen(false); fab.focus(); }
    });
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
      '<input name="name" maxlength="100">' +
      '<input name="contact" maxlength="200">' +
      '<textarea name="content" maxlength="5000" required></textarea>' +
      '<input name="_hp" class="mh-hp" tabindex="-1" autocomplete="off">' +
      '<button type="submit" class="mh-btn"></button><div class="mh-tip" role="status"></div>';
    label(box.querySelector("h3"), cfg.title || copy.formTitle);
    label(box.elements.name, copy.name, "placeholder");
    label(box.elements.contact, copy.formContact, "placeholder");
    label(box.elements.content, copy.formMessage, "placeholder");
    var tip = box.querySelector(".mh-tip"), btn = box.querySelector("button");
    label(btn, copy.submit);
    box.addEventListener("submit", function (e) {
      e.preventDefault();
      if (btn.disabled) return;
      var body = { site_key: cfg.siteKey, page_url: location.href };
      new FormData(box).forEach(function (v, k) { body[k] = v; });
      btn.disabled = true;
      tip.style.color = "#6b7280"; label(tip, copy.submitting);
      post(api, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) {
          if (r.ok) { box.reset(); tip.style.color = "#16a34a"; label(tip, copy.submitted); }
          else { tip.style.color = "#dc2626"; label(tip, responseError(r, copy.submitFailed)); }
        })
        .catch(function (err) { tip.style.color = "#dc2626"; label(tip, err.widgetCopy || err.message); })
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
    var title = el("b", null, cfg.title || copy.title);
    var sub = el("small");
    var dot = el("span", "mh-dot");
    var subText = el("span", null, "");
    sub.appendChild(dot); sub.appendChild(subText);
    head.appendChild(title); head.appendChild(sub);
    var net = el("div", "mh-net");
    var netText = el("span");
    netText.setAttribute("role", "status");
    var reconnect = el("button", null, copy.reconnect);
    reconnect.type = "button";
    net.appendChild(netText); net.appendChild(reconnect);
    function showNetwork(text, canRetry) {
      label(netText, text || "");
      net.style.display = text ? "flex" : "none";
      reconnect.hidden = !canRetry;
    }
    var off = el("div", "mh-off");
    var thinking = el("div", "mh-thinking", copy.thinking);
    thinking.setAttribute("role", "status");
    thinking.hidden = true;
    var list = el("div", "mh-list");
    var foot = el("div", "mh-foot");
    var picBtn = el("button", "mh-icon");
    picBtn.type = "button";
    var uploadIcon = glyph();
    uploadIcon.classList.add("mh-glyph-upload");
    picBtn.appendChild(uploadIcon);
    label(picBtn, copy.uploadImage, "title");
    label(picBtn, copy.uploadImage, "aria-label");
    var picInput = el("input");
    picInput.type = "file"; picInput.accept = "image/png,image/jpeg,image/gif,image/webp"; picInput.style.display = "none";
    var input = el("textarea");
    label(input, copy.composer, "placeholder");
    input.maxLength = 5000;
    label(input, copy.messageLabel, "aria-label");
    label(input, copy.composerHint, "title");
    var send = el("button", "mh-btn", copy.send);
    send.type = "button";
    foot.appendChild(picBtn); foot.appendChild(picInput); foot.appendChild(input); foot.appendChild(send);

    // 对话前填写资料
    var pre = el("form", "mh-pre");
    pre.innerHTML = '<p></p>' +
      '<input name="name" maxlength="100" autocomplete="name">' +
      '<input name="contact" maxlength="200" autocomplete="tel">' +
      '<div class="mh-err" role="status"></div><button type="submit" class="mh-btn"></button>';
    var preErr = pre.querySelector(".mh-err"), preButton = pre.querySelector("button");
    label(pre.querySelector("p"), copy.profileIntro);
    label(pre.elements.name, copy.name, "placeholder");
    label(pre.elements.contact, copy.profileContact, "placeholder");
    label(preButton, copy.startChat);

    var body = el("div");
    body.style.cssText = "flex:1;display:flex;flex-direction:column;position:relative;min-height:0";
    body.appendChild(off); body.appendChild(list); body.appendChild(thinking); body.appendChild(pre);
    box.appendChild(head); box.appendChild(net); box.appendChild(body); box.appendChild(foot);

    var ws = null, connected = false, retry = 0, retryTimer = null, pingTimer = null;
    var isOpen = false, unread = 0, seen = {}, pending = {}, ui = null;
    var site = null, profileOk = false, unavailable = false;
    var conversationStatus, waitingForHuman = false;
    var drafts = [], profileTimer;
    showPre(false);
    showNetwork(copy.connecting, false);

    function fmtTime(s) { return s ? String(s).slice(11, 16) : ""; }
    function scroll() { list.scrollTop = list.scrollHeight; }
    function abs(path) { return new URL(path, base).href; }
    function setSub(status) {
      conversationStatus = status;
      var online = site && site.online;
      var ai = site && site.ai_available && !waitingForHuman && status !== "closed" && status !== "active";
      dot.className = "mh-dot" + (online || ai ? " on" : "");
      label(subText, status === "closed" ? copy.closed
        : status === "active" ? copy.active : waitingForHuman ? copy.waiting
        : ai ? copy.aiAvailable : online ? copy.online : copy.offline);
      off.style.display = !online && !ai && status !== "active" && off.textContent ? "block" : "none";
    }

    function bubble(sender, msgType, content, time) {
      var cls = sender === "visitor" ? "me" : sender === "system" ? "sys" : sender === "ai" ? "ai" : "them";
      var row = el("div", "mh-row " + cls);
      if (sender === "ai") row.appendChild(el("div", "mh-who", copy.assistant));
      var b = el("div", "mh-b" + (msgType === "image" ? " mh-img" : ""));
      if (msgType === "image") {
        var img = el("img");
        if (content && (content.indexOf("blob:") === 0 || /^\/api\/media\/\d+\?/.test(content))) {
          img.src = content.indexOf("blob:") === 0 ? content : abs(content);
        }
        img.referrerPolicy = "no-referrer";
        label(img, copy.image, "alt");
        img.onload = scroll;
        img.onerror = function () { img.replaceWith(el("span", null, copy.imageBroken)); };
        img.onclick = function () { window.open(img.src, "_blank", "noopener"); };
        b.appendChild(img);
      } else {
        label(b, content || "");
      }
      if (time && sender !== "system") b.appendChild(el("span", "mh-t", fmtTime(time)));
      row.appendChild(b);
      list.appendChild(row);
      return row;
    }
    function render(m) {
      if (seen[m.id]) return;
      seen[m.id] = 1;
      var content = m.msg_type === "image" ? m.image_url
        : m.sender === "system" ? serverMessage(m.content, "") : m.content;
      bubble(m.sender, m.msg_type, content, m.created_at);
    }

    function showPre(show) {
      pre.style.display = show ? "flex" : "none";
      foot.style.display = show ? "none" : "flex";
      send.disabled = picBtn.disabled = !connected || show;
    }

    function delivery(row, message, state, restore) {
      row.className = "mh-row me " + state;
      var status = row.querySelector(".mh-delivery") || el("div", "mh-delivery");
      status.setAttribute("role", "status");
      var statusText = el("span", null, message);
      status.replaceChildren(statusText);
      if (restore) {
        var button = el("button", null, copy.restore);
        button.type = "button";
        button.onclick = function () {
          var draft = input.value + (input.value ? "\n" : "") + restore;
          if (draft.length > input.maxLength) {
            label(statusText, copy.draftTooLong);
            return;
          }
          input.value = draft;
          input.focus();
          button.remove();
        };
        status.appendChild(button);
      }
      row.appendChild(status);
    }

    function keepUnsent(cid, message) {
      var p = pending[cid];
      if (!p) return;
      clearTimeout(p.timer);
      if (p.preview) {
        URL.revokeObjectURL(p.preview);
        p.row.querySelector("img")?.replaceWith(el("span", null, copy.imageUnsent));
      }
      delivery(p.row, message, "failed", p.msgType === "text" && p.content);
      drafts.push(p.row);
      if (drafts.length > 20) drafts.shift().remove();
      delete pending[cid];
    }

    function bump() {
      if (isOpen || !ui || !ui.badge) return;
      unread++;
      ui.badge.textContent = unread > 99 ? "99+" : unread;
      ui.badge.style.display = "block";
    }

    function wsSend(obj) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      try { ws.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
    }

    function connect() {
      if (unavailable || (ws && ws.readyState < WebSocket.CLOSING)) return;
      clearTimeout(retryTimer);
      if (!navigator.onLine) { showNetwork(copy.disconnected, true); return; }
      showNetwork(site ? copy.reconnecting : copy.connecting, false);
      var socket;
      try { socket = new WebSocket(wsUrl.href); } catch (_) { scheduleRetry(); return; }
      ws = socket;
      var deadline = setTimeout(function () { socket.close(); }, 12000);
      var startupError;
      socket.onopen = function () {
        wsSend({ type: "hello", site_key: cfg.siteKey, token: store(TOKEN_KEY) || "", page_url: location.href });
      };
      socket.onmessage = function (ev) {
        if (ws !== socket) return;
        var d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        if (d.type === "pong") {
          clearTimeout(deadline);
        } else if (d.type === "init") {
          clearTimeout(deadline);
          connected = true; retry = 0;
          site = d.site || {};
          if (!d.profile) store(TOKEN_KEY, null);
          profileOk = !site.require_contact || !!(d.profile && d.profile.name && d.profile.contact);
          showNetwork(null);
          label(off, serverMessage(site.offline_text, ""));
          thinking.hidden = true;
          waitingForHuman = !!d.ai_handoff;
          list.textContent = ""; seen = {};
          var welcome = site.welcome === copy.welcome[0] ? "" : site.welcome;
          bubble("agent", "text", welcome || cfg.welcome || copy.welcome);
          (d.messages || []).forEach(render);
          drafts.forEach(function (row) { list.appendChild(row); });
          Object.keys(pending).forEach(function (cid) { list.appendChild(pending[cid].row); });
          setSub(d.conversation && d.conversation.status);
          showPre(!profileOk);
          preButton.disabled = false;
          if (profileOk) label(preErr, "");
          scroll();
          flushPending();
          clearInterval(pingTimer);
          pingTimer = setInterval(function () {
            if (wsSend({ type: "ping" })) deadline = setTimeout(function () { socket.close(); }, 12000);
          }, 25000);
        } else if (d.type === "token") {
          store(TOKEN_KEY, d.token);
        } else if (d.type === "profile_ok") {
          clearTimeout(profileTimer);
          profileOk = true;
          preButton.disabled = false;
          showPre(false);
          if (isOpen && matchMedia("(min-width: 481px)").matches) input.focus();
          flushPending();
        } else if (d.type === "msg") {
          var p = d.client_id && pending[d.client_id];
          if (p) {
            clearTimeout(p.timer);
            seen[d.message.id] = 1;
            p.row.className = "mh-row me";
            p.row.querySelector(".mh-delivery")?.remove();
          if (p.preview) {
            var sentImage = p.row.querySelector("img");
            if (sentImage && /^\/api\/media\/\d+\?/.test(d.message.image_url || "")) {
              sentImage.src = abs(d.message.image_url);
            }
            URL.revokeObjectURL(p.preview);
          }
            p.row.querySelector(".mh-b").appendChild(el("span", "mh-t", fmtTime(d.message.created_at)));
            delete pending[d.client_id];
          } else {
            render(d.message);
            if (d.message.sender !== "visitor") bump();
          }
          scroll();
        } else if (d.type === "ai_status") {
          if (d.handoff && conversationStatus !== "active" && conversationStatus !== "closed") {
            waitingForHuman = true;
            setSub(conversationStatus);
          }
          thinking.hidden = !d.thinking || conversationStatus === "active" || conversationStatus === "closed";
        } else if (d.type === "status") {
          waitingForHuman = false;
          if (d.status === "active" || d.status === "closed") thinking.hidden = true;
          setSub(d.status);
        } else if (d.type === "error") {
          if (!connected) { startupError = d.error; return; }
          var errorMessage = serverMessage(d.error, copy.unavailable);
          if (d.need_profile) { profileOk = false; showPre(true); }
          var f = d.client_id && pending[d.client_id];
          if (f) {
            clearTimeout(f.timer);
            if (d.need_profile) {
              f.sent = false;
              delivery(f.row, copy.sendAfterProfile, "pending");
            } else {
              keepUnsent(d.client_id, errorMessage);
            }
          } else if (pre.style.display === "flex") {
            clearTimeout(profileTimer);
            preButton.disabled = false;
            label(preErr, errorMessage);
          } else {
            bubble("system", "text", errorMessage);
          }
          scroll();
        }
      };
      socket.onclose = function (ev) {
        clearTimeout(deadline);
        if (ws !== socket) return;
        ws = null;
        connected = false;
        thinking.hidden = true;
        clearInterval(pingTimer);
        clearTimeout(profileTimer);
        showPre(site && !profileOk);
        if (preButton.disabled) {
          preButton.disabled = false;
          label(preErr, copy.profileInterrupted);
        }
        Object.keys(pending).forEach(function (cid) {
          if (pending[cid].sent) keepUnsent(cid, copy.deliveryUnconfirmed);
        });
        if (ev.code === 4403) {
          unavailable = true;
          showNetwork(serverMessage(startupError, copy.chatUnavailable), true);
          return;
        }
        if (isOpen || store(TOKEN_KEY)) scheduleRetry();
      };
    }
    function scheduleRetry() {
      clearTimeout(retryTimer);
      showNetwork(navigator.onLine ? copy.reconnecting : copy.disconnected, true);
      if (navigator.onLine) retryTimer = setTimeout(connect, Math.min(30000, 1000 * Math.pow(2, retry++)));
    }
    reconnect.onclick = function () {
      unavailable = false;
      connect();
    };
    window.addEventListener("online", function () {
      if (isOpen || store(TOKEN_KEY)) connect();
    });
    window.addEventListener("offline", function () {
      if (!isOpen && !ws) return;
      ws?.close();
      showNetwork(copy.disconnected, true);
    });

    function flushPending() {
      if (!connected || !profileOk) return;
      Object.keys(pending).forEach(function (cid) {
        var p = pending[cid];
        if (!p.sent && p.content && wsSend({ type: "msg", msg_type: p.msgType, content: p.content, client_id: cid, page_url: location.href })) {
          p.sent = true;
          delivery(p.row, copy.sending, "pending");
          p.timer = setTimeout(function () {
            keepUnsent(cid, copy.deliveryUnconfirmed);
            if (ws) ws.close();
          }, 15000);
        }
      });
    }

    function newCid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

    function doSend() {
      var text = input.value.trim();
      if (!text || !connected || !profileOk) return;
      var cid = newCid();
      pending[cid] = { msgType: "text", content: text, row: bubble("visitor", "text", text), sent: false };
      pending[cid].row.className = "mh-row me pending";
      input.value = "";
      scroll();
      flushPending();
    }

    function sendImage(file) {
      if (!file || !connected || !profileOk || !/^image\//.test(file.type)) return;
      if (file.size > 5 * 1024 * 1024) { bubble("system", "text", copy.imageTooLarge); scroll(); return; }
      var token = store(TOKEN_KEY);
      if (!token) { bubble("system", "text", copy.sendTextFirst); scroll(); return; }
      var cid = newCid();
      var preview = URL.createObjectURL(file);
      var p = pending[cid] = { msgType: "image", content: "", preview: preview, row: bubble("visitor", "image", preview), sent: false };
      delivery(p.row, copy.uploading, "pending");
      scroll();
      var fd = new FormData();
      fd.append("site_key", cfg.siteKey);
      fd.append("token", token);
      fd.append("file", file);
      post(uploadUrl, { method: "POST", body: fd })
        .then(function (r) {
          if (!r.url) throw widgetError(responseError(r, copy.uploadFailed));
          p.content = r.url;
          if (connected) flushPending();
          else { delivery(p.row, copy.sendWhenConnected, "pending"); connect(); }
        })
        .catch(function (err) {
          var detail = err.widgetCopy || err.message;
          keepUnsent(cid, copy.imageSendFailed.map(function (prefix, index) {
            return prefix + " · " + (Array.isArray(detail) ? detail[index] : detail);
          }));
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
      if (preButton.disabled) return;
      label(preErr, "");
      var name = pre.querySelector("[name=name]").value.trim();
      var contact = pre.querySelector("[name=contact]").value.trim();
      if (!name) { label(preErr, copy.nameRequired); return; }
      if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(contact) && (contact.replace(/\D/g, "").length < 7 || !/^\+?[\d\- ()]+$/.test(contact))) {
        label(preErr, copy.invalidContact); return;
      }
      if (!connected) { label(preErr, copy.waitForConnection); connect(); return; }
      if (wsSend({ type: "profile", name: name, contact: contact })) {
        preButton.disabled = true;
        label(preErr, copy.saving);
        profileTimer = setTimeout(function () {
          preButton.disabled = false;
          label(preErr, copy.saveUnconfirmed);
        }, 12000);
      } else {
        label(preErr, copy.profileInterrupted);
      }
    });

    bubble("agent", "text", cfg.welcome || copy.welcome);
    ready(function () {
      ui = mount(box, function (open) {
        isOpen = open;
        if (open) {
          unread = 0;
          if (ui && ui.badge) ui.badge.style.display = "none";
          connect();
          setTimeout(scroll, 0);
        }
      });
      if (ui.inline) { isOpen = true; connect(); }
      else if (store(TOKEN_KEY)) connect();  // 老访客：后台连着，客服回复时按钮上显示未读数
    });
  }

  if (cfg.mode === "form") formWidget(); else chatWidget();
})();
