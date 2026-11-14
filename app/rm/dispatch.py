"""Dispatch: who can take a job, assigning it (with hand-offs, or a supervisor override flagged as a breach), holding and
re-dispatching, crews standing themselves down (their day re-rostered to the nearest qualified crews), moving crews between regions,
the planning exceptions, and the jobs other screens raise (an outage, a situation, the asset panel)
with an immediate dispatch."""
import time
import uuid

import core
import overdue
from core import pg
from . import data as D
from . import plan as P
from . import store
from .solver import km


def recost(a, crew, travel_min=None):
    """The job's crew cost follows the crew: its hourly rate times the on-site and travel time."""
    tmin = a.get("travel_min", 0) if travel_min is None else travel_min
    a["cost_aud"] = round(crew["hourly_cost_aud"] * (a["duration_min"] + tmin) / 60.0, 2)
    return a


def persist(region, a, **extra):
    fr = a.get("reassigned_from")
    store.override_put(region, a["job_id"], dict(
        crew_id=a["crew_id"], crew_name=a.get("crew_name"), employment_type=a.get("employment_type"), travel_km=a.get("travel_km"),
        travel_min=a.get("travel_min"), cost_aud=a.get("cost_aud"), reassigned_from=fr, reassigned_from_name=(D.crew(fr) or {}).get("crew_name") if fr else None,
        job_type=a.get("job_type"), override=a.get("override"), breach=a.get("breach"), ts=time.time(), **extra))


def _loads(roster):
    used = {}
    for a in roster["assignments"]:
        used[a["crew_id"]] = used.get(a["crew_id"], 0.0) + a["duration_min"] + a.get("travel_min", 0)
    return used


def _hhmm(m):
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def _job_view(region, job_id):
    """A job's place and attributes, from the roster if it is on it, else from the on-hold queue (parked jobs are off the solve)."""
    roster = P.live_roster(region)
    a = next((x for x in roster["assignments"] if x["job_id"] == job_id), None)
    parked = None if a else next((p for p in store.parked_list(region) if p["job_id"] == job_id), None)
    return roster, a, parked, D.jobs_by_id().get(job_id)


def candidates(region, job_id):
    """Crews who could take the job: qualified first, then by name; each with its load and distance from where it is now."""
    e = D.eng()
    roster, a, parked, src = _job_view(region, job_id)
    if not (a or parked or src):
        return dict(error="That job was not found.")
    base = a or parked or src
    cur = (a or {}).get("crew_id") or (parked or {}).get("from_crew_id")
    used, pos = _loads(roster), {p["crew_id"]: p for p in P.positions(region)}
    out = []
    for c in e.crew_for_region(region):
        if c["crew_id"] == cur:
            continue
        p = pos.get(c["crew_id"], c)
        d = km(p.get("lat", c["home_lat"]), p.get("lon", c["home_lon"]), base["lat"], base["lon"])
        out.append(dict(crew_id=c["crew_id"], crew_name=c["crew_name"], persona=c["persona"], employment_type=c["employment_type"],
                        eligible=e.eligible(src, c) if src else True, load_pct=round(min(100, used.get(c["crew_id"], 0.0) / e.cap_min(c) * 100)),
                        km=round(d, 1), eta_min=round(d / P.DAY_SPEED * 60), status=p.get("status_label")))
    out.sort(key=lambda x: (not x["eligible"], x["crew_name"].lower()))
    return dict(job=dict(job_id=job_id, job_type=base["job_type"], job_type_label=D.label("job_type", base["job_type"]), title=base.get("title"),
                         priority=base.get("priority", 3), duration_min=base["duration_min"],
                         required_skill=D.label("skill", (src or {}).get("required_skill")), required_ticket=D.label("ticket", (src or {}).get("required_ticket"))),
                current=dict(crew_id=cur, crew_name=(D.crew(cur) or {}).get("crew_name") if cur else None), candidates=out[:40])


