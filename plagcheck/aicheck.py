"""AI-writing estimate using a Groq-hosted LLM as a judge, plus local style signals.

This is an *estimate*, not proof: LLM judges and every other AI detector produce false positives,
especially on formal academic prose and on writing by non-native English speakers.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics

import httpx

from .match import REF, SENT_RE, WORD_RE

GROQ_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
NON_CHAT = re.compile(r"whisper|tts|guard|playai|orpheus|distil|embed|vision-only", re.I)

# Phrases over-represented in LLM output. A hit is a weak signal on its own.
AI_PHRASES = [
    "delve", "delves", "delving", "tapestry", "in today's fast-paced", "in today's digital", "ever-evolving",
    "ever-changing", "it is important to note", "it's important to note", "it is worth noting", "plays a crucial role",
    "plays a pivotal role", "pivotal", "multifaceted", "underscore", "underscores", "navigate the complexities",
    "navigating the complexities", "in the realm of", "realm", "a testament to", "testament to", "seamless",
    "seamlessly", "holistic", "foster", "fostering", "leverage", "leveraging", "landscape", "paradigm shift",
    "intricate", "intricacies", "nuanced", "showcase", "showcasing", "embark", "unlock the potential",
    "harness the power", "a myriad of", "myriad", "in conclusion", "furthermore", "moreover", "additionally",
    "crucial", "vital role", "robust", "comprehensive overview", "shed light on", "sheds light on",
    "not only", "but also", "game-changer", "revolutionize", "cutting-edge", "transformative",
]
_PHRASE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in sorted(AI_PHRASES, key=len, reverse=True)) + r")\b",
                        re.I)

SYSTEM = """You are a careful forensic linguist who estimates whether passages were written by an AI language model \
(ChatGPT, Claude, Gemini, Llama, etc.) or by a human.

For EACH passage, estimate ai_probability from 0 to 100 and list up to 4 short, concrete signals you relied on.
Signals that suggest AI: uniform sentence length and rhythm; generic, abstract statements without specific \
details, numbers, names or personal experience; formulaic transitions (Moreover, Furthermore, In conclusion); \
stock phrases (delve, pivotal, tapestry, landscape, multifaceted, underscore); tidy lists of three; even-handed \
"on the one hand" hedging; summarising restatements; flawless but bland grammar.
Signals that suggest a human: specific anecdotes, local or personal details, concrete data with sources, \
uneven rhythm, idiosyncratic word choice, minor typos or informal asides, genuine argument or opinion.

Calibration rules:
- Formal academic style alone is NOT evidence of AI. Non-native English or simple grammar is NOT evidence of AI.
- Use 40-60 when signals are mixed or the passage is too short to judge. Only go above 85 when several strong \
signals agree.
- Judge each passage independently.

