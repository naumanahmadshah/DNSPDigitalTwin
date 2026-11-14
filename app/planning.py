"""Plan and invest, AI aided: a stepped programme the planner completes left to right. The first four steps are drafted by Opus
(ATLAS_LLM_PLAN) from the accepted output of the step before and the synced planning marts (gold), in a background thread; the planner
accepts, edits or re-runs each. A step opens only once the step before it is accepted, and saving a change to an accepted step resets the
steps after it. The work bundles step also compares the delivery options of the planner's packages, live. The last step releases the
locked near-term window: the planner keeps or leaves out each bundle, prepares and schedules the customer notices, and releases the plan,
which raises each bundle as a field job in resource management on its outage day (rm.dispatch.raise_job) and puts the plan on the
Released Plans board.

Runs, step outputs, edits, approvals, the release choice, releases and their items are business records in Lakebase schema atlas (fed to
the lakehouse by the change data feed). Deterministic facts (scores, checks, costs, the delivery schedule, notice dates, the roster) are
computed here from the marts, never by the model."""
import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
import uuid
from zoneinfo import ZoneInfo

import core
import notify as NT
from core import pg, pq
from rm import data as RD
from rm import dispatch as RX
from rm import store

PLAN_MODEL = os.environ.get("ATLAS_LLM_PLAN", "databricks-claude-opus-5-5")
SYD = ZoneInfo("Australia/Sydney")
STEPS = [
    dict(key="forecast", label="Forecast demand", question="Where will demand and new connections outgrow the network?", ai=True),
    dict(key="constraints", label="Network limitations and options", question="Which limits bind, and what can relieve each one?", ai=True),
    dict(key="optimise", label="Design interventions", question="Which option is worth doing, on balance?", ai=True),
    dict(key="bundles", label="Structure work bundles", question="Which works share one outage, and can the crews deliver each package?", ai=True),
    dict(key="release", label="Release works plan", question="What goes to the field now, and what waits?", ai=False),
]
STEP_KEYS = [s["key"] for s in STEPS]
AI_STEPS = [s["key"] for s in STEPS if s["ai"]]
PREV = {k: (STEP_KEYS[i - 1] if i else None) for i, k in enumerate(STEP_KEYS)}
LABEL = {s["key"]: s["label"] for s in STEPS}
# Weights of the six criteria in an option's total score (each criterion is scored 0 to 100 by the model; the total is computed here).
WEIGHTS = {"network_benefit": 0.25, "asset_risk": 0.20, "cost": 0.20, "reliability": 0.15, "sustainability": 0.10, "timing": 0.10}
PROGRAMME_WEEKS = 52     # the rolling twelve month plan
LOCKED_WEEKS = 2         # the locked near-term window that is released to the field
ROSTER_DAYS = 7          # crews are rostered to released work this many days ahead
PLANNED_OUTAGE_MAX_MIN = 240   # a planned outage longer than this needs a split or a longer notice
OUTAGE_START_MIN = 480   # planned outages start at 8:00 am
CREW_DAY_HOURS = 10      # hours a crew can plan on the tools in a day (gld_plan_assumption crew_day_hours)
# Output budget per step. Opus spends part of it on reasoning before the visible JSON, so these leave room for the answer.
MAX_TOKENS = dict(forecast=6000, constraints=4000, optimise=16000, bundles=6000)
# Shortlists sent to the model: the model works on these, the rest is listed and ranked by the marts (keeps each draft under about a minute).
SHORTLIST = dict(constraints=30, optimise=12, bundles=40)
DRAFT_TIMEOUT_S = 300.0
# Editable fields per step: the planner may change these on a draft (anything else is the model's output and the marts' facts). The
# bundles' package is any package of the draft, or a new one; the delivery option is one package or all of them ("ALL").
EDITABLE = dict(
    forecast={"pressure": {"high", "medium", "low"}, "why": None},
    constraints={"carry": {True, False}, "option_ids": None, "why": None},
    optimise={"recommended_option_id": None, "why": None},
    bundles={"package": None, "why": None, "scenario": None},
)
NEW_PACKAGE = "__new__"   # the package value that creates the next package (C, D ...)
PLAN_ITEM = "__plan__"    # the item id of a draft's own settings (the delivery option)
CAPACITY_TYPES = {"zone_capacity", "zone_security", "feeder_capacity", "transmission_capacity"}
COST_PARTS = ["labour", "materials", "plant", "traffic", "switching", "customer", "contingency"]
PROMPT_SYSTEM = (
    "You are the planning assistant for an electricity distribution network's asset and works planning team. You write for an asset "
    "planning engineer. Use plain words and sentence case. Never use an em dash or an en dash: use commas, colons, periods or parentheses. "
    "No emojis. Quote numbers only as they appear in the data you are given; never invent a figure, a name or an id. Use the names in the "
    "data, not the codes, in sentences. Answer with one JSON object and nothing else (no markdown fences)."
)


# ------------------------------------------------------------------ small helpers
def clean(v):
    """Text the planner reads: no em or en dashes (ranges become 'to'); recursively over lists and dicts."""
    if isinstance(v, str):
        t = re.sub(r"(?<=\d)\s*[\u2013\u2014]\s*(?=\d)", " to ", v)
        return re.sub(r"\s*[\u2013\u2014]\s*", ", ", t)
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return v


def _num(v, nd=None):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if nd is not None else x


def _check_step(step):
    if step not in STEP_KEYS:
        raise ValueError(f"No such planning step: {step}.")


def today():
    return dt.datetime.now(SYD).date()


def add_business_days(d, n):
    """The date n business days after d (weekends skipped; public holidays are not in the planning calendar)."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _business_days_before(d, n):
    """The date n business days before d (weekends skipped; public holidays are not in the planning calendar)."""
    x, k = d, 0
    while k < n:
        x = x - dt.timedelta(days=1)
        if x.weekday() < 5:
            k += 1
    return x


_tables = {}


def _exists(table):
    """Whether a synced planning table is in Lakebase yet (optional tables appear after their sync)."""
    if table not in _tables:
        _tables[table] = bool((pq("SELECT to_regclass(%(t)s) AS r", {"t": table}) or [{}])[0].get("r"))
    return _tables[table]


def gold(table, where="", params=None, ttl=120):
    """Rows of a synced planning mart (schema serving is first on the search path, so the table name is bare)."""
    return pq(f"SELECT * FROM {table} {where}", params, ttl=ttl)


def assumptions():
    return {r["key"]: r["value"] for r in gold("gld_plan_assumption", ttl=600)}


def notice_days():
    return int(assumptions().get("planned_notice_business_days", 4))


def _ref_labels(domain):
    return {r["code"]: r["label"] for r in pq("SELECT code, label FROM ref_label WHERE domain = %(d)s", {"d": domain}, ttl=600)}


# ------------------------------------------------------------------ records: runs, steps, edits, approvals
def _expire_stuck():
    """A draft that has been running for over eight minutes (a restart mid-draft) is marked failed, so the planner can re-run it."""
    pg.q("""UPDATE atlas.planning_step SET status = 'failed', error = 'The draft did not finish (timed out or the app restarted).'
            WHERE status = 'drafting' AND created_at < now() - interval '8 minutes'""")


def create_run(title, actor):
    rid = "PLN-" + uuid.uuid4().hex[:8].upper()
    a = assumptions()
    title = (title or "").strip()[:120] or f"Works programme from {today():%d %b %Y}"
    pg.q("""INSERT INTO atlas.planning_run (run_id, title, started_by, current_step, first_fy, plan_years)
            VALUES (%s, %s, %s, 'forecast', %s, %s)""", (rid, title, actor, int(a.get("plan_first_fy", 2027)), int(a.get("plan_years", 10))))
    core.audit(actor, "planning_run_start", rid, {"title": title})
    return rid


def list_runs():
    """The programmes being designed (not yet released), most recently worked on first, with how many steps each has accepted."""
    rows = pg.q("""SELECT r.run_id, r.title, r.started_by, r.started_at, r.updated_at, r.saved_at, r.saved_by, r.current_step,
                          (SELECT count(DISTINCT s.step_key) FROM atlas.planning_step s
                           WHERE s.run_id = r.run_id AND s.status = 'accepted' AND s.step_key = ANY(%s)) AS steps_done
                   FROM atlas.planning_run r WHERE r.status = 'open' ORDER BY r.updated_at DESC, r.started_at DESC""", (AI_STEPS,))
    for r in rows:
        r["steps_done"] = int(r["steps_done"] or 0)
        r["step_label"] = LABEL.get(STEP_KEYS[min(r["steps_done"], len(STEP_KEYS) - 1)])
    return rows


def save_run(run_id, actor, step=None):
    """The planner keeps the programme to come back to later (everything it holds is already recorded; this marks it saved), at the step
    in hand when one is given: opening Plan again continues from that step."""
    _run(run_id)
    if step is not None:
        _check_step(step)
    pg.q("UPDATE atlas.planning_run SET saved_at = now(), saved_by = %s, current_step = coalesce(%s, current_step) WHERE run_id = %s",
         (actor, step, run_id))
    core.audit(actor, "planning_run_save", run_id, {"step": step})
    return run_state(run_id)


def delete_run(run_id, actor):
    """Delete a programme being designed and everything it holds: its step versions, edits, decisions, release choice and the customer
    notices prepared for it that have not gone out. A released plan cannot be deleted."""
    run = _run(run_id)
    if run["status"] == "released":
        raise ValueError("A released plan cannot be deleted.")
    gone = NT.delete_notices([g["notice_id"] for g in pg.q("SELECT notice_id FROM atlas.notice WHERE plan_run_id = %s AND status <> 'sent'", (run_id,))])
    _delete_run_rows(run_id)
    core.audit(actor, "planning_run_delete", run_id, {"title": run["title"], "notices_removed": gone})
    return dict(ok=True, run_id=run_id, notices_removed=gone)


def _delete_run_rows(run_id):
    """A programme's own records: its release choice, edits, decisions, step versions and the programme itself."""
    for t in ("planning_selection", "planning_step", "planning_run"):
        pg.q(f"DELETE FROM atlas.{t} WHERE run_id = %s", (run_id,))


