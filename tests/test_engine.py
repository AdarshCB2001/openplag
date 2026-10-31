import sys, json; sys.path.insert(0, ".")
from tests.fixtures import ESSAY, SOURCE_A, SOURCE_B
from plagcheck.match import Analyzer, SourceInfo
an = Analyzer(ESSAY)
an.match_source(SourceInfo("a","Photosynthesis – Wikipedia","https://en.wikipedia.org/wiki/Photosynthesis","wikipedia","encyclopedia",True), SOURCE_A)
an.match_source(SourceInfo("b","What is climate change? UN","https://un.org/climatechange","web","web",True), SOURCE_B)
an.match_source(SourceInfo("c","Unrelated","https://x.com","web","web",False), "The quick brown fox jumps over the lazy dog many times every single day.")
r = an.result()
print("similarity", r["similarity"], "words", r["words"], "counted", r["counted_words"], "quoted", r["quoted_words"], "refs", r["reference_words"])
print("by_kind", r["by_kind"], "by_type", r["by_type"])
for s in r["sources"]: print(s["n"], s["id"], s["pct"], s["raw_pct"], s["title"])
for m in r["matches"]: print(m["kind"], m["n"], m["words"], m["score"], "|", m["doc"][:90], "||", m["src"][:120])
assert {s["id"] for s in r["sources"]} == {"a","b"}
kinds = {m["kind"] for m in r["matches"]}
assert "exact" in kinds and "near" in kinds, kinds
# excluding a source recalculates
r2 = an.result({"a"})
print("excluding a ->", r2["similarity"])
assert r2["similarity"] < r["similarity"]
# segments rebuild the original text exactly
assert "".join(s["t"] for s in r["segments"]) == ESSAY
print("OK")
