"""Customer notices: the notification engine behind Customers, tab "Customer notices" (a wizard to design a notice, and the notices
released). The wizard: choose what to tell customers about, choose the channels and draft with AI, then review and release.

1. Identify the customers. Planned work: the activities of the released works plans, each a planned outage at its isolation point (the
   customers of the distribution substations behind that device, gold gld_isolation_ds). A fault: the customers behind the tripped device.
   A weather hazard: the premises inside the forecast cells with gusts at or above the threshold. A custom notice: the whole feeders the
   planner chooses.
2. Apply the business rules. Four business days' notice before a planned interruption; only the customers the isolation affects; no
   second message about the same feeder within 24 hours (fault and weather messages are never held back); life support customers always
   get a contact centre call; SMS and calls wait until 8 am after 8 pm; each customer is reached on the channels the planner chose.
3. Personalise by preference. The customer's registered preferred channel when it is one of those chosen, else another chosen digital
   channel they opted into, else a letter when mail is chosen.
4. Orchestrate. Opus drafts each audience's text for the chosen channels and recommends the mix with a reason. A person edits and releases
   the notice: sent now, or scheduled for its notice date. Nothing is written to the outbox until it goes.
A works plan's release can also prepare and schedule the notice of each planned outage at once (prepare_plan_notice), with standard text.

Records: atlas.notice and atlas.notice_exclusion (the decision trail), atlas.notification_outbox (the messages; rec_id is the notice id),
app.notice_run (the drafting job's working state). Reads: synced gold gld_nmi, gld_isolation_ds, gld_weather_cell, gld_rm_feeder, the
released plans (atlas.planning_release_item), the app's atlas.live_outage, and ref_label.
"""
import datetime as dt
import json
import os
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import core
from core import AS_OF, pg, pq

SYD = ZoneInfo("Australia/Sydney")
PLAN_MODEL = os.environ.get("ATLAS_LLM_PLAN", "databricks-claude-opus-5-5")   # the planning model; Opus by default
NOTICE_DAYS = 4          # business days' notice before a planned interruption
DUP_HOURS = 24           # no second message about the same feeder within this many hours (planned and custom notices)
GUST_KMH = 70            # weather hazard threshold: forecast gusts at or above this, km/h
WEATHER_HOURS = 96       # forecast horizon for the weather notice: four days (the storm lead time the design guideline targets is 72 hours)
QUIET_FROM, QUIET_TO = 20, 8   # Sydney hours: no SMS or contact centre call from 8 pm until 8 am
SCHEDULED_HOUR = 9       # a scheduled planned-work notice goes at 9 am on its notice date
RUN_STALE_MIN = 10       # a drafting run still "running" after this long lost its thread (the app restarted)

PREFERENCE = {"Sms": "sms", "Email": "email", "Facebook": "app", "Mail": "mail"}   # gld_nmi.preferred_channel to a channel
CHANNELS = ("sms", "email", "app", "mail")                          # the channels a planner can choose
BASE_CHANNELS = {"customer": ("sms", "email", "app", "mail"), "life_support": ("sms", "email", "mail", "contact_centre")}
NMI_COLS = ("n.nmi, n.feeder_id, n.zs_id, n.ds_id, n.protective_device, n.suburb, n.life_support, n.sms_opt_in, n.email_opt_in, "
            "n.facebook_opt_in, n.preferred_channel, n.customer_type, n.critical_site")

_labels = {"t": 0.0, "rows": {}}
_lab_lock = threading.Lock()


# ------------------------------------------------------------------ time, labels and business days
def now_syd():
    return dt.datetime.now(SYD)


def today():
    return now_syd().date()


def shift():
    """Gold is dated at the twin's as-of day; the screen shows today. Gold dates plus this many days are today's dates."""
    return (today() - AS_OF).days


def add_business_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def earliest_interruption():
    """The first day a planned interruption may be notified for: four business days after today (public holidays not counted)."""
    return add_business_days(today(), NOTICE_DAYS)


def in_quiet_hours(t=None):
    t = t or now_syd()
    return t.hour >= QUIET_FROM or t.hour < QUIET_TO


def next_send_time(t=None):
    """Now, or the next 8 am Sydney when it is quiet hours (SMS and contact centre calls wait for it)."""
    t = t or now_syd()
    if not in_quiet_hours(t):
        return t
    d = t.date() if t.hour < QUIET_TO else t.date() + dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(QUIET_TO), tzinfo=SYD)


def _hm(t):
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def window_label(start, end=None):
    """"Wed 11 Nov 6:00am to 4:00pm", the way the phone preview shows it."""
    if not start:
        return ""
    day = f"{start.strftime('%a')} {start.day} {start.strftime('%b')}"
    return f"{day} {_hm(start)}" + (f" to {_hm(end)}" if end else "")


