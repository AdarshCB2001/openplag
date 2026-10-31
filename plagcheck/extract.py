"""Turn uploaded or downloaded files into plain text."""
from __future__ import annotations

import io
import re
import zipfile


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            txt = data.decode(enc)
            if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return txt
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def pdf_to_text(data: bytes, max_pages: int = 120) -> str:
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)
    except Exception:
        text = ""
    if len(text.strip()) < 200:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])
        except Exception:
            pass
    # re-join words hyphenated across line breaks, keep paragraph breaks
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"(?<![.!?:\n])\n(?!\n)", " ", text)
    return text


def html_to_text(html: str, url: str | None = None) -> str:
    try:
        import trafilatura
        txt = trafilatura.extract(html, url=url, favor_recall=True, include_tables=True,
                                  include_comments=False)
        if txt and len(txt) > 200:
            return txt
    except Exception:
        pass
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
            tag.decompose()
        return re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n"))
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def docx_to_text(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def odt_to_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("content.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</text:p>|<text:line-break/>|</text:h>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def rtf_to_text(data: bytes) -> str:
    s = _decode(data)
    s = re.sub(r"\\par[d]?", "\n", s)
    s = re.sub(r"\{\\\*[^{}]*\}|\\[a-z]+-?\d* ?|[{}]", "", s)
    return s


def file_to_text(name: str, data: bytes) -> str:
    n = (name or "").lower()
    if n.endswith(".pdf") or data[:4] == b"%PDF":
        return pdf_to_text(data)
    if n.endswith(".docx"):
        return docx_to_text(data)
    if n.endswith(".odt"):
        return odt_to_text(data)
    if n.endswith(".rtf"):
        return rtf_to_text(data)
    if n.endswith((".html", ".htm")):
        return html_to_text(_decode(data))
    if n.endswith(".doc"):
        raise ValueError("Old .doc files are not supported. Save it as .docx or PDF and try again.")
    return _decode(data)
