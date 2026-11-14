"""History generator: two years of network events conditioned on real historical weather.

Writes, in native shapes:
- adms/outages/*.jsonl         ADMS outage records with restoration stages (FLISR, switching, repair)
- adms/open_outages/*.jsonl    ADMS active incidents: the outages still in progress at the as-of moment, with the work so far
- lidar/campaign_YYYY/*.csv    per-span clearance results per annual campaign
- sap/QMEL, sap/QMIH           inspection defects as SAP notifications (ODP change files)
- sap/AUFK                     work orders (corrective, preventive, vegetation, storm repair)
- drone/patrol_*/              COCO-style detections with GPS metadata (images referenced by file name)
- wms/work_orders_*.csv        the work management system's planned field work for the coming days (workforce.py)
Stdlib only.
"""
import csv
import datetime as dt
import json
import math
import os

import workforce
from network import rng_for

CAUSES = [  # code, label, base weight, weather driver
    ("VEG", "Vegetation contact", 0.24, "wind"), ("WND", "Wind-blown debris", 0.10, "wind"),
    ("LTN", "Lightning", 0.10, "storm"), ("EQF", "Equipment failure", 0.22, "heat"),
    ("ANM", "Animal contact", 0.12, "none"), ("VEH", "Vehicle impact", 0.06, "none"),
    ("TPD", "Third-party damage", 0.06, "none"), ("UNK", "Cause unknown", 0.10, "none"),
]


class Topology:
    """True connectivity (the generator's ground truth) for impact and restoration."""

    def __init__(self, net):
        self.net = net
        self.children = {}
        self.parent_span = {}
        for s in net.spans:
            if s["role"] == "tie":
                continue
            self.children.setdefault(s["from_node"], []).append(s["to_node"])
            self.parent_span[s["to_node"]] = s
        self.node = {n["node_id"]: n for n in net.nodes}
        self.ds_at = {d["node_id"]: d for d in net.dss}
        self.cust_by_ds = {}
        self.ls_by_ds = {}
        for m in net.nmis:
            self.cust_by_ds[m["ds_id"]] = self.cust_by_ds.get(m["ds_id"], 0) + 1
            if m["life_support"]:
                self.ls_by_ds[m["ds_id"]] = self.ls_by_ds.get(m["ds_id"], 0) + 1
        self.dev_at = {}
        for d in net.devices:
            if d["type"] != "NOP":
                self.dev_at.setdefault(d["node_id"], []).append(d)
        self._down = {}

    def downstream(self, node_id):
        if node_id in self._down:
            return self._down[node_id]
        seen, stack = [], [node_id]
        while stack:
            n = stack.pop()
            seen.append(n)
            stack.extend(self.children.get(n, []))
        self._down[node_id] = seen
        return seen

    def customers_below(self, node_id):
        c = ls = 0
        for n in self.downstream(node_id):
            d = self.ds_at.get(n)
            if d:
                c += self.cust_by_ds.get(d["ds_id"], 0)
                ls += self.ls_by_ds.get(d["ds_id"], 0)
        return c, ls

    def protecting_device(self, node_id):
        """Nearest upstream protective device (fuse, isolator, recloser or breaker) for a fault at node_id."""
        n = node_id
        while True:
            for d in self.dev_at.get(n, []):
                if d["type"] in ("FUSE", "SWER_ISOLATOR", "RECLOSER", "CB"):
                    return d
            s = self.parent_span.get(n)
            if not s:
                return None
            n = s["from_node"]

    def upstream_switch(self, node_id, stop_dev):
        """Nearest upstream sectionalising switch (gas switch or recloser) below the operating device."""
        n = node_id
        while True:
            for d in self.dev_at.get(n, []):
                if d["device_id"] == stop_dev["device_id"]:
                    return None
                if d["type"] in ("GAS_SWITCH", "RECLOSER"):
                    return d
            s = self.parent_span.get(n)
            if not s:
                return None
            n = s["from_node"]


