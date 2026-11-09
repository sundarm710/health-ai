"""Client page: the NICU form simulator.   `make sim`   (needs `make up`, `make sim-seed`, `make sim-api`)

The UI is deliberately a thin client: it knows only the middleware's HTTP API (/login, /catalog,
/patients, /validate, /submit). It never talks to HAPI. The Trace tab shows both hops so you can see
what the form became at each step.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
import streamlit as st

from fhirlab.clients.nicu.catalog import matches

IST = ZoneInfo("Asia/Kolkata")
API = os.environ.get("SIM_API", "http://localhost:8000")
STAGES = ["authz", "field", "clinical", "state", "fhir"]
STAGE_HELP = {
    "authz": "May this signed-in person do this?", "field": "Types, required, ranges, options",
    "clinical": "Do the values make sense together?", "state": "Does it fit what the server already holds?",
    "fhir": "Does HAPI accept the generated resources?",
}


class Api:
    """Thin HTTP wrapper that records every call (the 'UI -> middleware' hop of the trace)."""

    def __init__(self, base: str = API, token: str | None = None, hops: list | None = None):
        self.base, self.token = base.rstrip("/"), token
        self.hops = hops if hops is not None else []
        self.http = httpx.Client(timeout=120)

    def call(self, method: str, path: str, **kw) -> tuple[int, dict | list]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        t0 = time.perf_counter()
        r = self.http.request(method, self.base + path, headers=headers, **kw)
        self.hops.append({"method": method, "url": self.base + path, "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000),
                          "request_id": r.headers.get("x-request-id", "")})  # look this id up on the Server page
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text}


def _claims(token: str) -> dict:
    """Decode (not verify) the token payload, only to show the user what the server will read."""
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def _api() -> Api:
    return Api(st.session_state.get("api_base", API), st.session_state.get("token"), st.session_state.setdefault("hops", []))


# --------------------------------------------------------------------------- form rendering
def is_required(f: dict, values: dict, policy: dict) -> bool:
    """Mirror of the server's rule (catalog.is_required), so the * on a label is true *right now*, not just sometimes."""
    return bool(f["required"] or (f.get("required_if") and matches(f["required_if"], values))
                or (f.get("required_if_policy") and policy.get(f["required_if_policy"])))


def _widget(op_id: str, f: dict, values: dict, doctors: list[dict], policy: dict):
    key = f"{op_id}:{f['key']}"
    req = is_required(f, values, policy)
    label = f["label"] + (f" ({f['unit']})" if f.get("unit") else "") + (" *" if req else "")
    t, default = f["type"], f.get("default")
    if t == "single":
        opts = list(f["options"])
        fmt = None
        if f.get("options_from") == "doctors":
            opts = [d["id"] for d in doctors]
            names = {d["id"]: d["name"] for d in doctors}
            fmt = lambda x: names.get(x, x)  # noqa: E731
        if f.get("other"):
            opts.append("Other")
        idx = opts.index(default) if default in opts else None
        return st.selectbox(label, opts, index=idx, key=key, format_func=fmt or str, placeholder="choose…")
    if t == "number":
        step = 1 if f.get("integer") else 0.01
        return st.number_input(label, value=default, step=step, format="%d" if f.get("integer") else "%.2f", key=key, placeholder="empty")
    if t == "date":
        return st.date_input(label, value=date.fromisoformat(default) if default else None, key=key, format="YYYY-MM-DD")
    if t == "time":
        return st.time_input(label, value=None, key=key, step=60)
    if t == "longtext":
        return st.text_area(label, value=default or "", key=key, height=80)
    return st.text_input(label, value=default or "", key=key)


def render_form(op: dict, doctors: list[dict], policy: dict) -> dict:
    """Render the fields of one operation. Fields appear/disappear live from their show_if."""
    values: dict = {}
    section = None
    for f in op["fields"]:
        if not matches(f.get("show_if"), values):
            continue
        if f.get("section") and f["section"] != section:
            section = f["section"]
            st.markdown(f"**{section}**")
        v = _widget(op["id"], f, values, doctors, policy)
        if isinstance(v, date) and not isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, dtime):
            v = v.strftime("%H:%M")
        if v not in (None, ""):
            values[f["key"]] = int(v) if f["type"] == "number" and f.get("integer") else v
        if f.get("assumed"):
            st.caption(f"ⓘ assumed default: {f['assumed']}")
    return values


