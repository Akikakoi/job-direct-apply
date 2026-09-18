"""扫描件 PDF OCR（P4）：渲染页面为图片 → OpenAI 兼容视觉 API 抽取文本。

触发条件（text_extract.py）：pypdf 抽出的文本过少（平均每页 < 20 字符）
即判定为扫描件/图片型 PDF。

后端配置（backend/.env，OpenAI 兼容协议，如 DashScope qwen-vl）：
    OCR_API_KEY=sk-...
    OCR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    OCR_MODEL=qwen-vl-plus
    OCR_MAX_PAGES=5        # 最多渲染页数（成本控制）

未配置 OCR_API_KEY 时抛 OcrNotConfigured——上传接口返回 400 明确提示，
而不是静默给出空文本（防幻觉）。
"""

from __future__ import annotations

import base64

import httpx

from app.core.config import settings

_PROMPT = (
    "你是 OCR 引擎。逐字转录图片中的所有文字，保持原有段落顺序，"
    "只输出文字本身，不要输出任何解释、页码标记或 Markdown 格式。"
)


class OcrNotConfigured(Exception):
    """ocr_api_key 未配置。"""


def render_pdf_pages(data: bytes, max_pages: int | None = None) -> list[bytes]:
    """pymupdf 渲染每页为 PNG（150 DPI，成本与可读性平衡）。"""
    import pymupdf

    pages = max_pages or settings.ocr_max_pages
    out: list[bytes] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc[:pages]:
            pix = page.get_pixmap(dpi=150)
            out.append(pix.tobytes("png"))
    return out


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _client() -> httpx.Client:
    if not settings.ocr_api_key:
        raise OcrNotConfigured(
            "扫描件 PDF 需要 OCR：请在 backend/.env 配置 OCR_API_KEY/OCR_BASE_URL/OCR_MODEL"
        )
    return httpx.Client(
        base_url=settings.ocr_base_url,
        headers={"Authorization": f"Bearer {settings.ocr_api_key}"},
        timeout=120,
    )


def ocr_pdf(data: bytes) -> str:
    """逐页视觉抽取，页文本用换行拼接；单页失败记占位不中断。"""
    pages = render_pdf_pages(data)
    if not pages:
        raise ValueError("PDF 无可渲染页面")
    texts: list[str] = []
    with _client() as client:
        for i, png in enumerate(pages, 1):
            try:
                resp = client.post(
                    "/chat/completions",
                    json={
                        "model": settings.ocr_model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": _data_uri(png)}},
                                    {"type": "text", "text": _PROMPT},
                                ],
                            }
                        ],
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                texts.append(resp.json()["choices"][0]["message"]["content"].strip())
            except Exception as exc:
                texts.append(f"[第{i}页 OCR 失败: {type(exc).__name__}]")
    return "\n".join(t for t in texts if t)