def _weather(cfg_dir):
    with open(os.path.join(cfg_dir, "weather_history.json")) as f:
        w = json.load(f)
    by_zone = {}
    for z in w["zones"]:
        days = {}
        for i, t in enumerate(z["time"]):
            days[t] = dict(tmax=z["temperature_2m_max"][i] or 22, tmin=z["temperature_2m_min"][i] or 12, rain=z["precipitation_sum"][i] or 0,
                           gust=z["wind_gusts_10m_max"][i] or 30, wind=z["wind_speed_10m_max"][i] or 15)
        by_zone[z["zs"]] = days
    return by_zone


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def _write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def generate_outages(net, topo, weather, as_of):
    """Unplanned outages per feeder-day from a weather-driven fault rate; restoration with FLISR where possible."""
    end = dt.date.fromisoformat(as_of)
    start = end - dt.timedelta(days=net.cfg["history_days"])
    feeders = net.feeders
    zone_code = {f["feeder_id"]: f["zs_id"][3:] for f in feeders}
    spans_by_feeder = {}
    for s in net.spans:
        if s["role"] != "tie":
            spans_by_feeder.setdefault(s["feeder_id"], []).append(s)
    nop_by_feeder = {d["feeder_id"]: d for d in net.devices if d["type"] == "NOP"}
    outages = []
    oid = 0
    day = start
    while day < end:
        key = day.isoformat()
        for f in feeders:
            w = weather.get(zone_code[f["feeder_id"]], {}).get(key)
            if not w:
                continue
            r = rng_for(net.seed, "fault", f["feeder_id"], key)
            oh_km = sum(s["length_m"] for s in spans_by_feeder[f["feeder_id"]] if s["overhead"]) / 1000
            base = 0.0008 * oh_km + 0.002
            wind = math.exp(max(0, w["gust"] - 55) / 11)
            storm = 1 + (w["rain"] > 25) * 1.8 + (w["rain"] > 50) * 2.5
            heat = 1 + max(0, w["tmax"] - 35) * 0.25
            lam = base * wind * storm * heat * (1.25 if f["bushfire_area"] else 1.0)
            # Poisson draw by inversion.
            p, u, k = math.exp(-lam), r.random(), 0
            cum = p
            while u > cum and k < 25:
                k += 1
                p *= lam / k
                cum += p
            for _ in range(k):
                oid += 1
                o = _one_outage(topo, f, spans_by_feeder[f["feeder_id"]], nop_by_feeder.get(f["feeder_id"]), day, w, r, oid)
                if o:
                    outages.append(o)
        day += dt.timedelta(days=1)
    return outages


