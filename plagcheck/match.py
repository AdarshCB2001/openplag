"""Text matching engine.

Three layers, strongest first:
  exact  - shared runs of word n-grams (shingles), merged across small gaps
  near   - whole sentences that are almost identical (light edits, word swaps)
  para   - optional semantic paraphrase detection with sentence embeddings

Every word of the checked document is attributed to at most one source
(the "primary source" model used by commercial checkers), so the overall
percentage never double-counts.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
SENT_RE = re.compile(r"[^.!?…\n]+(?:[.!?…]+[\"”’)\]]*|\n|$)")
QUOTE_RE = re.compile(r"\"[^\"\n]{3,600}\"|“[^”]{3,600}”|«[^»]{3,600}»|„[^“”]{3,600}[“”]")
REF_HEAD_RE = re.compile(
    r"^[ \t#*]*(references|bibliography|works cited|literature cited|reference list|"
    r"sources|citations|endnotes)[ \t]*:?[ \t*]*$",
    re.IGNORECASE | re.MULTILINE,
)

STOP = set(
    """a an the and or but if of to in on at for with by from as is are was were be been being this that
    these those it its into than then so such not no nor can could may might will would shall should do does
    did have has had which who whom whose what when where why how there their they them we our us you your
    he she his her him i me my mine also more most other some any each all both few many much very just only
    over under again further once here about above below between through during before after up down out off
    same own too s t one two new used use using""".split()
)

NORMAL, QUOTE, REF = 0, 1, 2
KIND_RANK = {"exact": 0, "near": 1, "para": 2}


def norm_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w.lower().replace("’", "'"))
    return "".join(c for c in w if not unicodedata.combining(c))


def tokenize(text: str):
    """Return (normalised_words, char_offsets)."""
    words, offs = [], []
    for m in WORD_RE.finditer(text):
        words.append(norm_word(m.group()))
        offs.append((m.start(), m.end()))
    return words, offs


def sentence_ranges(text: str, offs):
    """Token-index ranges [start, end) for each sentence."""
    out, ti, n = [], 0, len(offs)
    for m in SENT_RE.finditer(text):
        s, e = m.start(), m.end()
        while ti < n and offs[ti][0] < s:
            ti += 1
        start = ti
        while ti < n and offs[ti][0] < e:
            ti += 1
        if ti > start:
            out.append((start, ti))
    return out


@dataclass
class Hit:
    source: str
    start: int  # first doc token (inclusive)
    end: int  # last doc token (inclusive)
    kind: str  # exact | near | para
    src_excerpt: str
    score: float = 100.0


@dataclass
class SourceInfo:
    id: str
    title: str
    url: str
    provider: str
    type: str  # web | academic | encyclopedia | file
    full_text: bool
    chars: int = 0
    hits: list = field(default_factory=list)


class Analyzer:
    def __init__(self, text: str, n: int = 5, min_words: int = 8, gap: int = 2,
                 exclude_quotes: bool = True, exclude_refs: bool = True, near_threshold: int = 82):
        self.text = text
        self.n, self.min_words, self.gap, self.near_threshold = n, max(n, min_words), gap, near_threshold
        self.words, self.offs = tokenize(text)
        N = len(self.words)
        self.status = [NORMAL] * N

        if exclude_refs:
            heads = [m for m in REF_HEAD_RE.finditer(text) if m.start() > len(text) * 0.3]
            if heads:
                cut = heads[-1].start()
                for i, (s, _) in enumerate(self.offs):
                    if s >= cut:
                        self.status[i] = REF
        if exclude_quotes:
            spans = [(m.start(), m.end()) for m in QUOTE_RE.finditer(text)]
            if spans:
                j = 0
                for i, (s, e) in enumerate(self.offs):
                    while j < len(spans) and spans[j][1] <= s:
                        j += 1
                    if j < len(spans) and spans[j][0] <= s and e <= spans[j][1] and self.status[i] == NORMAL:
                        self.status[i] = QUOTE

        self.sentences = sentence_ranges(text, self.offs)
        self.index = defaultdict(list)
        w = self.words
        for i in range(N - n + 1):
            if all(self.status[k] == NORMAL for k in range(i, i + n)):
                self.index[tuple(w[i:i + n])].append(i)
        self.quad = {tuple(w[i:i + 4]) for i in range(N - 3)}
        self.sources: dict[str, SourceInfo] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def counted(self) -> int:
        return sum(1 for s in self.status if s != REF)

    def overlap_ratio(self, text: str) -> float:
        """Fraction of a snippet's word 4-grams that also occur in the document."""
        w, _ = tokenize(text)
        grams = [tuple(w[i:i + 4]) for i in range(len(w) - 3)]
        if not grams:
            return 0.0
        return sum(1 for g in grams if g in self.quad) / len(grams)

    @staticmethod
    def _excerpt(text, offs, a, b, pad=60):
        a, b = max(0, a), min(len(offs) - 1, b)
        if a > b:
            return ""
        s, e = offs[a][0], offs[b][1]
        ps, pe = max(0, s - pad), min(len(text), e + pad)
        pre = ("…" if ps > 0 else "") + text[ps:s]
        post = text[e:pe] + ("…" if pe < len(text) else "")
        return re.sub(r"\s+", " ", f"{pre}[[{text[s:e]}]]{post}").strip()

    # ------------------------------------------------------------------ matching
    def match_source(self, info: SourceInfo, src_text: str) -> int:
        """Find all matches between the document and one source. Returns matched word count."""
        self.sources[info.id] = info
        info.chars = len(src_text)
        sw, soffs = tokenize(src_text)
        n = self.n
        cover: dict[int, int] = {}
        for j in range(len(sw) - n + 1):
            for p in self.index.get(tuple(sw[j:j + n]), ()):
                for k in range(n):
                    cover.setdefault(p + k, j + k)

        hits: list[Hit] = []
        exact_cov = set()
        if cover:
            pos = sorted(cover)
            run = [pos[0]]
            for p in pos[1:] + [None]:
                if p is not None and p <= run[-1] + 1 + self.gap:
                    run.append(p)
                    continue
                if len(run) >= self.min_words:
                    sp = sorted(cover[x] for x in run)
                    a, b = sp[0], sp[-1]
                    if b - a > 3 * len(run) + 20:
                        a, b = cover[run[0]], cover[run[0]] + (run[-1] - run[0])
                    hits.append(Hit(info.id, run[0], run[-1], "exact",
                                    self._excerpt(src_text, soffs, a, b)))
                    exact_cov.update(range(run[0], run[-1] + 1))
                run = [p] if p is not None else []

        # near-duplicate sentences (light edits that break n-gram runs)
        if len(sw) >= self.min_words:
            s_sents = sentence_ranges(src_text, soffs)
            tri_index = defaultdict(set)
            s_strings = []
            for si, (a, b) in enumerate(s_sents):
                s_strings.append(" ".join(sw[a:b]))
                for k in range(a, b - 2):
                    tri_index[tuple(sw[k:k + 3])].add(si)
            for (a, b) in self.sentences:
                idx = [t for t in range(a, b) if self.status[t] == NORMAL]
                if len(idx) < self.min_words:
                    continue
                if sum(1 for t in idx if t in exact_cov) >= 0.6 * len(idx):
                    continue
                counts = defaultdict(int)
                for k in range(a, b - 2):
                    for si in tri_index.get(tuple(self.words[k:k + 3]), ()):
                        counts[si] += 1
                if not counts:
                    continue
                dstr = " ".join(self.words[t] for t in idx)
                best, best_si = 0.0, -1
                for si, c in sorted(counts.items(), key=lambda x: -x[1])[:4]:
                    if c < 2:
                        break
                    sstr = s_strings[si]
                    sc = fuzz.ratio(dstr, sstr)
                    if len(dstr) < len(sstr):
                        sc = max(sc, fuzz.partial_ratio(dstr, sstr) * 0.98)
                    if sc > best:
                        best, best_si = sc, si
                if best >= self.near_threshold:
                    sa, sb = s_sents[best_si]
                    hits.append(Hit(info.id, idx[0], idx[-1], "near",
                                    self._excerpt(src_text, soffs, sa, sb - 1, pad=20), round(best, 1)))

        info.hits = hits
        return sum(h.end - h.start + 1 for h in hits)

    def add_hits(self, sid: str, hits: list[Hit]):
        self.sources[sid].hits.extend(hits)

    # ------------------------------------------------------------------ results
    def attribute(self, excluded: set[str] = frozenset()):
        N = len(self.words)
        owner: list = [None] * N
        hit_ref: list = [None] * N
        active = {sid: s for sid, s in self.sources.items() if sid not in excluded and s.hits}
        size = {sid: sum(h.end - h.start + 1 for h in s.hits if h.kind == "exact") +
                0.5 * sum(h.end - h.start + 1 for h in s.hits if h.kind != "exact")
                for sid, s in active.items()}
        order = sorted(active, key=lambda x: (-size[x], not active[x].full_text, x))
        for kind in ("exact", "near", "para"):
            for sid in order:
                for h in active[sid].hits:
                    if h.kind != kind:
                        continue
                    for t in range(h.start, h.end + 1):
                        if self.status[t] == NORMAL and owner[t] is None:
                            owner[t] = sid
                            hit_ref[t] = h
        return owner, hit_ref

    def result(self, excluded: set[str] = frozenset()):
        owner, hit_ref = self.attribute(excluded)
        N = len(self.words)
        denom = max(1, self.counted)
        per = defaultdict(int)
        per_kind = defaultdict(int)
        for t in range(N):
            if owner[t] is not None:
                per[owner[t]] += 1
                per_kind[hit_ref[t].kind] += 1
        matched = sum(per.values())

        ranked = sorted(self.sources.values(), key=lambda s: (-per.get(s.id, 0), s.id))
        num = {}
        sources = []
        for s in ranked:
            raw = len({t for h in s.hits for t in range(h.start, h.end + 1) if self.status[t] == NORMAL})
            if raw == 0 and s.id not in excluded:
                continue
            num[s.id] = len(sources) + 1
            sources.append({
                "id": s.id, "n": num[s.id], "title": s.title or s.url, "url": s.url,
                "provider": s.provider, "type": s.type, "full_text": s.full_text,
                "words": per.get(s.id, 0), "pct": round(100 * per.get(s.id, 0) / denom, 1),
                "raw_pct": round(100 * raw / denom, 1), "excluded": s.id in excluded,
            })

        # highlighted segments over the original text
        segs = []
        cur = None
        pos = 0
        text = self.text

        def label(t):
            if owner[t] is not None:
                return ("m", owner[t], hit_ref[t].kind, id(hit_ref[t]))
            if self.status[t] == QUOTE:
                return ("q", None, None, None)
            if self.status[t] == REF:
                return ("r", None, None, None)
            return None

        hit_ids = {}
        hit_by_key = {id(h): h for h in hit_ref if h is not None}
        matches = []
        for t in range(N):
            lb = label(t)
            if cur and lb == cur[0]:
                cur[2] = self.offs[t][1]
                continue
            if cur:
                segs.append(cur)
            cur = [lb, self.offs[t][0], self.offs[t][1]] if lb else None
        if cur:
            segs.append(cur)

        out = []
        for lb, s, e in segs:
            if s > pos:
                out.append({"t": text[pos:s]})
            seg = {"t": text[s:e], "c": lb[0]}
            if lb[0] == "m":
                key = lb[3]
                if key not in hit_ids:
                    h = hit_by_key[key]
                    hit_ids[key] = len(matches)
                    matches.append({"i": len(matches), "source": lb[1], "n": num.get(lb[1]), "kind": lb[2],
                                    "doc": "", "src": h.src_excerpt, "score": h.score, "words": 0})
                m = matches[hit_ids[key]]
                m["doc"] = (m["doc"] + " … " if m["doc"] else "") + text[s:e]
                m["words"] += len(WORD_RE.findall(text[s:e]))
                seg.update({"s": lb[1], "n": num.get(lb[1]), "k": lb[2], "m": hit_ids[key]})
            out.append(seg)
            pos = e
        if pos < len(text):
            out.append({"t": text[pos:]})

        by_type = defaultdict(float)
        for s in sources:
            by_type[s["type"]] += s["pct"]
        return {
            "similarity": round(100 * matched / denom, 1),
            "words": N, "counted_words": denom, "matched_words": matched,
            "by_kind": {k: round(100 * v / denom, 1) for k, v in per_kind.items()},
            "by_type": {k: round(v, 1) for k, v in by_type.items()},
            "quoted_words": sum(1 for s in self.status if s == QUOTE),
            "reference_words": sum(1 for s in self.status if s == REF),
            "sources": sources, "segments": out, "matches": matches,
        }


