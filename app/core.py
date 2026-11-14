"""Shared plumbing for the app: its Lakebase project (pooled Postgres, OAuth token refresh, a small read cache), the foundation
model client and Genie. Everything authenticates as the app's service principal; no screen queries the SQL warehouse."""
import datetime as dt
import decimal
import json
import os
import threading
import time

from databricks.sdk import WorkspaceClient

SCHEMA = os.environ.get("ATLAS_SCHEMA", "atlas_electricity")
SERVING = os.environ.get("ATLAS_SERVING_SCHEMA", f"{SCHEMA}_serving")   # Postgres schema of the synced gold; first on the search path
LAKEBASE_ENDPOINT = os.environ.get("ATLAS_LAKEBASE_ENDPOINT", "projects/atlas-electricity/branches/production/endpoints/primary")
AS_OF = dt.date.fromisoformat(os.environ.get("ATLAS_AS_OF", "2026-10-31"))   # the twin's as-of day: gold dates are relative to it
GENIE_TITLE = os.environ.get("ATLAS_GENIE_TITLE", "")   # this deployment's own Genie space, built by the serving job
LLM = os.environ.get("ATLAS_LLM", "databricks-claude-haiku-5-5")
LIVE_JOB = os.environ.get("ATLAS_LIVE_JOB_ID", "")
FEEDS_JOB = os.environ.get("ATLAS_FEEDS_JOB_ID", "")

w = WorkspaceClient()


def _coerce(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if hasattr(v, "tolist"):
        return [_coerce(x) for x in v.tolist()]
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    return v


# ------------------------------------------------------------------ Lakebase (the app's own project)
_tok = {"v": None, "at": 0.0}
_tok_lock = threading.Lock()


def _token():
    """An OAuth database credential for the project's endpoint, reused for 30 minutes (it lives about an hour)."""
    with _tok_lock:
        if not _tok["v"] or time.time() - _tok["at"] > 1800:
            _tok["v"] = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": LAKEBASE_ENDPOINT})["token"]
            _tok["at"] = time.time()
        return _tok["v"]


class _PG:
    """A small pool of autocommit connections; each new connection gets a fresh token and is retired before the token expires. The
    synced gold schema leads the search path, so gold tables are named bare (gld_asset) and app tables by schema (atlas.situation)."""
    def __init__(self):
        self.pool = None
        self.lock = threading.Lock()

    def _open(self):
        import psycopg
        from psycopg_pool import ConnectionPool

        class Conn(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kw):
                return super().connect(conninfo, password=_token(), **kw)

        host = os.environ.get("PGHOST") or w.api_client.do("GET", f"/api/2.0/postgres/{LAKEBASE_ENDPOINT}")["status"]["hosts"]["host"]
        user = os.environ.get("PGUSER") or w.current_user.me().user_name
        self.pool = ConnectionPool(f"host={host} dbname={os.environ.get('PGDATABASE', 'databricks_postgres')} user={user} sslmode=require",
                                   connection_class=Conn, kwargs={"autocommit": True, "options": f"-c search_path={SERVING},public"}, min_size=1, max_size=12, max_lifetime=2400,
                                   check=ConnectionPool.check_connection, open=True)

    def q(self, sql_text, args=None, one=False):
        if self.pool is None:
            with self.lock:
                if self.pool is None:
                    self._open()
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(sql_text, args)
            if cur.description is None:
                return None
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, [_coerce(v) for v in r])) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows


pg = _PG()
_pcache, _pcache_lock = {}, threading.Lock()


def pq(sql_text, params=None, ttl=0):
    """Read rows from Lakebase, optionally cached in this process for ttl seconds (synced gold changes only when the sync runs)."""
    key = (sql_text, json.dumps(params, default=str) if params else None)
    if ttl:
        with _pcache_lock:
            hit = _pcache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
    rows = pg.q(sql_text, params)
    if ttl:
        with _pcache_lock:
            if len(_pcache) > 2000:   # bounded: drop the oldest half
                for k in sorted(_pcache, key=lambda k: _pcache[k][0])[:1000]:
                    _pcache.pop(k, None)
            _pcache[key] = (time.time(), rows)
    return rows


