"""Planning: each region's plan (one solve, the same in the Control Room and the field), each crew's field day, where every crew is
right now, and the executive view."""
import datetime as dt
import hashlib

from . import data as D
from . import store
from .solver import km

DAY_SHIFT, DAY_SPEED, DAY_END, DAY_MIN, DAY_MAX = 420, 42.0, 1020, 3, 8   # 07:00 start, stop past about 17:00, 3 to 8 stops


def parked_ids(region):
    return {p["job_id"] for p in store.parked_list(region)}


def awaiting(region, moves=None):
    """Crews moved into this region awaiting a deliberate dispatch: the auto-solve leaves them empty until then."""
    mv = moves if moves is not None else store.crew_region_all()
    return {cid for cid, p in mv.items() if p.get("region") == region and p.get("awaiting")}


def allow_sets(region, pol=None):
    """The supervisor's planning exceptions as the solver's two sets: open-incident crews allowed back in, crews allowed overtime."""
    pol = (store.policy_get(region) if pol is None else pol) or {}
    e = D.eng()
    in_region = {c["crew_id"] for c in D.data()["crews"] if c["region_id"] == region}
    incident = in_region & e.incident
    ai = set(incident) if pol.get("incident_all") else set(pol.get("incident_allow") or []) & incident
    af = set(in_region) if pol.get("fatigue_all") else set(pol.get("fatigue_allow") or [])
    return ai, af


def apply_overrides(region, roster):
    """The roster with the persisted manual moves laid over it, as a copy, so a cached solve stays as solved."""
    ov = store.override_map(region)
    return dict(roster, assignments=[dict(a, **ov[a["job_id"]]) if a["job_id"] in ov else a for a in roster["assignments"]])


def _solve(region, pol=None, moves=None, parked=None):
    """The region's plan, solved with its exceptions, the jobs on hold and the crews awaiting a deliberate dispatch left out."""
    ai, af = allow_sets(region, pol)
    return D.eng().solve(region, allow_incident=ai, allow_fatigue=af, exclude_jobs=parked_ids(region) if parked is None else parked,
                         exclude_crew=awaiting(region, moves))


def solve(region):
    """Solve the region's plan again (the Control Room's view of it, which is also what the field works) and keep it for the screens."""
    r = _solve(region)
    D.ensure()["rosters"][region] = r
    return apply_overrides(region, r)


def live_roster(region):
    r = D.ensure()["rosters"].get(region) or solve(region)
    return apply_overrides(region, r)


def field_roster(region):
    """What the field works for a region: the plan solved now, with the region's exceptions and the manual overrides."""
    return apply_overrides(region, _solve(region))


def is_fixed(job):
    """A booked customer appointment or callout with a window the crew must hit; an emergency is done now, so it is not locked."""
    return job["job_type"] in ("CUSTOMER_CONNECTION", "FAULT_RESPONSE") and not job.get("is_emergency")


def _trim_day(stops, crew, jobs):
    out, clock, lat, lon, fixed_used = [], float(DAY_SHIFT), crew["home_lat"], crew["home_lon"], 0
    for a in sorted(stops, key=lambda x: not x.get("is_emergency")):   # an emergency is the first stop
        src = jobs.get(a["job_id"], a)
        fixed = is_fixed(src)
        if fixed and fixed_used >= 1:   # at most one booked appointment a day, so two can never clash on one route
            continue
        start = clock + km(lat, lon, a["lat"], a["lon"]) / DAY_SPEED * 60.0
        if a.get("is_emergency"):
            start = max(start, D.now_min())
        if fixed:
            start = max(start, src.get("window_start_min", DAY_SHIFT))
        finish = start + a["duration_min"]
        home = km(a["lat"], a["lon"], crew["home_lat"], crew["home_lon"]) / DAY_SPEED * 60.0
        if out and len(out) >= DAY_MIN and finish + home > DAY_END and not a.get("is_emergency"):
            break
        out.append(a)
        fixed_used += fixed
        clock, lat, lon = finish, a["lat"], a["lon"]
        if len(out) >= DAY_MAX:
            break
    return out


def field_days(region, key=None):
    """Each crew's field day, trimmed once from the field roster and cached against the region's, crews' and jobs' revisions, so
    every surface (and every app process) serves the same day. Each crew's planned minutes go to app.crew_day for the detector."""
    S = D.ensure()
    key = key or store.revs(region, "__crews__", "__jobs__")
    ent = S["field_days"].get(region)
    if ent and ent[0] == key:
        return ent[1]
    r = field_roster(region)
    jobs, e = D.jobs_by_id(), D.eng()
    by = {}
    for a in r["assignments"]:
        by.setdefault(a["crew_id"], []).append(a)
    days = {cid: _trim_day(stops, e.crews[cid], jobs) for cid, stops in by.items() if cid in e.crews}
    S["field_days"][region] = (key, days)
    rows = []
    for cid, stops in days.items():
        t = schedule(e.crews[cid], stops)
        rows.append(dict(crew_id=cid, planned_min=round(t[-1]["finish"] - DAY_SHIFT if t else 0, 1)))
    store.crew_day_put(region, rows)
    return days


