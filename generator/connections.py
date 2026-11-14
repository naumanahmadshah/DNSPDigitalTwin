"""New connection applications: the distributor's case management export (connections/ in the landing volume).

Two files, in the shape a case management system writes them:
- connection_cases_<stamp>.csv: one row per application, the current state of the case (stage, payment, service provider, blocker).
- connection_stage_events_<stamp>.csv: one row per stage a case has been through, with the day it was entered and the day it was left
  (blank while it is the current stage).

Applications: new home connections, solar and battery and EV charger connections at premises already on the register, commercial
connections and subdivisions. Existing premises come from the NMI register; new premises sit beside a distribution substation on the
network. Deterministic from the network seed. Every date is on or before the as-of day, and the stage on each case is the stage it
is at on that day. Applicant names are synthetic.
"""
import datetime as dt
import os

from network import move, rng_for
from writers import _write_csv

STAGES = ["apply", "assess", "offer", "design", "build", "commission", "connected"]
DEFAULT_COUNT = 600

# Each application type: its share of applications, whether it is at premises already on the register, the target connection window
# (days from application), the days each stage typically takes, and the service provider's trade.
TYPES = {
    "residential": dict(weight=0.24, existing=False, target=(60, 120), kind="individual", provider="Network build",
                        days=dict(apply=(3, 8), assess=(10, 30), offer=(14, 40), design=(20, 45), build=(20, 40), commission=(5, 15))),
    "solar_battery": dict(weight=0.34, existing=True, target=(30, 60), kind="individual", provider="DER installer",
                          days=dict(apply=(2, 6), assess=(5, 15), offer=(7, 20), design=(7, 14), build=(5, 10), commission=(7, 20))),
    "ev_charging": dict(weight=0.14, existing=True, target=(30, 60), kind="individual", provider="DER installer",
                        days=dict(apply=(2, 5), assess=(3, 12), offer=(5, 14), design=(5, 10), build=(5, 12), commission=(5, 15))),
    "commercial": dict(weight=0.18, existing=False, target=(150, 240), kind="company", provider="Contracted electrician",
                       days=dict(apply=(5, 15), assess=(20, 50), offer=(21, 40), design=(30, 60), build=(45, 90), commission=(10, 25))),
    "subdivision": dict(weight=0.10, existing=False, target=(300, 480), kind="company", provider="Contracted civil and electrical",
                        days=dict(apply=(10, 30), assess=(45, 90), offer=(30, 60), design=(60, 120), build=(90, 180), commission=(20, 40))),
}
DER_TYPES = ("solar_battery", "ev_charging")

# The blockers a case can carry in its current stage, with the chance per type (checked in this order, the first hit is the blocker).
BLOCKERS = {
    "assess": [("transformer", {"solar_battery": .10, "ev_charging": .14, "commercial": .20, "residential": .04, "subdivision": .10}),
               ("council", {"commercial": .18, "subdivision": .35})],
    "offer": [("quote_unaccepted", {t: .30 for t in TYPES}),
              ("asp_missing", {"solar_battery": .08, "ev_charging": .08, "commercial": .10, "residential": .05, "subdivision": .10})],
    "design": [("easement", {"residential": .10, "commercial": .14, "subdivision": .25}),
               ("customer_design", {t: .12 for t in TYPES}),
               ("invoice_unpaid", {"residential": .12, "commercial": .15, "subdivision": .15, "solar_battery": .05, "ev_charging": .05})],
    "build": [("outage_needed", {"commercial": .20, "subdivision": .25, "residential": .03}),
              ("materials", {t: .06 for t in TYPES})],
    "commission": [("meter_not_booked", {t: .18 for t in TYPES}), ("docs_incomplete", {"solar_battery": .15, "ev_charging": .12})],
}

FIRST = ["Amara", "Liam", "Chloe", "Noah", "Priya", "Jack", "Mei", "Oliver", "Sienna", "Ahmed", "Grace", "Lucas", "Hannah", "Mateo", "Ruby",
         "Ethan", "Yasmin", "Harrison", "Zara", "Samuel", "Imogen", "Kofi", "Tahlia", "Declan", "Aisha", "Patrick", "Lily", "Dimitri", "Eva", "Rhys"]
LAST = ["Nguyen", "Smith", "Patel", "Brown", "Kowalski", "Jones", "Wong", "Taylor", "Haddad", "Murphy", "Singh", "Campbell", "Ibrahim", "Walsh",
        "Kim", "Anderson", "Costa", "Hughes", "Okafor", "Russo", "Lindqvist", "Mitchell", "Dang", "O'Brien", "Thompson", "Rahman"]
