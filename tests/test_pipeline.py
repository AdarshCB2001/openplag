"""End-to-end run with the network mocked (APIs answer in their real response formats)."""
import asyncio, json, sys, types; sys.path.insert(0, ".")
import httpx
from tests.fixtures import ESSAY, SOURCE_A, SOURCE_B

def handler(req: httpx.Request):
    u = str(req.url); h = req.url.host
    if h.endswith("wikipedia.org"):
        if req.url.params.get("list") == "search":
            return httpx.Response(200, json={"query":{"search":[{"title":"Photosynthesis","snippet":"<span>Photosynthesis</span> is the process used by plants"}]}})
        return httpx.Response(200, json={"query":{"pages":{"1":{"extract":SOURCE_A}}}})
    if h == "api.openalex.org":
        words = "Climate change refers to long-term shifts in temperatures".split()
        return httpx.Response(200, json={"results":[{"id":"https://openalex.org/W1","doi":"https://doi.org/10.1/abc","display_name":"Climate paper",
            "abstract_inverted_index":{w:[i] for i,w in enumerate(words)},"best_oa_location":{"pdf_url":None,"landing_page_url":"https://journal.org/a"}}]})
    if h == "api.crossref.org":
        return httpx.Response(200, json={"message":{"items":[{"DOI":"10.2/xyz","title":["Fossil fuels"],"abstract":"<jats:p>"+SOURCE_B+"</jats:p>","link":[]}]}})
    if h == "api.semanticscholar.org":
        return httpx.Response(429, json={})
    if h == "export.arxiv.org":
        return httpx.Response(200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id><title>Chloroplast</title><summary>The Calvin cycle then uses the ATP and NADPH produced to fix carbon dioxide into organic molecules in the stroma.</summary><link title="pdf" href="http://arxiv.org/pdf/1"/></entry></feed>')
    if h == "www.ebi.ac.uk":
        return httpx.Response(200, json={"resultList":{"result":[]}})
    if h == "doaj.org":
        return httpx.Response(200, json={"results":[]})
    if h == "un.org":
        return httpx.Response(200, headers={"content-type":"text/html"}, text="<html><body><nav>menu</nav><article><h1>What is climate change</h1><p>"+SOURCE_B+"</p><p>"+("More filler text about policy. "*20)+"</p></article></body></html>")
    if h == "arxiv.org":
        return httpx.Response(404)
    return httpx.Response(404)

_Real = httpx.AsyncClient
class MockClient(_Real):
    def __init__(self, *a, **k):
        k.pop("limits", None); k.pop("http2", None); k["transport"] = httpx.MockTransport(handler)
        super().__init__(*a, **k)
httpx.AsyncClient = MockClient

# fake ddgs metasearch
fake = types.ModuleType("ddgs")
class DDGS:
    def __init__(self, *a, **k): pass
    def text(self, q, **k):
        return [{"href":"https://un.org/climatechange?utm_source=x","title":"What is climate change? | UN","body":"Since the 1800s, human activities have been the main driver"},
                {"href":"https://www.un.org/climatechange/","title":"dup","body":"burning of fossil fuels like coal, oil and gas"}]
fake.DDGS = DDGS; sys.modules["ddgs"] = fake

import plagcheck.fetch as F, tempfile, pathlib
F.CACHE_DIR = pathlib.Path(tempfile.mkdtemp())
from plagcheck.pipeline import Job, run_check
from plagcheck.report import build_pdf

job = Job("t1", "essay.docx")
opts = {"mode":"quick","providers":["web","wikipedia","openalex","crossref","semantic","arxiv","europepmc","doaj"],"engines":["google","bing"]}
asyncio.run(run_check(job, ESSAY, [("my_old_essay.txt","Plants are amazing organisms that sustain almost all life on our planet and that is a fact.")], opts))
print("\n".join(job.log))
assert job.status == "done", job.error
r = job.result
print("similarity", r["similarity"], r["by_type"], r["meta"])
for s in r["sources"]: print(" ", s["n"], s["pct"], s["type"], s["provider"], s["full_text"], s["url"])
assert any(s["type"]=="file" for s in r["sources"])
assert any("wikipedia" in s["url"] for s in r["sources"])
assert sum(1 for s in r["sources"] if "un.org" in s["url"]) == 1, "duplicate URLs should merge"
pdf = build_pdf(r, "essay.docx"); open("/tmp/report.pdf","wb").write(pdf); print("pdf bytes", len(pdf))
print("OK")
