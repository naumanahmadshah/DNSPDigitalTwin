"""Live engine: emitter, storyline injector and situation detector (one time-boxed serverless job).

Everything lands in the app's Lakebase project first: the latest state every tick (feeder and zone loads, dispatched crews' positions, live
outages), and every raw record of the live streams in schema live (recent hours only). The Lakebase change data feed carries the streams and
the operational records to the lakehouse (lb_live, lb_atlas, lb_rm), where the pipeline's bronze layer, Genie and history read them.
The engine trips a feeder every few minutes on its own. Switching live ingestion off in the app sets the engine's status to stopping, and
the engine then writes what it holds and ends the run. Each trip raises an emergency fault job in resource management (schema rm); a crew dispatched to it drives there here, works the fault and restores supply.
Usage: live.py --src <dir> --catalog <c> --schema <s> --project <lakebase project> [--minutes 60]
"""
import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import time
import uuid

TICK_S = 4
FLUSH_S = 20
KEEP_H = 6           # hours of raw stream records kept in Lakebase (the lakehouse keeps them all)
PURGE_S = 300
DETECT_S = 30
TRIP_EVERY_MIN = 13
SPEEDUP = 6          # crews travel and repair this many times faster than real time, so a restoration fits a demo
UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC)


def _count(n, one):
    """A count with its noun, singular for one: '1 customer', '1,268 customers'."""
    return f"{n:,} {one if n == 1 else one + 's'}"


