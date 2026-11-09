import json
from pathlib import Path

import pytest

from fhirlab.corpus.corrupt import corrupt
from fhirlab.eval.score import score
from fhirlab.parse.parser import parse
from fhirlab.rules.mapper import map_message
from fhirlab.v2 import components, escape, fhir_to_v2_ts, unescape, v2_ts_to_fhir

CORPUS = Path("data/corpus")
needs_corpus = pytest.mark.skipif(not (CORPUS / "index.parquet").exists(), reason="run `make corpus` first")


def test_escape_roundtrip():
    s = r"a|b^c~d\e&f"
    assert unescape(escape(s)) == s


def test_ts_roundtrip():
    assert v2_ts_to_fhir(fhir_to_v2_ts("2022-04-06T06:21:39+05:30")) == "2022-04-06T06:21:39+05:30"
    with pytest.raises(ValueError):
        v2_ts_to_fhir("04/13/2026")


def _sample_ids(n=40):
    import pandas as pd

    idx = pd.read_parquet(CORPUS / "index.parquet")
    return idx[idx.level == 0].msg_id.head(n).tolist()


@needs_corpus
def test_level0_rules_equal_golden():
    for mid in _sample_ids():
        r = map_message((CORPUS / "level_0.0" / f"{mid}.hl7").read_text(), mid)
        gold = json.loads((CORPUS / "golden" / f"{mid}.json").read_text())
        assert r.clean
        assert json.dumps(r.bundle(), sort_keys=True) == json.dumps(gold, sort_keys=True)


@needs_corpus
def test_corruption_deterministic_and_nested():
    ctx = {"local_codes": json.loads((CORPUS / "local_codes.json").read_text())}
    for mid in _sample_ids(20):
        clean = (CORPUS / "level_0.0" / f"{mid}.hl7").read_text()
        a = corrupt(clean, 0.6, mid, ctx)
        assert a == corrupt(clean, 0.6, mid, ctx)
        lo = {c["type"] for c in corrupt(clean, 0.4, mid, ctx)[1]}
        hi = {c["type"] for c in a[1]}
        assert lo <= hi  # nesting: level 0.4 corruptions are a subset of 0.6


@needs_corpus
def test_manifest_accounts_for_every_changed_field():
    ctx = {"local_codes": json.loads((CORPUS / "local_codes.json").read_text())}
    for mid in _sample_ids(30):
        clean = (CORPUS / "level_0.0" / f"{mid}.hl7").read_text()
        text, manifest = corrupt(clean, 0.8, mid, ctx)
        a, b = parse(clean).segments, parse(text).segments
        changed = set()
        for sa, sb in zip(a, b):
            for n in range(1, max(len(sa.fields), len(sb.fields))):
                if sa.fields[n:n + 1] != sb.fields[n:n + 1] and (sa.fields[n:n + 1] or [""]) != (sb.fields[n:n + 1] or [""]):
                    changed.add((sa.index, n))
        logged = {(c["seg_index"], int(c["field"].split("-")[1].split(".")[0])) for c in manifest}
        assert changed <= logged


def test_score_perfect_and_empty():
    gold = json.loads(next((CORPUS / "golden").glob("*R01.json")).read_text()) if CORPUS.exists() else None
    if gold is None:
        pytest.skip("no corpus")
    s = score(gold, gold)
    assert s.get("fp_crit", 0) == 0 and s.get("fn_crit", 0) == 0 and s["tp_crit"] > 0
    empty = score(None, gold)
    assert empty.get("tp_crit", 0) == 0 and empty["fn_crit"] == s["tp_crit"]


def test_rules_flags_local_code_but_stays_valid_shape():
    msg = (
        "MSH|^~\\&|L|H|R|H|20240101120000+0000||ORU^R01^ORU_R01|X1|P|2.5.1\r"
        "PID|1||M1^^^HOSP^MR||Doe^Jane||19800101|F\r"
        "PV1|1|O|||||||||||||||||V1\r"
        "OBR|1||F1|57698-3^Lipid panel^LN|||20240101120000+0000|||||||||||||||20240101120000+0000|||F\r"
        "OBX|1|NM|LAB-GLU-11^GLUCOSE SER/PL^L||95|mg per dL|||||F|||20240101120000+0000\r"
    )
    r = map_message(msg)
    kinds = {e.kind for e in r.errors}
    assert {"LOCAL_CODE", "UNIT"} <= kinds
    obs = r.resources["obs1"]
    assert obs["code"]["coding"][0]["system"] == "urn:h2f:local-lab"
    assert "code" not in obs["valueQuantity"]  # never invents a UCUM code
    assert components("a^b\\S\\c") == ["a", "b^c"]


@needs_corpus
def test_diff_rows_reconcile_with_score():
    from fhirlab.eval.score import diff

    for mid in _sample_ids(15):
        gold = json.loads((CORPUS / "golden" / f"{mid}.json").read_text())
        text = (CORPUS / "level_0.8" / f"{mid}.hl7").read_text()
        pred = map_message(text, mid).bundle()
        s, rows = score(pred, gold), diff(pred, gold)
        for kind, crit in (("crit", True), ("cos", False)):
            n = lambda st: sum(1 for r in rows if r["status"] == st and r["critical"] == crit)  # noqa: E731
            assert n("match") == s.get(f"tp_{kind}", 0)
            assert n("wrong") + n("extra") == s.get(f"fp_{kind}", 0)
            assert n("wrong") + n("missing") == s.get(f"fn_{kind}", 0)
