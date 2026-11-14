"""Customers: two views of the customer base.

Connection applications: new connection applications from the case management export (gold gld_connection_*, synced into Lakebase): the pipeline by
stage with ageing and what is stuck, the applications, and each application's journey through the stages.
Existing customers: the premises register (synced gld_nmi): supply status (live outages on the protective device), life support, critical
sites, distributed energy, the notification preferences, the outages the premises' protective device has seen, and the notices sent to it.

Dates in the connection marts are the as-of day's; they move to today here, as the resource management data does. Outage history is
recorded history, so it is not moved.
"""
import datetime as dt

from core import AS_OF, pq
import notify as N

STAGE_ORDER = ["apply", "assess", "offer", "design", "build", "commission", "connected"]
_cache = {"t": 0.0, "k": None}


def _s():
    return N.shift()


def _sum_known(rows, key):
    return sum(int(r.get(key) or 0) for r in rows)


# ------------------------------------------------------------------ Connection applications
def pipeline():
    """The pipeline by stage (open applications, stuck, blocked, median time in stage, ageing) and the headline
    figures the screen shows across the top (the screen breaks each one down into its cohorts from the applications themselves)."""
    stages = pq("""SELECT stage, stage_label, stage_no, open_applications, stuck_applications, blocked_applications, median_days_in_stage,
                          aged_0_30_days, aged_31_60_days, aged_61_90_days, aged_over_90_days
                   FROM gld_connection_stage ORDER BY stage_no""", ttl=120)
    pay = pq("""SELECT payment_status, payment_label, count(*) AS applications FROM gld_connection_application WHERE is_open
                GROUP BY 1, 2 ORDER BY applications DESC""", ttl=120)
    open_n = _sum_known(stages, "open_applications")
    kpi = dict(open=open_n, stuck=_sum_known(stages, "stuck_applications"), blocked=_sum_known(stages, "blocked_applications"),
               awaiting_payment=sum(int(p["applications"]) for p in pay if p["payment_status"] in ("quoted", "invoiced")),
               over_target=pq("SELECT count(*) AS n FROM gld_connection_application WHERE is_open AND days_past_target > 0", ttl=120)[0]["n"])
    return dict(as_of=AS_OF.isoformat(), today=N.today().isoformat(), kpi=kpi, stages=stages)


def applications(stage=None, ctype=None, stuck_only=False, q=None, open_only=True, limit=2000):
    """Applications for the list, stuck first and then the longest in stage. Dates move to today."""
    s = _s()
    rows = pq("""SELECT application_id, applicant_name, applicant_kind_label, address, suburb, postcode, zs_id, feeder_id, nmi,
                        connection_type, connection_type_label, load_kva, export_kw, battery_kwh, lots, phases, stage, stage_label, stage_no,
                        is_open, stage_entered_date + %(s)s AS stage_entered_date, days_in_stage, applied_date + %(s)s AS applied_date,
                        days_since_applied, target_date + %(s)s AS target_date, days_past_target, payment_status, payment_label, asp,
                        blocker, blocker_label, stuck, stuck_reason
                 FROM gld_connection_application
                 WHERE (%(open)s = false OR is_open)
                   AND (%(st)s::text IS NULL OR stage = %(st)s::text)
                   AND (%(ty)s::text IS NULL OR connection_type = %(ty)s::text)
                   AND (%(sk)s = false OR stuck)
                   AND (%(q)s::text IS NULL OR applicant_name ILIKE %(qq)s OR address ILIKE %(qq)s OR application_id ILIKE %(qq)s
                        OR suburb ILIKE %(qq)s)
                 ORDER BY stuck DESC, days_in_stage DESC, application_id
                 LIMIT %(l)s""", {"s": s, "open": bool(open_only), "st": stage or None, "ty": ctype or None, "sk": bool(stuck_only),
                                  "q": q or None, "qq": f"%{q}%" if q else None, "l": int(limit)})
    total = pq("""SELECT count(*) AS n FROM gld_connection_application WHERE (%(open)s = false OR is_open)
                  AND (%(st)s::text IS NULL OR stage = %(st)s::text) AND (%(ty)s::text IS NULL OR connection_type = %(ty)s::text)
                  AND (%(sk)s = false OR stuck)""", {"open": bool(open_only), "st": stage or None, "ty": ctype or None, "sk": bool(stuck_only)})
    return dict(rows=rows, total=int(total[0]["n"]), shown=len(rows))