def _as_dt(v):
    """A timestamp from the database (a datetime, or ISO text) as Sydney time."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.astimezone(SYD)
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(SYD)


def _iso(t):
    return t.isoformat() if t else None


def label(domain, code):
    """A label from ref_label (cached five minutes); the code itself while the row is not there yet."""
    with _lab_lock:
        if time.time() - _labels["t"] > 300 or not _labels["rows"]:
            rows = {(r["domain"], r["code"]): r["label"] for r in pq("SELECT domain, code, label FROM ref_label")}   # a few hundred rows
            _labels.update(t=time.time(), rows=rows)
        return _labels["rows"].get((domain, code)) or code or ""




def _feeder_names(feeders):
    if not feeders:
        return {}
    return {r["feeder_id"]: r["feeder_name"] for r in pq("SELECT feeder_id, feeder_name FROM gld_rm_feeder WHERE feeder_id = ANY(%(f)s::text[])",
                                                         {"f": list(feeders)}, ttl=600)}




def draft_channels(channels):
    """The texts a notice needs: for each audience, the chosen channels it can be reached on (life support always adds the call)."""
    ch = [c for c in CHANNELS if c in (channels or CHANNELS)]
    return {"customer": [c for c in BASE_CHANNELS["customer"] if c in ch],
            "life_support": [c for c in BASE_CHANNELS["life_support"] if c in ch or c == "contact_centre"]}


# ------------------------------------------------------------------ faults and weather
def fault_candidates():
    rows = pq("""SELECT outage_id, feeder_id, device_id, start_time, etr, customers_off, life_support_off
                 FROM atlas.live_outage WHERE status = 'active' ORDER BY customers_off DESC NULLS LAST""")
    names = _feeder_names([o["feeder_id"] for o in rows if o["feeder_id"]])
    out = []
    for o in rows:
        st, etr = _as_dt(o["start_time"]), _as_dt(o["etr"])
        out.append(dict(id=f"fault|{o['outage_id']}", kind="network_fault", feeder_id=o["feeder_id"],
                        feeder_name=names.get(o["feeder_id"]) or o["feeder_id"], outage_id=o["outage_id"], device_id=o["device_id"],
                        customers_off=int(o["customers_off"] or 0), life_support_off=int(o["life_support_off"] or 0),
                        window_start=_iso(st), window_end=_iso(etr), window_label=window_label(st, etr), eligible=True, blocked=None,
                        job_types=[], jobs=0))
    return out


def _weather_cells():
    return pq("""SELECT h3, min(forecast_time) AS f0, max(forecast_time) AS f1, max(gust_kmh) AS gust_kmh, max(rain_mm) AS rain_mm, max(geo) AS geo
                 FROM gld_weather_cell WHERE forecast_time >= now() - interval '1 hour'
                   AND forecast_time < now() + make_interval(hours => %(h)s) AND gust_kmh >= %(g)s
                 GROUP BY h3 ORDER BY max(gust_kmh) DESC""", {"h": WEATHER_HOURS, "g": GUST_KMH}, ttl=300)


def _wkt_from_geojson(geo):
    """A GeoJSON polygon (the gold cell boundary) as WKT."""
    poly = json.loads(geo)
    ring = poly["coordinates"][0] if poly.get("type") == "Polygon" else poly["coordinates"][0][0]
    return "POLYGON((" + ", ".join(f"{x} {y}" for x, y in ring) + "))"


def weather_candidate():
    cells = _weather_cells()
    if not cells:
        return None
    start = min(_as_dt(c["f0"]) for c in cells)
    end = max(_as_dt(c["f1"]) for c in cells) + dt.timedelta(hours=1)
    return dict(id="weather|forecast", kind="severe_weather", cells=[dict(h3=c["h3"], gust_kmh=float(c["gust_kmh"]), rain_mm=float(c["rain_mm"] or 0),
                                                                      wkt=_wkt_from_geojson(c["geo"])) for c in cells],
                cell_count=len(cells), max_gust_kmh=float(cells[0]["gust_kmh"]), window_start=_iso(start), window_end=_iso(end),
                window_label=window_label(start, end), eligible=True, blocked=None, job_types=[], jobs=0)


# ------------------------------------------------------------------ planned work: the activities of the released works plans
PLAN_COLS = """i.release_id, i.bundle_id, i.run_id, i.feeder_id, i.feeder_name, i.zs_name, i.isolation_point, i.isolation_label, i.work_types,
               i.work_items, i.outage_date, i.outage_start, i.outage_end, i.notice_by, i.customers, i.life_support, i.notice_id,
               r.title AS plan_title, n.status AS notice_status, n.send_at AS notice_send_at"""


def _plan_item(i):
    """One planned-work candidate: a released plan's planned outage, with its window, customers and whether the notice period is met."""
    start, end = _as_dt(i["outage_start"]), _as_dt(i["outage_end"])
    nb = i["notice_by"] if isinstance(i["notice_by"], dt.date) else dt.date.fromisoformat(str(i["notice_by"]))
    ok = today() <= nb
    return dict(id=f"plan|{i['release_id']}|{i['bundle_id']}", kind="planned_work", feeder_id=i["feeder_id"], feeder_name=i["feeder_name"],
                plan_title=i["plan_title"], release_id=i["release_id"], bundle_id=i["bundle_id"], run_id=i["run_id"],
                isolation_point=i["isolation_point"], isolation=i["isolation_label"], works=i["work_types"], jobs=int(i["work_items"] or 0),
                customers=int(i["customers"] or 0), life_support=int(i["life_support"] or 0), date=str(i["outage_date"]),
                date_label=start.strftime("%a %-d %b") if start else "", window_start=_iso(start), window_end=_iso(end),
                window_label=window_label(start, end), notice_by=nb.isoformat(), eligible=ok,
                blocked=None if ok else f"The notice period has passed. Customers had to be notified by {nb.strftime('%a %-d %b')}.",
                notice=dict(id=i["notice_id"], status=i["notice_status"], label=label("notice_status", i["notice_status"])) if i["notice_id"] and i["notice_status"] else None)


def planned_candidates():
    rows = pg.q(f"""SELECT {PLAN_COLS} FROM atlas.planning_release_item i JOIN atlas.planning_release r ON r.release_id = i.release_id
                    LEFT JOIN atlas.notice n ON n.notice_id = i.notice_id
                    WHERE i.outage_date >= %s ORDER BY i.outage_date, i.feeder_name, i.bundle_id""", (today(),))
    return [_plan_item(i) for i in rows]


def _plan_candidate(release_id, bundle_id):
    r = pg.q(f"""SELECT {PLAN_COLS} FROM atlas.planning_release_item i JOIN atlas.planning_release r ON r.release_id = i.release_id
                 LEFT JOIN atlas.notice n ON n.notice_id = i.notice_id WHERE i.release_id = %s AND i.bundle_id = %s""", (release_id, bundle_id), one=True)
    if not r:
        raise ValueError("That planned outage is no longer in a released plan.")
    return _plan_item(r)


def _isolation(feeder_id, device):
    """The scope of an outage: the customers of the distribution substations behind the device (gld_isolation_ds), or the whole feeder
    when there is no device or it is the feeder breaker."""
    if not device or device.startswith("FEEDER-"):
        return dict(feeder_id=feeder_id, mode="feeder", key="", zs_id="")
    return dict(feeder_id=feeder_id, mode="iso", key=device, zs_id="")


def _planned_plan(c, plan_ids):
    """The plan of a planned-work notice: one planned outage of a works plan (released, or about to be)."""
    return dict(notice_type="planned_work", trigger_kind="plan", trigger_ref=f"{plan_ids.get('release_id') or plan_ids['run_id']}|{c['bundle_id']}",
                title="Planned power interruption", description=f"{c['works']} on {c['feeder_name']}", feeders=[c["feeder_id"]], area_code=None,
                scope=dict(isolations=[_isolation(c["feeder_id"], c["isolation_point"])], **plan_ids),
                window_start=dt.datetime.fromisoformat(c["window_start"]), window_end=dt.datetime.fromisoformat(c["window_end"]),
                eligible=c["eligible"], blocked=c["blocked"], job_types=[dict(label=c["works"], n=c["jobs"])], job_count=c["jobs"],
                feeder_names={c["feeder_id"]: c["feeder_name"]}, notice_by=c["notice_by"], plan=plan_ids)


def candidates(kind):
    """The candidates the planner can act on: planned_work (released plan activities), network_fault or severe_weather."""
    if kind == "network_fault":
        return dict(kind=kind, items=fault_candidates(), today=today().isoformat())
    if kind == "severe_weather":
        w = weather_candidate()
        return dict(kind=kind, items=[dict(w, cells=None)] if w else [], today=today().isoformat())
    return dict(kind="planned_work", items=planned_candidates(), today=today().isoformat(), earliest=earliest_interruption().isoformat(),
                notice_days=NOTICE_DAYS)


# ------------------------------------------------------------------ the plan of a notice: its trigger, scope and window
def _plan_from_candidate(cid):
    kind, _, rest = cid.partition("|")
    if kind == "plan":
        release_id, _, bundle_id = rest.partition("|")
        c = _plan_candidate(release_id, bundle_id)
        return _planned_plan(c, dict(run_id=c["run_id"], release_id=release_id, bundle_id=bundle_id))
    if kind == "fault":
        c = next((x for x in fault_candidates() if x["id"] == cid), None)
        if not c:
            raise ValueError("That outage is no longer active.")
        iso = _isolation(c["feeder_id"], c["device_id"])   # everyone behind the tripped device, through the downstream fuses too
        return dict(notice_type="network_fault", trigger_kind="fault", trigger_ref=c["outage_id"], title="Network fault in your area",
                    description=f"Supply interrupted on feeder {c['feeder_name']}", feeders=[c["feeder_id"]], area_code=None,
                    scope=dict(isolations=[iso], outage_id=c["outage_id"], device_id=c["device_id"]),
                    window_start=dt.datetime.fromisoformat(c["window_start"]) if c["window_start"] else now_syd(),
                    window_end=dt.datetime.fromisoformat(c["window_end"]) if c["window_end"] else None, eligible=True, blocked=None,
                    job_types=[], job_count=0, feeder_names={c["feeder_id"]: c["feeder_name"]})
    if kind == "weather":
        w = weather_candidate()
        if not w:
            raise ValueError("No forecast cells are at or above the gust threshold any more.")
        return dict(notice_type="severe_weather", trigger_kind="weather", trigger_ref=f"gust>={GUST_KMH}kmh next {WEATHER_HOURS}h",
                    title="Weather hazard warning for your area", description=f"Forecast gusts up to {w['max_gust_kmh']:.0f} km/h",
                    feeders=[], area_code=None, scope=dict(cells=w["cells"]),
                    window_start=dt.datetime.fromisoformat(w["window_start"]), window_end=dt.datetime.fromisoformat(w["window_end"]),
                    eligible=True, blocked=None, job_types=[], job_count=0, feeder_names={})
    raise ValueError(f"Unknown candidate: {cid}.")


