"""Field workforce and planned work, in the shapes of the systems that hold them.

- workforce/: the rostering system's crew roster (one row per deployable crew: a team and its vehicle, named by its leader), crew
  skills, the learning system's tickets and licences, HR leave, training bookings, the safety system's events, enterprise agreement
  rules, fleet and depots (with the zone substations each serves).
- wms/: the work management system's planned work orders for the coming days, each on a real pole, span, cable, substation,
  device or customer premises, sized from the customers really downstream of it.
A region is a depot: each zone substation belongs to the depot nearest it within its area. Stdlib only.
"""
import datetime as dt
import os

from network import dist_m, rng_for
from writers import _write_csv

# persona -> crew code, skill, fully loaded hourly rate of the crew and its vehicle (AUD), tickets, crew size, business unit,
# vehicle (type, weight) and the share run by contractors
PERSONAS = {
    "Linesperson": dict(code="LN", skill="LV_HV", rate=225, tickets=["HV_SWITCHING", "WORK_HEIGHTS", "EWP", "POLE_RESCUE"],
                        size=(2, 4), bu="Network Operations", vehicle=[("EWP", 0.7), ("LINE_TRUCK", 0.3)], contractor=0.15),
    "Cable Jointer": dict(code="CJ", skill="CABLE_JOINT", rate=245, tickets=["HV_SWITCHING", "CONFINED_SPACE", "CABLE_JOINT"],
                          size=(2, 3), bu="Network Operations", vehicle=[("JOINTING_VAN", 1.0)], contractor=0.20),
    "Substation Technician": dict(code="ST", skill="SUBSTATION", rate=275, tickets=["HV_SWITCHING", "ARC_FLASH", "CONFINED_SPACE"],
                                  size=(2, 2), bu="Substations and Protection", vehicle=[("UTE", 1.0)], contractor=0.08),
    "Protection Technician": dict(code="PT", skill="PROTECTION", rate=260, tickets=["HV_SWITCHING", "ARC_FLASH", "PROTECTION_RELAY"],
                                  size=(2, 2), bu="Substations and Protection", vehicle=[("UTE", 1.0)], contractor=0.08),
    "Vegetation Crew": dict(code="VG", skill="VEG", rate=165, tickets=["CHAINSAW", "EWP", "TRAFFIC_MGMT"],
                            size=(3, 4), bu="Vegetation and Inspection", vehicle=[("VEG_EWP", 1.0)], contractor=1.0),   # all vegetation cutting is contracted out
    "Asset Inspector / Drone": dict(code="AI", skill="INSPECTION", rate=150, tickets=["CASA_REPL", "WORK_HEIGHTS"],
                                    size=(1, 2), bu="Vegetation and Inspection", vehicle=[("DRONE_UTE", 1.0)], contractor=0.25),
    "Civil Crew": dict(code="CV", skill="CIVIL", rate=185, tickets=["TRAFFIC_MGMT", "CONFINED_SPACE"],
                       size=(3, 4), bu="Civil Works", vehicle=[("CIVIL_PLANT", 0.7), ("BORER", 0.3)], contractor=0.35),
    "Metering Technician": dict(code="MT", skill="METERING", rate=140, tickets=["METERING", "WORK_HEIGHTS"],
                                size=(1, 1), bu="Metering Services", vehicle=[("VAN", 1.0)], contractor=0.10),
}
# persona mix by area style: metro has underground estates, the Ranges is overhead and bushfire prone, the Coast is mixed
MIX = {
    "metro": {"Linesperson": .36, "Cable Jointer": .17, "Substation Technician": .08, "Protection Technician": .05, "Vegetation Crew": .06,
              "Asset Inspector / Drone": .05, "Civil Crew": .10, "Metering Technician": .13},
    "rural": {"Linesperson": .40, "Cable Jointer": .03, "Substation Technician": .06, "Protection Technician": .04, "Vegetation Crew": .24,
              "Asset Inspector / Drone": .13, "Civil Crew": .05, "Metering Technician": .05},
    "coastal": {"Linesperson": .38, "Cable Jointer": .10, "Substation Technician": .07, "Protection Technician": .05, "Vegetation Crew": .12,
                "Asset Inspector / Drone": .08, "Civil Crew": .08, "Metering Technician": .12},
}
COMMON_TICKETS = ["FIRST_AID", "CPR"]
TICKET_VALIDITY_MONTHS = {"HV_SWITCHING": 24, "WORK_HEIGHTS": 24, "EWP": 60, "POLE_RESCUE": 12, "CABLE_JOINT": 60, "CONFINED_SPACE": 24,
                          "ARC_FLASH": 36, "PROTECTION_RELAY": 36, "CHAINSAW": 36, "TRAFFIC_MGMT": 36, "CASA_REPL": 24, "FIRST_AID": 36,
                          "CPR": 12, "METERING": 60}
