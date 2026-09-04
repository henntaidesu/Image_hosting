"""按需生成并缓存缩略图。

图床对外只提供原图时，调用方（例如库存管理系统的商品列表）为了不在一屏里拉三十张
几 MB 的原图，只能自己下载原图再本地缩放——那等于把图床省下的带宽又原样吃回去。
这里在图床侧提供 ``/images/<slug>/<name>?w=560``：首次请求生成一份 JPEG 缩略图落盘，
之后直接命中缓存。

尺寸只接受 ``config.DERIVATIVE_WIDTHS`` 里的固定档位（请求值向上取整），否则任何人都能
用连续变化的 ``w`` 把服务器的 CPU 和磁盘吃光。
"""

import warnings
from pathlib import Path

from PIL import Image, ImageOps

from . import config


def normalize_width(raw):
    """把请求的宽度收敛到最近的档位；非法或缺省返回 None（表示要原图）。"""
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for width in config.DERIVATIVE_WIDTHS:
        if value <= width:
            return width
    return config.DERIVATIVE_WIDTHS[-1]


def derivative_path(slug, stored_name, width):
    # 每次现算，不用模块级常量：DATA_DIR 在运行时会被改写（打包态、测试）
    stem = stored_name.rsplit(".", 1)[0] if "." in stored_name else stored_name
    safe_stem = stem.replace("/", "_").replace("\\", "_")
    return config.DATA_DIR / config.DERIVATIVE_DIR_NAME / slug / f"{safe_stem}_w{width}.jpg"


def build_derivative(source, slug, stored_name, width):
    """返回缩略图路径；生成失败（源文件不是可解码图片）时返回 None，由调用方回退原图。"""
    # 视频没有缩略图这一档：直接返回 None，让调用方回退到原文件。不进 PIL——用 Image.open
    # 去啃一个 mp4 只会抛异常再落到同样的 None，白读一遍几百 MB。
    extension = stored_name.rsplit(".", 1)[-1].lower() if "." in stored_name else ""
    if extension in config.VIDEO_EXTENSIONS:
        return None
    target = derivative_path(slug, stored_name, width)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(source)
            if image.width * image.height > config.MAX_IMAGE_PIXELS:
                return None
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            if max(image.size) > width:
                scale = width / max(image.size)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            image.save(temporary, "JPEG", quality=78, optimize=True)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError):
        Path(temporary).unlink(missing_ok=True)
        return None
    temporary.replace(target)
    return target


def purge_derivatives(slug, stored_name):
    """原图删除后清掉它的所有缩略图，避免缓存目录只增不减。"""
    for width in config.DERIVATIVE_WIDTHS:
        derivative_path(slug, stored_name, width).unlink(missing_ok=True)