# --------------------------------------------------------------------------- result panels
def _stages(resp: dict) -> None:
    cols = st.columns(len(STAGES))
    for col, s in zip(cols, STAGES, strict=True):
        status = resp.get("stages", {}).get(s, "—")
        icon = "✅" if status == "ok" else "⏭️" if status in ("skipped", "not run", "—") else "❌"
        col.metric(s, f"{icon} {status if status != 'ok' else ''}".strip(), help=STAGE_HELP[s])


def _issues(resp: dict) -> None:
    if not resp.get("issues"):
        st.success("No issues. The payload is consistent with the rules and with the server's current state.")
    for i in resp.get("issues", []):
        (st.error if i["severity"] == "error" else st.warning)(f"**{i['stage']} · {i['path']}** — {i['message']}")


def _request_tab(resp: dict) -> None:
    if not resp.get("bundle"):
        st.info("No FHIR request yet: the pipeline stops before mapping while errors remain.")
        return
    st.caption("One transaction Bundle: all entries are written, or none. `ifNoneExist` makes a retry safe; `ifMatch` guards against concurrent edits.")
    st.dataframe(resp["requests"], hide_index=True, width="stretch")
    st.markdown("**`POST {HAPI}/fhir`** with this body:")
    st.code(json.dumps(resp["bundle"], indent=2), language="json", height=380)


def _trace_tab(resp: dict, hops: list[dict]) -> None:
    st.markdown("**Hop 1 · UI → middleware** (this app calling FastAPI). `request_id` is the key to find the same call on the **Server** page")
    st.dataframe(hops, hide_index=True, width="stretch")
    st.markdown("**Hop 2 · middleware → HAPI** (what FastAPI asked HAPI while handling it)")
    st.dataframe(resp.get("trace", []), hide_index=True, width="stretch")
    st.caption("Hop 3, HAPI → Postgres, is invisible here: open Adminer (`make db-ui`) and look at `hfj_resource` / `hfj_res_ver`.")


# Results stay in view while the form scrolls: on a long form you press Validate at the bottom and still see the answer.
# The column holding `results_pane` sticks below Streamlit's header and scrolls internally if it is taller than the screen.
_STICKY = """<style>
div[data-testid="stColumn"]:has(.st-key-results_pane) {
    position: sticky; top: 3.75rem; align-self: flex-start;
    max-height: calc(100vh - 4.5rem); overflow-y: auto; padding-right: .25rem;
}
</style>"""