def delete_release(release_id, actor):
    """Delete a released plan and everything it made, as if it had never been released or designed: the field jobs raised for its outages
    (with any crew assignment, field status, hold, crew position and field report on them), its customer notices not yet sent (with their
    messages), the release and its items, and the programme it was designed in. A notice already sent stays, with its messages, as the
    record of what customers were told; the notices list shows its works plan as removed. The audit log keeps a record of the deletion."""
    r = pg.q("SELECT release_id, run_id, title, job_ids FROM atlas.planning_release WHERE release_id = %s", (release_id,), one=True)
    if not r:
        raise ValueError(f"No such released plan: {release_id}.")
    items = pg.q("SELECT job_id, notice_id FROM atlas.planning_release_item WHERE release_id = %s", (release_id,))
    jobs = sorted({i["job_id"] for i in items if i["job_id"]} | set(r["job_ids"] or []))
    raised = pg.q("DELETE FROM rm.raised_job WHERE job_id = ANY(%s) AND source = 'plan' RETURNING job_id, region_id", (jobs,)) if jobs else []
    for j in raised:
        store.override_del(j["region_id"], j["job_id"])
        store.field_status_del(j["region_id"], j["job_id"])
        pg.q("DELETE FROM rm.parked WHERE region_id = %s AND job_id = %s", (j["region_id"], j["job_id"]))
    if jobs:
        pg.q("DELETE FROM rm.field_report WHERE job_id = ANY(%s)", (jobs,))
        for c in pg.q("SELECT crew_id FROM rm.crew_live WHERE job_id = ANY(%s)", (jobs,)):   # a crew out on one of its jobs is free again
            store.crew_live_del(c["crew_id"])
    store.bump("__jobs__")
    nids = [n["notice_id"] for n in pg.q("""SELECT notice_id FROM atlas.notice WHERE (release_id = %s OR plan_run_id = %s
                                              OR notice_id = ANY(%s)) AND status <> 'sent'""",
                                         (release_id, r["run_id"], [i["notice_id"] for i in items if i["notice_id"]]))]
    notices = NT.delete_notices(nids)   # sent notices are kept as the record of what customers were told
    pg.q("DELETE FROM atlas.planning_release_item WHERE release_id = %s", (release_id,))
    pg.q("DELETE FROM atlas.planning_release WHERE release_id = %s", (release_id,))
    _delete_run_rows(r["run_id"])
    core.audit(actor, "planning_release_delete", release_id, dict(title=r["title"], run=r["run_id"], jobs=len(raised), notices=notices))
    return dict(ok=True, release_id=release_id, jobs_removed=len(raised), notices_removed=notices)


def _run(run_id):
    r = pg.q("SELECT * FROM atlas.planning_run WHERE run_id = %s", (run_id,), one=True)
    if not r:
        raise ValueError(f"No such planning programme: {run_id}.")
    return r


def _accepted(run_id, step):
    return pg.q("SELECT * FROM atlas.planning_step WHERE run_id = %s AND step_key = %s AND status = 'accepted'", (run_id, step), one=True)


def _latest(run_id, step):
    """The newest version of a step that is still live: not one the planner discarded, nor one an upstream change reset."""
    return pg.q("""SELECT * FROM atlas.planning_step WHERE run_id = %s AND step_key = %s AND status NOT IN ('discarded', 'reset')
                   ORDER BY version DESC LIMIT 1""", (run_id, step), one=True)


def run_state(run_id):
    """The programme and, for each step: whether it is accepted (done), whether it can be opened (every step before it is accepted), and
    whether it holds changes not yet saved (a drafted version newer than the accepted one)."""
    _expire_stuck()
    run = _run(run_id)
    steps, open_ = [], True
    for s in STEPS:
        k = s["key"]
        if s["ai"]:
            acc, last = _accepted(run_id, k), _latest(run_id, k)
            done = bool(acc)
            pending = bool(acc and last and last["version"] > acc["version"] and last["status"] in ("drafted", "drafting"))
            status = "drafting" if last and last["status"] == "drafting" else ("accepted" if acc else (last["status"] if last else "not_started"))
        else:   # the release step: done once the plan is released
            acc, done, pending, status = None, run["status"] == "released", False, ("released" if run["status"] == "released" else "not_started")
        steps.append(dict(key=k, label=s["label"], question=s["question"], ai=s["ai"], enabled=open_, done=done, pending=pending,
                          status=status, accepted_version=acc["version"] if acc else None))
        open_ = open_ and done
    return dict(run=dict(run_id=run["run_id"], title=run["title"], started_by=run["started_by"], started_at=run["started_at"],
                         updated_at=run["updated_at"], saved_at=run["saved_at"], status=run["status"], current_step=run["current_step"],
                         released_at=run["released_at"], released_by=run["released_by"], release_id=run["release_id"]),
                steps=steps)


# ------------------------------------------------------------------ deterministic facts (never from the model)
def bundle_checks(b, cap, labels):
    """The constraints on one bundle, each with a status the planner can act on (ok or action, labelled from ref_label bundle_check):
    school zone, road occupancy, crew hours, outage duration and switching crew. Computed from the marts."""
    out = {}
    out["school_zone"] = dict(status="action" if b.get("school_on_feeder") else "ok",
                              note="A school is on this feeder: heavy plant must stay out of school drop-off and pick-up hours." if b.get("school_on_feeder")
                              else "No school on this feeder.")
    out["road_occupancy"] = dict(status="action" if b.get("traffic_management_needed") else "ok",
                                 note=("Likely road works on arterial roads: a road occupancy permit and a traffic control plan are needed."
                                       if b.get("traffic_management_needed") else "No road closure expected."))
    prog = _num((cap or {}).get("programme_hours")) or 0
    hrs = _num(b.get("crew_hours")) or 0
    out["crew_hours"] = dict(status="ok" if hrs <= prog else "action",
                             note=f"{hrs:.1f} crew hours against {prog:.0f} programme hours a week in its region.")
    mins = _num(b.get("outage_minutes")) or 0
    out["outage_duration"] = dict(status="ok" if mins <= PLANNED_OUTAGE_MAX_MIN else "action",
                                  note=f"Planned outage of about {mins / 60:.1f} hours, covering switching and the longest item.")
    out["switching_crew"] = dict(status="action" if (b.get("switching_jobs") or 0) > 0 else "ok",
                                 note=("Needs a protection or substation switching crew for the isolation." if (b.get("switching_jobs") or 0) > 0
                                       else "A standard switching crew isolates the work."))
    for k, v in out.items():
        v["label"] = labels.get(f"{k}.{v['status']}") or v["status"]
    return out


def _start():
    """The first day a planned outage can be booked: one business day to prepare the customer notices, then the notice period, so no
    notice is ever due before the plan is released."""
    return add_business_days(today(), notice_days() + 1)


def schedule(bundles, caps, start, weeks=PROGRAMME_WEEKS):
    """Greedy delivery schedule: bundles in priority order take the earliest week in their region with programme hours and a switching
    slot free (one slot per switching crew per working day), on the next working day of that week. Week one starts at the first bookable
    day. Returns the placed bundles and the ones that do not fit in the window."""
    hours = {r: [float(caps[r]["programme_hours"] or 0)] * weeks for r in caps}
    slots = {r: [int(caps[r]["switching_crews"] or 0) * 5] * weeks for r in caps}
    taken = {r: [0] * weeks for r in caps}
    placed, left = [], []
    order = sorted(bundles, key=lambda b: (-(b.get("priority") or 0), -(b.get("value_score") or 0), b["bundle_id"]))
    for b in order:
        r = b.get("region_id") or next(iter(caps))
        need = _num(b.get("crew_hours")) or 0
        done = False
        if r in hours:
            for w in range(weeks):
                if hours[r][w] >= need and slots[r][w] > 0:
                    hours[r][w] -= need
                    slots[r][w] -= 1
                    days = [start + dt.timedelta(days=7 * w + i) for i in range(7) if (start + dt.timedelta(days=7 * w + i)).weekday() < 5]
                    day = days[taken[r][w] % len(days)]
                    taken[r][w] += 1
                    placed.append(dict(bundle_id=b["bundle_id"], region_id=r, week=w, outage_date=day.isoformat(), crew_hours=round(need, 1)))
                    done = True
                    break
        if not done:
            left.append(b["bundle_id"])
    return placed, left


def _cost(b):
    """A bundle's estimated cost by category and in total (AUD), from the bundle mart."""
    out = {p: _num(b.get(f"cost_{p}_aud")) or 0.0 for p in COST_PARTS}
    out["total"] = _num(b.get("cost_total_aud")) or 0.0
    return out


def _sum_costs(rows):
    tot = {p: 0.0 for p in COST_PARTS + ["total"]}
    for r in rows:
        for p in tot:
            tot[p] += (r.get("cost") or {}).get(p, 0.0)
    return {p: round(v) for p, v in tot.items()}


