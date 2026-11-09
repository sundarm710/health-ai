"""The package boundary, enforced. core is the engine; clients and console depend on it, never the reverse."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "fhirlab"

# area -> the only areas it may import (besides itself)
ALLOWED = {
    "core": set(),
    "clients.nicu": {"core"},
    "clients.hl7v2": {"core"},
    "console": {"core", "clients.nicu"},  # the console fronts the NICU client; it must not know the HL7v2 batch client
}


def _area(mod: str) -> str | None:
    p = mod.split(".")[1:]  # drop "fhirlab"
    if not p:
        return None
    return ".".join(p[:2]) if p[0] == "clients" and len(p) > 1 else p[0]


def _imports(path: Path) -> set[str]:
    found = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            found |= {a.name for a in n.names if a.name.split(".")[0] == "fhirlab"}
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] == "fhirlab":
            found |= {n.module} | {f"{n.module}.{a.name}" for a in n.names} if n.module == "fhirlab" else {n.module}
    return found


def test_dependencies_only_point_at_the_core():
    bad = []
    for f in SRC.rglob("*.py"):
        area = _area("fhirlab." + ".".join(f.relative_to(SRC).with_suffix("").parts))
        if area not in ALLOWED:
            continue
        for mod in _imports(f):
            tgt = _area(mod)
            if tgt and tgt != area and tgt not in ALLOWED[area] and tgt in ALLOWED:
                bad.append(f"{f.relative_to(SRC)} ({area}) imports {mod} ({tgt})")
    assert not bad, "boundary violations:\n" + "\n".join(bad)


def test_source_tree_has_only_core_clients_console():
    top = {p.name for p in SRC.iterdir() if p.name != "__pycache__"}
    assert top == {"__init__.py", "core", "clients", "console"}, f"unexpected top-level entries: {sorted(top)}"
    assert {p.name for p in (SRC / "clients").iterdir() if p.name not in ("__pycache__", "__init__.py")} == {"nicu", "hl7v2"}