# --------------------------------------------------------------------------------------------- the network in memory
class World:
    def __init__(self, src, cfg):
        sys.path.insert(0, os.path.join(src, "generator"))
        from network import Network
        from history import Topology
        self.net = Network(cfg).build()
        self.topo = Topology(self.net)
        self.rng = random.Random(cfg["seed"] + int(time.time() // 3600))
        self.feeders = {f["feeder_id"]: f for f in self.net.feeders}
        self.cust = {}
        for m in self.net.nmis:
            self.cust[m["feeder_id"]] = self.cust.get(m["feeder_id"], 0) + 1
        self.nmis_by_ds = {}
        for m in self.net.nmis:
            self.nmis_by_ds.setdefault(m["ds_id"], []).append(m)
        self.ds_by_node = {d["node_id"]: d for d in self.net.dss}
        self.node = {n["node_id"]: n for n in self.net.nodes}
        self.devices = {d["device_id"]: d for d in self.net.devices}
        self.cb_open = set()
        self.overlay = {}

    def nmis_below(self, node_id):
        out = []
        for n in self.topo.downstream(node_id):
            d = self.ds_by_node.get(n)
            if d:
                out.extend(self.nmis_by_ds.get(d["ds_id"], []))
        return out


def load_shape(h):
    return (0.45 + 0.18 * math.exp(-((h - 7.5) / 1.6) ** 2) + 0.42 * math.exp(-((h - 18.5) / 2.2) ** 2)
            - 0.12 * math.exp(-((h - 12.5) / 2.0) ** 2))


# --------------------------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, a):
        from pyspark.sql import SparkSession
        self.spark = SparkSession.builder.getOrCreate()
        self.cat, self.sch = a.catalog, a.schema
        cfg = json.load(open(os.path.join(a.src, "config", "atlas.json")))
        self.cfg = cfg
        self.world = World(a.src, cfg)
        from lakebase import PG, stream_columns
        self.stream_columns = stream_columns
        self.pg = PG(a.project, f"{a.schema}_serving")
        from records import LAND
        self.buf = {k: [] for k in LAND}   # the live streams (generator/records.py), flushed to Lakebase schema live
        self.outages = {}
        self.repairs = {}   # outage id -> when the crew on site finishes, and which crew
        self.last_trip = time.time() - (TRIP_EVERY_MIN - 2) * 60
        self.last_flush = self.last_detect = self.last_purge = 0

    def fq(self, t):
        return f"`{self.cat}`.`{self.sch}`.{t}"

    # ------------------------------------------------------------------ setup
    def load_rm(self):
        """Resource management's feeders (for each fault's region), the crews' names and the fault cause labels, from the synced gold."""
        self.region_of = dict(self.pg.q("SELECT feeder_id, region_id FROM gld_rm_feeder", fetch=True) or [])
        self.crew_name = {c: f"{n}'s crew ({c})" for c, n in self.pg.q("SELECT crew_id, crew_name FROM gld_crew", fetch=True) or []}
        self.cause_label = {c: l.lower() for c, l in self.pg.q("SELECT code, label FROM ref_label WHERE domain = 'cause'", fetch=True) or []}
        # crews already on site at an outage from an earlier run finish their repair in this one
        for crew_id, job_id in self.pg.q("""SELECT crew_id, job_id FROM rm.crew_live WHERE status = 'on_site' AND job_id LIKE 'JOB-LIVE-%'""", fetch=True) or []:
            self.on_arrival(job_id, crew_id)

    def bump(self, region=None):
        """New revisions for the region (if any), the jobs and the whole network, so every app process re-reads the plan."""
        keys = sorted({k for k in (region, "__jobs__", "__global__") if k})
        self.pg.q(f"""INSERT INTO app.region_rev (region_id, rev) VALUES {", ".join(["(%s, 1)"] * len(keys))}
                      ON CONFLICT (region_id) DO UPDATE SET rev = app.region_rev.rev + 1""", tuple(keys))

    # ------------------------------------------------------------------ emitters
    def emit_scada(self, t):
        h = (t.astimezone(dt.timezone(dt.timedelta(hours=11))).hour + t.minute / 60)
        shape = load_shape(h)
        snap = {}
        zone_mw = {}
        for fid, f in self.world.feeders.items():
            r = self.world.rng
            mw = (self.world.cust.get(fid, 0) * 0.0021 + 0.4) * shape
            off = sum(o["customers_off"] for o in self.outages.values() if o["feeder_id"] == fid) / max(1, self.world.cust.get(fid, 1))
            mw *= max(0.0, 1 - off) * (1 + r.uniform(-0.02, 0.02))
            amps = mw * 1000 / (math.sqrt(3) * f["kv"]) * 1.03
            kv = f["kv"] * (1.02 - 0.025 * shape + r.uniform(-0.002, 0.002))
            status = 0 if fid in self.world.cb_open else 1
            for attr, v in (("MW", mw), ("AMPS", amps), ("KV", kv)):
                self.buf["scada"].append(dict(ref=f"{fid}-CB01/MMXU1.{ {'MW': 'TotW', 'AMPS': 'A', 'KV': 'PhV'}[attr] }", tag=f"{fid}.{attr}.PV",
                                              element=fid, attribute=attr, feeder_id=fid, ts=t, value=round(v, 3), quality="good", source="ADMS"))
            snap[fid] = dict(mw=round(mw, 2), amps=round(amps), kv=round(kv, 2), cb=status, rating=f["rating_amps"])
            zone_mw[f["zs_id"]] = zone_mw.get(f["zs_id"], 0) + mw
        zs = {}
        for tx in self.world.net.zs_transformers:
            mw = zone_mw[tx["zs_id"]] / 2 * (1.25 if tx["asset_id"] == "ZS-KNV-TX1" else 1.0)
            amb = 18 + 9 * math.sin(math.pi * (h - 9) / 12)
            top = amb + 8 + 48 * (mw / tx["rating_mva"]) ** 2 + self.world.rng.uniform(-0.6, 0.6)
            for attr, v in (("MW", mw), ("TOP_OIL_TEMP", top)):
                self.buf["scada"].append(dict(ref=f"{tx['asset_id']}/{'MMXU1.TotW' if attr == 'MW' else 'YPTR1.HPTmpOil'}",
                                              tag=f"{tx['asset_id'][3:]}.{attr}.PV", element=tx["asset_id"], attribute=attr, feeder_id=None,
                                              ts=t, value=round(v, 3), quality="good", source="SCADA"))
            zs[tx["asset_id"]] = dict(mw=round(mw, 2), top_oil=round(top, 1), rating=tx["rating_mva"])
        self.pg.q("""INSERT INTO app.live_state (key, value, updated_at) VALUES ('feeders', %s, now()), ('zone_transformers', %s, now())
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (json.dumps(snap), json.dumps(zs)))

    def move_crews(self, t, tick):
        """Drive each dispatched crew towards its job; on arrival it is on site and the repair starts."""
        rows = self.pg.q("""SELECT c.crew_id, c.lat, c.lon, c.status, c.job_id, j.lat, j.lon, j.region_id FROM rm.crew_live c
                            JOIN (SELECT job_id, lat, lon, region_id FROM rm.raised_job
                                  UNION ALL SELECT job_id, lat, lon, region_id FROM gld_field_job WHERE job_date = %s) j ON j.job_id = c.job_id
                            WHERE c.status IN ('travelling', 'on_site')""", (self.cfg["as_of"],), fetch=True) or []
        for crew_id, lat, lon, status, job_id, jlat, jlon, region in rows:
            if status == "travelling":
                d_lat, d_lon = jlat - lat, jlon - lon
                dist_km = math.hypot(d_lat * 110.5, d_lon * 92.5)
                step = 60 / 3600 * TICK_S * SPEEDUP
                if dist_km <= step:
                    lat, lon, status = jlat, jlon, "on_site"
                    self.pg.q("""INSERT INTO rm.field_status (region_id, job_id, status, updated_at) VALUES (%s,%s,'in_progress',now())
                                 ON CONFLICT (region_id, job_id) DO UPDATE SET status = 'in_progress', updated_at = now()""", (region, job_id))
                    self.bump(region)
                    self.on_arrival(job_id, crew_id)
                else:
                    lat, lon = lat + d_lat * step / dist_km, lon + d_lon * step / dist_km
                self.pg.q("UPDATE rm.crew_live SET lat = %s, lon = %s, status = %s, updated_at = now() WHERE crew_id = %s", (lat, lon, status, crew_id))
            else:
                self.pg.q("UPDATE rm.crew_live SET updated_at = now() WHERE crew_id = %s", (crew_id,))

    # ------------------------------------------------------------------ storyline: trips, FLISR, restoration
    def plan_trip(self, feeder_id=None):
        """Where a trip happens: the operating device, the faulted span below it, the switch FLISR opens and the customers it drops."""
        w = self.world
        r = w.rng
        if feeder_id is None:
            feeder_id = r.choice([f for f in w.feeders if f not in [o["feeder_id"] for o in self.outages.values()]])
        devs = [d for d in w.net.devices if d["feeder_id"] == feeder_id and d["type"] in ("CB", "RECLOSER")]
        dev = r.choice(devs) if r.random() < 0.6 else next(d for d in devs if d["type"] == "CB")
        below = [n for n in w.topo.downstream(dev["node_id"]) if w.node[n]["kind"] != "zs_bus"]
        fault_node = r.choice(below[len(below) // 3:]) if len(below) > 3 else dev["node_id"]
        sw = w.topo.upstream_switch(fault_node, dev)
        return dict(feeder_id=feeder_id, dev=dev, fault_node=fault_node, sw=sw, affected=w.nmis_below(dev["node_id"]))

    def trip(self, feeder_id=None, cause=None, plan=None, started=None, repair_hours=None):
        """Inject a trip now, or (started, repair_hours) one that began earlier and needs a long repair."""
        w = self.world
        r = w.rng
        p = plan or self.plan_trip(feeder_id)
        feeder_id, dev, fault_node, sw, affected = p["feeder_id"], p["dev"], p["fault_node"], p["sw"], p["affected"]
        if not affected:
            return None
        t = started or now()
        oid = f"LIVE-{t:%H%M%S}-{feeder_id}"
        cause = cause or r.choice(["VEG", "WND", "EQF", "ANM", "UNK"])
        fn = w.node[fault_node]
        o = dict(outage_id=oid, feeder_id=feeder_id, device_id=dev["device_id"], device_type=dev["type"], start=t, fault_node=fault_node,
                 switch=sw, affected=[m["nmi"] for m in affected], customers_off=len(affected), cause=cause, lat=fn["lat"], lon=fn["lon"],
                 flisr_at=t + dt.timedelta(seconds=r.randint(40, 75)), flisr_done=False, restored=False,
                 ls=sum(1 for m in affected if m["life_support"]))
        self.outages[oid] = o
        if dev["type"] == "CB":
            w.cb_open.add(feeder_id)
        ref = f"{dev['device_id']}/XCBR1.Pos.stVal"
        self.buf["scada"].append(dict(ref=ref, tag=None, element=dev["device_id"], attribute="POS", feeder_id=feeder_id, ts=t, value=0.0,
                                      quality="good", source="ADMS"))
        for m in affected:
            self.buf["meter_event"].append(dict(nmi=m["nmi"], meter_serial=f"M{m['nmi'][-6:]}", event_code="POWER_OUTAGE",
                                                ts=t + dt.timedelta(seconds=r.randint(5, 90)), metering_coordinator="MC-ONE"))
        for _ in range(min(40, max(2, len(affected) // 25))):
            m = r.choice(affected)
            self.buf["call"].append(dict(call_id=f"C{uuid.uuid4().hex[:8]}", ts=t + dt.timedelta(seconds=r.randint(30, 400)), nmi=m["nmi"],
                                         reason=r.choice(["No power", "No power", "Partial power", "Wires down", "Flickering lights"]), channel="IVR"))
        etr = t + (dt.timedelta(hours=repair_hours) if repair_hours else dt.timedelta(minutes=r.randint(70, 160)))
        self.pg.q("""INSERT INTO atlas.live_outage (outage_id, feeder_id, device_id, start_time, status, customers_off, customers_initial,
                     life_support_off, cause_code, etr, stages, lat, lon) VALUES (%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT DO NOTHING""",
                  (oid, feeder_id, dev["device_id"], t, len(affected), len(affected), o["ls"], cause, etr,
                   json.dumps([dict(ts=t.isoformat(), action="PROTECTION_TRIP", device=dev["device_id"], customers=len(affected))]),
                   fn["lat"], fn["lon"]))
        sid = f"SIT-TRIP-{oid}"
        crit = sum(1 for m in affected if m["critical_site"])
        self.pg.q("""INSERT INTO atlas.situation (situation_id, type, title, severity, status, source, feeders, customers, life_support,
                     critical_sites, headline, payload) VALUES (%s,'trip',%s,%s,'detected','detector',%s,%s,%s,%s,%s,%s)
                     ON CONFLICT DO NOTHING""",
                  (sid, f"{w.feeders[feeder_id]['name']} has tripped", "critical" if len(affected) > 800 or o["ls"] else "high", [feeder_id],
                   len(affected), o["ls"], crit,
                   f"{dev['device_id']} opened. {_count(len(affected), 'customer')} {'is' if len(affected) == 1 else 'are'} off supply while FLISR isolates the fault.",
                   json.dumps(dict(outage_id=oid, device_id=dev["device_id"], fault_lat=fn["lat"], fault_lon=fn["lon"],
                                   sectionalising_switch=sw["device_id"] if sw else None, cause_code=cause))))
        self.pg.q("UPDATE atlas.live_outage SET situation_id = %s WHERE outage_id = %s", (sid, oid))
        self.pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,'detector','detected',%s)",
                  (sid, json.dumps(dict(trigger=f"{dev['device_id']} protection trip"))))
        region = self.region_of.get(feeder_id)
        dur = r.randint(60, 150)
        self.pg.q("""INSERT INTO rm.raised_job (job_id, job_date, job_type, title, priority, duration_min, window_start_min, window_end_min, region_id,
                     depot_id, lat, lon, required_skill, required_ticket, funding, feeder_id, asset_id, customers, est_customer_minutes,
                     source, situation_id, is_emergency)
                     SELECT %s, (now() AT TIME ZONE 'Australia/Sydney')::date, 'FAULT_RESPONSE', %s, 5, %s, 0, 1440, %s, depot_id, %s, %s,
                     'LV_HV', 'HV_SWITCHING', 'EMERGENCY', %s, %s, %s, %s, 'outage', %s, true FROM gld_rm_region WHERE region_id = %s
                     ON CONFLICT (job_id) DO NOTHING""",
                  (f"JOB-{oid}", f"Fault response {feeder_id}: patrol and repair downstream of {sw['device_id'] if sw else dev['device_id']}",
                   dur, region, fn["lat"], fn["lon"], feeder_id, dev["device_id"], len(affected), len(affected) * dur, sid, region))
        if region:
            self.bump(region)
        return oid

    def advance_outages(self, t):
        w = self.world
        for oid, o in list(self.outages.items()):
            if not o["flisr_done"] and t >= o["flisr_at"]:
                o["flisr_done"], at = True, o["flisr_at"]
                if o["switch"]:
                    still = {m["nmi"] for m in w.nmis_below(o["switch"]["node_id"])}
                    restored = [n for n in o["affected"] if n not in still]
                    if restored:
                        for n in restored:
                            self.buf["meter_event"].append(dict(nmi=n, meter_serial=f"M{n[-6:]}", event_code="POWER_RESTORE",
                                                                ts=at + dt.timedelta(seconds=w.rng.randint(2, 30)), metering_coordinator="MC-ONE"))
                        o["affected"] = [n for n in o["affected"] if n in still]
                        o["customers_off"] = len(o["affected"])
                        w.cb_open.discard(o["feeder_id"])
                        self.buf["scada"].append(dict(ref=f"{o['switch']['device_id']}/XSWI1.Pos.stVal", tag=None, element=o["switch"]["device_id"],
                                                      attribute="POS", feeder_id=o["feeder_id"], ts=at, value=0.0, quality="good", source="ADMS"))
                        self.pg.q("""UPDATE atlas.live_outage SET customers_off = %s, stages = stages || %s::jsonb WHERE outage_id = %s""",
                                  (o["customers_off"], json.dumps([dict(ts=at.isoformat(), action="FLISR_RESTORE", device=o["switch"]["device_id"],
                                                                        customers=len(restored))]), oid))
                        self.pg.q("""UPDATE atlas.situation SET headline = %s, customers = %s, updated_at = now() WHERE situation_id = %s""",
                                  (f"FLISR isolated the fault at {o['switch']['device_id']} and restored {_count(len(restored), 'customer')} automatically. "
                                   f"{_count(o['customers_off'], 'customer')} {'is' if o['customers_off'] == 1 else 'are'} still off, and a fault patrol is needed.",
                                   o["customers_off"], f"SIT-TRIP-{oid}"))

    def on_arrival(self, job_id, crew_id):
        self.repairs[job_id.replace("JOB-", "", 1)] = dict(at=now() + dt.timedelta(seconds=self.world.rng.randint(150, 300)), crew=crew_id)

    def finish_repairs(self, t):
        for oid, rp in list(self.repairs.items()):
            if t < rp["at"]:
                continue
            del self.repairs[oid]
            o = self.outages.pop(oid, None)
            if o:
                for n in o["affected"]:
                    self.buf["meter_event"].append(dict(nmi=n, meter_serial=f"M{n[-6:]}", event_code="POWER_RESTORE",
                                                        ts=t + dt.timedelta(seconds=self.world.rng.randint(2, 30)), metering_coordinator="MC-ONE"))
                self.world.cb_open.discard(o["feeder_id"])
            row = self.pg.q("SELECT feeder_id, device_id, customers_off, cause_code FROM atlas.live_outage WHERE outage_id = %s AND status = 'active'",
                            (oid,), fetch=True)
            self.pg.q("UPDATE rm.field_status SET status = 'done', updated_at = now() WHERE job_id = %s", (f"JOB-{oid}",))
            self.pg.q("UPDATE rm.crew_live SET status = 'available', job_id = NULL, updated_at = now() WHERE crew_id = %s", (rp["crew"],))
            if row:
                feeder_id, device_id, customers, cause = row[0]
                self.bump(self.region_of.get(feeder_id))
                self.close_outage(oid, t, device_id, customers, cause, rp["crew"])

    def close_outage(self, oid, at, device_id, customers, cause, crew=None):
        self.pg.q("""UPDATE atlas.live_outage SET status = 'restored', customers_off = 0, restored_at = %s, crew_id = %s,
                     stages = stages || %s::jsonb WHERE outage_id = %s""",
                  (at, crew, json.dumps([dict(ts=at.isoformat(), action="REPAIR_COMPLETE", device=device_id, customers=customers)]), oid))
        if not crew:   # repaired by the field without a dispatch through the twin: the emergency job comes off the plan
            self.pg.q("DELETE FROM rm.raised_job WHERE job_id = %s RETURNING region_id", (f"JOB-{oid}",))
            self.pg.q("DELETE FROM rm.field_status WHERE job_id = %s", (f"JOB-{oid}",))
            self.pg.q("DELETE FROM rm.assignment_override WHERE job_id = %s", (f"JOB-{oid}",))
            self.bump()
        self.pg.q("""UPDATE atlas.situation SET status = 'closed', headline = %s, customers = 0, updated_at = now() WHERE situation_id = %s""",
                  (f"Supply restored after repair by {self.crew_name.get(crew, 'the field crew')}. Cause: {self.cause_label.get(cause, 'under investigation')}.", f"SIT-TRIP-{oid}"))
        self.pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,'field','closed',%s)",
                  (f"SIT-TRIP-{oid}", json.dumps(dict(crew=crew))))
        if crew:   # the crew's report back from the field, as the field app files it
            self.pg.q("""INSERT INTO rm.field_report (report_id, job_id, situation_id, outage_id, region_id, crew_id, asset_id, reported_by, reported_at,
                           patrol_complete, fault_located, switching_executed, customers_restored, outcome, notes)
                         SELECT 'FR-' || upper(substr(md5(random()::text), 1, 10)), 'JOB-' || %(oid)s, %(sid)s, %(oid)s, j.region_id, %(crew)s,
                                %(dev)s, 'live engine', %(at)s, true, true, true, %(cust)s, 'fixed',
                                'Patrolled, fault found and repaired; supply restored to the remaining customers.'
                         FROM (SELECT region_id FROM rm.raised_job WHERE job_id = 'JOB-' || %(oid)s) j""",
                      dict(oid=oid, sid=f"SIT-TRIP-{oid}", crew=crew, dev=device_id, at=at, cust=int(customers or 0)))

    def settle_outages(self):
        """Outages nobody dispatched to are repaired by the field at their estimated restoration time, including ones left from an earlier
        run of the engine, so live outages never pile up and age past the guaranteed service level by accident."""
        rows = self.pg.q("""SELECT o.outage_id, o.etr, o.device_id, o.customers_off, o.cause_code FROM atlas.live_outage o
                            WHERE o.status = 'active' AND o.etr < now() AND NOT EXISTS (SELECT 1 FROM rm.field_status f
                              WHERE f.job_id = 'JOB-' || o.outage_id AND f.status IN ('driving', 'in_progress'))""", fetch=True) or []
        for oid, etr, device_id, customers, cause in rows:
            o = self.outages.pop(oid, None)
            if o:
                self.world.cb_open.discard(o["feeder_id"])
                for n in o["affected"]:
                    self.buf["meter_event"].append(dict(nmi=n, meter_serial=f"M{n[-6:]}", event_code="POWER_RESTORE",
                                                        ts=etr + dt.timedelta(seconds=self.world.rng.randint(2, 30)), metering_coordinator="MC-ONE"))
            self.close_outage(oid, etr, device_id, customers, cause)

    def stop_asked(self):
        """Live ingestion was switched off in the app."""
        r = self.pg.q("SELECT value ->> 'status' FROM app.live_state WHERE key = 'engine'", fetch=True)
        return bool(r) and r[0][0] == "stopping"


    # ------------------------------------------------------------------ warm path
    def flush(self):
        """Append each stream's buffered records to its Lakebase table (one statement per stream); the change data feed takes them on."""
        from records import LAND
        for k, rows in self.buf.items():
            if not rows:
                continue
            batch, self.buf[k] = rows, []
            cols = ", ".join(c for c, _ in self.stream_columns(LAND[k][0]))
            self.pg.q(f"INSERT INTO live.{k} ({cols}) SELECT {cols} FROM jsonb_populate_recordset(NULL::live.{k}, %s::jsonb)",
                      (json.dumps(batch, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)),))

    def purge(self):
        """Lakebase keeps the recent hours of each stream for the screens; the lakehouse already holds every record."""
        from records import LAND
        for k in LAND:
            self.pg.q(f"DELETE FROM live.{k} WHERE ts < now() - make_interval(hours => %s)", (KEEP_H,))

    # ------------------------------------------------------------------ detector
    def detect(self):
        try:
            import detector
            detector.run(self.spark, self.pg, self.fq, self.cfg)
        except Exception as e:  # noqa: BLE001 - detection must never stop the stream
            print("detector error", repr(e)[:400], flush=True)

    def run(self, minutes):
        self.load_rm()
        self.settle_outages()
        self.pg.q("""INSERT INTO app.live_state (key, value) VALUES ('engine', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                     updated_at = now()""", (json.dumps(dict(status="live", started=now().isoformat(), minutes=minutes)),))
        end = time.time() + minutes * 60
        tick = 0
        self.detect()
        while time.time() < end:
            t0 = time.time()
            t = now()
            if self.stop_asked():
                break
            self.emit_scada(t)
            self.move_crews(t, tick)
            self.advance_outages(t)
            self.finish_repairs(t)
            if time.time() - self.last_trip > TRIP_EVERY_MIN * 60 and len(self.outages) < 3:
                self.trip()
                self.last_trip = time.time()
            if time.time() - self.last_flush > FLUSH_S:
                self.flush()
                self.last_flush = time.time()
                self.pg.q("""UPDATE app.live_state SET value = value || %s::jsonb, updated_at = now() WHERE key = 'engine'""",
                          (json.dumps(dict(last_flush=now().isoformat(), active_outages=len(self.outages))),))
            if time.time() - self.last_purge > PURGE_S:
                self.purge()
                self.last_purge = time.time()
            if time.time() - self.last_detect > DETECT_S:
                self.settle_outages()
                self.detect()
                self.last_detect = time.time()
            tick += 1
            time.sleep(max(0.0, TICK_S - (time.time() - t0)))
        self.flush()
        self.pg.q("UPDATE app.live_state SET value = value || '{\"status\": \"paused\"}'::jsonb, updated_at = now() WHERE key = 'engine'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--minutes", type=float, default=60)
    a, _ = ap.parse_known_args()
    sys.path[:0] = [os.path.join(a.src, "ops"), os.path.join(a.src, "generator")]
    Engine(a).run(a.minutes)


if __name__ == "__main__":
    main()
