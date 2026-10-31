"""Query generation and search providers.

Free, no key needed:
  web        - many search engines at once through the `ddgs` metasearch library
               (Google, Bing, Brave, DuckDuckGo, Mojeek, Yahoo, Yandex, Startpage…)
  wikipedia  - full article text through the MediaWiki API
  openalex   - 250M+ scholarly works (abstracts + open-access PDFs)
  crossref   - 150M+ DOI records (abstracts where deposited)
  semantic   - Semantic Scholar (abstracts + open-access PDFs)
  arxiv      - preprints (abstracts + PDFs)
  europepmc  - biomedical / life-science literature
  doaj       - open-access journals
Optional with a free key:
  core       - CORE: full text of millions of open-access papers
  google     - Google Programmable Search JSON API
  brave      - Brave Search API
"""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from .match import NORMAL, STOP, Analyzer

UA = "OpenPlag/1.0 (self-hosted plagiarism checker; https://github.com/)"


@dataclass
class Candidate:
    url: str
    title: str = ""
    snippet: str = ""
    providers: set = field(default_factory=set)
    type: str = "web"
    text: str = ""  # text supplied by the provider (abstract / full text)
    full_text: bool = False
    pdf_url: str = ""
    hits: int = 0
    rank: float = 0.0


def canon(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))]
        path = p.path.rstrip("/") or "/"
        return urlunsplit(("", host, path, urlencode(q), ""))
    except Exception:
        return url


# --------------------------------------------------------------------------- queries
def make_queries(an: Analyzer, max_q: int, length: int = 11):
    """Pick evenly spread, distinctive ~11-word windows from the document."""
    windows = []
    for (a, b) in an.sentences:
        idx = [t for t in range(a, b) if an.status[t] == NORMAL]
        if len(idx) < 6:
            continue
        starts = [0] if len(idx) <= length + 3 else range(0, len(idx) - length + 1, max(6, length - 2))
        for s in starts:
            sel = idx[s:s + length]
            words = [an.words[t] for t in sel]
            score = sum(1 for w in words if w not in STOP and len(w) > 3) + 0.05 * sum(len(w) for w in words)
            if sum(1 for w in words if w not in STOP) < 3:
                continue
            raw = an.text[an.offs[sel[0]][0]:an.offs[sel[-1]][1]]
            raw = re.sub(r"\s+", " ", raw)
            raw = re.sub(r"[\"“”«»]", "", raw)
            windows.append({"pos": sel[0], "score": score, "q": raw, "words": words})
    if len(windows) <= max_q:
        return windows
    out, size = [], len(windows) / max_q
    for i in range(max_q):
        bucket = windows[int(i * size):int((i + 1) * size)] or [windows[min(int(i * size), len(windows) - 1)]]
        out.append(max(bucket, key=lambda w: w["score"]))
    return out


def keywords(words, k=6):
    ws = [w for w in words if w not in STOP and len(w) > 3]
    seen, out = set(), []
    for w in sorted(ws, key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:k]


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# --------------------------------------------------------------------------- rate limiting
class Limiter:
    def __init__(self, concurrency: int, interval: float):
        self.sem = asyncio.Semaphore(concurrency)
        self.interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.sem.acquire()
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def __aexit__(self, *exc):
        self.sem.release()


