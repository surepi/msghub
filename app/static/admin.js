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
  const state = { page: 1, size: 20, sites: [], items: [], editingSite: null };

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 2200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, ...(opts.headers || {}) },
      body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
    });
    if (res.status === 401 && !path.endsWith("/login")) { logout(); throw new Error("请重新登录"); }
    const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    if (!res.ok) throw new Error((data && data.detail) || "请求失败");
    return data;
  }

  // ---------- 登录 ----------
  function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
  function logout() {
    token = "";
    clearTimeout(chat.timer);
    if (chat.ws) chat.ws.close();
    try { localStorage.removeItem("msghub_token"); } catch (_) {}
    showLogin();
  }
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#loginErr").textContent = "";
    try {
      const r = await api("/api/admin/login", { method: "POST", body: { password: e.target.password.value } });
      token = r.token;
      try { localStorage.setItem("msghub_token", token); } catch (_) {}
      e.target.reset();
      boot();
    } catch (err) { $("#loginErr").textContent = err.message; }
  });
  $("#logout").onclick = logout;

  // ---------- 路由 ----------
  function route() {
    const v = ["#sites", "#messages", "#ai"].includes(location.hash) ? location.hash.slice(1) : "chats";
    $$(".top nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
    ["chats", "messages", "sites", "ai"].forEach((n) => $("#view-" + n).classList.toggle("hidden", v !== n));
    document.body.dataset.view = v;
    if (v === "sites") { renderSites(); loadQQ(); }
    if (v === "ai") loadAI();
  }
  window.addEventListener("hashchange", route);

  // ---------- 统计 ----------
  async function loadStats() {
    const s = await api("/api/admin/stats");
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
    state.sites = await api("/api/admin/sites");
    const sel = $("#fSite"), cur = sel.value;
    sel.innerHTML = '<option value="">全部站点</option>' +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    sel.value = cur;
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

  async function loadMessages() {
    const p = filters();
    p.set("page", state.page); p.set("size", state.size);
    const r = await api("/api/admin/messages?" + p);
    state.items = r.items;
    $("#chkAll").checked = false;
    $("#msgBody").innerHTML = r.items.length ? r.items.map((m) => `
      <tr data-id="${m.id}" class="${m.status === "new" ? "unread" : ""}">
        <td class="w-chk"><input type="checkbox" class="chk" value="${m.id}"></td>
        <td class="nowrap">${esc(m.site_name)}</td>
        <td>${esc(m.name) || '<span class="muted">匿名</span>'}<br><span class="muted">${esc(m.contact)}</span></td>
        <td class="clip">${esc(m.content)}</td>
        <td><span class="tag ${m.status}">${STATUS[m.status]}</span></td>
        <td class="nowrap muted">${esc(m.created_at)}</td>
      </tr>`).join("") : '<tr><td colspan="6" class="empty">暂无留言</td></tr>';
    const pages = Math.max(1, Math.ceil(r.total / r.size));
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
    const m = state.items.find((x) => x.id === +tr.dataset.id);
    openMessage(m);
    if (m.status === "new") {
      await api(`/api/admin/messages/${m.id}`, { method: "PATCH", body: { status: "read" } });
      m.status = "read";
      loadMessages(); loadStats();
    }
  });

  function openMessage(m) {
    $("#drawerBody").innerHTML = `
      <dl class="kv">
        <dt>站点</dt><dd>${esc(m.site_name)}</dd>
        <dt>姓名</dt><dd>${esc(m.name) || "—"}</dd>
        <dt>联系方式</dt><dd>${esc(m.contact) || "—"}</dd>
        <dt>来源页面</dt><dd>${safeUrl(m.page_url) ? `<a href="${esc(m.page_url)}" target="_blank" rel="noopener noreferrer">${esc(m.page_url)}</a>` : esc(m.page_url) || "—"}</dd>
        <dt>IP</dt><dd>${esc(m.ip)}</dd>
        <dt>时间</dt><dd>${esc(m.created_at)}</dd>
      </dl>
      <div class="content-box">${esc(m.content)}</div>
      <label>内部备注<textarea id="noteInput" placeholder="处理记录、跟进情况…">${esc(m.note)}</textarea></label>
      <div class="actions">
        <button class="btn primary" data-s="handled">保存并标为已处理</button>
        <button class="btn" data-s="">仅保存备注</button>
        <button class="btn" data-s="spam">标为垃圾</button>
        <button class="btn danger" data-del>删除</button>
      </div>`;
    $("#drawer").classList.remove("hidden");
    $("#drawerBody .actions").onclick = async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      try {
        if (b.hasAttribute("data-del")) {
          if (!confirm("删除这条留言？")) return;
          await api(`/api/admin/messages/${m.id}`, { method: "DELETE" });
        } else {
          const body = { note: $("#noteInput").value };
          if (b.dataset.s) body.status = b.dataset.s;
          await api(`/api/admin/messages/${m.id}`, { method: "PATCH", body });
        }
        $("#drawer").classList.add("hidden");
        await Promise.all([loadMessages(), loadStats(), loadSites()]);
        toast("已保存");
      } catch (err) { toast(err.message); }
    };
  }

  // 关闭弹层
  $$(".drawer").forEach((d) => d.addEventListener("click", (e) => {
    if (e.target === d || e.target.closest("[data-close]")) d.classList.add("hidden");
  }));
  document.addEventListener("keydown", (e) => e.key === "Escape" && $$(".drawer").forEach((d) => d.classList.add("hidden")));

  // ---------- AI ----------
  function siteOptions(sel, all, value) {
    sel.innerHTML = (all ? '<option value="">所有站点</option>' : "") +
      state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    if (value !== undefined && value !== null) sel.value = String(value);
    if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  }

  async function loadAI() {
    let s;
    try { s = await api("/api/admin/ai/settings"); } catch (err) { return toast(err.message); }
    const f = $("#aiForm");
    f.enabled.checked = s.enabled;
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
    const body = { enabled: f.enabled.checked, base_url: f.base_url.value, model: f.model.value,
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
        <details><summary>参考到的资料（${r.context ? r.context.length + " 字" : "无"}）</summary>
        <pre>${esc(r.context || "没有匹配到资料")}</pre></details>`;
    } catch (err) { out.textContent = "测试失败：" + err.message; }
  };

  let qaItems = [], editingQA = null;
  async function loadQA() {
    const sid = $("#qaSite").value;
    qaItems = await api("/api/admin/qa" + (sid ? `?site_id=${sid}` : ""));
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
    const r = await api(`/api/admin/kb/${sid}`);
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
    let s;
    try { s = await api("/api/admin/qq/status"); } catch (err) { box.textContent = err.message; return; }
    if (!s.enabled) {
      box.innerHTML = `<h3>QQ 机器人</h3><p class="muted">未配置。在 <code>.env</code> 中填写 <code>QQ_APP_ID</code> 和
        <code>QQ_APP_SECRET</code> 后重启服务，客服即可在 QQ 上收发访客消息。</p>`;
      return;
    }
    const cb = location.origin + s.callback_path;
    const ok = (b, yes, no) => b ? `<span class="tag handled">${yes}</span>` : `<span class="tag spam">${no}</span>`;
    box.innerHTML = `
      <div class="site-head"><h3>QQ 机器人</h3>${ok(s.token_ok, "凭据有效", "凭据无效")}
        ${ok(s.bound, "客服已绑定", "客服未绑定")}<span class="spacer"></span>
        <button class="btn sm" data-qq="refresh">刷新</button></div>
      <dl class="kv">
        <dt>AppID</dt><dd>${esc(s.app_id)}</dd>
        ${s.token_ok ? "" : `<dt>错误</dt><dd>${esc(s.token_error)}</dd>`}
        <dt>回调地址</dt><dd><code>${esc(cb)}</code> <button class="btn sm ghost" data-qq="copy">复制</button>
          <br><small class="muted">填到 q.qq.com 机器人管理后台的回调地址配置中，并订阅单聊消息事件（C2C_MESSAGE_CREATE）</small></dd>
        <dt>最近回调</dt><dd>${esc(s.last_event_at) || "还没收到过平台回调"}</dd>
        <dt>客服</dt><dd>${s.bound
          ? `${esc(s.openid)} · 绑定于 ${esc(s.bound_at)} · ${s.online ? "在线" : "忙碌"}
             ${s.current_conv_id ? ` · 当前会话 #${s.current_conv_id}` : ""}`
          : "未绑定"}</dd>
        ${s.bound ? `<dt>转发状态</dt><dd>积压 ${s.pending} 条 · 被动回复窗口${
          s.passive_seconds_left ? `剩余 ${Math.ceil(s.passive_seconds_left / 60)} 分钟` : "已关闭"}</dd>` : ""}
        ${s.bind_code ? `<dt>绑定码</dt><dd><b class="bind-code">${esc(s.bind_code)}</b>
          <br><small class="muted">用客服的 QQ 给机器人发送「/绑定 ${esc(s.bind_code)}」，${Math.ceil(s.bind_expires_in / 60)} 分钟内有效</small></dd>` : ""}
      </dl>
      <div class="actions">
        <button class="btn sm ${s.bound ? "" : "primary"}" data-qq="bind">${s.bound ? "更换客服（重新生成绑定码）" : "生成绑定码"}</button>
        ${s.bound ? '<button class="btn sm" data-qq="test">发送测试消息</button><button class="btn sm danger" data-qq="unbind">解除绑定</button>' : ""}
      </div>`;
    box.dataset.cb = cb;
  }

  $("#qqCard").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-qq]");
    if (!b) return;
    try {
      switch (b.dataset.qq) {
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
  });

  // ---------- 对话 ----------
  const chat = { list: [], cur: null, msgIds: new Set(), ws: null, retry: 0, timer: null, ping: null };
  const CONV_STATUS = { waiting: "待回复", active: "进行中", closed: "已结束" };
  const CONV_TAG = { waiting: "new", active: "handled", closed: "read" };
  const shortTime = (s) => String(s || "").slice(5, 16);
  const convTitle = (c) => `#${c.id} ${c.visitor_name || "访客"}`;
  const EMPTY_PANE = '<div class="chat-empty muted">选择左侧的会话开始回复</div>';

  function renderConvList() {
    const box = $("#convList");
    if (!chat.list.length) { box.innerHTML = '<div class="empty">暂无对话</div>'; return; }
    box.innerHTML = chat.list.map((c) => `
      <div class="conv ${chat.cur && chat.cur.id === c.id ? "on" : ""}" data-id="${c.id}">
        <div class="conv-top">
          <span class="dot ${c.visitor_online ? "on" : ""}" title="${c.visitor_online ? "访客在线" : "访客离线"}"></span>
          <b>${esc(convTitle(c))}</b>${c.unread ? `<span class="unread">${c.unread}</span>` : ""}
        </div>
        <div class="conv-prev">${esc(c.last_preview) || "&nbsp;"}</div>
        <div class="conv-meta"><span>${esc(c.site_name)} · ${CONV_STATUS[c.status]}</span><span>${esc(shortTime(c.last_msg_at))}</span></div>
      </div>`).join("");
  }

  function convVisible(c, known) {
    const st = $("#cStatus").value;
    if (st === "open" && c.status === "closed") return false;
    if (st && st !== "open" && c.status !== st) return false;
    // 有搜索词时，实时推来的新会话不插入（搜索由服务端完成）
    return known || !$("#cQ").value.trim();
  }

  function upsertConv(c) {
    const i = chat.list.findIndex((x) => x.id === c.id);
    if (i >= 0) chat.list.splice(i, 1);
    if (convVisible(c, i >= 0)) {
      chat.list.push(c);
      chat.list.sort((a, b) => b.last_msg_at.localeCompare(a.last_msg_at) || b.id - a.id);
    }
    if (chat.cur && chat.cur.id === c.id) { chat.cur = c; renderChatHead(); }
    renderConvList();
  }

  async function loadConvs() {
    const p = new URLSearchParams({ size: 100 });
    if ($("#cStatus").value) p.set("status", $("#cStatus").value);
    if ($("#cQ").value.trim()) p.set("q", $("#cQ").value.trim());
    chat.list = (await api("/api/admin/conversations?" + p)).items;
    renderConvList();
  }

  function appendMsg(m) {
    const box = $("#chatMsgs");
    if (!box || chat.msgIds.has(m.id)) return;
    chat.msgIds.add(m.id);
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const isImg = m.msg_type === "image" && /^\/uploads\/[\w.]+$/.test(m.content);
    const body = isImg
      ? `<a href="${esc(m.content)}" target="_blank" rel="noopener"><img src="${esc(m.content)}" alt="图片" loading="lazy"></a>`
      : esc(m.content);
    box.insertAdjacentHTML("beforeend", `<div class="msg ${esc(m.sender)}">${m.sender === "ai" ? '<div class="who">AI 助手</div>' : ""}
      <div class="bub${isImg ? " img" : ""}">${body}${
      m.sender !== "system" ? `<time>${esc(shortTime(m.created_at))}</time>` : ""}</div></div>`);
    if (isImg) $("img", box.lastElementChild).onload = () => { if (nearBottom) box.scrollTop = box.scrollHeight; };
    if (nearBottom || m.sender === "agent") box.scrollTop = box.scrollHeight;
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

  async function openConv(id) {
    const r = await api(`/api/admin/conversations/${id}`);
    const c = r.conversation, v = r.visitor;
    chat.cur = c;
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
      <div class="chat-info">${info}</div>
      <div class="chat-msgs" id="chatMsgs"></div>
      <div class="chat-input">
        <button class="btn icon" id="replyPic" title="发送图片">🖼</button>
        <input type="file" id="replyFile" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
        <textarea id="replyInput" placeholder="输入回复，Enter 发送，Shift+Enter 换行；可粘贴图片" maxlength="5000"></textarea>
        <button class="btn primary" id="replyBtn">发送</button>
      </div>`;
    renderChatHead();
    r.messages.forEach(appendMsg);
    $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
    $(".chat-layout").classList.add("has-conv");
    renderConvList();
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
    $("#replyInput").focus();
  }

  function closePane() {
    chat.cur = null;
    $("#chatPane").innerHTML = EMPTY_PANE;
    $(".chat-layout").classList.remove("has-conv");
    renderConvList();
  }

  async function sendReply() {
    const t = $("#replyInput"), btn = $("#replyBtn");
    const content = t.value.trim();
    if (!content || !chat.cur) return;
    btn.disabled = true;
    try {
      const r = await api(`/api/admin/conversations/${chat.cur.id}/messages`, { method: "POST", body: { content } });
      t.value = "";
      appendMsg(r.message);
      if (!r.delivered) toast("访客当前不在线，下次打开网页时会看到回复");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; t.focus(); }
  }

  async function sendImageReply(file) {
    if (!file || !chat.cur) return;
    if (file.size > 5 * 1024 * 1024) return toast("图片不能超过 5MB");
    const convId = chat.cur.id;
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/admin/upload", { method: "POST", headers: { Authorization: "Bearer " + token }, body: fd });
      const up = await res.json();
      if (!res.ok) throw new Error(up.detail || "上传失败");
      const r = await api(`/api/admin/conversations/${convId}/messages`, { method: "POST", body: { content: up.url, msg_type: "image" } });
      appendMsg(r.message);
    } catch (err) { toast(err.message); }
  }

  $("#chatPane").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || !chat.cur) return;
    const id = chat.cur.id, act = b.dataset.act;
    try {
      if (act === "back") return closePane();
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
  $("#convList").addEventListener("click", (e) => {
    const d = e.target.closest(".conv");
    if (d) openConv(+d.dataset.id).catch((err) => toast(err.message));
  });
  $("#cStatus").onchange = () => loadConvs().catch((e) => toast(e.message));
  let qTimer;
  $("#cQ").addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => loadConvs().catch((e) => toast(e.message)), 300);
  });

  function markCurRead() {
    if (chat.cur && chat.cur.unread && !document.hidden) {
      api(`/api/admin/conversations/${chat.cur.id}/read`, { method: "POST" }).catch(() => {});
    }
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

  function setLive(ok) {
    const s = $("#liveState");
    s.textContent = ok ? "● 实时连接正常" : "实时连接已断开，正在重连…";
    s.classList.toggle("ok", ok);
  }

  function liveConnect() {
    if (!token) return;
    clearTimeout(chat.timer);
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/admin/ws");
    chat.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d.type === "ready") {
        chat.retry = 0;
        setLive(true);
        clearInterval(chat.ping);
        chat.ping = setInterval(() => ws.readyState === 1 && ws.send('{"type":"ping"}'), 25000);
      } else if (d.type === "msg") {
        const isCur = chat.cur && chat.cur.id === d.conversation.id;
        if (isCur) appendMsg(d.message);
        upsertConv(d.conversation);
        if (isCur && d.message.sender === "visitor") markCurRead();
        if (d.message.sender === "visitor" && (!isCur || document.hidden)) notify(d.conversation, d.message);
        refreshStats();
      } else if (d.type === "conv") {
        upsertConv(d.conversation);
        refreshStats();
      } else if (d.type === "conv_deleted") {
        chat.list = chat.list.filter((c) => c.id !== d.id);
        if (chat.cur && chat.cur.id === d.id) closePane(); else renderConvList();
        refreshStats();
      }
    };
    ws.onclose = (ev) => {
      clearInterval(chat.ping);
      if (chat.ws !== ws) return;  // 已被新连接取代
      setLive(false);
      if (ev.code === 4401) return logout();
      if (!token) return;
      chat.timer = setTimeout(() => {
        liveConnect();
        loadConvs().catch(() => {});  // 断线期间可能漏了事件，重新拉一次列表
      }, Math.min(30000, 1000 * 2 ** chat.retry++));
    };
  }

  // ---------- 启动 ----------
  async function boot() {
    if (!token) return showLogin();
    try {
      await Promise.all([loadStats(), loadSites()]);
      $("#login").classList.add("hidden");
      $("#app").classList.remove("hidden");
      route();
      await Promise.all([loadMessages(), loadConvs()]);
      liveConnect();
    } catch (err) { if (token) toast(err.message); }
  }
  boot();
})();