COMPANY_A = ["Harbour", "Ridgeline", "Westbrook", "Bluewater", "Summit", "Oakridge", "Kestrel", "Lanternfield", "Ironbark", "Paramount", "Coastal",
             "Tallowwood", "Gumleaf", "Redwood", "Keystone", "Silverbrook"]
COMPANY_B = ["Developments", "Holdings", "Property Group", "Constructions", "Retail Trust", "Logistics", "Capital", "Land Partners", "Enterprises"]
DER_INSTALLERS = ["Sunline Solar Installers", "Harbour Power Electrical", "Ridgeline Electrical Services", "Westbrook Renewables",
                  "Coastal Sparks Electrical", "Paramount Battery Installs", "Bluewater Electrical", "Summit Solar Co", "Kestrel EV Charging",
                  "Nightcap Electrical", "Lantern Power Services", "Oakridge Electrical"]
CONTRACTORS = ["Brightline Contracting", "Gantry Civil and Electrical", "Redwood Connections", "Tidewater Network Build",
               "Keystone Subdivision Electrical"]


def _pick_type(r):
    x, acc = r.random(), 0.0
    for code, spec in TYPES.items():
        acc += spec["weight"]
        if x < acc:
            return code
    return "residential"


def _timeline(r, ctype, applied, as_of):
    """Walk the case stage by stage from the application day, stopping at the as-of day. Some cases run long (a stage takes more than
    twice its usual time), which is what makes a pipeline show ageing. Returns (stage, stage_entered, [(stage, entered, exited)])."""
    spec, i, entered, events = TYPES[ctype]["days"], 0, applied, []
    while True:
        if i == len(STAGES) - 1:
            events.append(("connected", entered, None))
            break
        stage = STAGES[i]
        lo, hi = spec[stage]
        d = r.randint(lo, hi)
        if r.random() < 0.12:
            d = int(d * 2.2)
        exited = entered + dt.timedelta(days=d)
        if exited > as_of:
            events.append((stage, entered, None))
            break
        events.append((stage, entered, exited))
        entered, i = exited, i + 1
    return events[-1][0], events[-1][1], events


def _blocker(r, stage, ctype):
    for code, chance in BLOCKERS.get(stage, []):
        if r.random() < chance.get(ctype, 0):
            return code
    return ""


def _payment(r, stage, ctype, augmented, blocker):
    """Quoted once the offer is out, invoiced at design, paid from build. A DER connection that needs no network work pays nothing."""
    if ctype in DER_TYPES and not augmented:
        return "no_charge"
    if stage in ("apply", "assess"):
        return "not_due"
    if stage == "offer":
        return "quoted"
    if stage == "design":
        return "invoiced" if blocker == "invoice_unpaid" or r.random() < 0.5 else "paid"
    return "paid"


def _premises_for(net, r, ctype, ctx):
    """The location of a case: an existing premises for DER types, a new site beside a distribution substation otherwise."""
    if TYPES[ctype]["existing"]:
        m = r.choice(ctx["premises"])
        return dict(nmi=m["nmi"], address=f"{(m['unit'] + '/') if m['unit'] else ''}{m['house_number']} {m['street_name']} {m['street_type']}",
                    suburb=m["suburb"], postcode=m["postcode"], lat=m["lat"], lon=m["lon"], feeder_id=m["feeder_id"], ds_id=m["ds_id"])
    ds = r.choice(ctx["dss"])
    near = r.choice(ctx["by_ds"].get(ds["ds_id"]) or ctx["premises"])
    lat, lon = move(ds["lat"], ds["lon"], r.uniform(0, 360), r.uniform(20, 160))
    return dict(nmi="", address=f"{r.randint(2, 320)} {near['street_name']} {near['street_type']}", suburb=near["suburb"],
                postcode=near["postcode"], lat=lat, lon=lon, feeder_id=ds["feeder_id"], ds_id=ds["ds_id"])


def _size(r, ctype):
    """Load and export by type: kVA the premises draws, kW of solar it exports, kWh of battery, lots for a subdivision, phases."""
    if ctype == "residential":
        return dict(load_kva=r.choice([10, 15, 20]), export_kw=0, battery_kwh=0, lots="", phases=r.choice([1, 1, 3]))
    if ctype == "solar_battery":
        return dict(load_kva=r.choice([7, 10, 15]), export_kw=r.choice([5, 6.6, 8, 10]), battery_kwh=r.choice([10, 13.5]), lots="", phases=r.choice([1, 3]))
    if ctype == "ev_charging":
        return dict(load_kva=r.choice([7, 11]), export_kw=0, battery_kwh=0, lots="", phases=r.choice([1, 3]))
    if ctype == "commercial":
        return dict(load_kva=r.choice([63, 100, 160, 200, 315, 500]), export_kw=0, battery_kwh=0, lots="", phases=3)
    lots = r.randint(12, 120)
    return dict(load_kva=round(lots * 4.5), export_kw=0, battery_kwh=0, lots=lots, phases=3)