def plan_assign(region, job_id, crew_id):
    """direct: fits the crew's day; handoffs: fits once some of the crew's movable stops go to others; infeasible: not qualified,
    or past the shift limit even after hand-offs."""
    e = D.eng()
    roster, a, parked, src = _job_view(region, job_id)
    tgt = e.crews.get(crew_id)
    if not (a or parked) or not tgt:
        return dict(ok=False, kind="infeasible", reason="That job or crew was not found.")
    base = a or parked
    info = dict(crew_id=crew_id, crew_name=tgt["crew_name"], persona=tgt["persona"])
    if src and not e.eligible(src, tgt):
        return dict(ok=False, kind="infeasible", target=info, reason=f"{tgt['crew_name']} is not qualified for this job (needs "
                    f"{D.label('skill', src['required_skill'])} and a valid {D.label('ticket', src['required_ticket'])} ticket).")
    used, cap = _loads(roster), e.cap_min(tgt)
    need = base["duration_min"] + km(tgt["home_lat"], tgt["home_lon"], base["lat"], base["lon"]) / P.DAY_SPEED * 60.0
    load = used.get(crew_id, 0.0)
    if load + need <= cap:
        return dict(ok=True, kind="direct", target=info, finish=_hhmm(P.DAY_SHIFT + load + need), limit=_hhmm(P.DAY_SHIFT + cap))
    jobs = D.jobs_by_id()
    movable = sorted([x for x in roster["assignments"] if x["crew_id"] == crew_id and not P.is_fixed(jobs.get(x["job_id"], x))],
                     key=lambda x: -(x["duration_min"] + x.get("travel_min", 0)))   # free the most time first
    others = [c for c in e.crew_for_region(region) if c["crew_id"] != crew_id]
    sim, handoffs, after = dict(used), [], load
    for x in movable:
        if after + need <= cap:
            break
        nx = x["duration_min"] + x.get("travel_min", 0)
        best, bd = None, 1e18
        for c in others:
            if e.eligible(jobs.get(x["job_id"], x), c) and sim.get(c["crew_id"], 0.0) + nx <= e.cap_min(c):
                d = km(c["home_lat"], c["home_lon"], x["lat"], x["lon"])
                if d < bd:
                    best, bd = c, d
        if best:
            handoffs.append(dict(job_id=x["job_id"], title=x.get("title"), job_type_label=D.label("job_type", x["job_type"]),
                                 to=dict(crew_id=best["crew_id"], crew_name=best["crew_name"], persona=best["persona"], km=round(bd, 1))))
            sim[best["crew_id"]] = sim.get(best["crew_id"], 0.0) + nx
            after -= nx
    if after + need <= cap:
        return dict(ok=True, kind="handoffs", target=info, handoffs=handoffs, finish=_hhmm(P.DAY_SHIFT + after + need), limit=_hhmm(P.DAY_SHIFT + cap))
    return dict(ok=False, kind="infeasible", target=info,
                reason=f"Even after handing off {tgt['crew_name']}'s movable stops, this job pushes the day past the {_hhmm(P.DAY_SHIFT + cap)} shift limit.")


def apply_assign(region, job_id, crew_id, handoffs=(), override=False, breach_reason=None, actor="supervisor"):
    """Commit the assignment and any approved hand-offs. An override force-assigns past a failed rule check and is flagged as a
    breach on the roster and in the audit log."""
    roster = P.live_roster(region)
    by_id = {a["job_id"]: a for a in roster["assignments"]}
    parked = {p["job_id"]: p for p in store.parked_list(region)}
    jobs, crews, changed, unparked = D.jobs_by_id(), D.eng().crews, [], []

    def move(jid, to, forced=False):
        c = crews.get(to)
        from_hold = jid in parked
        a = dict(by_id[jid]) if jid in by_id and not from_hold else None
        if a is None:
            p = parked.get(jid)
            if not p:
                return
            src = jobs.get(jid, {})
            a = dict(job_id=jid, job_type=p["job_type"], title=p.get("title"), priority=p.get("priority", 3), crew_id=p.get("from_crew_id"),
                     region_id=region, lat=p["lat"], lon=p["lon"], duration_min=p["duration_min"], funding=p.get("funding"),
                     est_customer_minutes=src.get("est_customer_minutes", 0), is_emergency=bool(src.get("is_emergency")))
        d = km(c["home_lat"], c["home_lon"], a["lat"], a["lon"])
        tmin = round(d / P.DAY_SPEED * 60.0)
        a.update(reassigned_from=a.get("crew_id"), crew_id=to, crew_name=c["crew_name"], employment_type=c["employment_type"],
                 travel_km=round(d, 1), travel_min=tmin, override=bool(forced),
                 breach=(breach_reason or "The shift or qualification rule was overridden.") if forced else None)
        recost(a, c, tmin)
        persist(region, a)
        if from_hold:
            store.parked_pop(region, jid)
            unparked.append(jid)
        _clear_awaiting(to, region)
        changed.append({k: a.get(k) for k in ("job_id", "crew_id", "crew_name", "employment_type", "travel_km", "cost_aud", "override", "breach")})

    for h in handoffs or []:
        move(h.get("job_id"), h.get("to_crew_id"))
    move(job_id, crew_id, forced=override)
    core.audit(actor, "assign_override" if override else "assign", job_id, dict(crew=crew_id, region=region, handoffs=len(handoffs or []),
                                                                               breach=breach_reason if override else None))
    return dict(ok=True, changed=changed, unparked=unparked)


