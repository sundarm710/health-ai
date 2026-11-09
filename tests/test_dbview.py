"""DB view layer: pure service tests on a fake repo, SQL-guard tests, and live checks if Postgres is up."""

import json

import pandas as pd
import pytest

from h2f.dbview import PgRepo, UnsafeSql, service
from h2f.dbview.repo import check_safe, table_kind


def _obs(v):
    return json.dumps({"resourceType": "Observation", "code": {"text": "Body weight"}, "valueQuantity": {"value": v, "unit": "kg"}})


class FakeRepo:
    def patient_events(self, pid):
        t = pd.Timestamp("2026-11-01T10:00:00")
        return pd.DataFrame([
            {"res_id": 1, "fhir_id": "1", "res_type": "Patient", "res_ver": 1, "res_updated": t, "encoding": "JSON", "request_id": None,
             "body": json.dumps({"resourceType": "Patient", "name": [{"given": ["Baby"], "family": "A"}]})},
            {"res_id": 2, "fhir_id": "2", "res_type": "Observation", "res_ver": 1, "res_updated": t + pd.Timedelta("1h"), "encoding": "JSON", "request_id": "abc", "body": _obs(1.4)},
            {"res_id": 2, "fhir_id": "2", "res_type": "Observation", "res_ver": 2, "res_updated": t + pd.Timedelta("2h"), "encoding": "JSON", "request_id": "abd", "body": _obs(1.5)},
            {"res_id": 2, "fhir_id": "2", "res_type": "Observation", "res_ver": 3, "res_updated": t + pd.Timedelta("3h"), "encoding": "DEL", "request_id": None, "body": None},
        ])


def test_timeline_kinds_summaries_and_bodies():
    tl = service.patient_timeline(FakeRepo(), 1)
    assert list(tl["kind"]) == ["created", "created", "updated", "deleted"]
    assert list(tl["summary"]) == ["Baby A", "Body weight 1.4 kg", "Body weight 1.5 kg", "(deleted)"]
    assert tl["when"].dt.tz is not None and (tl["request_id"] == "").sum() == 2
    assert tl.attrs["bodies"]["2:2"] == _obs(1.5)


def test_diff_shows_only_what_changed():
    d = service.diff_versions(_obs(1.4), _obs(1.5))
    assert d.to_dict("records") == [{"path": "valueQuantity.value", "before": 1.4, "after": 1.5, "change": "changed"}]
    assert service.diff_versions(None, _obs(1.4))["change"].eq("added").all()


def test_empty_timeline_is_well_formed():
    class Empty:
        def patient_events(self, pid):
            return pd.DataFrame()

    assert service.patient_timeline(Empty(), 1).empty


@pytest.mark.parametrize("q", ["delete from hfj_resource", "select 1; drop table x", "update t set a=1", "", "insert into t values (1)",
                               "with x as (select 1) delete from t", "select * from t; select 2", "vacuum"])
def test_sql_guard_refuses_writes(q):
    with pytest.raises(UnsafeSql):
        check_safe(q)


@pytest.mark.parametrize("q", ["select 1", "SELECT * FROM hfj_resource", "with x as (select 1) select * from x", "explain select 1",
                               "select 'delete me' as s"])
def test_sql_guard_allows_reads(q):
    assert check_safe(q)


def test_table_kind():
    assert [table_kind(n) for n in ("hfj_resource", "hfj_spidx_token", "trm_concept", "mpi_link")] == ["hapi-core", "search-index", "terminology", "other"]


def _live() -> bool:
    try:
        PgRepo().tables()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _live(), reason="Postgres not running (make up)")
class TestLive:
    repo = PgRepo()

    def test_views_have_the_documented_columns(self):
        r = self.repo
        assert {"res_id", "fhir_id", "name", "resources", "versions", "last_updated"} <= set(r.patients().columns)
        assert {"res_type", "resources", "deleted", "versions"} == set(r.resource_counts().columns)
        assert {"ts", "res_type", "writes"} == set(r.activity("hour").columns)
        assert {"source", "target", "links"} == set(r.link_matrix().columns)
        assert "hfj_resource" in set(r.tables()["name"])
        assert len(r.table_columns("hfj_resource")) > 5

    def test_timeline_for_every_patient_is_consistent(self):
        for pid in self.repo.patients()["res_id"].head(5):
            tl = service.patient_timeline(self.repo, int(pid))
            assert not tl.empty and (tl["res_type"] == "Patient").any()
            assert tl["when"].is_monotonic_increasing

    def test_connection_is_read_only_and_unknown_tables_refused(self):
        with pytest.raises(UnsafeSql):
            self.repo.table_rows("pg_user", 1, 0)
        with pytest.raises(UnsafeSql):
            self.repo.run_sql("select * from nope_not_a_table")
        with self.repo._conn() as c, pytest.raises(Exception, match="read-only"):
            c.execute("create table zz(a int)")

    def test_free_sql_runs_and_is_row_limited(self):
        assert len(self.repo.run_sql("select generate_series(1, 1000) as n", limit=50)) == 50
