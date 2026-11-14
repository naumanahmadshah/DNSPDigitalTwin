"""The resource management working set: regions, crews, skills, tickets, leave, training, open safety incidents, work-hours rules,
today's jobs and feeders, read once from Lakebase and held in memory so solving is instant. The reference data and the
planned work orders are synced gold (schema serving, dated at the twin's as-of day and moved to today here); jobs raised at run time
(an outage, a situation, the asset panel) are rm.raised_job and bump the '__jobs__' revision; crew region moves bump '__crews__';
either one, or a new day in Sydney, refreshes what is held here."""
import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

from core import AS_OF, pg
from . import store
from .solver import RosterEngine

SYD = ZoneInfo("Australia/Sydney")
STATE = {}
_lock = threading.RLock()


def today():
    return dt.datetime.now(SYD).date()


def now_min():
    t = dt.datetime.now(SYD)
    return t.hour * 60 + t.minute + t.second / 60


JOB_COLS = ("job_id, job_type, title, priority, duration_min, window_start_min, window_end_min, region_id, depot_id, lat, lon, required_skill, "
            "required_ticket, funding, feeder_id, asset_id, nmi, address, suburb, customers, est_customer_minutes, source")


def _load():
    d = today()
    shift = (d - AS_OF).days   # gold is dated at the as-of day; the working set is today
    data = dict(day=d.isoformat())
    data["regions"] = pg.q("SELECT region_id, name, area, area_name, depot_id, depot_name, lat, lon FROM gld_rm_region ORDER BY region_id")
    data["crews"] = pg.q("""SELECT crew_id, crew_name, persona, primary_skill, region_id, depot_id, area, employment_type, hourly_cost_aud,
                                   home_lat, home_lon, crew_size AS size, vehicle_id, vehicle_type_label, business_unit
                            FROM gld_crew WHERE active ORDER BY crew_id""")
    data["skills"] = pg.q("SELECT crew_id, skill_code FROM gld_crew_skill")
    data["certs"] = pg.q("""SELECT crew_id, ticket_code, issued_date + %(s)s AS issued_date, expiry_date + %(s)s AS expiry_date, days_to_expiry,
                                   is_expired FROM gld_certification ORDER BY crew_id, ticket_code""", {"s": shift})
    data["leave"] = pg.q("SELECT crew_id, leave_type, status FROM gld_crew_leave WHERE leave_date = %s", (AS_OF,))
    data["training"] = pg.q("SELECT crew_id, course_name FROM gld_crew_training WHERE training_date = %s", (AS_OF,))
    data["incidents"] = pg.q("""SELECT event_id, event_date + %(s)s AS event_date, crew_id, depot_id, event_type, equipment_class, severity, status
                                FROM gld_safety_event WHERE status = 'Open' ORDER BY event_id""", {"s": shift})
    data["eba_rules"] = pg.q("""SELECT role, max_hours_per_day, max_consecutive_days, min_rest_hours, ot_multiplier, max_travel_as_work_hours
                                FROM gld_eba_rule""")
    data["feeders"] = pg.q("""SELECT feeder_id, feeder_name, region_id, zs_id, lat, lon, customers, life_support, stress_level, status
                              FROM gld_rm_feeder ORDER BY feeder_id""")
    data["labels"] = {}
    for r in pg.q("SELECT domain, code, label FROM ref_label"):
        data["labels"].setdefault(r["domain"], {})[r["code"]] = r["label"]
    for c in data["crews"]:
        c["orig_region"] = c["region_id"]
    return data


def _jobs(day):
    """Today's work: the planned work orders (synced gold, as-of day) and the jobs raised at run time. A raised job not yet done
    carries over midnight (a fault that tripped at 11:52 pm still needs its crew at 12:10 am)."""
    return _typed(pg.q(f"""SELECT {JOB_COLS}, %(d)s::date AS job_date, NULL::text AS situation_id, false AS is_emergency
                           FROM gld_field_job WHERE job_date = %(a)s
                           UNION ALL
                           SELECT {JOB_COLS}, %(d)s::date AS job_date, situation_id, is_emergency FROM rm.raised_job r
                           WHERE r.job_date = %(d)s OR (r.job_date < %(d)s AND NOT EXISTS (
                             SELECT 1 FROM rm.field_status f WHERE f.job_id = r.job_id AND f.status = 'done'))
                           ORDER BY job_id""", {"d": day, "a": AS_OF}))


def _typed(rows):
    for j in rows:   # the solver and the field day read these as plain numbers
        j["est_customer_minutes"] = int(j["est_customer_minutes"] or 0)
        j["is_emergency"] = bool(j["is_emergency"])
    return rows


def _job_specs(jobs):
    """Each job type's skill, ticket and typical duration, from today's work: used to raise a job at run time."""
    by = {}
    for j in jobs:
        by.setdefault(j["job_type"], []).append(j)
    out = {}
    for t, js in by.items():
        mid = sorted(js, key=lambda x: x["duration_min"])[len(js) // 2]
        out[t] = dict(required_skill=mid["required_skill"], required_ticket=mid["required_ticket"], duration_min=mid["duration_min"], funding=mid["funding"])
    return out


def _reset_caches():
    for k in ("rosters", "field_days"):
        STATE[k] = {}
    STATE.pop("exec_cache", None)
    STATE.pop("expiring", None)


def ensure():
    """Load (or refresh) the working set: a new Sydney day reloads everything, a new jobs revision reloads the jobs, a new crews
    revision re-applies the supervisors' region moves. One small read when nothing changed."""
    with _lock:
        if STATE.get("checked", 0) > time.time() - 1.0 and STATE.get("day") == today().isoformat() and not store.CHANGED[0]:
            return STATE   # checked within the last second: one request reads one consistent working set
        store.CHANGED[0] = False
        jobs_rev, crews_rev = store.revs("__jobs__", "__crews__")
        if STATE.get("day") != today().isoformat():
            STATE.clear()
            STATE["data"] = _load()
            STATE["day"] = STATE["data"]["day"]
            STATE["jobs_rev"] = None
        d = STATE["data"]
        if STATE.get("jobs_rev") != jobs_rev:
            d["jobs"] = _jobs(today())
            STATE["jobs_by_id"] = {j["job_id"]: j for j in d["jobs"]}
            STATE["specs"] = _job_specs([j for j in d["jobs"] if j["source"] not in ("outage", "situation", "manual")]) or STATE.get("specs", {})
            STATE["jobs_rev"] = jobs_rev
            STATE["crews_rev"] = None
        if STATE.get("crews_rev") != crews_rev:
            moves = store.crew_region_all()
            for c in d["crews"]:
                ov = moves.get(c["crew_id"])
                c["region_id"] = ov["region"] if ov else c["orig_region"]
            STATE["engine"] = RosterEngine(d)
            _reset_caches()
            STATE["crews_rev"] = crews_rev
        STATE["checked"] = time.time()
        return STATE


def eng():
    return ensure()["engine"]


def data():
    return ensure()["data"]


def label(domain, code):
    return (data()["labels"].get(domain) or {}).get(code) or (code or "")


def region_name(region):
    return next((r["name"] for r in data()["regions"] if r["region_id"] == region), region)


def regions_in_area(area):
    return [r["region_id"] for r in data()["regions"] if r["area"] == area]


def jobs_by_id():
    return ensure()["jobs_by_id"]


def crew(crew_id):
    return eng().crews.get(crew_id)