def _scenario_label(key, keys):
    if key == "ALL":
        return "Package " + keys[0] if len(keys) == 1 else "Packages " + ", ".join(keys[:-1]) + " and " + keys[-1]
    return f"Package {key} only"


def scenario_facts(by_key, caps, start):
    """The delivery options of the planner's packages: each package on its own, and all of them together, with the schedule, outages,
    customers and estimated cost of each, and whether the crews can carry it."""
    keys = sorted(k for k in by_key if by_key[k])
    out = []
    for key in keys + (["ALL"] if len(keys) > 1 else []):
        bs = [b for k in (keys if key == "ALL" else [key]) for b in by_key.get(k, [])]
        placed, left = schedule(bs, caps, start)
        weeks_needed = (max((p["week"] for p in placed), default=-1) + 1)
        byid = {b["bundle_id"]: b for b in bs}
        locked = [p for p in placed if p["week"] < LOCKED_WEEKS]
        out.append(dict(id=key, label=_scenario_label(key, keys), packages=keys if key == "ALL" else [key], bundles=len(bs),
                        crew_hours=round(sum(_num(b.get("crew_hours")) or 0 for b in bs), 1),
                        cost_aud=round(sum(_cost(b)["total"] for b in bs)), customers_interrupted=sum(int(b.get("customers_interrupted") or 0) for b in bs),
                        customer_hours=round(sum(float(b.get("customer_hours") or 0) for b in bs)), weeks_needed=weeks_needed, unscheduled=len(left),
                        locked_window_bundles=len(locked), locked_window_hours=round(sum(byid[p["bundle_id"]]["crew_hours"] for p in locked), 1),
                        feasible=len(left) == 0 and weeks_needed <= PROGRAMME_WEEKS, placed=placed))
    feasible = [f for f in out if f["feasible"]]
    rec = next((f["id"] for f in out if f["id"] == "ALL" and f["feasible"]), None) or \
        (max(feasible, key=lambda f: (f["bundles"], -f["cost_aud"]))["id"] if feasible else (out[-1]["id"] if out else None))
    for f in out:
        f["recommended"] = f["id"] == rec
    return out, rec


# ------------------------------------------------------------------ context builders: the data a step is drafted from
def _zones():
    """One row per zone substation: the first year's figures and the figures in FY31 and FY36, named in financial years for the model
    and the screen (fy27 is the first plan year)."""
    rows = gold("gld_plan_zone_demand", "WHERE years_out IN (0, 4, 9)", ttl=120)
    tag = {0: "fy27", 4: "fy31", 9: "fy36"}
    by = {}
    for r in rows:
        z = by.setdefault(r["zs_id"], dict(id=r["zs_id"], name=r["zs_name"], area=r["area"], single_fed=bool(r["single_fed"]),
                                           fortnight_peak_mw=_num(r["fortnight_peak_mw"]), hist_peak_mw=_num(r["hist_peak_mw"]),
                                           growth_percent=_num(r["growth_pa"] * 100, 1) if r["growth_pa"] is not None else None,
                                           firm_mva=_num(r["firm_mva"]), ev_mw_now=_num(r["ev_mw_now"]), solar_mw=_num(r["solar_mw"]),
                                           customers=int(r["customers"] or 0), life_support=int(r["life_support"] or 0)))
        t = tag[r["years_out"]]
        z[f"peak_{t}_mw"] = _num(r["peak_mw"])
        z[f"utilisation_{t}_pct"] = _num(r["utilisation_pct"])
    for z in by.values():
        first = gold("gld_plan_zone_demand", "WHERE zs_id = %(z)s AND over_firm ORDER BY fiscal_year LIMIT 1", {"z": z["id"]}, ttl=120)
        fy = int(first[0]["fiscal_year"]) if first else None
        z["first_over_fy"] = fy
        z["first_over"] = f"FY{fy % 100:02d}" if fy else None
    return list(by.values())


def _connections():
    """Open new connection applications per zone substation, when the connections mart is synced (absent until then: no figure is shown)."""
    if not _exists("gld_connection_application"):
        return None
    rows = pq("""SELECT zs_id, count(*) AS open_applications, sum(load_kva) AS load_kva, sum(export_kw) AS export_kw,
                        sum(CASE WHEN blocker IS NOT NULL AND blocker <> '' THEN 1 ELSE 0 END) AS blocked
                 FROM gld_connection_application WHERE coalesce(stage, '') NOT IN ('Commissioned', 'Completed', 'Cancelled') GROUP BY zs_id""", ttl=120)
    return {r["zs_id"]: dict(open_applications=int(r["open_applications"] or 0), load_kva=_num(r["load_kva"]), export_kw=_num(r["export_kw"]),
                             blocked=int(r["blocked"] or 0)) for r in rows}


def _constraint_rows(limit=70):
    rows = gold("gld_plan_constraint", "ORDER BY severity DESC, constraint_id", ttl=120)
    if _exists("gld_plan_constraint_transmission"):
        rows += gold("gld_plan_constraint_transmission", "ORDER BY severity DESC, constraint_id", ttl=120)
    rows.sort(key=lambda r: -(r.get("severity") or 0))
    return rows[:limit]


def _option_rows(constraint_ids):
    if not constraint_ids:
        return []
    ids = list(constraint_ids)
    rows = gold("gld_plan_intervention", "WHERE constraint_id = ANY(%(ids)s)", {"ids": ids}, ttl=120)
    if _exists("gld_plan_intervention_transmission"):
        rows += gold("gld_plan_intervention_transmission", "WHERE constraint_id = ANY(%(ids)s)", {"ids": ids}, ttl=120)
    return rows


def _option_brief(o):
    return dict(id=o["intervention_id"], constraint_id=o["constraint_id"], option_type=o["option_type_label"] or o["option_type"],
                option=o["option_label"], cost_10yr_aud=_num(o["cost_10yr_aud"]), relief_mw=_num(o["relief_mw"]),
                customers_protected=int(o["customers_protected"] or 0), asset_risk_reduction_pct=_num(o["asset_risk_reduction_pct"]),
                reliability_gain_pct=_num(o["reliability_gain_pct"]), carbon_tco2e_10yr=_num(o["carbon_tco2e_10yr"]),
                lead_time_months=_num(o["lead_time_months"]), earliest_fy=o["earliest_fy"])


def _bundles(limit=None):
    rows = gold("gld_plan_bundle", "ORDER BY value_score DESC, bundle_id", ttl=120)
    if limit:
        rows = rows[:limit]
    for b in rows:
        b["crew_hours"] = _num(b["crew_hours"])
        b["customers_interrupted"] = int(b["customers_interrupted"] or 0)
        b["life_support_interrupted"] = int(b["life_support_interrupted"] or 0)
        b["customer_hours"] = _num(b["customer_hours"])
        b["outage_minutes"] = int(b["outage_minutes"] or 0)
    return rows


def _caps():
    return {r["region_id"]: r for r in gold("gld_plan_region_capacity", ttl=120)}


def _build(step, run_id, up):
    """The compact facts a step is drafted from, plus the rows the screen shows. Returns (payload, facts)."""
    a = assumptions()
    if step == "forecast":
        zones = _zones()
        conn = _connections()
        for z in zones:
            c = (conn or {}).get(z["id"], {})
            z["open_connections"] = c.get("open_applications") if conn is not None else None
            z["connection_load_kva"] = c.get("load_kva") if conn is not None else None
        zones.sort(key=lambda z: (z["first_over_fy"] or 99, -(z.get("utilisation_fy27_pct") or 0)))
        public = [{k: v for k, v in z.items() if k != "first_over_fy"} for z in zones]
        assume = dict(growth_floor_percent=_num(a.get("growth_floor_pa", 0) * 100, 1), growth_cap_percent=_num(a.get("growth_cap_pa", 0) * 100, 1),
                      ev_growth_percent=_num(a.get("ev_growth_pa", 0) * 100, 1), coincident_peak_percent=_num(a.get("coincident_peak_share", 0) * 100, 0),
                      power_factor=a.get("power_factor"), planning_horizon="FY27 to FY36")
        return dict(zones=public, connections_synced=conn is not None, assumptions=assume), dict(zones=zones, connections_synced=conn is not None)
    if step == "constraints":
        cons = _constraint_rows(SHORTLIST["constraints"])
        opts = _option_rows([c["constraint_id"] for c in cons])
        by_c = {}
        for o in opts:
            by_c.setdefault(o["constraint_id"], []).append(_option_brief(o))
        items = [dict(id=c["constraint_id"], type=c["constraint_type_label"] or c["constraint_type"], level=c["constraint_level"],
                      zone=c["zs_name"], feeder=c["feeder_name"], metric=c["metric_label"], value=_num(c["metric_value"]), unit=c["metric_unit"],
                      limit=_num(c["threshold"]), years_to_limit=c["years_to_limit"], severity=c["severity"], customers=c["customers"],
                      life_support=c["life_support"], summary=c["summary"], options=by_c.get(c["constraint_id"], [])) for c in cons]
        return dict(constraints=items, upstream=up), dict(constraints=items)
    if step == "optimise":
        carried = [i for i in (up or {}).get("items", []) if i.get("carry") is not False][:SHORTLIST["optimise"]]
        ids = [i["id"] for i in carried]
        cons = {c["constraint_id"]: c for c in _constraint_rows(500)}
        opts = _option_rows(ids)
        allowed = {}
        for o in opts:
            allowed.setdefault(o["constraint_id"], []).append(_option_brief(o))
        items = []
        for i in carried:
            keep = set(i.get("option_ids") or [])
            c = cons.get(i["id"], {})
            os_ = [o for o in allowed.get(i["id"], []) if not keep or o["id"] in keep]
            items.append(dict(id=i["id"], summary=c.get("summary", ""), severity=c.get("severity"), type=c.get("constraint_type_label", ""),
                              options=os_))
        return dict(constraints=items, upstream=up), dict(constraints=items, weights=WEIGHTS)
    if step == "bundles":
        best = _bundles(SHORTLIST["bundles"])
        caps = _caps()
        labels = _ref_labels("bundle_check")
        items = []
        for b in best:
            items.append(dict(id=b["bundle_id"], feeder=b["feeder_name"], zone=b["zs_name"], region=b["region_id"], works=b["job_type_labels"],
                              items=b["job_count"], defects=b["defect_jobs"], crew_hours=b["crew_hours"], outage_minutes=b["outage_minutes"],
                              customers_interrupted=b["customers_interrupted"], life_support=b["life_support_interrupted"],
                              value_score=b["value_score"], school_on_feeder=bool(b["school_on_feeder"]), cost_aud=_cost(b)["total"],
                              traffic_management_needed=bool(b["traffic_management_needed"]), switching_jobs=b["switching_jobs"],
                              checks={k: v["status"] for k, v in bundle_checks(b, caps.get(b["region_id"]), labels).items()}))
        return dict(bundles=items, programme=[dict(region=r["region_name"], programme_hours=_num(r["programme_hours"]),
                                                   spare_hours=_num(r["spare_hours"])) for r in caps.values()],
                    decided_works=[dict(id=i["id"], recommended_option_id=i.get("recommended_option_id"), why=i.get("why"))
                                   for i in (up or {}).get("items", [])]), dict(bundles=best, caps=caps)
    raise ValueError(f"The {LABEL.get(step, step)} step is not drafted by AI.")


