# MsgHub 安全部署与升级

## 需要账号权限的操作

- **QQ 开放平台** · 运维创建回调密钥并启动新版后，由能管理机器人的人进入 q.qq.com，将单聊事件 `C2C_MESSAGE_CREATE` 的回调更新为后台显示的新地址并保存。旧地址停用。回调验证通过后，用已绑定 QQ 发一条测试回复，浏览器核对收发。两个地址无法同时使用，安排切换窗口。
- **模型服务** · 若现有接口使用 HTTP、私网地址或认证重定向，由模型服务管理者提供可直接访问的公网 HTTPS 接口。部署前核对模型是否满足这些条件，切换后用后台 AI 测试验证。知识资料可继续抓取公开 HTTP(S) 页面。Webhook 使用 HTTPS，模型与 Webhook 的请求均直接访问最终地址。
- **CDN 或上游代理** · 若站点经过 CDN，由有管理权限的人确认可信来源网段和该 CDN 的真实访客 IP 头。配置后清除旧 `/uploads/*` 公共缓存，并确保 `/api/media/*` 和 `/api/admin/*` 不经过共享缓存。

## 服务器切换

1. 备份 MySQL 和 `data/`，保留现有镜像。首次部署用 `cp -n .env.example .env` 创建配置，再填管理员密码、数据库名和专用运行账号。升级保留现有配置。
2. 为启用 QQ 的实例生成独立随机回调密钥，可用 `openssl rand -hex 32`。把结果只写入服务器的 `.env` 中的 `QQ_CALLBACK_KEY`，保留该文件的 `0600` 权限。所有会记录路径的代理、观察平台或 CDN 都要隐藏该路径及媒体签名。
3. Compose 容器使用 UID/GID `10001:10001`。启动前为 `data/`、`data/uploads/`、已有上传文件和 `data/.secret` 设置该用户所需的读写权限，签名密钥保留 `0600`。备份目录和文件保留原有权限。容器通过 `mariadb-shared` 网络和 TLS 连接共享 MariaDB 的 `mysql:3306`。
4. 为 MsgHub 库准备两个 MariaDB 账号。迁移账号需能在该库建表、修改表、建立外键及读写。运行账号只对 MsgHub 库授予 `SELECT, INSERT, UPDATE, DELETE`，并收回其他库范围授权。执行迁移后再用运行账号启动服务。

```bash
docker compose build msghub
# 升级时在维护窗口暂停旧实例，然后执行迁移。
# 从已获授权的安全来源加载迁移用户名和密码变量。
DB_USER="$MSGHUB_MIGRATION_USER" DB_PASSWORD="$MSGHUB_MIGRATION_PASSWORD" \
  docker compose run --rm --no-deps msghub python -m app.db
docker compose up -d msghub
```

运行账号启动时检查 schema 版本。升级会为历史上仅属于一个站点的访客绑定该站点并保留续接。无会话、或此前跨站使用的旧令牌会重新建档，已有会话仍可在后台查看。新版部署后管理员需要重新登录。已附到会话的历史图片通过有效期十分钟的签名链接访问，重新打开会话可加载新链接。旧公开路径将返回 404。无引用的图片最早在创建后 24 小时清理。

## 反代设置

公网反代继续走 HTTPS，后端端口保持绑定本机。先确认 Nginx 到容器的实际 TCP 来源，在 `FORWARDED_ALLOW_IPS` 中配置该单个地址。Docker 网桥部署可用以下命令查看候选网关，并在上线验收中核对应用实际收到的地址。

```bash
docker inspect --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{println}}{{end}}' msghub
```

在现有 MsgHub 转发段覆盖客户端提交的代理头。如果有 CDN，在此之前用 Nginx `real_ip_header` 和 `set_real_ip_from` 从明确可信的 CDN 网段还原 `$remote_addr`。

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
```

普通 API 请求体可按 `256k` 限制。为两个图片上传路径保留 `5184k`，登录保留 `4k`，并继承现有反代参数。按真实访客 IP 为登录、留言、上传和 WebSocket 握手限流。确保新增路径的 location 优先级高于现有反代匹配，两个 WebSocket 端点继续转发 Upgrade。

在 Nginx 的 `http` 作用域定义以下映射，并在 MsgHub 的 `server` 作用域给现有 access_log 追加 `if=$msghub_loggable`。

```nginx
map $uri $msghub_loggable {
    default 1;
    ~^/api/qq/callback(?:/|$) 0;
    ~^/api/media(?:/|$) 0;
}
```

生产切换前用 `nginx -t` 检查。切换后核对 HTTPS、后台登录和登出、访客实时消息和图片、QQ 回调、AI 测试，以及反代记录的实际客户端地址。