Reply with JSON only, exactly in this shape:
{"passages":[{"id":1,"ai_probability":0,"signals":["..."]}],"overall_comment":"one or two sentences"}"""


# --------------------------------------------------------------------------- chunking
def make_chunks(text: str, status: list[int], offs: list[tuple[int, int]], min_words=40, max_words=280):
    """Split the document into passages (char ranges), skipping the references section."""
    cut = len(text)
    for i, s in enumerate(status):
        if s == REF:
            cut = offs[i][0]
            break
    body = text[:cut]
    paras = [(m.start(), m.end()) for m in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", body)]
    units = []  # (start, end) of paragraphs split to max_words at sentence boundaries
    for s, e in paras:
        seg = body[s:e]
        if len(WORD_RE.findall(seg)) <= max_words:
            units.append((s, e))
            continue
        cur_s, count = None, 0
        for m in SENT_RE.finditer(seg):
            n = len(WORD_RE.findall(m.group()))
            if cur_s is None:
                cur_s = s + m.start()
            count += n
            if count >= max_words * 0.8:
                units.append((cur_s, s + m.end()))
                cur_s, count = None, 0
        if cur_s is not None:
            units.append((cur_s, e))
    chunks, cur = [], None
    for s, e in units:
        if cur is None:
            cur = [s, e]
        else:
            cur[1] = e
        if len(WORD_RE.findall(text[cur[0]:cur[1]])) >= min_words:
            chunks.append(tuple(cur))
            cur = None
    if cur is not None:
        if chunks and len(WORD_RE.findall(text[cur[0]:cur[1]])) < min_words:
            chunks[-1] = (chunks[-1][0], cur[1])
        else:
            chunks.append(tuple(cur))
    out = []
    for s, e in chunks:
        seg = text[s:e]
        # trim whitespace from range
        ls = len(seg) - len(seg.lstrip())
        rs = len(seg.rstrip())
        out.append((s + ls, s + rs))
    return [c for c in out if len(WORD_RE.findall(text[c[0]:c[1]])) >= 20]


def style_signals(seg: str) -> dict:
    sents = [m.group().strip() for m in SENT_RE.finditer(seg) if len(WORD_RE.findall(m.group())) >= 3]
    lens = [len(WORD_RE.findall(s)) for s in sents]
    words = [w.lower() for w in WORD_RE.findall(seg)]
    burst = round(statistics.pstdev(lens) / statistics.mean(lens), 2) if len(lens) >= 3 else None
    phrases = sorted({m.group().lower() for m in _PHRASE_RE.finditer(seg)})
    return {"sentences": len(sents), "avg_sentence_len": round(statistics.mean(lens), 1) if lens else 0,
            "burstiness": burst, "ttr": round(len(set(words)) / len(words), 2) if words else 0,
            "ai_phrases": phrases}


# --------------------------------------------------------------------------- Groq
async def list_models(key: str) -> list[str]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{GROQ_URL}/models", headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True) and not NON_CHAT.search(m["id"])]
    return sorted(ids, key=lambda x: (x not in FALLBACK_MODELS, FALLBACK_MODELS.index(x) if x in FALLBACK_MODELS
                                      else 0, x))


def _extras(model: str) -> dict:
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}
    if "qwen3" in m or "deepseek" in m or "r1" in m:
        return {"reasoning_format": "hidden"}
    return {}


def _parse_json(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group())
        raise


async def _ask(client: httpx.AsyncClient, key: str, model: str, passages: list[tuple[int, str]], log) -> dict:
    user = "Estimate each passage.\n\n" + "\n\n".join(f"### Passage {i}\n{t}" for i, t in passages)
    body = {"model": model, "temperature": 0, "max_completion_tokens": 300 + 120 * len(passages),
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            **_extras(model)}
    headers = {"Authorization": f"Bearer {key}"}
    for attempt in range(6):
        r = await client.post(f"{GROQ_URL}/chat/completions", json=body, headers=headers, timeout=90)
        if r.status_code == 429:
            wait = float(r.headers.get("retry-after", "0") or 0) or (8 * (attempt + 1))
            log(f"Groq rate limit reached – waiting {min(wait, 60):.0f} s…")
            await asyncio.sleep(min(wait, 60) + 0.5)
            continue
        if r.status_code == 400 and attempt < 3:
            err = r.text[:300]
            if any(k in body for k in ("reasoning_effort", "reasoning_format")):
                body.pop("reasoning_effort", None)
                body.pop("reasoning_format", None)
                continue
            if "json" in err.lower():
                body.pop("response_format", None)
                continue
        if r.status_code == 401:
            raise RuntimeError("Groq rejected the API key (401). Check the key in Advanced options.")
        if r.status_code == 404:
            raise RuntimeError(f"Groq model '{model}' was not found. Pick another model.")
        r.raise_for_status()
        return _parse_json(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError("Groq kept rate-limiting the request. Try again in a minute or use a smaller model.")


async def run_ai_check(text: str, an, key: str, model: str = DEFAULT_MODEL, log=print, batch_words: int = 900):
    chunks = make_chunks(text, an.status, an.offs)
    total_words = sum(len(WORD_RE.findall(text[s:e])) for s, e in chunks)
    passages = [{"id": i + 1, "start": s, "end": e, "words": len(WORD_RE.findall(text[s:e])),
                 **style_signals(text[s:e])} for i, (s, e) in enumerate(chunks)]
    if not passages:
        return {"available": False, "error": "Not enough continuous prose to estimate (need paragraphs of 20+ words)."}

    batches, cur, cw = [], [], 0
    for p in passages:
        if cur and cw + p["words"] > batch_words:
            batches.append(cur)
            cur, cw = [], 0
        cur.append(p)
        cw += p["words"]
    if cur:
        batches.append(cur)

    comments = []
    async with httpx.AsyncClient() as client:
        for bi, batch in enumerate(batches, 1):
            log(f"AI-writing estimate: batch {bi}/{len(batches)} ({sum(p['words'] for p in batch)} words)…")
            data = await _ask(client, key, model, [(p["id"], text[p["start"]:p["end"]]) for p in batch], log)
            got = {}
            for item in data.get("passages", []) or []:
                try:
                    got[int(item.get("id"))] = item
                except (TypeError, ValueError):
                    continue
            for p in batch:
                item = got.get(p["id"], {})
                try:
                    prob = float(item.get("ai_probability"))
                except (TypeError, ValueError):
                    prob = None
                p["ai"] = None if prob is None else max(0.0, min(100.0, prob))
                p["signals"] = [str(s)[:160] for s in (item.get("signals") or [])][:4]
            if data.get("overall_comment"):
                comments.append(str(data["overall_comment"])[:400])

    scored = [p for p in passages if p.get("ai") is not None]
    if not scored:
        return {"available": False, "error": "The model did not return usable scores. Try another Groq model."}
    sw = sum(p["words"] for p in scored)
    overall = sum(p["ai"] * p["words"] for p in scored) / sw
    share = lambda lo, hi: round(100 * sum(p["words"] for p in scored if lo <= p["ai"] < hi) / sw, 1)
    return {
        "available": True, "model": model, "overall": round(overall, 1),
        "likely_ai": share(70, 101), "mixed": share(40, 70), "likely_human": share(0, 40),
        "words": total_words, "short_text": total_words < 300,
        "comment": " ".join(comments[:2]),
        "passages": passages,
    }