# ------------------------------------------------------------------ drafting with Opus
SHAPES = {
    "forecast": '{"narrative": str, "headline": str, "connections": str, "items": [{"id": zone id, "pressure": "high"|"medium"|"low", "why": str}]}',
    "constraints": '{"narrative": str, "items": [{"id": constraint id, "carry": bool, "why": str (under 15 words)}]}',
    "optimise": ('{"narrative": str, "items": [{"id": constraint id, "recommended_option_id": option id, "why": str (under 20 words), '
                 '"scores": [{"option_id": str, "network_benefit": 0-100, "asset_risk": 0-100, "cost": 0-100, "reliability": 0-100, '
                 '"sustainability": 0-100, "timing": 0-100}]}]}'),
    "bundles": ('{"narrative": str, "packages": [{"key": "A" or "B", "label": str, "why": str (under 40 words)}], '
                '"items": [{"id": bundle id, "package": "A" or "B" or ""}]}'),
}
TASK = {
    "forecast": ("Write the demand and new connection outlook per zone substation: which zones outgrow their N-1 firm capacity first (use the "
                 "first over year), which are single fed, what rooftop solar and EV charging add, and what the connection pipeline means. Rank "
                 "the zones by pressure (high, medium, low) and say why in one or two sentences."),
    "constraints": ("Identify the binding constraints to carry into the programme (most severe and most urgent first). Keep those that matter, "
                    "drop noise, and for each keep the sensible option ids (maintain, replace, augment, reconfigure, flexibility, defer). Give "
                    "one reason each."),
    "optimise": ("For each carried constraint, score every option it lists on six criteria from 0 to 100 (network benefit: relief and "
                 "customers protected; asset risk: fault risk reduction; cost: lower ten year cost scores higher; reliability and resilience; "
                 "sustainability: carbon, fire and environment, community; timing: lead time and earliest year). Recommend one option and say "
                 "why, naming the trade-off. Deferral is a real option: recommend it when the risk is low or the cost is not justified."),
    "bundles": ("Group the work into two packages (A and B) that a planner can release as separate programmes. Pack bundles that share an "
                "isolation point or a feeder, respect school zones and road works, keep each package inside programme crew hours, and avoid "
                "switching-crew clashes. Assign each bundle to A, to B, or to neither (empty package). Give a reason per bundle that matters."),
}


def _prompt(step, payload, instruction):
    parts = [f"Step: {next(s['label'] for s in STEPS if s['key'] == step)}.", TASK[step],
             "Prose rules: write years as FY31 (financial year), percentages with a percent sign, places by name. Ids and codes belong only in "
             "the id fields, never in sentences. Keep the narrative under 200 words.",
             "Return a single JSON object with this shape (keys and types exactly): " + SHAPES[step],
             "Use only the ids given in the data for items, and only the option ids listed for each constraint. Be brief: answer "
             "directly, keep every why to the word limit given in the shape, and do not repeat the data back."]
    if instruction:
        parts.append("The planner asks for this in the re-run: " + instruction[:600])
    parts.append("DATA:\n" + json.dumps(payload, default=str, separators=(",", ":")))
    return "\n\n".join(parts)


def _ask(step, prompt):
    msgs = [{"role": "system", "content": PROMPT_SYSTEM}, {"role": "user", "content": prompt}]
    last = None
    for attempt in range(2):
        try:
            r = core.llm_client().with_options(timeout=DRAFT_TIMEOUT_S, max_retries=1).chat.completions.create(
                model=PLAN_MODEL, messages=msgs, max_tokens=MAX_TOKENS[step])
            text = core.text_of(r.choices[0].message.content)
            if r.choices[0].finish_reason == "length":
                raise ValueError("the reply ran out of room before the JSON finished")
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError("the model reply had no JSON object")
            return json.loads(m.group(0), strict=False), text
        except Exception as e:  # noqa: BLE001 - one retry: the client may hold an expired token, and the reply may not be JSON
            last = e
            core._llm = None   # rebuild the client as core.complete does
            msgs.append({"role": "user", "content": f"That reply could not be used ({str(e)[:160]}). Return only the JSON object."})
    raise RuntimeError(f"The AI model did not return a usable draft: {str(last)[:200]}.")


def _validate(step, out, facts):
    """Drop what the marts do not support (unknown ids, options not offered for a constraint, bad scores, unknown packages); record why."""
    warnings = []
    out = clean(out or {})
    items = out.get("items") or []
    if step == "forecast":
        ids = {z["id"] for z in facts["zones"]}
        keep = [i for i in items if i.get("id") in ids]
        warnings += [f"Dropped an unknown zone id {i.get('id')}." for i in items if i.get("id") not in ids]
        for i in keep:
            i["pressure"] = i.get("pressure") if i.get("pressure") in ("high", "medium", "low") else "medium"
        out["items"] = keep
    elif step == "constraints":
        cons = {c["id"]: c for c in facts["constraints"]}
        keep = []
        for i in items:
            c = cons.get(i.get("id"))
            if not c:
                warnings.append(f"Dropped an unknown constraint id {i.get('id')}.")
                continue
            offered = {o["id"] for o in c["options"]}
            i["option_ids"] = [x for x in (i.get("option_ids") or []) if x in offered] or sorted(offered)
            i["carry"] = bool(i.get("carry", True))
            keep.append(i)
        out["items"] = keep
    elif step == "optimise":
        cons = {c["id"]: c for c in facts["constraints"]}
        keep = []
        for i in items:
            c = cons.get(i.get("id"))
            if not c:
                warnings.append(f"Dropped an unknown constraint id {i.get('id')}.")
                continue
            offered = {o["id"] for o in c["options"]}
            scores = []
            for s in i.get("scores") or []:
                if s.get("option_id") not in offered:
                    continue
                row = {"option_id": s["option_id"]}
                for k in WEIGHTS:
                    row[k] = max(0, min(100, int(round(float(s.get(k, 0))))))
                row["total"] = round(sum(WEIGHTS[k] * row[k] for k in WEIGHTS), 1)
                scores.append(row)
            scores.sort(key=lambda r: -r["total"])
            rec = i.get("recommended_option_id") if i.get("recommended_option_id") in offered else (scores[0]["option_id"] if scores else None)
            if i.get("recommended_option_id") not in offered:
                warnings.append(f"Recommendation for {c['id']} was not one of its options. The top-scored option is used.")
            keep.append(dict(id=i["id"], recommended_option_id=rec, why=i.get("why", ""), scores=scores))
        out["items"] = keep
    elif step == "bundles":
        ids = {b["bundle_id"] for b in facts["bundles"]}   # facts carry the gold rows; the draft names them by id
        packs = [p for p in (out.get("packages") or []) if p.get("key") in ("A", "B")] or [dict(key="A"), dict(key="B")]
        for p in packs:
            p["label"] = f"Package {p['key']}"
        keys = {p["key"] for p in packs}
        keep = []
        for i in items:
            if i.get("id") not in ids:
                warnings.append(f"Dropped an unknown bundle id {i.get('id')}.")
                continue
            keep.append(dict(id=i["id"], package=i.get("package") if i.get("package") in keys else "", why=i.get("why", "")))
        out["items"], out["packages"] = keep, packs
    out["warnings"] = warnings
    return out


def _stage(run_id, step, version, text):
    """The stage a draft is in, shown on the screen while it runs (elapsed time comes from created_at)."""
    pg.q("UPDATE atlas.planning_step SET progress = %s, progress_at = now() WHERE run_id = %s AND step_key = %s AND version = %s",
         (text, run_id, step, version))


