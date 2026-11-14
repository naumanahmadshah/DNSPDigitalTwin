"""The outages already in progress when the twin's snapshot was taken (gold gld_open_outage, from the ADMS active incidents extract): the
app's starting operational state, so the Command Centre, Situations, Operations, Customers and Resources open on a network with live work
(three long repairs past the 12 hour guaranteed service level and one recent fault) rather than an empty one.

seed() writes each into the live outage register (atlas.live_outage) with its situation, the repair job in resource management and the
patrol crew's field report, and keeps that crew on site at the fault on its repair job (rm.crew_live, rm.field_status in progress), so
the Command Centre and Resources show the crews out on faults at any hour; the crew is released once the outage is restored. Every time is anchored to now, so an outage keeps the age it had at the snapshot: one 16 hours old at the
snapshot shows as 16 hours old today. It runs when the app starts, after a demo reset, and every 20 minutes (moving the times of any
that have drifted by more than a quarter of an hour). An outage the field has restored stays restored until the next reset."""
import datetime as dt
import json
import threading
import time
import traceback

from core import pg, pq
from rm import dispatch as RX
from rm import store as RS

REANCHOR_EVERY_S = 1200
DRIFT_H = 0.25
_lock = threading.Lock()


def _labels(domain):
    return {r["code"]: r["label"] for r in pq("SELECT code, label FROM ref_label WHERE domain = %s", (domain,), ttl=600)}


def seed():
    """Write the snapshot's open outages (and their situation, repair job and field report) anchored to now. Returns how many it wrote."""
    with _lock:
        rows = pq("SELECT * FROM gld_open_outage ORDER BY outage_id")
        if not rows:
            return 0
        have = {r["outage_id"]: r for r in pg.q("SELECT outage_id, status, start_time FROM atlas.live_outage WHERE outage_id = ANY(%s)",
                                                ([o["outage_id"] for o in rows],))}
        stage_label = _labels("outage_stage")
        crews = {c["crew_id"]: c for c in pq("SELECT crew_id, crew_name, region_id FROM gld_crew", ttl=600)}
        now = dt.datetime.now(dt.timezone.utc)
        n = 0
        for o in rows:
            t0 = now - dt.timedelta(hours=float(o["hours_off"]))
            old = have.get(o["outage_id"])
            if old:
                if old["status"] != "active":
                    _release_crew(o)
                    continue   # restored by the field: it stays restored until the next reset
                _crew_on_site(o)
                was = dt.datetime.fromisoformat(old["start_time"]) if isinstance(old["start_time"], str) else old["start_time"]
                shift = t0 - was
                if abs(shift.total_seconds()) < DRIFT_H * 3600:
                    continue
                _reanchor(o, t0, now, shift)
            else:
                _create(o, t0, now, stage_label, crews)
                _crew_on_site(o)
            n += 1
        return n


def _stages(o, t0):
    """The work so far, timed from the anchored start (the same shape the live engine writes, plus the note)."""
    return [dict(ts=(t0 + dt.timedelta(minutes=float(s["offset_min"]))).isoformat(), action=s["action"], device=s.get("device"),
                 customers=s.get("customers_restored") if s["action"] != "FUSE_OPERATED" else o["customers_interrupted"], note=s.get("note"))
            for s in json.loads(o["stages"] or "[]")]


def _situation(o, t0, stages, stage_label):
    hours = float(o["hours_off"])
    last = next((s for s in reversed(stages) if s.get("note")), None)
    n_hours = round(hours)
    parts = [f"{o['device_label']} {o['device_id']} operated {n_hours} {'hour' if n_hours == 1 else 'hours'} ago, with {o['customers_off']:,} "
             f"{'customer' if o['customers_off'] == 1 else 'customers'} still off supply"
             + (f", {o['life_support_off']} of them on life support" if o["life_support_off"] else "") + "."]
    if hours >= 12:
        parts.insert(0, "Guaranteed service level breached.")
    if last:
        parts.append(f"Latest, {stage_label.get(last['action'], last['action']).lower()}: {last['note']}.")
    headline = " ".join(parts)
    return dict(situation_id=f"SIT-TRIP-{o['outage_id']}", title=f"{o['cause_label']} on {o['feeder_name'] or o['feeder_id']}",
                severity="critical" if hours >= 12 or o["life_support_off"] else "high", headline=headline,
                payload=json.dumps(dict(outage_id=o["outage_id"], device_id=o["device_id"], asset_id=o["fault_asset_id"], span_id=o["span_id"],
                                        fault_lat=o["lat"], fault_lon=o["lon"], sectionalising_switch=None, cause_code=o["cause_code"])),
                detected=t0 + dt.timedelta(minutes=3), updated=dt.datetime.fromisoformat(stages[-1]["ts"]) if stages else t0)


