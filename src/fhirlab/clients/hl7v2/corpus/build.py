"""Build the paired corpus: data/corpus/{facts,golden,level_X}/... and index.parquet.

    uv run python -m fhirlab.clients.hl7v2.corpus.build
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pandas as pd

from fhirlab.clients.hl7v2.corpus.corrupt import corrupt, local_code_table
from fhirlab.clients.hl7v2.corpus.emitter import emit
from fhirlab.clients.hl7v2.corpus.golden import golden_bundle
from fhirlab.clients.hl7v2.corpus.synthea_load import load_patient

SEED = 42
LEVELS = [0.0, 0.2, 0.4, 0.6, 0.8]
SYNTHEA_DIR = Path("data/synthea/fhir")
OUT = Path("data/corpus")


def split_of(patient_id: str) -> str:
    """Patient-level split: every message of a patient lands in the same split."""
    h = int(hashlib.sha256(f"{SEED}:{patient_id}".encode()).hexdigest(), 16) % 100
    return "train" if h < 60 else "dev" if h < 80 else "test"


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False))


def main() -> None:
    files = sorted(p for p in SYNTHEA_DIR.glob("*.json") if not p.name.startswith(("hospital", "practitioner")))
    msgs = []
    for f in files:
        msgs += load_patient(f, random.Random(f"{SEED}:{f.name}"))

    loincs = {o.loinc: o.display for m in msgs if m.report for o in m.report.observations}
    local_codes = local_code_table(loincs)
    _dump(OUT / "local_codes.json", local_codes)
    ctx = {"local_codes": local_codes}

    rows = []
    for m in msgs:
        _dump(OUT / "facts" / f"{m.msg_id}.json", m.to_dict())
        _dump(OUT / "golden" / f"{m.msg_id}.json", golden_bundle(m))
        clean = emit(m)
        for level in LEVELS:
            text, manifest = corrupt(clean, level, m.msg_id, ctx)
            d = OUT / f"level_{level:.1f}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{m.msg_id}.hl7").write_text(text, encoding="utf-8")
            _dump(d / f"{m.msg_id}.manifest.json", manifest)
            rows.append(
                {
                    "msg_id": m.msg_id,
                    "patient_id": m.patient_id,
                    "split": split_of(m.patient_id),
                    "msg_type": m.msg_type,
                    "level": level,
                    "n_changes": len(manifest),
                    "corruptions": sorted({c["type"] for c in manifest} | set(m.tags)),
                }
            )
    df = pd.DataFrame(rows)
    df.to_parquet(OUT / "index.parquet")
    print(f"{len(msgs)} messages, {len(local_codes)} local codes")
    print(df[df.level == 0].groupby(["split", "msg_type"]).size().unstack())
    print(df.groupby("level").n_changes.mean().round(2))


if __name__ == "__main__":
    main()