# job type -> skill, ticket, base on-site minutes, customer exposure: minutes at risk per customer downstream if the job slips
# (asset work) or for the premises (customer work)
JOB_TYPES = {
    "LV_MAINT": ("LV_HV", "WORK_HEIGHTS", 80, 0.5),
    "HV_MAINT": ("LV_HV", "HV_SWITCHING", 130, 0.8),
    "CABLE_REPAIR": ("CABLE_JOINT", "CONFINED_SPACE", 150, 1.2),
    "SUBSTATION_MAINT": ("SUBSTATION", "HV_SWITCHING", 170, 6.0),
    "PROTECTION_TEST": ("PROTECTION", "PROTECTION_RELAY", 120, 0.06),
    "CUSTOMER_CONNECTION": ("LV_HV", "WORK_HEIGHTS", 70, 15),
    "FAULT_RESPONSE": ("LV_HV", "HV_SWITCHING", 110, 140),
    "VEGETATION": ("VEG", "CHAINSAW", 100, 0.2),
    "ASSET_INSPECTION": ("INSPECTION", "CASA_REPL", 45, 0.02),
    "CIVIL": ("CIVIL", "TRAFFIC_MGMT", 130, 0.1),
    "METERING": ("METERING", "METERING", 35, 5),
    "SERVICE_CALL": ("LV_HV", "WORK_HEIGHTS", 35, 20),
}
PREMISES = ("CUSTOMER_CONNECTION", "FAULT_RESPONSE", "METERING", "SERVICE_CALL")
BOOKED = ("CUSTOMER_CONNECTION", "FAULT_RESPONSE")   # carry a booked appointment window
# schedule of rates billed per job type, AUD per hour of on-site and travel time
BILL_RATE_PER_HR = {"LV_MAINT": 340, "HV_MAINT": 415, "CABLE_REPAIR": 420, "SUBSTATION_MAINT": 480, "PROTECTION_TEST": 450,
                    "CUSTOMER_CONNECTION": 320, "FAULT_RESPONSE": 445, "VEGETATION": 260, "ASSET_INSPECTION": 250, "CIVIL": 300,
                    "METERING": 230, "SERVICE_CALL": 295}
# how a linesperson's day splits across their job types (every other skill has one job type)
SKILL_TYPE_SHARE = {"LV_HV": {"SERVICE_CALL": .34, "LV_MAINT": .20, "HV_MAINT": .18, "CUSTOMER_CONNECTION": .14, "FAULT_RESPONSE": .14}}
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Jamie", "Chris", "Pat", "Dana", "Lee", "Robin", "Charlie", "Ash",
         "Drew", "Kai", "Noah", "Mia", "Zara", "Liam", "Ava", "Ethan", "Isla", "Mason", "Chloe", "Priya", "Wei", "Arjun", "Hana", "Tomas",
         "Leilani", "Omar", "Grace", "Hamish", "Sione", "Ruby", "Mateo", "Aisha", "Finn"]
LAST = ["Nguyen", "Smith", "Patel", "Brown", "Wilson", "Taylor", "Kelly", "Singh", "Martin", "Walker", "Young", "King", "Wright", "Scott",
        "Green", "Baker", "Hall", "Lopez", "Chen", "Murphy", "Rossi", "Haddad", "O'Brien", "Tran", "Fraser", "Papadopoulos", "Ahmed",
        "Kowalski", "Fifita", "Moreno", "Campbell", "Nakamura", "Doyle", "Mendes", "Reid", "Hussain"]