def _one_outage(topo, f, spans, nop, day, w, r, oid):
    """One unplanned outage on a feeder: the protecting device that trips, its auto-reclose or FLISR and repair, as an ADMS record
    (None when the fault cannot interrupt any customer)."""
    # Cause, weighted by the day's weather.
    weights = []
    for code, label, base, drv in CAUSES:
        m = 1.0
        if drv == "wind":
            m = 1 + max(0, w["gust"] - 50) / 12
        elif drv == "storm":
            m = 1 + (w["rain"] > 15) * 3 + max(0, w["gust"] - 60) / 20
        elif drv == "heat":
            m = 1 + max(0, w["tmax"] - 33) * 0.3
        weights.append(base * m)
    cause = r.choices(CAUSES, weights)[0]
    # Fault location: overhead spans weighted by length (vegetation and wind prefer bushfire-prone, older spans).
    cand = [s for s in spans if s["overhead"]] or spans
    sw = [s["length_m"] * (1.6 if s["bushfire_prone"] and cause[3] == "wind" else 1.0) *
          (1 + max(0, 2000 - s["install_year"]) / 40) for s in cand]
    span = r.choices(cand, sw)[0]
    fault_node = span["to_node"]
    dev = topo.protecting_device(fault_node)
    if not dev:
        return None
    hour = r.choices(range(24), [1 + (h in range(14, 22)) * 2.5 for h in range(24)])[0]
    t0 = dt.datetime.combine(day, dt.time(hour, r.randint(0, 59), r.randint(0, 59)))
    cust, ls = topo.customers_below(dev["node_id"])
    if cust == 0:
        return None
    stages = []
    oid_s = f"OUT-{day.strftime('%y%m%d')}-{oid:05d}"
    # Transient faults on reclosers and breakers: auto-reclose succeeds (momentary, not counted as sustained).
    if dev["type"] in ("RECLOSER", "CB") and r.random() < 0.75:
        tr = t0 + dt.timedelta(seconds=r.choice([2, 5, 15]))
        return dict(outage_id=oid_s, feeder_id=f["feeder_id"], device_id=dev["device_id"], device_type=dev["type"],
                    span_id=span["span_id"], start_time=t0, end_time=tr, planned=False, momentary=True,
                    cause_code=cause[0], customers_interrupted=cust, life_support_affected=ls, stages=[],
                    customer_minutes=0, weather=dict(gust_kmh=w["gust"], rain_mm=w["rain"], tmax_c=w["tmax"]))
    # Sustained outage. Severe weather slows restoration (crew-constrained).
    severity = 1 + max(0, w["gust"] - 65) / 20 + (w["rain"] > 40) * 0.8
    restore_min = r.lognormvariate(math.log({"VEG": 95, "WND": 90, "LTN": 75, "EQF": 120, "ANM": 60, "VEH": 150,
                                             "TPD": 115, "UNK": 70}[cause[0]]), 0.5) * severity
    remaining = cust
    cmi = 0.0
    # FLISR on breaker or recloser faults: isolate at the nearest switch upstream of the fault, restore the healthy part.
    if dev["type"] in ("CB", "RECLOSER"):
        sw = topo.upstream_switch(fault_node, dev)
        if sw:
            below, _ = topo.customers_below(sw["node_id"])
            healthy = cust - below
            if healthy > 0:
                t_flisr = t0 + dt.timedelta(seconds=r.randint(45, 150))
                stages.append(dict(ts=t_flisr, action="FLISR_RESTORE_UPSTREAM", device=sw["device_id"], customers_restored=healthy))
                cmi += healthy * (t_flisr - t0).total_seconds() / 60
                remaining = below
                # Downstream via the tie when the fault is not at the trunk end.
                if nop and remaining > 10 and r.random() < 0.5:
                    back = int(remaining * r.uniform(0.3, 0.6))
                    t_tie = t0 + dt.timedelta(minutes=r.randint(25, 70))
                    stages.append(dict(ts=t_tie, action="BACKFEED_VIA_TIE", device=nop["device_id"], customers_restored=back))
                    cmi += back * (t_tie - t0).total_seconds() / 60
                    remaining -= back
    t_end = t0 + dt.timedelta(minutes=max(20, restore_min))
    stages.append(dict(ts=t_end, action="REPAIR_COMPLETE", device=dev["device_id"], customers_restored=remaining))
    cmi += remaining * (t_end - t0).total_seconds() / 60
    return dict(outage_id=oid_s, feeder_id=f["feeder_id"], device_id=dev["device_id"], device_type=dev["type"],
                span_id=span["span_id"], start_time=t0, end_time=t_end, planned=False, momentary=False,
                cause_code=cause[0], customers_interrupted=cust, life_support_affected=ls, stages=stages,
                customer_minutes=round(cmi, 1), weather=dict(gust_kmh=w["gust"], rain_mm=w["rain"], tmax_c=w["tmax"]))


def write_outages(root, outages):
    by_month = {}
    for o in outages:
        by_month.setdefault(o["start_time"].strftime("%Y%m"), []).append(o)
    for m, rows in by_month.items():
        _write_jsonl(os.path.join(root, "adms", "outages", f"outages_{m}.jsonl"), rows)


