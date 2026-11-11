# MsgHub 留言汇

网站留言统一管理平台：多个网站的访客可以在网页上和客服实时对话，也可以提交留言；所有对话和留言汇总到一个后台集中处理。

二期（对接 QQ 官方机器人，客服在 QQ 上直接回复访客）开发中，已完成第 ① 阶段网页实时对话、第 ② 阶段 QQ 打通、第 ③ 阶段图片与 AI。

## 功能

- **网页实时对话**：网站嵌入一行代码即出现咨询按钮，访客无需登录即可和客服实时聊天；刷新、换页后记录保留，客服回复时按钮显示未读数
- **对话后台**：会话列表实时更新（未读数、访客是否在线），直接回复、结束 / 重新打开对话，浏览器标题与通知提醒新消息
- **多站点接入**：每个网站一个独立 Site Key，可限制允许的来源域名（Origin）、单独启用/停用、重置 Key
- **四种接入方式**：嵌入式表单挂件、右下角悬浮按钮、自有 HTML 表单直接提交、后端 API 调用（后台「站点」页可一键复制接入代码）
- **留言管理**：按站点 / 状态 / 关键词筛选，状态流转（未读 → 已读 → 已处理 / 垃圾），内部备注，批量操作，导出 CSV（Excel 可直接打开）
- **防刷**：每 IP 频率限制、蜜罐字段过滤机器人、长度限制
- **Webhook 推送**：新留言实时 POST 到你指定的地址（可对接企业微信 / 钉钉 / 飞书机器人中转、自有系统等）
- **零外部依赖**：SQLite 单文件存储，前端无 CDN 依赖

## 部署（Docker）

```bash
cp .env.example .env      # 设置 ADMIN_PASSWORD
docker compose up -d --build
```

打开 http://127.0.0.1:20100 ，用 `ADMIN_PASSWORD` 登录。

> 网站访客的浏览器需要能访问 MsgHub，公网使用请放在 Nginx / Caddy 等 HTTPS 反向代理之后，
> 并把 `.env` 中 `BIND_ADDR` 保持为 `127.0.0.1`。服务信任 `X-Forwarded-For` 来获取访客真实 IP，
> 若直接暴露端口到公网，访客可伪造该请求头绕过 IP 频率限制。

### 反向代理需要支持 WebSocket

在线对话走 WebSocket（`/api/chat/ws`、`/api/admin/ws`）。Caddy 默认支持；Nginx 需要：

```nginx
location / {
    proxy_pass http://127.0.0.1:20100;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
}
```

服务必须**单进程**运行（不要加 `--workers`）：在线连接保存在进程内存里。

数据全部在 `./data`（`msghub.db` + `.secret`），备份这个目录即可。

## 对接 QQ 官方机器人

客服只需一个 QQ：访客消息转发到 QQ，客服在 QQ 里回复，访客网页上实时看到。

1. `.env` 填写 `QQ_APP_ID`、`QQ_APP_SECRET`（q.qq.com 机器人的开发设置），`docker compose up -d` 重启
2. 后台「站点」页顶部的 QQ 卡片显示“凭据有效”后，复制其中的**回调地址**（`https://你的域名/api/qq/callback`），
   填到 q.qq.com 的回调配置，订阅单聊消息事件。回调地址必须是 HTTPS，端口限 80 / 443 / 8080 / 8443
3. 点「生成绑定码」，用客服的 QQ 给机器人发送 `/绑定 123456`，收到“绑定成功”即完成

客服在 QQ 里的用法（给机器人发 `/帮助` 可随时查看）：

| 操作 | 效果 |
|---|---|
| 引用某条转发消息后回复 | 发给那位访客 |
| `#12 内容` | 发给 12 号会话 |
| 直接发文字 | 发给当前会话（最近转发过来的那个），机器人回执“已发给 #12” |
| `/列表` `/历史 #12` `/切换 #12` `/结束 #12` `/忙` `/在` | 管理会话 |

转发策略：先发主动消息；失败时若客服 5 分钟内发过消息则改用被动回复；都失败则积压，客服下次给机器人发任何消息（如 `/在`）时补发。
同一会话 2 秒内的多条消息会合并成一条。为防止回错人，新会话消息刚进来的 10 秒内，“直接发文字”不会发出，需引用或用 `#编号`。

