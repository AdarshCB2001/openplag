"""AI-estimate test with Groq mocked (incl. one 429 and one 400 on reasoning params)."""
import asyncio, json, re, sys, types; sys.path.insert(0, ".")
import httpx
from tests.fixtures import ESSAY

AI_PARA = ("In today's fast-paced digital landscape, artificial intelligence plays a pivotal role in transforming "
           "education. Moreover, it is important to note that these multifaceted tools foster collaboration and "
           "unlock the potential of learners. Furthermore, institutions must navigate the complexities of ethical "
           "adoption. In conclusion, a holistic approach is crucial to harness the power of this transformative technology.")
HUMAN_PARA = ("Last monsoon our college lab in Ponda flooded twice, so half my readings from the pond-weed experiment "
              "are basically guesses. I re-ran trial 3 on a Sunday with a borrowed lamp (60 W, not the 100 W the manual "
              "asks for) and got 14 bubbles a minute, which honestly felt too low. My guide thinks the thermometer was off.")
TEXT = ESSAY.split("References")[0] + "\n\n" + AI_PARA + "\n\n" + HUMAN_PARA + "\n\nReferences\nSmith (2020) Plants."
state = {"calls": 0, "bodies": []}

def handler(req):
    if req.url.host != "api.groq.com":
        return httpx.Response(404)
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile", "active": True},
                                                   {"id": "whisper-large-v3", "active": True},
                                                   {"id": "openai/gpt-oss-120b", "active": True}]})
    body = json.loads(req.content); state["calls"] += 1; state["bodies"].append(body)
    if state["calls"] == 1:
        return httpx.Response(429, headers={"retry-after": "0.1"}, json={})
    if "reasoning_effort" in body and state["calls"] == 2:
        return httpx.Response(400, json={"error": {"message": "reasoning_effort not supported"}})
    user = body["messages"][1]["content"]
    out = []
    for m in re.finditer(r"### Passage (\d+)\n(.+?)(?=\n\n### Passage|\Z)", user, re.S):
        t = m.group(2)
        prob = 92 if "pivotal" in t else 8 if "monsoon" in t else 45
        out.append({"id": int(m.group(1)), "ai_probability": prob, "signals": ["stock phrases" if prob > 50 else "specific personal details"]})
    content = "```json\n" + json.dumps({"passages": out, "overall_comment": "Mostly human with one generic AI-like paragraph."}) + "\n```"
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

_Real = httpx.AsyncClient
class MockClient(_Real):
    def __init__(self, *a, **k):
        k.pop("limits", None); k.pop("http2", None); k["transport"] = httpx.MockTransport(handler); super().__init__(*a, **k)
httpx.AsyncClient = MockClient

from plagcheck.pipeline import Job, run_check
from plagcheck.aicheck import list_models
from plagcheck.report import build_pdf
print("models:", asyncio.run(list_models("k")))
job = Job("a1", "ai.docx")
asyncio.run(run_check(job, TEXT, [], {"providers": [], "ai_check": True, "groq_model": "openai/gpt-oss-120b", "keys": {"groq": "k"}}))
print("\n".join(job.log)); assert job.status == "done", job.error
ai = job.result["ai"]
print({k: ai[k] for k in ("overall", "likely_ai", "mixed", "likely_human", "words", "short_text", "model")})
for p in ai["passages"]:
    print(p["id"], p["ai"], p["words"], p["burstiness"], p["ai_phrases"][:4], "|", TEXT[p["start"]:p["end"]][:60].replace("\n", " "))
assert any(p["ai"] == 92 for p in ai["passages"]) and any(p["ai"] == 8 for p in ai["passages"])
assert all("Smith (2020)" not in TEXT[p["start"]:p["end"]] for p in ai["passages"]), "references must be skipped"
assert "reasoning_effort" not in state["bodies"][-1], "should drop unsupported params after 400"
open("/tmp/ai_report.pdf", "wb").write(build_pdf(job.result, "ai.docx"))
# no key -> graceful
job2 = Job("a2", "x"); asyncio.run(run_check(job2, TEXT, [], {"providers": [], "ai_check": True, "keys": {}}))
assert job2.status == "done" and job2.result["ai"]["available"] is False
print("OK")
