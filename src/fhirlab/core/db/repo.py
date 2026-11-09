"""Data access. Every method returns a DataFrame (or str) with a documented column set.

Safety: the connection is read-only at the session level, has a statement timeout, and the free-form SQL
box only accepts a single SELECT/WITH/EXPLAIN. In production this would be a dedicated read-replica role
with no access to PHI columns it does not need.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

DSN = os.environ.get("HAPI_DB_DSN", "postgresql://hapi:hapi@localhost:5432/hapi")
_PATIENT_NAME = (
    "coalesce(j->'name'->0->>'text', concat_ws(' ', j->'name'->0->'given'->>0, j->'name'->0->>'family'))"
)
_FREQ = {"minute", "hour", "day"}


class UnsafeSql(ValueError):
    """The free-form query box refused this statement."""


class DbRepo(Protocol):
    def patients(self) -> pd.DataFrame:
        """res_id, fhir_id, name, birth_date, resources, versions, last_updated"""

    def patient_events(self, patient_pid: int) -> pd.DataFrame:
        """One row per stored version of every resource in the patient's compartment:
        res_id, fhir_id, res_type, res_ver, res_updated (UTC), encoding, request_id, body (JSON text or None)"""

    def resource_counts(self) -> pd.DataFrame:
        """res_type, resources, deleted, versions"""

    def activity(self, freq: str) -> pd.DataFrame:
        """ts, res_type, writes   (one row per bucket x type; freq in minute|hour|day)"""

    def link_matrix(self) -> pd.DataFrame:
        """source, target, links   (reference edges between resource types)"""

    def tables(self) -> pd.DataFrame:
        """name, approx_rows, size_bytes, kind (hapi-core | search-index | terminology | other)"""

    def table_columns(self, table: str) -> pd.DataFrame:
        """column, type, nullable"""

    def table_rows(self, table: str, limit: int, offset: int) -> pd.DataFrame: ...
    def run_sql(self, query: str, limit: int = 200) -> pd.DataFrame: ...


def table_kind(name: str) -> str:
    if name in ("hfj_resource", "hfj_res_ver", "hfj_res_link", "hfj_res_tag", "hfj_history_tag", "hfj_res_ver_prov"):
        return "hapi-core"
    if name.startswith(("hfj_spidx", "hfj_idx", "hfj_res_param")):
        return "search-index"
    if name.startswith("trm_"):
        return "terminology"
    return "other"


_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|set|reset)\b", re.I)


def check_safe(query: str) -> str:
    q = query.strip().rstrip(";").strip()
    if not q:
        raise UnsafeSql("empty query")
    if ";" in q:
        raise UnsafeSql("one statement at a time")
    if not re.match(r"(select|with|explain|show)\b", q, re.I):
        raise UnsafeSql("only SELECT / WITH / EXPLAIN are allowed")
    if _FORBIDDEN.search(re.sub(r"'[^']*'", "''", q)):
        raise UnsafeSql("write/DDL keywords are not allowed")
    return q


class PgRepo:
    def __init__(self, dsn: str = DSN):
        self.dsn = dsn

    def _conn(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, options="-c default_transaction_read_only=on -c statement_timeout=5000",
                               connect_timeout=3)

    def _df(self, query, params=None) -> pd.DataFrame:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(query, params)
            return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

    # ---- patients
    def patients(self) -> pd.DataFrame:
        return self._df(f"""
            with cur as (select r.res_id, r.fhir_id, r.res_updated, v.res_text_vc::jsonb j
                         from hfj_resource r join hfj_res_ver v on v.res_id = r.res_id and v.res_ver = r.res_ver
                         where r.res_type = 'Patient' and r.res_deleted_at is null),
            comp as (select c.res_id pid, c.res_id rid from cur c
                     union select target_resource_id, src_resource_id from hfj_res_link
                           where target_resource_id in (select res_id from cur))
            select cur.res_id, cur.fhir_id, {_PATIENT_NAME} as name, j->>'birthDate' as birth_date,
                   (select count(distinct rid) from comp where pid = cur.res_id) as resources,
                   (select count(*) from hfj_res_ver v where v.res_id in (select rid from comp where pid = cur.res_id)) as versions,
                   cur.res_updated as last_updated
            from cur order by cur.res_updated desc""")

    def patient_events(self, patient_pid: int) -> pd.DataFrame:
        return self._df("""
            with comp0 as (select %(p)s::bigint as rid
                           union select src_resource_id from hfj_res_link where target_resource_id = %(p)s),
            comp as (select rid from comp0
                     union select l.src_resource_id from hfj_res_link l join comp0 c on l.target_resource_id = c.rid
                           where l.source_resource_type = 'Provenance')
            select v.res_id, r.fhir_id, v.res_type, v.res_ver, v.res_updated, v.res_encoding as encoding, v.request_id, v.res_text_vc as body
            from hfj_res_ver v join hfj_resource r on r.res_id = v.res_id
            where v.res_id in (select rid from comp)
            order by v.res_updated, v.res_id, v.res_ver""", {"p": patient_pid})

    # ---- landscape
    def resource_counts(self) -> pd.DataFrame:
        return self._df("""
            select r.res_type, count(*) filter (where r.res_deleted_at is null) as resources,
                   count(*) filter (where r.res_deleted_at is not null) as deleted,
                   (select count(*) from hfj_res_ver v where v.res_type = r.res_type) as versions
            from hfj_resource r group by r.res_type order by resources desc""")

    def activity(self, freq: str = "hour") -> pd.DataFrame:
        if freq not in _FREQ:
            raise ValueError(f"freq must be one of {sorted(_FREQ)}")
        return self._df(f"select date_trunc('{freq}', res_updated) as ts, res_type, count(*) as writes "
                        "from hfj_res_ver group by 1, 2 order by 1")

    def link_matrix(self) -> pd.DataFrame:
        return self._df("select source_resource_type as source, target_resource_type as target, count(*) as links "
                        "from hfj_res_link group by 1, 2 order by 3 desc")

    # ---- raw tables
    def tables(self) -> pd.DataFrame:
        df = self._df("select relname as name, n_live_tup as approx_rows, pg_total_relation_size(relid) as size_bytes "
                      "from pg_stat_user_tables order by n_live_tup desc, relname")
        df["kind"] = df["name"].map(table_kind)
        return df

    def _known(self, table: str) -> None:
        if table not in set(self.tables()["name"]):
            raise UnsafeSql(f"unknown table {table!r}")

    def table_columns(self, table: str) -> pd.DataFrame:
        self._known(table)
        return self._df("select column_name as \"column\", data_type as type, is_nullable = 'YES' as nullable "
                        "from information_schema.columns where table_schema = 'public' and table_name = %s order by ordinal_position", (table,))

    def table_rows(self, table: str, limit: int = 100, offset: int = 0) -> pd.DataFrame:
        self._known(table)
        q = sql.SQL("select * from {} limit {} offset {}").format(sql.Identifier(table), sql.Literal(int(limit)), sql.Literal(int(offset)))
        return self._df(q)

    def run_sql(self, query: str, limit: int = 200) -> pd.DataFrame:
        q = check_safe(query)
        with self._conn() as c, c.cursor() as cur:
            try:
                cur.execute(q)
            except psycopg.Error as e:
                raise UnsafeSql(str(e).splitlines()[0]) from None
            rows = cur.fetchmany(limit)
            return pd.DataFrame(rows, columns=[d.name for d in cur.description])
