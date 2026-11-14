"""Deterministic builder for the simulated distribution network.

Builds the physical network (bulk supply, zone substations, feeders, poles, spans,
switching devices, distribution substations, customers, DER) over real geography
from config/atlas.json. Pure Python (stdlib only) so it runs locally and on serverless.
Every random draw comes from a generator seeded per entity, so any rebuild is identical.
"""
import json
import math
import random

M_PER_DEG_LAT = 110540.0


def m_per_deg_lon(lat):
    return 111320.0 * math.cos(math.radians(lat))


def move(lat, lon, bearing_deg, dist_m):
    """Point reached from (lat, lon) after dist_m metres on a bearing (flat-earth, fine at feeder scale)."""
    b = math.radians(bearing_deg)
    return (lat + dist_m * math.cos(b) / M_PER_DEG_LAT,
            lon + dist_m * math.sin(b) / m_per_deg_lon(lat))


def dist_m(a_lat, a_lon, b_lat, b_lon):
    dy = (a_lat - b_lat) * M_PER_DEG_LAT
    dx = (a_lon - b_lon) * m_per_deg_lon((a_lat + b_lat) / 2)
    return math.hypot(dx, dy)


def rng_for(seed, *keys):
    return random.Random(f"{seed}|" + "|".join(str(k) for k in keys))


def nmi_checksum(nmi):
    """AEMO NMI checksum (Luhn-style over ASCII values, doubling from the right)."""
    total, double = 0, True
    for ch in reversed(nmi):
        v = ord(ch)
        if double:
            v *= 2
        total += sum(int(d) for d in str(v))
        double = not double
    return str((10 - total % 10) % 10)


STYLE = {
    # span length (mean, sd) m, trunk spans (min, max), spur every (min, max), spur spans (min, max), wander deg
    "metro":   dict(span=(45, 5), trunk=(105, 150), spur_every=(9, 14), spur=(8, 26), wander=3, grid=True,
                    ds_every=(15, 21), cust_pole=(30, 75), cust_kiosk=(45, 100), lv_radius=170),
    "coastal": dict(span=(50, 7), trunk=(95, 140), spur_every=(9, 14), spur=(8, 24), wander=5, grid=True,
                    ds_every=(15, 21), cust_pole=(28, 70), cust_kiosk=(40, 90), lv_radius=200),
    "rural":   dict(span=(105, 28), trunk=(110, 170), spur_every=(7, 13), spur=(10, 40), wander=11, grid=False,
                    ds_every=(9, 15), cust_pole=(3, 22), cust_kiosk=(15, 45), lv_radius=650),
}

# Street addresses. Names are generic Australian street names (trees, birds, landforms, early colonial and local words); a name and
# type is used once per suburb. Types carry their Australian address standard abbreviation, which the market data uses.
STREET_NAMES = [
    "Acacia", "Albert", "Alexander", "Alfred", "Allambie", "Anzac", "Arthur", "Ash", "Aspen", "Banksia", "Barton", "Bass", "Bay", "Bellbird",
    "Beltana", "Birch", "Blaxland", "Bligh", "Bluegum", "Boronia", "Bottlebrush", "Boundary", "Bowman", "Bridge", "Brolga", "Brush",
    "Bunya", "Burra", "Cabbage Tree", "Callistemon", "Cambridge", "Camellia", "Carrington", "Casuarina", "Cedar", "Church", "Clarence",
    "Coachwood", "Collins", "Cook", "Coral", "Corella", "Creek", "Cypress", "Dalwood", "Darling", "Denison", "Dove", "Drummond", "Dunmore",
    "Eagle", "Eastern", "Elder", "Elizabeth", "Emerald", "Eucalypt", "Fairway", "Fern", "Ferntree", "Fig", "Finch", "Fitzroy", "Flinders",
    "Forest", "Fraser", "Galah", "George", "Gilbert", "Gipps", "Glen", "Golden", "Grevillea", "Gully", "Gymea", "Hakea", "Harbour",
    "Hawthorn", "Heath", "Heron", "High", "Hill", "Hillcrest", "Hobart", "Hume", "Hunter", "Ibis", "Jacaranda", "James", "Jarrah", "Kangaroo",
    "Karri", "Kestrel", "King", "Kingfisher", "Kookaburra", "Kurrajong", "Lachlan", "Lagoon", "Lake", "Laurel", "Lawson", "Leichhardt",
    "Lilly Pilly", "Lime", "Lorikeet", "Lyrebird", "Macarthur", "Macquarie", "Magnolia", "Magpie", "Mahogany", "Mallee", "Manning", "Maple",
    "Marine", "Market", "Melaleuca", "Merino", "Mill", "Mimosa", "Mitchell", "Moore", "Mountain", "Mulga", "Murray", "Myrtle", "Nelson",
    "Nepean", "Norfolk", "Oak", "Ocean", "Olive", "Orchard", "Osprey", "Oxley", "Palm", "Paperbark", "Park", "Parkes", "Pelican", "Peppermint",
    "Phillip", "Pine", "Pioneer", "Platypus", "Plover", "Poplar", "Prince", "Queen", "Quarry", "Railway", "Range", "Ridge", "River",
    "Riverview", "Robin", "Rose", "Rosella", "Rowan", "Russell", "Sandpiper", "School", "Sea", "Shearwater", "Silky Oak", "Smith", "Spinifex",
    "Spring", "Spotted Gum", "Station", "Stringybark", "Stuart", "Sturt", "Sunset", "Swallow", "Swan", "Sydney", "Tallowwood", "Tasman",
    "Tea Tree", "Tern", "Thomas", "Turpentine", "Valley", "Victoria", "View", "Waratah", "Warrigal", "Water Gum", "Wattle", "Wentworth",
    "Werona", "Whipbird", "William", "Willow", "Windsor", "Wombat", "Woodland", "Wren", "Yarran", "York"]
STREET_TYPES = [("Street", "ST", 30), ("Road", "RD", 14), ("Avenue", "AVE", 12), ("Crescent", "CRES", 9), ("Close", "CL", 6),
                ("Place", "PL", 7), ("Parade", "PDE", 4), ("Drive", "DR", 8), ("Way", "WAY", 4), ("Court", "CT", 3), ("Circuit", "CCT", 3),
                ("Grove", "GR", 2), ("Lane", "LANE", 2)]
