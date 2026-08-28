# 公网部署安全说明

## 启动方式

安装更新后的依赖后，使用 `start.bat` 或 `python app.py` 启动服务。它使用 Waitress，并监听 `0.0.0.0:9990`。公网部署必须通过 HTTPS 反向代理访问，避免在明文 HTTP 上传输登录凭据与会话 Cookie。

本机 HTTP 开发时可在当前 PowerShell 会话中临时设置 `$env:PICTURE_BED_INSECURE_COOKIES = "1"` 和 `$env:PICTURE_BED_HOST = "127.0.0.1"`；公网部署不要设置这些变量。

## 首次公网部署

1. 在仅绑定 `127.0.0.1` 的本机 HTTP 开发模式下登录后台。
2. 在“系统设置”中把公开访问基地址设置为完整 HTTPS 域名，例如 `https://images.example.com`。
3. 停止本地开发服务，使用 `start.bat` 启动安全模式。
4. 配置反向代理的域名、证书路径和请求限速；如使用 Nginx，将 `limit_req_zone` 指令放到 `http {}` 块中。
5. 执行 `nginx -t` 验证配置后重新加载 Nginx。

公开访问基地址同时是应用允许接收的 Host。Nginx 的 `server_name` 必须与该域名一致；域名未配置前，应用只接受本机 Host，避免意外暴露。

## 文件权限与网络边界

- 仅让运行 Image Hosting 的服务账号读写 `data/picture_bed.sqlite3` 与上传目录；备份文件也必须按同等敏感级别保护。
- Nginx 的站点根目录不要指向项目目录，全部请求应使用反向代理转发；否则数据库或其他运行文件可能被直接下载。
- 推荐防火墙仅开放 80/443，并将 9990 限制为反向代理主机可访问；同时拒绝外网访问数据库和上传目录所在主机的管理端口。
- `client_max_body_size` 必须不大于后台配置的单文件上传上限；修改后台上限时同步调整 Nginx 的值。

## 已实现的应用侧控制

- CSRF Token 覆盖登录、退出和所有管理端写操作；Bearer Token 上传 API 不使用 Cookie，因此不纳入 CSRF。
- HTTPS 会话使用 `Secure`、`HttpOnly`、`SameSite=Lax` Cookie，并在 8 小时后失效。
- 应用限制请求体、表单字段与单次上传文件数，并拒绝超像素或超帧图片。
- 已提供 Host 白名单、可信的一层 Nginx 转发头和安全响应头配置。