def regions(net):
    """Depot regions and the zone substation -> region map (nearest depot within the zone's area)."""
    zone_region = {}
    for z in net.zones:
        deps = [d for d in net.cfg["depots"] if d["area"] == z["area"]]
        zone_region[z["zs_id"]] = min(deps, key=lambda d: dist_m(d["lat"], d["lon"], z["lat"], z["lon"]))["code"]
    out = [dict(region_id=d["code"], name=d["name"].replace(" depot", ""), area=d["area"], depot_id=d["code"], depot_name=d["name"],
                lat=d["lat"], lon=d["lon"], zones=sorted(z for z, r in zone_region.items() if r == d["code"])) for d in net.cfg["depots"]]
    return out, zone_region


def _alloc(total, weights):
    """Split a whole number over weighted keys (largest remainder), so counts are exact and stable."""
    s = sum(weights.values())
    raw = {k: total * w / s for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: -(raw[k] - int(raw[k])))[:total - sum(out.values())]:
        out[k] += 1
    return out


def _add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, 28))


def _patches(net, region_feeders):
    """Feeder centroids in farthest-point order from the region's middle, so crews of one persona spread across the region."""
    pts = {}
    for n in net.nodes:
        if n["feeder_id"] in region_feeders:
            pts.setdefault(n["feeder_id"], []).append((n["lat"], n["lon"]))
    centre = {f: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for f, v in pts.items()}
    left = sorted(centre)
    mid = (sum(centre[f][0] for f in left) / len(left), sum(centre[f][1] for f in left) / len(left))
    d = lambda a, b: (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * 0.83) ** 2  # noqa: E731
    seq = [min(left, key=lambda f: d(centre[f], mid))]
    left.remove(seq[0])
    while left:
        nxt = max(left, key=lambda f: min(d(centre[f], centre[g]) for g in seq))
        seq.append(nxt)
        left.remove(nxt)
    return seq, centre


