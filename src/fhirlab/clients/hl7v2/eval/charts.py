"""Summary table + README charts from results/*.parquet.

    uv run python -m fhirlab.clients.hl7v2.eval.charts
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from fhirlab.clients.hl7v2.eval.score import prf  # noqa: E402

RESULTS, DOCS = Path("results"), Path("docs/clients/hl7v2")
# Colour follows the config (fixed slot order), never its rank in a given chart.
CONFIG_ORDER = ["rules_only", "llm_only", "rules_mapper", "rules_mapper_repair", "full"]
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
COLOR = dict(zip(CONFIG_ORDER, PALETTE))
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#1f1f1e", "#6b6a64", "#e6e5df"


def summarize(g: pd.DataFrame) -> pd.Series:
    s = g.sum(numeric_only=True)
    p, r, f = prf(s.tp_crit, s.fp_crit, s.fn_crit)
    return pd.Series(
        {
            "crit_P": p,
            "crit_R": r,
            "crit_F1": f,
            "cos_F1": prf(s.tp_cos, s.fp_cos, s.fn_cos)[2],
            "valid_pct": 100 * g.valid.mean(),
            "escalated_pct": 100 * g.escalated.mean(),
            "wrong_per_msg": s.wrong_crit / len(g),
            "coverage": g.coverage.mean(),
            "cost_per_msg": g.cost_usd.mean(),
            "mean_iterations": g.iterations.mean(),
            "n": len(g),
        }
    )


def _style(ax, title: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="semibold")
    ax.set_ylabel(ylabel, color=MUTED, fontsize=9)
    ax.set_xlabel("corruption level", color=MUTED, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def load() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(RESULTS.glob("*.parquet"))]
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    df = load()
    DOCS.mkdir(exist_ok=True)
    table = df.groupby(["config", "level"]).apply(summarize, include_groups=False).round(3)
    (DOCS / "summary.md").write_text(table.to_markdown())
    print(table.to_string())

    configs = [c for c in CONFIG_ORDER if c in set(df.config)]
    panels = [("crit_F1", "Critical-field F1", "F1"), ("escalated_pct", "Escalated to human", "% of messages"),
              ("wrong_per_msg", "Wrong critical values written", "per message")]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), facecolor=SURFACE)
    for ax, (col, title, ylabel) in zip(axes, panels):
        _style(ax, title, ylabel)
        for c in configs:
            t = table.loc[c]
            ax.plot(t.index, t[col], color=COLOR[c], linewidth=2, marker="o", markersize=5, label=c)
        if col == "crit_F1":
            ax.set_ylim(0.6, 1.02)
    if len(configs) > 1:
        axes[0].legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(DOCS / "headline.png", dpi=160, facecolor=SURFACE)

    # F1 by corruption type at level 0.6, per config (horizontal bars, sorted).
    x = df[df.level == 0.6].explode("corruptions").dropna(subset=["corruptions"])
    by = x.groupby(["corruptions", "config"]).apply(summarize, include_groups=False)["crit_F1"].unstack()
    by = by.sort_values(configs[0])
    fig, ax = plt.subplots(figsize=(7, 4), facecolor=SURFACE)
    _style(ax, "Critical F1 by corruption type (level 0.6)", "")
    ax.set_xlabel("critical-field F1", color=MUTED, fontsize=9)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    h = 0.8 / len(configs)
    for k, c in enumerate(configs):
        ys = [i + k * h for i in range(len(by))]
        ax.barh(ys, by[c], height=h - 0.04, color=COLOR[c], label=c)
    ax.set_yticks([i + h * (len(configs) - 1) / 2 for i in range(len(by))], by.index, fontsize=8, color=INK)
    ax.set_xlim(0.5, 1.0)
    if len(configs) > 1:
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(DOCS / "by_corruption.png", dpi=160, facecolor=SURFACE)
    print(f"wrote {DOCS}/headline.png, {DOCS}/by_corruption.png, {DOCS}/summary.md")


if __name__ == "__main__":
    main()
