"""扫描件 OCR 测试（P4）：mock 视觉 API，不触网；渲染用真实最小 PDF。"""

from __future__ import annotations

import pytest

from app.pipelines.ocr import OcrNotConfigured, ocr_pdf, render_pdf_pages
from app.pipelines.text_extract import _looks_scanned, extract_pdf_text


def _make_pdf(text: str) -> bytes:
    """程序化构造合法 PDF（同 test_resume_parse.py 的最小 PDF）。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj ".encode() + body + b" endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    return out


def test_looks_scanned():
    assert _looks_scanned(1, "") is True
    assert _looks_scanned(2, "short") is True  # 平均 < 20 字符/页
    assert _looks_scanned(1, "x" * 100) is False
    assert _looks_scanned(0, "anything") is False


def test_render_pdf_pages():
    pages = render_pdf_pages(_make_pdf("Hello OCR"), max_pages=3)
    assert len(pages) == 1
    assert pages[0][:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数


def test_ocr_not_configured_raises():
    with pytest.raises(OcrNotConfigured):
        ocr_pdf(_make_pdf("scanned page"))


def test_ocr_pdf_success(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "ocr_api_key", "fake")
    monkeypatch.setattr(config.settings, "ocr_base_url", "https://ocr.test/v1")
    monkeypatch.setattr(config.settings, "ocr_model", "test-vision-model")

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "陈布凡  后端工程师  8年经验"}}]}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("app.pipelines.ocr._client", lambda: FakeClient())
    text = ocr_pdf(_make_pdf("scanned"))  # PDF 内容无所谓，走 mock
    assert text == "陈布凡  后端工程师  8年经验"
    assert captured["url"] == "/chat/completions"
    # 消息里应含图片 data URI + OCR 指令
    content = captured["json"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["type"] == "text"


def test_extract_pdf_scanned_branch(monkeypatch):
    """文本型 PDF 正常走 pypdf；扫描件分支进 OCR（这里 mock 掉验证接线）。"""
    from app.pipelines import text_extract as te

    text_pdf = _make_pdf("Plain text resume with enough characters per page to avoid OCR trigger path")
    assert "Plain text resume" in extract_pdf_text(text_pdf)

    # 强制判定为扫描件 → 应调用 ocr_pdf
    called = []
    monkeypatch.setattr(te, "_looks_scanned", lambda n, t: True)

    def fake_ocr(data):
        called.append(data)
        return "OCR 结果文本"

    monkeypatch.setattr("app.pipelines.ocr.ocr_pdf", fake_ocr)
    assert extract_pdf_text(text_pdf) == "OCR 结果文本"
    assert called == [text_pdf]