# --------------------------------------------------------------------------- providers
class Provider:
    name = "base"
    label = "Base"
    type = "web"
    needs_key = False
    budget_ratio = 1.0  # share of the query list this provider gets
    budget_cap = 1000
    concurrency, interval = 4, 0.0

    def __init__(self, keys: dict, opts: dict):
        self.keys, self.opts = keys, opts
        self.limiter = Limiter(self.concurrency, self.interval)
        self.failures = 0
        self.disabled = False
        self.errors: list[str] = []

    def available(self) -> bool:
        return True

    def budget(self, n_queries: int) -> int:
        return min(self.budget_cap, max(1, int(round(n_queries * self.budget_ratio))))

    async def run(self, client, q) -> list[Candidate]:
        if self.disabled:
            return []
        try:
            async with self.limiter:
                res = await self.search(client, q)
            self.failures = 0
            return res
        except Exception as e:  # network / parsing / rate limit
            self.failures += 1
            msg = f"{type(e).__name__}: {str(e)[:120]}"
            if msg not in self.errors:
                self.errors.append(msg)
            if self.failures >= 6:
                self.disabled = True
            return []

    async def search(self, client, q) -> list[Candidate]:
        raise NotImplementedError

    async def get_json(self, client, url, **kw):
        headers = {"User-Agent": UA, **kw.pop("headers", {})}
        r = await client.get(url, headers=headers, timeout=20, **kw)
        if r.status_code == 429:
            await asyncio.sleep(3)
            r = await client.get(url, headers=headers, timeout=20, **kw)
        r.raise_for_status()
        return r.json()


class WebSearch(Provider):
    name, label, type = "web", "Web (multi-engine)", "web"
    concurrency, interval = 3, 0.4
    ENGINES = ["google", "bing", "brave", "duckduckgo", "mojeek", "yahoo", "yandex", "startpage"]

    def available(self):
        try:
            import ddgs  # noqa: F401
            return True
        except Exception:
            return False

    async def search(self, client, q):
        from ddgs import DDGS
        engines = self.opts.get("engines") or ["auto"]
        backend = ",".join(engines)
        region = self.opts.get("region") or "us-en"
        text = f'"{q["q"]}"' if self.opts.get("exact_phrase") else q["q"]

        def _run():
            # ask for ~10 results per engine so every selected engine is actually queried
            n = self.opts.get("per_query") or min(80, 10 * (len(engines) if engines != ["auto"] else 6))
            return DDGS(timeout=15).text(text, region=region, max_results=n, backend=backend)

        rows = await asyncio.to_thread(_run)
        return [Candidate(url=r.get("href") or r.get("url") or "", title=r.get("title", ""),
                          snippet=r.get("body", ""), type="web") for r in rows if r.get("href") or r.get("url")]