def _supply_chain(app):
    """How the premises is supplied, from the bulk supply point down: the bulk supply point, the transmission substation (the first of
    two when the zone is fed from both, the other named), the zone substation, the feeder, the distribution substation (the one the NMI
    is on, or for a premises with no NMI yet the nearest on the feeder, its likely connection point) and the premises."""
    f = pq("SELECT feeder_id, feeder_name, zs_id, zs_name, customers, status FROM gld_feeder_state WHERE feeder_id = %(f)s",
           {"f": app["feeder_id"]}, ttl=600)
    f = f[0] if f else dict(feeder_id=app["feeder_id"], feeder_name=app["feeder_id"], zs_id=app["zs_id"], zs_name=app["zs_id"], customers=None, status=None)
    zs = pq("SELECT ts_ids, single_fed FROM gld_zone_supply WHERE zs_id = %(z)s", {"z": f["zs_id"]}, ttl=600)
    ts_ids = list((zs[0]["ts_ids"] if zs else None) or [])
    ts = {t["ts_id"]: t for t in pq("""SELECT ts_id, name, kv, secondary_kv, bsp_id, bulk_supply_name, status FROM gld_transmission_substation
                                        WHERE ts_id = ANY(%(t)s::text[])""", {"t": ts_ids}, ttl=600)} if ts_ids else {}
    main = ts.get(ts_ids[0]) if ts_ids else None
    bsp = pq("SELECT bsp_id, name, kv FROM gld_bulk_supply WHERE bsp_id = %(b)s", {"b": main["bsp_id"]}, ttl=600) if main else []
    ds, nearest = None, False
    if app.get("nmi"):
        ds = pq("""SELECT d.ds_id, d.kva, d.customers, d.status FROM gld_nmi n JOIN gld_ds_state d ON d.ds_id = n.ds_id WHERE n.nmi = %(n)s""",
                {"n": app["nmi"]}, ttl=600)
    if not ds and app.get("lat") is not None:
        ds, nearest = pq("""SELECT ds_id, kva, customers, status FROM gld_ds_state WHERE feeder_id = %(f)s
                            ORDER BY (lat - %(y)s) ^ 2 + (lon - %(x)s) ^ 2 LIMIT 1""", {"f": app["feeder_id"], "y": app["lat"], "x": app["lon"]}, ttl=600), True
    ds = ds[0] if ds else None
    others = [ts[t]["name"] for t in ts_ids[1:] if t in ts]
    node = lambda kind, label, name, detail=None, status=None, ref=None: dict(kind=kind, label=label, name=name, detail=detail, status=status, ref=ref)
    chain = [
        node("bulk_supply", "Bulk supply point", bsp[0]["name"] if bsp else (main or {}).get("bulk_supply_name") or "Not yet in the network data.",
             f"{bsp[0]['kv']} kV" if bsp and bsp[0].get("kv") else None),
        node("transmission", "Transmission substation", main["name"] if main else "Not yet in the network data.",
             ", ".join(x for x in [f"{main['kv']}/{main['secondary_kv']} kV" if main and main.get("kv") else None,
                                   f"also supplied from {' and '.join(others)}" if others else None] if x) or None,
             (main or {}).get("status"), (main or {}).get("ts_id")),
        node("zone", "Zone substation", f["zs_name"], "Single supply circuit" if zs and zs[0].get("single_fed") else None, None, f["zs_id"]),
        node("feeder", "Feeder", f["feeder_name"] or f["feeder_id"], f"{int(f['customers']):,} customers" if f.get("customers") else None, f.get("status"), f["feeder_id"]),
        node("distribution", "Distribution substation", ds["ds_id"] if ds else "Not yet assigned",
             (f"{round(float(ds['kva'])):,} kVA" + (", the nearest on the feeder (likely connection point)" if nearest else "")) if ds else None,
             (ds or {}).get("status"), (ds or {}).get("ds_id")),
        node("premises", "Premises", app["address"] if app.get("suburb") and app["suburb"] in (app.get("address") or "") else f"{app['address']}, {app['suburb']}",
             f"NMI {app['nmi']}" if app.get("nmi") else "NMI not yet issued"),   # a register address already carries its suburb
    ]
    return chain