def feeder_outages():
    """Feeders with outages in progress, keyed by feeder: tripped when the feeder breaker or a recloser operated (the feeder, or the part
    behind the recloser, is off), otherwise an outage on a fused spur only (the feeder itself stays in service). With customers off."""
    rows = pg.q("""SELECT o.feeder_id, bool_or(a.asset_class IN ('cb', 'recloser')) AS tripped, sum(o.customers_off) AS customers_off
                   FROM atlas.live_outage o LEFT JOIN gld_asset a ON a.asset_id = o.device_id
                   WHERE o.status = 'active' GROUP BY o.feeder_id""")
    return {r["feeder_id"]: dict(tripped=bool(r["tripped"]), customers_off=int(r["customers_off"] or 0)) for r in rows}


def audit(actor, action, entity, detail=None):
    pg.q("INSERT INTO atlas.audit_log (actor, action, entity, detail) VALUES (%s,%s,%s,%s)",
         (actor, action, entity, json.dumps(detail or {}, default=str)))


# ------------------------------------------------------------------ foundation models (Databricks-served)
_llm = None


def llm_client():
    global _llm
    if _llm is None:
        _llm = w.serving_endpoints.get_open_ai_client()
    return _llm


def complete(messages, max_tokens=700):
    global _llm
    kw = dict(model=LLM, messages=messages, max_tokens=max_tokens)
    try:
        return llm_client().chat.completions.create(**kw)
    except Exception as e:  # noqa: BLE001 - the client holds a token that expires: rebuild it once and retry
        print(f"LLM call failed ({type(e).__name__}: {str(e)[:160]}); rebuilding the client", flush=True)
        _llm = None
        return llm_client().chat.completions.create(**kw)


def text_of(c):
    """Message content as plain text (some endpoints return a list of content blocks)."""
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") or "" for b in c).strip()
    return (c or "").strip()


def write(prompt, system, max_tokens=600):
    try:
        r = complete([{"role": "system", "content": system}, {"role": "user", "content": prompt}], max_tokens=max_tokens)
        return text_of(r.choices[0].message.content)
    except Exception as e:  # noqa: BLE001 - the screen still works without the generated text
        return f"Briefing unavailable: {str(e)[:120]}."


# ------------------------------------------------------------------ Genie (Genie Agents API)
_genie = {"id": "", "at": 0.0}


def genie_space():
    """The id of the Genie space titled for this deployment, looked up again at most once a minute until it exists."""
    if not _genie["id"] and time.time() - _genie["at"] > 60:
        _genie["at"] = time.time()
        try:
            _genie["id"] = next((s["space_id"] for s in w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces") or []
                                 if s.get("title") == GENIE_TITLE), "")
        except Exception:  # noqa: BLE001 - Genie is optional: the button stays hidden
            pass
    return _genie["id"]


def genie_link():
    """Where the Genie button opens the space: Genie in Databricks One, the business-user surface (not the authoring workspace)."""
    space = genie_space()
    if not space:
        return None
    host = w.config.host.rstrip("/")
    try:
        ws = w.get_workspace_id()
    except Exception:  # noqa: BLE001
        ws = None
    return f"{host}/one/agents/{space}" + (f"?o={ws}" if ws else "")


_live_run = {"at": 0, "run": None}


def live_run():
    """The live engine job's active run, if any (cached a few seconds): its state and when it started."""
    if not LIVE_JOB:
        return None
    if time.time() - _live_run["at"] > 4:
        run = None
        try:
            for r in w.jobs.list_runs(job_id=int(LIVE_JOB), active_only=True, limit=5):
                run = dict(run_id=r.run_id, state=str(r.state.life_cycle_state.value) if r.state and r.state.life_cycle_state else None,
                           start_ms=r.start_time)
                break
        except Exception:  # noqa: BLE001 - status is advisory; the button falls back to the engine heartbeat
            run = _live_run["run"]
        _live_run.update(at=time.time(), run=run)
    return _live_run["run"]


def run_job(job_id, params=None):
    return w.jobs.run_now(job_id=int(job_id), job_parameters=params or {}).run_id


def cancel_if_running(run_id):
    """Cancel a job run unless it has already ended."""
    r = w.jobs.get_run(run_id=run_id)
    if r.state and r.state.life_cycle_state and r.state.life_cycle_state.value in ("PENDING", "QUEUED", "RUNNING", "BLOCKED", "WAITING_FOR_RETRY"):
        w.jobs.cancel_run(run_id=run_id)
    _live_run.update(at=0)


def stop_feeds():
    """End the public feeds job's active runs: it polls only while the live run is on."""
    if not FEEDS_JOB:
        return
    for r in w.jobs.list_runs(job_id=int(FEEDS_JOB), active_only=True, limit=5):
        w.jobs.cancel_run(run_id=r.run_id)