def _plan_custom(body):
    """A custom notice: the planner describes the work and chooses the feeders it interrupts."""
    feeders = sorted({str(f) for f in (body.get("feeders") or []) if f})
    if not feeders:
        raise ValueError("Choose at least one feeder for the notice.")
    start = dt.datetime.fromisoformat(body["start"]).replace(tzinfo=SYD)
    end = dt.datetime.fromisoformat(body["end"]).replace(tzinfo=SYD)
    if end <= start:
        raise ValueError("The window must end after it starts.")
    gate = earliest_interruption()
    if start.date() < gate:
        raise ValueError(f"A planned interruption needs four business days' notice: the earliest is {gate.strftime('%a %-d %b')}.")
    return dict(notice_type="project_work", trigger_kind="project", trigger_ref=((body.get("project_code") or "CUSTOM")[:40] + "|" + start.date().isoformat()),
                title=(body.get("title") or "").strip() or "Planned work in your area", description=(body.get("description") or "").strip(),
                feeders=feeders, area_code=None, scope=dict(isolations=[dict(feeder_id=f, mode="feeder", key="", zs_id="") for f in feeders]),
                window_start=start, window_end=end, eligible=True, blocked=None, job_types=[], job_count=0, feeder_names=_feeder_names(feeders))


def _plan_of(n):
    """The plan of an existing notice, rebuilt from its record (for the release and the customer list)."""
    return dict(notice_type=n["notice_type"], trigger_kind=n["trigger_kind"], feeders=list(n.get("feeders") or []), scope=n.get("scope") or {},
                job_types=(n.get("counts") or {}).get("job_types") or [], job_count=(n.get("counts") or {}).get("jobs") or 0)


# ------------------------------------------------------------------ who the plan reaches, and the rules
def _scope_rows(plan):
    """The customers the scope covers: inside the forecast cells for weather, else behind the isolation points and on the feeders."""
    sc = plan["scope"] or {}
    if plan["notice_type"] == "severe_weather":
        wkts = [c["wkt"] for c in sc.get("cells") or []]
        if not wkts:
            return []
        # planar cover on the lon and lat degrees (the cells are a few kilometres across): several times faster than geography here
        # the union is one aggregate row (an ARRAY sub-select in the select list was re-evaluated for every premises: 35 s instead of 0.2 s)
        return pq(f"""WITH cells AS (SELECT ST_SetSRID(ST_GeomFromText(w), 4326) AS g FROM unnest(%(w)s::text[]) w),
                           area AS (SELECT ST_Union(array_agg(g)) AS g FROM cells)
                      SELECT {NMI_COLS} FROM gld_nmi n CROSS JOIN area a
                      WHERE ST_Covers(a.g, ST_SetSRID(ST_MakePoint(n.lon, n.lat), 4326))""", {"w": wkts})
    iso = sc.get("isolations") or []
    if not iso:
        return []
    return pq(f"""SELECT DISTINCT ON (n.nmi) {NMI_COLS} FROM gld_nmi n
                  JOIN unnest(%(f)s::text[], %(m)s::text[], %(k)s::text[], %(z)s::text[]) AS i(feeder_id, mode, key, zs_id)
                    ON (i.mode = 'zone' AND n.zs_id = i.zs_id)
                    OR (n.feeder_id = i.feeder_id AND (i.mode = 'feeder' OR (i.mode = 'device' AND n.protective_device = i.key)
                                                       OR (i.mode = 'ds' AND n.ds_id = i.key)
                                                       OR (i.mode = 'iso' AND n.ds_id IN (SELECT d.ds_id FROM gld_isolation_ds d WHERE d.device_id = i.key))))
                  ORDER BY n.nmi""", {"f": [i["feeder_id"] for i in iso], "m": [i["mode"] for i in iso],
                                      "k": [i["key"] for i in iso], "z": [i.get("zs_id") or "" for i in iso]})


def _feeder_customers(feeders):
    if not feeders:
        return []
    return pq(f"SELECT {NMI_COLS} FROM gld_nmi n WHERE n.feeder_id = ANY(%(f)s::text[])", {"f": list(feeders)})


def _recent_messaged(nmis, feeders, exclude_id):
    """Customers already messaged about these feeders within the duplicate window, by a notice other than this one."""
    if not nmis or not feeders:
        return set()
    rows = pq("""SELECT DISTINCT o.nmi FROM atlas.notification_outbox o JOIN atlas.notice n ON n.notice_id = o.rec_id
                 WHERE o.created_at > now() - make_interval(hours => %(h)s) AND o.status IN ('queued', 'sent', 'delivered')
                   AND n.notice_id <> %(x)s AND n.feeders && %(f)s::text[] AND o.nmi = ANY(%(n)s::text[])""",
              {"h": DUP_HOURS, "x": exclude_id or "", "f": list(feeders), "n": list(nmis)})
    return {r["nmi"] for r in rows}


def route(n, channels=CHANNELS):
    """The channel a customer is reached on, from the preference on their record (synced gold) and the channels chosen: the preferred
    channel when it is chosen (a letter for a customer whose preference is mail), else another chosen digital channel they opted into,
    else a letter when mail is chosen. A life support customer also gets a contact centre call. primary is None when no chosen channel
    reaches the customer."""
    digital = [c for c, on in (("sms", n["sms_opt_in"]), ("email", n["email_opt_in"]), ("app", n["facebook_opt_in"])) if on and c in channels]
    pref = PREFERENCE.get(n.get("preferred_channel") or "")
    if pref in digital or (pref == "mail" and "mail" in channels):
        primary, why = pref, "Preferred channel on record."
    elif digital:
        primary, why = digital[0], "Another chosen channel the customer opted into."
    elif "mail" in channels:
        primary, why = "mail", "No chosen digital channel registered: a letter."
    else:
        primary, why = None, "Not reachable on the channels chosen."
    audience = "life_support" if n["life_support"] else "customer"
    chans = ([primary] if primary else []) + (["contact_centre"] if audience == "life_support" else [])
    return dict(audience=audience, primary=primary, channels=chans, why=why)