# The outages still in progress at the as-of moment (midnight starting the as-of day, where the history ends): three long repairs past
# the 12 hour guaranteed service level and one recent fault, each on a fused spur (so its customers are exactly the spur's), with the
# work done so far as ADMS stages. key, area style, underground, cause, customers (min, max), hours off at the snapshot, hours to the
# estimated restoration, fault kind, repair job type, repair minutes, access, hazards, materials used so far, stages (minutes after the
# start, action, note), the patrol crew's note (None: no field report yet).
OPEN_OUTAGES = [
    ("bushland_tree", "rural", False, "VEG", (20, 90), (19, 22), (4, 6), "span", "FAULT_RESPONSE", 240, "restricted", ["tree", "live_wire"],
     [("conductor", 90), ("tie_wire", 6)],
     [(0, "FUSE_OPERATED", None), (25, "PATROL_STARTED", "Line crew patrolling the spur from the fuse"),
      (140, "FAULT_LOCATED", "Eucalypt uprooted across span {span}; conductor on the ground in steep bushland"),
      (175, "MADE_SAFE", "Spur isolated and earthed at the fuse; the fire trail taped off"),
      (300, "CUSTOMERS_CONTACTED", "Customers on the spur called with a restoration estimate"),
      (420, "PLANT_BOOKED", "Vegetation crew and a tracked elevated work platform booked for first light; the fire trail needs clearing first")],
     "Tree came down across the line in steep country and the conductor is on the ground. Made safe overnight. Needs a vegetation crew "
     "to cut the tree and clear the fire trail, then about 90 m of conductor restrung."),
    ("cable_fault", "metro", True, "EQF", (60, 160), (14, 16), (5, 7), "cable", "CABLE_REPAIR", 300, "clear", ["traffic"], [],
     [(0, "FUSE_OPERATED", None), (30, "PATROL_STARTED", "Line crew checking the spur and the kiosks"),
      (95, "FAULT_LOCATED", "Underground cable fault pinpointed in section {span} between two pits (thumper and time domain reflectometer)"),
      (120, "MADE_SAFE", "Cable isolated and earthed at both ends"),
      (150, "CUSTOMERS_CONTACTED", "Life support customers on the spur called: alternative arrangements confirmed"),
      (480, "PLANT_BOOKED", "Excavation permit approved; cable jointers, a joint kit and traffic control booked for this morning")],
     "Underground cable fault located between pits; the spur cannot be back-fed. Needs cable jointers and an excavation crew, with "
     "traffic control on the street."),
    ("pole_down", "coastal", False, "VEH", (40, 140), (13, 15), (2, 4), "pole", "FAULT_RESPONSE", 300, "restricted", ["unstable_pole", "traffic"],
     [("pole", 1), ("cross_arm", 1), ("insulator", 3)],
     [(0, "FUSE_OPERATED", None), (20, "PATROL_STARTED", "Line crew sent to a report of a car into a pole"),
      (50, "FAULT_LOCATED", "Car hit pole {pole}; the pole snapped at the base and is leaning on the conductors"),
      (80, "MADE_SAFE", "Spur isolated at the fuse; police holding the road"),
      (210, "GENERATOR_CONNECTED", "Mobile generator connected at {ds}, picking up the customers it supplies"),
      (360, "PLANT_BOOKED", "Replacement pole, crane truck and traffic control booked for this morning")],
     "Pole snapped at the base after a car hit it; leaning on the conductors, made safe and isolated. A generator is carrying part of "
     "the spur. Needs a new pole, cross arm and insulators with a crane truck and traffic control."),
    ("possum", "metro", False, "ANM", (20, 80), (2.5, 4), (1, 2), "transformer", "FAULT_RESPONSE", 90, "clear", [], [],
     [(0, "FUSE_OPERATED", None), (35, "PATROL_STARTED", "Line crew patrolling the spur"),
      (80, "FAULT_LOCATED", "Possum on the bushings of {ds}; HV fuse and surge arrester damaged")],
     None),
]