def _draft_worker(run_id, step, version, up_out, instruction):
    try:
        _stage(run_id, step, version, "Reading the planning data.")
        payload, facts = _build(step, run_id, up_out)
        prompt = _prompt(step, payload, instruction)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        _stage(run_id, step, version, f"The AI is drafting ({len(prompt) // 1000} thousand characters of data).")
        raw, _ = _ask(step, prompt)
        _stage(run_id, step, version, "Checking the draft against the planning data.")
        out = _validate(step, raw, facts)
        narrative = clean(str(out.get("narrative") or out.get("headline") or ""))
        pg.q("""UPDATE atlas.planning_step SET status = 'drafted', output = %s::jsonb, narrative = %s, model = %s, error = NULL,
                       input_digest = %s, progress = NULL WHERE run_id = %s AND step_key = %s AND version = %s""",
             (json.dumps(out, default=str), narrative, PLAN_MODEL, digest, run_id, step, version))
        pg.q("UPDATE atlas.planning_run SET updated_at = now(), current_step = %s WHERE run_id = %s", (step, run_id))
    except Exception as e:  # noqa: BLE001 - the planner sees the failure and can re-run
        pg.q("UPDATE atlas.planning_step SET status = 'failed', error = %s, progress = NULL WHERE run_id = %s AND step_key = %s AND version = %s",
             (str(e)[:400], run_id, step, version))


def draft(run_id, step, actor, instruction=""):
    """Start a background draft of one step from the accepted output of the step before. Returns at once; poll the step."""
    _check_step(step)
    if step not in AI_STEPS:
        raise ValueError(f"The {LABEL[step]} step is not drafted by AI.")
    if _run(run_id)["status"] == "released":
        raise ValueError("This plan is released; start a new programme to plan again.")
    _expire_stuck()
    if pg.q("SELECT 1 AS x FROM atlas.planning_step WHERE run_id = %s AND status = 'drafting' LIMIT 1", (run_id,), one=True):
        raise ValueError("A draft is already running for this programme; wait for it to finish.")
    up = PREV[step]
    up_row = _accepted(run_id, up) if up else None
    if up and not up_row:
        raise ValueError(f"Complete the '{LABEL[up]}' step first.")
    up_out = (up_row or {}).get("output")
    up_version = (up_row or {}).get("version")
    v = _insert_version(run_id, step, """INSERT INTO atlas.planning_step (run_id, step_key, version, status, source, instruction, input_version, created_by)
                                         VALUES (%s, %s, %s, 'drafting', 'opus', %s, %s, %s)""", ((instruction or "")[:600], up_version, actor))
    pg.q("UPDATE atlas.planning_run SET current_step = %s, updated_at = now() WHERE run_id = %s", (step, run_id))
    core.audit(actor, "planning_draft", f"{run_id}/{step}", {"version": v, "instruction": (instruction or "")[:200]})
    threading.Thread(target=_draft_worker, args=(run_id, step, v, up_out, instruction), daemon=True).start()
    return dict(run_id=run_id, step=step, version=v, status="drafting")


def _insert_version(run_id, step, sql, rest):
    """Insert a new version of a step at the next free number (run_id, step and the version lead the values); returns the number. Two
    writers at once (a double click) take the next number in turn rather than failing on the same one."""
    import psycopg
    for _ in range(5):
        v = pg.q("SELECT coalesce(max(version), 0) + 1 AS v FROM atlas.planning_step WHERE run_id = %s AND step_key = %s", (run_id, step), one=True)["v"]
        try:
            pg.q(sql, (run_id, step, v, *rest))
            return v
        except psycopg.errors.UniqueViolation:
            continue
    raise ValueError("The step is being changed elsewhere; try again.")


def _next_package(out):
    keys = sorted(p["key"] for p in out.get("packages") or [])
    nxt = chr(ord(keys[-1]) + 1) if keys else "A"
    if nxt > "Z":
        raise ValueError("No more packages can be created.")
    out.setdefault("packages", []).append(dict(key=nxt, label=f"Package {nxt}", why="Created by the planner."))
    return nxt


def edit(run_id, step, version, edits, actor):
    """A planner edit: a new version of the step with the changes applied (each change recorded in the audit log). The new version waits
    for the planner to accept it (save) before it feeds the next step."""
    _check_step(step)
    src = pg.q("SELECT * FROM atlas.planning_step WHERE run_id = %s AND step_key = %s AND version = %s", (run_id, step, version), one=True)
    if not src or src["status"] not in ("drafted", "accepted", "superseded"):
        raise ValueError("Only a drafted version can be edited.")
    out = json.loads(json.dumps(src["output"] or {}))
    rules = EDITABLE.get(step, {})
    applied = []
    for e in edits or []:
        item_id, field, value = e.get("item_id"), e.get("field"), e.get("value")
        if field not in rules:
            raise ValueError(f"'{field}' cannot be edited in this step.")
        if item_id == PLAN_ITEM:   # the draft's own settings: the delivery option of the bundles
            if field != "scenario":
                raise ValueError(f"'{field}' is not a setting of the draft.")
            keys = sorted({i.get("package") for i in out.get("items", []) if i.get("package")})
            if value not in keys + ["ALL"]:
                raise ValueError("Choose a delivery option from the packages.")
            applied.append((item_id, field, out.get("scenario"), value))
            out["scenario"] = value
            continue
        allowed = rules[field]
        if step == "bundles" and field == "package":
            if value == NEW_PACKAGE:
                value = _next_package(out)
            elif value not in {p["key"] for p in out.get("packages") or []} | {""}:
                raise ValueError("Choose one of the packages, or create a new one.")
        elif allowed is not None and value not in allowed:
            raise ValueError(f"'{field}' must be one of {sorted(map(str, allowed))}.")
        it = next((i for i in out.get("items", []) if i.get("id") == item_id), None)
        if it is None:
            raise ValueError(f"No item {item_id} in this draft.")
        old = it.get(field)
        if field == "option_ids":
            value = [str(x) for x in (value or [])]
        it[field] = clean(value)
        applied.append((item_id, field, old, it[field]))
    v = _insert_version(run_id, step, """INSERT INTO atlas.planning_step (run_id, step_key, version, status, source, output, narrative, input_version,
                                                                      model, created_by)
                                         VALUES (%s, %s, %s, 'drafted', 'planner', %s::jsonb, %s, %s, %s, %s)""",
                        (json.dumps(out, default=str), out.get("narrative", src["narrative"]), src["input_version"], src["model"], actor))
    pg.q("UPDATE atlas.planning_run SET updated_at = now() WHERE run_id = %s", (run_id,))
    core.audit(actor, "planning_edit", f"{run_id}/{step}", {"version": v, "changes": len(applied),
                                                             "edits": [dict(item_id=i, field=f, old=o, new=n) for i, f, o, n in applied]})
    return dict(run_id=run_id, step=step, version=v, status="drafted", changes=len(applied))


def accept(run_id, step, version, actor, reason=""):
    """The planner accepts (saves) a version: it becomes the input of the next step, an earlier accepted version of this step is superseded,
    and every later step's work is reset (it was built from the version this one replaces), so those steps are drafted again."""
    _check_step(step)
    if _run(run_id)["status"] == "released":
        raise ValueError("This plan is released; start a new programme to plan again.")
    src = pg.q("SELECT * FROM atlas.planning_step WHERE run_id = %s AND step_key = %s AND version = %s", (run_id, step, version), one=True)
    if not src or src["status"] not in ("drafted", "accepted", "superseded"):
        raise ValueError("Only a drafted version can be accepted.")
    up = PREV[step]
    if up and not _accepted(run_id, up):
        raise ValueError(f"Complete the '{LABEL[up]}' step first.")
    pg.q("UPDATE atlas.planning_step SET status = 'superseded' WHERE run_id = %s AND step_key = %s AND status = 'accepted'", (run_id, step))
    pg.q("""UPDATE atlas.planning_step SET status = 'accepted', accepted_at = now(), accepted_by = %s
            WHERE run_id = %s AND step_key = %s AND version = %s""", (actor, run_id, step, version))
    pg.q("UPDATE atlas.planning_step SET status = 'discarded' WHERE run_id = %s AND step_key = %s AND status = 'drafted' AND version < %s",
         (run_id, step, version))
    later = STEP_KEYS[STEP_KEYS.index(step) + 1:]
    reset = pg.q("""UPDATE atlas.planning_step SET status = 'reset' WHERE run_id = %s AND step_key = ANY(%s)
                    AND status IN ('accepted', 'drafted', 'superseded', 'failed') RETURNING step_key""", (run_id, later))
    reset_steps = sorted({r["step_key"] for r in reset}, key=STEP_KEYS.index)
    pg.q("""UPDATE atlas.planning_run SET current_step = %s, updated_at = now(), saved_at = now(), saved_by = %s
            WHERE run_id = %s""", (later[0] if later else step, actor, run_id))   # each accept saves the programme, at the step that follows
    core.audit(actor, "planning_accept", f"{run_id}/{step}", {"version": version, "reason": (reason or "")[:400], "reset": reset_steps})
    return dict(run_id=run_id, step=step, version=version, status="accepted", reset=[LABEL[k] for k in reset_steps])