def _clear_awaiting(crew_id, region):
    mv = store.crew_region_all().get(crew_id)
    if mv and mv.get("awaiting") and mv.get("region") == region:
        store.crew_region_put(crew_id, dict(mv, awaiting=False))


def _nearest_free(region, job, roster, exclude=()):
    e, used = D.eng(), _loads(roster)
    cands = []
    for c in e.crew_for_region(region):
        if c["crew_id"] in exclude or not e.eligible(job, c):
            continue
        if used.get(c["crew_id"], 0.0) + job["duration_min"] > e.cap_min(c):
            continue
        cands.append((km(c["home_lat"], c["home_lon"], job["lat"], job["lon"]), c))
    return min(cands, key=lambda x: x[0]) if cands else None


def redispatch(region, job_id, crew_id=None, actor="supervisor"):
    """Re-dispatch a job on hold to a crew (the nearest qualified one with room, unless one is named)."""
    entry = next((p for p in store.parked_list(region) if p["job_id"] == job_id), None)
    if not entry:
        return dict(ok=False, error="The job is not on hold.")
    roster, e = P.live_roster(region), D.eng()
    job = dict(D.jobs_by_id().get(job_id) or {}, **{k: entry[k] for k in ("lat", "lon", "duration_min")})
    job.setdefault("required_skill", entry.get("required_skill"))
    job.setdefault("required_ticket", entry.get("required_ticket"))
    chosen = e.crews.get(crew_id) if crew_id and e.crews.get(crew_id) and e.eligible(job, e.crews[crew_id]) else None
    if not chosen:
        best = _nearest_free(region, job, roster)
        chosen = best[1] if best else None
    if not chosen:
        return dict(ok=False, error="No qualified crew has room today.")
    r = apply_assign(region, job_id, chosen["crew_id"], actor=actor)
    d = km(chosen["home_lat"], chosen["home_lon"], job["lat"], job["lon"])
    return dict(ok=True, assigned_to=dict(crew_id=chosen["crew_id"], crew_name=chosen["crew_name"], persona=chosen["persona"], km=round(d, 1)),
                parked_count=len(store.parked_list(region)), changed=r["changed"])


def park(region, job_id, crew_id, reason, reason_label, actor="field"):
    """Put a stop on hold (a field blocker, or the dispatcher): it leaves the crew's day and waits on the on-hold queue."""
    src = D.jobs_by_id().get(job_id)
    if not src:
        return dict(ok=False, error="That job was not found.")
    c = D.crew(crew_id) or {}
    store.parked_put(region, job_id, dict(job_id=job_id, job_type=src["job_type"], title=src["title"], priority=src["priority"],
                                          lat=src["lat"], lon=src["lon"], duration_min=src["duration_min"], required_skill=src["required_skill"],
                                          required_ticket=src["required_ticket"], region=region, reason=reason, reason_label=reason_label or reason,
                                          from_crew_id=crew_id, from_crew_name=c.get("crew_name", crew_id), funding=src["funding"], ts=time.time()))
    D.ensure()["rosters"].pop(region, None)   # the preview re-solves without the held job
    core.audit(actor, "hold", job_id, dict(region=region, reason=reason_label or reason))
    return dict(ok=True, parked_count=len(store.parked_list(region)))