def _create(o, t0, now, stage_label, crews):
    oid, stages = o["outage_id"], _stages(o, t0)
    s = _situation(o, t0, stages, stage_label)
    etr = now + dt.timedelta(hours=float(o["etr_hours"]))
    pg.q("""INSERT INTO atlas.live_outage (outage_id, feeder_id, device_id, start_time, status, customers_off, customers_initial, life_support_off,
                                           cause_code, etr, stages, situation_id, lat, lon)
            VALUES (%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) ON CONFLICT (outage_id) DO NOTHING""",
         (oid, o["feeder_id"], o["device_id"], t0, o["customers_off"], o["customers_interrupted"], o["life_support_off"], o["cause_code"], etr,
          json.dumps(stages), s["situation_id"], o["lat"], o["lon"]))
    pg.q("""INSERT INTO atlas.situation (situation_id, type, title, severity, status, detected_at, updated_at, source, feeders,
                                         customers, life_support, critical_sites, headline, payload)
            VALUES (%(id)s,'trip',%(title)s,%(sev)s,'detected',%(det)s,%(upd)s,'detector',%(feeders)s,%(cust)s,%(ls)s,%(crit)s,%(head)s,%(payload)s::jsonb)
            ON CONFLICT (situation_id) DO UPDATE SET status = 'detected', title = EXCLUDED.title, severity = EXCLUDED.severity,
              detected_at = EXCLUDED.detected_at, updated_at = EXCLUDED.updated_at, customers = EXCLUDED.customers,
              life_support = EXCLUDED.life_support, critical_sites = EXCLUDED.critical_sites, headline = EXCLUDED.headline, payload = EXCLUDED.payload""",
         dict(id=s["situation_id"], title=s["title"], sev=s["severity"], det=s["detected"], upd=s["updated"], feeders=[o["feeder_id"]],
              cust=o["customers_off"], ls=o["life_support_off"], crit=o["critical_sites"], head=s["headline"], payload=s["payload"]))
    pg.q("DELETE FROM atlas.situation_event WHERE situation_id = %s AND actor = 'detector'", (s["situation_id"],))
    pg.q("INSERT INTO atlas.situation_event (situation_id, ts, actor, action, detail) VALUES (%s,%s,'detector','detected',%s::jsonb)",
         (s["situation_id"], s["detected"], json.dumps(dict(trigger=f"{o['device_id']} operated.", source="ADMS active incidents"))))
    # the repair still to do, on today's plan for the region's crews (a crew can be sent from the situation)
    job_id = f"JOB-{oid}"
    if not pg.q("SELECT 1 FROM rm.raised_job WHERE job_id = %s", (job_id,), one=True):
        RX.raise_job(o["repair_job_type"], f"Repair the {o['fault_kind_label'].lower()} {o['fault_asset_id']} on {o['feeder_id']}. "
                     f"{o['customers_off']:,} {'customer is' if o['customers_off'] == 1 else 'customers are'} off supply.", o["lat"], o["lon"], "detector", feeder_id=o["feeder_id"],
                     asset_id=o["fault_asset_id"], priority=5, customers=o["customers_off"], source="outage",
                     situation_id=s["situation_id"], emergency=True, duration_min=o["repair_minutes"], job_id=job_id)
    pg.q("UPDATE rm.raised_job SET created_at = %s WHERE job_id = %s", (t0 + dt.timedelta(minutes=10), job_id))
    crew = crews.get(o["crew_id"]) if o.get("crew_id") else None
    if crew:   # the patrol crew's report: what they found and made safe, and what the repair needs
        by = {x["action"]: x for x in stages}
        arrived = by.get("PATROL_STARTED", stages[0])["ts"]
        reported = dt.datetime.fromisoformat(by.get("MADE_SAFE", stages[-1])["ts"]) + dt.timedelta(minutes=15)
        restored = sum(x.get("customers") or 0 for x in stages if x["action"] == "GENERATOR_CONNECTED")
        pg.q("""INSERT INTO rm.field_report (report_id, job_id, situation_id, outage_id, region_id, crew_id, asset_id, reported_at, reported_by,
                                             arrived_at, response_note, patrol_complete, fault_located, fault_kind, fault_ref, switching_executed,
                                             customers_restored, access_status, hazards, materials, notes, outcome)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,true,%s,%s,true,%s,%s,%s,%s::jsonb,%s,'follow_up')
                ON CONFLICT (report_id) DO UPDATE SET reported_at = EXCLUDED.reported_at, arrived_at = EXCLUDED.arrived_at""",
             (f"FR-{oid}", job_id, s["situation_id"], oid, crew["region_id"], crew["crew_id"], o["fault_asset_id"], reported, crew["crew_name"],
              arrived, (by.get("PATROL_STARTED") or {}).get("note"), o["fault_kind"],
              o["fault_asset_id"], restored, o["access_status"], json.loads(o["hazards"] or "[]"), o["materials"] or "[]", o["crew_note"]))


