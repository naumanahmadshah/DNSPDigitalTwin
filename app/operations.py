"""Operations: the restoration view of the network, read from Lakebase only.

restoration(): outages in progress and restored in the last six hours (atlas.live_outage), customers restored of the total, the crews the live
engine is driving (rm.crew_live) and what each is working on, the repair jobs and their status (rm.field_status), the field reports on those
outages, and the life support customers.
outage(): one outage in full: its stages, feeder and device, crew and repair job, its latest field report and the customers it has off.
off_supply(): the premises off supply now (all of them, or the life support customers only), for the tiles' detail.
live(): the timeline of the Lakebase live streams (meter alarms, customer calls, switch positions), each call with its customer and a check of
the problem against the network.
learn(): resource performance per crew (field reports) and damage insights by cause.
"""
import datetime as dt

import core
import customers as C
from core import pg, pq
from fieldreport import label, labels


# Timestamps reach Python as ISO text (core.pg converts them), so every duration is computed in SQL, in minutes.


# ---------------------------------------------------------------- restoration in progress
def restoration():
    """The restoration board: the outages that are active or were restored in the last six hours, with the feeder, device, crew, job and
    field report for each, the crews deployed and what each is working on, and the totals behind the headline tiles."""
    outs = pg.q("""SELECT o.outage_id, o.feeder_id, o.device_id, o.status, o.start_time, o.etr, o.customers_initial, o.customers_off,
                       o.life_support_off, o.cause_code, o.situation_id, o.crew_id, o.restored_at, o.lat, o.lon, o.stages
                   FROM atlas.live_outage o
                   WHERE o.status = 'active' OR o.restored_at > now() - interval '6 hours'
                   ORDER BY (o.status = 'active') DESC, o.start_time DESC LIMIT 60""")
    ids = [o["outage_id"] for o in outs]
    feeders = {r["feeder_id"]: r for r in pq("SELECT feeder_id, feeder_name, zs_name FROM gld_feeder_state", ttl=600)}
    devices = {r["asset_id"]: r["asset_class_label"] for r in pq("SELECT asset_id, asset_class_label FROM gld_asset WHERE asset_id = ANY(%(d)s::text[])",
                                                                   {"d": [o["device_id"] for o in outs if o["device_id"]]}, ttl=600)} if outs else {}
    jobs = {r["job_id"]: r for r in pg.q("SELECT job_id, status FROM rm.field_status WHERE job_id = ANY(%s)", ([f"JOB-{i}" for i in ids],))} if ids else {}
    names = {r["crew_id"]: r["crew_name"] for r in pq("SELECT crew_id, crew_name FROM gld_crew", ttl=600)}
    reports = {}
    for r in pg.q("""SELECT DISTINCT ON (outage_id) outage_id, report_id, outcome, customers_restored, reported_at, crew_id
                     FROM rm.field_report WHERE outage_id = ANY(%s) ORDER BY outage_id, reported_at DESC""", (ids,)) if ids else []:
        reports[r["outage_id"]] = r
    dom = labels("field_outcome")
    rows = []
    total = off = life = 0
    for o in outs:
        total += int(o["customers_initial"] or 0)
        if o["status"] == "active":
            off += int(o["customers_off"] or 0)
            life += int(o["life_support_off"] or 0)
        job = jobs.get(f"JOB-{o['outage_id']}")
        rep = reports.get(o["outage_id"])
        flisr = sum(int(s.get("customers") or 0) for s in (o["stages"] or []) if str(s.get("action", "")).startswith("FLISR"))
        rows.append(dict(outage_id=o["outage_id"], feeder_id=o["feeder_id"], device_id=o["device_id"], status=o["status"],
                         status_label="Restored" if o["status"] == "restored" else "Active", start_time=o["start_time"], etr=o["etr"],
                         customers_initial=int(o["customers_initial"] or 0), customers_off=int(o["customers_off"] or 0),
                         customers_restored=max(0, int(o["customers_initial"] or 0) - int(o["customers_off"] or 0)),
                         life_support_off=int(o["life_support_off"] or 0), cause_label=label("cause", o["cause_code"]),
                         crew_name=names.get(o["crew_id"]) or (names.get(rep["crew_id"]) if rep else None),
                         job_status=job["status"] if job else None, job_status_label=label("ops_job_status", job["status"]) if job else None,
                         flisr_customers=flisr, field_outcome=rep["outcome"] if rep else None,
                         field_outcome_label=dom.get(rep["outcome"]) if rep else None,
                         field_customers=int(rep["customers_restored"] or 0) if rep else 0,
                         field_crew_id=rep["crew_id"] if rep else None, field_reported_at=rep["reported_at"] if rep else None,
                         situation_id=o["situation_id"], crew_id=o["crew_id"],
                         feeder_name=(feeders.get(o["feeder_id"]) or {}).get("feeder_name"), zs_name=(feeders.get(o["feeder_id"]) or {}).get("zs_name"),
                         device_label=devices.get(o["device_id"]), restored_at=o["restored_at"],
                         stage_label=label("outage_stage", (o["stages"] or [{}])[-1].get("action")) if o["stages"] else None))
    crews = pg.q("""SELECT c.crew_id, c.status, c.job_id, c.since, g.crew_name, g.persona, g.region_name, g.crew_size, g.vehicle_type_label,
                           g.employment_type FROM rm.crew_live c
                    LEFT JOIN gld_crew g ON g.crew_id = c.crew_id WHERE c.updated_at > now() - interval '3 hours' ORDER BY c.status, c.crew_id""")
    for c in crews:
        c["status_label"] = label("ops_crew_status", c["status"])
    deployed = sum(1 for c in crews if c["status"] in ("travelling", "on_site"))
    by_job = {f"JOB-{r['outage_id']}": r for r in rows}
    for c in crews:   # what each crew is working on: the outage its job is for
        o = by_job.get(c["job_id"])
        c["working_on"] = f"{o['feeder_name'] or o['feeder_id']}, {o['cause_label'].lower()}" if o else None
        c["outage_id"], c["situation_id"] = (o["outage_id"], o["situation_id"]) if o else (None, None)
    return dict(
        totals=dict(customers_total=total, customers_off=off, customers_restored=max(0, total - off), active_outages=sum(1 for o in outs if o["status"] == "active"),
                    restored_recent=sum(1 for o in outs if o["status"] == "restored"), life_support_off=life, crews_deployed=deployed,
                    crews_on_site=sum(1 for c in crews if c["status"] == "on_site"), field_customers_reported=sum(r["field_customers"] for r in rows)),
        outages=rows, crews=crews,
    )