def generate_open_outages(net, topo, as_of):
    """The ADMS active incidents at the as-of moment (OPEN_OUTAGES), each placed on a real fused spur of the right kind of area."""
    snap = dt.datetime.combine(dt.date.fromisoformat(as_of), dt.time(0))
    wf = workforce.build_workforce(net)
    feeder = {f["feeder_id"]: f for f in net.feeders}
    style = {f["feeder_id"]: net.areas[f["area"]]["style"] for f in net.feeders}
    span_to = {s["to_node"]: s for s in net.spans if s["role"] != "tie"}
    used, out = set(), []
    for n, (key, area_style, ug, cause, (cmin, cmax), hours, etr_h, kind, job_type, job_min, access, hazards, materials, stages, note) \
            in enumerate(OPEN_OUTAGES, 1):
        r = rng_for(net.seed, "open_outage", key)
        cands = []
        for d in sorted(net.devices, key=lambda d: d["device_id"]):
            if d["type"] != "FUSE" or style[d["feeder_id"]] != area_style or d["feeder_id"] in used:
                continue
            nodes = topo.downstream(d["node_id"])
            spans = [span_to[x] for x in nodes if x in span_to and span_to[x]["overhead"] != ug]
            if kind == "span":
                spans = [x for x in spans if x["bushfire_prone"]]
            dss = [topo.ds_at[x] for x in nodes if x in topo.ds_at and topo.cust_by_ds.get(topo.ds_at[x]["ds_id"])]
            cust, ls = topo.customers_below(d["node_id"])
            if spans and dss and cmin <= cust <= cmax and (key != "cable_fault" or ls) and (key != "pole_down" or len(dss) > 1):
                cands.append((d, spans, dss, cust, ls))
        if not cands:
            continue
        dev, spans, dss, cust, ls = r.choice(cands)
        used.add(dev["feeder_id"])
        span = r.choice(spans)
        ds = r.choice(dss)
        node = topo.node[ds["node_id"] if kind == "transformer" else span["to_node"]]   # where the fault is
        start = snap - dt.timedelta(hours=round(r.uniform(*hours), 2))
        restored = topo.cust_by_ds.get(ds["ds_id"], 0) if key == "pole_down" else 0
        ls_restored = topo.ls_by_ds.get(ds["ds_id"], 0) if key == "pole_down" else 0
        fill = dict(span=span["span_id"], pole=node.get("pole_id") or span["span_id"], ds=ds["ds_id"])
        region = wf["zone_region"][feeder[dev["feeder_id"]]["zs_id"]]
        crews = sorted(c["crew_id"] for c in wf["crews"] if c["region"] == region and c["persona"] == "Linesperson")
        out.append(dict(
            outage_id=f"OUT-{snap:%y%m%d}-A{n:02d}", status="IN_PROGRESS", snapshot_time=snap, feeder_id=dev["feeder_id"],
            device_id=dev["device_id"], device_type=dev["type"], span_id=span["span_id"],
            fault_asset_id=fill["pole"] if kind == "pole" else ds["ds_id"] if kind == "transformer" else span["span_id"], fault_kind=kind,
            fault_lat=node["lat"], fault_lon=node["lon"], cause_code=cause, start_time=start,
            etr=snap + dt.timedelta(hours=round(r.uniform(*etr_h), 2)), customers_interrupted=cust, customers_off=cust - restored,
            life_support_affected=ls, life_support_off=ls - ls_restored, repair_job_type=job_type, repair_minutes=job_min,
            access_status=access, hazards=hazards, materials=[dict(code=c, qty=q) for c, q in materials],
            stages=[dict(ts=start + dt.timedelta(minutes=m), action=a, device=dev["device_id"],
                         customers_restored=restored if a == "GENERATOR_CONNECTED" else None, note=t.format(**fill) if t else None)
                    for m, a, t in stages],
            crew_id=r.choice(crews) if note and crews else None, crew_note=note))
    return out


def write_open_outages(root, rows, as_of):
    _write_jsonl(os.path.join(root, "adms", "open_outages", f"open_outages_{as_of.replace('-', '')}.jsonl"), rows)