# --------------------------------------------------------------------------- page
def client_page() -> None:
    st.title("NICU FHIR simulation")
    st.caption("Form → middleware (parse · authorise · check · map) → FHIR transaction → HAPI. Synthetic data only.")
    ss = st.session_state
    with st.popover("Connection", icon=":material/lan:"):
        ss.api_base = st.text_input("Middleware URL", ss.get("api_base", API))
    api = _api()
    try:
        code, cat = api.call("GET", "/catalog")
        _, pracs = api.call("GET", "/practitioners")
        _, babies = api.call("GET", "/patients")
        ok = code == 200
    except httpx.HTTPError:
        ok = False
    if not ok:
        st.error(f"Cannot reach the middleware at {ss.api_base}. Run `make up`, `make sim-seed`, then `make sim-api`.")
        return
    ops, doctors = cat["operations"], cat["dynamic_options"]["doctors"]

    # ---- filters: who am I, what am I doing, to whom
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 2, 2])
        role = c1.selectbox("Role", ["Nurse", "Doctor"])
        spoof = c3.checkbox("Allow a mismatched role (tests the server's check)")
        pool = [p for p in pracs if spoof or p["role"] == role]
        who = c2.selectbox("Practitioner", pool, format_func=lambda p: f"{p['name']} ({p['role']})")
        if who and ss.get("login_key") != (role, who["id"]):
            ss.pop("last", None)
            ss.pop("result", None)
            c, r = api.call("POST", "/login", json={"practitioner_id": who["id"], "role": role})
            ss.login_key = (role, who["id"])
            ss.token, ss.login_err = (r["token"], None) if c == 200 else (None, r.get("detail"))
        if ss.get("login_err"):
            c3.error(f"Sign-in refused: {ss.login_err}")
        elif ss.get("token"):
            c3.success(f"Signed in as {who['name']} · {role}")
        labels = {k: f"{o['form']} · {o['title']}" + ("" if o["roles"].get(role) else f"  — 🚫 not permitted for {role}") for k, o in ops.items()}
        d1, d2, d3 = st.columns([3, 2, 1], vertical_alignment="bottom")
        op_id = d1.selectbox("Operation", list(ops), format_func=labels.get, key="op")
        op = ops[op_id]
        if op["needs_patient"]:
            pat = d2.selectbox("Baby (active admissions)", babies, format_func=lambda b: f"{b['name']} · id {b['id']}", index=0 if babies else None)
        else:
            pat = None
            d2.caption("No baby needed: this operation creates the patient.")
        d3.button("Refresh", icon=":material/refresh:", on_click=st.cache_data.clear, help="Reload the patient list")
        if ss.get("token"):
            with st.expander("Token (what the middleware trusts)"):
                st.caption("Signed claims. The form never sends a role; the server reads it from here.")
                st.code(json.dumps(_claims(ss.token), indent=2), language="json")
    if not ss.get("token"):
        st.warning("Choose a practitioner above to sign in.")
        return
    api.token = ss.token
    st.caption(op["purpose"])
    if op.get("flag"):
        st.info(op["flag"])

    st.html(_STICKY)
    left, right = st.columns([5, 6], gap="large")
    with left:
        st.subheader("Form")
        c1, c2 = st.columns(2)
        d = c1.date_input("Event date (IST)", value=datetime.now(IST).date(), key=f"{op_id}:evd")
        t = c2.time_input("Event time (IST)", value=datetime.now(IST).time().replace(second=0, microsecond=0), key=f"{op_id}:evt", step=60)
        event_time = datetime.combine(d, t, tzinfo=IST).isoformat()
        values = render_form(op, doctors, cat.get("unit_policy", {}))
        if f"idem:{op_id}" not in ss:
            ss[f"idem:{op_id}"] = str(uuid.uuid4())
        body = {"op": op_id, "values": values, "patient_id": pat["id"] if pat else None,
                "idempotency_key": ss[f"idem:{op_id}"], "event_time": event_time}
        sig = json.dumps(body, sort_keys=True, default=str)

        last = ss.get("last")
        valid_now = bool(last and last["resp"].get("ok") and last["sig"] == sig)
        b1, b2 = st.columns(2)
        if b1.button("Validate", type="primary", width="stretch"):
            ss.hops, ss.result = [], None
            api.hops = ss.hops
            code, resp = api.call("POST", "/validate", json=body)
            ss.last = {"resp": resp if code == 200 else {"issues": [{"severity": "error", "stage": "http", "path": str(code), "message": str(resp)}], "stages": {}}, "sig": sig}
            st.rerun()
        if b2.button("Submit", disabled=not valid_now, width="stretch", help="Enabled only when the current form passed validation unchanged."):
            ss.hops = []
            api.hops = ss.hops
            code, resp = api.call("POST", "/submit", json={**body, "validated_hash": last["resp"]["payload_hash"]})
            ss.result = {"code": code, "resp": resp}
            if resp.get("submitted"):
                ss[f"idem:{op_id}"] = str(uuid.uuid4())  # a new submission needs a new idempotency key
                ss.last = None
            st.rerun()
        if last and last["sig"] != sig:
            st.caption("✏️ The form changed since the last validation. Validate again to enable Submit.")
        st.caption(f"idempotency key `{ss[f'idem:{op_id}'][:8]}…` · retrying a submit with the same key creates nothing twice")

    with right, st.container(key="results_pane"):
        res = ss.get("result")
        resp = (res["resp"] if res else (last or {}).get("resp")) if (res or last) else None
        if not resp:
            st.info("Fill the form and press **Validate**. Nothing is written until you Submit.")
            return
        if res:
            if resp.get("submitted"):
                st.success("Submitted. All entries were written in one transaction.")
            else:
                st.error(f"Not submitted (HTTP {res['code']}): {resp.get('error') or 'validation failed'}")
        _stages(resp)
        tabs = st.tabs(["Issues", "Parsed", "FHIR request", "Trace", "Result"])
        with tabs[0]:
            _issues(resp)
        with tabs[1]:
            st.caption("What the middleware understood: typed values, then values it computed itself.")
            c1, c2 = st.columns(2)
            c1.markdown("**Parsed from the form**")
            c1.json(resp.get("parsed", {}))
            c2.markdown("**Derived by the middleware**")
            c2.json(resp.get("derived", {}))
        with tabs[2]:
            _request_tab(resp)
        with tabs[3]:
            _trace_tab(resp, ss.get("hops", []))
        with tabs[4]:
            if resp.get("results"):
                st.dataframe(resp["results"], hide_index=True, width="stretch")
                st.caption("`201 Created` = new. `200 OK` = already existed (a retry) or was updated. Look at them in Adminer (`make db-ui`).")
            else:
                st.info("Nothing written yet.")