def unpark(region, job_id):
    store.parked_pop(region, job_id)
    return dict(ok=True, parked_count=len(store.parked_list(region)))


def reassign(region, crew_id, job_ids, reason_label=None, stand_down=False, actor="field"):
    """Hand the given stops (a crew standing down: its whole remaining day) to the nearest qualified crews with room; a stop nobody
    can take goes on hold with the reason. The pulled-back day is saved as the stood-down snapshot, so every surface shows the same."""
    e = D.eng()
    roster = P.field_roster(region)
    by_id, jobs = {a["job_id"]: a for a in roster["assignments"]}, D.jobs_by_id()
    used = _loads(roster)
    others = [c for c in e.crew_for_region(region) if c["crew_id"] != crew_id]
    snapshot, result = [], {}
    for jid in job_ids:
        a, src = by_id.get(jid), jobs.get(jid)
        if not a or not src:
            result[jid] = None
            continue
        snapshot.append({k: a.get(k) for k in ("job_id", "job_type", "title", "priority", "lat", "lon", "duration_min", "funding",
                                               "est_customer_minutes", "is_emergency")})
        need = a["duration_min"] + a.get("travel_min", 0)
        cands = [(km(c["home_lat"], c["home_lon"], a["lat"], a["lon"]), c) for c in others
                 if e.eligible(src, c) and used.get(c["crew_id"], 0.0) + need <= e.cap_min(c)]
        if not cands:
            park(region, jid, crew_id, "stand_down_no_crew", f"{reason_label or 'Stood down'}. No qualified crew was available.", actor)
            result[jid] = None
            continue
        d, c = min(cands, key=lambda x: x[0])
        a.update(crew_id=c["crew_id"], crew_name=c["crew_name"], employment_type=c["employment_type"], reassigned_from=crew_id)
        recost(a, c)
        persist(region, a)
        used[c["crew_id"]] = used.get(c["crew_id"], 0.0) + need
        result[jid] = dict(crew_id=c["crew_id"], crew_name=c["crew_name"], persona=c["persona"], km=round(d, 1))
    if stand_down:
        w = e.crews.get(crew_id, {})
        store.stood_down_put(region, crew_id, dict(reason_label=reason_label or "Stood down", jobs=result, count=len(snapshot), stops=snapshot,
                                                    crew_name=w.get("crew_name", crew_id), persona=w.get("persona"),
                                                    employment_type=w.get("employment_type"), ts=time.time()))
        store.crew_live_del(crew_id)
        core.audit(actor, "stand_down", crew_id, dict(region=region, reason=reason_label, stops=len(snapshot)))
    return dict(reassigned=result, count=len(snapshot))


def stand_down(region, crew_id, reason_label, actor):
    """Stand a crew down from the Control Room or a situation: its remaining stops are re-rostered as if it reported from the field."""
    fs = store.field_status_region(region)
    day = P.field_days(region).get(crew_id, [])
    return reassign(region, crew_id, [a["job_id"] for a in day if fs.get(a["job_id"]) != "done"], reason_label, True, actor)


def restore_crew(region, crew_id, actor):
    """Take a stand-down back: the crew returns to the roster and the stops handed on (or put on hold) come back to it."""
    rec = store.stood_down_get(region, crew_id)
    if not rec:
        return dict(ok=False, error="The crew is not stood down.")
    for jid, to in (rec.get("jobs") or {}).items():
        if to:
            store.override_del(region, jid)
        elif any(p["job_id"] == jid for p in store.parked_list(region)):
            store.parked_pop(region, jid)
    store.stood_down_del(region, crew_id)
    core.audit(actor, "restore_crew", crew_id, dict(region=region))
    return dict(ok=True, restored=len(rec.get("jobs") or {}))