def discard(run_id, step, actor):
    """Throw away the work on a step not yet accepted: the changes made since it was accepted (the drafted versions after the accepted one),
    or, on a step never accepted, its drafts, so the step goes back to being drafted afresh."""
    acc = _accepted(run_id, step)
    n = pg.q("""UPDATE atlas.planning_step SET status = 'discarded' WHERE run_id = %s AND step_key = %s AND status IN ('drafted', 'failed')
                AND version > %s RETURNING version""", (run_id, step, acc["version"] if acc else 0))
    if not n:
        raise ValueError("Nothing to discard. This step has no draft or changes that are not yet accepted.")
    core.audit(actor, "planning_discard", f"{run_id}/{step}", {"versions": len(n)})
    return dict(run_id=run_id, step=step, discarded=len(n))


def step_view(run_id, step):
    """One step as the screen shows it: the newest live version (a draft in progress, changes not yet saved, or the accepted one), its
    text and items, the gold facts behind it (live from the marts), and the steps after it that a saved change would reset."""
    _check_step(step)
    _expire_stuck()
    st = run_state(run_id)
    s = next(x for x in st["steps"] if x["key"] == step)
    acc, last = _accepted(run_id, step), _latest(run_id, step)
    shown = acc
    if last and (not acc or last["version"] > acc["version"]):
        shown = last if last["status"] in ("drafting", "drafted") else acc
    later = [x["label"] for x in st["steps"][STEP_KEYS.index(step) + 1:] if x["done"] or x["pending"]]
    up_row = _accepted(run_id, PREV[step]) if PREV[step] else None
    view = dict(run_id=run_id, step=step, label=s["label"], question=s["question"], enabled=s["enabled"], done=s["done"],
                pending=bool(acc and shown and shown["version"] > acc["version"]), later_done=later,
                failed=(last["error"] or "The draft failed") if last and last["status"] == "failed" and (not acc or last["version"] > acc["version"]) else None,
                version=None, status="not_started", output={}, narrative="", rows=[], facts={})
    if not shown:
        return view
    view.update(version=shown["version"], status=shown["status"], narrative=shown.get("narrative") or "", output=shown.get("output") or {},
                progress=shown.get("progress"), created_at=shown.get("created_at"))
    if shown["status"] in ("drafted", "accepted"):
        view["rows"], view["facts"] = _rows(step, shown["output"] or {}, up_row)
    return view


def _benefit(ctype, o):
    """What an option does for the problem it answers, in words: capacity relief, fault risk, interruptions or voltage."""
    if (o.get("option_type") or "") == "defer":
        return "Monitoring only"
    if ctype in CAPACITY_TYPES:
        return f"{o['relief_mw']:.1f} MW relief" if o.get("relief_mw") else "No capacity relief"
    if ctype in ("feeder_asset_risk", "transformer_asset_risk"):
        return f"Fault risk down {o['asset_risk_reduction_pct']:.0f}%" if o.get("asset_risk_reduction_pct") else "No change to the fault risk"
    if ctype == "feeder_reliability":
        return f"Interruptions down {o['reliability_gain_pct']:.0f}%" if o.get("reliability_gain_pct") else "No change to interruptions"
    if ctype == "feeder_voltage":
        return f"Voltage held for {o['customers_protected']:,} customers" if o.get("customers_protected") else "No change to voltage"
    return "-"


def _bundle_row(b, i, caps, labels):
    return dict(id=b["bundle_id"], feeder=b["feeder_name"], zone=b["zs_name"], region=b["region_id"], isolation=b.get("isolation_label") or b["isolation_point"],
                isolation_basis=b.get("iso_basis"), works=b["job_type_labels"], items=b["job_count"], crew_hours=b["crew_hours"],
                outage_minutes=b["outage_minutes"], customers=b["customers_interrupted"], life_support=b["life_support_interrupted"],
                package=(i or {}).get("package") or "", cost=_cost(b), checks=bundle_checks(b, caps.get(b["region_id"]), labels))


def _rows(step, out, up_row):
    """The table the screen shows for a step: the marts' facts with the model's decisions beside them."""
    items = {i["id"]: i for i in out.get("items", [])}
    if step == "forecast":
        rows = [dict(z, pressure=items.get(z["id"], {}).get("pressure"), why=items.get(z["id"], {}).get("why")) for z in _zones()]
        rows.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}.get(r.get("pressure"), 3), r["first_over_fy"] or 99))
        return rows, dict(connections_synced=_exists("gld_connection_application"))
    if step == "constraints":
        rows = []
        for c in _constraint_rows():
            d = items.get(c["constraint_id"])
            rows.append(dict(id=c["constraint_id"], type=c["constraint_type_label"], zone=c["zs_name"], feeder=c["feeder_name"], summary=c["summary"],
                             value=_num(c["metric_value"]), unit=c["metric_unit"], years_to_limit=c["years_to_limit"], severity=c["severity"],
                             customers=c["customers"], carry=(d or {}).get("carry", False), option_ids=(d or {}).get("option_ids", []), why=(d or {}).get("why", "")))
        return rows, {}
    if step == "optimise":
        allopt = {o["intervention_id"]: dict(_option_brief(o), option_type=o["option_type"]) for o in _option_rows([i["id"] for i in out.get("items", [])])}
        cons = {c["constraint_id"]: c for c in _constraint_rows(500)}
        rows = []
        for i in out.get("items", []):
            ctype = (cons.get(i["id"]) or {}).get("constraint_type")
            scores = []
            for sc in i.get("scores", []):
                o = allopt.get(sc["option_id"], {})
                scores.append(dict(sc, option=o.get("option"), cost_10yr_aud=o.get("cost_10yr_aud"), benefit=_benefit(ctype, o) if o else "-",
                                   carbon_tco2e_10yr=o.get("carbon_tco2e_10yr"), lead_time_months=o.get("lead_time_months")))
            rows.append(dict(id=i["id"], recommended=allopt.get(i["recommended_option_id"], {}).get("option"), recommended_id=i["recommended_option_id"],
                             why=i.get("why"), summary=(cons.get(i["id"]) or {}).get("summary", ""), scores=scores))
        return rows, dict(weights=WEIGHTS)
    if step == "bundles":
        allb = {b["bundle_id"]: b for b in _bundles()}
        caps, labels = _caps(), _ref_labels("bundle_check")
        rows = [_bundle_row(allb[i["id"]], i, caps, labels) for i in out.get("items", []) if i["id"] in allb]
        keys = sorted(p["key"] for p in out.get("packages") or [])
        by_key = {k: [allb[r["id"]] for r in rows if r["package"] == k] for k in keys}
        options, rec = scenario_facts(by_key, caps, _start())
        chosen = out.get("scenario") if out.get("scenario") in [o["id"] for o in options] else rec
        for o in options:
            o.pop("placed")
            o["selected"] = o["id"] == chosen
        packs = []
        for k in keys + [""]:
            members = [r for r in rows if r["package"] == k]
            if k == "" and not members:
                continue
            packs.append(dict(key=k, label=f"Package {k}" if k else "Not in a package", bundles=len(members), items=sum(r["items"] or 0 for r in members),
                              crew_hours=round(sum(r["crew_hours"] or 0 for r in members), 1), customers=sum(r["customers"] for r in members),
                              cost=_sum_costs(members)))
        return rows, dict(packages=packs, package_keys=keys, options=options, recommended=rec, scenario=chosen, start=_start().isoformat(),
                          cost_labels=_ref_labels("plan_cost"))
    return [], {}


# ------------------------------------------------------------------ release: the locked window, the planner's choice and the notices
def _window(day, outage_minutes):
    """The planned interruption: from 8:00 am for the outage, rounded up to the half hour (Sydney time)."""
    start = dt.datetime.combine(day, dt.time(0), tzinfo=SYD) + dt.timedelta(minutes=OUTAGE_START_MIN)
    return start, start + dt.timedelta(minutes=int(math.ceil(max(60, outage_minutes or 0) / 30.0) * 30))