def write_lidar(net, root, as_of):
    """Annual LiDAR campaigns: clearance to vegetation per overhead span, with grow-in between campaigns."""
    year = int(as_of[:4])
    rows_by_year = {y: [] for y in (year - 2, year - 1, year)}
    species = ["Eucalyptus (fast)", "Eucalyptus (moderate)", "Acacia", "Casuarina", "Exotic street tree", "Native shrub"]
    for s in net.spans:
        if not s["overhead"] or s["role"] == "tie":
            continue
        r = rng_for(net.seed, "veg", s["span_id"])
        rural = s["feeder_id"][:3] in ("LPH", "KNV", "GVL")
        sp = r.choices(species, [0.35, 0.2, 0.1, 0.05, 0.2, 0.1] if rural else [0.1, 0.1, 0.05, 0.05, 0.55, 0.15])[0]
        grow = {"Eucalyptus (fast)": 1.4, "Eucalyptus (moderate)": 0.9, "Acacia": 0.8, "Casuarina": 0.7,
                "Exotic street tree": 0.6, "Native shrub": 0.4}[sp] * r.uniform(0.6, 1.3)
        clear = r.uniform(1.2, 6.5) if rural else r.uniform(1.5, 5.0)
        hazard = rural and r.random() < 0.04
        for y in sorted(rows_by_year):
            cut = clear < 1.5 and r.random() < 0.7
            if cut:
                clear = r.uniform(3.5, 5.5)
            cat = "A" if clear < 1.0 else ("B" if clear < 2.0 else ("C" if clear < 3.0 else "CLEAR"))
            rows_by_year[y].append([s["span_id"], s["feeder_id"], f"{y}-0{r.randint(3, 8)}-{r.randint(10, 28)}", round(clear, 2),
                                    cat, sp, round(grow, 2), 1 if hazard else 0, round(r.uniform(0, 3.5), 1),
                                    s["bushfire_prone"] and 1 or 0, 14, 5])
            clear = max(0.1, clear - grow * r.uniform(0.8, 1.2))
    for y, rows in rows_by_year.items():
        _write_csv(os.path.join(root, "lidar", f"campaign_{y}", "span_clearance.csv"),
                   ["span_id", "feeder", "capture_date", "min_vegetation_clearance_m", "encroachment_category",
                    "dominant_species", "estimated_growth_m_per_year", "hazard_tree_flag", "pole_lean_deg",
                    "bushfire_prone_land", "conductor_class_code", "vegetation_class_code"], rows)
    return sum(len(v) for v in rows_by_year.values()), {row[0]: (row[4], row[3]) for row in rows_by_year[year]}


INSPECTION_CYCLE_DAYS = 1643    # every pole is inspected about every 4.5 years (planning assumption for a NSW distributor's wood pole cycle)
INSPECTION_YEARS = 9            # the inspection history kept in the work order extract


def inspection_orders(pole_ids, seed, end, first_order=7000000):
    """Routine pole inspections as SAP orders (AUFK rows, type PM04): each pole on its own 4.5 year cycle, every inspection in the last
    nine years, completed the day it was done. The same pole always gets the same dates and costs (seeded by the pole id)."""
    rows, an = [], first_order
    for pid in sorted(pole_ids):
        r = rng_for(seed, "inspection", pid)
        last = end - dt.timedelta(days=r.randint(1, INSPECTION_CYCLE_DAYS))
        d = last
        while (end - d).days <= INSPECTION_YEARS * 365:
            an += 1
            rows.append(["C", 0, f"{an:012d}", "PM04", f"Routine inspection and ground-line test {pid}", "", pid, d.strftime("%Y%m%d"),
                         d.strftime("%Y%m%d"), "TECO", round(r.uniform(140, 260), 2)])
            d -= dt.timedelta(days=INSPECTION_CYCLE_DAYS + r.randint(-60, 60))
    return rows


