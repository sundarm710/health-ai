"""SQLite store for expert annotations (kept in git: the one artifact you can't regenerate)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB = Path("annotations/annotations.sqlite")

# Field-level verdict: is the automatic metric right about this discrepancy?
VERDICTS = {
    "pipeline_error": "Pipeline error (metric is right)",
    "equivalent": "Clinically equivalent (metric too strict)",
    "unrecoverable": "Unrecoverable from source (omission is correct)",
    "golden_wrong": "Golden is wrong",
}
SEVERITIES = ["none", "minor", "major", "patient-safety"]
# Message-level: was the routing decision right?
ESCALATION = {
    "should_escalate": "Needs a human",
    "safe_auto": "Safe to auto-post",
}

_SCHEMA = """
create table if not exists field_annotations (
  config text, level real, msg_id text, resource text, key text, path text, status text,
  pred text, gold text, verdict text, severity text, comment text, reviewer text, ts text,
  primary key (config, level, msg_id, key, path, pred, gold)
);
create table if not exists message_annotations (
  config text, level real, msg_id text, escalation text, comment text, reviewer text, ts text,
  primary key (config, level, msg_id)
);
"""


@contextmanager
def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(_SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_fields(config: str, level: float, msg_id: str, rows: list[dict], reviewer: str) -> int:
    """Upsert rows that carry a verdict; rows whose verdict was cleared are deleted."""
    n = 0
    with conn() as c:
        for r in rows:
            k = (config, level, msg_id, r["key"], r["path"], r.get("pred") or "", r.get("gold") or "")
            if r.get("verdict"):
                c.execute(
                    "insert or replace into field_annotations values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*k[:3], r["resource"], r["key"], r["path"], r["status"], k[5], k[6],
                     r["verdict"], r.get("severity") or "none", r.get("comment") or "", reviewer, _now()),
                )
                n += 1
            else:
                c.execute(
                    "delete from field_annotations where config=? and level=? and msg_id=? and key=? and path=? and pred=? and gold=?",
                    k,
                )
    return n


def save_message(config: str, level: float, msg_id: str, escalation: str, comment: str, reviewer: str) -> None:
    with conn() as c:
        c.execute(
            "insert or replace into message_annotations values (?,?,?,?,?,?,?)",
            (config, level, msg_id, escalation, comment, reviewer, _now()),
        )


def fields_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql("select * from field_annotations", c)


def messages_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql("select * from message_annotations", c)


def adjusted_counts(diff_rows: list[dict], annotations: pd.DataFrame) -> dict:
    """Re-score one message's diff rows under expert verdicts. Unannotated rows keep their
    automatic status. Returns tp/fp/fn for critical fields."""
    ann = {}
    for a in annotations.itertuples():
        ann[(a.key, a.path, a.pred or "", a.gold or "")] = a.verdict
    tp = fp = fn = 0
    for r in diff_rows:
        if not r["critical"]:
            continue
        v = ann.get((r["key"], r["path"], r["pred"] or "", r["gold"] or ""))
        s = r["status"]
        if s == "match" or v == "equivalent":
            tp += 1
        elif v == "golden_wrong":
            continue
        elif v == "unrecoverable":
            fp += s in ("wrong", "extra")  # guessing a value that isn't in the source is still an error
        else:
            fp += s in ("wrong", "extra")
            fn += s in ("wrong", "missing")
    return {"tp": tp, "fp": fp, "fn": fn}