def _name(r, ctype):
    """Synthetic applicant: a person for home and DER connections, a company for commercial and subdivision applications."""
    if ctype == "subdivision":
        return f"{r.choice(COMPANY_A)} {r.choice(COMPANY_B)} Pty Ltd"
    if ctype == "commercial":
        return f"{r.choice(COMPANY_A)} Pty Ltd"
    return f"{r.choice(FIRST)} {r.choice(LAST)}"


def build_connections(net, count=DEFAULT_COUNT):
    """The applications (cases) and their stage events, deterministic from the network seed. Returns (cases, events)."""
    as_of = dt.date.fromisoformat(net.cfg["as_of"])
    feeders = {f["feeder_id"]: f for f in net.feeders}
    ctx = dict(premises=[m for m in net.nmis if m["customer_type"] != "large_business" and not m.get("critical_site")],
               dss=[d for d in net.dss if d["kind"] != "pole_swer"], by_ds={})
    for m in net.nmis:
        ctx["by_ds"].setdefault(m["ds_id"], []).append(m)
    cases, events = [], []
    for i in range(count):
        r = rng_for(net.seed, "connection", i)
        ctype = _pick_type(r)
        spec = TYPES[ctype]
        applied = as_of - dt.timedelta(days=1 + int(300 * r.random() ** 2.2))   # most applications are recent; a tail is older
        target = applied + dt.timedelta(days=r.randint(*spec["target"]))
        stage, stage_entered, timeline = _timeline(r, ctype, applied, as_of)
        blocker = "" if stage in ("apply", "connected") else _blocker(r, stage, ctype)
        augmented = ctype not in DER_TYPES or r.random() < 0.2
        payment = _payment(r, stage, ctype, augmented, blocker)
        if blocker == "quote_unaccepted":
            payment = "quoted"
        if ctype in DER_TYPES:
            asp = "" if blocker == "asp_missing" else r.choice(DER_INSTALLERS)
        else:
            asp = "" if (blocker == "asp_missing" or STAGES.index(stage) < 3) else r.choice(CONTRACTORS)
        place = _premises_for(net, r, ctype, ctx)
        feeder = feeders[place["feeder_id"]]
        size = _size(r, ctype)
        cases.append(dict(
            case_id=f"CN-{i + 1:05d}", applicant_name=_name(r, ctype), applicant_kind=spec["kind"], connection_type=ctype,
            address=place["address"], suburb=place["suburb"], postcode=place["postcode"], state="NSW",
            latitude=round(place["lat"], 6), longitude=round(place["lon"], 6), nmi=place["nmi"], zs_id=feeder["zs_id"],
            feeder_id=place["feeder_id"], ds_id=place["ds_id"], load_kva=size["load_kva"], export_kw=size["export_kw"],
            battery_kwh=size["battery_kwh"], lots=size["lots"], phases=size["phases"], applied_date=applied.isoformat(),
            target_date=target.isoformat(), current_stage=stage, stage_entered_date=stage_entered.isoformat(),
            payment_status=payment, asp_name=asp, blocker_code=blocker))
        for seq, (st, entered, exited) in enumerate(timeline, start=1):
            events.append(dict(case_id=f"CN-{i + 1:05d}", stage=st, stage_seq=seq, entered_date=entered.isoformat(),
                               exited_date=exited.isoformat() if exited else ""))
    return cases, events


CASE_COLS = ["case_id", "applicant_name", "applicant_kind", "connection_type", "address", "suburb", "postcode", "state", "latitude",
             "longitude", "nmi", "zs_id", "feeder_id", "ds_id", "load_kva", "export_kw", "battery_kwh", "lots", "phases", "applied_date",
             "target_date", "current_stage", "stage_entered_date", "payment_status", "asp_name", "blocker_code"]
EVENT_COLS = ["case_id", "stage", "stage_seq", "entered_date", "exited_date"]


def write_connections(net, root, stamp, count=DEFAULT_COUNT):
    """Write the connection application export into <root>/connections. Called by generator/run_setup.py after the network is written.
    Returns the counts for the setup log."""
    cases, events = build_connections(net, count)
    out = os.path.join(root, "connections")
    _write_csv(os.path.join(out, f"connection_cases_{stamp}.csv"), CASE_COLS, [[c[k] for k in CASE_COLS] for c in cases])
    _write_csv(os.path.join(out, f"connection_stage_events_{stamp}.csv"), EVENT_COLS, [[e[k] for k in EVENT_COLS] for e in events])
    open_cases = sum(1 for c in cases if c["current_stage"] != "connected")
    return dict(cases=len(cases), stage_events=len(events), open=open_cases, connected=len(cases) - open_cases)