def application(app_id):
    """One application's journey: every stage with the dates it was entered and left, the payment, the provider, the blocker and where it
    is stuck, plus how the premises is supplied, from the bulk supply point down."""
    s = _s()
    row = pq("""SELECT application_id, applicant_name, applicant_kind_label, address, suburb, postcode, lat, lon, nmi, zs_id, feeder_id,
                       connection_type, connection_type_label, load_kva, export_kw, battery_kwh, lots, phases, stage, stage_label, stage_no,
                       is_open, stage_entered_date + %(s)s AS stage_entered_date, days_in_stage, applied_date + %(s)s AS applied_date,
                       days_since_applied, target_date + %(s)s AS target_date, days_past_target, payment_status, payment_label, asp,
                       blocker, blocker_label, stuck, stuck_reason
                FROM gld_connection_application WHERE application_id = %(a)s""", {"s": s, "a": app_id}, ttl=60)
    if not row:
        raise ValueError(f"No such connection application: {app_id}.")
    app = row[0]
    journey = pq("""SELECT stage, stage_label, stage_no, stage_seq, entered_date + %(s)s AS entered_date, exited_date + %(s)s AS exited_date,
                           days_in_stage
                    FROM gld_connection_stage_event WHERE application_id = %(a)s ORDER BY stage_seq""", {"s": s, "a": app_id}, ttl=60)
    have = {j["stage"] for j in journey}
    steps = []
    for code in STAGE_ORDER:
        j = next((x for x in journey if x["stage"] == code), None)
        steps.append(dict(stage=code, stage_label=N.label("connection_stage", code), reached=code in have,
                          current=code == app["stage"], entered_date=j["entered_date"] if j else None, exited_date=j["exited_date"] if j else None,
                          days=j["days_in_stage"] if j else None))
    app["supply_chain"] = _supply_chain(app)
    return dict(application=app, journey=steps, as_of=AS_OF.isoformat(), today=N.today().isoformat())


# ------------------------------------------------------------------ Existing customers
def _live_devices():
    """Devices with an active outage now, and the customers they have off supply."""
    return {r["device_id"]: r for r in pq("""SELECT device_id, outage_id, feeder_id, customers_off, life_support_off, etr, stages
                                              FROM atlas.live_outage WHERE status = 'active' AND device_id IS NOT NULL""", ttl=0)}


def _off_supply(live=None):
    """Which premises are off supply now, and by which live outage. A device that trips takes off every premises behind it (the
    connectivity model, gld_isolation_ds). When automatic restoration (FLISR) then opens a switch to isolate the faulted section and
    recloses, the premises up to that switch come back: only those behind the switch stay off. So the premises listed agree with the
    customers each outage has off."""
    live = _live_devices() if live is None else live
    out = {}
    for dev, o in live.items():
        isolating = [st.get("device") for st in (o.get("stages") or []) if "RESTORE" in str(st.get("action", "")) and st.get("device")]
        rows = pq("""SELECT DISTINCT n.nmi FROM gld_nmi n JOIN gld_isolation_ds i ON i.ds_id = n.ds_id WHERE i.device_id = %(d)s""",
                  {"d": isolating[-1] if isolating else dev}, ttl=30)
        for r in rows:
            out.setdefault(r["nmi"], o)
    return out


def existing_summary():
    """The customer base at a glance: counts of customers, life support, critical sites, DER, the notification preferences, and how many
    are off supply now."""
    if _cache["k"] is None or dt.datetime.now().timestamp() - _cache["t"] > 600:
        k = pq("""SELECT count(*) AS customers, count(*) FILTER (WHERE life_support) AS life_support,
                         count(*) FILTER (WHERE critical_site IS NOT NULL) AS critical_sites,
                         count(*) FILTER (WHERE solar_kw > 0 OR has_battery = 1 OR has_ev_charger = 1) AS der,
                         count(*) FILTER (WHERE solar_kw > 0) AS solar, count(*) FILTER (WHERE has_battery = 1) AS batteries,
                         count(*) FILTER (WHERE has_ev_charger = 1) AS ev_chargers,
                         count(*) FILTER (WHERE sms_opt_in) AS sms, count(*) FILTER (WHERE email_opt_in) AS email,
                         count(*) FILTER (WHERE facebook_opt_in) AS app_or_web,
                         count(*) FILTER (WHERE NOT (sms_opt_in OR email_opt_in OR facebook_opt_in)) AS no_digital,
                         count(DISTINCT feeder_id) AS feeders, count(DISTINCT zs_id) AS zone_substations
                  FROM gld_nmi""")[0]
        _cache.update(t=dt.datetime.now().timestamp(), k=k)
    k = dict(_cache["k"])
    live = _live_devices()
    k["off_supply_now"] = len(_off_supply(live))   # the premises off now, as the list shows them
    k["outages_now"] = len(live)
    k["life_support_off"] = _sum_known(live.values(), "life_support_off")
    return dict(kpi=k, as_of=AS_OF.isoformat(), today=N.today().isoformat())


