"""Resource management's live state in Lakebase: in schema rm (fed to the lakehouse) the on-hold queue, stood-down crews, assignment
overrides, field job status and live crew state; in schema app (working state, not fed) planning exceptions, published plans, crew
region moves, each crew's planned minutes, the field crews' chosen stop order and a revision counter per region.
Every write bumps its region's revision (and a global one), so each app process rebuilds its cached field days when anything
changes, from any surface."""
import json

from core import pg


CHANGED = [False]   # set by every write here, so this process re-reads its working set on the next look rather than within the second


def bump(region):
    CHANGED[0] = True
    pg.q("""INSERT INTO app.region_rev (region_id, rev) VALUES (%s, 1), ('__global__', 1)
            ON CONFLICT (region_id) DO UPDATE SET rev = app.region_rev.rev + 1""", (region,))


def revs(*regions):
    rows = pg.q("SELECT region_id, rev FROM app.region_rev WHERE region_id = ANY(%s)", (list(regions),))
    got = {r["region_id"]: r["rev"] for r in rows}
    return tuple(got.get(x, 0) for x in regions)


def _put(table, key_cols, keys, payload, region):
    cols = ", ".join(key_cols)
    pg.q(f"""INSERT INTO {table} ({cols}, payload) VALUES ({", ".join(["%s"] * len(keys))}, %s)
             ON CONFLICT ({cols}) DO UPDATE SET payload = EXCLUDED.payload""", (*keys, json.dumps(payload, default=str)))
    bump(region)


# ---------------------------------------------------------------- on-hold queue
def parked_put(region, job_id, entry):
    _put("rm.parked", ["region_id", "job_id"], (region, job_id), entry, region)


def parked_pop(region, job_id):
    pg.q("DELETE FROM rm.parked WHERE region_id = %s AND job_id = %s", (region, job_id))
    bump(region)


def parked_list(region=None):
    rows = pg.q("SELECT payload FROM rm.parked" + (" WHERE region_id = %s" if region else ""), (region,) if region else None)
    return [r["payload"] for r in rows]


# ---------------------------------------------------------------- stood-down crews (their pulled-back day is the snapshot)
def stood_down_put(region, crew_id, rec):
    _put("rm.stood_down", ["region_id", "crew_id"], (region, crew_id), rec, region)


def stood_down_get(region, crew_id):
    r = pg.q("SELECT payload FROM rm.stood_down WHERE region_id = %s AND crew_id = %s", (region, crew_id), one=True)
    return r["payload"] if r else None


def stood_down_region(region):
    return {r["crew_id"]: r["payload"] for r in pg.q("SELECT crew_id, payload FROM rm.stood_down WHERE region_id = %s", (region,))}


def stood_down_all():
    return [(r["region_id"], r["crew_id"], r["payload"]) for r in pg.q("SELECT region_id, crew_id, payload FROM rm.stood_down")]


def stood_down_del(region, crew_id):
    pg.q("DELETE FROM rm.stood_down WHERE region_id = %s AND crew_id = %s", (region, crew_id))
    bump(region)


# ---------------------------------------------------------------- assignment overrides: who owns a job after a manual move
def override_put(region, job_id, payload):
    _put("rm.assignment_override", ["region_id", "job_id"], (region, job_id), payload, region)


def override_map(region):
    return {r["job_id"]: r["payload"] for r in pg.q("SELECT job_id, payload FROM rm.assignment_override WHERE region_id = %s", (region,))}


def override_all():
    return [(r["region_id"], r["job_id"], r["payload"]) for r in pg.q("SELECT region_id, job_id, payload FROM rm.assignment_override")]


def override_del(region, job_id):
    pg.q("DELETE FROM rm.assignment_override WHERE region_id = %s AND job_id = %s", (region, job_id))
    bump(region)


# ---------------------------------------------------------------- planning exceptions and published plans
def policy_get(region):
    r = pg.q("SELECT payload FROM app.planning_policy WHERE region_id = %s", (region,), one=True)
    return r["payload"] if r else None


def policy_all():
    return {r["region_id"]: r["payload"] for r in pg.q("SELECT region_id, payload FROM app.planning_policy")}


def policy_put(region, payload):
    _put("app.planning_policy", ["region_id"], (region,), payload, region)


# ---------------------------------------------------------------- crew region moves (shared; bump the crews revision)
def crew_region_put(crew_id, payload):
    pg.q("""INSERT INTO app.crew_region (crew_id, payload) VALUES (%s, %s)
            ON CONFLICT (crew_id) DO UPDATE SET payload = EXCLUDED.payload""", (crew_id, json.dumps(payload, default=str)))
    bump("__crews__")


def crew_region_all():
    return {r["crew_id"]: r["payload"] for r in pg.q("SELECT crew_id, payload FROM app.crew_region")}


def crew_region_del(crew_id):
    pg.q("DELETE FROM app.crew_region WHERE crew_id = %s", (crew_id,))
    bump("__crews__")


# ---------------------------------------------------------------- the field crews' chosen stop order and job status
def field_seq_put(region, crew_id, order):
    _put("app.field_seq", ["region_id", "crew_id"], (region, crew_id), dict(order=list(order)), region)


def field_seq_get(region, crew_id):
    r = pg.q("SELECT payload FROM app.field_seq WHERE region_id = %s AND crew_id = %s", (region, crew_id), one=True)
    return (r["payload"] or {}).get("order") if r else None


def field_status_put(region, job_id, status):
    pg.q("""INSERT INTO rm.field_status (region_id, job_id, status, updated_at) VALUES (%s,%s,%s,now())
            ON CONFLICT (region_id, job_id) DO UPDATE SET status = EXCLUDED.status, updated_at = now()""", (region, job_id, status))
    bump(region)


def field_status_region(region):
    return {r["job_id"]: r["status"] for r in pg.q("SELECT job_id, status FROM rm.field_status WHERE region_id = %s", (region,))}


def field_status_all():
    return pg.q("SELECT region_id, job_id, status FROM rm.field_status")


def field_status_del(region, job_id):
    pg.q("DELETE FROM rm.field_status WHERE region_id = %s AND job_id = %s", (region, job_id))
    bump(region)


# ---------------------------------------------------------------- crews the live engine is driving, and each crew's planned day
def crew_live_put(crew_id, lat, lon, status, job_id):
    pg.q("""INSERT INTO rm.crew_live (crew_id, lat, lon, status, job_id, since, updated_at) VALUES (%s,%s,%s,%s,%s,now(),now())
            ON CONFLICT (crew_id) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon, status = EXCLUDED.status, job_id = EXCLUDED.job_id,
            since = now(), updated_at = now()""", (crew_id, lat, lon, status, job_id))


def crew_live_all():
    return {r["crew_id"]: r for r in pg.q("SELECT * FROM rm.crew_live WHERE updated_at > now() - interval '3 hours'")}


def crew_live_del(crew_id):
    pg.q("DELETE FROM rm.crew_live WHERE crew_id = %s", (crew_id,))


def crew_day_put(region, rows):
    """Each crew's planned minutes today, for the live detector's fatigue check."""
    pg.q("DELETE FROM app.crew_day WHERE region_id = %s", (region,))
    if rows:
        pg.q("""INSERT INTO app.crew_day (crew_id, region_id, planned_min, updated_at)
                SELECT x ->> 'crew_id', %s, (x ->> 'planned_min')::float, now() FROM jsonb_array_elements(%s::jsonb) x
                ON CONFLICT (crew_id) DO UPDATE SET region_id = EXCLUDED.region_id, planned_min = EXCLUDED.planned_min, updated_at = now()""",
             (region, json.dumps(rows)))
