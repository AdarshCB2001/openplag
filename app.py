"""OpenPlag – self-hosted plagiarism checker.

Run:  python app.py            (then open http://127.0.0.1:8000)
Env:  PORT, HOST, GOOGLE_CSE_KEY, GOOGLE_CSE_CX, BRAVE_API_KEY, CORE_API_KEY, S2_API_KEY, CONTACT_EMAIL
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from plagcheck.extract import file_to_text
from plagcheck.match import semantic_available
from plagcheck.pipeline import MODES, Job, run_check
from plagcheck.report import PALETTE, build_pdf
from plagcheck.search import WebSearch, provider_info

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 25 * 1024 * 1024
JOBS: dict[str, Job] = {}

ENV_KEYS = {
    "google_key": os.environ.get("GOOGLE_CSE_KEY", ""), "google_cx": os.environ.get("GOOGLE_CSE_CX", ""),
    "brave": os.environ.get("BRAVE_API_KEY", ""), "core": os.environ.get("CORE_API_KEY", ""),
    "s2": os.environ.get("S2_API_KEY", ""), "email": os.environ.get("CONTACT_EMAIL", ""),
    "groq": os.environ.get("GROQ_API_KEY", ""),
}

app = FastAPI(title="OpenPlag")


def _prune():
    now = time.time()
    for jid in [j for j, job in JOBS.items() if now - job.created > 6 * 3600]:
        JOBS.pop(jid, None)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/info")
def info():
    return {
        "providers": provider_info(), "engines": WebSearch.ENGINES, "modes": MODES,
        "semantic": semantic_available(), "palette": PALETTE,
        "env_keys": [k for k, v in ENV_KEYS.items() if v],
    }


async def _read(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{f.filename} is larger than 25 MB.")
    return data


@app.post("/api/check")
async def start_check(text: str = Form(""), options: str = Form("{}"), file: UploadFile | None = File(None),
                      refs: list[UploadFile] | None = File(None)):
    _prune()
    try:
        opts = json.loads(options or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Bad options JSON")
    name = "Pasted text"
    if file is not None and file.filename:
        try:
            text = file_to_text(file.filename, await _read(file))
        except ValueError as e:
            raise HTTPException(400, str(e))
        name = file.filename
    text = (text or "").replace("\r\n", "\n").strip()
    if len(re.findall(r"\w+", text)) < 15:
        raise HTTPException(400, "Please provide at least 15 words of text (or a readable file).")
    ref_texts = []
    for rf in refs or []:
        if rf.filename:
            try:
                rt = file_to_text(rf.filename, await _read(rf))
                if rt.strip():
                    ref_texts.append((rf.filename, rt))
            except ValueError:
                pass
    keys = {k: v for k, v in ENV_KEYS.items() if v}
    keys.update({k: v for k, v in (opts.get("keys") or {}).items() if v})
    opts["keys"] = keys
    job = Job(id=uuid.uuid4().hex[:12], name=opts.get("title") or name)
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_check(job, text, ref_texts, opts))
    return {"id": job.id, "name": job.name, "words": len(re.findall(r"\w+", text))}


def _job(jid) -> Job:
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Check not found (it may have expired).")
    return job


@app.post("/api/groq/models")
async def groq_models(body: dict):
    from plagcheck.aicheck import FALLBACK_MODELS, list_models
    key = (body or {}).get("key") or ENV_KEYS.get("groq")
    if not key:
        return {"models": FALLBACK_MODELS, "live": False}
    try:
        return {"models": await list_models(key) or FALLBACK_MODELS, "live": True}
    except Exception as e:
        return {"models": FALLBACK_MODELS, "live": False, "error": str(e)[:200]}


@app.get("/api/jobs/{jid}")
def job_status(jid: str, since: int = 0):
    job = _job(jid)
    return {"id": job.id, "name": job.name, "status": job.status, "stage": job.stage,
            "progress": round(job.progress, 1), "log": job.log[since:], "log_len": len(job.log),
            "error": job.error, "result": job.result if job.status == "done" else None}


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    job = _job(jid)
    job.cancelled = True
    if job.task and not job.task.done():
        job.task.cancel()
    return {"ok": True}


@app.post("/api/jobs/{jid}/exclude")
async def exclude(jid: str, body: dict):
    job = _job(jid)
    if not job.analyzer or job.status != "done":
        raise HTTPException(409, "Check is not finished yet.")
    excluded = set(body.get("exclude") or [])
    res = job.analyzer.result(excluded)
    res["meta"] = (job.result or {}).get("meta", {})
    res["excluded"] = sorted(excluded)
    res["ai"] = (job.result or {}).get("ai")
    job.result = res
    return res


@app.get("/api/jobs/{jid}/report.pdf")
async def report(jid: str):
    job = _job(jid)
    if not job.result:
        raise HTTPException(409, "Check is not finished yet.")
    pdf = await asyncio.to_thread(build_pdf, job.result, job.name)
    fname = re.sub(r"[^\w.-]+", "_", Path(job.name).stem)[:60] or "document"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="similarity-report-{fname}.pdf"'})


@app.exception_handler(HTTPException)
async def http_err(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    import webbrowser

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if host in ("127.0.0.1", "localhost") and not os.environ.get("NO_BROWSER"):
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  OpenPlag is running at http://127.0.0.1:{port}  (Ctrl+C to stop)\n")
    uvicorn.run(app, host=host, port=port)
