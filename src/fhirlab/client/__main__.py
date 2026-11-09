"""CLI: `uv run python -m fhirlab.client <command>`. Add `--trace` to see every HTTP call."""

from __future__ import annotations

import argparse
import json
import sys

from fhirlab.client.fhir_client import OBSERVATIONS, FhirClient, FhirError


def _name(p: dict) -> str:
    n = (p.get("name") or [{}])[0]
    return f"{' '.join(n.get('given', []))} {n.get('family', '')}".strip()


def _obs_line(o: dict) -> str:
    c = o["code"]["coding"][0]
    q = o.get("valueQuantity", {})
    when = o.get("effectiveDateTime", "-")
    return f"  {when:<26} {c.get('display', c['code']):<42} {q.get('value')} {q.get('unit', '')}"


def demo(c: FhirClient) -> None:
    """One pass through every interaction, narrated. Run it twice to see idempotency."""
    step = lambda s: print(f"\n== {s}")  # noqa: E731
    c.trace = True

    step("1. create Patient (conditional on MRN: 201 first run, 200 on re-run, never a duplicate)")
    pat = c.create_patient("Rao", "Asha", "1988-04-12", "female", mrn="DEMO-001")
    pid = pat.id
    print(f"  status={pat.status} id={pid} version={pat.version}")

    step("2. add Observations (each is a separate resource pointing at the patient)")
    c.add_observation(pid, "29463-7", 61.5, effective="2026-11-01T09:00:00+05:30")
    c.add_observation(pid, "8867-4", 72, effective="2026-11-01T09:00:00+05:30")
    c.add_observation(pid, "29463-7", 60.8, effective="2026-11-05T09:00:00+05:30")

    step("3. read Patient by id")
    got = c.read("Patient", pid)
    print(f"  {_name(got.resource)}  etag={got.etag}")

    step("4. search: patient by identifier, observations by patient + code")
    c.search("Patient", {"identifier": "http://hospital.example.org/mrn|DEMO-001"})
    for o in c.patient_observations(pid, "29463-7"):
        print(_obs_line(o))

    step("5. update (PUT) with If-Match -> new version; a stale ETag is rejected: 412 per spec, 409 on HAPI")
    p = got.resource
    p["telecom"] = [{"system": "phone", "value": "+91-98765-43210"}]
    upd = c.update(p, if_match=got.etag)
    print(f"  version {got.version} -> {upd.version}")

    step("6. history + vread: the old version is still there")
    hist = c.history("Patient", pid)
    print(f"  {len(hist)} versions: " + ", ".join(e['resource']['meta']['versionId'] for e in hist))
    c.vread("Patient", pid, "1")

    step("7. $everything: patient + everything that references them")
    for r in c.everything(pid):
        print(f"  {r['resourceType']}/{r['id']}")

    step("8. a failure on purpose: invalid gender -> OperationOutcome")
    try:
        c.create({"resourceType": "Patient", "gender": "banana"})
        print("  accepted (validation is off in docker-compose; try $validate, see docs)")
    except FhirError as e:
        print(f"  {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fhirlab.client", description=__doc__)
    ap.add_argument("--base", default=None, help="FHIR base URL (default $HAPI_BASE or http://localhost:8080/fhir)")
    ap.add_argument("--trace", action="store_true", help="print each HTTP request/response")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create-patient")
    s.add_argument("--family", required=True)
    s.add_argument("--given", required=True)
    s.add_argument("--birth-date", required=True, help="YYYY-MM-DD")
    s.add_argument("--gender", required=True, choices=["male", "female", "other", "unknown"])
    s.add_argument("--mrn", help="makes the create idempotent (conditional create)")

    s = sub.add_parser("add-obs", help="add a numeric observation")
    s.add_argument("patient_id")
    s.add_argument("loinc", choices=sorted(OBSERVATIONS), help="; ".join(f"{k}={v[0]}" for k, v in OBSERVATIONS.items()))
    s.add_argument("value", type=float)
    s.add_argument("--effective", help="ISO datetime, e.g. 2026-11-05T09:00:00+05:30")

    s = sub.add_parser("get", help="read one resource: get Patient 123")
    s.add_argument("rtype")
    s.add_argument("rid")

    s = sub.add_parser("summary", help="patient details + their observations")
    s.add_argument("patient_id")

    s = sub.add_parser("search", help="search: search Patient family=Rao")
    s.add_argument("rtype")
    s.add_argument("params", nargs="*", help="key=value pairs")

    s = sub.add_parser("history", help="all versions of a resource")
    s.add_argument("rtype")
    s.add_argument("rid")

    sub.add_parser("demo", help="narrated tour of every REST interaction")
    sub.add_parser("capabilities", help="what this server supports")

    a = ap.parse_args(argv)
    c = FhirClient(a.base, trace=a.trace) if a.base else FhirClient(trace=a.trace)
    try:
        if a.cmd == "create-patient":
            r = c.create_patient(a.family, a.given, a.birth_date, a.gender, a.mrn)
            print(f"{'created' if r.status == 201 else 'already existed'}: Patient/{r.id} (HTTP {r.status})")
        elif a.cmd == "add-obs":
            r = c.add_observation(a.patient_id, a.loinc, a.value, effective=a.effective)
            print(f"created: Observation/{r.id} (HTTP {r.status})")
        elif a.cmd == "get":
            print(json.dumps(c.read(a.rtype, a.rid).resource, indent=2))
        elif a.cmd == "summary":
            p = c.read("Patient", a.patient_id).resource
            print(f"{_name(p)}  {p.get('gender')}  born {p.get('birthDate')}  (Patient/{p['id']})")
            for o in c.patient_observations(a.patient_id):
                print(_obs_line(o))
        elif a.cmd == "search":
            params = dict(kv.split("=", 1) for kv in a.params)
            for r in c.search(a.rtype, params):
                print(f"{r['resourceType']}/{r['id']}  {_name(r) if r['resourceType'] == 'Patient' else ''}")
        elif a.cmd == "history":
            for e in c.history(a.rtype, a.rid):
                print(f"v{e['resource']['meta']['versionId']}  {e['request']['method']}  {e['resource']['meta']['lastUpdated']}")
        elif a.cmd == "capabilities":
            cs = c.capabilities()
            print(f"{cs['software']['name']} {cs['software']['version']}  FHIR {cs['fhirVersion']}")
            print("resources:", ", ".join(r["type"] for r in cs["rest"][0]["resource"]))
        elif a.cmd == "demo":
            demo(c)
    except FhirError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # connection refused etc.
        print(f"error: {type(e).__name__}: {e}\nIs the server up? `make up`", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