def move_region(crew_id, to_region, actor):
    """Move a crew to another region: its stops in the old region go to the nearest qualified crews there, or on hold with the
    reason. In the new region it waits, empty, until a dispatcher gives it work."""
    e = D.eng()
    c = e.crews.get(crew_id)
    if not c:
        return dict(ok=False, error="That crew was not found.")
    frm = c["region_id"]
    if frm == to_region or to_region not in e.regions:
        return dict(ok=False, error="The crew is already in that region." if frm == to_region else "That region was not found.")
    roster = P.field_roster(frm)
    mine = [a for a in roster["assignments"] if a["crew_id"] == crew_id]
    used, jobs = _loads(roster), D.jobs_by_id()
    others = [x for x in e.crew_for_region(frm) if x["crew_id"] != crew_id]
    moved, parked = 0, []
    why = f"Crew {c['crew_name']} moved to {D.region_name(to_region)}. No qualified crew had room in {D.region_name(frm)}."
    for a in mine:
        src = jobs.get(a["job_id"])
        if not src:
            continue
        need = a["duration_min"] + a.get("travel_min", 0)
        cands = [(km(x["home_lat"], x["home_lon"], a["lat"], a["lon"]), x) for x in others
                 if e.eligible(src, x) and used.get(x["crew_id"], 0.0) + need <= e.cap_min(x)]
        if cands:
            d, x = min(cands, key=lambda t: t[0])
            a.update(crew_id=x["crew_id"], crew_name=x["crew_name"], employment_type=x["employment_type"], reassigned_from=crew_id)
            recost(a, x)
            persist(frm, a)
            used[x["crew_id"]] = used.get(x["crew_id"], 0.0) + need
            moved += 1
        else:
            park(frm, a["job_id"], crew_id, "crew_moved_region", why, actor)
            parked.append(a["job_id"])
    store.crew_region_put(crew_id, dict(region=to_region, orig_region=c["orig_region"], from_region=frm, ts=time.time(), awaiting=True))
    store.bump(frm)
    D.ensure()
    core.audit(actor, "move_region", crew_id, dict(frm=frm, to=to_region, moved=moved, parked=len(parked)))
    return dict(ok=True, crew_id=crew_id, crew_name=c["crew_name"], from_region=D.region_name(frm), to_region=D.region_name(to_region),
                stops=len(mine), reassigned=moved, parked=len(parked), parked_job_ids=parked)


def reset_region(crew_id, actor):
    """Undo a move: back to the original region, still awaiting a dispatch (the flag clears with its first job). The stops the move
    put on hold go back to the crew."""
    c = D.crew(crew_id)
    if not c:
        return dict(ok=False, error="That crew was not found.")
    cur = store.crew_region_all().get(crew_id)
    if not cur or cur.get("region") == c["orig_region"]:
        store.crew_region_del(crew_id)
    else:
        store.crew_region_put(crew_id, dict(region=c["orig_region"], orig_region=c["orig_region"], from_region=cur.get("region"),
                                            ts=time.time(), awaiting=True))
    D.ensure()
    held = [p["job_id"] for p in store.parked_list(c["orig_region"]) if p.get("reason") == "crew_moved_region" and p.get("from_crew_id") == crew_id]
    back = sum(1 for jid in held if redispatch(c["orig_region"], jid, crew_id, actor).get("ok"))
    core.audit(actor, "reset_region", crew_id, dict(stops_back=back, of=len(held)))
    return dict(ok=True, crew_id=crew_id, restored_to=D.region_name(c["orig_region"]), stops_back=back)


def crew_roster(region):
    days, moved = P.field_days(region), set(store.crew_region_all())
    out = [dict(crew_id=c["crew_id"], crew_name=c["crew_name"], persona=c["persona"], employment_type=c["employment_type"],
                jobs_today=len(days.get(c["crew_id"], [])), moved=c["crew_id"] in moved) for c in D.eng().crew_for_region(region)]
    out.sort(key=lambda x: (-x["jobs_today"], x["crew_name"]))
    return dict(region=region, crew=out, count=len(out))