def write_defects_and_work(net, root, as_of, outages):
    """Inspection defects as SAP notifications (QMEL/QMIH) and work orders (AUFK-like), as ODP change files."""
    end = dt.date.fromisoformat(as_of)
    qmel, qmih, aufk, open_defects = [], [], [], []
    qn = 100000000
    an = 4000000
    equnr_pole = lambda pid: f"{int(pid[1:]) + 30000000:018d}"
    defect_codes = [("CRACKED_CROSSARM", "Cross-arm cracked or split", 2), ("INSULATOR_DAMAGE", "Insulator chipped or broken", 2),
                    ("POLE_ROT", "Pole below serviceability (rot at ground line)", 1), ("LEANING_POLE", "Pole leaning", 3),
                    ("CONDUCTOR_DAMAGE", "Conductor strand damage", 2), ("VEGETATION_ENCROACH", "Vegetation inside clearance", 3),
                    ("TIE_WIRE_LOOSE", "Tie wire loose", 4), ("FUSE_HOLDER", "Fuse holder damaged", 3)]
    for n in net.nodes:
        if not n["pole_id"]:
            continue
        r = rng_for(net.seed, "defect", n["pole_id"])
        age = end.year - n["install_year"]
        p = 0.025 + max(0, age - 35) * 0.004 + (n["material"] == "WOOD") * 0.02
        if r.random() < p:
            code, text, prio = r.choices(defect_codes, [0.22, 0.18, 0.12 + (n["material"] == "WOOD") * 0.1, 0.1, 0.1, 0.12, 0.1, 0.06])[0]
            qn += 1
            created = end - dt.timedelta(days=int(r.triangular(1, 700, 60)))
            closed = r.random() < 0.6 and (end - created).days > 30
            status = "NOCO" if closed else "OSNO"
            qmel.append(["C", 0, f"{qn:012d}", "M2", f"{code}: {text}"[:40], created.strftime("%Y%m%d"),
                         "093000", str(prio), status, code])
            qmih.append(["C", 0, f"{qn:012d}", equnr_pole(n["pole_id"]), n["pole_id"]])
            if not closed:
                open_defects.append(dict(notification=f"{qn:012d}", pole_id=n["pole_id"], node_id=n["node_id"], feeder_id=n["feeder_id"],
                                         code=code, prio=prio, created=created.isoformat()))
            if closed or r.random() < 0.4:
                an += 1
                aufk.append(["C", 0, f"{an:012d}", "PM02" if prio <= 2 else "PM01", f"Rectify {code.lower()} {n['pole_id']}",
                             f"{qn:012d}", n["pole_id"], created.strftime("%Y%m%d"),
                             (created + dt.timedelta(days=r.randint(5, 90))).strftime("%Y%m%d") if closed else "",
                             "TECO" if closed else "REL", round(r.uniform(900, 14000), 2)])
    for o in outages:
        if o["momentary"]:
            continue
        an += 1
        aufk.append(["C", 0, f"{an:012d}", "PM03", f"Fault repair {o['device_id']} ({o['cause_code']})", "", o["span_id"],
                     o["start_time"].strftime("%Y%m%d"), o["end_time"].strftime("%Y%m%d"), "TECO",
                     round(1500 + (o["end_time"] - o["start_time"]).total_seconds() / 60 * 18, 2)])
    h = ["ODQ_CHANGEMODE", "ODQ_ENTITYCNTR"]
    stamp = end.strftime("%Y%m%d")
    _write_csv(os.path.join(root, "sap", "QMEL", f"QMEL_{stamp}_000001.csv"),
               h + ["QMNUM", "QMART", "QMTXT", "QMDAT", "MZEIT", "PRIOK", "STAT", "FECOD"], qmel)
    _write_csv(os.path.join(root, "sap", "QMIH", f"QMIH_{stamp}_000001.csv"), h + ["QMNUM", "EQUNR", "INVNR"], qmih)
    _write_csv(os.path.join(root, "sap", "AUFK", f"AUFK_{stamp}_000001.csv"),
               h + ["AUFNR", "AUART", "KTEXT", "QMNUM", "INVNR", "ERDAT", "IDAT2", "STAT", "COST_AUD"], aufk)
    # the routine inspection orders come as a second change file of the same extract
    insp = inspection_orders([n["pole_id"] for n in net.nodes if n["pole_id"]], net.seed, end)
    _write_csv(os.path.join(root, "sap", "AUFK", f"AUFK_{stamp}_000002.csv"),
               h + ["AUFNR", "AUART", "KTEXT", "QMNUM", "INVNR", "ERDAT", "IDAT2", "STAT", "COST_AUD"], insp)
    return len(qmel), len(aufk) + len(insp), open_defects