def outage(outage_id):
    """One outage in full, for its detail window: the outage and its stages (labelled), the feeder and device, the crew and repair job, the
    latest field report, and the customers it has off (life support first)."""
    o = next((r for r in restoration()["outages"] if r["outage_id"] == outage_id), None)
    if not o:
        raise ValueError(f"Outage {outage_id} is not active or recently restored.")
    raw = pg.q("SELECT stages FROM atlas.live_outage WHERE outage_id = %s", (outage_id,), one=True)
    stages = [dict(ts=x.get("ts"), action_label=label("outage_stage", x.get("action")), device=x.get("device"), note=x.get("note"),
                   customers=x.get("customers")) for x in (raw["stages"] or [])]
    rep = pg.q("""SELECT r.*, g.crew_name FROM rm.field_report r LEFT JOIN gld_crew g ON g.crew_id = r.crew_id
                  WHERE r.outage_id = %s ORDER BY r.reported_at DESC LIMIT 1""", (outage_id,), one=True)
    report = None
    if rep:
        report = dict(crew_name=rep["crew_name"], reported_at=rep["reported_at"], arrived_at=rep["arrived_at"], outcome_label=label("field_outcome", rep["outcome"]),
                      fault_kind_label=label("field_fault_kind", rep["fault_kind"]), fault_ref=rep["fault_ref"], customers_restored=rep["customers_restored"],
                      hazards=[label("field_hazard", h) for h in (rep["hazards"] or [])], notes=rep["notes"], response_note=rep["response_note"])
    premises = [p for p in off_supply() if p["outage_id"] == outage_id]
    return dict(outage=o, stages=stages, report=report, premises=premises)


def off_supply(life_support=False):
    """The premises off supply now, by the live outage each is off in (life support customers only when asked), for the tiles' detail."""
    off = C._off_supply()
    rows = pq("""SELECT nmi, address, suburb, feeder_id, life_support, critical_site, customer_type_label FROM gld_nmi
                 WHERE nmi = ANY(%(n)s::text[])""", {"n": list(off)}, ttl=30) if off else []
    out = [dict(r, outage_id=off[r["nmi"]]["outage_id"], etr=off[r["nmi"]]["etr"]) for r in rows if r["life_support"] or not life_support]
    return sorted(out, key=lambda r: (not r["life_support"], r["outage_id"], r["address"] or ""))


