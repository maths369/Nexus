"""基于视觉大模型的 OCR 文本提取器。

通过 DashScope OpenAI 兼容接口调用 Qwen-VL 系列模型，
实现对手写体、印刷体、复杂版面的高质量文字识别。

返回签名符合 ArtifactService 的 image_text_extractor 协议：
    Callable[[Path], tuple[str, str]]
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 支持的图片 MIME 类型
_SUPPORTED_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# 默认 VL 模型和提示词
DEFAULT_VL_MODEL = "qwen-vl-max"
DEFAULT_VL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

OCR_SYSTEM_PROMPT = (
    "你是一个专业的 OCR 文字识别引擎。"
    "请仔细识别图片中的所有文字内容，包括手写体、印刷体、表格、公式等。"
    "要求：\n"
    "1. 尽可能完整准确地还原图片中的文字\n"
    "2. 保持原文的段落结构和换行\n"
    "3. 对于数学公式，使用 LaTeX 格式表示\n"
    "4. 对于表格，使用 Markdown 表格格式\n"
    "5. 如果文字模糊不清，用 [?] 标记不确定的字符\n"
    "6. 只输出识别到的文字内容，不要添加解释或评论"
)


def build_vision_ocr_extractor(
    *,
    api_key: str,
    base_url: str = DEFAULT_VL_BASE_URL,
    model: str = DEFAULT_VL_MODEL,
    timeout_seconds: float = 30.0,
):
    """构造视觉 OCR 提取器闭包。

    返回的函数签名：(image_path: Path) -> tuple[str, str]
    可直接作为 ArtifactService 的 image_text_extractor 参数。
    """

    def extract(image_path: Path) -> tuple[str, str]:
        """同步调用视觉模型提取图片文字。"""
        if not image_path.exists():
            return "", "vision_ocr:file_not_found"

        ext = image_path.suffix.lower()
        mime = _SUPPORTED_MIMES.get(ext)
        if not mime:
            mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"

        # 读取并 base64 编码
        raw = image_path.read_bytes()
        if not raw:
            return "", "vision_ocr:empty_file"

        b64 = base64.b64encode(raw).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        # 构造多模态消息
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": OCR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": "请识别这张图片中的所有文字内容。"},
                ],
            },
        ]

        try:
            import openai

            client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                max_tokens=4096,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                logger.info(
                    "视觉 OCR 成功: file=%s model=%s chars=%d",
                    image_path.name,
                    model,
                    len(text),
                )
                return text, f"vision_ocr:{model}"
            return "", f"vision_ocr:{model}:empty"
        except Exception as exc:
            logger.warning(
                "视觉 OCR 失败，将回退到 Tesseract: file=%s err=%s",
                image_path.name,
                exc,
                exc_info=True,
            )
            # 返回空字符串，上层 _extract_image_text 会级联到 Tesseract
            return "", None  # type: ignore[return-value]

    return extract