RURAL_TYPES = [("Road", "RD", 60), ("Lane", "LANE", 15), ("Drive", "DR", 10), ("Way", "WAY", 8), ("Creek Road", "RD", 7)]
# lot frontage (m), how far a house sits from the street centreline (m), and how long one street may run each side of its transformer (m)
LOTS = {"metro": dict(frontage=(15, 18), setback=14, reach=230), "coastal": dict(frontage=(16, 20), setback=15, reach=250),
        "rural": dict(frontage=(55, 140), setback=35, reach=600), "village": dict(frontage=(20, 28), setback=16, reach=300)}

# Indicative continuous (summer) ratings in amps for the conductors in use; a real network takes these from its line design manual.
CONDUCTOR_RATING = {"AAC Pluto": 535, "AAAC Neptune": 420, "AAC Mars": 300, "ABC 95": 255, "ACSR Raisin": 150, "SC/GZ 3/2.75": 60,
                    "XLPE 400 Al": 465, "XLPE 240 Al": 355}
# A feeder's trunk is built in one heavy conductor and its spurs in lighter ones (by area); SWER lines are steel.
RUN_CONDUCTOR = {
    ("metro", "trunk"): [("AAC Pluto", .45), ("AAAC Neptune", .4), ("AAC Mars", .15)], ("metro", "spur"): [("AAC Mars", .6), ("ABC 95", .4)],
    ("coastal", "trunk"): [("AAAC Neptune", .5), ("AAC Pluto", .3), ("AAC Mars", .2)], ("coastal", "spur"): [("AAC Mars", .7), ("ABC 95", .3)],
    ("rural", "trunk"): [("AAAC Neptune", .3), ("AAC Mars", .3), ("ACSR Raisin", .4)], ("rural", "spur"): [("ACSR Raisin", .7), ("SC/GZ 3/2.75", .3)],
    ("rural", "swer"): [("SC/GZ 3/2.75", 1.0)], ("cable", "trunk"): [("XLPE 400 Al", .6), ("XLPE 240 Al", .4)], ("cable", "spur"): [("XLPE 240 Al", 1.0)]}
DEVICE_RATING = {"CB": 630, "RECLOSER": 630, "GAS_SWITCH": 630, "NOP": 630, "SWER_ISOLATOR": 20}
EMERGENCY = 1.1   # a short-term (emergency) rating about 10 percent above the continuous one
# Circuit ratings in MVA by voltage: 132 kV bulk supply to transmission substation lines, and subtransmission at 33 kV (metro and coast) or 66 kV (ranges)
TX_LINE_RATING = 135
SUBTX_RATING = {33: 36, 66: 45}
TS_EQUNR_BASE = 40000000   # SAP equipment numbers of the transmission transformers (a block of their own, so no other number moves)

CONDUCTORS = {"metro": ["AAC Mars", "AAAC Neptune", "ABC 95"], "coastal": ["AAC Mars", "AAAC Neptune", "AAC Pluto"],
              "rural": ["ACSR Raisin", "SC/GZ 3/2.75", "AAAC Neptune"]}


