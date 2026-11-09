"""Run a pipeline config over the corpus and write per-message results.

    uv run python -m fhirlab.clients.hl7v2.eval.run --config rules_only --splits dev test
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from fhirlab.clients.hl7v2.eval.score import score
from fhirlab.clients.hl7v2.pipeline import CONFIGS

CORPUS = Path("data/corpus")
RESULTS = Path("results")
ARTIFACT_KEYS = ("bundle", "field_errors", "issues")  # saved per message, kept out of parquet


def run_one(config: str, row: dict) -> dict:
    lvl = f"level_{row['level']:.1f}"
    text = (CORPUS / lvl / f"{row['msg_id']}.hl7").read_text(encoding="utf-8")
    gold = json.loads((CORPUS / "golden" / f"{row['msg_id']}.json").read_text())
    out = CONFIGS[config](text, row["msg_id"])
    art = RESULTS / "artifacts" / config / lvl / f"{row['msg_id']}.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({k: getattr(out, k) for k in ARTIFACT_KEYS}, ensure_ascii=False))
    rec = {k: v for k, v in asdict(out).items() if k not in ARTIFACT_KEYS}
    return {**row, "config": config, **rec, **score(out.bundle, gold)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="rules_only", choices=list(CONFIGS))
    ap.add_argument("--splits", nargs="+", default=["dev", "test"])
    ap.add_argument("--levels", nargs="+", type=float, default=None)
    ap.add_argument("--sample", type=int, default=0, help="messages per level (0 = all)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    idx = pd.read_parquet(CORPUS / "index.parquet")
    idx = idx[idx.split.isin(args.splits)]
    if args.levels:
        idx = idx[idx.level.isin(args.levels)]
    if args.sample:
        # Same message ids at every level so levels are directly comparable.
        ids = idx[idx.level == 0].msg_id.sample(n=args.sample, random_state=42)
        idx = idx[idx.msg_id.isin(set(ids))]
    rows = idx.to_dict("records")
    with ThreadPoolExecutor(args.workers) as ex:
        out = list(ex.map(lambda r: run_one(args.config, r), rows))
    df = pd.DataFrame(out).fillna(0)
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{args.config}.parquet"
    df.to_parquet(path)
    print(f"wrote {path} ({len(df)} rows)")


if __name__ == "__main__":
    main()