# ---------------------------------------------------------------- the timeline of the live streams, and the switch positions
def live():
    """The latest meter alarms, customer calls and switch positions as one timeline. A call or an alarm carries its premises (address and
    feeder), and a call carries the check of its problem against the network now."""
    timeline = pg.q("""SELECT * FROM (
                         (SELECT 'alarm' AS kind, ts, nmi AS ref, event_code AS code, NULL AS channel, NULL AS feeder FROM live.meter_event ORDER BY ts DESC LIMIT 25)
                         UNION ALL (SELECT 'call', ts, nmi, reason, channel, NULL FROM live.call ORDER BY ts DESC LIMIT 25)
                         UNION ALL (SELECT 'switch', ts, element, quality, NULL, feeder_id FROM live.scada WHERE attribute = 'POS' ORDER BY ts DESC LIMIT 25)) t
                       ORDER BY ts DESC LIMIT 40""")
    nmis = [e["ref"] for e in timeline if e["kind"] in ("alarm", "call")]
    prem = {r["nmi"]: r for r in pq("SELECT nmi, address, suburb, feeder_id, life_support FROM gld_nmi WHERE nmi = ANY(%(n)s::text[])",
                                     {"n": nmis}, ttl=60)} if nmis else {}
    off, meters, busy = C._off_supply(), C.meter_last(set(nmis)), C.busy_feeders()
    kinds = {"alarm": "Meter alarm", "call": "Customer call", "switch": "Switch position"}
    for e in timeline:
        e["kind_label"] = kinds.get(e["kind"], e["kind"])
        p = prem.get(e["ref"]) or {}
        e["address"] = p.get("address")   # the register's address already carries the suburb and postcode
        own = e.pop("feeder")   # a switch reading names its feeder; an alarm or a call takes its premises' feeder
        e["feeder_id"] = p.get("feeder_id") or own
        e["life_support"] = bool(p.get("life_support"))
        if e["kind"] == "alarm":
            e["detail"] = label("ops_meter_event", e["code"])
        elif e["kind"] == "switch":
            e["detail"] = label("ops_switch_quality", e["code"])
        else:
            e["detail"] = e["code"]
            e["check"], e["check_note"] = C.check_call(e["code"], e["ref"], p.get("feeder_id"), off, meters, busy)
            e["check_label"] = C.CALL_CHECK[e["check"]]
    return dict(timeline=timeline)


# ---------------------------------------------------------------- learn and improve
def learn():
    """What the recent work teaches: resource performance per crew (field reports), and damage insights by cause."""
    since = core.AS_OF - dt.timedelta(days=365)
    damage = pq("""SELECT coalesce(cause_label, cause_code) AS cause, count(*) AS events, sum(customers_interrupted) AS customers,
                       round(sum(customer_minutes)) AS customer_minutes
                   FROM gld_outage WHERE NOT momentary AND NOT planned AND outage_date > %(since)s
                   GROUP BY 1 ORDER BY customer_minutes DESC NULLS LAST""", {"since": since}, ttl=600)
    perf_crew = pg.q("""SELECT r.crew_id, g.crew_name, g.persona, g.region_name AS region, count(*) AS reports,
                           count(*) FILTER (WHERE r.outcome = 'fixed') AS fixed, count(*) FILTER (WHERE r.outcome = 'follow_up') AS follow_up,
                           coalesce(sum(r.customers_restored), 0) AS customers_restored,
                           round(avg(extract(epoch FROM r.arrived_at - rj.created_at) / 60) FILTER (WHERE r.arrived_at IS NOT NULL AND rj.created_at IS NOT NULL))
                               AS avg_arrival_min
                        FROM rm.field_report r LEFT JOIN gld_crew g ON g.crew_id = r.crew_id LEFT JOIN rm.raised_job rj ON rj.job_id = r.job_id
                        GROUP BY 1, 2, 3, 4 ORDER BY reports DESC LIMIT 30""")
    return dict(resources=dict(by_crew=perf_crew), damage=dict(by_cause=damage, since=since.isoformat()))