def evaluate(plan, exclude_id=None, now=None, channels=CHANNELS):
    """Identify, apply the rules and personalise: the customers reached, the customers left out and why, the rules applied with how many
    each one changed, and the messages each chosen channel would carry. Writes nothing."""
    now = now or now_syd()
    ntype = plan["notice_type"]
    channels = [c for c in CHANNELS if c in (channels or CHANNELS)]
    in_scope = _scope_rows(plan)
    in_nmis = {r["nmi"] for r in in_scope}
    excluded, rules = [], []
    # rule: notice period (the candidate or custom window was checked when the plan was made)
    rules.append(dict(code="notice_period", label=label("notice_rule", "notice_period"),
                      detail=(f"Customers must be notified by {dt.date.fromisoformat(plan['notice_by']).strftime('%a %-d %b')}." if plan.get("notice_by")
                              else f"The earliest interruption is on {earliest_interruption().strftime('%a %-d %b')}.") if ntype in ("planned_work", "project_work")
                      else "Not needed: a fault or weather hazard is notified at once.", changed=0))
    # rule: only the customers the isolation affects; customers on the same feeders behind another device are left out
    outside = 0
    if ntype in ("planned_work", "network_fault"):
        for r in _feeder_customers(plan["feeders"]):
            if r["nmi"] not in in_nmis:
                outside += 1
                excluded.append(dict(nmi=r["nmi"], audience="life_support" if r["life_support"] else "customer", reason="outside_isolation",
                                     feeder_id=r["feeder_id"]))
    rules.append(dict(code="isolation_scope", label=label("notice_rule", "isolation_scope"),
                      detail="Only customers behind the isolation point or the tripped device are notified.", changed=outside))
    # rule: no duplicate within 24 hours (planned and custom notices); fault and weather messages are never held back; life support never excluded
    dup, keep = 0, []
    if ntype in ("planned_work", "project_work"):
        already = _recent_messaged([r["nmi"] for r in in_scope], plan["feeders"], exclude_id)
        for r in in_scope:
            if r["nmi"] in already and not r["life_support"]:
                dup += 1
                excluded.append(dict(nmi=r["nmi"], audience="customer", reason="duplicate_24h", feeder_id=r["feeder_id"]))
            else:
                keep.append(r)
    else:
        keep = list(in_scope)
    rules.append(dict(code="duplicate_24h", label=label("notice_rule", "duplicate_24h"),
                      detail=f"Customers messaged about these feeders in the last {DUP_HOURS} hours are left out, except life support customers.", changed=dup))
    rules.append(dict(code="safety_messages", label=label("notice_rule", "safety_messages"),
                      detail="Fault and weather hazard messages are never held back by the 24-hour rule", changed=0))
    # rules: personalise by preference among the chosen channels; life support first with a contact centre call; quiet hours
    messages, by_chan, ls_count, mail, unreached, reached = [], {}, 0, 0, 0, []
    for r in keep:
        rt = route(r, channels)
        if not rt["channels"]:
            unreached += 1
            excluded.append(dict(nmi=r["nmi"], audience=rt["audience"], reason="no_chosen_channel", feeder_id=r["feeder_id"]))
            continue
        reached.append(r)
        ls_count += 1 if rt["audience"] == "life_support" else 0
        mail += 1 if rt["primary"] == "mail" else 0
        for ch in rt["channels"]:
            messages.append(dict(nmi=r["nmi"], audience=rt["audience"], channel=ch, feeder_id=r["feeder_id"]))
            by_chan.setdefault(rt["audience"], {}).setdefault(ch, 0)
            by_chan[rt["audience"]][ch] += 1
    rules.append(dict(code="life_support_first", label=label("notice_rule", "life_support_first"),
                      detail="Life support customers are messaged first and get a contact centre call", changed=ls_count))
    quiet = in_quiet_hours(now)
    held = sum(v for aud in by_chan.values() for ch, v in aud.items() if ch in ("sms", "contact_centre")) if quiet else 0
    rules.append(dict(code="quiet_hours", label=label("notice_rule", "quiet_hours"),
                      detail=(f"Quiet hours now: SMS and calls go at {next_send_time(now).strftime('%-I:%M %p').lower()}." if quiet
                              else "Outside quiet hours: SMS and calls go at once."), changed=held))
    rules.append(dict(code="channel_preference", label=label("notice_rule", "channel_preference"),
                      detail="The preferred channel on the customer's record, if it is chosen; otherwise another chosen digital channel they registered for; otherwise a letter if Mail is chosen.",
                      changed=mail + unreached))
    suburbs = {}
    for r in reached:
        if r.get("suburb"):
            suburbs[r["suburb"]] = suburbs.get(r["suburb"], 0) + 1
    feeders = sorted({r["feeder_id"] for r in reached if r.get("feeder_id")})
    counts = dict(in_scope=len(in_scope), reached=len(reached), life_support=ls_count,
                  excluded={"outside_isolation": outside, "duplicate_24h": dup, "no_chosen_channel": unreached},
                  messages={a: dict(c) for a, c in by_chan.items()}, messages_total=len(messages), mail=mail, quiet_hours=quiet,
                  suburbs=[dict(suburb=s, customers=n) for s, n in sorted(suburbs.items(), key=lambda x: -x[1])[:12]], feeders=feeders,
                  job_types=plan.get("job_types") or [], jobs=plan.get("job_count") or 0, channels=channels)
    return dict(messages=messages, excluded=excluded, rules=rules, counts=counts, feeders=feeders)


def _rule_list(rules):
    return [dict(code=r["code"], label=r["label"], detail=r["detail"], changed=r["changed"]) for r in rules]


def _exclusion_summary(rows):
    """Excluded customers grouped by reason (with labels), and a sample of the first 200 for the screen."""
    by = {}
    for x in rows:
        by[x["reason"]] = by.get(x["reason"], 0) + 1
    return dict(total=len(rows), by_reason=[dict(reason=k, label=label("exclusion_reason", k), n=v) for k, v in by.items()],
                sample=[dict(nmi=x["nmi"], audience=x["audience"], reason=x["reason"], label=label("exclusion_reason", x["reason"]),
                             feeder_id=x.get("feeder_id")) for x in rows[:200]])


# ------------------------------------------------------------------ GenAI: the AI drafts the text (each customer's channel is the preference on their record)
DRAFT_SYSTEM = """You write customer notices for an electricity distributor. Never name the company. Plain, calm English that a customer with no
technical background understands. Be specific about the time, the area and what the customer should do, and say when power will return when
that is known. Never use the long dash characters (em or en dash); write with full stops, commas and parentheses instead.
Reply with one JSON object and nothing else, with this key:
"drafts": an object with "customer" and "life_support", each holding exactly the channel keys you are asked to write for, with the text.
Text rules: sms under 300 characters, starting with the headline and the window; email starts with "Subject: " on its own line, then the
body; app is a short card of a few lines with a heading and one line on what to do; mail is a letter with a greeting and a sign-off from the
network team; contact_centre is a short script for an agent calling a life support customer, offering help to arrange backup supply and to
say when to call back. Do not promise a time outside the planned window."""


def _plan_complete(messages, max_tokens):
    """One call to the planning model (Opus by default). The client holds a token that expires: rebuild it once and retry."""
    kw = dict(model=PLAN_MODEL, messages=messages, max_tokens=max_tokens)
    try:
        return core.llm_client().chat.completions.create(**kw)
    except Exception as e:  # noqa: BLE001
        print(f"planning model call failed ({type(e).__name__}: {str(e)[:160]}); rebuilding the client", flush=True)
        core._llm = None
        return core.llm_client().chat.completions.create(**kw)


def _dashes(s):
    """No em or en dash reaches a draft: a hyphen stands in for one."""
    return s.replace("\u2014", "-").replace("\u2013", "-") if isinstance(s, str) else s


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return _dashes(x)


def _parse_json(text):
    """The first JSON object in the reply; anything the model writes after it is ignored."""
    i = (text or "").find("{")
    if i < 0:
        raise ValueError("The model did not return a JSON object.")
    return json.JSONDecoder().raw_decode(text[i:])[0]


