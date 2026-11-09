"""Loaders for the review app: results table + per-message detail."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from h2f.eval.score import diff
from h2f.v2 import split_segments

CORPUS, RESULTS = Path("data/corpus"), Path("results")


def load_results() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(RESULTS.glob("*.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    for col in ("corruptions", "rule_errors"):
        df[col] = df[col].apply(lambda v: list(v) if v is not None and len(v) else [])
    return df


@dataclass
class Detail:
    msg_id: str
    level: float
    config: str
    raw: str
    clean: str
    manifest: list[dict]
    bundle: dict | None
    field_errors: list[dict]
    issues: list[dict]
    golden: dict
    diff: list[dict]


def load_detail(config: str, level: float, msg_id: str) -> Detail:
    lvl = f"level_{level:.1f}"
    art = json.loads((RESULTS / "artifacts" / config / lvl / f"{msg_id}.json").read_text())
    golden = json.loads((CORPUS / "golden" / f"{msg_id}.json").read_text())
    return Detail(
        msg_id=msg_id,
        level=level,
        config=config,
        raw=(CORPUS / lvl / f"{msg_id}.hl7").read_text(encoding="utf-8"),
        clean=(CORPUS / "level_0.0" / f"{msg_id}.hl7").read_text(encoding="utf-8"),
        manifest=json.loads((CORPUS / lvl / f"{msg_id}.manifest.json").read_text()),
        bundle=art["bundle"],
        field_errors=art["field_errors"],
        issues=art["issues"],
        golden=golden,
        diff=diff(art["bundle"], golden),
    )


def _field_no(label: str) -> int:
    # "OBX-3.3" -> 3 ; "ZPI-2" -> 2
    return int(label.split("-", 1)[1].split(".")[0])


def render_v2(text: str, manifest: list[dict], field_errors: list[dict]) -> str:
    """HTML for a v2 message: corrupted fields highlighted, rule-flagged fields underlined.
    Every value is HTML-escaped: NTE/OBX free text is untrusted input here too."""
    corrupted: dict[tuple[int, int], str] = {}
    for c in manifest:
        k = (c["seg_index"], _field_no(c["field"]))
        corrupted[k] = f"{c['type']}: {c['before']!r} → {c['after']!r}"
    flagged: dict[tuple[int, int], str] = {}
    for e in field_errors:
        for src in e["source"].split(","):
            if "-" in src:
                flagged[(e["seg_index"], _field_no(src))] = f"{e['kind']}: {e['detail']}"

    lines = []
    for i, seg in enumerate(split_segments(text)):
        start = 2 if seg[0] == "MSH" else 1
        parts = []
        for n in range(start, len(seg)):
            val = html.escape(seg[n])
            cls, tips = [], []
            if (i, n) in corrupted:
                cls.append("corrupt")
                tips.append(corrupted[(i, n)])
            if (i, n) in flagged:
                cls.append("flagged")
                tips.append(flagged[(i, n)])
            if cls and not val:
                val = "∅"
            if cls:
                val = f'<span class="{" ".join(cls)}" title="{html.escape(" | ".join(tips))}">{val}</span>'
            parts.append(val)
        seg_id = html.escape(seg[0])
        seg_cls = "seg z" if seg[0].startswith("Z") else "seg"
        lines.append(f'<span class="{seg_cls}">{seg_id}</span>|' + "|".join(parts))
    return (
        "<style>"
        ".v2{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.7;white-space:pre-wrap;"
        "word-break:break-all;padding:12px 14px;border-radius:8px;border:1px solid rgba(128,128,128,.25)}"
        ".v2 .seg{font-weight:700}.v2 .seg.z{color:#d95926}"
        ".v2 .corrupt{background:rgba(235,104,52,.28);border-radius:3px;padding:0 2px}"
        ".v2 .flagged{border-bottom:2px dashed #2a78d6}"
        "</style>"
        f'<div class="v2">{"<br>".join(lines)}</div>'
    )