def release_preview(run_id):
    """What the release would send to the field now: the locked near-term window of the chosen delivery option, each bundle with its
    planned outage, notice date, estimated cost, the planner's keep or leave-out choice and its customer notice."""
    run = _run(run_id)
    st = run_state(run_id)
    if not next(x for x in st["steps"] if x["key"] == "release")["enabled"]:
        raise ValueError("Complete the steps before releasing the plan.")
    out = _accepted(run_id, "bundles")["output"] or {}
    allb = {b["bundle_id"]: b for b in _bundles()}
    caps, labels = _caps(), _ref_labels("bundle_check")
    rows = [_bundle_row(allb[i["id"]], i, caps, labels) for i in out.get("items", []) if i["id"] in allb]
    keys = sorted(p["key"] for p in out.get("packages") or [])
    by_key = {k: [allb[r["id"]] for r in rows if r["package"] == k] for k in keys}
    options, rec = scenario_facts(by_key, caps, _start())
    key = out.get("scenario") if out.get("scenario") in [o["id"] for o in options] else rec
    opt = next((o for o in options if o["id"] == key), None)
    if not opt:
        raise ValueError("No package has any bundles. Put bundles in a package first.")
    pkg = {r["id"]: r["package"] for r in rows}
    nd = notice_days()
    sel = {r["bundle_id"]: r for r in pg.q("SELECT bundle_id, included, notice_id FROM atlas.planning_selection WHERE run_id = %s", (run_id,))}
    nids = [r["notice_id"] for r in sel.values() if r["notice_id"]]
    notices = {n["notice_id"]: n for n in pg.q("SELECT notice_id, status, send_at FROM atlas.notice WHERE notice_id = ANY(%s)", (nids,))} if nids else {}
    status_label = _ref_labels("notice_status")
    locked = []
    for p in sorted([p for p in opt["placed"] if p["week"] < LOCKED_WEEKS], key=lambda p: (p["outage_date"], p["bundle_id"])):
        b = allb[p["bundle_id"]]
        od = dt.date.fromisoformat(p["outage_date"])
        start, end = _window(od, b["outage_minutes"])
        s = sel.get(b["bundle_id"]) or {}
        n = notices.get(s.get("notice_id")) if s.get("notice_id") else None
        locked.append(dict(bundle_id=b["bundle_id"], package=pkg.get(b["bundle_id"]), feeder=b["feeder_name"], feeder_id=b["feeder_id"],
                           zone=b["zs_name"], region=b["region_id"], isolation=b.get("isolation_label") or b["isolation_point"],
                           isolation_point=b["isolation_point"], works=b["job_type_labels"], work_items=b["job_count"],
                           outage_date=od.isoformat(), window=NT.window_label(start, end), window_start=start.isoformat(), window_end=end.isoformat(),
                           notice_by=_business_days_before(od, nd).isoformat(),
                           customers=b["customers_interrupted"], life_support=b["life_support_interrupted"], crew_hours=p["crew_hours"],
                           crews_needed=max(1, math.ceil((p["crew_hours"] or 0) / CREW_DAY_HOURS)), outage_minutes=b["outage_minutes"],
                           cost=_cost(b), included=s.get("included", True),
                           notice=dict(id=n["notice_id"], status=n["status"], label=status_label.get(n["status"], n["status"]),
                                       send_at=NT.window_label(NT._as_dt(n["send_at"])) if n.get("send_at") else None) if n else None))
    chosen = [x for x in locked if x["included"]]
    return dict(run_id=run_id, title=run["title"], status=run["status"], scenario=key, scenario_label=opt["label"], start=_start().isoformat(),
                notice_days=nd, locked=locked, rolling_bundles=sum(1 for p in opt["placed"] if p["week"] >= LOCKED_WEEKS), unscheduled=opt["unscheduled"],
                saved_at=run["saved_at"], totals=dict(items=len(chosen), customers=sum(x["customers"] for x in chosen),
                                                      life_support=sum(x["life_support"] for x in chosen),
                                                      crew_hours=round(sum(x["crew_hours"] or 0 for x in chosen), 1), cost=_sum_costs(chosen),
                                                      notices=sum(1 for x in chosen if x["notice"])),
                cost_labels=_ref_labels("plan_cost"))


def save_selection(run_id, included, actor):
    """Save and continue later: the planner's keep or leave-out choice for each bundle of the locked window, and the programme marked saved."""
    if _run(run_id)["status"] == "released":
        raise ValueError("This plan is already released.")
    for bid, inc in (included or {}).items():
        pg.q("""INSERT INTO atlas.planning_selection (run_id, bundle_id, included, updated_by) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, bundle_id) DO UPDATE SET included = EXCLUDED.included, updated_by = EXCLUDED.updated_by, updated_at = now()""",
             (run_id, str(bid), bool(inc), actor))
    pg.q("UPDATE atlas.planning_run SET saved_at = now(), saved_by = %s, updated_at = now() WHERE run_id = %s", (actor, run_id))
    core.audit(actor, "planning_selection_save", run_id, {"bundles": len(included or {})})
    return release_preview(run_id)


_prepare_lock = threading.Lock()   # one notice preparation at a time, so a second press cannot prepare a bundle's notice twice


def prepare_notices(run_id, bundle_ids, actor):
    """Prepare and schedule the customer notice of each chosen bundle's planned outage (the notices engine, notify.prepare_plan_notice):
    who it reaches, the text for each channel, and the send time on its notice date. A bundle that already has one keeps it."""
    with _prepare_lock:
        return _prepare_notices(run_id, bundle_ids, actor)


def _prepare_notices(run_id, bundle_ids, actor):
    prev = release_preview(run_id)
    want = set(bundle_ids or [x["bundle_id"] for x in prev["locked"] if x["included"]])
    made, kept = [], 0
    for x in prev["locked"]:
        if x["bundle_id"] not in want:
            continue
        if x["notice"] or pg.q("""SELECT 1 AS x FROM atlas.notice WHERE plan_run_id = %s AND plan_bundle_id = %s
                                  AND status IN ('draft', 'scheduled', 'sent') LIMIT 1""", (run_id, x["bundle_id"]), one=True):
            kept += 1
            continue
        nid = NT.prepare_plan_notice(dict(x, run_id=run_id, title=prev["title"]), actor)
        pg.q("""INSERT INTO atlas.planning_selection (run_id, bundle_id, included, notice_id, updated_by) VALUES (%s, %s, true, %s, %s)
                ON CONFLICT (run_id, bundle_id) DO UPDATE SET notice_id = EXCLUDED.notice_id, included = true, updated_by = EXCLUDED.updated_by,
                updated_at = now()""", (run_id, x["bundle_id"], nid, actor))
        made.append(nid)
    core.audit(actor, "planning_prepare_notices", run_id, {"prepared": len(made), "kept": kept})
    return dict(prepared=len(made), kept=kept, preview=release_preview(run_id))


