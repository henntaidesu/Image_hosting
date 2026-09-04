# Image Hosting

一个可自托管的 Python 图片托管服务：管理端创建多个项目，图片按项目分目录保存，并通过稳定的公开 URL 对外提供。

## 快速启动

```powershell
cd Picture_bed
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PICTURE_BED_INSECURE_COOKIES = "1"  # 本机 HTTP 开发时使用；公网部署不要设置
python app.py
```

也可以直接双击 `start.bat`；它会激活已创建的 `picture-bed` Conda 环境并启动服务。

打开 `http://127.0.0.1:9990` 登录管理端。首次密码为 `admin`；请立即进入“系统设置”改为至少 8 个字符的强密码。

管理员密码哈希、会话密钥、上传大小上限、公开访问基地址、项目与图片索引均保存在 SQLite 数据库中，无需 `.env` 文件。数据库位于 `data/`；请连同所有配置的图片存储目录一起备份。

## 项目结构

- `app.py`：唯一启动入口，负责创建应用并启动 Waitress。
- `src/`：应用代码，以及 `templates/`、`static/` 页面资源。
- `tests/`：隔离运行的自动化测试。
- `docs/`：安全部署文档。
- `data/`：SQLite 数据库及默认上传目录，不纳入 Git。

创建项目（如 `website`）后，图片会通过以下路径公开：

```
http://你的域名/images/website/随机文件名.png
```

创建项目时请填写图片存储目录的绝对路径，例如 `D:\ImageHosting\website`。该目录可以位于任意本机硬盘，服务会在目录不存在时自动创建它。

每个项目可在项目详情中添加多个存储位置。新增位置会立即成为“当前写入位置”，此后的新图片只写入新位置；手动切换也会立即生效。磁盘空间不足或写入失败时，服务按故障切换优先级自动选择下一个可用位置，并将其设为新的当前写入位置。每张图片会在数据库中记录实际保存目录，因此旧图片不会因切换而无法访问。

## 给其他项目使用

在项目详情中复制其 API Token，然后以 `multipart/form-data` 调用：

```bash
curl -X POST "http://127.0.0.1:9990/api/v1/projects/website/images" \
  -H "Authorization: Bearer <项目 API Token>" \
  -F "file=@./logo.png"
```

服务返回：`{"url":"公开图片地址","path":"/images/website/...","project":"website",...}`。

### API 一览（`/api/v1`）

全部只认 `Authorization: Bearer <项目 API Token>`，只回 JSON。

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/projects/<slug>/ping` | 连接自检：项目信息 + 服务端限制（单文件上限、允许扩展名、缩略图档位） |
| POST | `/api/v1/projects/<slug>/images` | 上传。可带 `external_key`（幂等）与 `sha256`（内容校验） |
| GET | `/api/v1/projects/<slug>/images` | 列表，按 `after_id` 游标翻页 |
| POST | `/api/v1/projects/<slug>/images/lookup` | 按 `external_keys` 数组批量查在不在 |
| GET | `/api/v1/projects/<slug>/images/<存储名>` | 单张元数据 |
| DELETE | `/api/v1/projects/<slug>/images/<存储名>` | 删除（幂等：删不存在的也返回成功） |

**`external_key` 是接入方自己的稳定标识**（例如原文件名）。带上它之后上传就是幂等的：同一个
key 重复上传直接返回已有记录、不产生第二份文件。搬运一批历史图片时中断再重跑，靠的就是这个。

### 缩略图

公开图片地址支持 `?w=<像素>`，首次请求生成一份 JPEG 缩略图落盘，之后命中缓存：

```
https://images.example.com/images/website/xxxx.png?w=300
```

宽度只接受固定档位（100 / 200 / 300 / 400 / 560 / 800 / 1200），请求值向上取整到最近的一档。
不设白名单的话，任何人都能用连续变化的 `w` 逼服务生成上千份不同尺寸，把 CPU 和磁盘一起吃光。

### 内网直连要填「附加访问主机名」

一旦在「系统设置」里填了公开访问基地址，服务默认只接受**该域名**的请求。同机或局域网里的
程序按 IP 直连调 API（例如 `http://192.168.1.5:9990`）会被判成非法主机而 400。把这些地址填进
「附加访问主机名」即可（空格或逗号分隔，可省略端口）。

## 对外部署

服务默认监听 `0.0.0.0:9990`。通过 Nginx 或 Caddy 将本机的 `127.0.0.1:9990` 反向代理为 HTTPS 域名，然后在“系统设置”中填写该公网域名。管理端复制的图片链接与 API 返回地址会自动使用这个基地址。

详细步骤见 `docs/security-deployment.md`。
