"""The app's own Lakebase project: setup, and the gold sync into it.

setup: PostGIS and pg_trgm, the app's schemas and tables (serving/lakebase_schema.sql), the live streams (generator/records.py LAND),
       REPLICA IDENTITY FULL, grants, and a change data feed per fed schema (atlas, rm, live to UC <schema>_lb_atlas, _lb_rm, _lb_live,
       made when the workspace accepts the feed).
sync:  every gold table in serving/tables.py SERVING becomes a synced table <schema>_serving.<t> (SNAPSHOT, all on one shared
       pipeline); synced tables no longer listed are removed; the pipeline runs; then grants and the PostGIS indexes are laid over them.
Every UC schema is named after the deployment's medallion schema, so deployments sharing a catalog never collide.
Usage: lakebase.py setup|sync --src <dir> --catalog <c> --schema <s> --project <lakebase project id>
"""
import argparse
import os
import re
import sys
import threading
import time

FED = ["atlas", "rm", "live"]   # Postgres schemas whose every change goes back to the lakehouse (app and media are not fed)
PG_TYPE = {"STRING": "TEXT", "TIMESTAMP": "TIMESTAMPTZ", "DOUBLE": "DOUBLE PRECISION", "BOOLEAN": "BOOLEAN", "INT": "INT", "BIGINT": "BIGINT"}


def stream_columns(spec):
    """(column, type) pairs of a live stream's canonical spec in generator/records.py, comments dropped."""
    return re.findall(r"(\w+) (STRING|TIMESTAMP|DOUBLE|BOOLEAN|INT|BIGINT)\b", re.sub(r"COMMENT '[^']*'", "", spec))


class PG:
    """A connection to the project's primary endpoint as the job's identity, with the OAuth token renewed before it expires. The synced
    gold schema leads the search path, as in the app, so gold tables are named bare."""
    def __init__(self, project, serving):
        from databricks.sdk import WorkspaceClient
        import psycopg
        self.w, self.psycopg = WorkspaceClient(), psycopg
        self.endpoint = f"projects/{project}/branches/production/endpoints/primary"
        self.host = self.w.api_client.do("GET", f"/api/2.0/postgres/{self.endpoint}")["status"]["hosts"]["host"]
        self.user = self.w.current_user.me().user_name
        self.serving, self.conn, self.born, self.lock = serving, None, 0, threading.Lock()

    def _connect(self):
        tok = self.w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": self.endpoint})["token"]
        self.conn = self.psycopg.connect(host=self.host, dbname="databricks_postgres", user=self.user, password=tok, sslmode="require",
                                         autocommit=True, options=f"-c search_path={self.serving},public")
        self.born = time.time()

    def q(self, sql, args=None, fetch=False):
        with self.lock:
            for attempt in range(2):
                try:
                    if self.conn is None or self.conn.closed or time.time() - self.born > 2400:
                        self._connect()
                    with self.conn.cursor() as c:
                        c.execute(sql, args)
                        return c.fetchall() if fetch else None
                except self.psycopg.OperationalError:
                    self.conn = None
                    if attempt:
                        raise


def live_ddl():
    """Postgres tables for the live streams, from the canonical LAND schemas; recent rows only (the live engine purges)."""
    from records import LAND
    out = ["CREATE SCHEMA IF NOT EXISTS live",
           "COMMENT ON SCHEMA live IS 'Raw live streams from the operational systems, landed here first and fed to the lakehouse bronze layer by the change data feed.'"]
    for name, (spec, comment) in LAND.items():
        cols, notes = [], []
        for col, typ, note in re.findall(r"(\w+) (STRING|TIMESTAMP|DOUBLE|BOOLEAN|INT|BIGINT)(?: COMMENT '([^']*)')?", spec):
            cols.append(f"{col} {PG_TYPE[typ]}")
            if note:
                notes.append(f"COMMENT ON COLUMN live.{name}.{col} IS '{note}'")
        out += [f"CREATE TABLE IF NOT EXISTS live.{name} (id BIGSERIAL PRIMARY KEY, {', '.join(cols)})",
                f"CREATE INDEX IF NOT EXISTS {name}_ts ON live.{name} (ts)",
                f"COMMENT ON TABLE live.{name} IS '{comment}'", *notes]
    out.append("CREATE INDEX IF NOT EXISTS scada_element_ts ON live.scada (element, ts)")
    return out