def _crew_on_site(o):
    """The patrol crew of an open outage is on site at the fault, working its repair job (refreshed every pass, so it stays current). A
    crew the live engine or a dispatcher has since sent elsewhere is left where it is."""
    if not o.get("crew_id"):
        return   # a fault too recent for a crew to be there yet
    job = f"JOB-{o['outage_id']}"
    moved = pg.q("SELECT 1 AS x FROM rm.crew_live WHERE crew_id = %s AND status IN ('travelling', 'on_site') AND job_id IS DISTINCT FROM %s",
                 (o["crew_id"], job), one=True)
    done = pg.q("SELECT 1 AS x FROM rm.field_status WHERE job_id = %s AND status = 'done'", (job,), one=True)
    if moved or done:
        return
    RS.crew_live_put(o["crew_id"], o["lat"], o["lon"], "on_site", job)
    region = pg.q("SELECT region_id FROM rm.raised_job WHERE job_id = %s", (job,), one=True)
    if region and not pg.q("SELECT 1 AS x FROM rm.field_status WHERE job_id = %s", (job,), one=True):
        RS.field_status_put(region["region_id"], job, "in_progress")


def _release_crew(o):
    """Once the outage is restored, its crew is free again (unless it has already been sent to other work)."""
    if o.get("crew_id"):
        pg.q("UPDATE rm.crew_live SET status = 'available', job_id = NULL, updated_at = now() WHERE crew_id = %s AND job_id = %s",
             (o["crew_id"], f"JOB-{o['outage_id']}"))


def _reanchor(o, t0, now, shift):
    """Move an open outage's times (and its situation, job and report) by the drift, so it keeps the age it had at the snapshot."""
    oid = o["outage_id"]
    pg.q("UPDATE atlas.live_outage SET start_time = %s, etr = %s, stages = %s::jsonb WHERE outage_id = %s AND status = 'active'",
         (t0, now + dt.timedelta(hours=float(o["etr_hours"])), json.dumps(_stages(o, t0)), oid))
    pg.q("UPDATE atlas.situation SET detected_at = detected_at + %(d)s, updated_at = updated_at + %(d)s WHERE situation_id = %(s)s",
         dict(d=shift, s=f"SIT-TRIP-{oid}"))
    pg.q("UPDATE atlas.situation_event SET ts = ts + %s WHERE situation_id = %s AND actor = 'detector'", (shift, f"SIT-TRIP-{oid}"))
    pg.q("UPDATE rm.raised_job SET created_at = created_at + %s WHERE job_id = %s", (shift, f"JOB-{oid}"))
    pg.q("UPDATE rm.field_report SET arrived_at = arrived_at + %(d)s, reported_at = reported_at + %(d)s WHERE report_id = %(r)s",
         dict(d=shift, r=f"FR-{oid}"))


def keep():
    """Background loop: seed now, then re-anchor every REANCHOR_EVERY_S seconds."""
    while True:
        try:
            seed()
        except Exception:
            traceback.print_exc()
        time.sleep(REANCHOR_EVERY_S)


def start():
    threading.Thread(target=keep, name="open-outages", daemon=True).start()