# ---------------------------------------------------------------- planning exceptions, publishing, the feeds
def get_policy(region):
    e, crews = D.eng(), D.data()["crews"]
    events = {}
    for o in D.data()["incidents"]:
        events.setdefault(o["crew_id"], o)
    incident = sorted([dict(crew_id=c["crew_id"], crew_name=c["crew_name"], persona=c["persona"],
                            event_type=D.label("event_type", events[c["crew_id"]]["event_type"]), severity=events[c["crew_id"]]["severity"],
                            equipment=events[c["crew_id"]]["equipment_class"])
                       for c in crews if c["region_id"] == region and c["crew_id"] in e.incident],
                      key=lambda x: (-(x["severity"] or 0), x["crew_name"]))
    fatigue = []
    for cid, u in _loads(P.live_roster(region)).items():
        c = e.crews.get(cid)
        if c and u / e.cap_min(c) * 100 >= 85:
            fatigue.append(dict(crew_id=cid, crew_name=c["crew_name"], persona=c["persona"], load_pct=round(u / e.cap_min(c) * 100),
                                eba_hours=round(e.cap_min(c) / 60)))
    pol = store.policy_get(region) or {}
    return dict(region=region, incident_crew=incident, fatigue_crew=sorted(fatigue, key=lambda x: -x["load_pct"])[:30],
                policy=dict(incident_all=bool(pol.get("incident_all")), incident_allow=pol.get("incident_allow") or [],
                            fatigue_all=bool(pol.get("fatigue_all")), fatigue_allow=pol.get("fatigue_allow") or []))


def set_policy(region, pol, actor):
    store.policy_put(region, pol)
    D.ensure()["rosters"].pop(region, None)
    core.audit(actor, "planning_exceptions", region, pol)
    return dict(ok=True)


def rerostered():
    crews, items = D.eng().crews, []
    for region, cid, rec in store.stood_down_all():
        moves = [dict(job_id=j, title=(D.jobs_by_id().get(j) or {}).get("title"), to=to) for j, to in (rec.get("jobs") or {}).items()]
        n = sum(1 for m in moves if m["to"])
        w = crews.get(cid, {})
        items.append(dict(region=region, region_name=D.region_name(region), crew_id=cid, crew_name=rec.get("crew_name") or w.get("crew_name", cid),
                          persona=rec.get("persona") or w.get("persona"), employment_type=rec.get("employment_type") or w.get("employment_type"),
                          reason_label=rec.get("reason_label", "Stood down"), count=rec.get("count", len(moves)), reassigned=n,
                          unplaced=len(moves) - n, ts=rec.get("ts", 0), moves=moves))
    return dict(rerostered=sorted(items, key=lambda x: -x["ts"]), count=len(items))


def overrides():
    crews, items = D.eng().crews, []
    for region, jid, p in store.override_all():
        if not p.get("override"):
            continue   # breaches only, not routine re-assignments
        b = (p.get("breach") or "").lower()
        kind = ("Qualification" if any(k in b for k in ("qualif", "ticket", "skill")) else
                "Shift and fatigue" if any(k in b for k in ("shift", "hour", "fatigue", "limit")) else "Override")
        w = crews.get(p.get("crew_id"), {})
        items.append(dict(region=region, region_name=D.region_name(region), job_id=jid, title=(D.jobs_by_id().get(jid) or {}).get("title"),
                          job_type_label=D.label("job_type", p.get("job_type")), crew_id=p.get("crew_id"), crew_name=p.get("crew_name") or w.get("crew_name"),
                          persona=w.get("persona"), employment_type=p.get("employment_type") or w.get("employment_type"),
                          from_crew_name=p.get("reassigned_from_name"), breach=p.get("breach"), kind=kind, ts=p.get("ts", 0)))
    return dict(overrides=sorted(items, key=lambda x: -x["ts"]), count=len(items))


# ---------------------------------------------------------------- jobs other screens raise, and an immediate dispatch
def region_for(lat, lon, feeder_id=None):
    f = next((f for f in D.data()["feeders"] if f["feeder_id"] == feeder_id), None) if feeder_id else None
    if f:
        return f["region_id"]
    return min(D.data()["regions"], key=lambda r: km(r["lat"], r["lon"], lat, lon))["region_id"]