def build_workforce(net):
    wf = net.cfg["workforce"]
    as_of = dt.date.fromisoformat(net.cfg["as_of"])
    regs, zone_region = regions(net)
    skills_all = sorted({p["skill"] for p in PERSONAS.values()})
    crews, skills, tickets, leave, training, vehicles, used_names = [], [], [], [], [], [], set()
    for reg in regs:
        feeders = {f["feeder_id"] for f in net.feeders if zone_region[f["zs_id"]] == reg["region_id"]}
        order, centre = _patches(net, feeders)
        counts = _alloc(wf["crews"][reg["region_id"]], MIX[net.areas[reg["area"]]["style"]])
        for persona, p in PERSONAS.items():
            for i in range(counts[persona]):
                cid = f"{reg['region_id']}-{p['code']}-{i + 1:02d}"
                r = rng_for(net.seed, "crew", cid)
                name = f"{r.choice(FIRST)} {r.choice(LAST)}"
                while name in used_names:
                    name = f"{r.choice(FIRST)} {r.choice(LAST)}"
                used_names.add(name)
                contractor = r.random() < p["contractor"]
                mult = r.gauss(1.65, 0.10) if contractor else r.gauss(1.0, 0.06)
                f = order[i % len(order)]
                crews.append(dict(crew_id=cid, crew_name=name, persona=persona, primary_skill=p["skill"], region=reg["region_id"],
                                  depot=reg["depot_id"], area=reg["area"], employment_type="Contractor" if contractor else "Internal",
                                  hourly_cost_aud=round(p["rate"] * max(0.8, mult), 2),
                                  home_lat=round(centre[f][0] + r.uniform(-0.004, 0.004), 6),
                                  home_lon=round(centre[f][1] + r.uniform(-0.005, 0.005), 6),
                                  crew_size=r.randint(*p["size"]), business_unit=p["bu"], patch_feeder=f, active="Y"))
                skills.append(dict(crew_id=cid, skill_code=p["skill"], is_primary="Y"))
                if r.random() < 0.3:
                    extra = r.choice(skills_all)
                    if extra != p["skill"]:
                        skills.append(dict(crew_id=cid, skill_code=extra, is_primary="N"))
                for t in sorted(set(p["tickets"]) | set(COMMON_TICKETS)):
                    valid = TICKET_VALIDITY_MONTHS[t]
                    issued = _add_months(as_of, -r.randint(1, valid + 4))
                    tickets.append(dict(crew_id=cid, ticket_code=t, holder_name=name, issued_date=issued.isoformat(),
                                        expiry_date=_add_months(issued, valid).isoformat()))
                vehicles.append(dict(vehicle_id=f"V{len(vehicles) + 1:04d}", rego=f"EV{len(vehicles) + 1:04d}",
                                     vehicle_type=r.choices([v for v, _ in p["vehicle"]], [w for _, w in p["vehicle"]])[0],
                                     crew_id=cid, depot=reg["depot_id"]))
                rl = rng_for(net.seed, "leave", cid)
                if rl.random() < 0.12:
                    start = rl.randint(0, wf["horizon_days"] - 1)
                    kind, status = rl.choice(["ANNUAL", "PERSONAL", "RDO"]), rl.choices(["Approved", "Pending"], [.85, .15])[0]
                    for k in range(rl.randint(1, 4)):
                        leave.append(dict(crew_id=cid, leave_date=(as_of + dt.timedelta(days=start + k)).isoformat(), leave_type=kind,
                                          status=status))
                if rl.random() < 0.08:
                    training.append(dict(crew_id=cid, training_date=(as_of + dt.timedelta(days=rl.randint(0, wf["horizon_days"] - 1))).isoformat(),
                                         course_name=rl.choice(["HV switching refresher", "Working at heights", "Confined space",
                                                                "First aid recertification", "EWP recertification"]),
                                         duration_hours=rl.choice([4, 8])))
    for reg in regs:
        vehicles.append(dict(vehicle_id=f"V{len(vehicles) + 1:04d}", rego=f"EV{len(vehicles) + 1:04d}", vehicle_type="MOBILE_GENERATOR",
                             crew_id="", depot=reg["depot_id"]))
    rs = rng_for(net.seed, "safety")
    days = wf["safety_event_weeks"] * 7
    events = []
    for i in range(days * 4):
        crew = rs.choice(crews)
        events.append(dict(event_id=f"SE{i + 1:06d}", event_date=(as_of - dt.timedelta(days=rs.randint(0, days - 1))).isoformat(),
                           crew_id=crew["crew_id"] if rs.random() < 0.85 else "", depot=crew["depot"],
                           event_type=rs.choices(["INCIDENT", "NEAR_MISS", "HAZARD"], [.2, .5, .3])[0],
                           equipment_class=rs.choice(["Pole", "Transformer", "HV switchgear", "Feeder", "EWP", "Cable", "Service pit", "Vehicle"]),
                           severity=rs.choices([1, 2, 3, 4], [.4, .35, .2, .05])[0], status="Open" if rs.random() < 0.30 else "Closed"))
    eba = [dict(role=s, max_hours_per_day=12.0, max_consecutive_days=6, min_rest_hours=10.0, ot_multiplier=1.5, max_travel_as_work_hours=2.0)
           for s in skills_all]
    return dict(regions=regs, zone_region=zone_region, crews=crews, skills=skills, tickets=tickets, leave=leave, training=training,
                vehicles=vehicles, events=events, eba=eba)


def _rows(path, rows):
    _write_csv(path, list(rows[0].keys()), [list(x.values()) for x in rows])


def write_workforce(net, root, stamp):
    out = os.path.join(root, "workforce")
    wf = build_workforce(net)
    for name, key in (("crews", "crews"), ("crew_skills", "skills"), ("crew_tickets", "tickets"), ("crew_leave", "leave"),
                      ("crew_training", "training"), ("vehicles", "vehicles"), ("safety_events", "events"), ("eba_rules", "eba")):
        _rows(os.path.join(out, f"{name}_{stamp}.csv"), wf[key])
    _write_csv(os.path.join(out, f"depots_{stamp}.csv"), ["depot", "name", "area", "latitude", "longitude", "region_name", "zone_substations"],
               [[r["depot_id"], r["depot_name"], r["area"], r["lat"], r["lon"], r["name"], ";".join(r["zones"])] for r in wf["regions"]])
    return wf


# ---------------------------------------------------------------------------------------------- work management system
DEFECT_JOB = {"CRACKED_CROSSARM": ("HV_MAINT", "Replace cracked cross-arm on pole {p}"), "INSULATOR_DAMAGE": ("HV_MAINT", "Replace damaged insulator on pole {p}"),
              "POLE_ROT": ("HV_MAINT", "Replace pole {p} (rot at ground line)"), "LEANING_POLE": ("HV_MAINT", "Straighten leaning pole {p}"),
              "CONDUCTOR_DAMAGE": ("HV_MAINT", "Repair conductor strand damage at pole {p}"),
              "VEGETATION_ENCROACH": ("VEGETATION", "Clear vegetation inside clearance at pole {p}"),
              "TIE_WIRE_LOOSE": ("LV_MAINT", "Fix loose tie wire on pole {p}"), "FUSE_HOLDER": ("LV_MAINT", "Replace damaged fuse holder on pole {p}")}
