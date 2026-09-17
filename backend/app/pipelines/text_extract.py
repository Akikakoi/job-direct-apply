"""简历文件文本抽取：txt/md 直读，pdf 用 pypdf（纯文本型；扫描件 OCR 挂账 P3+）。"""

from __future__ import annotations

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5MB
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf"}


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
    return "\n".join(p for p in pages if p.strip())
