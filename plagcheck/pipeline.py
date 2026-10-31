"""Orchestrates a full check: queries -> search -> rank -> fetch -> compare -> result."""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

import httpx

from .fetch import fetch_text
from .match import Analyzer, SourceInfo, semantic_available, semantic_pass
from .search import PROVIDERS, Candidate, canon, wikipedia_text

MODES = {
    "quick": {"queries": 12, "fetch": 15},
    "standard": {"queries": 30, "fetch": 40},
    "deep": {"queries": 70, "fetch": 90},
    "exhaustive": {"queries": 150, "fetch": 180},
}


@dataclass
class Job:
    id: str
    name: str
    created: float = field(default_factory=time.time)
    status: str = "running"  # running | done | error | cancelled
    stage: str = "Starting"
    progress: float = 0.0
    log: list = field(default_factory=list)
    result: dict | None = None
    error: str = ""
    cancelled: bool = False
    analyzer: Analyzer | None = None
    meta: dict = field(default_factory=dict)
    task: asyncio.Task | None = None

    def say(self, msg: str):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        self.log = self.log[-400:]

    def check(self):
        if self.cancelled:
            raise asyncio.CancelledError()


def _sid(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


async def run_check(job: Job, text: str, refs: list[tuple[str, str]], opts: dict):
    try:
        await _run(job, text, refs, opts)
    except asyncio.CancelledError:
        job.status, job.stage = "cancelled", "Cancelled"
        job.say("Check cancelled.")
    except Exception as e:  # pragma: no cover - surfaced to UI
        job.status, job.error, job.stage = "error", f"{type(e).__name__}: {e}", "Error"
        job.say(f"Error: {e}")


async def _run(job: Job, text: str, refs, opts):
    t0 = time.time()
    mode = MODES.get(opts.get("mode", "standard"), MODES["standard"])
    max_q = int(opts.get("max_queries") or mode["queries"])
    max_fetch = int(opts.get("max_fetch") or mode["fetch"])
    keys = opts.get("keys") or {}
    excluded_domains = [d.strip().lower().removeprefix("www.") for d in opts.get("exclude_domains", []) if d.strip()]

    job.stage = "Analysing document"
    an = Analyzer(text, n=int(opts.get("ngram", 5)), min_words=int(opts.get("min_words", 8)),
                  exclude_quotes=opts.get("exclude_quotes", True), exclude_refs=opts.get("exclude_refs", True))
    job.analyzer = an
    job.say(f"{len(an.words)} words, {len(an.sentences)} sentences"
            f"{', references section excluded' if an.counted < len(an.words) else ''}.")
    if len(an.words) < 15:
        raise ValueError("The text is too short to check (need at least 15 words).")

    default_providers = ["web", "wikipedia", "openalex", "crossref", "semantic", "arxiv", "europepmc", "doaj",
                         "core", "google", "brave"]
    chosen = set(opts["providers"]) if opts.get("providers") is not None else set(default_providers)
    from .search import make_queries
    queries = make_queries(an, max_q) if chosen else []
    if queries:
        job.say(f"Built {len(queries)} search queries from the text.")

    providers = []
    for P in PROVIDERS:
        if P.name not in chosen:
            continue
        p = P(keys, opts)
        if p.available():
            providers.append(p)
        elif P.needs_key:
            pass
        else:
            job.say(f"{P.label} unavailable (missing library).")
    job.meta = {"queries": len(queries), "providers": [p.label for p in providers], "mode": opts.get("mode")}

    candidates: dict[str, Candidate] = {}
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(follow_redirects=True, limits=limits, http2=False) as client:
        # ------------------------------------------------------------------ search
        if providers and queries:
            job.stage = "Searching"
            tasks = []
            for p in providers:
                b = p.budget(len(queries))
                step = len(queries) / b
                for i in range(b):
                    tasks.append((p, queries[int(i * step)]))
            job.say(f"Searching {len(providers)} source groups with {len(tasks)} requests: "
                    + ", ".join(p.label for p in providers) + ".")
            done = 0

            async def one(p, q):
                return p, await p.run(client, q)

            pending = [asyncio.create_task(one(p, q)) for p, q in tasks]
            try:
                for fut in asyncio.as_completed(pending):
                    p, res = await fut
                    done += 1
                    job.progress = 5 + 45 * done / len(pending)
                    for c in res:
                        if not c.url:
                            continue
                        key = canon(c.url)
                        host = key.lstrip("/").split("/")[0]
                        if any(host == d or host.endswith("." + d) for d in excluded_domains):
                            continue
                        ex = candidates.get(key)
                        if ex is None:
                            c.providers = {p.name}
                            c.hits = 1
                            candidates[key] = c
                        else:
                            ex.hits += 1
                            ex.providers.add(p.name)
                            if len(c.text) > len(ex.text):
                                ex.text, ex.full_text = c.text, c.full_text
                            if c.snippet and c.snippet not in ex.snippet:
                                ex.snippet = (ex.snippet + " … " + c.snippet)[:4000]
                            ex.pdf_url = ex.pdf_url or c.pdf_url
                            ex.title = ex.title or c.title
                    if done % 10 == 0:
                        job.check()
                        job.stage = f"Searching ({done}/{len(pending)}) – {len(candidates)} candidate sources"
            finally:
                for t in pending:
                    t.cancel()
            for p in providers:
                if p.errors:
                    job.say(f"{p.label}: {len(p.errors)} error type(s), e.g. {p.errors[0]}"
                            + (" – provider paused for this run." if p.disabled else ""))
            job.say(f"Found {len(candidates)} unique candidate sources.")

        # ------------------------------------------------------------------ rank
        for c in candidates.values():
            ov = an.overlap_ratio(c.snippet + " " + c.text[:20000])
            c.rank = c.hits * 1.5 + ov * 20 + (2 if c.pdf_url or c.text else 0) + (3 if c.full_text else 0)
        ranked = sorted(candidates.values(), key=lambda c: -c.rank)
        to_fetch = [c for c in ranked if not c.full_text][:max_fetch]

        # ------------------------------------------------------------------ fetch
        fetched: dict[str, str] = {}
        if to_fetch and opts.get("fetch_pages", True):
            job.stage = "Downloading sources"
            job.say(f"Downloading full text of the top {len(to_fetch)} sources…")
            sem = asyncio.Semaphore(10)

            async def grab(c: Candidate):
                async with sem:
                    txt = ""
                    try:
                        if "wikipedia.org/wiki/" in c.url:
                            txt = await wikipedia_text(client, c.url)
                        if not txt and c.pdf_url:
                            txt = await fetch_text(client, c.pdf_url)
                        if not txt:
                            txt = await fetch_text(client, c.url)
                    except Exception:
                        txt = ""
                    return c, txt

            pending = [asyncio.create_task(grab(c)) for c in to_fetch]
            n_ok = 0
            try:
                for i, fut in enumerate(asyncio.as_completed(pending), 1):
                    c, txt = await fut
                    if len(txt) > 200:
                        fetched[canon(c.url)] = txt
                        n_ok += 1
                    job.progress = 50 + 30 * i / len(pending)
                    job.stage = f"Downloading sources ({i}/{len(pending)})"
                    if i % 5 == 0:
                        job.check()
            finally:
                for t in pending:
                    t.cancel()
            job.say(f"Downloaded {n_ok} of {len(to_fetch)} sources; the rest are compared using snippets/abstracts.")

    # ---------------------------------------------------------------------- compare
    job.stage = "Comparing"
    job.check()
    source_texts: dict[str, str] = {}
    items = list(candidates.items())
    for i, (key, c) in enumerate(items, 1):
        full = fetched.get(key, "")
        txt = full if full else c.text
        txt = (txt + "\n\n" + c.snippet) if c.snippet and c.snippet[:80] not in txt else txt
        if not txt.strip():
            continue
        sid = _sid(key)
        info = SourceInfo(sid, c.title, c.url, ",".join(sorted(c.providers)), c.type, bool(full or c.full_text))
        await asyncio.to_thread(an.match_source, info, txt)
        source_texts[sid] = txt
        if i % 20 == 0:
            job.progress = 80 + 12 * i / max(1, len(items))
            job.check()

    for name, rtext in refs:
        sid = _sid("file:" + name)
        info = SourceInfo(sid, name, "", "your files", "file", True)
        await asyncio.to_thread(an.match_source, info, rtext)
        source_texts[sid] = rtext
    if refs:
        job.say(f"Compared against {len(refs)} reference file(s) you uploaded.")

    if opts.get("semantic") and semantic_available():
        job.stage = "Semantic paraphrase check"
        job.say("Running semantic paraphrase check (sentence embeddings)…")
        n = await asyncio.to_thread(semantic_pass, an, source_texts)
        job.say(f"Semantic check flagged {n} possible paraphrased sentence(s).")

    ai = None
    if opts.get("ai_check"):
        gkey = keys.get("groq")
        if not gkey:
            job.say("AI-writing estimate skipped: add a Groq API key in Advanced options.")
            ai = {"available": False, "error": "No Groq API key was provided."}
        else:
            from .aicheck import DEFAULT_MODEL, run_ai_check
            job.stage = "AI-writing estimate"
            job.progress = 93
            model = opts.get("groq_model") or DEFAULT_MODEL
            job.say(f"Estimating AI-written text with Groq ({model})…")
            try:
                ai = await run_ai_check(text, an, gkey, model, log=job.say)
                if ai.get("available"):
                    job.say(f"AI-writing estimate: {ai['overall']}% overall "
                            f"({ai['likely_ai']}% of passages look AI-written).")
                else:
                    job.say("AI-writing estimate: " + ai.get("error", "not available"))
            except Exception as e:
                ai = {"available": False, "error": f"{e}"}
                job.say(f"AI-writing estimate failed: {e}")

    job.progress = 97
    res = an.result()
    res["ai"] = ai
    res["meta"] = {**job.meta, "candidates": len(candidates), "downloaded": len(fetched),
                   "reference_files": len(refs), "seconds": round(time.time() - t0, 1)}
    job.result = res
    job.status, job.stage, job.progress = "done", "Done", 100
    job.say(f"Done in {res['meta']['seconds']} s – overall similarity {res['similarity']}% "
            f"across {len(res['sources'])} matching source(s).")