def _draft_prompt(n, note=""):
    """What the model is told: the work, the window, the areas, the counts by audience and channel, and the channels to write for. No
    customer identifiers, no addresses, nothing beyond what the notice needs."""
    c = n.get("counts") or {}
    want = draft_channels(n.get("channels"))
    facts = dict(notice_type=label("notice_type", n["notice_type"]), title=n["title"], description=n.get("description") or "",
                 window=window_label(_as_dt(n["window_start"]), _as_dt(n.get("window_end"))),
                 suburbs=[s["suburb"] for s in c.get("suburbs", [])][:10], customers_reached=c.get("reached"),
                 life_support_customers=c.get("life_support"), messages_by_audience_and_channel=c.get("messages"),
                 planned_work=[f"{j['n']} {j['label']}" if j.get("n") else j.get("label") for j in c.get("job_types", [])],
                 write_for=want, channel_rule="Each customer gets the preferred channel on their record when it is chosen, else another chosen channel.")
    return json.dumps(facts, default=str) + (f"\n\nThe planner asks for this in the redraft: {note[:600]}" if note else "")


def _run_set(nid, status, error=None):
    if status == "running":
        pg.q("""INSERT INTO app.notice_run (notice_id, status, model, started_at, finished_at, error) VALUES (%s, 'running', %s, now(), NULL, NULL)
                ON CONFLICT (notice_id) DO UPDATE SET status = 'running', model = EXCLUDED.model, started_at = now(), finished_at = NULL,
                error = NULL""", (nid, PLAN_MODEL))
    else:
        pg.q("UPDATE app.notice_run SET status = %s, finished_at = now(), error = %s WHERE notice_id = %s", (status, error, nid))


