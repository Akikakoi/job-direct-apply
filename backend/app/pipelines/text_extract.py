"""简历文件文本抽取：txt/md 直读；pdf 先 pypdf（纯文本型），
抽出文本过少判定为扫描件 → 走 OCR（app/pipelines/ocr.py，需配置视觉 API）。"""

from __future__ import annotations

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5MB
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf"}
# 平均每页低于该字符数 → 判定扫描件（pypdf 对图片型 PDF 只能抽出空串/乱码页眉）
SCANNED_CHARS_PER_PAGE = 20


class UnsupportedFile(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFile(f"不支持的文件类型: {filename}（支持 txt/md/pdf）")
    if len(data) > MAX_FILE_BYTES:
        raise UnsupportedFile("文件超过 5MB 限制")
    if suffix == ".pdf":
        return extract_pdf_text(data)
    return data.decode("utf-8", errors="replace")


def extract_pdf_text(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(p for p in pages if p.strip())
    if _looks_scanned(reader.pages and len(reader.pages) or 1, text):
        # 扫描件：pypdf 无能为力，转 OCR（未配置 OCR 时抛 OcrNotConfigured 明确报错）
        from app.pipelines.ocr import ocr_pdf

        return ocr_pdf(data)
    return text


def _looks_scanned(n_pages: int, text: str) -> bool:
    if n_pages <= 0:
        return False
    return len(text.strip()) / n_pages < SCANNED_CHARS_PER_PAGE
