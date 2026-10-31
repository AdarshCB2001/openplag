"""Download candidate pages / PDFs and extract their text, with a small disk cache."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

from .extract import html_to_text, pdf_to_text

CACHE_DIR = Path(os.environ.get("PLAGCHECK_CACHE", Path(__file__).resolve().parent.parent / ".cache"))
CACHE_TTL = 7 * 24 * 3600
MAX_BYTES = 15_000_000
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3", ".zip", ".exe", ".svg")


def _key(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def cache_get(url: str):
    p = _key(url)
    try:
        if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
            return json.loads(p.read_text("utf-8"))["text"]
    except Exception:
        pass
    return None


def cache_put(url: str, text: str):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _key(url).write_text(json.dumps({"url": url, "text": text}), "utf-8")
    except Exception:
        pass


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """Return extracted text ('' on failure)."""
    if not url.startswith(("http://", "https://")) or url.lower().split("?")[0].endswith(SKIP_EXT):
        return ""
    cached = cache_get(url)
    if cached is not None:
        return cached
    try:
        async with client.stream("GET", url, headers=BROWSER_HEADERS, timeout=20) as r:
            if r.status_code >= 400:
                return ""
            ctype = r.headers.get("content-type", "").lower()
            if any(x in ctype for x in ("image/", "video/", "audio/", "zip", "octet-stream")) \
                    and not url.lower().endswith(".pdf"):
                return ""
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    break
            data = bytes(buf)
            final_url = str(r.url)
    except Exception:
        return ""
    try:
        if "pdf" in ctype or data[:4] == b"%PDF":
            text = await asyncio.to_thread(pdf_to_text, data)
        else:
            enc = "utf-8"
            if "charset=" in ctype:
                enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
            try:
                html = data.decode(enc, "ignore")
            except LookupError:
                html = data.decode("utf-8", "ignore")
            text = await asyncio.to_thread(html_to_text, html, final_url)
    except Exception:
        text = ""
    text = (text or "")[:2_000_000]
    if text:
        cache_put(url, text)
    return text