class Wikipedia(Provider):
    name, label, type = "wikipedia", "Wikipedia", "encyclopedia"
    budget_ratio, budget_cap, concurrency, interval = 0.6, 40, 4, 0.1

    async def search(self, client, q):
        lang = self.opts.get("wiki_lang") or "en"
        data = await self.get_json(client, f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": q["q"], "srlimit": 4, "format": "json"})
        out = []
        for r in data.get("query", {}).get("search", []):
            title = r["title"]
            out.append(Candidate(url=f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                                 title=f"{title} – Wikipedia", snippet=strip_tags(r.get("snippet", "")),
                                 type="encyclopedia"))
        return out


async def wikipedia_text(client, url: str) -> str:
    m = re.match(r"https://(\w+)\.wikipedia\.org/wiki/(.+)", url)
    if not m:
        return ""
    from urllib.parse import unquote
    r = await client.get(f"https://{m.group(1)}.wikipedia.org/w/api.php", headers={"User-Agent": UA}, params={
        "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
        "titles": unquote(m.group(2)).replace("_", " "), "format": "json"}, timeout=20)
    pages = r.json().get("query", {}).get("pages", {})
    return "\n".join(p.get("extract", "") for p in pages.values())


class OpenAlex(Provider):
    name, label, type = "openalex", "OpenAlex (scholarly)", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.6, 40, 3, 0.15

    async def search(self, client, q):
        params = {"search": q["q"], "per-page": 6,
                  "select": "id,doi,display_name,abstract_inverted_index,best_oa_location,primary_location"}
        if self.keys.get("email"):
            params["mailto"] = self.keys["email"]
        data = await self.get_json(client, "https://api.openalex.org/works", params=params)
        out = []
        for w in data.get("results", []):
            inv = w.get("abstract_inverted_index") or {}
            pos = {}
            for word, idxs in inv.items():
                for i in idxs:
                    pos[i] = word
            abstract = " ".join(pos[i] for i in sorted(pos))
            oa = w.get("best_oa_location") or {}
            prim = w.get("primary_location") or {}
            url = w.get("doi") or oa.get("landing_page_url") or prim.get("landing_page_url") or w.get("id")
            out.append(Candidate(url=url, title=w.get("display_name") or "", text=abstract, snippet=abstract[:300],
                                 pdf_url=oa.get("pdf_url") or "", type="academic"))
        return out


class Crossref(Provider):
    name, label, type = "crossref", "Crossref (DOIs)", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.4, 25, 2, 0.3

    async def search(self, client, q):
        params = {"query": q["q"], "rows": 6, "select": "DOI,title,abstract,URL,link"}
        if self.keys.get("email"):
            params["mailto"] = self.keys["email"]
        data = await self.get_json(client, "https://api.crossref.org/works", params=params)
        out = []
        for it in data.get("message", {}).get("items", []):
            abstract = strip_tags(it.get("abstract", ""))
            if not abstract:
                continue  # nothing to compare without a fetchable page; skip noise
            pdf = next((l["URL"] for l in it.get("link", []) if "pdf" in l.get("content-type", "")), "")
            out.append(Candidate(url=f"https://doi.org/{it['DOI']}", title=(it.get("title") or [""])[0],
                                 text=abstract, snippet=abstract[:300], pdf_url=pdf, type="academic"))
        return out


class SemanticScholar(Provider):
    name, label, type = "semantic", "Semantic Scholar", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.3, 15, 1, 1.1

    async def search(self, client, q):
        headers = {"x-api-key": self.keys["s2"]} if self.keys.get("s2") else {}
        data = await self.get_json(client, "https://api.semanticscholar.org/graph/v1/paper/search", params={
            "query": " ".join(keywords(q["words"], 8)), "limit": 6, "fields": "title,abstract,url,openAccessPdf"},
            headers=headers)
        out = []
        for p in data.get("data", []) or []:
            if not p.get("abstract"):
                continue
            out.append(Candidate(url=p.get("url") or "", title=p.get("title") or "", text=p["abstract"],
                                 snippet=p["abstract"][:300], pdf_url=(p.get("openAccessPdf") or {}).get("url") or "",
                                 type="academic"))
        return out


class Arxiv(Provider):
    name, label, type = "arxiv", "arXiv", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.25, 12, 1, 3.0

    async def search(self, client, q):
        kw = keywords(q["words"], 5)
        if len(kw) < 3:
            return []
        query = " AND ".join(f"all:{w}" for w in kw)
        r = await client.get("https://export.arxiv.org/api/query", params={
            "search_query": query, "max_results": 5}, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.text)
        out = []
        for e in root.findall("a:entry", ns):
            summary = re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()
            link = e.findtext("a:id", "", ns)
            pdf = next((l.get("href") for l in e.findall("a:link", ns) if l.get("title") == "pdf"), "")
            out.append(Candidate(url=link, title=re.sub(r"\s+", " ", e.findtext("a:title", "", ns)),
                                 text=summary, snippet=summary[:300], pdf_url=pdf, type="academic"))
        return out


class EuropePMC(Provider):
    name, label, type = "europepmc", "Europe PMC (biomedical)", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.3, 15, 2, 0.3

    async def search(self, client, q):
        data = await self.get_json(client, "https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
            "query": " ".join(keywords(q["words"], 7)), "format": "json", "pageSize": 5, "resultType": "core"})
        out = []
        for r in data.get("resultList", {}).get("result", []):
            abstract = strip_tags(r.get("abstractText", ""))
            if not abstract:
                continue
            url = f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id')}"
            pdf = ""
            for u in (r.get("fullTextUrlList") or {}).get("fullTextUrl", []):
                if u.get("documentStyle") == "pdf" and u.get("availabilityCode") == "OA":
                    pdf = u.get("url", "")
            out.append(Candidate(url=url, title=r.get("title", ""), text=abstract, snippet=abstract[:300],
                                 pdf_url=pdf, type="academic"))
        return out