def _draft_worker(nid, note):
    """Background: the AI writes the drafts for the chosen channels, stored on the notice."""
    try:
        n = _notice_row(nid)
        want = draft_channels(n.get("channels"))
        for attempt in range(2):   # a reply that cannot be read, or that leaves out a text, gets one more try
            try:
                r = _plan_complete([{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": _draft_prompt(n, note)}], max_tokens=9000)
                drafts = _clean(_parse_json(core.text_of(r.choices[0].message.content))).get("drafts") or {}
                missing = [f"the {aud} text for {ch}" for aud, chans in want.items() for ch in chans if not (drafts.get(aud) or {}).get(ch)]
                if missing:
                    raise ValueError(f"The model left out {', '.join(missing)}.")
                break
            except ValueError as e:   # JSONDecodeError is a ValueError
                if attempt:
                    raise
                print(f"drafting {nid}: {e}; asking the model again", flush=True)
        drafts = {aud: {ch: drafts[aud][ch] for ch in chans} for aud, chans in want.items()}
        pg.q("""UPDATE atlas.notice SET ai_drafts = %s::jsonb, drafts = %s::jsonb, model = %s WHERE notice_id = %s AND status <> 'sent'""",
             (json.dumps(drafts), json.dumps(drafts), getattr(r, "model", None) or PLAN_MODEL, nid))
        _run_set(nid, "done")
    except Exception as e:  # noqa: BLE001 - the notice still works without drafts; the planner can redraft
        print(f"drafting {nid} failed: {type(e).__name__}: {str(e)[:300]}", flush=True)
        _run_set(nid, "failed", f"{type(e).__name__}: {str(e)[:300]}")


def start_drafting(nid, note=""):
    """Start Opus on a notice in the background. Refused while a run is fresh, so two runs never race."""
    cur = pq("SELECT status, started_at FROM app.notice_run WHERE notice_id = %(n)s", {"n": nid})
    if cur and cur[0]["status"] == "running" and (now_syd() - _as_dt(cur[0]["started_at"])).total_seconds() < RUN_STALE_MIN * 60:
        return False
    _run_set(nid, "running")
    threading.Thread(target=_draft_worker, args=(nid, note), daemon=True).start()
    return True


# ------------------------------------------------------------------ the notice record
def _notice_row(nid):
    r = pg.q("SELECT * FROM atlas.notice WHERE notice_id = %s", (nid,), one=True)
    if not r:
        raise ValueError(f"No such notice: {nid}.")
    return r


def _write_exclusions(nid, excluded):
    """The customers a notice left out and why (replaces any earlier list for this notice)."""
    pg.q("DELETE FROM atlas.notice_exclusion WHERE notice_id = %s", (nid,))
    rows = [dict(nmi=x["nmi"], audience=x["audience"], reason=x["reason"]) for x in excluded]
    for i in range(0, len(rows), 2000):
        pg.q("""INSERT INTO atlas.notice_exclusion (notice_id, nmi, audience, reason)
                SELECT %s, x ->> 'nmi', x ->> 'audience', x ->> 'reason' FROM jsonb_array_elements(%s::jsonb) x
                ON CONFLICT (notice_id, nmi) DO NOTHING""", (nid, json.dumps(rows[i:i + 2000])))


def _chosen(channels):
    ch = [c for c in CHANNELS if c in (channels or [])]
    if not ch:
        raise ValueError("Choose at least one channel.")
    return ch


def _insert(plan, ev, channels, actor, status="draft", drafts=None, send_at=None):
    """Write the notice record and its exclusions; returns the notice id."""
    nid = "NTC-" + uuid.uuid4().hex[:8].upper()
    p = plan.get("plan") or {}
    pg.q("""INSERT INTO atlas.notice (notice_id, notice_type, trigger_kind, trigger_ref, title, description, area_code, feeders, suburbs, scope,
                                     window_start, window_end, status, rules, counts, created_by, channels, plan_run_id, plan_bundle_id, release_id,
                                     drafts, ai_drafts, send_at, released_by, released_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::text[], %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s)""",
         (nid, plan["notice_type"], plan["trigger_kind"], plan["trigger_ref"], plan["title"], plan["description"], plan["area_code"],
          ev["feeders"], [s["suburb"] for s in ev["counts"]["suburbs"]], json.dumps(plan["scope"], default=str), plan["window_start"],
          plan["window_end"], status, json.dumps(_rule_list(ev["rules"])), json.dumps(ev["counts"], default=str), actor, channels,
          p.get("run_id"), p.get("bundle_id"), p.get("release_id"), json.dumps(drafts) if drafts else None, json.dumps(drafts) if drafts else None,
          send_at, actor if status == "scheduled" else None, now_syd() if status == "scheduled" else None))
    _write_exclusions(nid, ev["excluded"])
    if p.get("release_id"):
        pg.q("UPDATE atlas.planning_release_item SET notice_id = %s WHERE release_id = %s AND bundle_id = %s", (nid, p["release_id"], p["bundle_id"]))
    core.audit(actor, "notice_create", nid, dict(type=plan["notice_type"], reached=ev["counts"]["reached"], excluded=ev["counts"]["excluded"],
                                                 channels=channels, status=status))
    return nid


def create_notice(body, actor):
    """Create a draft notice from a candidate (a released plan's planned outage, a fault, a weather hazard) or a custom notice the planner
    describes, reaching customers on the chosen channels. The plan is evaluated, the customers left out are recorded, and Opus starts
    drafting the chosen channels. Nothing is sent."""
    plan = _plan_custom(body) if body.get("notice_type") == "project_work" else _plan_from_candidate(body.get("candidate") or "")
    if not plan["eligible"]:
        raise ValueError(plan["blocked"] or "This work cannot be notified yet.")
    channels = _chosen(body.get("channels") or list(CHANNELS))
    nid = _insert(plan, evaluate(plan, channels=channels), channels, actor)
    start_drafting(nid)
    return detail(nid)


def _standard_drafts(plan, counts, channels):
    """The standard planned-outage text for every chosen channel (used when a works plan prepares its notices at once; the planner can
    redraft any of them with AI before it goes)."""
    win = window_label(plan["window_start"], plan["window_end"])
    where = ", ".join(s["suburb"] for s in counts.get("suburbs", [])[:3]) or "your area"
    what = "We need to switch off the power to work safely on the network"
    t = {
        "sms": f"Planned power interruption, {win}, in {where}. {what}. Please switch off sensitive equipment before we start.",
        "email": (f"Subject: Planned power interruption on {win}\n\nDear customer,\n\n{what} near you. Your power will be off on {win}. "
                  "Please switch off and unplug sensitive equipment before we start, and treat all wires as live. We will restore power as soon "
                  "as the work is done.\n\nThank you for your patience,\nThe network team"),
        "app": f"Planned power interruption\n{win}\nSwitch off sensitive equipment before we start.",
        "mail": (f"Dear customer,\n\nPlanned power interruption on {win}\n\n{what} in {where}. Your power will be off for this time. Please "
                 "switch off and unplug sensitive equipment before we start, and treat all wires as live.\n\nYours sincerely,\nThe network team"),
        "contact_centre": (f"Hello, I am calling from your electricity network about a planned power interruption on {win}. Our records show "
                           "someone at your address relies on life support equipment. Do you have a backup plan for that time? We can help you "
                           "arrange one. Please call us back if anything changes."),
    }
    return {aud: {ch: t[ch] for ch in chans} for aud, chans in draft_channels(channels).items()}


def _scheduled_send(notice_by):
    """When a planned-work notice goes: 9 am on its notice date, or now if that time has already passed."""
    t = dt.datetime.combine(dt.date.fromisoformat(notice_by), dt.time(SCHEDULED_HOUR), tzinfo=SYD)
    return max(t, now_syd())


def prepare_plan_notice(x, actor):
    """Prepare and schedule the notice of one planned outage of a works plan (an item of the release step): the customers behind its
    isolation point, every channel, the standard text, and the send time on its notice date. Returns the notice id."""
    start = dt.datetime.fromisoformat(x["window_start"])
    c = dict(bundle_id=x["bundle_id"], feeder_id=x["feeder_id"], feeder_name=x["feeder"], isolation_point=x["isolation_point"], works=x["works"],
             jobs=x["work_items"], window_start=x["window_start"], window_end=x["window_end"], notice_by=x["notice_by"],
             eligible=today() <= dt.date.fromisoformat(x["notice_by"]), blocked=None)
    plan = _planned_plan(c, dict(run_id=x["run_id"], release_id=x.get("release_id"), bundle_id=x["bundle_id"]))
    channels = list(CHANNELS)
    ev = evaluate(plan, channels=channels)
    send_at = _scheduled_send(x["notice_by"]) if start.date() > today() else now_syd()
    return _insert(plan, ev, channels, actor, status="scheduled", drafts=_standard_drafts(plan, ev["counts"], channels), send_at=send_at)


def delete_notices(nids):
    """Delete notices and everything that refers to them (no foreign keys are declared, so each reference is cleared here): their messages
    in the outbox (rec_id is the notice id), the customers taken off them, the AI drafting run, and the notice named on a works plan's
    selection or released item (left empty, so the plan shows the outage as having no notice). Returns how many notices went."""
    nids = list(nids or [])
    if not nids:
        return 0
    pg.q("DELETE FROM atlas.notification_outbox WHERE rec_id = ANY(%s)", (nids,))
    pg.q("DELETE FROM atlas.notice_exclusion WHERE notice_id = ANY(%s)", (nids,))
    pg.q("DELETE FROM app.notice_run WHERE notice_id = ANY(%s)", (nids,))
    pg.q("UPDATE atlas.planning_selection SET notice_id = NULL, updated_at = now() WHERE notice_id = ANY(%s)", (nids,))
    pg.q("UPDATE atlas.planning_release_item SET notice_id = NULL WHERE notice_id = ANY(%s)", (nids,))
    return len(pg.q("DELETE FROM atlas.notice WHERE notice_id = ANY(%s) RETURNING notice_id", (nids,)))


def delete_draft(nid, actor):
    """Delete a draft notice and everything about it. Only a draft: a released notice is scheduled for customers or is the record of what
    they were told."""
    n = _notice_row(nid)
    if n["status"] != "draft":
        raise ValueError("Only a draft can be deleted. This notice has been released.")
    delete_notices([nid])
    core.audit(actor, "notice_delete", nid, dict(title=n["title"], notice_type=n["notice_type"]))
    return dict(ok=True, notice_id=nid)


def save_drafts(nid, edits, actor):
    """The planner's edits: each replaces the text for one audience and channel, and each is logged with who and when."""
    n = _notice_row(nid)
    if n["status"] == "sent":
        raise ValueError("This notice has been sent; its messages can no longer be edited.")
    drafts = dict(n.get("drafts") or {})
    want = draft_channels(n.get("channels"))
    log = list(n.get("edits") or [])
    for e in edits or []:
        aud, ch, text = e.get("audience"), e.get("channel"), _dashes((e.get("text") or "").strip())
        if ch not in want.get(aud, []) or not text:
            continue
        before = len((drafts.get(aud) or {}).get(ch) or "")
        drafts.setdefault(aud, {})[ch] = text
        log.append(dict(audience=aud, channel=ch, by=actor, at=now_syd().isoformat(), chars_before=before, chars_after=len(text)))
    pg.q("UPDATE atlas.notice SET drafts = %s::jsonb, edits = %s::jsonb WHERE notice_id = %s", (json.dumps(drafts), json.dumps(log), nid))
    return detail(nid)


def redraft(nid, actor, note=""):
    """Ask Opus for the drafts again, with the planner's note. The planner's edits are replaced (they stay in the edit log)."""
    n = _notice_row(nid)
    if n["status"] == "sent":
        raise ValueError("This notice has been sent; it can no longer be redrafted.")
    if not start_drafting(nid, note):
        raise ValueError("Drafting is already running for this notice.")
    core.audit(actor, "notice_redraft", nid, {"note": (note or "")[:200]})
    return detail(nid)


def release_notice(nid, when, actor):
    """Release the notice: sent now, or scheduled (planned work goes at 9 am on its notice date). Nothing reaches a customer before it goes."""
    n = _notice_row(nid)
    if n["status"] == "sent":
        raise ValueError("This notice has already been sent.")
    if not n.get("drafts"):
        raise ValueError("The drafts are not ready yet: wait for the AI to finish, or redraft.")
    if when == "scheduled":
        nb = _notice_by(n)
        if not nb:
            raise ValueError("Only a planned interruption can be scheduled; send this one now.")
        pg.q("""UPDATE atlas.notice SET status = 'scheduled', send_at = %s, released_by = %s, released_at = now() WHERE notice_id = %s""",
             (_scheduled_send(nb.isoformat()), actor, nid))
        core.audit(actor, "notice_schedule", nid, {})
        sweep()
        return detail(nid)
    pg.q("UPDATE atlas.notice SET released_by = %s, released_at = now() WHERE notice_id = %s", (actor, nid))
    return _send(nid, actor)


def _notice_by(n):
    """The last day customers of a planned interruption must be told (four business days before it starts); None for faults and weather."""
    if n["notice_type"] not in ("planned_work", "project_work") or not n.get("window_start"):
        return None
    d, k = _as_dt(n["window_start"]).date(), 0
    while k < NOTICE_DAYS:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            k += 1
    return d


def _send(nid, actor):
    """The notice goes: the customers are evaluated again (the 24 hour rule may have changed), the exclusions are updated, and the messages
    are written to the outbox with the text as it stands. In quiet hours the SMS and contact centre messages are queued until 8 am;
    everything else goes at once (delivery is simulated)."""
    n = _notice_row(nid)
    ev = evaluate(_plan_of(n), exclude_id=nid, channels=n.get("channels") or list(CHANNELS))
    now = now_syd()
    quiet = in_quiet_hours(now)
    send_at = next_send_time(now)
    drafts = n.get("drafts") or {}
    msgs = [dict(m=f"MSG-{uuid.uuid4().hex[:12]}", n=m["nmi"], a=m["audience"], c=m["channel"], b=_draft_text(drafts, m["audience"], m["channel"]),
                 s="queued" if (m["channel"] in ("sms", "contact_centre") and quiet) else "sent") for m in ev["messages"]]
    for i in range(0, len(msgs), 2000):
        pg.q("""INSERT INTO atlas.notification_outbox (msg_id, situation_id, rec_id, nmi, audience, channel, body, status, created_at, sent_at)
                SELECT x ->> 'm', NULL, %s, x ->> 'n', x ->> 'a', x ->> 'c', x ->> 'b', x ->> 's', now(),
                       CASE WHEN x ->> 's' = 'sent' THEN now() END
                FROM jsonb_array_elements(%s::jsonb) x""", (nid, json.dumps(msgs[i:i + 2000])))
    _write_exclusions(nid, ev["excluded"])
    counts = dict(ev["counts"], sent_at=now.isoformat(), held_until=send_at.isoformat() if quiet else None)
    pg.q("""UPDATE atlas.notice SET status = 'sent', sent_by = %s, sent_at = now(), send_at = %s, counts = %s::jsonb, rules = %s::jsonb,
            feeders = %s::text[], suburbs = %s::text[] WHERE notice_id = %s AND status <> 'sent'""",
         (actor, send_at, json.dumps(counts, default=str), json.dumps(_rule_list(ev["rules"])), ev["feeders"],
          [s["suburb"] for s in ev["counts"]["suburbs"]], nid))
    core.audit(actor, "notice_send", nid, dict(messages=len(msgs), reached=ev["counts"]["reached"], excluded=ev["counts"]["excluded"]))
    return detail(nid)


def _draft_text(drafts, aud, ch):
    """The text for an audience and channel, falling back to the customer's text for a channel the life support set does not carry."""
    d = drafts or {}
    return (d.get(aud) or {}).get(ch) or (d.get("customer") or {}).get(ch) or next(iter((d.get("customer") or {}).values()), "")


def sweep():
    """Scheduled notices whose time has come go out, and messages held for quiet hours go once their send time has passed (delivery is
    simulated, so they are marked sent)."""
    for r in pg.q("SELECT notice_id, released_by FROM atlas.notice WHERE status = 'scheduled' AND send_at <= now() ORDER BY send_at"):
        _send(r["notice_id"], r["released_by"] or "scheduler")
    pg.q("""UPDATE atlas.notification_outbox o SET status = 'sent', sent_at = now() FROM atlas.notice n
            WHERE n.notice_id = o.rec_id AND o.status = 'queued' AND n.status = 'sent' AND n.send_at <= now()""")


def _messages(nid):
    rows = pq("""SELECT audience, channel, count(*) AS messages, count(*) FILTER (WHERE status = 'sent') AS sent
                 FROM atlas.notification_outbox WHERE rec_id = %(n)s GROUP BY 1, 2 ORDER BY 1, 2""", {"n": nid})
    out = {}
    for r in rows:
        out.setdefault(r["audience"], {})[r["channel"]] = dict(messages=int(r["messages"]), sent=int(r["sent"]),
                                                               channel_label=label("channel", r["channel"]))
    return out


def _run(nid):
    r = pg.q("SELECT status, model, started_at, finished_at, error FROM app.notice_run WHERE notice_id = %s", (nid,), one=True)
    if r and r["status"] == "running" and (now_syd() - _as_dt(r["started_at"])).total_seconds() > RUN_STALE_MIN * 60:
        r = dict(r, status="failed", error="Drafting stopped before it finished (the app restarted). Redraft to try again.")
    return r


def _format(n):
    """A notice as the screen shows it: times as ISO and in words, codes as labels, and the drafts."""
    out = {k: v for k, v in n.items() if k not in ("drafts", "ai_drafts", "scope", "edits", "rules", "counts")}
    for k in ("window_start", "window_end", "send_at", "created_at", "reviewed_at", "sent_at", "released_at"):
        out[k] = _iso(_as_dt(n.get(k)))
    out["window_label"] = window_label(_as_dt(n.get("window_start")), _as_dt(n.get("window_end")))
    out["send_at_label"] = window_label(_as_dt(n.get("send_at"))) if n.get("send_at") else None
    out["notice_type_label"] = label("notice_type", n["notice_type"])
    out["trigger_label"] = label("notice_trigger", n["trigger_kind"])
    out["status_label"] = label("notice_status", n["status"])
    out["drafts"] = n.get("drafts") or {}
    out["edits"] = n.get("edits") or []
    out["channels"] = list(n.get("channels") or CHANNELS)
    out["draft_channels"] = draft_channels(out["channels"])
    nb = _notice_by(n)
    out["notice_by"] = nb.isoformat() if nb else None
    out["schedulable"] = bool(nb) and n["status"] != "sent"
    return out


def _ui_labels():
    """Channel and audience labels from ref_label, for the screen (the screen never shows a code)."""
    return dict(channel={c: label("channel", c) for c in ("sms", "email", "app", "mail", "contact_centre")},
                audience={a: label("audience", a) for a in BASE_CHANNELS})


def detail(nid):
    """One notice in full: the record, the AI run, the rules applied, the customers left out and why, the messages once sent."""
    sweep()
    n = _notice_row(nid)
    run = _run(nid)
    sample = pq("SELECT nmi, audience, reason FROM atlas.notice_exclusion WHERE notice_id = %(n)s ORDER BY reason, nmi LIMIT 200", {"n": nid})
    total = pq("SELECT reason, count(*) AS n FROM atlas.notice_exclusion WHERE notice_id = %(n)s GROUP BY 1", {"n": nid})
    excluded = _exclusion_summary([dict(x, feeder_id=None) for x in sample])
    excluded["total"] = sum(int(t["n"]) for t in total)
    excluded["by_reason"] = [dict(reason=t["reason"], label=label("exclusion_reason", t["reason"]), n=int(t["n"])) for t in total]
    drafts = n.get("drafts") or {}
    return dict(notice=_format(n),
                ai=dict(status=(run or {}).get("status") or ("done" if drafts else "none"), error=(run or {}).get("error"),
                        started_at=_iso(_as_dt((run or {}).get("started_at")))),
                rules=n.get("rules") or [], counts=n.get("counts") or {}, exclusions=excluded,
                messages=_messages(nid) if n["status"] == "sent" else None,
                feeders=n.get("feeders") or [], feeder_names=_feeder_names(n.get("feeders") or []), labels=_ui_labels())


def notice_customers(nid, limit=500):
    """The customers a notice reaches, with how each is reached: the messages written once it has gone, else the evaluation now. Each with
    the premises address, suburb, feeder, life support and the channels."""
    n = _notice_row(nid)
    if n["status"] == "sent":
        msgs = pq("SELECT nmi, audience, channel FROM atlas.notification_outbox WHERE rec_id = %(n)s", {"n": nid})
    else:
        msgs = evaluate(_plan_of(n), exclude_id=nid, channels=n.get("channels") or list(CHANNELS))["messages"]
    by = {}
    for m in msgs:
        x = by.setdefault(m["nmi"], dict(nmi=m["nmi"], audience=m["audience"], channels=[]))
        x["channels"].append(m["channel"])
    nmis = sorted(by)
    rows = {r["nmi"]: r for r in pq("""SELECT nmi, address, suburb, feeder_id, customer_type_label, life_support_equipment FROM gld_nmi
                                       WHERE nmi = ANY(%(n)s::text[])""", {"n": nmis[:limit]})} if nmis else {}
    names = _feeder_names({r["feeder_id"] for r in rows.values() if r["feeder_id"]})
    out = []
    for k in sorted(nmis[:limit], key=lambda k: (by[k]["audience"] != "life_support", (rows.get(k) or {}).get("suburb") or "", k)):
        r, x = rows.get(k) or {}, by[k]
        out.append(dict(nmi=k, address=r.get("address"), suburb=r.get("suburb"), feeder=names.get(r.get("feeder_id")) or r.get("feeder_id"),
                        customer_type=r.get("customer_type_label"), life_support=x["audience"] == "life_support",
                        equipment=r.get("life_support_equipment"), channels=[label("channel", c) for c in x["channels"]]))
    return dict(total=len(nmis), life_support=sum(1 for v in by.values() if v["audience"] == "life_support"), shown=len(out), customers=out)


def preview_candidate(cid, channels=None):
    """What a candidate would reach on the chosen channels, before anything is created: the same evaluation, nothing written. A planned
    outage that already has a notice names it, so the wizard opens that notice instead of making another."""
    plan = _plan_from_candidate(cid)
    ev = evaluate(plan, channels=channels or list(CHANNELS))
    rows = [dict(x, feeder_id=x.get("feeder_id")) for x in ev["excluded"]]
    existing = None
    if cid.startswith("plan|"):
        _, rel, bundle = cid.split("|")
        existing = (_plan_candidate(rel, bundle).get("notice") or {}).get("id")
    return dict(candidate=cid, notice_type=plan["notice_type"], notice_type_label=label("notice_type", plan["notice_type"]),
                title=plan["title"], description=plan["description"], feeders=ev["feeders"], feeder_names=plan.get("feeder_names") or {},
                window_label=window_label(plan["window_start"], plan["window_end"]), eligible=plan["eligible"], blocked=plan["blocked"],
                rules=_rule_list(ev["rules"]), counts=ev["counts"], exclusions=_exclusion_summary(rows), notice_id=existing, labels=_ui_labels())


def notices(status=None, limit=100):
    """Notices, newest first: status draft (being designed), released (scheduled or sent), or any."""
    sweep()
    states = {"draft": ["draft"], "released": ["scheduled", "sent"]}.get(status or "", ["draft", "scheduled", "sent", "in_review"])
    # the works plan a planned-outage notice belongs to: the released plan it went out with, or the programme still being designed
    rows = pq("""SELECT n.notice_id, n.notice_type, n.trigger_kind, n.title, n.description, n.feeders, n.status, n.window_start, n.window_end,
                        n.created_at, n.created_by, n.sent_at, n.sent_by, n.send_at, n.released_at, n.released_by, n.channels, n.counts,
                        rel.release_id AS plan_release_id, rel.title AS release_title, rel.released_at AS plan_released_at,
                        run.run_id AS plan_run, run.title AS run_title, run.status AS run_status,
                        n.release_id AS notice_release, n.plan_run_id AS notice_run
                 FROM atlas.notice n
                 LEFT JOIN atlas.planning_release rel ON rel.release_id = n.release_id
                 LEFT JOIN atlas.planning_run run ON run.run_id = n.plan_run_id
                 WHERE n.status = ANY(%(s)s::text[]) ORDER BY coalesce(n.released_at, n.created_at) DESC LIMIT %(l)s""",
              {"s": states, "l": int(limit)})
    names = _feeder_names({f for r in rows for f in (r["feeders"] or [])})
    out = []
    for r in rows:
        c = r.pop("counts") or {}
        r["plan"] = _works_plan(r)
        out.append(dict(r, notice_type_label=label("notice_type", r["notice_type"]), status_label=label("notice_status", r["status"]),
                        window_label=window_label(_as_dt(r["window_start"]), _as_dt(r["window_end"])),
                        send_at_label=window_label(_as_dt(r["send_at"])) if r["send_at"] else None,
                        feeder_label=", ".join(names.get(f, f) for f in (r["feeders"] or [])[:2]) + (" and more" if len(r["feeders"] or []) > 2 else ""),
                        channel_labels=[label("channel", ch) for ch in (r["channels"] or CHANNELS)],
                        reached=c.get("reached"), life_support=c.get("life_support"), excluded=(c.get("excluded") or {}),
                        **{k: _iso(_as_dt(r[k])) for k in ("window_start", "window_end", "created_at", "sent_at", "send_at", "released_at")}))
    return out


def _works_plan(r):
    """The works plan behind a notice, for the notices lists: its title, whether it is released or still being designed, and where it opens."""
    rel, run, released_at, run_status = r.pop("plan_release_id"), r.pop("plan_run"), r.pop("plan_released_at"), r.pop("run_status")
    rel_title, run_title = r.pop("release_title"), r.pop("run_title")
    linked = r.pop("notice_release") or r.pop("notice_run", None)
    r.pop("notice_run", None)
    title = rel_title or run_title
    if rel:
        return dict(title=title, state="Released", released_at=_iso(_as_dt(released_at)), release_id=rel)
    if run and run_status != "released":
        return dict(title=title, state="In design", run_id=run)
    if linked:   # it names a works plan that no longer exists: the plan was deleted, the notice (already sent) is kept as a record
        return dict(title=None, state="Removed")
    return None


def overview():
    """The figures across the top: notices being designed, scheduled to go, and sent this week with the customers they reached."""
    sweep()
    st = {r["status"]: int(r["n"]) for r in pq("SELECT status, count(*) AS n FROM atlas.notice GROUP BY status", ttl=0)}
    wk = pq("""SELECT count(*) AS notices, coalesce(sum((counts->>'reached')::int), 0) AS reached FROM atlas.notice
               WHERE status = 'sent' AND sent_at > now() - interval '7 days'""", ttl=0)[0]
    nxt = pq("SELECT min(send_at) AS t FROM atlas.notice WHERE status = 'scheduled'", ttl=0)[0]["t"]
    return dict(today=today().isoformat(), draft=st.get("draft", 0) + st.get("in_review", 0), scheduled=st.get("scheduled", 0),
                sent_week=int(wk["notices"] or 0), reached_week=int(wk["reached"] or 0),
                next_send=window_label(_as_dt(nxt)) if nxt else None, notice_days=NOTICE_DAYS)


def feeders_for_project():
    """The feeders a custom notice can cover, with customers and life support counts."""
    return pq("SELECT feeder_id, feeder_name, zs_id, region_id, customers, life_support FROM gld_rm_feeder ORDER BY feeder_id", ttl=600)