def setup(a, pg, w):
    pg.q("CREATE EXTENSION IF NOT EXISTS postgis")
    pg.q("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    sql_dir = os.path.join(a.src, "serving")   # the core schema first, then each feature's own DDL file
    for f in ["lakebase_schema.sql"] + sorted(f for f in os.listdir(sql_dir) if f.startswith("lakebase_") and f.endswith(".sql") and f != "lakebase_schema.sql"):
        pg.q(open(os.path.join(sql_dir, f)).read())
    for stmt in live_ddl():
        pg.q(stmt)
    pg.q("GRANT USAGE ON SCHEMA live TO PUBLIC; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA live TO PUBLIC; "
         "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA live TO PUBLIC")
    for sch, tbl in pg.q("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                         "AND table_schema = ANY(%s)", (FED,), fetch=True):
        pg.q(f'ALTER TABLE {sch}."{tbl}" REPLICA IDENTITY FULL')   # the feed needs whole rows for updates and deletes
    change_feeds(a, w)
    print("setup done", flush=True)


def change_feeds(a, w):
    """The Lakebase change data feed of each fed schema into <schema>_lb_<postgres schema>, a UC schema made for it only when its feed
    is set up. The workspace can refuse the feed: the preview is off, or the catalog is on Default Storage (not a supported destination).
    The app then still runs from Lakebase and the pipeline reads the missing history tables as empty, so a refusal is reported, the schema
    made for it is removed again, and setup carries on; deploy.sh repeats the warning at its end."""
    parent = f"projects/{a.project}/branches/production/databases/databricks-postgres"
    mark = f"Created by the Asset Atlas deploy ({a.project})"   # cleanup.sh removes the schemas whose comment starts with this
    try:
        have = {c["postgres_schema"] for c in w.api_client.do("GET", f"/api/2.0/postgres/{parent}/cdf-configs").get("cdf_configs", [])}
    except Exception as e:  # noqa: BLE001 - the preview is off
        print(f"warning: the Lakebase change data feed could not be set up ({e}); the app runs without the lakehouse copy of its records", flush=True)
        return
    for sch in FED:
        if sch in have:
            continue
        dest = f"{a.schema}_lb_{sch}"
        made = uc_schema(w, a.catalog, dest, f"{mark}. Change history of the Lakebase schema {sch} (one lb_<table>_history table per table).")
        try:
            w.api_client.do("POST", f"/api/2.0/postgres/{parent}/cdf-configs", query={"cdf_config_id": f"atlas_{sch}"},
                            body={"catalog": a.catalog, "schema": dest, "postgres_schema": sch})
            print("change data feed", sch, "->", f"{a.catalog}.{dest}", flush=True)
        except Exception as e:  # noqa: BLE001 - refused for this catalog, so it would be refused for every fed schema
            if made:
                w.api_client.do("DELETE", f"/api/2.1/unity-catalog/schemas/{a.catalog}.{dest}")
            print(f"warning: the Lakebase change data feed could not be set up ({e}); the app runs without the lakehouse copy of its records", flush=True)
            return


def uc_schema(w, catalog, name, comment):
    """Create the UC schema unless it exists; True when this call created it."""
    try:
        w.api_client.do("GET", f"/api/2.1/unity-catalog/schemas/{catalog}.{name}")
        return False
    except Exception:  # noqa: BLE001 - not there yet
        w.api_client.do("POST", "/api/2.1/unity-catalog/schemas", body={"catalog_name": catalog, "name": name, "comment": comment})
        return True


def synced(w, name):
    try:
        return w.api_client.do("GET", f"/api/2.0/postgres/synced_tables/{name}")
    except Exception:  # noqa: BLE001 - not created yet
        return None


def sync(a, pg, w):
    from tables import SERVING, INDEXES
    srv = pg.serving
    uc_schema(w, a.catalog, srv, "Gold tables synced into the app's Lakebase project (same schema name there); the app reads only these.")
    # drop copies of tables no longer served
    for t in w.api_client.do("GET", "/api/2.1/unity-catalog/tables", query={"catalog_name": a.catalog, "schema_name": srv}).get("tables", []):
        if t["name"] not in SERVING and synced(w, t["full_name"]):
            w.api_client.do("DELETE", f"/api/2.0/postgres/synced_tables/{t['full_name']}")
            pg.q(f'DROP TABLE IF EXISTS "{srv}"."{t["name"]}"')
            print("removed", t["full_name"], flush=True)
    pipeline = None
    for t in SERVING:
        st = synced(w, f"{a.catalog}.{srv}.{t}")
        pipeline = pipeline or (st or {}).get("status", {}).get("pipeline_id")
    created, missing = [], []
    for t, pk in SERVING.items():
        name, source = f"{a.catalog}.{srv}.{t}", f"{a.catalog}.{a.schema}.{t}"
        if synced(w, name):
            continue
        try:
            w.api_client.do("GET", f"/api/2.1/unity-catalog/tables/{source}")
        except Exception:  # noqa: BLE001 - gold not built yet: reported below, the rest still syncs
            missing.append(t)
            continue
        spec = {"source_table_full_name": source, "branch": f"projects/{a.project}/branches/production", "primary_key_columns": pk,
                "scheduling_policy": "SNAPSHOT", "postgres_database": "databricks_postgres", "create_database_objects_if_missing": True}
        if pipeline:
            spec["existing_pipeline_id"] = pipeline
        w.api_client.do("POST", "/api/2.0/postgres/synced_tables", query={"synced_table_id": name}, body={"spec": spec})
        created.append(t)
        for _ in range(60):   # the first table creates the shared pipeline; the rest join it
            pipeline = pipeline or (synced(w, name) or {}).get("status", {}).get("pipeline_id")
            if pipeline:
                break
            time.sleep(5)
    print("created", len(created), "synced tables:", ", ".join(created) or "none", "| pipeline", pipeline, flush=True)
    wait_idle(w, pipeline)
    # one run of the shared pipeline snapshots every table on it (tables that joined it load on this run too)
    w.api_client.do("POST", f"/api/2.0/pipelines/{pipeline}/updates", body={"full_refresh": False})
    time.sleep(20)
    wait_idle(w, pipeline)
    bad = [f"{t}: no gold table yet" for t in missing]
    for t in SERVING:
        state = (synced(w, f"{a.catalog}.{srv}.{t}") or {}).get("status", {}).get("detailed_state", "")
        if t not in missing and "ONLINE" not in state:
            bad.append(f"{t}: {state}")
    pg.q(f'GRANT USAGE ON SCHEMA "{srv}" TO PUBLIC; GRANT SELECT ON ALL TABLES IN SCHEMA "{srv}" TO PUBLIC')
    for stmt in INDEXES:
        table = re.search(r" ON (\w+)", stmt).group(1)
        if pg.q("SELECT to_regclass(%s)", (f"{srv}.{table}",), fetch=True)[0][0]:   # tables still missing are reported below
            pg.q(stmt)
    pg.q("ANALYZE")
    if bad:
        raise RuntimeError("synced tables not online: " + "; ".join(bad))
    print("sync done:", len(SERVING), "tables online", flush=True)


def wait_idle(w, pipeline, timeout=3600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ups = w.api_client.do("GET", f"/api/2.0/pipelines/{pipeline}/updates", query={"max_results": 1}).get("updates", [])
        if not ups or ups[0].get("state") in ("COMPLETED", "FAILED", "CANCELED"):
            if ups and ups[0].get("state") == "FAILED":
                raise RuntimeError(f"sync pipeline {pipeline} update failed")
            return
        time.sleep(15)
    raise TimeoutError("sync pipeline still running")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["setup", "sync"])
    ap.add_argument("--src", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--project", required=True)
    a, _ = ap.parse_known_args()
    sys.path[:0] = [os.path.join(a.src, "generator"), os.path.join(a.src, "serving")]
    pg = PG(a.project, f"{a.schema}_serving")
    (setup if a.action == "setup" else sync)(a, pg, pg.w)


if __name__ == "__main__":
    main()