def write_drone(net, root, as_of):
    """Two recent patrols over Ranges feeders: images with GPS EXIF-equivalent metadata and COCO detections."""
    end = dt.date.fromisoformat(as_of)
    classes = [{"id": 1, "name": "cracked_crossarm"}, {"id": 2, "name": "damaged_insulator"}, {"id": 3, "name": "vegetation_contact"},
               {"id": 4, "name": "pole_damage"}, {"id": 5, "name": "bird_nest"}]
    count = 0
    for p, feeders in enumerate((["KNV-F02", "KNV-F04"], ["LPH-F03", "GVL-F01"])):
        day = end - dt.timedelta(days=12 - p * 9)
        images, ann = [], []
        aid = 0
        poles = [n for n in net.nodes if n["pole_id"] and n["feeder_id"] in feeders]
        for i, n in enumerate(poles[::4]):
            r = rng_for(net.seed, "drone", n["pole_id"], p)
            img = dict(id=i + 1, file_name=f"{n['pole_id']}_{day.strftime('%Y%m%d')}.jpg", width=4000, height=3000,
                       date_captured=f"{day}T{9 + i % 6:02d}:{i % 60:02d}:00", gps_latitude=round(n["lat"], 6),
                       gps_longitude=round(n["lon"], 6), gps_altitude_m=round(r.uniform(28, 45), 1), pole_hint=n["pole_id"])
            images.append(img)
            if r.random() < 0.06:
                aid += 1
                c = r.choices(classes, [0.35, 0.25, 0.2, 0.1, 0.1])[0]
                x, y = r.randint(800, 2600), r.randint(500, 1800)
                ann.append(dict(id=aid, image_id=img["id"], category_id=c["id"], bbox=[x, y, r.randint(200, 700), r.randint(150, 500)],
                                area=0, score=round(r.uniform(0.62, 0.97), 3), iscrowd=0))
        for a in ann:
            a["area"] = a["bbox"][2] * a["bbox"][3]
        d = os.path.join(root, "drone", f"patrol_{day.strftime('%Y%m%d')}_{'_'.join(feeders)}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "detections_coco.json"), "w") as f:
            json.dump(dict(info=dict(description="Patrol drone defect detections", date_created=str(day), model="vision-defect v3.2"),
                           images=images, annotations=ann, categories=classes), f)
        count += len(ann)
    return count


def write_all(net, root, cfg_dir):
    topo = Topology(net)
    weather = _weather(cfg_dir)
    outages = generate_outages(net, topo, weather, net.cfg["as_of"])
    write_outages(root, outages)
    open_now = generate_open_outages(net, topo, net.cfg["as_of"])
    write_open_outages(root, open_now, net.cfg["as_of"])
    lid, veg = write_lidar(net, root, net.cfg["as_of"])
    nq, na, open_defects = write_defects_and_work(net, root, net.cfg["as_of"], outages)
    nd = write_drone(net, root, net.cfg["as_of"])
    nw = workforce.write_work_orders(net, root, topo, open_defects, veg)
    sustained = [o for o in outages if not o["momentary"]]
    return dict(outages=len(outages), sustained=len(sustained), open_outages=len(open_now), lidar_rows=lid, notifications=nq,
                work_orders=na, drone_detections=nd, planned_work_orders=nw,
                cmos=round(sum(o["customer_minutes"] for o in sustained)),
                saidi_2y=round(sum(o["customer_minutes"] for o in sustained) / len(net.nmis), 1))