# Each figure on the Existing customers tab is a set of premises, broken into cohorts: the set's condition, and the column that names
# each premises' cohort (for the premises off supply now, the outage, worked out in code)
DER_KIND = """CASE WHEN n.solar_kw > 0 AND n.has_battery = 1 AND n.has_ev_charger = 1 THEN 'solar_battery_ev'
                   WHEN n.solar_kw > 0 AND n.has_battery = 1 THEN 'solar_battery' WHEN n.solar_kw > 0 AND n.has_ev_charger = 1 THEN 'solar_ev'
                   WHEN n.solar_kw > 0 THEN 'solar' WHEN n.has_battery = 1 AND n.has_ev_charger = 1 THEN 'battery_ev'
                   WHEN n.has_battery = 1 THEN 'battery' WHEN n.has_ev_charger = 1 THEN 'ev' END"""
DER_LABEL = {"solar": "Solar only", "solar_battery": "Solar and a battery", "solar_ev": "Solar and an EV charger",
             "solar_battery_ev": "Solar, a battery and an EV charger", "battery": "Battery only", "ev": "EV charger only",
             "battery_ev": "Battery and an EV charger"}
PREFERRED_LABEL = {"Sms": "Prefers SMS", "Email": "Prefers email, also reachable by SMS", "Facebook": "Prefers the app or web, also reachable by SMS",
                   "Mail": "Prefers mail, also reachable by SMS"}
FIGURES = {
    "all": ("true", "n.customer_type"),
    "life_support": ("n.life_support", "n.life_support_status"),
    "critical": ("n.critical_site IS NOT NULL", "n.critical_site"),
    "der": ("(n.solar_kw > 0 OR n.has_battery = 1 OR n.has_ev_charger = 1)", DER_KIND),
    "sms": ("n.sms_opt_in", "n.preferred_channel"),
    "off_supply": ("n.nmi = ANY(%(off)s::text[])", None),
}


def _cohort_label(figure, key, row=None):
    if figure == "all":
        return (row or {}).get("customer_type_label") or key
    if figure == "life_support":
        return N.label("life_support_status", key)
    if figure == "critical":
        return N.label("critical_site", key)
    if figure == "der":
        return DER_LABEL.get(key, key)
    return PREFERRED_LABEL.get(key, key)


def existing_cohorts(figure):
    """The cohorts of one figure, largest first: each with its key, its label and how many premises are in it (they add up to the figure)."""
    if figure not in FIGURES:
        raise ValueError(f"There is no figure called {figure}.")
    if figure == "off_supply":
        live = _live_devices()
        off, names = _off_supply(live), {}
        by = {}
        for o in off.values():
            by[o["outage_id"]] = by.get(o["outage_id"], 0) + 1
        feeders = {r["feeder_id"]: r["feeder_name"] for r in pq("SELECT feeder_id, feeder_name FROM gld_rm_feeder", ttl=600)}
        for o in live.values():
            names[o["outage_id"]] = f"{feeders.get(o['feeder_id'], o['feeder_id'])}, behind {o['device_id']}"
        return dict(figure=figure, cohorts=sorted([dict(key=k, label=names.get(k, k), n=v) for k, v in by.items()], key=lambda x: -x["n"]))
    cond, col = FIGURES[figure]
    rows = pq(f"""SELECT {col} AS k, min(n.customer_type_label) AS customer_type_label, count(*) AS n FROM gld_nmi n WHERE {cond}
                  GROUP BY 1 ORDER BY 3 DESC""", ttl=600)
    return dict(figure=figure, cohorts=[dict(key=r["k"], label=_cohort_label(figure, r["k"], r), n=int(r["n"])) for r in rows if r["k"] is not None])


