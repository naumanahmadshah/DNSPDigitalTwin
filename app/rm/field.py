"""The field app's view of the plan: which crews have a day in a region, and one crew's day as an ordered route with everything the
crew needs on each stop. The client times the day (and re-times it on real roads) and sends back order, status and changes."""
import core
from . import data as D
from . import plan as P
from . import store


def meta():
    rs = D.data()["regions"]
    return dict(regions=[dict(id=r["region_id"], name=r["name"]) for r in rs], default_region=rs[0]["region_id"] if rs else None)


def workers(region):
    """Every crew with a day in the region; a stood-down crew keeps its pulled-back day (from the saved snapshot) in the picker."""
    days, sd, crews = P.field_days(region), store.stood_down_region(region), D.eng().crews
    out = []
    for cid in sorted(set(days) | {c for c, r in sd.items() if r.get("stops")}):
        c = crews.get(cid)
        if c:
            n = len(sd[cid]["stops"]) if sd.get(cid, {}).get("stops") else len(days.get(cid, []))
            out.append(dict(crew_id=cid, name=c["crew_name"], persona=c["persona"], employment_type=c["employment_type"], jobs=n, stood_down=cid in sd))
    out.sort(key=lambda x: x["name"])
    return dict(region=region, workers=out)


def _pick(days):
    """With no crew asked for, open the one with the most varied full day (both re-ordering and a locked stop to show)."""
    jobs = D.jobs_by_id()

    def score(cid):
        js = days[cid]
        if len(js) < 5:
            return (0, 0, 0)
        fixed = sum(1 for a in js if P.is_fixed(jobs.get(a["job_id"], a)))
        return (1 if len(js) - fixed >= 3 and fixed >= 1 else 0, len({a["job_type"] for a in js}), len(js))
    return max(days, key=score)


def itinerary(region, crew_id=None):
    days = P.field_days(region)
    sd = store.stood_down_get(region, crew_id) if crew_id else None
    if not (crew_id and (crew_id in days or sd)):
        if not days:
            return dict(region=region, region_name=D.region_name(region), jobs=[])
        crew_id = _pick(days)
        sd = store.stood_down_get(region, crew_id)
    c, jobs = D.crew(crew_id), D.jobs_by_id()
    mine = sd["stops"] if sd and sd.get("stops") else days.get(crew_id, [])
    if not (sd and sd.get("stops")):
        saved = store.field_seq_get(region, crew_id)   # the crew's own order, from any device; new stops go to the end
        if saved:
            rank = {j: i for i, j in enumerate(saved)}
            mine = sorted(mine, key=lambda a: (not a.get("is_emergency"), rank.get(a["job_id"], len(rank))))
    out = []
    for a in mine:
        src = jobs.get(a["job_id"], a)
        out.append(dict(job_id=a["job_id"], type=src["job_type"], type_label=D.label("job_type", src["job_type"]), title=src.get("title"),
                        priority=src.get("priority", 3), lat=a["lat"], lon=a["lon"], duration_min=a["duration_min"],
                        window_start_min=src.get("window_start_min", P.DAY_SHIFT), window_end_min=src.get("window_end_min", P.DAY_END),
                        fixed=P.is_fixed(src), funding_label=D.label("funding", src.get("funding")), est_customer_minutes=src.get("est_customer_minutes", 0),
                        customers=src.get("customers", 0), required_skill_label=D.label("skill", src.get("required_skill")),
                        required_ticket_label=D.label("ticket", src.get("required_ticket")), address=src.get("address"), suburb=src.get("suburb"),
                        feeder_id=src.get("feeder_id"), asset_id=src.get("asset_id"), is_emergency=bool(src.get("is_emergency"))))
    ids = {j["job_id"] for j in out}
    return dict(region=region, region_name=D.region_name(region),
                crew=dict(id=crew_id, name=c["crew_name"], persona=c["persona"], depot_id=c["depot_id"], employment_type=c["employment_type"],
                          vehicle=c.get("vehicle_type_label")),
                home=dict(lat=c["home_lat"], lon=c["home_lon"]), shift_start_min=P.DAY_SHIFT, speed_kmh=P.DAY_SPEED, max_shift_min=D.eng().cap_min(c),
                jobs=out, stood_down=sd, job_status={j: s for j, s in store.field_status_region(region).items() if j in ids})


def reorder(region, crew_id, order):
    store.field_seq_put(region, crew_id, order)
    return dict(ok=True)


def status(region, job_id, st, actor):
    store.field_status_put(region, job_id, st)
    core.audit(actor, "field_status", job_id, dict(status=st))
    return dict(ok=True)