CAPEX_DEFECTS = ("CRACKED_CROSSARM", "INSULATOR_DAMAGE", "POLE_ROT", "CONDUCTOR_DAMAGE")
PREMISES_TITLES = {
    "SERVICE_CALL": ["Service call: flickering lights", "Service call: partial supply", "Service call: tingling taps (shock report)",
                     "Service call: low voltage complaint", "Service call: damaged meter box"],
    "CUSTOMER_CONNECTION": ["New connection", "Supply upgrade to three phase", "Solar and battery connection", "EV charger connection",
                            "Temporary builder's supply"],
    "FAULT_RESPONSE": ["No supply callout", "Service fuse failed", "Off-peak hot water not working", "Wires down report"],
    "METERING": ["Meter exchange", "Meter fault investigation", "Meter reconfiguration for solar", "Special meter read"],
}
FUNDING = {"CUSTOMER_CONNECTION": "CUSTOMER", "METERING": "RETAILER", "FAULT_RESPONSE": "EMERGENCY", "CABLE_REPAIR": "CAPEX", "CIVIL": "CAPEX"}
SOURCE = {"LV_MAINT": "maintenance", "HV_MAINT": "maintenance", "CABLE_REPAIR": "cable", "SUBSTATION_MAINT": "maintenance",
          "PROTECTION_TEST": "protection", "CUSTOMER_CONNECTION": "connection", "FAULT_RESPONSE": "callout", "VEGETATION": "vegetation",
          "ASSET_INSPECTION": "inspection", "CIVIL": "civil", "METERING": "metering", "SERVICE_CALL": "service"}


def _hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _address(m):
    return f"{m['unit'] + '/' if m['unit'] else ''}{m['house_number']} {m['street_name']} {m['street_type']}, {m['suburb']}"


def type_mix(crews, region, jobs):
    """Jobs of each type for a region's day, in proportion to what its crews can do (a crew's productive 10 hours over each job type
    its skill covers), so every trade has a full day and no trade sits idle."""
    supply = {}
    for c in crews:
        if c["region"] == region:
            supply[c["primary_skill"]] = supply.get(c["primary_skill"], 0) + 1
    demand = {}
    for t, (skill, _, dur, _) in JOB_TYPES.items():
        share = SKILL_TYPE_SHARE.get(skill, {t: 1.0}).get(t, 0)
        if supply.get(skill) and share:
            demand[t] = supply[skill] * share * 600 / (dur + 25)
    return _alloc(jobs, demand)