def schedule(crew, stops):
    """Arrive, start and finish for a crew's stops in order, from 07:00, at the planning speed; booked appointments wait for their window."""
    out, t, lat, lon, jobs = [], float(DAY_SHIFT), crew["home_lat"], crew["home_lon"], D.jobs_by_id()
    for a in stops:
        arrive = t + km(lat, lon, a["lat"], a["lon"]) / DAY_SPEED * 60.0
        src = jobs.get(a["job_id"], a)
        if a.get("is_emergency"):   # an emergency is worked now, whatever the plan said earlier
            arrive = max(arrive, D.now_min())
        start = max(arrive, src.get("window_start_min", DAY_SHIFT)) if is_fixed(src) else arrive
        t = start + a["duration_min"]
        out.append(dict(job_id=a["job_id"], arrive=arrive, start=start, finish=t, from_lat=lat, from_lon=lon,
                        depart=arrive - km(lat, lon, a["lat"], a["lon"]) / DAY_SPEED * 60.0))
        lat, lon = a["lat"], a["lon"]
    return out


STATUS = {"not_started": "Not on shift yet", "travelling": "Travelling", "on_site": "On site", "available": "Available",
          "finished": "Day finished", "stood_down": "Stood down", "leave": "On leave", "training": "In training",
          "incident": "Off the roster (open safety incident)"}


def positions(region=None):
    """Where every crew is now: driven by the live engine when dispatched to an emergency, otherwise from its field day and the
    Sydney clock (at home before 07:00, on a leg or on site through the day, home again when the day is done)."""
    e, now = D.eng(), D.now_min()
    regs = [region] if region else e.regions
    live, revs = store.crew_live_all(), store.revs(*regs, "__crews__", "__jobs__")
    sd_all, fs_all = {}, {}
    for reg, cid, rec in store.stood_down_all():
        sd_all.setdefault(reg, {})[cid] = rec
    for r in store.field_status_all():
        fs_all.setdefault(r["region_id"], {})[r["job_id"]] = r["status"]
    jobs, out = D.jobs_by_id(), []
    for i, reg in enumerate(regs):
        days, sd, fs = field_days(reg, (revs[i],) + revs[-2:]), sd_all.get(reg, {}), fs_all.get(reg, {})
        for c in (c for c in D.data()["crews"] if c["region_id"] == reg):
            cid, stops = c["crew_id"], days.get(c["crew_id"], [])
            p = dict(crew_id=cid, crew_name=c["crew_name"], persona=c["persona"], region_id=reg, employment_type=c["employment_type"],
                     lat=c["home_lat"], lon=c["home_lon"], status="available", job_id=None, stops=len(stops),
                     done=sum(1 for a in stops if fs.get(a["job_id"]) == "done"))
            lv = live.get(cid)
            if cid in sd:
                p["status"] = "stood_down"
            elif cid in e.on_leave:
                p["status"] = "leave"
            elif cid in e.in_training:
                p["status"] = "training"
            elif lv and lv["status"] in ("travelling", "on_site"):
                p.update(lat=lv["lat"], lon=lv["lon"], status=lv["status"], job_id=lv["job_id"])
            elif lv:
                p.update(lat=lv["lat"], lon=lv["lon"])
            elif stops:   # an emergency counts once a crew is actually sent to it (the live engine then drives it)
                p.update(_where(c, [a for a in stops if not a.get("is_emergency") or fs.get(a["job_id"]) in ("driving", "in_progress", "done")], now))
            elif cid in e.incident:
                p["status"] = "incident"
            p["status_label"] = STATUS[p["status"]]
            j = jobs.get(p["job_id"]) if p["job_id"] else None
            p["job_title"] = j["title"] if j else None
            out.append(p)
    return out


def _where(crew, stops, now):
    if now < DAY_SHIFT:
        return dict(status="not_started")
    for s, a in zip(schedule(crew, stops), stops):
        if now < s["arrive"]:   # on the leg to this stop
            f = max(0.0, min(1.0, (now - s["depart"]) / max(1e-6, s["arrive"] - s["depart"])))
            return dict(status="travelling", job_id=a["job_id"], lat=s["from_lat"] + (a["lat"] - s["from_lat"]) * f,
                        lon=s["from_lon"] + (a["lon"] - s["from_lon"]) * f)
        if now < s["finish"]:
            return dict(status="on_site", job_id=a["job_id"], lat=a["lat"], lon=a["lon"])
    return dict(status="finished")


def hours_on_shift(p):
    """Hours since the 07:00 start for a crew working today (none for a crew with no work, or off the roster)."""
    if not p or p.get("status") in ("not_started", "leave", "training", "stood_down", "incident") or (p.get("status") == "available" and not p.get("stops")):
        return 0.0
    return round(max(0.0, D.now_min() - DAY_SHIFT) / 60, 1)