def release(run_id, actor, bundle_ids=None, with_notices=True):
    """Release and schedule the plan: each chosen bundle of the locked window is raised as a field job in resource management on its outage
    day (rm.dispatch.raise_job), its customer notice is prepared and scheduled if it has none, and the release and its items are recorded
    for the Released Plans board. A bundle that cannot be raised does not stop the others."""
    run = _run(run_id)
    if run["status"] == "released":
        raise ValueError("This plan is already released.")
    prev = release_preview(run_id)
    want = set(bundle_ids) if bundle_ids is not None else {x["bundle_id"] for x in prev["locked"] if x["included"]}
    if not want:
        raise ValueError("Choose at least one bundle to release.")
    if bundle_ids is not None:
        save_selection(run_id, {x["bundle_id"]: x["bundle_id"] in want for x in prev["locked"]}, actor)
    if with_notices:
        prev = prepare_notices(run_id, sorted(want), actor)["preview"]
    chosen = [x for x in prev["locked"] if x["bundle_id"] in want]
    if not with_notices:   # released without customer notices: any prepared for these outages are withdrawn before they go
        NT.delete_notices([n["notice_id"] for n in pg.q("""SELECT notice_id FROM atlas.notice WHERE plan_run_id = %s AND plan_bundle_id = ANY(%s)
                                                          AND status <> 'sent'""", (run_id, sorted(want)))])
        pg.q("UPDATE atlas.planning_selection SET notice_id = NULL WHERE run_id = %s AND bundle_id = ANY(%s)", (run_id, sorted(want)))
        for x in chosen:
            if (x["notice"] or {}).get("status") != "sent":
                x["notice"] = None
    rid = "REL-" + uuid.uuid4().hex[:8].upper()
    specs = RD.ensure()["specs"]
    allb = {b["bundle_id"]: b for b in _bundles()}
    raised, failed = [], []
    for x in chosen:
        b = allb[x["bundle_id"]]
        jt = b["primary_job_type"] if b["primary_job_type"] in specs else "LV_MAINT"
        title = f"Planned works on {b['feeder_name']}: {b['job_type_labels']}, isolation at {b['isolation_point']}"[:200]
        od = dt.date.fromisoformat(x["outage_date"])
        start, end = _window(od, b["outage_minutes"])
        job_id = f"JOB-PLN-{rid}-{b['bundle_id']}"[:90]
        try:
            RX.raise_job(jt, title, float(b["lat"]), float(b["lon"]), actor, feeder_id=b["feeder_id"], asset_id=b["isolation_point"],
                         priority=max(3, min(5, int(b["priority"] or 3))), customers=b["customers_interrupted"], source="plan",
                         duration_min=max(60, min(600, int(round((x["crew_hours"] or 1) * 60)))), job_id=job_id, job_date=od,
                         window=(OUTAGE_START_MIN, OUTAGE_START_MIN + 600))
            raised.append(job_id)
        except Exception as e:  # noqa: BLE001 - one bundle that cannot be raised does not stop the others; the planner sees it
            failed.append(dict(bundle_id=b["bundle_id"], error=str(e)[:200]))
            job_id = None
        c = x["cost"]
        pg.q("""INSERT INTO atlas.planning_release_item (release_id, bundle_id, run_id, package_key, region_id, feeder_id, feeder_name, zs_name,
                    isolation_point, isolation_label, work_types, work_items, outage_date, outage_start, outage_end, notice_by, crew_hours, crews_needed,
                    customers, life_support, cost_labour_aud, cost_materials_aud, cost_plant_aud, cost_traffic_aud, cost_switching_aud,
                    cost_customer_aud, cost_contingency_aud, cost_total_aud, job_id, notice_id, primary_job_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
             (rid, b["bundle_id"], run_id, x["package"], b["region_id"], b["feeder_id"], b["feeder_name"], b["zs_name"], b["isolation_point"],
              x["isolation"], b["job_type_labels"], b["job_count"], od, start, end, x["notice_by"], x["crew_hours"], x["crews_needed"],
              x["customers"], x["life_support"], c["labour"], c["materials"], c["plant"], c["traffic"], c["switching"], c["customer"],
              c["contingency"], c["total"], job_id, (x["notice"] or {}).get("id"), jt))
    t = _sum_costs(chosen)
    dates = [x["outage_date"] for x in chosen]
    payload = dict(raised=raised, failed=failed, scenario=prev["scenario"], left_out=sorted({x["bundle_id"] for x in prev["locked"]} - want))
    pg.q("""INSERT INTO atlas.planning_release (release_id, run_id, released_by, scenario_key, scenario_label, title, jobs_raised, job_ids, payload,
                status, items, customers, life_support, crew_hours, cost_total_aud, first_outage, last_outage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'released', %s, %s, %s, %s, %s, %s, %s)""",
         (rid, run_id, actor, prev["scenario"], prev["scenario_label"], run["title"], len(raised), json.dumps(raised), json.dumps(payload),
          len(chosen), sum(x["customers"] for x in chosen), sum(x["life_support"] for x in chosen), round(sum(x["crew_hours"] or 0 for x in chosen), 1),
          t["total"], min(dates), max(dates)))
    nids = [x["notice"]["id"] for x in chosen if x["notice"]]
    if nids:
        pg.q("UPDATE atlas.notice SET release_id = %s WHERE notice_id = ANY(%s)", (rid, nids))
    pg.q("UPDATE atlas.planning_run SET status = 'released', released_at = now(), released_by = %s, release_id = %s, updated_at = now() WHERE run_id = %s",
         (actor, rid, run_id))
    core.audit(actor, "planning_release", rid, dict(run=run_id, items=len(chosen), jobs=len(raised), failed=len(failed), cost=t["total"]))
    roster(rid)
    return dict(release_id=rid, items=len(chosen), jobs_raised=len(raised), failed=failed, notices=len(nids), cost_total_aud=t["total"])


# ------------------------------------------------------------------ the Released Plans board
def roster(release_id=None):
    """Roster crews to released work once its day is within the roster horizon (seven days ahead): the crews of the item's region who hold
    the skill its work needs, least loaded that day first, as many as the work needs to finish on the day."""
    horizon = today() + dt.timedelta(days=ROSTER_DAYS)
    due = pg.q("""SELECT release_id, bundle_id, region_id, outage_date, crews_needed, primary_job_type FROM atlas.planning_release_item
                  WHERE crew_ids IS NULL AND outage_date BETWEEN %s AND %s AND (%s::text IS NULL OR release_id = %s)
                  ORDER BY outage_date, bundle_id""", (today(), horizon, release_id, release_id))
    if not due:
        return 0
    crews = pq("SELECT crew_id, crew_name, region_id FROM gld_crew WHERE active ORDER BY crew_id", ttl=600)
    skills = {}
    for r in pq("SELECT crew_id, skill_code FROM gld_crew_skill", ttl=600):
        skills.setdefault(r["crew_id"], set()).add(r["skill_code"])
    specs = RD.ensure()["specs"]
    load = {}
    for r in pg.q("""SELECT outage_date, unnest(crew_ids) AS crew_id FROM atlas.planning_release_item WHERE crew_ids IS NOT NULL
                     AND outage_date BETWEEN %s AND %s""", (today(), horizon)):
        k = (r["crew_id"], str(r["outage_date"]))
        load[k] = load.get(k, 0) + 1
    for i in due:
        need = (specs.get(i["primary_job_type"]) or {}).get("required_skill")
        pool = [c for c in crews if c["region_id"] == i["region_id"] and (not need or need in skills.get(c["crew_id"], set()))] or \
            [c for c in crews if c["region_id"] == i["region_id"]]
        pool.sort(key=lambda c: (load.get((c["crew_id"], str(i["outage_date"])), 0), c["crew_id"]))
        pick = pool[:max(1, int(i["crews_needed"] or 1))]
        for c in pick:
            load[(c["crew_id"], str(i["outage_date"]))] = load.get((c["crew_id"], str(i["outage_date"])), 0) + 1
        pg.q("""UPDATE atlas.planning_release_item SET crew_ids = %s, crew_names = %s, rostered_at = now()
                WHERE release_id = %s AND bundle_id = %s""", ([c["crew_id"] for c in pick], [c["crew_name"] for c in pick], i["release_id"], i["bundle_id"]))
    return len(due)


def _item_status(i, field):
    """Where an item of a released plan is: upcoming (booked, crews not yet rostered), on the roster, in the field (its day, or a crew on
    it), or complete (done in the field, or its day has passed)."""
    d = i["outage_date"] if isinstance(i["outage_date"], dt.date) else dt.date.fromisoformat(str(i["outage_date"]))
    if field == "done" or d < today():
        return "complete"
    if d == today() or field in ("driving", "in_progress"):
        return "in_field"
    return "rostered" if i.get("crew_ids") else "upcoming"


def released_plans():
    """Every released plan, newest first, with its headline figures and how many of its items are at each stage."""
    roster()
    rels = pg.q("""SELECT release_id, run_id, title, scenario_label, released_by, released_at, items, customers, life_support, crew_hours,
                          cost_total_aud, first_outage, last_outage FROM atlas.planning_release ORDER BY released_at DESC""")
    items = pg.q("""SELECT i.release_id, i.outage_date, i.feeder_id, i.crew_ids, f.status AS field FROM atlas.planning_release_item i
                    LEFT JOIN rm.field_status f ON f.job_id = i.job_id""")
    by, spread = {}, {}
    for i in items:
        sp = spread.setdefault(i["release_id"], (set(), set()))   # the feeders and days its outages fall on
        sp[0].add(i["feeder_id"]); sp[1].add(i["outage_date"])
        c = by.setdefault(i["release_id"], {})
        s = _item_status(i, i["field"])
        c[s] = c.get(s, 0) + 1
    for r in rels:
        r["stages"] = by.get(r["release_id"], {})
        f, days = spread.get(r["release_id"], (set(), set()))
        r["feeders"], r["days"] = len(f), len(days)
        r["cost_total_aud"] = _num(r["cost_total_aud"])
        r["crew_hours"] = _num(r["crew_hours"])
    return dict(plans=rels, labels=_ref_labels("plan_item_status"))


def released_plan(release_id):
    """One released plan: its figures, the cost by category, package and region, and every item on the board by stage (upcoming, on the
    roster, in the field, complete) with its day, crews and customer notice."""
    roster(release_id)
    r = pg.q("SELECT * FROM atlas.planning_release WHERE release_id = %s", (release_id,), one=True)
    if not r:
        raise ValueError(f"No such released plan: {release_id}.")
    items = pg.q("""SELECT i.*, f.status AS field, n.status AS notice_status, n.send_at AS notice_send_at, n.sent_at AS notice_sent_at
                    FROM atlas.planning_release_item i
                    LEFT JOIN rm.field_status f ON f.job_id = i.job_id
                    LEFT JOIN atlas.notice n ON n.notice_id = i.notice_id
                    WHERE i.release_id = %s ORDER BY i.outage_date, i.feeder_name, i.bundle_id""", (release_id,))
    regions = {x["region_id"]: x["name"] for x in pq("SELECT region_id, name FROM gld_rm_region", ttl=600)}
    status_label, notice_label = _ref_labels("plan_item_status"), _ref_labels("notice_status")
    cats = {p: 0.0 for p in COST_PARTS}
    by_pkg, by_region = {}, {}
    out = []
    for i in items:
        cost = {p: _num(i[f"cost_{p}_aud"]) or 0.0 for p in COST_PARTS}
        total = _num(i["cost_total_aud"]) or 0.0
        for p in COST_PARTS:
            cats[p] += cost[p]
        by_pkg[i["package_key"] or ""] = by_pkg.get(i["package_key"] or "", 0.0) + total
        rn = regions.get(i["region_id"], i["region_id"])
        by_region[rn] = by_region.get(rn, 0.0) + total
        s = _item_status(i, i["field"])
        start = NT._as_dt(i["outage_start"])
        out.append(dict(bundle_id=i["bundle_id"], package=i["package_key"], feeder=i["feeder_name"], zone=i["zs_name"], region=rn,
                        isolation=i["isolation_label"], works=i["work_types"], work_items=i["work_items"], outage_date=str(i["outage_date"]),
                        window=NT.window_label(start, NT._as_dt(i["outage_end"])), crew_hours=_num(i["crew_hours"]),
                        crews_needed=i["crews_needed"], crews=i["crew_names"] or [], customers=i["customers"], life_support=i["life_support"],
                        cost=round(total), stage=s, stage_label=status_label.get(s, s), job_id=i["job_id"],
                        notice=dict(id=i["notice_id"], status=i["notice_status"], label=notice_label.get(i["notice_status"], i["notice_status"]),
                                    send_at=NT.window_label(NT._as_dt(i["notice_send_at"])) if i["notice_send_at"] else None)
                        if i["notice_id"] and i["notice_status"] else None))
    plan = {k: v for k, v in r.items() if k not in ("payload", "job_ids")}
    plan["cost_total_aud"] = _num(plan["cost_total_aud"])
    plan["crew_hours"] = _num(plan["crew_hours"])
    cost_labels = _ref_labels("plan_cost")
    return dict(plan=plan, items=out, stages=[dict(key=k, label=status_label.get(k, k)) for k in ("upcoming", "rostered", "in_field", "complete")],
                cost=dict(categories=[dict(key=p, label=cost_labels.get(p, p), aud=round(cats[p])) for p in COST_PARTS],
                          packages=[dict(key=k, label=f"Package {k}" if k else "Not in a package", aud=round(v)) for k, v in sorted(by_pkg.items())],
                          regions=[dict(label=k, aud=round(v)) for k, v in sorted(by_region.items(), key=lambda x: -x[1])],
                          total=round(sum(i["cost"] for i in out))),
                roster_days=ROSTER_DAYS, notices=sum(1 for i in out if i["notice"]))