## 站点设置与 AI 自动应答

每个站点（后台「站点」→ 编辑）可设置：欢迎语、对话前必须留称呼和联系方式（默认开启）、工作时间、非工作时间提示语。

**无人值守时的处理**：非工作时间或客服在 QQ 发了 `/忙` → 立即处理；工作时间内访客消息超过设定分钟数（默认 3）没人回复 → 处理。
- 开启了 AI：AI 根据问答库和抓取的网站内容回答，回复同步发给客服 QQ；资料里没有答案或访客要人工 → 记为留言并提醒客服
- 未开启 AI：记为留言（含访客联系方式），并给访客提示

**AI 配置**（后台「AI」页）：兼容 OpenAI 的接口地址（填到 `/v1`）、API Key、模型名。
资料来源：问答库（优先）+ 网站内容（每天自动抓取站点域名或指定起始地址，同域名最多 50 页，遵守 robots.txt）。
页面上可以直接输入问题测试 AI 的回答和它参考了哪些资料。

**图片**：访客可点按钮或粘贴发送图片（PNG/JPG/GIF/WebP，≤5MB），客服可在网页后台或 QQ 里发图片。
QQ 只支持 PNG/JPG，其他格式会提示客服到网页后台查看。图片保存在 `./data/uploads`。

## 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ADMIN_PASSWORD=dev uvicorn app.main:app --reload --port 20100
```

接口文档：http://127.0.0.1:20100/api/docs

## 接入示例

```html
<!-- 在线对话：右下角咨询按钮 -->
<script src="https://msg.example.com/embed.js" data-site-key="sk_xxx"></script>
```

| 属性 | 说明 |
|---|---|
| `data-site-key` | 站点 Key，必填 |
| `data-target` | 嵌入到页面中的某个容器（如 `#msghub`），不填则为右下角悬浮按钮 |
| `data-mode` | `chat`（默认，在线对话）或 `form`（旧版留言表单） |
| `data-title` / `data-color` / `data-welcome` | 标题 / 主题色 / 欢迎语 |

```bash
# 后端直接调用
curl -X POST https://msg.example.com/api/public/messages \
  -H "Content-Type: application/json" -H "X-Site-Key: sk_xxx" \
  -d '{"name":"张三","contact":"13800000000","content":"想咨询一下价格"}'
```

### 提交字段

| 字段 | 说明 |
|---|---|
| `site_key` | 站点 Key（也可用请求头 `X-Site-Key`） |
| `content` | 留言内容，必填，≤ 5000 字 |
| `name` | 称呼 |
| `contact` | 联系方式 |
| `page_url` | 来源页面，缺省取 Referer |
| `_redirect` | 表单提交成功后跳转的地址（仅 HTML 表单需要） |
| `_hp` | 蜜罐字段，隐藏且留空；有值视为机器人 |

### Webhook 数据格式

```json
{"event":"message.created","site":"官网","id":1,"site_id":1,"name":"张三",
 "contact":"138...","content":"...","page_url":"...","ip":"...","user_agent":"..."}
```

## 目录结构

```
msghub/
├── app/
│   ├── main.py        # 路由：公开提交接口 + 管理接口
│   ├── chat.py        # 在线对话：访客 / 后台 WebSocket、对话接口
│   ├── qq.py          # QQ 官方机器人：回调验签、转发、客服指令
│   ├── auto.py        # AI 自动应答、转留言、网站内容抓取
│   ├── media.py       # 图片上传与存储
│   ├── common.py      # 限流、来源校验
│   ├── db.py          # SQLite 表结构
│   ├── auth.py        # 管理员登录令牌
│   ├── config.py      # 配置
│   └── static/        # 管理后台（index.html/admin.js/admin.css）与 embed.js 挂件
├── data/              # 运行数据（已 gitignore）
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## 后续可扩展

- 邮件 / Telegram 通知
- 多管理员账号与权限
- 留言回复（通过邮件回复访客）
- 图形验证码 / Turnstile 人机验证
