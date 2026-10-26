"""OCR 引擎：把后端结果做阈值过滤与结构化。"""

from __future__ import annotations

from .backend import OcrBackend, get_backend
from .io_image import load_image
from .models import OcrResult


def ocr_image(
    image: str | bytes | object,
    *,
    backend: OcrBackend | None = None,
    backend_name: str = "rapidocr",
    score_threshold: float = 0.5,
) -> OcrResult:
    """对一张图像做 OCR，返回按置信度过滤后的 ``OcrResult``。

    参数
    ----
    image: 本地图片路径、base64 PNG 字符串，或已加载的 numpy 数组。
    backend: 已构造好的后端；为 None 时按 ``backend_name`` 从进程级缓存取（首次惰性构造）。
    score_threshold: 低于该置信度的识别结果会被丢弃。
    """
    if backend is None:
        backend = get_backend(backend_name)
    arr = load_image(image)  # type: ignore[arg-type]
    result = backend.recognize(arr)
    result.words = [w for w in result.words if w.score >= score_threshold]
    return result