class Network:
    def __init__(self, cfg):
        self.cfg = cfg
        self.seed = cfg["seed"]
        self.areas = {a["code"]: a for a in cfg["areas"]}
        self.bsps, self.zones, self.zs_transformers, self.feeders = [], [], [], []
        self.nodes, self.spans, self.devices, self.dss = [], [], [], []
        self.nmis, self.der, self.critical_sites, self.ev_hubs = [], [], [], []
        self.subtx_lines = []
        self.tss, self.ts_transformers, self.tx_lines = [], [], []   # transmission substations, their transformers, 132 kV lines
        self.streets = []
        self._street_names = {}   # suburb -> names in use
        self._lines, self._houses, self._addr_used = {}, {}, set()
        self._geo = None
        self._pole_no = 200000
        self._eq_no = 10000000

    # ------------------------------------------------------------------ ids
    def next_pole(self):
        self._pole_no += 1
        return self._pole_no

    def next_equnr(self):
        self._eq_no += 1
        return f"{self._eq_no:018d}"

    # ------------------------------------------------------------------ build
    def build(self):
        self._bulk_supply()
        self._transmission()
        for z in self.cfg["zone_substations"]:
            self._zone(z)
        self._ties()
        self._attach_device_spans()
        self._customers()
        self._reinforce()
        return self

    def _reinforce(self):
        """Planners keep feeders inside their rating: one whose evening peak would exceed about 92 percent of its trunk rating has had its
        trunk rebuilt in the next heavier conductor (the same peak estimate the historian uses: about 2.1 kW per customer plus a
        commercial base, on a warm evening)."""
        heavier = ["ACSR Raisin", "AAC Mars", "AAAC Neptune", "AAC Pluto"]
        cables = ["XLPE 240 Al", "XLPE 400 Al"]
        cust = {}
        for m in self.nmis:
            cust[m["feeder_id"]] = cust.get(m["feeder_id"], 0) + 1
        for f in self.feeders:
            peak = (cust.get(f["feeder_id"], 0) * 0.0021 + 0.4) * 1000 / (math.sqrt(3) * f["kv"]) * 1.03 * 1.08
            f["design_peak_amps"] = round(peak)
            while peak > 0.92 * f["rating_amps"]:
                ladder = cables if f["trunk_conductor"] in cables else heavier
                i = ladder.index(f["trunk_conductor"]) if f["trunk_conductor"] in ladder else -1
                if i + 1 >= len(ladder):
                    break
                new = ladder[i + 1]
                for sp in self.spans:
                    if sp["feeder_id"] == f["feeder_id"] and sp["role"] == "trunk":
                        sp["conductor"], sp["rating_amps"] = new, CONDUCTOR_RATING[new]
                f["trunk_conductor"] = new
                crating = CONDUCTOR_RATING[new]
                f["rating_amps"] = min(crating, DEVICE_RATING["CB"])
                f["emergency_amps"] = round(f["rating_amps"] * EMERGENCY)
                f["limited_by"] = f"Trunk conductor ({new})" if crating <= DEVICE_RATING["CB"] else "Feeder circuit breaker"
                f["reinforced"] = True

    def _attach_device_spans(self):
        """Give every switching device the span it interrupts (incoming span to its node, or its tie span)."""
        incoming = {s["to_node"]: s["span_id"] for s in self.spans if s["role"] != "tie"}
        ties = {s.get("tie_device"): s["span_id"] for s in self.spans if s["role"] == "tie"}
        for d in self.devices:
            if d["type"] == "NOP":
                d["span_id"] = ties.get(d["device_id"])
            elif not d["span_id"]:
                d["span_id"] = incoming.get(d["node_id"])

    def _bulk_supply(self):
        # Bulk supply points (where the transmission network hands over): from config, each with two transformers; one without a set
        # position sits near the centroid of its area's zone substations.
        for b in self.cfg["bulk_supply_points"]:
            zs = [z for z in self.cfg["zone_substations"] if z["area"] == b["area"]]
            lat = b.get("lat", sum(z["lat"] for z in zs) / len(zs) + 0.012)
            lon = b.get("lon", sum(z["lon"] for z in zs) / len(zs) - 0.015)
            self.bsps.append(dict(bsp_id=f"BSP-{b['code']}", name=b["name"], area=b["area"], lat=lat, lon=lon, kv=b["kv"],
                                  capacity_mva=b["capacity_mva"], transformers=b["transformers"],
                                  firm_mva=round(b["capacity_mva"] / b["transformers"] * 1.2)))   # what is left with one transformer out (cyclic)

    def _transmission(self):
        """Transmission substations (the second level, between the bulk supply points and the zone substations). Each sits between its
        bulk supply point and the zones it feeds, with a pair of 132 kV circuits on separate routes from the bulk supply point and its
        transformers in parallel. Its transformers take SAP numbers from their own block, so every other equipment number is unchanged."""
        for t in self.cfg["transmission_substations"]:
            bsp = next(b for b in self.bsps if b["bsp_id"] == f"BSP-{t['bsp']}")
            r = rng_for(self.seed, "transmission", t["code"])
            ts_id = f"TS-{t['code']}"
            n, rating = t["transformers"], t["transformer_mva"]
            site = dict(ts_id=ts_id, code=t["code"], name=f"{t['name']} transmission substation", area=t["area"], lat=t["lat"], lon=t["lon"],
                        kv_primary=132, kv_secondary=t["kv_secondary"], n_transformers=n, transformer_mva=rating,
                        installed_mva=n * rating, firm_mva=(n - 1) * rating, bsp_id=bsp["bsp_id"], bsp_name=bsp["name"],
                        commissioned=r.randint(1972, 2019))
            self.tss.append(site)
            for k in range(2):   # the pair: one circuit each side of the straight route
                side = 1 if k == 0 else -1
                d = dist_m(bsp["lat"], bsp["lon"], site["lat"], site["lon"])
                b = math.degrees(math.atan2((site["lon"] - bsp["lon"]) * m_per_deg_lon(site["lat"]), (site["lat"] - bsp["lat"]) * M_PER_DEG_LAT))
                mid = move((bsp["lat"] + site["lat"]) / 2, (bsp["lon"] + site["lon"]) / 2, b + 90 * side, min(1500, d * 0.08))
                self.tx_lines.append(dict(line_id=f"TL-{bsp['bsp_id'][4:]}-{t['code']}-{k + 1}", from_id=bsp["bsp_id"], to_id=ts_id, circuit=k + 1,
                                          kv=132, route_km=round(d * 1.08 / 1000, 1), overhead=True, rating_mva=TX_LINE_RATING,
                                          install_year=rng_for(self.seed, "tl", t["code"], k).randint(1965, 2015),
                                          coords=[(bsp["lat"], bsp["lon"]), mid, (site["lat"], site["lon"])]))
            for x in range(1, n + 1):
                self.ts_transformers.append(dict(
                    asset_id=f"{ts_id}-TX{x}", ts_id=ts_id, name=f"{t['name']} transmission transformer {x}", rating_mva=rating,
                    cyclic_rating_mva=round(rating * 1.22, 1), manufacturer=r.choice(["Wilson", "Tyree", "ABB", "Siemens", "Hyundai"]),
                    year=r.randint(1975, 2019), equnr=f"{TS_EQUNR_BASE + len(self.ts_transformers) + 1:018d}", cooling="ONAN/ONAF/ONAF",
                    tap_range="+/-10 x 1.25%"))

    def _zone(self, z):
        area = self.areas[z["area"]]
        style = STYLE[area["style"]]
        r = rng_for(self.seed, "zone", z["code"])
        zs_id = f"ZS-{z['code']}"
        big = area["style"] != "rural"
        tx_mva = 33 if big else 15
        # the supply voltage of the zone is its subtransmission voltage: 33 kV metro and coast, 66 kV ranges (fed from the 132 kV sites)
        zone = dict(zs_id=zs_id, code=z["code"], name=f"{z['name']} zone substation", area=area["code"],
                    lat=z["lat"], lon=z["lon"], kv_primary=33 if big else 66, kv_secondary=area["voltage_kv"],
                    firm_mva=tx_mva, n_transformers=2, bsp_id=f"BSP-{area['code']}",
                    commissioned=r.randint(1968, 2012), underground_share=z["underground_share"])
        self.zones.append(zone)
        # the subtransmission circuits into the zone, each from a transmission substation: two for most (from one or two sites, on
        # separate routes), one for the long radial lines, which have no alternative supply
        circuits = self.cfg["subtransmission"][z["code"]]
        sites = [next(t for t in self.tss if t["code"] == code) for code in circuits]
        zone["supply_ts"] = [t["ts_id"] for t in sites]   # the site each circuit comes from, in circuit order
        zone["ts_ids"] = sorted(set(zone["supply_ts"]))
        zone["bsp_id"] = sites[0]["bsp_id"]
        zone["supply_circuits"] = len(circuits)
        for k, ts in enumerate(sites):
            side = 1 if k % 2 == 0 else -1
            d = dist_m(ts["lat"], ts["lon"], z["lat"], z["lon"])
            b = math.degrees(math.atan2((z["lon"] - ts["lon"]) * m_per_deg_lon(z["lat"]), (z["lat"] - ts["lat"]) * M_PER_DEG_LAT))
            mid = move((ts["lat"] + z["lat"]) / 2, (ts["lon"] + z["lon"]) / 2, b + 90 * side, min(1500, d * 0.08) * (1 if len(circuits) > 1 else 0.4))
            self.subtx_lines.append(dict(line_id=f"STX-{z['code']}-{k + 1}", from_id=ts["ts_id"], to_id=zs_id, ts_id=ts["ts_id"],
                                         bsp_id=ts["bsp_id"], circuit=k + 1, kv=zone["kv_primary"], route_km=round(d * 1.08 / 1000, 1),
                                         overhead=True, rating_mva=SUBTX_RATING[zone["kv_primary"]],
                                         install_year=rng_for(self.seed, "stx", z["code"], k).randint(1965, 2015),
                                         coords=[(ts["lat"], ts["lon"]), mid, (z["lat"], z["lon"])]))
        for t in (1, 2):
            self.zs_transformers.append(dict(
                asset_id=f"{zs_id}-TX{t}", zs_id=zs_id, name=f"{z['name']} transformer {t}", rating_mva=tx_mva,
                cyclic_rating_mva=round(tx_mva * 1.22, 1), manufacturer=r.choice(["Wilson", "Tyree", "ABB", "Siemens"]),
                year=r.randint(1970, 2015), equnr=self.next_equnr(), cooling="ONAN/ONAF", tap_range="+/-8 x 1.25%"))
        for k in range(self.cfg["feeders_per_zone"]):
            self._feeder(zone, k + 1, style, area)

    def _feeder(self, zone, k, style, area):
        fid = f"{zone['code']}-F{k:02d}"
        r = rng_for(self.seed, "feeder", fid)
        base_bearing = (k - 1) * 360 / self.cfg["feeders_per_zone"] + r.uniform(-18, 18)
        underground_feeder = r.random() < zone["underground_share"] * 0.35
        feeder = dict(feeder_id=fid, zs_id=zone["zs_id"], area=area["code"], kv=area["voltage_kv"],
                      name=f"{zone['name'].replace(' zone substation', '')} feeder {k}",
                      rating_amps=400 if area["style"] != "rural" else 250, style=area["style"],
                      hero=fid in self.cfg["hero_feeders"], bushfire_area=area["style"] == "rural")
        self.feeders.append(feeder)
        head = self._node(fid, zone["lat"], zone["lon"], kind="zs_bus", overhead=False, is_bus=True)
        # Feeder circuit breaker sits on the first span out of the zone substation.
        trunk_len = r.randint(*style["trunk"])
        trunk = self._run(feeder, head, base_bearing, trunk_len, style, r, phases="ABC", role="trunk",
                          underground=underground_feeder, first_device="CB")
        feeder["trunk_end_node"] = trunk[-1]["node_id"]
        # the feeder's rating is whatever limits its trunk: the trunk conductor or the feeder circuit breaker
        cond = self.spans[-1]["conductor"] if trunk else None
        crating = CONDUCTOR_RATING.get(cond, 300)
        feeder["rating_amps"] = min(crating, DEVICE_RATING["CB"])
        feeder["emergency_amps"] = round(feeder["rating_amps"] * EMERGENCY)
        feeder["trunk_conductor"] = cond
        feeder["limited_by"] = f"Trunk conductor ({cond})" if crating <= DEVICE_RATING["CB"] else "Feeder circuit breaker"
        # Recloser part way down long feeders, gas switches as sectionalisers.
        if area["style"] == "rural" or trunk_len > 70:
            self._device_on(feeder, trunk[int(len(trunk) * r.uniform(0.35, 0.45))], "RECLOSER", r)
        for i in range(24, len(trunk) - 5, r.randint(22, 30)):
            self._device_on(feeder, trunk[i], "GAS_SWITCH", r)
        # Spurs off the trunk.
        i = r.randint(*style["spur_every"])
        while i < len(trunk) - 2:
            side = r.choice((-1, 1))
            bearing = trunk[i]["bearing"] + side * (90 if style["grid"] else r.uniform(45, 100))
            ug = underground_feeder or r.random() < zone["underground_share"]
            swer = area["style"] == "rural" and not ug and r.random() < 0.12
            phases = "A" if swer else ("ABC" if r.random() < 0.8 else "AB")
            spur = self._run(feeder, trunk[i], bearing, r.randint(*style["spur"]), style, r, phases=phases,
                             role="swer" if swer else "spur", underground=ug, first_device="SWER_ISOLATOR" if swer else "FUSE")
            # Occasional sub-spur on long spurs.
            if len(spur) > 14 and r.random() < 0.45:
                j = r.randint(5, len(spur) - 3)
                self._run(feeder, spur[j], spur[j]["bearing"] + r.choice((-1, 1)) * 80, r.randint(4, 12), style, r,
                          phases=phases, role="spur", underground=ug, first_device="FUSE")
            i += r.randint(*style["spur_every"])

    def _node(self, fid, lat, lon, kind, overhead, is_bus=False, bearing=0.0):
        node = dict(node_id=f"CN-{len(self.nodes) + 1:07d}", feeder_id=fid, lat=lat, lon=lon, kind=kind,
                    overhead=overhead, bearing=bearing, pole_id=None, ds_id=None)
        if overhead and not is_bus:
            node["pole_id"] = f"P{self.next_pole()}"
            pr = rng_for(self.seed, "pole", node["pole_id"])
            rural = fid.split("-")[0] in ("LPH", "KNV", "GVL")
            node["material"] = pr.choices(["WOOD", "CONCRETE", "STEEL"], [0.75, 0.2, 0.05] if rural else [0.45, 0.5, 0.05])[0]
            node["height_m"] = pr.choice([11.0, 12.5, 14.0])
            node["install_year"] = pr.randint(1962, 2024)
            node["crossarm_year"] = max(node["install_year"], pr.randint(1975, 2025))
        self.nodes.append(node)
        return node

    def _run(self, feeder, start, bearing, n_spans, style, r, phases, role, underground, first_device):
        """Lay n_spans spans from a start node; returns the list of nodes laid (start excluded)."""
        laid, prev, b = [], start, bearing
        rc = rng_for(self.seed, "conductor", feeder["feeder_id"], start["node_id"], role)
        opts = RUN_CONDUCTOR[("cable", "trunk" if role == "trunk" else "spur")] if underground else \
            RUN_CONDUCTOR.get((feeder["style"], role)) or RUN_CONDUCTOR[(feeder["style"], "spur")]
        run_conductor = rc.choices([c for c, _ in opts], [w for _, w in opts])[0]
        since_ds = r.randint(1, style["ds_every"][0])
        for s in range(n_spans):
            if style["grid"] and r.random() < 0.05:
                dev = (b - bearing + 180) % 360 - 180
                b += (-90 if dev > 45 else 90 if dev < -45 else r.choice((-90, 90)))
            b += r.uniform(-style["wander"], style["wander"])
            length = max(18.0, r.gauss(*style["span"])) * (1.6 if role == "swer" else 1.0)
            lat, lon = move(prev["lat"], prev["lon"], b, length)
            node = self._node(feeder["feeder_id"], lat, lon, kind="cable_joint" if underground else "pole",
                              overhead=not underground, bearing=b)
            span = dict(span_id=f"S{node['pole_id'][1:]}" if node["pole_id"] else f"C{len(self.spans) + 1:06d}",
                        feeder_id=feeder["feeder_id"], from_node=prev["node_id"], to_node=node["node_id"],
                        length_m=round(length, 1), overhead=not underground, phases=phases, role=role,
                        conductor=(run_conductor if underground else (r.choice(CONDUCTORS[feeder["style"]]), run_conductor)[1]),   # the old draw is kept
                        rating_amps=CONDUCTOR_RATING[run_conductor],
                        install_year=r.randint(1962, 2024) if not underground else r.randint(1995, 2025),
                        bushfire_prone=(feeder["style"] == "rural" and r.random() < 0.85) or
                                       (feeder["style"] != "rural" and not underground and r.random() < 0.08),
                        coords=[(prev["lat"], prev["lon"]), (lat, lon)])
            self.spans.append(span)
            if s == 0 and first_device:
                self._device_on(feeder, node, first_device, r, span_id=span["span_id"])
            laid.append(node)
            since_ds += 1
            if since_ds >= r.randint(*style["ds_every"]) or s == n_spans - 1 and role != "trunk":
                self._ds(feeder, node, underground, role, r)
                since_ds = 0
            prev = node
        return laid

    def _device_on(self, feeder, node, dtype, r, span_id=None):
        n = sum(1 for d in self.devices if d["feeder_id"] == feeder["feeder_id"] and d["type"] == dtype) + 1
        letter = {"CB": "CB", "RECLOSER": "R", "GAS_SWITCH": "G", "FUSE": "F", "SWER_ISOLATOR": "I", "NOP": "N"}[dtype]
        dev = dict(device_id=f"{feeder['feeder_id']}-{letter}{n:02d}", feeder_id=feeder["feeder_id"], type=dtype,
                   node_id=node["node_id"], span_id=span_id, lat=node["lat"], lon=node["lon"],
                   normally_open=dtype == "NOP", equnr=self.next_equnr(),
                   make=r.choice({"CB": ["Schneider", "ABB"], "RECLOSER": ["NOJA OSM", "Schneider ADVC"],
                                  "GAS_SWITCH": ["Schneider RL", "Lucy"], "FUSE": ["ABB EDO", "Schneider DO"],
                                  "SWER_ISOLATOR": ["Wilson SWER"], "NOP": ["Schneider RL"]}[dtype]),
                   year=r.randint(1985, 2024))
        dev["rating_amps"] = DEVICE_RATING.get(dtype) or rng_for(self.seed, "fuse", dev["device_id"]).choice([25, 40, 65, 100] if feeder["style"] != "rural" else [10, 16, 25, 40])
        self.devices.append(dev)
        return dev

    def _ds(self, feeder, node, underground, role, r):
        fnum = feeder["feeder_id"].replace("-F", "")
        n = sum(1 for d in self.dss if d["feeder_id"] == feeder["feeder_id"]) + 1
        rural = feeder["style"] == "rural"
        if underground:
            kind, kva = "kiosk", r.choice([500, 630, 1000])
        elif role == "swer":
            kind, kva = "pole_swer", r.choice([10, 25])
        else:
            kind, kva = "pole", r.choice([25, 63, 100] if rural else [200, 315, 400])
        ds = dict(ds_id=f"DS-{fnum}-{n:03d}", feeder_id=feeder["feeder_id"], node_id=node["node_id"], kind=kind,
                  kva=kva, lat=node["lat"], lon=node["lon"], pole_id=node["pole_id"], equnr=self.next_equnr(),
                  year=r.randint(1970, 2025), lv_monitor=r.random() < (0.35 if feeder["hero"] else 0.06))
        node["ds_id"] = ds["ds_id"]
        self.dss.append(ds)

    def _ties(self):
        """Normally open points between trunk ends and the nearest node of a neighbouring feeder. A feeder from another zone
        substation is preferred (it is what can carry customers through a zone substation outage), so it wins if it is within
        about twice the distance of the nearest same-zone feeder; town feeders reach a little further for one. Long rural
        feeders often end too far from anything and stay radial."""
        by_feeder = {}
        for n in self.nodes:
            if n["kind"] != "zs_bus":
                by_feeder.setdefault(n["feeder_id"], []).append(n)
        for f in self.feeders:
            end = next(n for n in self.nodes if n["node_id"] == f["trunk_end_node"])
            best = None
            for g in self.feeders:
                if g["feeder_id"] == f["feeder_id"] or g["area"] != f["area"]:
                    continue
                other = g["zs_id"] != f["zs_id"]
                reach = 2500 if f["style"] == "rural" or not other else 4200
                for n in by_feeder[g["feeder_id"]][::3]:
                    d = dist_m(end["lat"], end["lon"], n["lat"], n["lon"])
                    score = d * (0.5 if other else 1.0)
                    if d < reach and (best is None or score < best[0]):
                        best = (score, g, n, d)
            if best:
                _, g, n, d = best
                r = rng_for(self.seed, "tie", f["feeder_id"])
                dev = self._device_on(f, end, "NOP", r)
                dev.update(tie_to_feeder=g["feeder_id"], tie_to_node=n["node_id"], tie_length_m=round(d, 1))
                self.spans.append(dict(span_id=f"T{len(self.spans) + 1:06d}", feeder_id=f["feeder_id"],
                                       from_node=end["node_id"], to_node=n["node_id"], length_m=round(d, 1),
                                       overhead=True, phases="ABC", role="tie", conductor="AAAC Neptune", rating_amps=CONDUCTOR_RATING["AAAC Neptune"],
                                       install_year=r.randint(1990, 2020), bushfire_prone=f["style"] == "rural",
                                       coords=[(end["lat"], end["lon"]), (n["lat"], n["lon"])], tie_device=dev["device_id"]))

    def _customers(self):
        rates = self.cfg["rates"]
        self.node_index = {x["node_id"]: x for x in self.nodes}
        feeders = {f["feeder_id"]: f for f in self.feeders}
        nmi_seq = 3400000
        crit_plan = [("hospital", 4, ("metro", "coastal")), ("aged_care", 10, ("metro", "coastal", "rural")),
                     ("school", 26, ("metro", "coastal", "rural")), ("water_pumping", 12, ("metro", "coastal", "rural")),
                     ("telecom", 15, ("metro", "coastal", "rural")), ("data_centre", 1, ("metro",))]
        rc = rng_for(self.seed, "critical")
        crit_ds = {}
        for kind, count, styles in crit_plan:
            pool = [d for d in self.dss if feeders[d["feeder_id"]]["style"] in styles and d["kind"] != "pole_swer"
                    and d["ds_id"] not in crit_ds]
            for d in rc.sample(pool, count):
                crit_ds[d["ds_id"]] = kind
        for ds in self.dss:
            f = feeders[ds["feeder_id"]]
            style = STYLE[f["style"]]
            r = rng_for(self.seed, "cust", ds["ds_id"])
            if ds["kind"] == "pole_swer":
                n = r.randint(1, 4)
            elif ds["kind"] == "kiosk":
                n = r.randint(*style["cust_kiosk"])
            else:
                n = r.randint(*style["cust_pole"])
            lots = self._lots(ds, f, n + (1 if ds["ds_id"] in crit_ds else 0))
            for i in range(n):
                nmi_seq += 1
                base = f"491{nmi_seq:07d}"[:10]
                r.uniform(0, 360), r.gauss(0, 1)   # the draws that used to scatter the house; kept so every other attribute stays the same
                lot = lots[i]
                lat, lon = lot["lat"], lot["lon"]
                ctype = "residential" if r.random() < 0.925 else ("small_business" if r.random() < 0.9 else "large_business")
                nmi = dict(nmi=base, checksum=nmi_checksum(base), ds_id=ds["ds_id"], feeder_id=ds["feeder_id"],
                           lat=lat, lon=lon, customer_type=ctype, phases=1 if ctype == "residential" and r.random() < 0.8 else 3,
                           life_support=ctype == "residential" and r.random() < rates["life_support_share"],
                           critical_site=None, tariff="EA010" if ctype == "residential" else "EA050",
                           hero=f["hero"], recorded_ds_id=ds["ds_id"], **lot["addr"])
                self.nmis.append(nmi)
                solar_p = rates["solar_share"] * (1.25 if f["zs_id"] in ("ZS-BRP", "ZS-MGF", "ZS-CTC") else
                                                  0.85 if f["style"] == "rural" else 1.0)
                if ctype == "residential" and r.random() < solar_p:
                    kw = r.choice([3.0, 5.0, 6.6, 6.6, 6.6, 8.0, 10.0, 13.2])
                    self.der.append(dict(der_id=f"DER-{base}-PV", nmi=base, kind="solar_pv", kw=kw,
                                         export_limit_kva=5.0 if kw > 5 else kw, installed=r.randint(2009, 2026)))
                    if r.random() < rates["battery_share_of_solar"]:
                        self.der.append(dict(der_id=f"DER-{base}-BAT", nmi=base, kind="battery",
                                             kw=r.choice([5.0, 5.0, 7.6]), kwh=r.choice([10.0, 13.5, 13.5]),
                                             export_limit_kva=5.0, installed=r.randint(2018, 2026)))
                if ctype == "residential" and r.random() < rates["home_ev_share"]:
                    self.der.append(dict(der_id=f"DER-{base}-EV", nmi=base, kind="home_ev_charger",
                                         kw=r.choice([7.0, 7.0, 11.0]), export_limit_kva=0.0, installed=r.randint(2019, 2026)))
            if ds["ds_id"] in crit_ds:
                kind = crit_ds[ds["ds_id"]]
                nmi_seq += 1
                base = f"491{nmi_seq:07d}"[:10]
                lot = lots[n]
                self.nmis.append(dict(nmi=base, checksum=nmi_checksum(base), ds_id=ds["ds_id"], feeder_id=ds["feeder_id"],
                                      lat=lot["lat"], lon=lot["lon"], customer_type="large_business",
                                      phases=3, life_support=False, critical_site=kind, tariff="EA302",
                                      hero=f["hero"], recorded_ds_id=ds["ds_id"], **lot["addr"]))
                rg = rng_for(self.seed, "generator", base)   # its own draws, so nothing else changes
                gen = rg.random() < self.cfg["critical_site_backup"][kind]
                self.critical_sites.append(dict(nmi=base, kind=kind, ds_id=ds["ds_id"], feeder_id=ds["feeder_id"],
                                                name=f"{kind.replace('_', ' ').title()} at {ds['ds_id']}", backup_generator=gen,
                                                generator_kva=rg.choice({"hospital": [500, 800, 1250], "data_centre": [2000, 2500],
                                                                         "telecom": [20, 30, 50], "aged_care": [100, 150, 250],
                                                                         "water_pumping": [150, 300, 500], "school": [60, 100]}[kind]) if gen else 0,
                                                fuel_hours=rg.choice([8, 12, 24, 48, 72]) if gen else 0))
        # Public DC charging hubs and community batteries on metro and coastal kiosks.
        rh = rng_for(self.seed, "hubs")
        kiosks = [d for d in self.dss if d["kind"] == "kiosk"]
        for i, d in enumerate(rh.sample(kiosks, min(12, len(kiosks)))):
            self.ev_hubs.append(dict(hub_id=f"EVH-{i + 1:02d}", ds_id=d["ds_id"], feeder_id=d["feeder_id"],
                                     lat=d["lat"], lon=d["lon"], chargers=rh.choice([4, 6, 8]), kw_each=rh.choice([75, 150])))
        for i, d in enumerate(rh.sample(kiosks, min(6, len(kiosks)))):
            self.der.append(dict(der_id=f"CB-{i + 1:02d}", nmi=None, ds_id=d["ds_id"], kind="community_battery",
                                 kw=250.0, kwh=500.0, export_limit_kva=250.0, installed=rh.randint(2022, 2026)))
        # Records that disagree: a small share of NMIs are recorded against a neighbouring transformer.
        rq = rng_for(self.seed, "dq")
        ds_by_feeder = {}
        for d in self.dss:
            ds_by_feeder.setdefault(d["feeder_id"], []).append(d["ds_id"])
        for n in self.nmis:
            if n["critical_site"] is None and rq.random() < 0.004:
                sibs = [x for x in ds_by_feeder[n["feeder_id"]] if x != n["ds_id"]]
                if sibs:
                    n["recorded_ds_id"] = rq.choice(sibs)

    # ------------------------------------------------------------------ streets and lots
    def _street(self, rp, lat, lon, rural):
        """A new street: its suburb and postcode from where it sits, and a name and type not yet used in that suburb."""
        if self._geo is None:
            from geo import Geo
            self._geo = Geo()
        loc = self._geo.locality(lat, lon) or dict(locality="", postcode="")
        used = self._street_names.setdefault(loc["locality"], set())
        types = RURAL_TYPES if rural else STREET_TYPES
        for _ in range(400):
            t = rp.choices(types, [w for _, _, w in types])[0]
            name = rp.choice(STREET_NAMES)
            if t[0] == "Creek Road":   # "Gully Creek Road": the creek is part of the name, the type is Road
                name, t = f"{name} Creek", ("Road", "RD", 0)
            if (name, t[0]) not in used:
                break
        used.add((name, t[0]))
        st = dict(street_id=f"STR-{len(self.streets) + 1:05d}", street_name=name, street_type=t[0], street_type_code=t[1],
                  suburb=loc["locality"], postcode=loc["postcode"])
        self.streets.append(st)
        return st

    def _lots(self, ds, feeder, n):
        """n house lots for the customers of one distribution substation, nearest the transformer first.

        The street the line runs along passes the transformer (overhead and underground lines follow the road reserve); a busy
        transformer also feeds side streets crossing it. A street that lines up with one already laid nearby is the same road, so it keeps
        that road's name and numbering. Houses face the street, odd numbers on the left and even on the right, numbered by distance
        along the road (urban: one number per lot frontage; rural: metres from the start of the road / 10). Lots in another street's
        road reserve (corner blocks), on top of a neighbouring block's house, or repeating an address already used are skipped. Where
        the block runs out of land, the remaining customers live in units on its nearest lots ("3/45 Wattle Street")."""
        rp = rng_for(self.seed, "lots", ds["ds_id"])
        node = self.node_index[ds["node_id"]]
        kind = "village" if feeder["style"] == "rural" and ds["kind"] == "kiosk" else feeder["style"]
        L = LOTS[kind]
        rural = kind == "rural"
        b0 = node.get("bearing") or rp.uniform(0, 360)
        per_street = 2 * int(2 * L["reach"] / (sum(L["frontage"]) / 2))
        k = 1 if rural else min(4, max(1, math.ceil(n * 1.25 / per_street)))
        geo = [(ds["lat"], ds["lon"], b0)]
        for j in range(1, k):   # side streets cross the main one at alternating distances from the transformer
            off = (1 if j % 2 else -1) * L["reach"] * (0.35 + 0.25 * (j // 2)) * rp.uniform(0.85, 1.15)
            la, lo = move(ds["lat"], ds["lon"], b0, off)
            geo.append((la, lo, b0 + 90 + rp.uniform(-6, 6)))
        lines = [self._road(rp, la, lo, b, rural, L) for la, lo, b in geo]
        reach, chosen = L["reach"], []
        for attempt in range(12):   # a crowded block needs a longer stretch of road, and then another side street
            if attempt and attempt % 4 == 0:
                off = (1 if attempt % 8 else -1) * L["reach"] * (0.6 + 0.3 * attempt / 4) * rp.uniform(0.9, 1.1)
                la, lo = move(ds["lat"], ds["lon"], b0, off)
                g = (la, lo, b0 + 90 + rp.uniform(-6, 6))
                geo.append(g); lines.append(self._road(rp, *g, rural, L))
                reach = L["reach"]
            chosen, keys, here = [], set(), []
            for c in sorted(self._candidates(rp, geo, lines, L, reach, rural, ds), key=lambda c: c["d"]):
                key = (c["line"]["st"]["street_id"], c["num"])
                if key in self._addr_used or key in keys or self._occupied(c["lat"], c["lon"], 9.0) or \
                        any(dist_m(c["lat"], c["lon"], a, b) < 9.0 for a, b in here):
                    continue
                chosen.append(c); keys.add(key); here.append((c["lat"], c["lon"]))
                if len(chosen) == n:
                    break
            if len(chosen) == n:
                break
            reach *= 1.3
        if not chosen:   # every lot here is taken: a new short street off the main one
            g = (ds["lat"], ds["lon"], b0 + 45)
            line = self._road(rp, *g, rural, L, new=True)
            chosen = [c for c in sorted(self._candidates(rp, [g], [line], L, L["reach"], rural, ds), key=lambda c: c["d"])
                      if (c["line"]["st"]["street_id"], c["num"]) not in self._addr_used][:max(1, n // 6)]
        # where the block has run out of land, the rest live in units: small blocks of flats on the lots nearest the transformer
        units, spare = {}, n - len(chosen)
        for i in range(spare):
            units.setdefault(i % max(1, min(len(chosen), math.ceil(spare / 8))), []).append(i)
        out = []
        for j, c in enumerate(chosen):
            self._addr_used.add((c["line"]["st"]["street_id"], c["num"]))
            self._house_at(c["lat"], c["lon"])
            st = c["line"]["st"]
            flats = [""] if j not in units else [str(u + 1) for u in range(len(units[j]) + 1)]
            for unit in flats:
                lat, lon = (c["lat"], c["lon"]) if not unit else move(c["lat"], c["lon"], rp.uniform(0, 360), rp.uniform(0.5, 4))
                out.append(dict(lat=lat, lon=lon, addr=dict(
                    unit=unit, house_number=str(c["num"]), street_name=st["street_name"], street_type=st["street_type"],
                    street_type_code=st["street_type_code"], suburb=st["suburb"], postcode=st["postcode"], street_id=st["street_id"],
                    elevation_m=max(1.0, self._geo.elevation(lat, lon)))))   # no house sits below the high-water mark
        out.sort(key=lambda o: dist_m(o["lat"], o["lon"], ds["lat"], ds["lon"]))
        return out[:n]

    # roads: every street segment laid so far, in a 0.01 degree (about 1 km) bucket index
    def _near_lines(self, lat, lon):
        ci, cj = int(lat // 0.01), int(lon // 0.01)
        return [ln for di in (-1, 0, 1) for dj in (-1, 0, 1) for ln in self._lines.get((ci + di, cj + dj), [])]

    def _road(self, rp, la, lo, b, rural, L, new=False):
        """The road segment through (la, lo) on bearing b: the continuation of a road already laid if one lines up, otherwise a new street."""
        for ln in ([] if new else self._near_lines(la, lo)):
            same_way = abs((b - ln["b"] + 90) % 180 - 90) < 12
            if same_way and abs(self._across(la, lo, (ln["la"], ln["lo"], ln["b"]))) < 25 and \
                    abs(self._along(la, lo, (ln["la"], ln["lo"], ln["b"]))) < 2200:
                ref = ln["ref"]
                break
        else:
            ref = dict(st=self._street(rp, la, lo, rural), la=la, lo=lo, b=b, fr=rp.uniform(*L["frontage"]),
                       start=rp.randint(30, 600) * 10 if rural else rp.randint(250, 1200))   # metres of road before this point
        line = dict(la=la, lo=lo, b=b, reach=L["reach"] * 2.2, ref=ref, st=ref["st"])
        self._lines.setdefault((int(la // 0.01), int(lo // 0.01)), []).append(line)
        return line

    def _occupied(self, lat, lon, within):
        ci, cj = int(lat // 0.0005), int(lon // 0.0005)
        return any(dist_m(lat, lon, a, b) < within for di in (-1, 0, 1) for dj in (-1, 0, 1) for a, b in self._houses.get((ci + di, cj + dj), []))

    def _house_at(self, lat, lon):
        self._houses.setdefault((int(lat // 0.0005), int(lon // 0.0005)), []).append((lat, lon))

    def _candidates(self, rp, geo, lines, L, reach, rural, ds):
        """Every lot along the block's streets within reach of the transformer, both sides, with its house number. Lots sit on a
        fixed grid along each road (one lot frontage apart, counted from the start of the road), so a road continued from a
        neighbouring block keeps its spacing and numbering, and every lot has its own number."""
        cand = []
        for (la, lo, b), line in zip(geo, lines):
            ref = line["ref"]; rl = (ref["la"], ref["lo"], ref["b"]); fr = ref["fr"]
            here = ref["start"] + self._along(la, lo, rl)   # metres along the road at this segment's centre
            for k in range(max(0, math.ceil((here - reach) / fr)), int((here + reach) / fr) + 1):
                a = (k + 0.5) * fr - here
                for side in (-1, 1):
                    dist = L["setback"] + rp.uniform(-1.5, 1.5) * (6 if rural else 1)
                    pa, po = move(la, lo, b, a + rp.uniform(-1, 1))
                    pa, po = move(pa, po, b + 90 * side, dist)
                    if any(ln is not line and abs(self._across(pa, po, (ln["la"], ln["lo"], ln["b"]))) < L["setback"] * 0.9 and
                           abs(self._along(pa, po, (ln["la"], ln["lo"], ln["b"]))) < ln["reach"] for ln in self._near_lines(pa, po)):
                        continue   # in another street's road reserve (a corner block)
                    left = self._across(pa, po, rl) < 0
                    num = max(1, int((k + 0.5) * fr) // 10) if rural else 2 * k + 1   # rural: metres from the start of the road / 10
                    if (num % 2 == 1) != left:
                        num += 1   # odd on the left of the road, even on the right
                    d = dist_m(pa, po, ds["lat"], ds["lon"])
                    if d <= 1100:   # an LV circuit doesn't run further (and the records check treats beyond 1.2 km as a wrong record)
                        cand.append(dict(lat=pa, lon=po, d=d, line=line, num=num))
        return cand

    @staticmethod
    def _along(lat, lon, street):
        la, lo, b = street
        dy, dx = (lat - la) * M_PER_DEG_LAT, (lon - lo) * m_per_deg_lon(la)
        return dx * math.sin(math.radians(b)) + dy * math.cos(math.radians(b))

    @staticmethod
    def _across(lat, lon, street):
        la, lo, b = street
        dy, dx = (lat - la) * M_PER_DEG_LAT, (lon - lo) * m_per_deg_lon(la)
        return dx * math.cos(math.radians(b)) - dy * math.sin(math.radians(b))

    # ------------------------------------------------------------------ summary
    def summary(self):
        return dict(bulk_supply=len(self.bsps), transmission_substations=len(self.tss), transmission_transformers=len(self.ts_transformers),
                    transmission_lines=len(self.tx_lines), zone_substations=len(self.zones), zs_transformers=len(self.zs_transformers),
                    feeders=len(self.feeders), nodes=len(self.nodes), poles=sum(1 for n in self.nodes if n["pole_id"]),
                    spans=len(self.spans), devices=len(self.devices), distribution_substations=len(self.dss),
                    nmis=len(self.nmis), life_support=sum(1 for n in self.nmis if n["life_support"]),
                    critical_sites=len(self.critical_sites), der=len(self.der), ev_hubs=len(self.ev_hubs),
                    hero_nmis=sum(1 for n in self.nmis if n["hero"]),
                    misrecorded_nmis=sum(1 for n in self.nmis if n["recorded_ds_id"] != n["ds_id"]), streets=len(self.streets),
                    suburbs=len({n["suburb"] for n in self.nmis}))


def load_config(path):
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    import os
    import sys
    import time
    here = os.path.dirname(os.path.abspath(__file__))
    t = time.time()
    net = Network(load_config(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "config", "atlas.json"))).build()
    print(json.dumps(net.summary(), indent=1), f"{time.time() - t:.1f}s")