# ---------------------------------------------------------------- executive view: every region's actual field plan
def exec_summary():
    S = D.ensure()
    grev = store.revs("__global__", "__crews__", "__jobs__")
    if S.get("exec_cache") and S["exec_cache"][0] == grev:
        return S["exec_cache"][1]
    policies, moves = store.policy_all(), store.crew_region_all()
    parked = {}
    for p in store.parked_list():
        parked.setdefault(p["region"], set()).add(p["job_id"])
    e, rows, teams = D.eng(), [], {}
    for region in e.regions:
        r = _solve(region, policies.get(region) or {}, moves, parked.get(region) or set())
        k = r["kpis"]
        rows.append(dict(region=region, region_name=D.region_name(region),
                         **{x: k[x] for x in ("jobs_assigned", "jobs_deferred", "crew_deployed", "total_cost_aud", "total_travel_km",
                                              "deferred_customer_minutes")}))
        for a in r["assignments"]:
            t = teams.setdefault(e.crews[a["crew_id"]]["persona"], dict(jobs=0, cost=0.0, minutes=0.0, crews=set()))
            t["jobs"] += 1
            t["cost"] += a["cost_aud"]
            t["minutes"] += a["duration_min"] + a["travel_min"]
            t["crews"].add(a["crew_id"])
    # trades by work done per crew today, with what that work costs
    by_persona = sorted([dict(persona=p, jobs=t["jobs"], crews=len(t["crews"]), cost_aud=round(t["cost"]), cost_per_job=round(t["cost"] / t["jobs"]),
                              jobs_per_crew=round(t["jobs"] / len(t["crews"]), 1), hours_per_crew=round(t["minutes"] / 60 / len(t["crews"]), 1))
                         for p, t in teams.items()], key=lambda x: -x["jobs_per_crew"])
    rows.sort(key=lambda x: -x["total_cost_aud"])
    crew_n, cost = sum(x["crew_deployed"] for x in rows), sum(x["total_cost_aud"] for x in rows)
    totals = dict(jobs_assigned=sum(x["jobs_assigned"] for x in rows), jobs_deferred=sum(x["jobs_deferred"] for x in rows), crew_deployed=crew_n,
                  total_cost_aud=cost, total_travel_km=sum(x["total_travel_km"] for x in rows),
                  deferred_customer_minutes=sum(x["deferred_customer_minutes"] for x in rows))
    # Thirty days of network crew cost ending today: today is the solved plan; earlier days are modelled around it (a stable hash of
    # each date gives the day's variation, weekends are lighter, a mild upward trend), so the series is the same on every call.
    anchor, d0, history = float(totals["total_cost_aud"]), D.today(), []
    for back in range(29, -1, -1):
        day = d0 - dt.timedelta(days=back)
        h = int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16)
        val = anchor if back == 0 else anchor * (1 + ((h % 1000) / 1000 - 0.5) * 0.16 + (-0.28 if day.weekday() >= 5 else 0) + (29 - back) / 29 * 0.06)
        history.append(dict(date=day.isoformat(), cost_aud=round(val), is_today=back == 0, weekend=day.weekday() >= 5))
    out = dict(day=d0.isoformat(), regions=rows, totals=totals, by_persona=by_persona, cost_history=history)
    S["exec_cache"] = (grev, out)
    return out


def expiring(days=45):
    """Ticket risk: certifications already lapsed, and those expired or expiring inside the window, by ticket."""
    S = D.ensure()
    if "expiring" in S:
        return S["expiring"]
    by, expired = {}, 0
    for c in D.data()["certs"]:
        expired += bool(c["is_expired"])
        if c["is_expired"] or (c["days_to_expiry"] is not None and 0 <= c["days_to_expiry"] <= days):
            by[c["ticket_code"]] = by.get(c["ticket_code"], 0) + 1
    S["expiring"] = dict(window_days=days, expired_total=expired,
                         by_ticket=[dict(ticket_code=k, ticket=D.label("ticket", k), n=n) for k, n in sorted(by.items(), key=lambda x: -x[1])])
    return S["expiring"]


def expiring_detail(ticket_code=None, scope="at_risk", days=45, offset=0, limit=200):
    crews, items = D.eng().crews, []
    for c in D.data()["certs"]:
        if ticket_code and c["ticket_code"] != ticket_code:
            continue
        at_risk = c["is_expired"] or (c["days_to_expiry"] is not None and 0 <= c["days_to_expiry"] <= days)
        if (scope == "expired" and not c["is_expired"]) or (scope == "at_risk" and not at_risk):
            continue
        w = crews.get(c["crew_id"])
        if w:
            items.append(dict(crew_id=c["crew_id"], crew_name=w["crew_name"], persona=w["persona"], region=D.region_name(w["region_id"]),
                              employment_type=w["employment_type"], ticket_code=c["ticket_code"], ticket=D.label("ticket", c["ticket_code"]),
                              expiry_date=c["expiry_date"], days_to_expiry=c["days_to_expiry"], is_expired=bool(c["is_expired"])))
    items.sort(key=lambda x: (not x["is_expired"], x["days_to_expiry"] if x["days_to_expiry"] is not None else 1e9, x["crew_id"], x["ticket_code"]))
    page = items[max(0, offset):max(0, offset) + limit]
    return dict(ticket_code=ticket_code, scope=scope, window_days=days, total=len(items), offset=offset, returned=len(page),
                has_more=offset + len(page) < len(items), items=page)
