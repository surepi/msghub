(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STATUS = { new: "未读", read: "已读", handled: "已处理", spam: "垃圾" };
  // 访客提交的链接只允许 http(s)，防止 javascript: 等伪协议
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
  const ORIGIN = location.origin;

  let token = "";
  try { token = localStorage.getItem("msghub_token") || ""; } catch (_) {}
  const state = { page: 1, size: 20, sites: [], items: [], editingSite: null, messageSeq: 0, messageId: null, listSeq: 0 };
  const related = { convId: null, page: 1, seq: 0 };
  const messageDrafts = new Map();
  let relatedCountSeq = 0;
  const qqLog = { page: 1, seq: 0, detailSeq: 0, detailId: null, busy: new Set() };
  const triageView = {seq:0,convId:null,selected:null,busy:new Set()};
  const triageKind = kind => ({priority:"高优先级提醒",handoff:"转人工通知",visitor:"访客消息"}[kind] || "访客消息");
  const summaryView = { seq: 0, convId: null, busy: new Set(), selected: null };
  const aiLog = { seq: 0, convId: null, page: 1, messageId: null, deliveryId: null, detailId: null };

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 2200);
  }

  async function api(path, opts = {}) {
    const requestToken = token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeout ?? (opts.method && opts.method !== "GET" ? 60000 : 15000));
    try {
      const form = opts.body instanceof FormData;
      const res = await fetch(path, {
        method: opts.method,
        headers: { ...(form ? {} : {"Content-Type":"application/json"}), Authorization: "Bearer " + requestToken, ...opts.headers },
        body: form || typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body),
        signal: controller.signal,
      });
      if (res.status === 401 && !path.endsWith("/login") && requestToken === token) {
        logout();
        $("#loginErr").textContent = "登录已过期，请重新登录";
        throw new Error("请重新登录");
      }
      const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
      if (!res.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "请求失败，请稍后再试");
      return data;
    } catch (err) {
      if (controller.signal.aborted) throw new Error("请求超时，请检查网络后重试");
      if (err instanceof TypeError) throw new Error("无法连接服务器，请检查网络后重试");
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  const panelRequests = new WeakMap();
  function panelStatus(panel, text, retry, loading = false) {
    const box = document.createElement(panel.tagName === "TBODY" ? "tr" : "div");
    const content = box.tagName === "TR" ? box.insertCell() : box;
    if (content !== box) content.colSpan = 6;
    content.className = "panel-status";
    const message = document.createElement("span");
    message.setAttribute("role", retry ? "alert" : "status");
    message.textContent = text;
    content.append(message);
    if (retry) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn sm";
      button.textContent = "重新加载";
      button.onclick = () => Promise.resolve(retry()).catch(() => {});
      content.append(button);
    }
    panel.replaceChildren(box);
    panel.setAttribute("aria-busy", String(loading));
  }

  async function panelData(selector, label, read, retry, current = () => true, quiet = false) {
    const panel = $(selector);
    const request = {};
    const session = token;
    panelRequests.set(panel, request);
    const active = () => !!session && session === token && panelRequests.get(panel) === request && current();
    if (!quiet || !panel.children.length) panelStatus(panel, "正在加载" + label + "…", null, true);
    try {
      const data = await read();
      return active() ? data : undefined;
    } catch (err) {
      if (active()) panelStatus(panel, label + "加载失败 · " + err.message, retry);
    } finally {
      if (panelRequests.get(panel) === request) panel.setAttribute("aria-busy", "false");
    }
  }

  // ---------- 登录 ----------
  function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
  function logout() {
    token = "";
    ++state.messageSeq; ++state.listSeq; ++related.seq;
    state.messageId = null;
    messageDrafts.clear();
    ++qqLog.seq; ++qqLog.detailSeq;
    ++aiLog.seq; ++summaryView.seq; ++triageView.seq;
    chat.list = [];
    chat.page = 1; chat.total = 0;
    ++chat.listSeq;
    chat.selected.clear();
    closePane();
    chat.notes = [];
    quickReplies = [];
    $$(".drawer").forEach((d) => d.classList.add("hidden"));
    clearTimeout(chat.timer);
    clearInterval(chat.ping);
    const ws = chat.ws;
    chat.ws = null;
    ws?.close();
    chat.retry = 0;
    chat.wasLive = false;
    chat.recovering = false;
    chat.missed = [];
    setLive("connecting");
    try { localStorage.removeItem("msghub_token"); } catch (_) {}
    showLogin();
  }
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = $('button[type="submit"]', e.target);
    if (button.disabled) return;
    button.disabled = true;
    $("#loginErr").textContent = "正在登录…";
    try {
      const r = await api("/api/admin/login", { method: "POST", body: { password: e.target.password.value } });
      token = r.token;
      try { localStorage.setItem("msghub_token", token); } catch (_) {}
      e.target.reset();
      $("#loginErr").textContent = "";
      boot();
    } catch (err) { $("#loginErr").textContent = err.message; }
    finally { button.disabled = false; }
  });
  $("#logout").onclick = async () => {
    try {
      await api("/api/admin/logout", { method: "POST" });
      logout();
    } catch (error) {
      toast(error.message);
    }
  };

  // ---------- 路由 ----------
  function route() {
    const v = ["#sites", "#messages", "#ai", "#replies"].includes(location.hash) ? location.hash.slice(1) : "chats";
    const copy = {
      chats: ["对话", "集中处理正在进行的访客会话。"],
      messages: ["留言", "查看、筛选和处理来自各个站点的留言。"],
      sites: ["站点", "管理接入网站、挂件配置和 QQ 客服连接。"],
      ai: ["AI", "维护自动应答设置、问答库和网站资料。"],
      replies: ["快捷回复", "维护常用话术，在会话中插入后编辑发送。"],
    };
    $("#pageTitle").textContent = copy[v][0];
    $("#pageSubtitle").textContent = copy[v][1];
    $$(".top nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
    ["chats", "messages", "sites", "ai", "replies"].forEach((n) => $("#view-" + n).classList.toggle("hidden", v !== n));
    document.body.dataset.view = v;
    if (!token) return;
    if (v === "chats") markCurRead();
    if (v === "sites") { loadSites(); loadQQ(); }
    if (v === "ai") loadAI();
    if (v === "messages") loadMessages().catch((err) => toast(err.message));
    if (v === "replies") { renderReplyScopes(); loadQuickReplies(); }
  }
  window.addEventListener("hashchange", route);

  function renderReplyScopes() {
    const sel = $("#replyScope"), value = sel.value;
    sel.innerHTML = '<option value="">全部范围</option><option value="global">仅全局</option>' +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}（含全局）</option>`).join("");
    sel.value = [...sel.options].some((o) => o.value === value) ? value : "";
  }

  // ---------- 统计 ----------
  async function loadStats() {
    const s = await panelData("#stats", "统计", () => api("/api/admin/stats"), loadStats, undefined, true);
    if (!s) return;
    const cells = [
      ["待回复对话", s.conv_waiting], ["进行中对话", s.conv_open],
      ["今日新增留言", s.today], ["未读留言", s.by_status.new], ["接入站点", s.sites],
    ];
    const badge = $("#navUnread");
    badge.textContent = s.conv_unread;
    badge.classList.toggle("hidden", !s.conv_unread);
    document.title = (s.conv_unread ? `(${s.conv_unread}) ` : "") + "MsgHub 留言汇";
    $("#stats").innerHTML = cells.map(([k, v]) => `<div class="card stat"><b>${v}</b><span>${k}</span></div>`).join("");
  }

  // ---------- 站点 ----------
  async function loadSites() {
    const sites = await panelData("#siteList", "站点列表", () => api("/api/admin/sites"), loadSites);
    if (!sites) return;
    state.sites = sites;
    renderSites();
    const sel = $("#fSite"), cur = sel.value;
    sel.innerHTML = '<option value="">全部站点</option>' +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    sel.value = cur;
    const convSite = $("#cSite"), oldSite = convSite.value;
    convSite.innerHTML = '<option value="">全部站点</option>' +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    convSite.value = state.sites.some((s) => String(s.id) === oldSite) ? oldSite : "";
    renderReplyScopes();
    const kbSite = $("#kbSite").value;
    siteOptions($("#aiTestSite"), false, $("#aiTestSite").value || undefined);
    siteOptions($("#qaSite"), true, $("#qaSite").value);
    siteOptions($("#kbSite"), false, kbSite || undefined);
    if (kbSite !== $("#kbSite").value && document.body.dataset.view === "ai") loadKB();

  }

  function snippets(s) {
    const widget = `<div id="msghub"></div>\n<script src="${ORIGIN}/embed.js" data-site-key="${s.site_key}" data-target="#msghub"></script>`;
    const fab = `<script src="${ORIGIN}/embed.js" data-site-key="${s.site_key}"></script>`;
    const form = `<form action="${ORIGIN}/api/public/messages" method="post">\n  <input type="hidden" name="site_key" value="${s.site_key}">\n  <input type="hidden" name="_redirect" value="https://你的网站/thanks.html">\n  <input name="name" placeholder="称呼">\n  <input name="contact" placeholder="联系方式">\n  <textarea name="content" required></textarea>\n  <input name="_hp" style="display:none">\n  <button>提交</button>\n</form>`;
    const curl = `curl -X POST ${ORIGIN}/api/public/messages \\\n  -H "Content-Type: application/json" \\\n  -H "X-Site-Key: ${s.site_key}" \\\n  -d '{"name":"张三","contact":"138xxxx","content":"你好"}'`;
    const formWidget = `<script src="${ORIGIN}/embed.js" data-site-key="${s.site_key}" data-mode="form"></script>`;
    return [["在线对话（右下角按钮，推荐）", fab], ["在线对话（嵌入页面指定位置）", widget],
      ["留言表单挂件", formWidget], ["自有 HTML 表单", form], ["后端 API 调用", curl]]
      .map(([t, c]) => `<p class="muted" style="margin:8px 0 4px">${t}</p><pre>${esc(c)}</pre>`).join("");
  }

  function renderSites() {
    const list = $("#siteList");
    if (!state.sites.length) {
      list.innerHTML = '<div class="card empty">还没有站点，点击右上角「新建站点」开始接入。</div>';
      return;
    }
    list.innerHTML = state.sites.map((s) => `
      <div class="card site" data-id="${s.id}">
        <div class="site-head">
          <h3>${esc(s.name)}</h3>
          ${s.enabled ? '<span class="tag handled">启用</span>' : '<span class="tag spam">停用</span>'}
          <span class="muted">共 ${s.total} 条 · 未读 ${s.unread}</span>
          <span class="spacer"></span>
          <button class="btn sm" data-act="preview">预览挂件</button>
          <button class="btn sm" data-act="view">查看留言</button>
          <button class="btn sm" data-act="edit">编辑</button>
          <button class="btn sm" data-act="rotate">重置 Key</button>
          <button class="btn sm danger" data-act="del">删除</button>
        </div>
        <dl class="kv">
          <dt>域名</dt><dd>${esc(s.domain) || "—"}</dd>
          <dt>Site Key</dt><dd><code>${esc(s.site_key)}</code> <button class="btn sm ghost" data-act="copy">复制</button></dd>
          <dt>允许来源</dt><dd>${esc(s.allowed_origins) || "不限制"}</dd>
          <dt>Webhook</dt><dd>${esc(s.webhook_url) || "—"}</dd>
        </dl>
        <details><summary>接入代码</summary>${snippets(s)}</details>
      </div>`).join("");
  }

  $("#siteList").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = +btn.closest(".site").dataset.id;
    const s = state.sites.find((x) => x.id === id);
    const act = btn.dataset.act;
    try {
      if (act === "copy") { await navigator.clipboard.writeText(s.site_key); toast("已复制"); }
      if (act === "preview") window.open(`/static/preview.html?site=${id}`, "_blank", "noopener");
      if (act === "view") { $("#fSite").value = id; location.hash = "#messages"; state.page = 1; loadMessages(); }
      if (act === "edit") openSite(s);
      if (act === "rotate" && confirm("重置后旧 Key 立即失效，网站上的接入代码需要同步更新。确定？")) {
        await api(`/api/admin/sites/${id}/rotate-key`, { method: "POST" });
        await loadSites(); renderSites(); toast("已重置");
      }
      if (act === "del" && confirm(`删除站点「${s.name}」及其全部留言？此操作不可恢复。`)) {
        await api(`/api/admin/sites/${id}`, { method: "DELETE" });
        await Promise.all([loadSites(), loadStats()]); renderSites(); toast("已删除");
      }
    } catch (err) { toast(err.message); }
  });

  function openSite(s) {
    state.editingSite = s || null;
    const f = $("#siteForm");
    f.reset();
    $("#siteModalTitle").textContent = s ? "编辑站点" : "新建站点";
    let wh = null;
    if (s) {
      ["name", "domain", "allowed_origins", "webhook_url", "welcome", "offline_text", "crawl_urls"]
        .forEach((k) => (f[k].value = s[k] || ""));
      f.enabled.checked = !!s.enabled;
      f.require_contact.checked = !!s.require_contact;
      f.ai_enabled.checked = !!s.ai_enabled;
      try { wh = s.work_hours ? JSON.parse(s.work_hours) : null; } catch (_) { wh = null; }
    }
    f.wh_all.checked = !wh;
    $$("[name=wh_day]", f).forEach((c) => (c.checked = wh ? wh.days.includes(+c.value) : +c.value <= 5));
    f.wh_start.value = wh ? wh.start : "09:00";
    f.wh_end.value = wh ? wh.end : "18:00";
    $(".wh-detail", f).classList.toggle("hidden", !wh);
    $("#siteModal").classList.remove("hidden");
  }
  $("#siteForm").wh_all.onchange = (e) => $(".wh-detail").classList.toggle("hidden", e.target.checked);
  $("#btnNewSite").onclick = () => openSite(null);
  $("#siteForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
      name: f.name.value, domain: f.domain.value, allowed_origins: f.allowed_origins.value,
      webhook_url: f.webhook_url.value, enabled: f.enabled.checked,
      welcome: f.welcome.value, offline_text: f.offline_text.value, crawl_urls: f.crawl_urls.value,
      require_contact: f.require_contact.checked, ai_enabled: f.ai_enabled.checked,
      work_hours: f.wh_all.checked ? null : {
        days: $$("[name=wh_day]:checked", f).map((c) => +c.value), start: f.wh_start.value, end: f.wh_end.value,
      },
    };
    try {
      if (state.editingSite) await api(`/api/admin/sites/${state.editingSite.id}`, { method: "PUT", body });
      else await api("/api/admin/sites", { method: "POST", body });
      $("#siteModal").classList.add("hidden");
      await Promise.all([loadSites(), loadStats()]);
      renderSites();
      toast("已保存");
    } catch (err) { toast(err.message); }
  });

  // ---------- 留言 ----------
  function filters() {
    const p = new URLSearchParams();
    if ($("#fSite").value) p.set("site_id", $("#fSite").value);
    if ($("#fStatus").value) p.set("status", $("#fStatus").value);
    if ($("#fQ").value.trim()) p.set("q", $("#fQ").value.trim());
    return p;
  }

  async function loadMessages(quiet = false) {
    const seq = ++state.listSeq;
    const p = filters();
    p.set("page", state.page); p.set("size", state.size);
    if (!quiet) { $("#pager").textContent = ""; $("#chkAll").checked = false; }
    const r = await panelData("#msgBody", "留言", () => api("/api/admin/messages?" + p), loadMessages, () => seq === state.listSeq, quiet);
    if (!r) { if (seq === state.listSeq && $("#msgBody .panel-status")) $("#pager").textContent = ""; return; }
    const pages = Math.max(1, Math.ceil(r.total / r.size));
    if (state.page > pages) { state.page = pages; return loadMessages(); }
    state.items = r.items;
    $("#chkAll").checked = false;
    $("#msgBody").innerHTML = r.items.length ? r.items.map((m) => `
      <tr data-id="${m.id}" class="${m.status === "new" ? "unread" : ""}">
        <td class="w-chk"><input type="checkbox" class="chk" value="${m.id}" aria-label="选择留言 #${m.id}"></td>
        <td class="nowrap message-site">${esc(m.site_name)}<br><span class="message-source muted">${m.linked_conversation_id ? `来自对话 #${m.linked_conversation_id}` : m.conversation_id ? `来源对话 #${m.conversation_id} 不可用` : "独立留言"}</span></td>
        <td class="message-visitor">${esc(m.name) || '<span class="muted">匿名</span>'}<br><span class="muted">${esc(m.contact)}</span></td>
        <td class="clip">${esc(m.content)}</td>
        <td><span class="tag ${m.status}">${STATUS[m.status]}</span></td>
        <td class="nowrap muted message-time">${esc(m.created_at)}</td>
      </tr>`).join("") : '<tr><td colspan="6" class="empty">暂无留言</td></tr>';
    $("#pager").innerHTML = `<span class="muted">共 ${r.total} 条</span>
      <button class="btn sm" ${state.page <= 1 ? "disabled" : ""} data-p="${state.page - 1}">上一页</button>
      <span>${state.page} / ${pages}</span>
      <button class="btn sm" ${state.page >= pages ? "disabled" : ""} data-p="${state.page + 1}">下一页</button>`;
  }

  $("#pager").addEventListener("click", (e) => {
    const p = e.target.dataset.p;
    if (p) { state.page = +p; loadMessages(); }
  });
  const search = () => { state.page = 1; loadMessages().catch((e) => toast(e.message)); };
  $("#btnSearch").onclick = search;
  $("#fSite").onchange = search;
  $("#fStatus").onchange = search;
  $("#fQ").addEventListener("keydown", (e) => e.key === "Enter" && search());
  $("#chkAll").onchange = (e) => $$(".chk").forEach((c) => (c.checked = e.target.checked));

  $("#batchAction").onchange = async (e) => {
    const action = e.target.value;
    e.target.value = "";
    const ids = $$(".chk:checked").map((c) => +c.value);
    if (!action) return;
    if (!ids.length) return toast("请先勾选留言");
    if (action === "delete" && !confirm(`删除选中的 ${ids.length} 条留言？`)) return;
    try {
      await api("/api/admin/messages/batch", { method: "POST", body: { ids, action } });
      await Promise.all([loadMessages(), loadStats()]);
      toast("操作成功");
    } catch (err) { toast(err.message); }
  };

  $("#btnExport").onclick = async () => {
    try {
      const res = await fetch("/api/admin/messages/export.csv?" + filters(), { headers: { Authorization: "Bearer " + token } });
      if (!res.ok) throw new Error("导出失败");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await res.blob());
      a.download = `messages-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { toast(err.message); }
  };

  $("#msgBody").addEventListener("click", async (e) => {
    if (e.target.classList.contains("chk")) return;
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    try { await openMessage(+tr.dataset.id); } catch (err) { toast(err.message); }
  });

  async function openMessage(id, fromRelated = false) {
    const seq = ++state.messageSeq;
    $("#drawer").classList.remove("hidden");
    $("#relatedDrawer").classList.add("hidden");
    const m = await panelData("#drawerBody", "留言详情", () => api(`/api/admin/messages/${id}`),
      () => openMessage(id, fromRelated), () => seq === state.messageSeq);
    if (!m) return;
    state.messageId = id;
    $("#relatedDrawer").classList.add("hidden");
    $("#drawerBody").innerHTML = `
      ${fromRelated ? '<button class="btn sm" type="button" id="backToRelated">← 返回关联留言</button>' : ""}
      <dl class="kv">
        <dt>留言编号</dt><dd>#${m.id}</dd>
        <dt>站点</dt><dd>${esc(m.site_name)}</dd>
        <dt>姓名</dt><dd>${esc(m.name) || "—"}</dd>
        <dt>联系方式</dt><dd>${esc(m.contact) || "—"}</dd>
        <dt>来源页面</dt><dd>${safeUrl(m.page_url) ? `<a href="${esc(m.page_url)}" target="_blank" rel="noopener noreferrer">${esc(m.page_url)}</a>` : esc(m.page_url) || "—"}</dd>
        <dt>IP</dt><dd>${esc(m.ip)}</dd>
        <dt>时间</dt><dd>${esc(m.created_at)}</dd>
        <dt>处理状态</dt><dd><span id="messageStatus" class="tag ${m.status}">${STATUS[m.status]}</span></dd>
        <dt>来源会话</dt><dd>${m.linked_conversation_id
          ? `<button type="button" id="viewSourceConv" class="btn sm" data-conv-id="${m.linked_conversation_id}">查看对话 #${m.linked_conversation_id}</button>`
          : m.conversation_id ? `对话 #${m.conversation_id} 已删除或不可用，留言仍保留。` : "独立留言（未关联会话）"}</dd>
      </dl>
      <div class="content-box">${esc(m.content)}</div>
      <label>内部备注<textarea id="noteInput" placeholder="处理记录、跟进情况…">${esc(messageDrafts.has(id) ? messageDrafts.get(id) : m.note)}</textarea></label>
      <div class="actions">
        <button class="btn primary" data-s="handled">保存并标为已处理</button>
        <button class="btn" data-s="">仅保存备注</button>
        <button class="btn" data-s="spam">标为垃圾</button>
        <button class="btn danger" data-del>删除</button>
      </div>`;
    $("#drawer").classList.remove("hidden");
    $("#noteInput").oninput = (e) => messageDrafts.set(id, e.target.value);
    if (fromRelated) $("#backToRelated").onclick = () => {
      ++state.messageSeq;
      $("#drawer").classList.add("hidden");
      showRelated(related.convId, false).catch((err) => toast(err.message));
    };
    if (m.linked_conversation_id) $("#viewSourceConv").onclick = async (e) => {
      e.target.disabled = true;
      try {
        location.hash = "#chats"; route();
        if (!chat.cur || chat.cur.id !== m.linked_conversation_id) await openConv(m.linked_conversation_id);
        if (seq !== state.messageSeq || !token) return;
        $(".chat-layout").classList.add("has-conv");
        $("#drawer").classList.add("hidden");
      } catch (err) { toast(err.message); }
      finally { e.target.disabled = false; }
    };
    $("#drawerBody .actions").onclick = async (e) => {
      const b = e.target.closest("button");
      if (!b || b.disabled) return;
      b.disabled = true;
      const note = $("#noteInput").value;
      try {
        if (b.hasAttribute("data-del")) {
          if (!confirm("删除这条留言？")) return;
          await api(`/api/admin/messages/${m.id}`, { method: "DELETE" });
          messageDrafts.delete(id);
        } else {
          const body = { note };
          if (b.dataset.s) body.status = b.dataset.s;
          await api(`/api/admin/messages/${m.id}`, { method: "PATCH", body });
          if (!messageDrafts.has(id) || messageDrafts.get(id) === note) messageDrafts.delete(id);
        }
        if (seq === state.messageSeq) $("#drawer").classList.add("hidden");
        await Promise.all([loadMessages(), loadStats(), refreshRelatedCount()]);
        toast("已保存");
      } catch (err) { toast(err.message); }
      finally { b.disabled = false; }
    };
    if (m.status === "new") {
      const read = await api(`/api/admin/messages/${id}/read`, { method: "POST" });
      if (seq === state.messageSeq && $("#messageStatus")) {
        $("#messageStatus").textContent = STATUS[read.status]; $("#messageStatus").className = "tag " + read.status;
      }
    }
  }

  async function refreshRelatedCount() {
    if (!chat.cur || !token) return;
    const id = chat.cur.id, seq = chat.openSeq, countSeq = ++relatedCountSeq;
    const r = await api(`/api/admin/conversations/${id}/inbox-messages?size=1`);
    if (chat.cur?.id === id && chat.openSeq === seq && countSeq === relatedCountSeq && $("#relatedCount")) $("#relatedCount").textContent = r.total;
  }

  async function showRelated(convId, reset = true) {
    if (!convId || !token) return;
    related.convId = convId;
    if (reset) related.page = 1;
    const seq = ++related.seq;
    $("#relatedDrawer").classList.remove("hidden");
    $("#relatedTitle").textContent = `对话 #${convId} 的关联留言`;
    $("#relatedPager").innerHTML = "";
    try {
      const r = await panelData("#relatedList", "关联留言", () => api(`/api/admin/conversations/${convId}/inbox-messages?page=${related.page}&size=10`),
        () => showRelated(convId, false), () => seq === related.seq);
      if (!r) return;
      const pages = Math.max(1, Math.ceil(r.total / r.size));
      if (related.page > pages) { related.page = pages; return showRelated(convId, false); }
      $("#relatedList").innerHTML = r.items.length ? r.items.map((m) => `
        <article class="related-message">
          <div class="related-message-head"><b>留言 #${m.id}</b><span class="tag ${m.status}">${STATUS[m.status]}</span></div>
          <time class="muted">${esc(m.created_at)}</time><p>${esc(m.content)}</p>
          <button class="btn sm" type="button" data-open-message="${m.id}">查看并处理</button>
        </article>`).join("") : '<p class="empty">此会话暂无关联留言。</p>';
      $("#relatedPager").innerHTML = `<span class="muted">${related.page}/${pages} · 共 ${r.total} 条</span>
        <button class="btn sm" type="button" data-related-page="${related.page - 1}" ${related.page <= 1 ? "disabled" : ""}>上一页</button>
        <button class="btn sm" type="button" data-related-page="${related.page + 1}" ${related.page >= pages ? "disabled" : ""}>下一页</button>`;
    } catch (err) {
      if (seq === related.seq) $("#relatedList").textContent = err.message;
      throw err;
    }
  }
  $("#relatedList").onclick = (e) => {
    const b = e.target.closest("[data-open-message]");
    if (b) openMessage(+b.dataset.openMessage, true).catch((err) => toast(err.message));
  };
  $("#relatedPager").onclick = (e) => {
    const b = e.target.closest("[data-related-page]");
    if (!b || b.disabled) return;
    related.page = +b.dataset.relatedPage;
    showRelated(related.convId, false).catch((err) => toast(err.message));
  };

  // 关闭弹层
  $$(".drawer").forEach((d) => d.addEventListener("click", (e) => {
    if (e.target === d || e.target.closest("[data-close]")) {
      d.classList.add("hidden");
      if (d.id === "drawer") ++state.messageSeq;
      if (d.id === "relatedDrawer") ++related.seq;
      if (d.id === "qqDeliveryDrawer") { ++qqLog.seq; ++qqLog.detailSeq; qqLog.detailId = null; }
      if (d.id === "aiDecisionDrawer") ++aiLog.seq;
      if (d.id === "summaryDrawer") ++summaryView.seq;
      if (d.id === "triageDrawer") ++triageView.seq;
    }
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    ++state.messageSeq; ++related.seq;
    ++qqLog.seq; ++qqLog.detailSeq; qqLog.detailId = null;
    ++aiLog.seq; ++summaryView.seq; ++triageView.seq;
    $$(".drawer").forEach((d) => d.classList.add("hidden"));
  });

  // ---------- AI ----------
  function siteOptions(sel, all, value) {
    sel.innerHTML = (all ? '<option value="">所有站点</option>' : "") +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    if (value !== undefined && value !== null) sel.value = String(value);
    if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  }

  // ---------- 快捷回复 ----------
  let quickReplies = [], editingReply = null;
  async function loadQuickReplies() {
    const items = await panelData("#quickReplyList", "快捷回复", () => api("/api/admin/quick-replies"), loadQuickReplies, undefined, true);
    if (!items) return;
    quickReplies = items;
    const scope = $("#replyScope").value;
    const visible = items.filter((q) => !scope || (scope === "global" ? q.site_id === null : !q.site_id || q.site_id === +scope));
    $("#quickReplyList").innerHTML = visible.length ? visible.map((q) => `
      <article class="card quick-reply" data-id="${q.id}">
        <div class="site-head"><h3>${esc(q.title)}</h3><span class="tag read">${esc(q.site_name || "所有站点")}</span>
          <span class="spacer"></span><button class="btn sm" data-qr="edit">编辑</button>
          <button class="btn sm danger" data-qr="delete">删除</button></div>
        <p>${esc(q.content)}</p>
      </article>`).join("") : '<div class="card empty"><strong>还没有快捷回复</strong>添加常用话术，在会话输入框中一键插入。</div>';
    renderReplyPicker();
  }
  function openQuickReply(q) {
    editingReply = q;
    const f = $("#quickReplyForm");
    f.reset();
    siteOptions(f.elements.site_id, true, q ? q.site_id || "" : +$("#replyScope").value || "");
    f.elements.title.value = q ? q.title : "";
    f.elements.content.value = q ? q.content : "";
    $("#quickReplyModalTitle").textContent = q ? "编辑快捷回复" : "添加快捷回复";
    $("#quickReplyModal").classList.remove("hidden");
    f.elements.title.focus();
  }
  $("#replyNew").onclick = () => openQuickReply(null);
  $("#replyScope").onchange = () => loadQuickReplies().catch((err) => toast(err.message));
  $("#quickReplyList").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-qr]");
    if (!b) return;
    const q = quickReplies.find((x) => x.id === +b.closest("[data-id]").dataset.id);
    if (!q) return;
    if (b.dataset.qr === "edit") return openQuickReply(q);
    if (!confirm(`删除快捷回复「${q.title}」？`)) return;
    try {
      await api(`/api/admin/quick-replies/${q.id}`, { method: "DELETE" });
      await loadQuickReplies();
      toast("已删除");
    } catch (err) { toast(err.message); }
  });
  $("#quickReplyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = $("button[type=submit]", f), id = editingReply && editingReply.id;
    btn.disabled = true;
    try {
      await api("/api/admin/quick-replies" + (id ? `/${id}` : ""), {
        method: id ? "PUT" : "POST",
        body: { title: f.elements.title.value, content: f.elements.content.value, site_id: +f.elements.site_id.value || null },
      });
      $("#quickReplyModal").classList.add("hidden");
      await loadQuickReplies();
      toast("已保存");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });
  function renderReplyPicker() {
    const sel = $("#replyPicker");
    if (!sel || !chat.cur) return;
    sel.innerHTML = '<option value="">插入快捷回复…</option>' + quickReplies
      .filter((q) => !q.site_id || q.site_id === chat.cur.site_id)
      .map((q) => `<option value="${q.id}">${esc(q.title)}${q.site_id ? "" : " · 全局"}</option>`).join("");
    sel.disabled = sel.options.length === 1;
    if (sel.disabled) sel.options[0].textContent = "暂无适用话术";
  }
  function renderNotes(notes) {
    const box = $("#convNotes");
    if (!box) return;
    box.innerHTML = notes.length ? notes.map((n) => `
      <article class="internal-note"><time>${esc(n.created_at)}</time><p>${esc(n.content)}</p></article>`).join("")
      : '<p class="muted">暂无内部备注。</p>';
  }

  async function loadAI() {
    const f = $("#aiForm");
    const save = $('button[type="submit"]', f);
    save.disabled = true;
    const s = await panelData("#aiState", "AI 设置", () => api("/api/admin/ai/settings"), loadAI);
    if (!s) return;
    save.disabled = false;
    f.enabled.checked = s.enabled;
    f.triage_enabled.checked = s.triage_enabled; f.auto_tags.checked = s.auto_tags;
    f.base_url.value = s.base_url;
    f.model.value = s.model;
    f.prompt.value = s.prompt;
    f.no_reply_minutes.value = s.no_reply_minutes;
    f.api_key.value = "";
    f.api_key.placeholder = s.has_key ? `已设置（${s.key_hint}），不修改请留空` : "填写 API Key";
    const ready = s.enabled && s.has_key && s.model && s.base_url;
    $("#aiState").innerHTML = ready ? '<span class="tag handled">已开启</span>'
      : `<span class="tag read">${s.enabled ? "已开启，但接口信息不完整" : "未开启"}</span>`;
    siteOptions($("#aiTestSite"), false, $("#aiTestSite").value);
    siteOptions($("#qaSite"), true, $("#qaSite").value);
    siteOptions($("#kbSite"), false, $("#kbSite").value);
    loadQA().catch((e) => toast(e.message));
    loadKB().catch((e) => toast(e.message));
  }

  $("#aiForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { enabled: f.enabled.checked, triage_enabled: f.triage_enabled.checked, auto_tags: f.auto_tags.checked, base_url: f.base_url.value, model: f.model.value,
      prompt: f.prompt.value, no_reply_minutes: +f.no_reply_minutes.value || 3 };
    if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
    try { await api("/api/admin/ai/settings", { method: "PUT", body }); toast("已保存"); loadAI(); }
    catch (err) { toast(err.message); }
  });

  $("#aiTestBtn").onclick = async () => {
    const q = $("#aiTestQ").value.trim();
    if (!q) return toast("请输入一个问题");
    const out = $("#aiTestOut");
    out.classList.remove("hidden");
    out.textContent = "AI 思考中…（使用已保存的设置）";
    try {
      const r = await api("/api/admin/ai/test", { method: "POST", body: { question: q, site_id: +$("#aiTestSite").value || null } });
      out.innerHTML = `<b>${r.handoff ? "转人工" : "AI 回答"}：</b>${esc(r.answer) || "（无）"}
        <p>${esc(r.reason || "")} · 模型：${esc(r.model || "—")}</p>
        <p class="muted">本次提供给模型的参考资料，供客服核对。测试不会保存为会话记录。</p>${renderAISources(r.sources || [])}
        <details><summary>完整参考文本快照</summary><pre>${esc(r.context || "没有匹配到资料")}</pre></details>`;
    } catch (err) { out.textContent = "测试失败：" + err.message; }
  };

  function renderAISources(sources) {
    return sources.length ? sources.map((s) => `<article class="ai-source"><b>[资料${s.ref}] ${esc(s.title)}</b>
      <p class="muted">${s.type === "qa" ? `问答 #${s.id} · ${esc(s.scope)}` : `网页 · ${esc(s.scope)}${s.fetched_at ? " · 抓取于 " + esc(s.fetched_at) : ""}`}${s.truncated ? " · 摘录已截短" : ""}</p>
      ${s.url ? `<p>${safeUrl(s.url) ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a>` : esc(s.url)}</p>` : ""}
      <div class="delivery-content">${esc(s.snippet)}</div></article>`).join("") : '<p class="empty">本次未检索到可提供的匹配资料。</p>';
  }
  async function openSummary(convId, summaryId = null) {
    summaryView.convId = convId; summaryView.selected = summaryId;
    $("#summaryDrawer").classList.remove("hidden");
    await loadSummary();
  }
  function renderSummary(r) {
    const d = r.item, busy = summaryView.busy.has(summaryView.convId);
    const facts = (items, empty) => items.length ? items.map(n => `<article class="ai-source"><b>${esc(n.value)}</b><p class="muted">访客消息 #${n.message_id}</p><div class="ai-snapshot">${esc(n.quote)}</div></article>`).join("") : `<p class="muted">${empty}</p>`;
    $("#summaryBody").innerHTML = `<p>对话 #${summaryView.convId}</p><button type="button" class="btn primary" data-summary-generate ${busy ? "disabled" : ""}>${busy ? "正在生成…" : "生成 / 更新总结"}</button>
      <p class="muted">需开启全局与本站 AI 并配置模型。相同消息范围复用已有成功总结；新增消息后可更新。</p>
      ${r.history.length ? `<label>最近 20 个总结快照<select id="summaryHistory">${r.history.map(h => `<option value="${h.id}" ${d?.id === h.id ? "selected" : ""}>#${h.id} · ${esc(h.created_at)} · ${h.status === "ready" ? "已生成" : "生成失败"}</option>`).join("")}</select></label>` : ""}
      ${d ? `<article class="ai-source"><p>${esc(d.created_at)} · ${esc(d.model)} · 截至消息 #${d.source_upto}</p>
      ${r.stale ? '<p class="summary-warning">此快照之后有新消息，建议更新总结。</p>' : ""}
      ${d.truncated ? `<p class="summary-warning">记录较长，仅使用最近 ${d.sources.length} 条可见消息（共 ${d.source_count} 条，最多 12000 字），部分消息可能截短。</p>` : ""}
      ${d.status === "ready" ? `<h4>会话总结 · AI 生成</h4><div class="ai-snapshot">${esc(d.summary)}</div>` : `<p role="alert">${esc(d.error)}</p>`}</article>
      ${d.status === "ready" ? `<h4>客户需求 · AI 提取，供核对</h4>${facts(d.needs, "未提取到明确需求")}
      <h4>联系方式 · 待核对</h4>${facts(d.contacts, "未提取到访客明确提供的联系方式")}` : ""}
      <details class="ai-source"><summary>查看本次使用的聊天快照（${d.sources.length} 条）</summary>${d.sources.map(m => `<p class="muted">#${m.id} · ${esc(m.sender === "visitor" ? "访客" : m.sender === "agent" ? "客服" : "AI")} · ${esc(m.created_at)}${m.truncated ? " · 已截短" : ""}</p><div class="ai-snapshot">${esc(m.content)}</div>`).join("")}</details>` : '<p class="empty">尚无会话总结。转人工时自动生成，也可点击上方按钮生成。</p>'}`;
  }
  async function loadSummary(generate = false) {
    const cid = summaryView.convId, seq = ++summaryView.seq;
    $("#summaryBody").textContent = generate ? "正在生成总结…" : "正在加载总结…";
    if (generate) summaryView.busy.add(cid);
    try {
      const r = await api(`/api/admin/conversations/${cid}/summary${!generate && summaryView.selected ? "?summary_id=" + summaryView.selected : ""}`, generate ? { method: "POST" } : {});
      if (seq !== summaryView.seq || !token) return;
      if (generate) summaryView.busy.delete(cid);
      renderSummary(r);
    } catch (err) {
      if (seq === summaryView.seq) $("#summaryBody").innerHTML = `<p role="alert">${esc(err.message)}</p><button class="btn" data-summary-reload>重新加载</button>`;
    } finally {
      if (generate) {
        summaryView.busy.delete(cid);
        if (seq !== summaryView.seq && token && summaryView.convId === cid && !$("#summaryDrawer").classList.contains("hidden")) loadSummary();
      }
    }
  }
  $("#summaryBody").onclick = e => {
    if (e.target.closest("[data-summary-generate]") && !summaryView.busy.has(summaryView.convId)) { summaryView.selected = null; loadSummary(true); }
    if (e.target.closest("[data-summary-reload]")) loadSummary();
  };
  $("#summaryBody").onchange = e => {
    if (e.target.id === "summaryHistory") { summaryView.selected = +e.target.value; loadSummary(); }
  };

  async function openTriage(cid, selected = null) {
    triageView.convId = cid; triageView.selected = selected;
    $("#triageDrawer").classList.remove("hidden");
    await loadTriage();
  }
  function renderTriage(r) {
    const d = r.item, busy = triageView.busy.has(triageView.convId);
    const evidence = (e) => e?.quote ? `<p class="muted">访客消息 #${e.message_id}</p><div class="ai-snapshot">${esc(e.quote)}</div>` : '<p class="muted">未提供有效原话依据</p>';
    $("#triageBody").innerHTML = `<p>对话 #${triageView.convId}</p><div class="actions"><button class="btn primary" data-triage-generate ${busy ? "disabled" : ""}>${busy ? "正在分析…" : "生成 / 更新分类"}</button><button class="btn" data-triage-dismiss ${busy ? "disabled" : ""}>取消高优先级</button></div>
      <p class="muted">当前标签：${r.tags.map(t => esc(t.name)).join("、") || "无"}。相同访客消息范围复用成功分析。</p>
      ${r.history.length ? `<label>最近 20 个分类快照<select id="triageHistory">${r.history.map(h => `<option value="${h.id}" ${d?.id === h.id ? "selected" : ""}>#${h.id} · ${esc(h.created_at)} · ${h.status === "ready" ? "已分析" : "失败"}</option>`).join("")}</select></label>` : ""}
      ${d ? `<article class="ai-source"><p>${esc(d.created_at)} · ${esc(d.model)} · 截至访客消息 #${d.source_upto}</p>${r.stale ? '<p class="summary-warning">有新访客消息，请更新分析再应用标签。</p>' : ""}
        ${d.truncated ? '<p class="summary-warning">聊天记录较长，仅分析最近部分消息，部分内容已截短。</p>' : ""}
        ${d.status === "ready" ? `<h4>AI 分类：${esc(d.category_name)}</h4>${evidence(d.category_evidence)}<h4>AI 建议优先级：${d.priority === "high" ? "高" : "普通"}${d.dismissed ? "（已撤销）" : ""}</h4>${d.priority === "high" ? evidence(d.priority_evidence) : ""}<p class="muted">优先级快照反映当时判断。客服回复或结束会话后，待处理标记和未发送的提醒停止。</p>` : `<p role="alert">${esc(d.error)}</p>`}</article>
        ${d.status === "ready" ? `<h4>标签建议 · 仅使用已有标签</h4>${d.tag_suggestions.length ? d.tag_suggestions.map(t => `<article class="ai-source"><b>${esc(t.name)}</b>${evidence(t)}</article>`).join("") : '<p class="muted">无有效标签建议</p>'}<button class="btn" data-triage-apply="${d.id}" ${busy || r.stale || !d.tag_suggestions.length ? "disabled" : ""}>应用仍有效的标签建议</button>` : ""}
        <details class="ai-source"><summary>查看分析使用的聊天快照（${d.sources.length} 条）</summary>${d.sources.map(m => `<p class="muted">#${m.id} · ${esc(m.sender === "visitor" ? "访客" : m.sender === "agent" ? "客服" : "AI")}${m.truncated ? " · 已截短" : ""}</p><div class="ai-snapshot">${esc(m.content)}</div>`).join("")}</details>` : '<p class="empty">暂无分类。可手动分析，或在 AI 设置开启自动分类，处理之后的新访客消息。</p>'}`;
  }
  async function loadTriage(action = "", id = null) {
    const cid = triageView.convId, seq = ++triageView.seq, mutation = !!action;
    if (mutation) triageView.busy.add(cid);
    $("#triageBody").textContent = mutation ? "正在处理…" : "正在加载分类…";
    const suffix = action === "apply" ? `/${id}/apply-tags` : action === "dismiss" ? "/dismiss" : !mutation && triageView.selected ? "?analysis_id=" + triageView.selected : "";
    try {
      const r = await api(`/api/admin/conversations/${cid}/triage${suffix}`, mutation ? {method:"POST"} : {});
      if (seq !== triageView.seq || !token) return;
      if (mutation) triageView.busy.delete(cid);
      renderTriage(r);
    } catch(err) {
      if (seq === triageView.seq) $("#triageBody").innerHTML = `<p role="alert">${esc(err.message)}</p><button class="btn" data-triage-reload>重新加载</button>`;
    } finally {
      if (mutation) {
        triageView.busy.delete(cid);
        if (seq !== triageView.seq && token && triageView.convId === cid && !$("#triageDrawer").classList.contains("hidden")) loadTriage();
      }
    }
  }
  $("#triageBody").onclick = e => {
    const b = e.target.closest("button"); if (!b || b.disabled) return;
    if (b.hasAttribute("data-triage-reload")) return loadTriage();
    if (triageView.busy.has(triageView.convId)) return;
    if (b.hasAttribute("data-triage-generate")) { triageView.selected = null; return loadTriage("generate"); }
    if (b.hasAttribute("data-triage-dismiss")) return loadTriage("dismiss");
    if (b.dataset.triageApply) return loadTriage("apply",+b.dataset.triageApply);
  };
  $("#triageBody").onchange = e => {
    if (e.target.id === "triageHistory") { triageView.selected = +e.target.value; loadTriage(); }
  };

  const AI_OUTCOME = { answered: "AI 已回答", handoff: "转人工", error: "AI 异常转人工", disabled: "系统转留言" };
  async function openAIDecisions(convId, messageId = null, deliveryId = null) {
    Object.assign(aiLog, { convId, page: 1, messageId, deliveryId, detailId: null });
    $("#aiDecisionDrawer").classList.remove("hidden");
    await loadAIDecisions(true);
  }
  async function loadAIDecisions(autoDetail = false) {
    const seq = ++aiLog.seq, convId = aiLog.convId;
    aiLog.detailId = null;
    $("#aiDecisionBody").textContent = "正在加载处理记录…";
    const p = new URLSearchParams({ page: aiLog.page, size: 10 });
    if (aiLog.messageId) p.set("message_id", aiLog.messageId);
    if (aiLog.deliveryId) p.set("delivery_id", aiLog.deliveryId);
    try {
      const r = await api(`/api/admin/conversations/${convId}/ai-decisions?${p}`);
      if (seq !== aiLog.seq || !token) return;
      if (autoDetail && (aiLog.messageId || aiLog.deliveryId) && r.total === 1) return loadAIDecisionDetail(r.items[0].id);
      const pages = Math.max(1, Math.ceil(r.total / r.size));
      if (aiLog.page > pages) { aiLog.page = pages; return loadAIDecisions(); }
      $("#aiDecisionBody").innerHTML = `<p>对话 #${convId}${aiLog.messageId ? " · 此条 AI 消息" : aiLog.deliveryId ? " · 此条转人工通知" : " · 全部处理记录"}</p>
        ${(aiLog.messageId || aiLog.deliveryId) ? '<button class="btn sm" data-ai-all>查看此会话全部记录</button>' : ""}
        ${r.items.length ? r.items.map((d) => `<article class="ai-source"><b>${AI_OUTCOME[d.outcome]} · ${esc(d.created_at)}</b><p>${esc(d.reason)}</p>
          <p class="muted">${esc(d.trigger)} · ${d.source_count} 条参考资料 · ${esc(d.model || "未调用模型")}</p><button class="btn sm" data-ai-detail="${d.id}">查看资料与依据 #${d.id}</button></article>`).join("") : '<p class="empty">暂无处理记录。此条历史消息或通知尚未保存资料快照。</p>'}
        <div class="pager"><span>${aiLog.page}/${pages} · 共 ${r.total} 条</span><button class="btn sm" data-ai-page="${aiLog.page-1}" ${aiLog.page <= 1 ? "disabled" : ""}>上一页</button><button class="btn sm" data-ai-page="${aiLog.page+1}" ${aiLog.page >= pages ? "disabled" : ""}>下一页</button></div>`;
    } catch (err) { if (seq === aiLog.seq && token) panelStatus($("#aiDecisionBody"), "加载失败 · " + err.message, () => loadAIDecisions(autoDetail)); }
  }
  async function loadAIDecisionDetail(id) {
    const seq = ++aiLog.seq, convId = aiLog.convId;
    aiLog.detailId = id;
    $("#aiDecisionBody").textContent = "正在加载资料快照…";
    try {
      const d = await api(`/api/admin/conversations/${convId}/ai-decisions/${id}`);
      if (seq !== aiLog.seq || !token) return;
      $("#aiDecisionBody").innerHTML = `<button class="btn sm" data-ai-back>← 返回处理记录</button>
        <article class="ai-source"><b>记录 #${d.id} · ${AI_OUTCOME[d.outcome]}</b><p>处理结果：${esc(d.reason)}</p>
        <p class="muted">${esc(d.reason_origin)} · 触发：${esc(d.trigger)} · ${esc(d.created_at)}</p>
        <p class="muted">模型：${esc(d.model || "未调用模型")}${d.message_id ? " · AI 消息 #" + d.message_id : ""}${d.inbox_message_id ? " · 来源留言 #" + d.inbox_message_id : ""}</p>
        ${d.reason_origin === "模型分类" ? '<p class="muted">转人工分类由模型返回，供客服复核。</p>' : ""}</article>
        <h4>访客问题快照${d.question_truncated ? "（较长，仅保存前 6000 字）" : ""}</h4><div class="ai-snapshot">${esc(d.question)}</div>
        <h4>AI 回答快照</h4><div class="ai-snapshot">${esc(d.answer || "本次未生成 AI 回答")}</div>
        <h4>参考资料（${d.sources.length} 条）</h4>${renderAISources(d.sources)}
        <details class="ai-source"><summary>完整参考文本快照</summary><pre class="ai-snapshot">${esc(d.context || "没有匹配到资料")}</pre></details>`;
    } catch (err) { if (seq === aiLog.seq && token) panelStatus($("#aiDecisionBody"), "加载失败 · " + err.message, () => loadAIDecisionDetail(id)); }
  }
  $("#aiDecisionBody").onclick = (e) => {
    const b = e.target.closest("button"); if (!b || b.disabled) return;
    if (b.dataset.aiDetail) return loadAIDecisionDetail(+b.dataset.aiDetail);
    if (b.hasAttribute("data-ai-all")) { aiLog.messageId = null; aiLog.deliveryId = null; aiLog.page = 1; return loadAIDecisions(); }
    if (b.hasAttribute("data-ai-back")) return loadAIDecisions();
    if (b.dataset.aiPage) { aiLog.page = +b.dataset.aiPage; return loadAIDecisions(); }
  };

  let qaItems = [], editingQA = null;
  async function loadQA() {
    const sid = $("#qaSite").value;
    const items = await panelData("#qaList", "问答", () => api("/api/admin/qa" + (sid ? `?site_id=${sid}` : "")), loadQA);
    if (!items) return;
    qaItems = items;
    const siteName = (id) => (id ? (state.sites.find((s) => s.id === id) || {}).name || "已删除的站点" : "所有站点");
    $("#qaList").innerHTML = qaItems.length ? qaItems.map((q) => `
      <div class="qa" data-id="${q.id}">
        <div class="qa-q"><b>${esc(q.question)}</b><span class="muted">${esc(siteName(q.site_id))}</span><span class="spacer"></span>
          <button class="btn sm" data-qa="edit">编辑</button><button class="btn sm danger" data-qa="del">删除</button></div>
        <div class="qa-a">${esc(q.answer)}</div>
      </div>`).join("") : '<div class="empty">还没有问答。添加常见问题和标准答案，AI 会优先参考。</div>';
  }
  function openQA(q) {
    editingQA = q || null;
    const f = $("#qaForm");
    f.reset();
    siteOptions(f.site_id, true, q ? q.site_id || "" : $("#qaSite").value);
    $("#qaModalTitle").textContent = q ? "编辑问答" : "添加问答";
    if (q) { f.question.value = q.question; f.answer.value = q.answer; }
    $("#qaModal").classList.remove("hidden");
  }
  $("#qaNew").onclick = () => openQA(null);
  $("#qaSite").onchange = () => loadQA().catch((e) => toast(e.message));
  $("#qaForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { question: f.question.value, answer: f.answer.value, site_id: +f.site_id.value || null };
    try {
      if (editingQA) await api(`/api/admin/qa/${editingQA.id}`, { method: "PUT", body });
      else await api("/api/admin/qa", { method: "POST", body });
      $("#qaModal").classList.add("hidden");
      toast("已保存");
      loadQA();
    } catch (err) { toast(err.message); }
  });
  $("#qaList").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-qa]");
    if (!b) return;
    const q = qaItems.find((x) => x.id === +b.closest(".qa").dataset.id);
    if (b.dataset.qa === "edit") return openQA(q);
    if (!confirm("删除这条问答？")) return;
    try { await api(`/api/admin/qa/${q.id}`, { method: "DELETE" }); loadQA(); } catch (err) { toast(err.message); }
  });

  let kbTimer;
  async function loadKB() {
    clearTimeout(kbTimer);
    const sid = $("#kbSite").value, box = $("#kbBox");
    if (!sid) { box.innerHTML = '<div class="empty">请先在「站点」页创建站点</div>'; return; }
    const r = await panelData("#kbBox", "网站资料", () => api(`/api/admin/kb/${sid}`), loadKB, () => $("#kbSite").value === sid, true);
    if (!r) return;
    const st = r.state || {};
    const chars = r.pages.reduce((a, p) => a + p.chars, 0);
    box.innerHTML = `
      <dl class="kv">
        <dt>抓取地址</dt><dd>${r.starts.length ? r.starts.map(esc).join("<br>")
          : '<span class="muted">未设置：在站点设置里填写域名或抓取起始地址</span>'}</dd>
        <dt>已收录</dt><dd>${r.pages.length} 个页面${r.pages.length ? `，共 ${chars} 字` : ""}</dd>
        ${st.running ? "<dt>状态</dt><dd>抓取中…</dd>"
          : st.finished_at ? `<dt>上次抓取</dt><dd>${esc(st.finished_at)} · ${st.ok ? `成功，${st.pages} 页` : esc(st.error)}</dd>` : ""}
      </dl>
      <div class="actions">
        <button class="btn sm primary" data-kb="crawl" ${st.running || !r.starts.length ? "disabled" : ""}>立即抓取</button>
        ${r.pages.length ? '<button class="btn sm danger" data-kb="clear">清空</button>' : ""}
      </div>
      ${r.pages.length ? `<details><summary>页面列表</summary><ul class="kb-pages">${r.pages.map((p) =>
        `<li>${esc(p.title || p.url)} <span class="muted">${esc(p.url)} · ${p.chars} 字</span></li>`).join("")}</ul></details>` : ""}`;
    if (st.running) kbTimer = setTimeout(() => loadKB().catch(() => {}), 3000);
  }
  $("#kbSite").onchange = () => loadKB().catch((e) => toast(e.message));
  $("#kbBox").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-kb]");
    if (!b) return;
    const sid = $("#kbSite").value;
    try {
      if (b.dataset.kb === "crawl") { await api(`/api/admin/kb/${sid}/crawl`, { method: "POST" }); toast("已开始抓取"); }
      else { if (!confirm("清空该站点已抓取的网页资料？")) return; await api(`/api/admin/kb/${sid}`, { method: "DELETE" }); }
      setTimeout(() => loadKB().catch(() => {}), 600);
    } catch (err) { toast(err.message); }
  });

  // ---------- QQ 机器人 ----------
  async function loadQQ() {
    const box = $("#qqCard");
    const s = await panelData("#qqCard", "QQ 状态", () => api("/api/admin/qq/status"), loadQQ);
    if (!s) return;
    if (!s.enabled) {
      box.innerHTML = `<h3>QQ 机器人</h3><p class="muted">未配置。在 <code>.env</code> 中填写 <code>QQ_APP_ID</code> 和
        <code>QQ_APP_SECRET</code> 后重启服务，客服即可在 QQ 上收发访客消息。</p><button class="btn sm" data-qq="deliveries">查看投递记录</button>`;
      return;
    }
    const cb = s.callback_path ? location.origin + s.callback_path : "";
    const ok = (b, yes, no) => b ? `<span class="tag handled">${yes}</span>` : `<span class="tag spam">${no}</span>`;
    box.innerHTML = `
      <div class="site-head"><h3>QQ 机器人</h3>${ok(s.token_ok, "凭据有效", "凭据无效")}
        ${ok(s.bound, "客服已绑定", "客服未绑定")}<span class="spacer"></span>
        <button class="btn sm" data-qq="refresh">刷新</button></div>
      <dl class="kv">
        <dt>AppID</dt><dd>${esc(s.app_id)}</dd>
        ${s.token_ok ? "" : `<dt>错误</dt><dd>${esc(s.token_error)}</dd>`}
        <dt>回调地址</dt><dd>${cb ? `<code>${esc(cb)}</code> <button class="btn sm ghost" data-qq="copy">复制</button>
          <br><small class="muted">仅在 q.qq.com 机器人后台填写此地址并订阅单聊消息事件，请保密</small>`
          : "尚未启用接收。请设置 QQ_CALLBACK_KEY 后重新部署。"}</dd>
        <dt>最近回调</dt><dd>${esc(s.last_event_at) || "还没收到过平台回调"}</dd>
        <dt>客服</dt><dd>${s.bound
          ? `${esc(s.openid)} · 绑定于 ${esc(s.bound_at)} · ${s.online ? "在线" : "忙碌"}
             ${s.current_conv_id ? ` · 当前会话 #${s.current_conv_id}` : ""}`
          : "未绑定"}</dd>
        ${s.bound ? `<dt>转发状态</dt><dd>访客消息积压 ${s.pending} 条 · 转人工通知待完成 ${s.handoffs_pending || 0} 条 · 优先级提醒待完成 ${s.priority_pending || 0} 条 · 被动回复窗口${
          s.passive_seconds_left ? `剩余 ${Math.ceil(s.passive_seconds_left / 60)} 分钟` : "已关闭"}</dd>` : ""}
        ${s.bound ? `<dt>QQ 回复接收</dt><dd>待处理 ${s.inbound_pending || 0} 条${s.inbound_failed ? ` · <span class="delivery-error">${s.inbound_failed} 条处理失败，请核对 QQ 中的回复</span>` : ""}</dd>` : ""}
        ${s.bind_code ? `<dt>绑定码</dt><dd><b class="bind-code">${esc(s.bind_code)}</b>
          <br><small class="muted">用客服的 QQ 给机器人发送「/绑定 ${esc(s.bind_code)}」，${Math.ceil(s.bind_expires_in / 60)} 分钟内有效</small></dd>` : ""}
      </dl>
      <div class="actions">
        <button class="btn sm" data-qq="deliveries">查看投递记录</button>
        <button class="btn sm ${s.bound ? "" : "primary"}" data-qq="bind">${s.bound ? "更换客服（重新生成绑定码）" : "生成绑定码"}</button>
        ${s.bound ? '<button class="btn sm" data-qq="test">发送测试消息</button><button class="btn sm danger" data-qq="unbind">解除绑定</button>' : ""}
      </div>`;
    box.dataset.cb = cb;
  }

  $("#qqCard").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-qq]");
    if (!b || b.disabled) return;
    b.disabled = true;
    try {
      switch (b.dataset.qq) {
        case "deliveries": return await openQQDeliveries();
        case "copy": await navigator.clipboard.writeText($("#qqCard").dataset.cb); toast("已复制"); return;
        case "bind": await api("/api/admin/qq/bind-code", { method: "POST" }); break;
        case "test": await api("/api/admin/qq/test", { method: "POST" }); toast("已发送，请查看客服 QQ"); break;
        case "unbind":
          if (!confirm("解除后访客消息不再转发到 QQ，确定？")) return;
          await api("/api/admin/qq/unbind", { method: "POST" });
          break;
      }
      loadQQ();
    } catch (err) { toast(err.message); }
    finally { b.disabled = false; }
  });

  $("#qqDeliveryDetail").addEventListener("click", e => {
    const b = e.target.closest("[data-delivery-summary]");
    if (!b) return;
    ++qqLog.seq; ++qqLog.detailSeq; $("#qqDeliveryDrawer").classList.add("hidden");
    openSummary(+b.dataset.deliverySummary, +b.dataset.summaryId);
  });
  const DELIVERY_STATUS = { pending: "等待发送", sending: "发送中", failed: "失败待重试", uncertain: "结果未确认", sent: "已发送", cancelled: "已取消", skipped: "图片未转发" };
  const deliveryTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";
  function deliverySummary(d) {
    return `<div class="site-head"><b>${triageKind(d.kind)} #${d.id} · 对话 #${d.conversation_id}</b><span class="tag ${d.status === "sent" ? "handled" : ["failed", "uncertain"].includes(d.status) ? "spam" : "read"}">${DELIVERY_STATUS[d.status]}</span></div>
      <p class="muted">${esc(d.site_name)} · ${d.message_count} 条来源消息 · 已发送 ${d.sent_parts}/${d.part_count} 段${d.skipped_parts ? ` · 跳过 ${d.skipped_parts} 段图片（另发提示）` : ""}</p>
      ${d.reason ? `<p><b>转人工原因：</b>${esc(d.reason)}</p>` : ""}
      <p class="muted">尝试 ${d.attempts} 次（重试 ${Math.max(0, d.attempts - 1)} 次） · 最近尝试：${esc(deliveryTime(d.last_attempt_at))}</p>
      ${d.next_attempt_at ? `<p class="muted">自动重试最早时间：${esc(deliveryTime(d.next_attempt_at))}</p>` : ""}
      ${d.last_error ? `<p class="delivery-error">${esc(d.last_error)}</p>` : ""}
      ${d.warning ? `<p class="delivery-error">${esc(d.warning)}</p>` : ""}`;
  }
  async function openQQDeliveries() {
    qqLog.page = 1; qqLog.detailId = null; ++qqLog.detailSeq;
    $("#qqDeliveryDrawer").classList.remove("hidden");
    $("#qqDeliveryListView").classList.remove("hidden"); $("#qqDeliveryDetail").classList.add("hidden");
    await loadQQDeliveries();
  }
  async function loadQQDeliveries() {
    const seq = ++qqLog.seq;
    const p = new URLSearchParams({ page: qqLog.page, size: 20 });
    if ($("#qqDeliveryStatus").value) p.set("status", $("#qqDeliveryStatus").value);
    if ($("#qqDeliveryKind").value) p.set("kind", $("#qqDeliveryKind").value);
    if ($("#qqDeliveryConv").value) p.set("conversation_id", $("#qqDeliveryConv").value);
    const r = await panelData("#qqDeliveryList", "投递记录", () => api("/api/admin/qq/deliveries?" + p),
      loadQQDeliveries, () => seq === qqLog.seq, true);
    if (!r) { if (seq === qqLog.seq) $("#qqDeliveryPager").textContent = ""; return; }
    const pages = Math.max(1, Math.ceil(r.total / r.size));
    if (qqLog.page > pages) { qqLog.page = pages; return loadQQDeliveries(); }
    $("#qqDeliveryList").innerHTML = r.items.length ? r.items.map((d) => `<article class="qq-delivery">${deliverySummary(d)}<button class="btn sm" type="button" data-delivery-id="${d.id}">查看分段与补发</button></article>`).join("") : '<p class="empty">暂无符合条件的投递记录。</p>';
    $("#qqDeliveryPager").innerHTML = `<span>${qqLog.page}/${pages} · 共 ${r.total} 条</span><button class="btn sm" type="button" data-delivery-page="${qqLog.page - 1}" ${qqLog.page <= 1 ? "disabled" : ""}>上一页</button><button class="btn sm" type="button" data-delivery-page="${qqLog.page + 1}" ${qqLog.page >= pages ? "disabled" : ""}>下一页</button>`;
  }
  async function loadQQDeliveryDetail(id) {
    qqLog.detailId = id;
    const seq = ++qqLog.detailSeq;
    const r = await panelData("#qqDeliveryDetail", "投递详情", () => api(`/api/admin/qq/deliveries/${id}`),
      () => loadQQDeliveryDetail(id), () => seq === qqLog.detailSeq, true);
    if (seq !== qqLog.detailSeq || !token || $("#qqDeliveryDrawer").classList.contains("hidden")) return;
    if (!r) {
      $("#qqDeliveryListView").classList.add("hidden"); $("#qqDeliveryDetail").classList.remove("hidden");
      $("#qqDeliveryDetail").insertAdjacentHTML("beforeend", '<button class="btn sm" type="button" data-delivery-back>← 返回投递列表</button>');
      return;
    }
    const d = r.delivery;
    $("#qqDeliveryListView").classList.add("hidden"); $("#qqDeliveryDetail").classList.remove("hidden");
    $("#qqDeliveryDetail").innerHTML = `<button class="btn sm" type="button" data-delivery-back>← 返回投递列表</button><article class="qq-delivery">${deliverySummary(d)}
      <p class="muted">创建：${esc(d.created_at)}${d.sent_at ? " · 完成：" + esc(d.sent_at) : ""}</p>
      ${d.kind !== "visitor" ? `<p><b>访客需求（原话摘录，非 AI 总结）</b></p><div class="delivery-content">${esc(d.summary)}</div>` : ""}
      <div class="actions"><button class="btn sm" type="button" data-delivery-conv="${d.conversation_id}">查看来源对话 #${d.conversation_id}</button>${d.inbox_message_id ? `<button class="btn sm" type="button" data-delivery-inbox="${d.inbox_message_id}">查看来源留言 #${d.inbox_message_id}</button>` : d.kind === "handoff" ? '<span class="muted">来源留言已删除</span>' : ""}${d.triage_id ? `<button class="btn sm" type="button" data-delivery-triage="${d.conversation_id}" data-triage-id="${d.triage_id}">查看优先级依据</button>` : ""}${d.summary_id ? `<button class="btn sm" type="button" data-delivery-summary="${d.conversation_id}" data-summary-id="${d.summary_id}">查看通知中的总结</button>` : ""}${d.kind === "handoff" ? `<button class="btn sm" type="button" data-delivery-evidence="${d.conversation_id}" data-evidence-delivery="${d.id}">查看 AI / 转人工依据</button>` : ""}</div>
      ${["pending", "failed", "uncertain"].includes(d.status) ? `<div class="actions"><button class="btn primary sm" type="button" data-delivery-retry="${id}" data-uncertain="${d.status === "uncertain"}">补发未完成部分</button>${d.status === "uncertain" ? `<button class="btn sm" type="button" data-delivery-confirm="${id}">已在 QQ 确认收到</button>` : ""}</div>` : ""}
      ${d.status === "uncertain" ? '<p class="delivery-error">请求可能已送达 QQ，系统暂停自动补发。请先核对收件情况，再确认收到或选择补发未确认部分。</p>' : ""}
      <p class="muted">自动补发会跳过已发送分段；手动补发须间隔 10 秒。</p></article>
      <h4>来源消息</h4>${r.messages_truncated ? '<p class="muted">仅显示最近 100 条来源消息，完整记录请查看来源对话。</p>' : ""}${r.messages.map((m) => `<div class="qq-delivery"><b>消息 #${m.id} · ${esc(m.created_at)}</b><div class="delivery-content">${esc(m.msg_type === "image" ? "[图片] " + m.content : m.content)}</div></div>`).join("")}
      <h4>QQ 分段进度</h4>${r.parts.map((p) => `<article class="qq-delivery"><b>第 ${p.sequence + 1} 段 · ${{text:"文字",image:"图片",fallback:"图片降级提示"}[p.kind]} · ${DELIVERY_STATUS[p.status]}</b><p class="muted">尝试 ${p.attempts} 次${p.manually_confirmed ? " · 客服人工确认收到" : ""}${p.qq_message_id ? " · QQ 消息编号：" + esc(p.qq_message_id) : ""}</p>${p.last_error ? `<p class="delivery-error">${esc(p.last_error)}</p>` : ""}<div class="delivery-content">${esc(p.content)}</div></article>`).join("")}`;
    if (qqLog.busy.has(id)) $$("[data-delivery-retry], [data-delivery-confirm]", $("#qqDeliveryDetail")).forEach((b) => b.disabled = true);
  }
  $("#qqDeliveryFilters").onsubmit = (e) => { e.preventDefault(); qqLog.page = 1; loadQQDeliveries().catch((err) => toast(err.message)); };
  $("#qqDeliveryPager").onclick = (e) => {
    const b = e.target.closest("[data-delivery-page]"); if (!b || b.disabled) return;
    qqLog.page = +b.dataset.deliveryPage; loadQQDeliveries().catch((err) => toast(err.message));
  };
  $("#qqDeliveryList").onclick = (e) => {
    const b = e.target.closest("[data-delivery-id]"); if (b) loadQQDeliveryDetail(+b.dataset.deliveryId).catch((err) => toast(err.message));
  };
  $("#qqDeliveryDetail").onclick = async (e) => {
    const b = e.target.closest("button"); if (!b || b.disabled) return;
    if (b.dataset.deliveryTriage) {
      ++qqLog.seq; ++qqLog.detailSeq; qqLog.detailId = null;
      $("#qqDeliveryDrawer").classList.add("hidden");
      return openTriage(+b.dataset.deliveryTriage,+b.dataset.triageId);
    }
    if (b.dataset.deliveryEvidence) {
      ++qqLog.seq; ++qqLog.detailSeq; qqLog.detailId = null;
      $("#qqDeliveryDrawer").classList.add("hidden");
      return openAIDecisions(+b.dataset.deliveryEvidence,null,+b.dataset.evidenceDelivery);
    }
    if (b.dataset.deliveryConv || b.dataset.deliveryInbox) {
      ++qqLog.seq; ++qqLog.detailSeq; qqLog.detailId = null;
      $("#qqDeliveryDrawer").classList.add("hidden");
      try {
        if (b.dataset.deliveryInbox) await openMessage(+b.dataset.deliveryInbox);
        else {
          location.hash = "#chats"; route();
          if (!chat.cur || chat.cur.id !== +b.dataset.deliveryConv) await openConv(+b.dataset.deliveryConv);
          $(".chat-layout").classList.add("has-conv");
        }
      } catch (err) { toast(err.message); }
      return;
    }
    if (b.hasAttribute("data-delivery-back")) {
      qqLog.detailId = null; ++qqLog.detailSeq;
      $("#qqDeliveryListView").classList.remove("hidden"); $("#qqDeliveryDetail").classList.add("hidden");
      return loadQQDeliveries().catch((err) => toast(err.message));
    }
    const id = +(b.dataset.deliveryRetry || b.dataset.deliveryConfirm);
    if (!id || qqLog.busy.has(id)) return;
    const confirming = b.hasAttribute("data-delivery-confirm"), uncertain = b.dataset.uncertain === "true";
    if ((confirming || uncertain) && !confirm(confirming ? "确认已在 QQ 收到结果未确认的分段？系统将保留该分段，并继续补发后续未发送部分。" : "请求可能已送达 QQ。确认仍要补发未确认部分？这部分可能重复，已明确成功的分段不会重发。")) return;
    qqLog.busy.add(id);
    $$("[data-delivery-retry], [data-delivery-confirm]", $("#qqDeliveryDetail")).forEach((button) => button.disabled = true);
    try {
      const r = await api(`/api/admin/qq/deliveries/${id}/${confirming ? "confirm" : "retry"}`, { method: "POST", body: confirming ? {} : { confirm_uncertain: uncertain } });
      toast(confirming ? "已记录人工确认" : r.status === "sent" ? "未完成部分已补发" : r.status === "cancelled" ? "该通知已无需处理，已停止补发" : "本次补发未完成，请查看状态和原因");
      if (qqLog.detailId === id) await loadQQDeliveryDetail(id);
    } catch (err) { toast(err.message); }
    finally {
      qqLog.busy.delete(id);
      if (qqLog.detailId === id) $$("[data-delivery-retry], [data-delivery-confirm]", $("#qqDeliveryDetail")).forEach((button) => button.disabled = false);
    }
  };

  // ---------- 对话 ----------
  const chat = { list: [], cur: null, notes: [], selected: new Set(), msgIds: new Set(), ws: null, retry: 0, timer: null, ping: null, openSeq: 0, listSeq: 0,
    page: 1, size: 30, total: 0, loading: false, messages: [], hasOlder: false, hasNewer: false, historyBusy: false };
  let pendingRead = null;
  const CONV_STATUS = { waiting: "待回复", active: "进行中", closed: "已结束" };
  const CONV_TAG = { waiting: "new", active: "handled", closed: "read" };
  const shortTime = (s) => String(s || "").slice(5, 16);
  const convTitle = (c) => `#${c.id} ${c.visitor_name || "访客"}`;
  const EMPTY_PANE = '<div class="chat-empty"><span class="empty-symbol" aria-hidden="true">↗</span><h3>让对话从这里继续</h3><p>选择一条会话，查看访客需求并开始回复。</p></div>';

  function renderConvList() {
    const box = $("#convList");
    box.classList.remove("list-error");
    const visible = new Set(chat.list.map((c) => c.id));
    for (const id of chat.selected) if (!visible.has(id)) chat.selected.delete(id);
    renderSelection();
    renderConvPagination();
    if (!chat.list.length) { box.innerHTML = '<div class="empty">暂无符合条件的对话</div>'; return; }
    box.innerHTML = chat.list.map((c) => `
      <div class="conv ${chat.cur && chat.cur.id === c.id ? "on" : ""}" data-id="${c.id}">
        <div class="conv-top">
          <input class="conv-check" type="checkbox" aria-label="选择会话 #${c.id}" ${chat.selected.has(c.id) ? "checked" : ""}>
          <span class="dot ${c.visitor_online ? "on" : ""}" title="${c.visitor_online ? "访客在线" : "访客离线"}"></span>
          <b>${esc(convTitle(c))}</b>${c.unread ? `<span class="unread">${c.unread}</span>` : ""}
        </div>
        ${c.triage ? `<div class="conv-labels"><span class="conv-label">AI · ${esc(c.triage.category_name)}${c.triage.stale ? " · 待更新" : ""}</span>${c.triage.high ? '<span class="triage-high">高优先级待处理</span>' : ""}</div>` : ""}
        <div class="conv-prev">${esc(c.last_preview) || "&nbsp;"}</div>
        ${c.search_match ? `<button class="conv-match" type="button" data-message-id="${c.search_match.id}" title="查看命中消息及上下文"><span>${$("#cQ").value.trim() ? "内容命中" : "范围内消息"} · ${esc(shortTime(c.search_match.created_at))}</span>${esc(c.search_match.snippet)}</button>` : ""}
        ${(c.tags || []).length ? `<div class="conv-labels">${c.tags.map((t) => `<span class="conv-label">${esc(t.name)}</span>`).join("")}</div>` : ""}
        <div class="conv-meta"><span>${esc(c.site_name)} · ${CONV_STATUS[c.status]}</span><span>${esc(shortTime(c.last_msg_at))}</span></div>
      </div>`).join("");
  }

  function renderConvPagination() {
    const pages = Math.max(1, Math.ceil(chat.total / chat.size));
    $("#convPageInfo").textContent = chat.loading ? "加载中…" : `${chat.page}/${pages} · 共 ${chat.total} 条`;
    $("#convPrev").disabled = chat.loading || chat.page <= 1;
    $("#convNext").disabled = chat.loading || chat.page >= pages;
  }

  function upsertConv(c) {
    if (chat.cur && chat.cur.id === c.id) { chat.cur = c; renderChatHead(); renderConversationTags(); }
    // The server owns filtering, match snippets, ordering and page boundaries.
    scheduleConvRefresh();
  }

  async function loadConvs() {
    const seq = ++chat.listSeq;
    const p = new URLSearchParams({ size: chat.size, page: chat.page });
    if ($("#cStatus").value) p.set("status", $("#cStatus").value);
    if ($("#cQ").value.trim()) p.set("q", $("#cQ").value.trim());
    if ($("#cTag").value) p.set("tag_id", $("#cTag").value);
    if ($("#cSite").value) p.set("site_id", $("#cSite").value);
    if ($("#cCategory").value) p.set("category", $("#cCategory").value);
    if ($("#cPriority").value) p.set("priority", $("#cPriority").value);
    if ($("#cFrom").value) p.set("date_from", $("#cFrom").value);
    if ($("#cTo").value) p.set("date_to", $("#cTo").value);
    chat.loading = true;
    if (!chat.list.length) panelStatus($("#convList"), "正在加载对话…", null, true);
    renderConvPagination();
    try {
      const result = await api("/api/admin/conversations?" + p);
      if (seq !== chat.listSeq || !token) return;
      chat.total = result.total;
      const pages = Math.max(1, Math.ceil(result.total / chat.size));
      if (chat.page > pages) { chat.page = pages; return await loadConvs(); }
      chat.list = result.items;
      renderConvList();
    } catch (err) {
      if (seq === chat.listSeq) {
        chat.list = []; chat.selected.clear();
        renderConvList();
        panelStatus($("#convList"), "对话加载失败 · " + err.message, loadConvs);
      }
      throw err;
    } finally {
      if (seq === chat.listSeq) { chat.loading = false; $("#convList").setAttribute("aria-busy", "false"); renderConvPagination(); }
    }
  }

  function changeConvFilters() {
    clearTimeout(qTimer);
    ++chat.listSeq; // Invalidate an older response as soon as the inputs change.
    chat.page = 1;
    chat.selected.clear();
    chat.list = [];
    chat.total = 0;
    $("#convFilterState").textContent = [$("#cSite").value, $("#cFrom").value, $("#cTo").value, $("#cCategory").value, $("#cPriority").value].some(Boolean) ? "· 已筛选" : "";
    renderConvList();
    if ($("#cFrom").value && $("#cTo").value && $("#cFrom").value > $("#cTo").value) {
      chat.loading = false;
      renderConvPagination();
      return toast("开始日期不能晚于结束日期");
    }
    loadConvs().catch((e) => toast(e.message));
  }

  let convRefreshTimer;
  function scheduleConvRefresh() {
    clearTimeout(convRefreshTimer);
    convRefreshTimer = setTimeout(() => { if (token) loadConvs().catch((e) => toast(e.message)); }, 150);
  }

  // ---------- 会话标签 ----------
  let tagItems = [], tagLoadSeq = 0;
  async function loadTags() {
    const seq = ++tagLoadSeq;
    const items = await panelData("#tagCatalog", "标签", () => api("/api/admin/tags"), loadTags, () => seq === tagLoadSeq, true);
    if (!items) return;
    tagItems = items;
    const filter = $("#cTag"), previous = filter.value;
    filter.innerHTML = '<option value="">全部标签</option>' + items.map((t) =>
      `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    filter.value = items.some((t) => String(t.id) === previous) ? previous : "";
    if (filter.value !== previous) scheduleConvRefresh();
    const batch = $("#batchTag"), oldBatch = batch.value;
    batch.innerHTML = '<option value="">选择标签…</option>' + items.map((t) =>
      `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    if (items.some((t) => String(t.id) === oldBatch)) batch.value = oldBatch;
    $("#tagCatalog").innerHTML = items.length ? items.map((t) => `
      <form class="tag-edit" data-tag-id="${t.id}">
        <label class="sr-only" for="tagName${t.id}">标签名称</label>
        <input id="tagName${t.id}" name="name" maxlength="30" required value="${esc(t.name)}">
        <button class="btn sm" type="submit">保存</button>
        <button class="btn sm danger" type="button" data-delete-tag="${t.id}">删除</button>
      </form>`).join("") : '<p class="empty">暂无标签，添加后即可整理会话。</p>';
    renderConversationTags();
  }

  function renderConversationTags() {
    const box = $("#conversationTags");
    if (!box || !chat.cur) return;
    const assigned = chat.cur.tags || [];
    box.innerHTML = `<span class="muted">标签</span>
      ${assigned.map((t) => `<button type="button" class="conv-label label-remove" data-remove-tag="${t.id}" aria-label="移除标签：${esc(t.name)}">${esc(t.name)} <span aria-hidden="true">×</span></button>`).join("")}
      <select id="addConvTag" aria-label="添加会话标签"><option value="">添加标签…</option>
        ${tagItems.filter((t) => !assigned.some((a) => a.id === t.id)).map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
      </select>`;
  }

  function renderSelection() {
    const all = $("#convSelectAll"), count = chat.selected.size;
    all.checked = !!chat.list.length && count === chat.list.length;
    all.indeterminate = count > 0 && count < chat.list.length;
    all.disabled = !chat.list.length;
    $("#convSelectionCount").textContent = `已选 ${count} 条`;
    $("#tagBatchForm").classList.toggle("hidden", !count);
  }

  async function refreshTagViews() {
    await loadTags();
    await loadConvs();
    if (chat.cur) {
      // Read only tag metadata; don't reload the pane or clear the reply/note drafts.
      const id = chat.cur.id, seq = chat.openSeq;
      const latest = await api(`/api/admin/conversations/${id}/tags`);
      if (chat.cur && chat.cur.id === id && chat.openSeq === seq) {
        chat.cur.tags = latest.tags;
        renderConversationTags();
      }
    }
  }

  $("#manageTags").onclick = async () => {
    $("#tagManager").classList.remove("hidden");
    try { await loadTags(); $("#newTagName").focus(); } catch (e) { toast(e.message); }
  };
  $("#tagCreateForm").onsubmit = async (e) => {
    e.preventDefault();
    const input = $("#newTagName"), name = input.value.trim(), btn = $("button", e.target);
    if (!name || btn.disabled) return;
    btn.disabled = true;
    try {
      await api("/api/admin/tags", { method: "POST", body: { name } });
      if (input.value.trim() === name) input.value = "";
      await loadTags();
      toast("标签已添加");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  };
  $("#tagCatalog").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = $("button[type=submit]", f);
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await api(`/api/admin/tags/${f.dataset.tagId}`, { method: "PUT", body: { name: f.elements.name.value } });
      await refreshTagViews();
      toast("标签已保存");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });
  $("#tagCatalog").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-delete-tag]");
    if (!b || b.disabled) return;
    if (!confirm("删除这个标签及全部会话上的关联？聊天记录会保留。")) return;
    b.disabled = true;
    try {
      await api(`/api/admin/tags/${b.dataset.deleteTag}`, { method: "DELETE" });
      await refreshTagViews();
      toast("标签已删除");
    } catch (err) { toast(err.message); }
    finally { b.disabled = false; }
  });
  $("#convSelectAll").onchange = (e) => {
    chat.selected = e.target.checked ? new Set(chat.list.map((c) => c.id)) : new Set();
    renderConvList();
  };
  $("#convList").addEventListener("change", (e) => {
    if (!e.target.matches(".conv-check")) return;
    const id = +e.target.closest(".conv").dataset.id;
    if (e.target.checked) chat.selected.add(id); else chat.selected.delete(id);
    renderSelection();
  });
  $("#cTag").onchange = changeConvFilters;
  $("#tagBatchForm").onsubmit = async (e) => {
    e.preventDefault();
    const ids = [...chat.selected], tag_id = +$("#batchTag").value, action = e.submitter?.value;
    if (!ids.length || !tag_id || !action) return;
    const buttons = $$("button", e.target);
    if (buttons.some((b) => b.disabled)) return;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/admin/conversation-tags/batch", {
        method: "POST", body: { conversation_ids: ids, tag_id, action },
      });
      ids.forEach((id) => chat.selected.delete(id));
      for (const c of r.conversations) upsertConv(c);
      await loadConvs();
      toast(`已${action === "add" ? "添加" : "移除"}标签，涉及 ${r.affected} 条会话`);
    } catch (err) { toast(err.message); }
    finally { buttons.forEach((b) => { b.disabled = false; }); }
  };

  function appendMsg(m) {
    const box = $("#chatMsgs");
    if (!box || chat.msgIds.has(m.id)) return;
    chat.msgIds.add(m.id);
    $("[data-empty-chat]", box)?.remove();
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const isImg = m.msg_type === "image" && /^\/api\/media\/\d+\?/.test(m.image_url || "");
    const body = isImg
      ? `<a href="${esc(m.image_url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(m.image_url)}" alt="图片" loading="lazy" referrerpolicy="no-referrer"></a>`
      : esc(m.content);
    box.insertAdjacentHTML("beforeend", `<div class="msg ${esc(m.sender)}${m.id === chat.focusMessageId ? " search-target" : ""}" data-message-id="${m.id}">${m.sender === "ai" ? '<div class="who">AI 助手</div>' : ""}
      <div class="bub${isImg ? " img" : ""}">${body}${
      m.sender !== "system" ? `<time>${esc(shortTime(m.created_at))}</time>` : ""}</div>${m.sender === "ai" ? `<button class="btn sm ai-evidence-btn" type="button" data-ai-message="${m.id}">参考资料与处理依据</button>` : ""}</div>`);
    if (isImg) {
      const image = $("img", box.lastElementChild);
      image.onload = () => { if (nearBottom) box.scrollTop = box.scrollHeight; };
      image.onerror = () => { image.parentElement.textContent = "图片无法加载，点击查看原图"; };
    }
    if (nearBottom || m.sender === "agent") box.scrollTop = box.scrollHeight;
  }

  function renderHistoryControls() {
    const older = $("#loadOlder"), newer = $("#loadNewer"), latest = $("#jumpLatest");
    if (!older) return;
    older.classList.toggle("hidden", !chat.hasOlder);
    newer.classList.toggle("hidden", !chat.hasNewer);
    latest.classList.toggle("hidden", !chat.hasNewer);
    [older, newer, latest].forEach((b) => { b.disabled = chat.historyBusy; });
    $("#historyHint").textContent = chat.hasNewer ? "正在查看历史消息" : "";
    $("#historyTools").classList.toggle("hidden", !chat.hasOlder && !chat.hasNewer);
  }

  function renderHistoryMessages() {
    chat.msgIds = new Set();
    $("#chatMsgs").innerHTML = chat.messages.length ? "" : '<p class="empty" data-empty-chat>暂无聊天消息</p>';
    chat.messages.forEach(appendMsg);
  }

  function receiveChatMessage(m) {
    if (chat.recovering) { chat.missed.push(m); return; }
    if (chat.historyBusy) chat.historyLive.push(m);
    if (chat.hasNewer) return; // Keep the historical window contiguous until the agent returns to latest.
    if (chat.msgIds.has(m.id)) return;
    chat.messages.push(m);
    appendMsg(m);
  }

  async function loadHistory(direction) {
    if (!chat.cur || chat.historyBusy || chat.recovering || (direction !== "latest" && !chat.messages.length)) return;
    const id = chat.cur.id, seq = chat.openSeq;
    const p = new URLSearchParams({ limit: 100 });
    if (direction === "older") p.set("before_id", chat.messages[0].id);
    if (direction === "newer") p.set("after_id", chat.messages[chat.messages.length - 1].id);
    chat.historyBusy = true;
    chat.historyLive = [];
    renderHistoryControls();
    try {
      const r = await api(`/api/admin/conversations/${id}/messages?${p}`);
      if (!chat.cur || chat.cur.id !== id || chat.openSeq !== seq || chat.recovering) return;
      const box = $("#chatMsgs"), top = box.scrollTop, height = box.scrollHeight;
      if (direction === "latest") {
        chat.messages = r.items;
        chat.hasOlder = r.has_older; chat.hasNewer = r.has_newer;
      } else {
        const byId = new Map([...chat.messages, ...r.items].map((m) => [m.id, m]));
        chat.messages = [...byId.values()].sort((a, b) => a.id - b.id);
        if (direction === "older") chat.hasOlder = r.has_older;
        else chat.hasNewer = r.has_newer;
      }
      if (!chat.hasNewer) {
        const byId = new Map([...chat.messages, ...chat.historyLive].map((m) => [m.id, m]));
        chat.messages = [...byId.values()].sort((a, b) => a.id - b.id);
      }
      renderHistoryMessages();
      box.scrollTop = direction === "latest" ? box.scrollHeight : direction === "older" ? top + box.scrollHeight - height : top;
    } catch (err) { toast(err.message); }
    finally {
      if (chat.openSeq === seq) {
        chat.historyBusy = false;
        renderHistoryControls();
        markCurRead();
      }
    }
  }

  function renderChatHead() {
    const c = chat.cur;
    const head = $("#chatHead");
    if (!c || !head) return;
    head.innerHTML = `
      <button class="btn sm back-btn" data-act="back">← 返回</button>
      <h3>${esc(convTitle(c))}</h3>
      <span class="tag ${CONV_TAG[c.status]}">${CONV_STATUS[c.status]}</span>
      <span class="muted">${esc(c.site_name)} · ${c.visitor_online ? "访客在线" : "访客离线"}</span>
      <span class="spacer"></span>
      ${c.status === "closed"
        ? '<button class="btn sm" data-act="reopen">重新打开</button>'
        : '<button class="btn sm" data-act="close">结束对话</button>'}
      <button class="btn sm danger" data-act="delete">删除</button>`;
  }

  async function openConv(id, messageId = null) {
    const seq = ++chat.openSeq;
    const pane = $("#chatPane");
    const status = $("#conversationLoad") || document.createElement("div");
    status.id = "conversationLoad";
    pane.prepend(status);
    $(".chat-layout").classList.add("has-conv");
    const back = document.createElement("button");
    back.type = "button"; back.className = "btn sm back-btn"; back.textContent = "← 返回";
    back.onclick = closePane;
    const result = panelData("#conversationLoad", "会话", () => api(`/api/admin/conversations/${id}${messageId ? "?message_id=" + messageId : ""}`),
      () => openConv(id, messageId), () => seq === chat.openSeq);
    status.prepend(back);
    const r = await result;
    if (seq !== chat.openSeq) return;
    if (!r) { status.prepend(back); return; }
    const c = r.conversation, v = r.visitor;
    chat.cur = c;
    chat.syncing = null;
    chat.missed = [];
    chat.recovering = chat.wasLive && !chat.live;
    chat.notes = r.notes || [];
    chat.messages = r.messages;
    chat.focusMessageId = messageId;
    chat.hasOlder = r.has_older; chat.hasNewer = r.has_newer; chat.historyBusy = false; chat.historyLive = [];
    chat.msgIds = new Set();
    const info = [
      v.contact && "联系方式：" + esc(v.contact),
      "IP：" + esc(v.ip),
      c.page_url && "页面：" + (safeUrl(c.page_url)
        ? `<a href="${esc(c.page_url)}" target="_blank" rel="noopener noreferrer">${esc(c.page_url)}</a>`
        : esc(c.page_url)),
      "首次访问：" + esc(v.created_at),
    ].filter(Boolean).join(" · ");
    $("#chatPane").innerHTML = `
      <div class="chat-head" id="chatHead"></div>
      <div class="chat-info"><button type="button" class="btn sm" data-act="related">关联留言 <span id="relatedCount">…</span></button><button type="button" class="btn sm" data-act="ai-evidence">AI / 转人工依据</button><button type="button" class="btn sm" data-act="summary">AI 会话总结</button><button type="button" class="btn sm" data-act="triage">分类 / 优先级</button><span>${info}</span></div>
      <div id="conversationTags" class="conversation-tags"></div>
      <details class="conv-notes">
        <summary>内部备注 · 仅客服可见</summary>
        <div class="notes-body">
          <div id="convNotes"></div>
          <form id="noteForm"><label for="convNoteInput">追加备注</label>
            <textarea id="convNoteInput" maxlength="2000" rows="2" required placeholder="记录需求、跟进情况…"></textarea>
            <button class="btn sm" type="submit">保存备注</button>
          </form>
        </div>
      </details>
      <div id="chatLinkState" class="chat-link-state hidden"><span role="status"></span><button type="button" class="btn sm" data-act="reconnect">立即重连</button></div>
      <div id="chatSync" class="hidden"></div>
      <div id="historyTools" class="history-tools hidden">
        <button type="button" id="loadOlder" class="btn sm">更早消息</button>
        <button type="button" id="loadNewer" class="btn sm">后续消息</button>
        <button type="button" id="jumpLatest" class="btn sm">回到最新</button>
        <span id="historyHint" class="muted"></span>
      </div>
      <div class="chat-msgs" id="chatMsgs"></div>
      <div class="reply-tools"><select id="replyPicker" aria-label="插入快捷回复"></select><a href="#replies">管理话术</a></div>
      <div class="chat-input">
        <button class="btn icon" id="replyPic" title="发送图片">🖼</button>
        <input type="file" id="replyFile" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
        <textarea id="replyInput" placeholder="输入回复，Enter 发送，Shift+Enter 换行；可粘贴图片" maxlength="5000"></textarea>
        <button class="btn primary" id="replyBtn">发送</button>
      </div>`;
    renderChatHead();
    setLive(chat.linkState || "connecting");
    refreshRelatedCount().catch((err) => toast(err.message));
    renderConversationTags();
    renderNotes(chat.notes);
    renderReplyPicker();
    loadQuickReplies().catch((err) => toast(err.message));
    $("#replyPicker").onchange = (e) => {
      const q = quickReplies.find((x) => x.id === +e.target.value);
      e.target.value = "";
      if (!q) return;
      const t = $("#replyInput"), start = t.selectionStart, end = t.selectionEnd;
      if (t.value.length - (end - start) + q.content.length > 5000) return toast("插入后超过 5000 字，请先缩短回复");
      t.setRangeText(q.content, start, end, "end");
      t.focus();
    };
    $("#noteForm").onsubmit = async (e) => {
      e.preventDefault();
      const input = $("#convNoteInput"), content = input.value.trim(), btn = $("button", e.target);
      if (!content || btn.disabled) return;
      btn.disabled = true;
      try {
        const saved = await api(`/api/admin/conversations/${id}/notes`, { method: "POST", body: { content } });
        if (chat.cur && chat.cur.id === id && seq === chat.openSeq) {
          chat.notes.unshift(saved.note);
          renderNotes(chat.notes);
          if (input.value.trim() === content) input.value = "";
        }
        toast("备注已保存");
      } catch (err) { toast(err.message); }
      finally { btn.disabled = false; }
    };
    renderHistoryMessages();
    renderHistoryControls();
    $("#loadOlder").onclick = () => loadHistory("older");
    $("#loadNewer").onclick = () => loadHistory("newer");
    $("#jumpLatest").onclick = () => loadHistory("latest");
    $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
    $(".chat-layout").classList.add("has-conv");
    if (messageId) {
      const target = $(`#chatMsgs [data-message-id="${messageId}"]`);
      if (target) {
        const box = $("#chatMsgs");
        box.scrollTop += target.getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
      }
    }
    renderConvList();
    $("#chatMsgs").addEventListener("scroll", markCurRead, { passive: true });
    markCurRead();
    $("#replyBtn").onclick = sendReply;
    $("#replyPic").onclick = () => $("#replyFile").click();
    $("#replyFile").onchange = (e) => { sendImageReply(e.target.files[0]); e.target.value = ""; };
    $("#replyInput").addEventListener("paste", (e) => {
      const f = e.clipboardData && e.clipboardData.files[0];
      if (f && f.type.startsWith("image/")) { e.preventDefault(); sendImageReply(f); }
    });
    $("#replyInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); sendReply(); }
    });
    if (!messageId && matchMedia("(min-width: 761px)").matches) $("#replyInput").focus();
  }

  function closePane() {
    ++chat.openSeq;
    chat.syncing = null;
    chat.cur = null;
    chat.missed = [];
    $("#chatPane").innerHTML = EMPTY_PANE;
    $(".chat-layout").classList.remove("has-conv");
    renderConvList();
  }

  async function sendReply() {
    const t = $("#replyInput"), btn = $("#replyBtn");
    const content = t.value.trim();
    if (!content || !chat.cur || btn.disabled) return;
    const id = chat.cur.id, seq = chat.openSeq;
    btn.disabled = true;
    try {
      const r = await api(`/api/admin/conversations/${id}/messages`, { method: "POST", body: { content } });
      if (chat.cur && chat.cur.id === id && seq === chat.openSeq) {
        if (t.value.trim() === content) t.value = "";
        if (chat.hasNewer) await loadHistory("latest");
        else receiveChatMessage(r.message);
      }
      if (!r.delivered) toast("访客当前不在线，下次打开网页时会看到回复");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; if (t.isConnected) t.focus(); }
  }

  async function sendImageReply(file) {
    if (!file || !chat.cur) return;
    if (file.size > 5 * 1024 * 1024) return toast("图片不能超过 5MB");
    const convId = chat.cur.id, seq = chat.openSeq;
    try {
      const fd = new FormData();
      fd.append("file", file);
      const up = await api("/api/admin/upload", {method:"POST", body:fd, timeout:30000});
      const r = await api(`/api/admin/conversations/${convId}/messages`, { method: "POST", body: { content: up.url, msg_type: "image" } });
      if (chat.cur && chat.cur.id === convId && seq === chat.openSeq) {
        if (chat.hasNewer) await loadHistory("latest");
        else receiveChatMessage(r.message);
      }
    } catch (err) { toast(err.message); }
  }

  $("#chatPane").addEventListener("click", async (e) => {
    const evidence = e.target.closest("[data-ai-message]");
    if (evidence && chat.cur) return openAIDecisions(chat.cur.id,+evidence.dataset.aiMessage);
    const remove = e.target.closest("[data-remove-tag]");
    if (remove && chat.cur) {
      const id = chat.cur.id;
      remove.disabled = true;
      try {
        const r = await api(`/api/admin/conversations/${id}/tags/${remove.dataset.removeTag}`, { method: "DELETE" });
        upsertConv(r.conversation);
      } catch (err) { toast(err.message); }
      finally { remove.disabled = false; }
      return;
    }
    const b = e.target.closest("[data-act]");
    if (!b || !chat.cur) return;
    const id = chat.cur.id, act = b.dataset.act;
    try {
      if (act === "related") return await showRelated(id);
      if (act === "triage") return await openTriage(id);
      if (act === "summary") return await openSummary(id);
      if (act === "ai-evidence") return await openAIDecisions(id);
      if (act === "back") return closePane();
      if (act === "reconnect") return liveConnect();
      if (act === "close" || act === "reopen") {
        await api(`/api/admin/conversations/${id}/status`, { method: "POST", body: { status: act === "close" ? "closed" : "waiting" } });
      }
      if (act === "delete") {
        if (!confirm(`删除会话 #${id} 及全部聊天记录？此操作不可恢复。`)) return;
        await api(`/api/admin/conversations/${id}`, { method: "DELETE" });
        closePane();
      }
      loadStats();
    } catch (err) { toast(err.message); }
  });
  $("#chatPane").addEventListener("change", async (e) => {
    if (e.target.id !== "addConvTag" || !e.target.value || !chat.cur) return;
    const id = chat.cur.id, tagId = +e.target.value, select = e.target;
    select.disabled = true;
    try {
      const r = await api(`/api/admin/conversations/${id}/tags/${tagId}`, { method: "PUT" });
      upsertConv(r.conversation);
    } catch (err) { toast(err.message); }
    finally { select.disabled = false; select.value = ""; }
  });
  $("#convList").addEventListener("click", (e) => {
    if (e.target.closest(".conv-check")) return;
    const d = e.target.closest(".conv");
    if (d) {
      const target = e.target.closest("[data-message-id]");
      openConv(+d.dataset.id, target ? +target.dataset.messageId : null).catch((err) => toast(err.message));
    }
  });
  ["#cStatus", "#cSite", "#cFrom", "#cTo", "#cCategory", "#cPriority"].forEach((s) => { $(s).onchange = changeConvFilters; });
  $("#clearConvFilters").onclick = () => {
    ["#cQ", "#cTag", "#cSite", "#cFrom", "#cTo", "#cStatus", "#cCategory", "#cPriority"].forEach((s) => { $(s).value = ""; });
    changeConvFilters();
  };
  function changeConvPage(delta) {
    chat.page += delta;
    chat.selected.clear();
    chat.list = [];
    renderConvList();
    loadConvs().catch((e) => toast(e.message));
  }
  $("#convPrev").onclick = () => changeConvPage(-1);
  $("#convNext").onclick = () => changeConvPage(1);
  let qTimer;
  $("#cQ").addEventListener("input", () => {
    clearTimeout(qTimer);
    ++chat.listSeq;
    qTimer = setTimeout(changeConvFilters, 300);
  });

  async function markCurRead() {
    if (!chat.cur || chat.hasNewer || chat.recovering || chat.historyBusy ||
        document.hidden || document.body.dataset.view !== "chats") return;
    const box = $("#chatMsgs"), last = box?.lastElementChild;
    const id = chat.cur.id, upto = Number(last?.dataset.messageId);
    if (!upto || upto <= chat.cur.read_upto || pendingRead?.id === id || !box.clientHeight) return;
    const message = last.getBoundingClientRect(), view = box.getBoundingClientRect();
    if (message.top >= view.bottom || message.bottom <= view.top) return;

    const pending = { id };
    pendingRead = pending;
    try {
      await api(`/api/admin/conversations/${id}/read`, { method: "POST", body: { upto_id: upto } });
      if (chat.cur?.id === id) chat.cur.read_upto = Math.max(chat.cur.read_upto || 0, upto);
    } catch (_) {
      return;
    } finally {
      if (pendingRead === pending) pendingRead = null;
    }
    markCurRead();
  }
  document.addEventListener("visibilitychange", markCurRead);

  function notify(c, m) {
    toast(`${convTitle(c)}：${m.content.slice(0, 30)}`);
    try {
      if (document.hidden && "Notification" in window && Notification.permission === "granted") {
        new Notification(`MsgHub · ${c.site_name} ${convTitle(c)}`, { body: m.content.slice(0, 100), tag: "conv-" + c.id });
      }
    } catch (_) {}
  }
  // 浏览器通知权限：首次在后台点击时申请
  document.addEventListener("click", function ask() {
    document.removeEventListener("click", ask);
    try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); } catch (_) {}
  });

  let statsTimer;
  const refreshStats = () => { clearTimeout(statsTimer); statsTimer = setTimeout(() => loadStats().catch(() => {}), 500); };

  async function syncOpenChat() {
    if (!chat.cur || !$("#chatSync") || !chat.live || chat.syncing) return;
    const id = chat.cur.id, seq = chat.openSeq, box = $("#chatSync");
    const sync = {};
    chat.syncing = sync;
    chat.recovering = true;
    box.classList.remove("hidden");
    panelStatus(box, "正在同步断线期间的消息…");
    try {
      const anchor = chat.messages.at(-1)?.id;
      const params = chat.hasNewer && anchor ? "after_id=" + anchor + "&limit=1" : "limit=500";
      const result = await api(`/api/admin/conversations/${id}/messages?${params}`);
      if (seq !== chat.openSeq || !chat.live || chat.syncing !== sync) return;
      if (chat.hasNewer) chat.hasNewer = result.has_newer || result.items.length > 0;
      else {
        const messages = result.items;
        const first = messages[0]?.id;
        const overlap = messages.some((m) => chat.msgIds.has(m.id));
        const older = overlap ? chat.messages.filter((m) => m.id < first) : [];
        const byId = new Map([...older, ...messages, ...chat.missed].map((m) => [m.id, m]));
        chat.messages = [...byId.values()].sort((a, b) => a.id - b.id);
        if (!older.length) chat.hasOlder = result.has_older;
        const list = $("#chatMsgs");
        const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
        const top = list.scrollTop;
        renderHistoryMessages();
        list.scrollTop = nearBottom ? list.scrollHeight : top;
      }
      chat.missed = [];
      chat.recovering = false;
      box.classList.add("hidden");
      renderHistoryControls();
      markCurRead();
    } catch (err) {
      if (seq === chat.openSeq && chat.live && chat.syncing === sync) panelStatus(box, "消息同步失败 · " + err.message, syncOpenChat);
    } finally {
      if (chat.syncing === sync) chat.syncing = null;
    }
  }

  function setLive(status) {
    const live = status === "ready";
    chat.live = live;
    chat.linkState = status;
    const text = {
      ready: "● 实时连接正常",
      connecting: "正在连接实时消息…",
      reconnecting: "实时连接已断开，正在重连…",
      offline: "网络已断开，联网后将自动重连",
    }[status];
    $("#liveState").textContent = text;
    $("#liveState").classList.toggle("ok", live);
    const paneState = $("#chatLinkState");
    if (paneState) {
      paneState.classList.toggle("hidden", live);
      $("span", paneState).textContent = text;
      $("button", paneState).classList.toggle("hidden", status === "connecting");
    }
    $("#retryLive").classList.toggle("hidden", live || status === "connecting");
  }

  function liveConnect() {
    if (!token || chat.ws?.readyState === WebSocket.CONNECTING || chat.ws?.readyState === WebSocket.OPEN) return;
    clearTimeout(chat.timer);
    if (!navigator.onLine) { setLive("offline"); return; }
    setLive(chat.wasLive ? "reconnecting" : "connecting");
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/admin/ws");
    chat.ws = ws;
    let deadline = setTimeout(() => ws.close(), 12000);
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      if (chat.ws !== ws) return;
      let d;
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d.type === "pong") {
        clearTimeout(deadline);
      } else if (d.type === "ready") {
        clearTimeout(deadline);
        clearInterval(chat.ping);
        chat.ping = setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          ws.send('{"type":"ping"}');
          deadline = setTimeout(() => ws.close(), 12000);
        }, 25000);
        const reconnected = chat.wasLive || chat.retry > 0;
        chat.wasLive = true;
        chat.retry = 0;
        setLive("ready");
        if (reconnected) {
          syncOpenChat();
          refreshTagViews().catch((e) => toast(e.message));
          refreshRelatedCount().catch(() => {});
          if (document.body.dataset.view === "messages") loadMessages(true);
          refreshStats();
        }
      } else if (d.type === "inbox_changed") {
        if (document.body.dataset.view === "messages") loadMessages(true);
        if (chat.cur && (d.conversation_ids || []).includes(chat.cur.id)) refreshRelatedCount().catch((e) => toast(e.message));
        if (!$("#relatedDrawer").classList.contains("hidden") && (d.conversation_ids || []).includes(related.convId)) {
          showRelated(related.convId, false).catch((e) => toast(e.message));
        }
        refreshStats();
      } else if (d.type === "triage_changed") {
        if (!$("#qqDeliveryDrawer").classList.contains("hidden")) {
          if (qqLog.detailId) loadQQDeliveryDetail(qqLog.detailId).catch(e => toast(e.message));
          else loadQQDeliveries().catch(e => toast(e.message));
        }
        if (!$("#triageDrawer").classList.contains("hidden") && triageView.convId === d.conversation_id && !triageView.busy.has(d.conversation_id)) loadTriage();
      } else if (d.type === "conversation_summary_changed") {
        if (!$("#summaryDrawer").classList.contains("hidden") && summaryView.convId === d.conversation_id && !summaryView.busy.has(d.conversation_id) && !summaryView.selected) loadSummary();
      } else if (d.type === "ai_decision_changed") {
        if (!$("#aiDecisionDrawer").classList.contains("hidden") && aiLog.convId === d.conversation_id && !aiLog.detailId) loadAIDecisions().catch((e) => toast(e.message));
      } else if (d.type === "qq_delivery_changed") {
        if (!$("#qqDeliveryDrawer").classList.contains("hidden")) {
          if (qqLog.detailId) loadQQDeliveryDetail(qqLog.detailId).catch((e) => toast(e.message));
          else loadQQDeliveries().catch((e) => toast(e.message));
        }
      } else if (d.type === "tags_changed") {
        refreshTagViews().catch((e) => toast(e.message));
      } else if (d.type === "msg") {
        if (d.message.sender === "visitor" && !$("#triageDrawer").classList.contains("hidden") && triageView.convId === d.conversation.id && !triageView.busy.has(d.conversation.id)) loadTriage();
        if (!$("#summaryDrawer").classList.contains("hidden") && summaryView.convId === d.conversation.id && !summaryView.busy.has(d.conversation.id)) loadSummary();
        const isCur = chat.cur && chat.cur.id === d.conversation.id;
        if (isCur) receiveChatMessage(d.message);
        upsertConv(d.conversation);
        if (isCur && d.message.sender === "visitor") markCurRead();
        if (d.message.sender === "visitor" && (!isCur || document.hidden)) notify(d.conversation, d.message);
        refreshStats();
      } else if (d.type === "conv") {
        upsertConv(d.conversation);
        refreshStats();
      } else if (d.type === "conv_deleted") {
        if (triageView.convId === d.id) { ++triageView.seq; $("#triageDrawer").classList.add("hidden"); }
        if (summaryView.convId === d.id) { ++summaryView.seq; $("#summaryDrawer").classList.add("hidden"); }
        if (aiLog.convId === d.id) { ++aiLog.seq; $("#aiDecisionDrawer").classList.add("hidden"); }
        if (related.convId === d.id) { ++related.seq; $("#relatedDrawer").classList.add("hidden"); $("#backToRelated")?.remove(); }
        const source = $("#viewSourceConv");
        if (source?.dataset.convId === String(d.id)) { source.disabled = true; source.textContent = "来源会话已删除，留言仍保留"; }
        if (document.body.dataset.view === "messages") loadMessages(true);
        chat.list = chat.list.filter((c) => c.id !== d.id);
        if (chat.cur && chat.cur.id === d.id) closePane(); else renderConvList();
        scheduleConvRefresh();
        refreshStats();
      }
    };
    ws.onclose = (ev) => {
      clearTimeout(deadline);
      if (chat.ws !== ws) return;
      clearInterval(chat.ping);
      chat.ws = null;
      if (ev.code === 4401) { logout(); $("#loginErr").textContent = "登录已过期，请重新登录"; return; }
      if (!token) return;
      chat.recovering = Boolean(chat.cur);
      chat.syncing = null;
      if (!navigator.onLine) { setLive("offline"); return; }
      setLive("reconnecting");
      chat.timer = setTimeout(liveConnect, Math.min(30000, 1000 * 2 ** chat.retry++));
    };
  }
  $("#retryLive").onclick = liveConnect;
  window.addEventListener("online", liveConnect);
  if (window.visualViewport) {
    const sizeForKeyboard = () => {
      const viewport = window.visualViewport;
      const inset = document.documentElement.clientHeight - viewport.height;
      document.body.classList.toggle("keyboard-open", viewport.scale < 1.1 && inset > 100);
      document.body.style.setProperty("--visible-height", viewport.height + "px");
    };
    window.visualViewport.addEventListener("resize", sizeForKeyboard);
    sizeForKeyboard();
  }
  window.addEventListener("offline", () => {
    if (!token) return;
    setLive("offline");
    chat.ws?.close();
  });

  // ---------- 启动 ----------
  function boot() {
    if (!token) return showLogin();
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    route();
    if (document.body.dataset.view !== "sites") loadSites();
    loadStats();
    loadTags().catch((err) => toast(err.message));
    loadConvs().catch(() => {});
    liveConnect();
  }
  boot();
})();
