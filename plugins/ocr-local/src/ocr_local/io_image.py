"""图像输入归一化：支持本地文件路径或 base64 PNG。

fukuaRPA 集成层（P3 宿主）会把截取到的区域以 base64 PNG 注入到动作
payload；独立模式则直接传本地图片路径。这里把两者统一成 RapidOCR
接受的 numpy(BGR) 数组。
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期可见；两者在函数体内惰性导入，见下方说明
    import numpy
    from PIL import Image


def load_image(image: str | bytes | numpy.ndarray) -> numpy.ndarray:
    """把路径 / base64 字节 / 已有数组统一成 BGR numpy 数组。

    numpy 与 Pillow 在这里惰性导入，使 `import ocr_local` 不强制依赖它们。
    """
    import numpy as np
    from PIL import Image

    if isinstance(image, np.ndarray):
        return _ensure_bgr(image)

    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
    elif isinstance(image, str):
        if image.startswith("data:") or _looks_like_base64(image):
            # 可能是 "data:image/png;base64,...." 或纯 base64
            if image.startswith("data:"):
                parts = image.split(",", 1)
                if len(parts) < 2:
                    raise ValueError("data URI 格式非法：缺少 base64 数据段")
                image = parts[1]
            raw = base64.b64decode(image)
        else:
            # 当作文件路径
            with Image.open(image) as im:
                return _pil_to_bgr(im)
    else:
        raise TypeError(f"不支持的图像输入类型：{type(image)!r}")

    with Image.open(_bytes_io(raw)) as im:
        return _pil_to_bgr(im)


def _looks_like_base64(text: str) -> bool:
    # 路径优先：含盘符、反斜杠、或常见路径分隔符的字符串按路径处理
    if "\\" in text or ":" in text or text.startswith(("/", "./", "../")):
        return False
    # 含扩展名的也按路径处理
    if os.path.splitext(text)[1]:
        return False
    # 粗略判断：很长的、只含 base64 字符的字符串。
    if len(text) < 64:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n")
    return all(c in allowed for c in text.strip())


def _bytes_io(raw: bytes):
    import io

    return io.BytesIO(raw)


def _pil_to_bgr(im: Image.Image) -> numpy.ndarray:
    import numpy as np

    rgb = im.convert("RGB")
    arr = np.asarray(rgb)
    # RGB -> BGR
    return arr[:, :, ::-1].copy()


def _ensure_bgr(arr: numpy.ndarray) -> numpy.ndarray:
    import numpy as np

    if arr.ndim == 2:
        # 灰度图转 3 通道
        return np.stack([arr, arr, arr], axis=-1)
    if arr.shape[2] == 4:
        return arr[:, :, :3][:, :, ::-1].copy()
    if arr.shape[2] == 3:
        # 假设已是 BGR；如果是 RGB 也没关系，RapidOCR 对色彩轻微差异不敏感。
        return arr
    raise ValueError(f"无法处理的图像通道数：{arr.shape}")