def semantic_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


_MODEL = None


def semantic_pass(an: Analyzer, source_texts: dict[str, str], threshold: float = 0.80,
                  model_name: str = "paraphrase-multilingual-MiniLM-L12-v2", max_src_sents: int = 12000):
    """Flag doc sentences that are close in meaning to a source sentence but not lexically matched."""
    global _MODEL
    from sentence_transformers import SentenceTransformer, util

    if _MODEL is None:
        _MODEL = SentenceTransformer(model_name)
    owner, _ = an.attribute()
    doc_items = []
    for (a, b) in an.sentences:
        idx = [t for t in range(a, b) if an.status[t] == NORMAL]
        if len(idx) < an.min_words or sum(1 for t in idx if owner[t]) > 0.3 * len(idx):
            continue
        doc_items.append((idx[0], idx[-1], an.text[an.offs[idx[0]][0]:an.offs[idx[-1]][1]]))
    if not doc_items:
        return 0
    src_items = []
    for sid, txt in source_texts.items():
        _, soffs = tokenize(txt)
        for (a, b) in sentence_ranges(txt, soffs):
            if b - a >= an.min_words:
                src_items.append((sid, txt[soffs[a][0]:soffs[b - 1][1]]))
            if len(src_items) >= max_src_sents:
                break
    if not src_items:
        return 0
    de = _MODEL.encode([d[2] for d in doc_items], convert_to_tensor=True, normalize_embeddings=True)
    se = _MODEL.encode([s[1] for s in src_items], convert_to_tensor=True, normalize_embeddings=True,
                       batch_size=64)
    sims = util.cos_sim(de, se)
    found = 0
    for i, (a, b, _) in enumerate(doc_items):
        score, j = sims[i].max(dim=0)
        score, j = float(score), int(j)
        if score >= threshold:
            sid, stext = src_items[j]
            an.add_hits(sid, [Hit(sid, a, b, "para", re.sub(r"\s+", " ", stext), round(score * 100, 1))])
            found += 1
    return found
