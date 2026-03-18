"""PDF 文字擷取"""
import pdfplumber
from typing import Optional


def extract_text(filepath: str, max_pages: Optional[int] = None) -> tuple[str, int]:
    """從 PDF 擷取文字內容

    Returns:
        (text, page_count)
    """
    pages_text = []
    page_count = 0

    with pdfplumber.open(filepath) as pdf:
        page_count = len(pdf.pages)
        limit = max_pages if max_pages else page_count
        for i, page in enumerate(pdf.pages[:limit]):
            text = page.extract_text()
            if text:
                pages_text.append(text)

    full_text = "\n\n".join(pages_text)
    return full_text, page_count