def raise_job(job_type, title, lat, lon, actor, feeder_id=None, asset_id=None, nmi=None, priority=4, customers=0, source="manual",
              situation_id=None, emergency=False, duration_min=None, job_id=None, job_date=None, window=(360, 1080)):
    """Raise a job into resource management: today's work by default, or planned work on its day (job_date) inside a time window
    (minutes after midnight). It joins that day's roster when the day comes."""
    spec = D.ensure()["specs"][job_type]
    region = region_for(lat, lon, feeder_id)
    jid = job_id or f"JOB-{source.upper()[:3]}-{uuid.uuid4().hex[:8].upper()}"
    dur = int(duration_min or spec["duration_min"])
    pg.q("""INSERT INTO rm.raised_job (job_id, job_date, job_type, title, priority, duration_min, window_start_min, window_end_min, region_id,
                                       depot_id, lat, lon, required_skill, required_ticket, funding, feeder_id, asset_id, nmi, customers,
                                       est_customer_minutes, source, situation_id, is_emergency)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, depot_id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM gld_rm_region WHERE region_id = %s
            ON CONFLICT (job_id) DO NOTHING""",
         (jid, job_date or D.today(), job_type, title, int(priority), dur, int(window[0]), int(window[1]), region, lat, lon, spec["required_skill"], spec["required_ticket"],
          "EMERGENCY" if emergency else spec["funding"], feeder_id, asset_id, nmi, int(customers or 0), int((customers or 1) * dur),
          source, situation_id, bool(emergency), region))
    store.bump("__jobs__")
    store.bump(region)
    core.audit(actor, "raise_job", jid, dict(job_type=job_type, region=region, title=title))
    D.ensure()
    return dict(ok=True, job_id=jid, region=region, region_name=D.region_name(region))


def delete_jobs(job_ids, actor):
    """Withdraw raised jobs (an Undo): off the roster, the on-hold queue and any crew's day."""
    rows = pg.q("DELETE FROM rm.raised_job WHERE job_id = ANY(%s) AND source IN ('manual', 'situation') RETURNING job_id, region_id", (list(job_ids),))
    for r in rows:
        store.override_del(r["region_id"], r["job_id"])
        store.field_status_del(r["region_id"], r["job_id"])
        pg.q("DELETE FROM rm.parked WHERE region_id = %s AND job_id = %s", (r["region_id"], r["job_id"]))
    store.bump("__jobs__")
    core.audit(actor, "withdraw_jobs", ",".join(job_ids), dict(n=len(rows)))
    return dict(ok=True, withdrawn=len(rows))


def nearest(job_id, n=5):
    """The best crews to send now: qualified, free of the guards, with room left today, nearest to where they are now."""
    job = D.jobs_by_id().get(job_id)
    if not job:
        return []
    region, e = job["region_id"], D.eng()
    ai, _ = P.allow_sets(region)
    used, sd = _loads(P.field_roster(region)), store.stood_down_region(region)
    out = []
    for p in P.positions(region):
        c = e.crews[p["crew_id"]]
        blocked = ("Stood down" if p["crew_id"] in sd else "On leave" if p["crew_id"] in e.on_leave else "In training" if p["crew_id"] in e.in_training
                   else "Open safety incident" if p["crew_id"] in e.incident and p["crew_id"] not in ai
                   else "Not qualified" if not e.eligible(job, c) else "No hours left today" if used.get(p["crew_id"], 0.0) + job["duration_min"] > e.cap_min(c)
                   else None)
        if blocked == "Not qualified":
            continue
        d = km(p["lat"], p["lon"], job["lat"], job["lon"])
        out.append(dict(crew_id=p["crew_id"], crew_name=c["crew_name"], persona=c["persona"], employment_type=c["employment_type"],
                        km=round(d, 1), eta_min=round(d / P.DAY_SPEED * 60), status=p["status_label"], blocked=blocked,
                        hours_on_shift=P.hours_on_shift(p)))
    out.sort(key=lambda x: (x["blocked"] is not None, x["km"]))
    return out[:n]


