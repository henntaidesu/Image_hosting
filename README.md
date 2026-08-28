# Picture Bed

一个可自托管的 Python 图床服务：管理端创建多个项目，图片按项目分目录保存，并通过稳定的公开 URL 对外提供。

## 快速启动

```powershell
cd Picture_bed
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PICTURE_BED_INSECURE_COOKIES = "1"  # 本机 HTTP 开发时使用；公网部署不要设置
python serve.py
```

也可以直接双击 `start.bat`；它会激活已创建的 `picture-bed` Conda 环境并启动服务。

打开 `http://127.0.0.1:8000` 登录管理端。首次密码为 `admin`；请立即进入“系统设置”改为至少 8 个字符的强密码。

管理员密码哈希、会话密钥、上传大小上限、公开访问基地址、项目与图片索引均保存在 SQLite 数据库中，无需 `.env` 文件。数据库位于 `data/`；请连同所有配置的图片存储目录一起备份。

创建项目（如 `website`）后，图片会通过以下路径公开：

```
http://你的域名/images/website/随机文件名.png
```

创建项目时请填写图片存储目录的绝对路径，例如 `D:\PictureBed\website`。该目录可以位于任意本机硬盘，服务会在目录不存在时自动创建它。

每个项目可在项目详情中添加多个存储位置。新增位置会立即成为“当前写入位置”，此后的新图片只写入新位置；手动切换也会立即生效。磁盘空间不足或写入失败时，服务按故障切换优先级自动选择下一个可用位置，并将其设为新的当前写入位置。每张图片会在数据库中记录实际保存目录，因此旧图片不会因切换而无法访问。

## 给其他项目使用

在项目详情中复制其 API Token，然后以 `multipart/form-data` 调用：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/projects/website/images" \
  -H "Authorization: Bearer <项目 API Token>" \
  -F "file=@./logo.png"
```

服务返回：`{"url":"公开图片地址","path":"/images/website/...","project":"website"}`。

## 对外部署

通过 Nginx 或 Caddy 将本机的 `127.0.0.1:8000` 反向代理为 HTTPS 域名，然后在“系统设置”中填写该公网域名。管理端复制的图片链接与 API 返回地址会自动使用这个基地址。