def existing_search(q="", flt="all", limit=60, cohort=None):
    """Premises in one figure (all, life support, critical sites, DER, reachable by SMS, off supply now) and, when given, one of its
    cohorts, optionally matching a search (NMI, address or suburb), with supply status now. Life support and critical premises come
    first. The total is every premises that matches; the rows are the first ones."""
    if flt not in FIGURES:
        flt = "all"
    qq = (q or "").strip()
    live = _live_devices()
    off = _off_supply(live)
    cond, col = FIGURES[flt]
    if cohort and flt == "off_supply":
        off_ids = [k for k, o in off.items() if o["outage_id"] == cohort]
        sub = "true"
    else:
        off_ids = list(off)
        sub = f"{col} = %(c)s" if cohort and col else "true"
    where = f"""WHERE (%(q)s::text IS NULL OR n.nmi = %(q)s OR n.address ILIKE %(qq)s OR n.suburb ILIKE %(qq)s) AND {cond} AND {sub}"""
    args = {"q": qq or None, "qq": f"%{qq}%" if qq else None, "off": off_ids, "c": cohort, "l": int(limit)}
    rows = pq(f"""SELECT nmi, address, suburb, postcode, feeder_id, zs_id, ds_id, protective_device, customer_type, customer_type_label,
                         life_support, life_support_status, critical_site, site_name, solar_kw, has_battery, has_ev_charger, sms_opt_in,
                         email_opt_in, facebook_opt_in, preferred_channel
                  FROM gld_nmi n {where}
                  ORDER BY (n.life_support OR n.critical_site IS NOT NULL) DESC, n.nmi
                  LIMIT %(l)s""", args)
    total = pq(f"SELECT count(*) AS n FROM gld_nmi n {where}", args)[0]["n"]
    out = []
    for r in rows:
        o = off.get(r["nmi"])
        out.append(dict(r, off_supply=bool(o), outage_id=o["outage_id"] if o else None, channels=_channel_text(r),
                        life_support_label=N.label("life_support_status", r.get("life_support_status") or "")))
    return dict(rows=out, shown=len(out), total=int(total), filter=flt, cohort=cohort, query=qq)


def _channel_text(n):
    """The channels a customer is registered for, in words, with the preferred one first."""
    route = N.route(n)
    names = [N.label("channel", c) for c in route["channels"]]
    return dict(primary=route["primary"], primary_label=N.label("channel", route["primary"]), channels=names, why=route["why"])


# ------------------------------------------------------------------ customer calls, checked against the network
# A call reports a problem; the network data says whether it stands. No power or partial power is verified when the premises is off supply
# in a live outage, or its meter reported a loss of supply after its last restore; resolved when the meter has since reported supply back;
# otherwise not verified. Wires down is a hazard a crew must attend whatever the data says. Flickering lights is consistent with the network
# when its feeder has an outage or switching in the hour; otherwise not verified.
CALL_CHECK = {"verified": "Verified", "resolved": "Resolved", "not_verified": "Not verified", "attend": "Crew to attend"}


def meter_last(nmis):
    """Each premises' latest power-off or power-restore alarm in the live stream (recent hours): {nmi: (code, ts)}."""
    rows = pq("""SELECT DISTINCT ON (nmi) nmi, event_code, ts FROM live.meter_event WHERE nmi = ANY(%(n)s::text[])
                 AND event_code IN ('POWER_OUTAGE', 'POWER_RESTORE') ORDER BY nmi, ts DESC""", {"n": list(nmis)}, ttl=0) if nmis else []
    return {r["nmi"]: (r["event_code"], r["ts"]) for r in rows}


def check_call(reason, nmi, feeder_id, off, meters, busy_feeders):
    """(status, note) for one call: off is _off_supply(), meters is meter_last(), busy_feeders the feeders with an outage or switching now."""
    o, m = off.get(str(nmi)), meters.get(str(nmi))
    r = (reason or "").lower()
    if r in ("no power", "partial power"):
        if o:
            return "verified", f"Off supply in outage {o['outage_id']}."
        if m and m[0] == "POWER_OUTAGE":
            return "verified", "The meter reported a loss of supply."
        if m and m[0] == "POWER_RESTORE":
            return "resolved", "The meter has since reported supply back."
        return "not_verified", "The network shows the premises on supply."
    if r == "wires down":
        where = f"; the premises is off supply in outage {o['outage_id']}" if o else ""
        return "attend", f"A crew must make the site safe{where}."
    if feeder_id in busy_feeders:
        return "verified", "Consistent with an outage or switching on the feeder."
    return "not_verified", "No network event on the feeder explains it."