class DOAJ(Provider):
    name, label, type = "doaj", "DOAJ (open-access journals)", "academic"
    budget_ratio, budget_cap, concurrency, interval = 0.3, 15, 2, 0.5

    async def search(self, client, q):
        query = " ".join(keywords(q["words"], 6))
        data = await self.get_json(client, f"https://doaj.org/api/search/articles/{quote(query)}",
                                   params={"pageSize": 5})
        out = []
        for r in data.get("results", []):
            b = r.get("bibjson", {})
            abstract = strip_tags(b.get("abstract", ""))
            links = b.get("link", [])
            url = next((l.get("url") for l in links if l.get("url")), "")
            if not url or not abstract:
                continue
            out.append(Candidate(url=url, title=b.get("title", ""), text=abstract, snippet=abstract[:300],
                                 type="academic"))
        return out


class Core(Provider):
    name, label, type, needs_key = "core", "CORE (open-access full text)", "academic", True
    budget_ratio, budget_cap, concurrency, interval = 0.4, 20, 1, 1.0

    def available(self):
        return bool(self.keys.get("core"))

    async def search(self, client, q):
        data = await self.get_json(client, "https://api.core.ac.uk/v3/search/works", params={
            "q": q["q"], "limit": 5}, headers={"Authorization": f"Bearer {self.keys['core']}"})
        out = []
        for r in data.get("results", []):
            full = r.get("fullText") or ""
            text = full or r.get("abstract") or ""
            if not text:
                continue
            url = r.get("downloadUrl") or (r.get("links") or [{}])[0].get("url") or \
                (f"https://doi.org/{r['doi']}" if r.get("doi") else f"https://core.ac.uk/works/{r.get('id')}")
            out.append(Candidate(url=url, title=r.get("title", ""), text=text[:1_500_000], full_text=bool(full),
                                 snippet=(r.get("abstract") or "")[:300], type="academic"))
        return out


class GoogleCSE(Provider):
    name, label, type, needs_key = "google", "Google Programmable Search", "web", True
    budget_ratio, budget_cap, concurrency, interval = 1.0, 90, 3, 0.2

    def available(self):
        return bool(self.keys.get("google_key") and self.keys.get("google_cx"))

    async def search(self, client, q):
        data = await self.get_json(client, "https://www.googleapis.com/customsearch/v1", params={
            "key": self.keys["google_key"], "cx": self.keys["google_cx"], "q": q["q"], "num": 10})
        return [Candidate(url=i["link"], title=i.get("title", ""), snippet=i.get("snippet", ""), type="web")
                for i in data.get("items", [])]


class BraveAPI(Provider):
    name, label, type, needs_key = "brave", "Brave Search API", "web", True
    budget_ratio, budget_cap, concurrency, interval = 1.0, 200, 1, 1.05

    def available(self):
        return bool(self.keys.get("brave"))

    async def search(self, client, q):
        data = await self.get_json(client, "https://api.search.brave.com/res/v1/web/search", params={
            "q": q["q"], "count": 20}, headers={"X-Subscription-Token": self.keys["brave"],
                                                "Accept": "application/json"})
        return [Candidate(url=r["url"], title=r.get("title", ""), snippet=strip_tags(r.get("description", "")),
                          type="web") for r in (data.get("web") or {}).get("results", [])]


PROVIDERS = [WebSearch, GoogleCSE, BraveAPI, Wikipedia, OpenAlex, Crossref, SemanticScholar, Arxiv,
             EuropePMC, DOAJ, Core]


def provider_info():
    return [{"name": p.name, "label": p.label, "type": p.type, "needs_key": p.needs_key} for p in PROVIDERS]