def dispatch(job_id, crew_id, actor):
    """Send a crew now: the job becomes theirs (first on their field app), the crew is en route, and the live engine drives it there."""
    job, c = D.jobs_by_id().get(job_id), D.crew(crew_id)
    if not job or not c:
        return dict(ok=False, error="That job or crew was not found.")
    region = job["region_id"]
    pos = next((p for p in P.positions(region) if p["crew_id"] == crew_id), None) or dict(lat=c["home_lat"], lon=c["home_lon"])
    d = km(pos["lat"], pos["lon"], job["lat"], job["lon"])
    roster = P.field_roster(region)
    prev = next((a for a in roster["assignments"] if a["job_id"] == job_id), None)
    a = dict(job_id=job_id, job_type=job["job_type"], crew_id=crew_id, crew_name=c["crew_name"], employment_type=c["employment_type"],
             travel_km=round(d, 1), travel_min=round(d / P.DAY_SPEED * 60), duration_min=job["duration_min"],
             reassigned_from=(prev or {}).get("crew_id") if (prev or {}).get("crew_id") != crew_id else None)
    recost(a, c)
    persist(region, a, dispatched=True)
    store.field_status_put(region, job_id, "driving")
    store.crew_live_put(crew_id, pos["lat"], pos["lon"], "travelling", job_id)
    _clear_awaiting(crew_id, region)
    overdue.watch(job, crew_id, c["crew_name"], a["travel_min"], actor)   # an emergency crew is watched until it reports
    core.audit(actor, "dispatch", job_id, dict(crew=crew_id, km=round(d, 1)))
    return dict(ok=True, crew_id=crew_id, crew_name=c["crew_name"], km=round(d, 1), eta_min=round(d / P.DAY_SPEED * 60))


def recall(job_id, actor):
    """Undo a dispatch: the job goes back to the plan and the crew back to its day."""
    job = D.jobs_by_id().get(job_id)
    if not job:
        return dict(ok=False, error="That job was not found.")
    region = job["region_id"]
    ov = store.override_map(region).get(job_id) or {}
    store.override_del(region, job_id)
    store.field_status_del(region, job_id)
    if ov.get("crew_id"):
        store.crew_live_del(ov["crew_id"])
    overdue.stop(job_id, "recalled", f"{ov.get('crew_name') or 'The crew'} was recalled from the job.")
    core.audit(actor, "recall", job_id, dict(crew=ov.get("crew_id")))
    return dict(ok=True, crew_id=ov.get("crew_id"))


def crew_detail(crew_id):
    c = D.crew(crew_id)
    if not c:
        return None
    region, e = c["region_id"], D.eng()
    days, fs = P.field_days(region), store.field_status_region(region)
    stops = days.get(crew_id, [])
    sched = {s["job_id"]: s for s in P.schedule(c, stops)}
    pos = next((p for p in P.positions(region) if p["crew_id"] == crew_id), {})
    jobs = D.jobs_by_id()
    return dict(crew=dict(c, region_name=D.region_name(region), skills=[D.label("skill", s) for s in sorted(e.skills.get(crew_id, set()))]),
                position=pos, hours_on_shift=P.hours_on_shift(pos),
                stood_down=store.stood_down_get(region, crew_id),
                stops=[dict(job_id=a["job_id"], title=a.get("title"), job_type_label=D.label("job_type", a["job_type"]),
                            start=_hhmm(sched[a["job_id"]]["start"]), finish=_hhmm(sched[a["job_id"]]["finish"]),
                            status=fs.get(a["job_id"], "planned"), address=(jobs.get(a["job_id"]) or {}).get("address"),
                            is_emergency=a.get("is_emergency")) for a in stops],
                tickets=[dict(ticket=D.label("ticket", t["ticket_code"]), expiry_date=t["expiry_date"], days_to_expiry=t["days_to_expiry"],
                              is_expired=bool(t["is_expired"])) for t in D.data()["certs"] if t["crew_id"] == crew_id],
                on_leave=crew_id in e.on_leave, in_training=crew_id in e.in_training, open_incident=crew_id in e.incident)


def summary():
    """Crews out now, free, stood down: the Command Centre's crews figure."""
    ps = P.positions()
    return dict(deployed=sum(1 for p in ps if p["status"] in ("travelling", "on_site")),
                available=sum(1 for p in ps if p["status"] in ("available", "not_started", "finished")),
                stood_down=sum(1 for p in ps if p["status"] == "stood_down"), total=len(ps))