def busy_feeders():
    """Feeders with an active outage, or switching reported in the last hour."""
    rows = pq("""SELECT feeder_id FROM atlas.live_outage WHERE status = 'active'
                 UNION SELECT feeder_id FROM live.scada WHERE attribute = 'POS' AND ts > now() - interval '60 minutes'""", ttl=0)
    return {r["feeder_id"] for r in rows if r["feeder_id"]}


def existing_customer(nmi):
    """One premises in full: the register, supply now, how it is supplied from the bulk supply point down, the protective device's outages
    in the last year, the calls from it (checked against the network), the notices sent to it, and how it would be reached in the next notice."""
    rows = pq("""SELECT n.*, f.feeder_name FROM gld_nmi n LEFT JOIN gld_rm_feeder f ON f.feeder_id = n.feeder_id WHERE n.nmi = %(n)s""",
              {"n": str(nmi)}, ttl=60)
    if not rows:
        raise ValueError(f"No such premises: {nmi}.")
    n = rows[0]
    dev = _off_supply().get(str(nmi))   # the live outage the premises is off supply in, if any
    outages = pq("""SELECT outage_id, start_time, end_time, cause_label, duration_min, customers_interrupted, planned, momentary, device_label
                    FROM gld_outage WHERE device_id = %(d)s AND start_time >= (%(a)s::date - 365) ORDER BY start_time DESC LIMIT 25""",
                 {"d": n.get("protective_device") or "", "a": AS_OF.isoformat()}, ttl=300) if n.get("protective_device") else []
    notices = pq("""SELECT o.msg_id, o.created_at, o.audience, o.channel, o.status, o.sent_at, o.rec_id, o.situation_id, o.body,
                           coalesce(nt.title, s.title, CASE WHEN o.situation_id LIKE 'AREA-%%' THEN 'Message to customers in a map area' END,
                                    'Customer message') AS notice_title,
                           nt.notice_type, nt.window_start, nt.window_end, nt.description AS notice_description
                    FROM atlas.notification_outbox o
                    LEFT JOIN atlas.notice nt ON nt.notice_id = o.rec_id
                    LEFT JOIN atlas.situation s ON s.situation_id = o.situation_id
                    WHERE o.nmi = %(n)s ORDER BY o.created_at DESC LIMIT 12""", {"n": str(nmi)}, ttl=0)
    for x in notices:
        x["audience_label"] = N.label("audience", x["audience"])
        x["channel_label"] = N.label("channel", x["channel"])
        x["notice_type_label"] = N.label("notice_type", x["notice_type"]) if x.get("notice_type") else None
        x["status_label"] = N.label("outbox_status", x["status"])
        x["window_label"] = N.window_label(N._as_dt(x["window_start"]), N._as_dt(x["window_end"])) if x.get("window_start") else None
        x["created_at"] = N._iso(N._as_dt(x["created_at"]))
        x["sent_at"] = N._iso(N._as_dt(x["sent_at"])) if x.get("sent_at") else None
        x.pop("window_start", None); x.pop("window_end", None)
    off = _off_supply()
    meters, busy = meter_last([str(nmi)]), busy_feeders()
    calls = pq("SELECT call_id, ts, reason, channel FROM live.call WHERE nmi = %(n)s ORDER BY ts DESC LIMIT 20", {"n": str(nmi)}, ttl=0)
    for c in calls:   # the calls from this premises in the live stream (recent hours), each checked against the network now
        c["check"], c["check_note"] = check_call(c["reason"], nmi, n.get("feeder_id"), off, meters, busy)
        c["check_label"] = CALL_CHECK[c["check"]]
    return dict(premises=n, supply=dict(off_supply=bool(dev), outage_id=dev["outage_id"] if dev else None,
                                        etr=dev["etr"] if dev else None, customers_off=dev["customers_off"] if dev else None),
                outages=outages, notices=notices, calls=calls, supply_chain=_supply_chain(n),
                route=_channel_text(n), life_support_label=N.label("life_support_status", n.get("life_support_status") or ""))