def write_work_orders(net, root, topo, open_defects, veg):
    """Planned work for the horizon (from the as-of day), one CSV of work orders. `open_defects` are the open SAP notifications on poles
    (they become the first maintenance jobs, most urgent first); `veg` is each overhead span's latest LiDAR category and clearance."""
    wf = net.cfg["workforce"]
    built = build_workforce(net)
    zone_region = built["zone_region"]
    as_of = dt.date.fromisoformat(net.cfg["as_of"])
    freg = {f["feeder_id"]: zone_region[f["zs_id"]] for f in net.feeders}
    node = {n["node_id"]: n for n in net.nodes}
    zone = {z["zs_id"]: z for z in net.zones}
    pool = {r["region_id"]: dict(poles=[], spans_veg=[], spans_fire=[], cables=[], dss=[], devices=[], zs=[], nmis=[], defects=[])
            for r in built["regions"]}
    for n in net.nodes:
        if n["pole_id"]:
            pool[freg[n["feeder_id"]]]["poles"].append(n)
    for s in net.spans:
        if s["role"] == "tie":
            continue
        p = pool[freg[s["feeder_id"]]]
        if not s["overhead"]:
            p["cables"].append(s)
        elif s["bushfire_prone"]:
            p["spans_fire"].append(s)
        if s["overhead"] and veg.get(s["span_id"], ("CLEAR",))[0] in ("A", "B"):
            p["spans_veg"].append(s)
    for d in net.dss:
        pool[freg[d["feeder_id"]]]["dss"].append(d)
    for d in net.devices:
        if d["type"] in ("CB", "RECLOSER"):
            pool[freg[d["feeder_id"]]]["devices"].append(d)
    for t in net.zs_transformers:
        pool[zone_region[t["zs_id"]]]["zs"].append(t)
    for m in net.nmis:
        pool[freg[m["feeder_id"]]]["nmis"].append(m)
    for d in sorted(open_defects, key=lambda d: (d["prio"], d["created"], d["pole_id"])):
        pool[freg[d["feeder_id"]]]["defects"].append(d)
    for p in pool.values():
        p["spans_veg"].sort(key=lambda s: (veg[s["span_id"]][1], s["span_id"]))
    zone_cust = {}
    for m in net.nmis:
        zs = m["feeder_id"].split("-")[0]
        zone_cust[zs] = zone_cust.get(zs, 0) + 1
    below = lambda node_id: topo.customers_below(node_id)[0]  # noqa: E731
    mid = lambda s: ((s["coords"][0][0] + s["coords"][-1][0]) / 2, (s["coords"][0][1] + s["coords"][-1][1]) / 2)  # noqa: E731
    rows, seq, used_defects = [], 4100000, set()
    for day in range(wf["horizon_days"]):
        date = as_of + dt.timedelta(days=day)
        for reg in built["regions"]:
            rid = reg["region_id"]
            p = pool[rid]
            r = rng_for(net.seed, "wms", rid, day)
            counts = type_mix(built["crews"], rid, int(round(wf["jobs_per_day"][rid] * r.uniform(0.96, 1.04))))
            cursor = {}

            def take(key):   # walk a pool from a random start in random strides, so a day spreads over the region and rarely repeats
                lst = p[key]
                if not lst:
                    return None
                i = cursor.get(key, r.randrange(len(lst)))
                cursor[key] = (i + r.randint(1, max(1, len(lst) // 40))) % len(lst)
                return lst[i]

            for jt, (skill, ticket, base_dur, exposure) in JOB_TYPES.items():
                for _ in range(counts.get(jt, 0)):
                    nmi = addr = suburb = notif = None
                    prio = r.choices([1, 2, 3, 4, 5], [.08, .17, .45, .22, .08])[0]
                    funding, source, k = FUNDING.get(jt, "OPEX"), SOURCE[jt], exposure
                    d = None
                    if jt in ("HV_MAINT", "LV_MAINT", "VEGETATION"):
                        d = next((x for x in p["defects"] if x["notification"] not in used_defects and DEFECT_JOB[x["code"]][0] == jt), None)
                    if d:
                        used_defects.add(d["notification"])
                        n = node[d["node_id"]]
                        asset, feeder, notif, loc = n["pole_id"], n["feeder_id"], d["notification"], (n["lat"], n["lon"])
                        title, customers, source = DEFECT_JOB[d["code"]][1].format(p=n["pole_id"]), below(n["node_id"]), "defect"
                        prio = {1: 5, 2: 4, 3: 3, 4: 2}[d["prio"]]
                        funding = "CAPEX" if d["code"] in CAPEX_DEFECTS else "OPEX"
                    elif jt == "HV_MAINT":
                        n = take("poles")
                        asset, feeder, customers, loc = n["pole_id"], n["feeder_id"], below(n["node_id"]), (n["lat"], n["lon"])
                        title = f"HV maintenance on pole {n['pole_id']} ({n['feeder_id']})"
                    elif jt in ("LV_MAINT", "CIVIL") or (jt == "CABLE_REPAIR" and not p["cables"]) or \
                            (jt == "SUBSTATION_MAINT" and not (p["zs"] and r.random() < 0.06)):
                        x = take("dss")
                        asset, feeder, customers, loc = x["ds_id"], x["feeder_id"], topo.cust_by_ds.get(x["ds_id"], 0), (x["lat"], x["lon"])
                        title = r.choice({"LV_MAINT": ["LV fuse and link maintenance at {d}", "LV pillar repair near {d}",
                                                       "LV board inspection and repair at {d}"],
                                          "CIVIL": ["Pit and conduit works near {d}", "Pillar replacement near {d}",
                                                    "Trenching for new estate supply near {d}", "Footpath reinstatement near {d}"],
                                          "CABLE_REPAIR": ["Cable termination repair at {d}"],
                                          "SUBSTATION_MAINT": ["Maintenance on distribution substation {d}"]}[jt]).format(d=x["ds_id"])
                    elif jt == "SUBSTATION_MAINT":
                        t = take("zs")
                        z = zone[t["zs_id"]]
                        asset, feeder, customers, loc = t["asset_id"], None, zone_cust.get(t["zs_id"][3:], 0) // 2, (z["lat"], z["lon"])
                        title, k = f"Zone substation transformer service {t['asset_id']}", 0.08
                    elif jt == "CABLE_REPAIR":
                        s = take("cables")
                        asset, feeder, customers, loc = s["span_id"], s["feeder_id"], below(s["to_node"]), mid(s)
                        title = f"Repair cable joint on section {s['span_id']} ({s['feeder_id']})"
                    elif jt == "VEGETATION":
                        s = take("spans_veg") or take("spans_fire")
                        asset, feeder, customers, loc = s["span_id"], s["feeder_id"], below(s["to_node"]), mid(s)
                        title = f"Vegetation cut on span {s['span_id']} (clearance {veg.get(s['span_id'], ('', 2.0))[1]:.1f} m)"
                    elif jt == "PROTECTION_TEST":
                        x = take("devices")
                        asset, feeder, customers, loc = x["device_id"], x["feeder_id"], below(x["node_id"]), (x["lat"], x["lon"])
                        title = (f"Protection relay test on feeder breaker {x['device_id']}" if x["type"] == "CB"
                                 else f"Recloser test {x['device_id']}")
                    elif jt == "ASSET_INSPECTION" and p["spans_fire"] and r.random() < 0.6:
                        s = take("spans_fire")
                        n = node[s["to_node"]]
                        asset, feeder, customers, loc = n["pole_id"], s["feeder_id"], below(s["to_node"]), (n["lat"], n["lon"])
                        title, source = f"Bushfire patrol of {s['feeder_id']} from pole {n['pole_id']}", "patrol"
                    elif jt == "ASSET_INSPECTION":
                        n = take("poles")
                        asset, feeder, customers, loc = n["pole_id"], n["feeder_id"], below(n["node_id"]), (n["lat"], n["lon"])
                        title = f"Drone inspection of pole {n['pole_id']}"
                    else:
                        m = p["nmis"][r.randrange(len(p["nmis"]))]
                        asset, nmi, feeder, customers, loc = None, m["nmi"], m["feeder_id"], 1, (m["lat"], m["lon"])
                        addr, suburb = _address(m), m["suburb"]
                        title = f"{r.choice(PREMISES_TITLES[jt])}, {addr}"
                        if jt == "FAULT_RESPONSE":
                            prio = r.choice([4, 5])
                    dur = int(max(30, r.gauss(base_dur, base_dur * 0.18)))
                    if jt in BOOKED:
                        ws = r.choice([540, 600, 660, 720, 780, 840])
                        we = ws + dur + r.choice([45, 60, 75, 90])
                    else:
                        ws = r.choice([360, 420, 480, 540])
                        we = ws + r.choice([240, 300, 360, 480])
                    est = int(round(k * r.uniform(0.7, 1.3) * (1 if jt in PREMISES else customers)))
                    seq += 1
                    rows.append([f"WO{seq}", date.isoformat(), jt, title, prio, dur, _hhmm(ws), _hhmm(we), rid, feeder or "", asset or "",
                                 nmi or "", addr or "", suburb or "", round(loc[0], 6), round(loc[1], 6), skill, ticket, funding,
                                 customers, est, round(BILL_RATE_PER_HR[jt] * max(0.85, r.gauss(1.0, 0.08)), 2), source, notif or ""])
    _write_csv(os.path.join(root, "wms", f"work_orders_{as_of:%Y%m%d}.csv"),
               ["order_id", "planned_date", "work_type", "description", "priority", "est_duration_min", "window_start", "window_end",
                "depot", "feeder_id", "asset_id", "nmi", "address", "suburb", "latitude", "longitude", "required_skill", "required_ticket",
                "funding_code", "customers_affected", "customer_minutes_at_risk", "schedule_rate_aud_per_hr", "source", "notification_id"],
               rows)
    return len(rows)
