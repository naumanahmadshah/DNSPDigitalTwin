"""Vegetation management (feature 6): the five steps as back-end logic.

1 capture and model: LiDAR survey coverage per campaign and zone, and the bay model (an overhead span between two poles).
2 identify and predict: current intrusions against predicted grow-in in 12 months, by bay and by feeder, bushfire-prone land.
3 create and issue work: the deterministic seasonal plan baseline and work packages of bays issued to a contractor crew.
4 perform and capture: the contractor works a bay (starts it, photographs it, notes exceptions, marks the cut complete), offline ops replay.
5 audit and validate: a post-cut capture is taken when a bay is completed; a reviewer accepts a compliant bay (closed) or reissues a non-
  compliant one to the contractor.

Gold marts are read from the synced Lakebase copy (gld_veg_*, gld_vegetation_risk, ml_span_risk, gld_crew, ref_label). The records
written here live in schema rm (serving/lakebase_vegetation.sql). Photos are not stored here: each photo goes through the asset photo
pipeline (app/assets.py, built by another worker) and only its id is linked to a bay task. Every write is audited (atlas.audit_log).
"""
import datetime as dt
import hashlib
import time
import uuid

import core
from core import audit, pg, pq

# Demo clearance standard (mirrors pipelines/vegetation.py): metres of clear space the span must keep from vegetation.
REQUIRED_M = {"bushfire_prone": 3.0, "general": 2.0}
# Days from today to the due date by risk tier when a planner does not set one.
DUE_DAYS = {"high_risk": 30, "cut_required": 60, "planned": 90}
# The cut aims this far beyond the requirement (metres); the simulated post-cut capture reflects it.
CUT_TARGET_MARGIN_M = 1.0
# Share of cuts (deterministic per bay and attempt) that the simulated post-cut capture finds short of the requirement.
UNDER_CUT_PERCENT = 12
EXCEPTION_KINDS = ("access_blocked", "partial_cut", "tree_too_large", "hazard", "landowner", "equipment", "other")
TIER_ORDER = {"high_risk": 1, "cut_required": 2, "planned": 3}
POST_CAPTURE_METHOD = "Simulated post-cut LiDAR capture (demo, fixed for each bay and attempt)"

# Labels (mirrors the VEG_LABELS list in pipelines/vegetation.py). The ref_label rows win when present; this is the fallback.
LABEL_FALLBACK = {
    ("veg_class", "current_intrusion"): "Current intrusion", ("veg_class", "predicted_grow_in"): "Predicted grow-in in 12 months",
    ("veg_class", "no_action"): "No action required",
    ("veg_tier", "high_risk"): "High risk", ("veg_tier", "cut_required"): "Cut required", ("veg_tier", "planned"): "Planned",
    ("veg_tier", "no_action"): "No action required",
    ("veg_basis", "bushfire_prone"): "Bushfire-prone land, 3 m", ("veg_basis", "general"): "General overhead span, 2 m",
    ("veg_task_status", "issued"): "Issued to contractor", ("veg_task_status", "in_progress"): "In progress",
    ("veg_task_status", "awaiting_audit"): "Cut complete, awaiting audit", ("veg_task_status", "closed"): "Closed, accepted",
    ("veg_audit_result", "pending"): "Awaiting decision", ("veg_audit_result", "compliant"): "Compliant, accepted and closed",
    ("veg_audit_result", "non_compliant"): "Non-compliant, reissued to contractor",
    ("veg_exception", "access_blocked"): "Access blocked", ("veg_exception", "partial_cut"): "Partial cut only",
    ("veg_exception", "tree_too_large"): "Tree too large for the crew", ("veg_exception", "hazard"): "Hazard found on site",
    ("veg_exception", "landowner"): "Landowner objection", ("veg_exception", "equipment"): "Equipment fault",
    ("veg_exception", "other"): "Other",
    ("veg_photo_kind", "before"): "Before the cut", ("veg_photo_kind", "after"): "After the cut",
    ("veg_photo_kind", "exception"): "Exception",
}
_labels = {"at": 0.0, "map": {}}


class NotFound(LookupError):
    """A bay, task, package or audit the request names does not exist (or is not the caller's)."""


# ------------------------------------------------------------------ helpers
def _today():
    return dt.date.today()


def _label_map():
    """Every vegetation label: the ref_label rows (cached a few minutes), over the fallback list."""
    if time.time() - _labels["at"] > 300:
        m = dict(LABEL_FALLBACK)
        try:
            for r in pq("SELECT domain, code, label FROM ref_label WHERE domain LIKE 'veg%' OR domain = 'photo_status'", ttl=0):
                m[(r["domain"], r["code"])] = r["label"]
        except Exception:  # noqa: BLE001 - labels fall back to this file's list
            pass
        _labels.update(at=time.time(), map=m)
    return _labels["map"]


def label(domain, code):
    """The words for a code. A code with no label anywhere shows as Unlabelled, never as the raw code."""
    if code is None:
        return None
    return _label_map().get((domain, str(code)), "Unlabelled")


def _with_labels(rows, pairs):
    """Add a *_label key for each (code key, domain) pair to every row (labels come from the reference table, not the screen)."""
    for r in rows:
        for key, domain in pairs:
            if key in r:
                r[f"{key}_label"] = label(domain, r[key])
    return rows


def _where(clauses_args):
    clauses, args = [], {}
    for sql_text, name, value in clauses_args:
        if value not in (None, ""):
            clauses.append(sql_text)
            args[name] = value
    return (" AND " + " AND ".join(clauses)) if clauses else "", args


def _tx(fn):
    """Run fn(cursor) in one transaction on a pooled Lakebase connection (all or nothing across the rows a step writes)."""
    if pg.pool is None:
        pg.q("SELECT 1")
    with pg.pool.connection() as c:
        with c.transaction():
            with c.cursor() as cur:
                return fn(cur)


def _cursor_rows(cur):
    if cur.description is None:
        return []
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, [core._coerce(v) for v in r])) for r in cur.fetchall()]


def _crew(crew_id):
    rows = pq("SELECT crew_id, crew_name, region_id, persona, employment_type FROM gld_crew WHERE crew_id = %(c)s", {"c": crew_id})
    if not rows:
        raise NotFound(f"Crew {crew_id} was not found.")
    return rows[0]


def _task_for_crew(task_id, crew_id):
    """The task, and checked against the crew that is acting on it (a contractor sees only its own bays)."""
    rows = pq("""SELECT t.*, p.title AS package_title, p.due_date AS package_due_date FROM rm.veg_bay_task t
                 JOIN rm.veg_work_package p ON p.package_id = t.package_id WHERE t.task_id = %(t)s""", {"t": task_id})
    if not rows:
        raise NotFound(f"Task {task_id} was not found.")
    if crew_id and rows[0]["crew_id"] != crew_id:
        raise NotFound(f"Task {task_id} is not issued to this crew.")
    return rows[0]


def _actor_label(actor, crew_name=None):
    return f"{crew_name} (contractor)" if crew_name else actor


# ------------------------------------------------------------------ photos of the bays
# A contractor's photo belongs to the bay task, and is reviewed with the cut in the post-cut audit (not in Asset management). Its bytes go to
# the shared photo store (media.photo_bytes, not fed to the lakehouse) and its record to rm.veg_task_photo. The checks on the file are the
# same as for every photo in the app (JPEG or PNG, at most 8 MB).
from assets import _decode as _decode_photo, _dims as _photo_dims, image as _photo_bytes   # noqa: E402 - the shared photo checks and store


def photo_url(photo_id):
    return f"/api/vegetation/photo/{photo_id}/image"


def photo_image(photo_id):
    """(bytes, mime) of a bay photo, or NotFound for any other photo id."""
    if not pq("SELECT 1 FROM rm.veg_task_photo WHERE photo_id = %(p)s", {"p": photo_id}):
        raise NotFound(f"Photo {photo_id} was not found.")
    got = _photo_bytes(photo_id)
    if not got:
        raise NotFound(f"The picture of photo {photo_id} is no longer held.")
    return got


# ------------------------------------------------------------------ step 1: capture and model
MODEL = {
    "bay": "A bay is one overhead span between two poles. Its ID is the span's asset ID, shown as BAY- followed by the span number.",
    "clearance": "Clearance is the shortest distance from vegetation to the conductor, measured by the annual LiDAR campaign.",
    "standard": "The demo standard requires 2 m of clearance on general overhead spans and 3 m on bushfire-prone land.",
    "grow_in": "Predicted clearance in 12 months is today's clearance minus the vegetation's measured growth a year.",
    "scope": "The point cloud itself is out of scope here: the app shows the clearance data each survey produced.",
}


def capture():
    """Survey coverage per campaign (the campaigns, overall) and per zone for the latest campaign, and the bay totals."""
    campaigns = pq("""SELECT campaign_year, sum(overhead_spans) AS overhead_spans, sum(surveyed_spans) AS surveyed_spans,
                             round(sum(surveyed_spans) * 100.0 / sum(overhead_spans), 1) AS coverage_pct,
                             sum(not_surveyed_spans) AS not_surveyed_spans, sum(intrusions) AS intrusions,
                             sum(spans_grown_in) AS spans_grown_in, sum(spans_cut_back) AS spans_cut_back,
                             round(avg(avg_clearance_m)::numeric, 2) AS avg_clearance_m,
                             min(capture_start) AS capture_start, max(capture_end) AS capture_end
                      FROM gld_veg_survey_coverage GROUP BY campaign_year ORDER BY campaign_year""", ttl=300)
    latest = campaigns[-1]["campaign_year"] if campaigns else None
    zones = pq("""SELECT zs_id, overhead_spans, surveyed_spans, not_surveyed_spans, coverage_pct, intrusions, spans_grown_in, spans_cut_back
                  FROM gld_veg_survey_coverage WHERE campaign_year = %(y)s ORDER BY intrusions DESC""", {"y": latest}, ttl=300) if latest else []
    tiers = pq("""SELECT risk_tier, count(*) AS bays, sum(CASE WHEN bushfire_prone THEN 1 ELSE 0 END) AS bushfire_bays
                  FROM gld_veg_bay GROUP BY risk_tier""", ttl=300)
    _with_labels(tiers, [("risk_tier", "veg_tier")])
    # the regions the cutting programme is planned and contracted by (each feeder's area), with the bushfire-prone split regulators ask for;
    # the coverage figures roll the zone results up by each zone substation's region
    regions = pq("""WITH area AS (SELECT DISTINCT zs_id, area, area_label FROM gld_feeder_state),
                         cov AS (SELECT a.area, sum(c.overhead_spans) AS overhead_spans, sum(c.surveyed_spans) AS surveyed_spans,
                                        sum(c.not_surveyed_spans) AS not_surveyed_spans, sum(c.spans_grown_in) AS spans_grown_in,
                                        sum(c.spans_cut_back) AS spans_cut_back
                                 FROM gld_veg_survey_coverage c JOIN area a USING (zs_id) WHERE c.campaign_year = %(y)s GROUP BY a.area),
                         bay AS (SELECT f.area, max(f.area_label) AS area_label, count(DISTINCT b.feeder_id) AS feeders,
                                        sum(CASE WHEN b.classification = 'current_intrusion' THEN 1 ELSE 0 END) AS intrusions,
                                        sum(CASE WHEN b.bushfire_prone THEN 1 ELSE 0 END) AS bushfire_bays,
                                        sum(CASE WHEN b.bushfire_prone AND b.classification = 'current_intrusion' THEN 1 ELSE 0 END) AS bushfire_intrusions,
                                        sum(CASE WHEN b.risk_tier = 'high_risk' THEN 1 ELSE 0 END) AS high_risk,
                                        sum(CASE WHEN b.classification = 'predicted_grow_in' THEN 1 ELSE 0 END) AS grow_in
                                 FROM gld_veg_bay b JOIN gld_feeder_state f USING (feeder_id) GROUP BY f.area)
                    SELECT bay.*, cov.overhead_spans, cov.surveyed_spans, cov.not_surveyed_spans, cov.spans_grown_in, cov.spans_cut_back,
                           round(cov.surveyed_spans * 100.0 / nullif(cov.overhead_spans, 0), 1) AS coverage_pct
                    FROM bay LEFT JOIN cov USING (area) ORDER BY bay.high_risk DESC, bay.intrusions DESC""", {"y": latest}, ttl=300) if latest else []
    return dict(campaigns=campaigns, zones=zones, regions=regions, latest_campaign=latest, tiers=tiers, model=MODEL,
                required_m=REQUIRED_M, as_of=core.AS_OF.isoformat())


# ------------------------------------------------------------------ step 2: identify and predict
def risk(limit=200):
    """The risk across the network for the region summary: the feeders, most urgent first, and the riskiest bays (for the map)."""
    feeders = pq("""SELECT feeder_id, zs_id, bays, current_intrusions, high_risk, cut_required, planned, bushfire_bays, hazard_trees,
                           customers_at_intrusion, max_risk_score, feeder_status
                    FROM gld_veg_feeder ORDER BY high_risk DESC, cut_required DESC, planned DESC, feeder_id LIMIT 80""", ttl=120)
    bays = pq("""SELECT bay_id, bay_label, feeder_id, zs_id, risk_tier, classification, clearance_m, clearance_12m_m,
                        required_clearance_m, clearance_gap_m, bushfire_prone, hazard_tree, dominant_species, growth_m_per_year,
                        customers_downstream, risk_score, lat, lon, required_basis
                 FROM gld_veg_bay WHERE classification <> 'no_action'
                 ORDER BY CASE risk_tier WHEN 'high_risk' THEN 1 WHEN 'cut_required' THEN 2 ELSE 3 END, risk_score DESC, bay_id
                 LIMIT %(n)s""", {"n": int(limit)}, ttl=120)
    _with_labels(bays, [("classification", "veg_class"), ("risk_tier", "veg_tier"), ("required_basis", "veg_basis")])
    return dict(feeders=feeders, bays=bays)


# ------------------------------------------------------------------ create and issue work (the package wizard)
def candidates(limit=300):
    """Bays in the baseline that no open task holds, in the baseline's order (what a planner can put in a package), each with its span length
    and its feeder's surveyed spans and the spans on it that need work."""
    rows = pq("""SELECT p.bay_id, p.bay_label, p.feeder_id, p.zs_id, p.risk_tier, p.plan_seq, p.plan_month_label,
                         b.clearance_m, b.clearance_12m_m, b.required_clearance_m, b.clearance_gap_m, b.bushfire_prone, b.hazard_tree,
                         b.customers_downstream, b.risk_score, b.lat, b.lon,
                         a.rating AS span_length_m, f.bays AS feeder_spans, f.high_risk + f.cut_required + f.planned AS feeder_spans_to_cut
                  FROM gld_veg_plan_baseline p JOIN gld_veg_bay b ON b.bay_id = p.bay_id
                  LEFT JOIN gld_asset a ON a.asset_id = p.bay_id
                  LEFT JOIN gld_veg_feeder f ON f.feeder_id = p.feeder_id
                  WHERE NOT EXISTS (SELECT 1 FROM rm.veg_bay_task t WHERE t.bay_id = p.bay_id AND t.status <> 'closed')
                  ORDER BY p.plan_seq LIMIT %(n)s""", {"n": int(limit)}, ttl=0)
    return _with_labels(rows, [("risk_tier", "veg_tier")])


PACKAGE_STATUS = {"not_started": "Not started", "in_progress": "In progress", "complete": "Complete"}


def packages():
    """Every released work package, newest first, with the status of its bays and its own status: not started (no bay begun),
    complete (every bay accepted and closed) or in progress."""
    rows = pq("""SELECT p.package_id, p.title, p.crew_id, p.crew_name, p.region_id, g.name AS region_name, g.area_name, p.zs_id, p.priority_tier,
                        p.due_date, p.bay_count, p.issued_by, p.issued_at, p.notes,
                        count(t.task_id) FILTER (WHERE t.status = 'issued') AS issued,
                        count(t.task_id) FILTER (WHERE t.status = 'in_progress') AS in_progress,
                        count(t.task_id) FILTER (WHERE t.status = 'awaiting_audit') AS awaiting_audit,
                        count(t.task_id) FILTER (WHERE t.status = 'closed') AS closed,
                        max(t.updated_at) AS last_activity
                 FROM rm.veg_work_package p LEFT JOIN rm.veg_bay_task t ON t.package_id = p.package_id
                 LEFT JOIN gld_rm_region g ON g.region_id = p.region_id
                 GROUP BY p.package_id, g.name, g.area_name ORDER BY p.issued_at DESC LIMIT 200""", ttl=0)
    for r in rows:
        total = r["issued"] + r["in_progress"] + r["awaiting_audit"] + r["closed"]
        r["status"] = "complete" if total and r["closed"] == total else "not_started" if r["issued"] == total else "in_progress"
        r["status_label"] = PACKAGE_STATUS[r["status"]]
    return _with_labels(rows, [("priority_tier", "veg_tier")])


def package_detail(package_id):
    """One released package: its record and every bay in it, with the bay's status, attempt and the latest post-cut result."""
    pk = [p for p in packages() if p["package_id"] == package_id]
    if not pk:
        raise NotFound(f"Package {package_id} was not found.")
    bays = pq("""SELECT t.task_id, t.bay_id, t.bay_label, t.feeder_id, t.status, t.attempt, t.risk_tier_at_issue, t.required_clearance_m,
                        t.clearance_before_m, t.started_at, t.completed_at, t.closed_at,
                        (SELECT a.post_cut_clearance_m FROM rm.veg_audit a WHERE a.task_id = t.task_id ORDER BY a.attempt DESC LIMIT 1) AS post_cut_clearance_m,
                        (SELECT count(*) FROM rm.veg_task_photo f WHERE f.task_id = t.task_id) AS photos,
                        (SELECT count(*) FROM rm.veg_exception e WHERE e.task_id = t.task_id) AS exceptions
                 FROM rm.veg_bay_task t WHERE t.package_id = %(p)s
                 ORDER BY CASE t.status WHEN 'in_progress' THEN 1 WHEN 'awaiting_audit' THEN 2 WHEN 'issued' THEN 3 ELSE 4 END, t.bay_label""",
              {"p": package_id}, ttl=0)
    _with_labels(bays, [("status", "veg_task_status"), ("risk_tier_at_issue", "veg_tier")])
    return dict(package=pk[0], bays=bays)


def contractor_workload():
    """Every vegetation contractor crew with the work it holds: open packages, bays still to do, bays cut and awaiting the audit, bays
    closed in the last 14 days, and the next due date. For the package wizard and for Field staff management."""
    rows = pq("""SELECT g.crew_id, g.crew_name, g.region_id, r.name AS region_name, r.area, r.area_name, g.crew_size, g.hourly_cost_aud,
                        count(DISTINCT t.package_id) FILTER (WHERE t.status <> 'closed') AS open_packages,
                        count(t.task_id) FILTER (WHERE t.status IN ('issued', 'in_progress')) AS to_do,
                        count(t.task_id) FILTER (WHERE t.status = 'in_progress') AS in_progress,
                        count(t.task_id) FILTER (WHERE t.status = 'awaiting_audit') AS awaiting_audit,
                        count(t.task_id) FILTER (WHERE t.status = 'closed' AND t.closed_at > now() - interval '14 days') AS closed_14d,
                        min(t.due_date) FILTER (WHERE t.status IN ('issued', 'in_progress')) AS next_due
                 FROM gld_crew g JOIN gld_rm_region r ON r.region_id = g.region_id
                 LEFT JOIN rm.veg_bay_task t ON t.crew_id = g.crew_id
                 WHERE g.persona = 'Vegetation Crew' AND g.employment_type = 'Contractor' AND g.active
                 GROUP BY g.crew_id, g.crew_name, g.region_id, r.name, r.area, r.area_name, g.crew_size, g.hourly_cost_aud
                 ORDER BY r.area_name, r.name, g.crew_id""", ttl=0)
    return rows


def crew_work(crew_id):
    """A vegetation contractor crew's packages and the bays in them that are still open, for its drawer in Field staff management."""
    crew = _crew(crew_id)
    pk = [p for p in packages() if p["crew_id"] == crew_id]
    return dict(crew=crew, packages=pk, bays=crew_bays(crew_id))


def create_package(body, actor):
    """Issue a package: the named bays to one contractor crew, each as a task. A bay carries one open task at a time."""
    crew_id = body.get("crew_id")
    bay_ids = list(dict.fromkeys(body.get("bay_ids") or []))
    if not crew_id:
        raise ValueError("Choose the contractor crew the package goes to.")
    if not bay_ids:
        raise ValueError("Choose at least one bay for the package.")
    if len(bay_ids) > 300:
        raise ValueError("A package takes at most 300 bays; split it into several packages.")
    crew = _crew(crew_id)
    if crew["persona"] != "Vegetation Crew":
        raise ValueError(f"{crew['crew_name']} is not a vegetation crew.")
    bays = pq("""SELECT bay_id, bay_label, feeder_id, zs_id, risk_tier, classification, clearance_m, required_clearance_m
                 FROM gld_veg_bay WHERE bay_id = ANY(%(b)s::text[])""", {"b": bay_ids})
    found = {b["bay_id"]: b for b in bays}
    missing = [b for b in bay_ids if b not in found]
    if missing:
        raise ValueError(f"These IDs are not bays in the survey: {', '.join(missing[:5])}.")
    no_work = [b["bay_label"] for b in bays if b["classification"] == "no_action"]
    if no_work:
        raise ValueError(f"No action is required on {', '.join(no_work[:5])}; only bays with an intrusion or a predicted grow-in can be issued.")
    held = pq("SELECT bay_id FROM rm.veg_bay_task WHERE bay_id = ANY(%(b)s::text[]) AND status <> 'closed'", {"b": bay_ids})
    if held:
        raise ValueError(f"These bays already have an open task: {', '.join(h['bay_id'] for h in held[:5])}.")
    today = _today()
    tiers = sorted((found[b]["risk_tier"] for b in bay_ids), key=lambda t: TIER_ORDER.get(t, 9))
    priority = tiers[0]
    due = body.get("due_date") or (today + dt.timedelta(days=DUE_DAYS.get(priority, 60))).isoformat()
    title = (body.get("title") or f"{crew['region_id']} {label('veg_tier', priority).lower()}, {len(bay_ids)} bay{'s' if len(bay_ids) != 1 else ''}").strip()
    package_id = f"VP-{today:%Y%m%d}-{uuid.uuid4().hex[:4]}"
    zs = found[bay_ids[0]]["zs_id"] if len({found[b]["zs_id"] for b in bay_ids}) == 1 else None

    def write(cur):
        cur.execute("""INSERT INTO rm.veg_work_package (package_id, title, crew_id, crew_name, region_id, zs_id, priority_tier, due_date,
                                                       bay_count, issued_by, notes)
                       VALUES (%(p)s, %(t)s, %(c)s, %(cn)s, %(r)s, %(z)s, %(pt)s, %(d)s, %(n)s, %(by)s, %(no)s)""",
                    dict(p=package_id, t=title, c=crew_id, cn=crew["crew_name"], r=crew["region_id"], z=zs, pt=priority, d=due,
                         n=len(bay_ids), by=actor, no=body.get("notes")))
        for b in bay_ids:
            bay = found[b]
            cur.execute("""INSERT INTO rm.veg_bay_task (task_id, package_id, bay_id, bay_label, feeder_id, zs_id, crew_id, status,
                                                      risk_tier_at_issue, required_clearance_m, clearance_before_m, attempt, due_date, issued_by)
                           VALUES (%(t)s, %(p)s, %(b)s, %(bl)s, %(f)s, %(z)s, %(c)s, 'issued', %(rt)s, %(rq)s, %(cb)s, 1, %(d)s, %(by)s)""",
                        dict(t=f"VT-{bay['bay_label'][4:]}-{uuid.uuid4().hex[:4]}", p=package_id, b=b, bl=bay["bay_label"], f=bay["feeder_id"],
                             z=bay["zs_id"], c=crew_id, rt=bay["risk_tier"], rq=bay["required_clearance_m"], cb=bay["clearance_m"],
                             d=due, by=actor))
    _tx(write)
    audit(actor, "veg_package_issued", package_id, dict(crew_id=crew_id, bays=len(bay_ids), priority=priority, due=due))
    return dict(package_id=package_id, title=title, bays=len(bay_ids), crew_name=crew["crew_name"], priority_tier=priority, due_date=due)


# ------------------------------------------------------------------ step 4: perform and capture (contractor side)
def contractor_crews():
    """The vegetation contractor crews (every vegetation crew is a contractor), with the region, sub-region and depot each works from."""
    return pq("""SELECT g.crew_id, g.crew_name, g.region_id, r.name AS region_name, r.area, r.area_name, g.crew_size, g.skills_label,
                        r.depot_name, r.lat AS depot_lat, r.lon AS depot_lon
                 FROM gld_crew g JOIN gld_rm_region r ON r.region_id = g.region_id
                 WHERE g.persona = 'Vegetation Crew' AND g.employment_type = 'Contractor' AND g.active
                 ORDER BY r.area_name, r.name, g.crew_id""", ttl=600)


def crew_bays(crew_id):
    """The crew's open bays (and those it closed in the last two weeks), most urgent first, with the bay's position and risk."""
    _crew(crew_id)
    rows = pq("""SELECT t.task_id, t.bay_id, t.bay_label, t.feeder_id, t.status, t.risk_tier_at_issue, t.required_clearance_m,
                        t.clearance_before_m, t.attempt, t.due_date, t.closed_at, p.title AS package_title,
                        b.lat, b.lon, b.geom_wkt, b.clearance_m, b.bushfire_prone
                 FROM rm.veg_bay_task t JOIN rm.veg_work_package p ON p.package_id = t.package_id
                 LEFT JOIN gld_veg_bay b ON b.bay_id = t.bay_id
                 WHERE t.crew_id = %(c)s AND (t.status <> 'closed' OR t.closed_at > now() - interval '14 days')
                 ORDER BY (t.status = 'closed'), CASE t.risk_tier_at_issue WHEN 'high_risk' THEN 1 WHEN 'cut_required' THEN 2 ELSE 3 END,
                          t.due_date, t.bay_label""", {"c": crew_id})
    _with_labels(rows, [("risk_tier_at_issue", "veg_tier"), ("status", "veg_task_status")])
    return rows


def task_detail(task_id, crew_id=None):
    """A bay task in full: the bay's clearance and risk, its photos (with review status), exceptions, completions, audits, reissues."""
    t = _task_for_crew(task_id, crew_id)
    bay = pq("""SELECT b.bay_id, b.bay_label, b.feeder_id, b.zs_id, b.clearance_m, b.clearance_12m_m, b.clearance_prior_m,
                       b.clearance_change_m, b.required_clearance_m, b.clearance_gap_m, b.required_basis, b.classification,
                       b.risk_tier, b.bushfire_prone, b.hazard_tree, b.dominant_species, b.growth_m_per_year, b.pole_lean_deg,
                       b.customers_downstream, b.risk_score, b.lat, b.lon, r.fault_probability
                FROM gld_veg_bay b LEFT JOIN ml_span_risk r ON r.span_id = b.bay_id WHERE b.bay_id = %(b)s""", {"b": t["bay_id"]}, ttl=300)
    photos = pq("""SELECT p.link_id, p.photo_id, p.kind, p.attempt, p.note, p.crew_id, g.crew_name, p.created_at,
                          EXISTS (SELECT 1 FROM media.photo_bytes b WHERE b.photo_id = p.photo_id) AS has_image
                   FROM rm.veg_task_photo p LEFT JOIN gld_crew g ON g.crew_id = p.crew_id WHERE p.task_id = %(t)s ORDER BY p.created_at""", {"t": task_id})
    for ph in photos:   # a photo whose picture is no longer held keeps its record, without a picture
        ph["image_url"] = photo_url(ph["photo_id"]) if ph["has_image"] else None
    _with_labels(photos, [("kind", "veg_photo_kind")])
    exceptions = pq("""SELECT exception_id, attempt, kind, detail, reported_by, reported_at FROM rm.veg_exception
                       WHERE task_id = %(t)s ORDER BY reported_at""", {"t": task_id})
    _with_labels(exceptions, [("kind", "veg_exception")])
    completions = pq("SELECT * FROM rm.veg_completion WHERE task_id = %(t)s ORDER BY attempt", {"t": task_id})
    audits = pq("SELECT * FROM rm.veg_audit WHERE task_id = %(t)s ORDER BY attempt", {"t": task_id})
    _with_labels(audits, [("result", "veg_audit_result")])
    reissues = pq("SELECT * FROM rm.veg_reissue WHERE task_id = %(t)s ORDER BY issued_at", {"t": task_id})
    _with_labels([t], [("status", "veg_task_status"), ("risk_tier_at_issue", "veg_tier")])
    return dict(task=t, bay=(bay[0] if bay else None), photos=photos, exceptions=exceptions, completions=completions,
                audits=audits, reissues=reissues, exception_kinds=[
                    dict(code=k, label=label("veg_exception", k)) for k in EXCEPTION_KINDS])


def start(task_id, crew_id, actor=None):
    """The crew starts the bay (idempotent)."""
    t = _task_for_crew(task_id, crew_id)
    if t["status"] == "issued":
        pg.q("UPDATE rm.veg_bay_task SET status = 'in_progress', started_at = now(), updated_at = now() WHERE task_id = %(t)s AND status = 'issued'",
             {"t": task_id})
        audit(actor or "contractor", "veg_bay_started", task_id, dict(bay=t["bay_label"]))
    return task_detail(task_id, crew_id)["task"]


def add_photo(task_id, crew_id, image_b64, note, kind, actor, client_op_id=None):
    """A photo of the bay (before or after the cut, or of an exception), kept with the bay task for the post-cut audit."""
    t = _task_for_crew(task_id, crew_id)
    if t["status"] not in ("issued", "in_progress"):
        raise ValueError("This bay is no longer open for photos.")
    kind = kind if kind in ("before", "after", "exception") else "after"
    if client_op_id:
        dup = pq("SELECT link_id, photo_id, kind FROM rm.veg_task_photo WHERE client_op_id = %(o)s", {"o": client_op_id})
        if dup:
            return dict(dup[0], duplicate=True)
    raw, mime = _decode_photo(image_b64)
    w, hgt = _photo_dims(raw, mime)
    photo_id = f"VPH-{uuid.uuid4().hex[:10].upper()}"

    def write(cur):
        cur.execute("INSERT INTO media.photo_bytes (photo_id, image, mime) VALUES (%s, %s, %s)", (photo_id, raw, mime))
        cur.execute("""INSERT INTO rm.veg_task_photo (task_id, photo_id, kind, attempt, note, crew_id, client_op_id)
                       VALUES (%(t)s, %(p)s, %(k)s, %(a)s, %(n)s, %(c)s, %(o)s) RETURNING link_id""",
                    dict(t=task_id, p=photo_id, k=kind, a=t["attempt"], n=(note or "").strip()[:1000] or None, c=crew_id, o=client_op_id))
        return _cursor_rows(cur)[0]["link_id"]
    link_id = _tx(write)
    audit(actor, "veg_photo_added", task_id, dict(photo_id=photo_id, kind=kind, bay=t["bay_label"], width_px=w, height_px=hgt))
    return dict(link_id=link_id, photo_id=photo_id, image_url=photo_url(photo_id), kind=kind, duplicate=False)


def add_exception(task_id, crew_id, kind, detail, actor, client_op_id=None):
    """A problem on the bay the crew cannot fix on the spot (access, a partial cut, a hazard, and so on)."""
    t = _task_for_crew(task_id, crew_id)
    if kind not in EXCEPTION_KINDS:
        raise ValueError("Choose the kind of exception.")
    if t["status"] not in ("issued", "in_progress"):
        raise ValueError("This bay is no longer open for exceptions.")
    if client_op_id:
        dup = pq("SELECT exception_id FROM rm.veg_exception WHERE client_op_id = %(o)s", {"o": client_op_id})
        if dup:
            return dict(exception_id=dup[0]["exception_id"], duplicate=True)
    row = pg.q("""INSERT INTO rm.veg_exception (task_id, attempt, kind, detail, reported_by, client_op_id)
                  VALUES (%(t)s, %(a)s, %(k)s, %(d)s, %(by)s, %(o)s) RETURNING exception_id""",
               dict(t=task_id, a=t["attempt"], k=kind, d=detail, by=actor, o=client_op_id), one=True)
    audit(actor, "veg_exception_noted", task_id, dict(kind=kind, detail=detail, bay=t["bay_label"]))
    return dict(exception_id=row["exception_id"], kind=kind, label=label("veg_exception", kind), duplicate=False)


def simulate_post_cut(bay_id, attempt, required_m, before_m, exception_kinds):
    """The clearance a post-cut LiDAR capture would measure, deterministic from the bay, the attempt and the crew's exceptions.
    A cut usually lands about CUT_TARGET_MARGIN_M beyond the requirement. About UNDER_CUT_PERCENT of cuts (and any cut the crew
    reported as partial) come back short: the clearance rises part of the way and stays under the requirement."""
    digest = int(hashlib.md5(f"{bay_id}:{attempt}".encode()).hexdigest(), 16)
    target = required_m + CUT_TARGET_MARGIN_M
    short = (digest % 100) < UNDER_CUT_PERCENT or "partial_cut" in exception_kinds
    if short:
        post = before_m + (target - before_m) * 0.4 if before_m < target else before_m
    else:
        post = max(before_m, target) + ((digest // 100) % 60) / 100.0
    return round(post, 2)


def complete(task_id, crew_id, notes, actor, client_op_id=None):
    """The crew marks the cut complete. Needs at least one photo of the cut. The post-cut capture is taken now and an audit row is
    written awaiting a reviewer decision. Replays with the same client_op_id return the first result."""
    t = _task_for_crew(task_id, crew_id)
    if client_op_id:
        dup = pq("""SELECT a.* FROM rm.veg_completion c JOIN rm.veg_audit a ON a.completion_id = c.completion_id
                    WHERE c.client_op_id = %(o)s""", {"o": client_op_id})
        if dup:
            return dict(dup[0], duplicate=True)
    if t["status"] not in ("issued", "in_progress"):
        raise ValueError("This bay is not open for completion.")
    photos = pq("SELECT count(*) AS n FROM rm.veg_task_photo WHERE task_id = %(t)s AND attempt = %(a)s", {"t": task_id, "a": t["attempt"]})[0]["n"]
    if photos < 1:
        raise ValueError("Add at least one photo of the cut before marking the bay complete.")
    kinds = [r["kind"] for r in pq("SELECT kind FROM rm.veg_exception WHERE task_id = %(t)s AND attempt = %(a)s",
                                   {"t": task_id, "a": t["attempt"]})]
    required = float(t["required_clearance_m"] or REQUIRED_M["general"])
    before = float(t["clearance_before_m"] if t["clearance_before_m"] is not None else required)
    post = simulate_post_cut(t["bay_id"], t["attempt"], required, before, set(kinds))
    suggested = "compliant" if post >= required else "non_compliant"
    who = actor or "contractor"

    def write(cur):
        cur.execute("""INSERT INTO rm.veg_completion (task_id, attempt, crew_id, completed_by, notes, exception_count, photo_count, client_op_id)
                       VALUES (%(t)s, %(a)s, %(c)s, %(by)s, %(n)s, %(e)s, %(p)s, %(o)s) RETURNING completion_id""",
                    dict(t=task_id, a=t["attempt"], c=crew_id, by=who, n=notes, e=len(kinds), p=photos, o=client_op_id))
        completion_id = _cursor_rows(cur)[0]["completion_id"]
        cur.execute("""UPDATE rm.veg_bay_task SET status = 'awaiting_audit', completed_at = now(), updated_at = now() WHERE task_id = %(t)s""",
                    {"t": task_id})
        cur.execute("""INSERT INTO rm.veg_audit (audit_id, task_id, bay_id, attempt, completion_id, required_clearance_m, clearance_before_m,
                                                 post_cut_clearance_m, clearance_gain_m, post_capture_method, suggested_result, result)
                       VALUES (%(id)s, %(t)s, %(b)s, %(a)s, %(c)s, %(rq)s, %(bf)s, %(pc)s, %(g)s, %(m)s, %(s)s, 'pending')""",
                    dict(id=f"VA-{task_id}-{t['attempt']}", t=task_id, b=t["bay_id"], a=t["attempt"], c=completion_id, rq=required,
                         bf=before, pc=post, g=round(post - before, 2), m=POST_CAPTURE_METHOD, s=suggested))
        cur.execute("SELECT * FROM rm.veg_audit WHERE audit_id = %(id)s", {"id": f"VA-{task_id}-{t['attempt']}"})
        return _cursor_rows(cur)[0]
    row = _tx(write)
    audit(who, "veg_bay_completed", task_id, dict(bay=t["bay_label"], attempt=t["attempt"], post_cut_m=post, suggested=suggested,
                                                 notes=notes))
    return dict(row, duplicate=False)


# ------------------------------------------------------------------ step 5: audit and validate
def audits(result=None, limit=200):
    """Post-cut audits, awaiting decisions first, with the before and after clearance and the photos of the cut."""
    where, args = _where([("a.result = %(r)s", "r", result)])
    rows = pq(f"""SELECT a.audit_id, a.task_id, a.bay_id, a.attempt, a.required_clearance_m, a.clearance_before_m,
                         a.post_cut_clearance_m, a.clearance_gain_m, a.post_capture_method, a.suggested_result, a.result,
                         a.decided_by, a.decided_at, a.decision_notes, a.created_at,
                         t.bay_label, t.feeder_id, t.status AS task_status, t.risk_tier_at_issue, c.crew_id, g.crew_name,
                         (SELECT count(*) FROM rm.veg_task_photo p WHERE p.task_id = a.task_id AND p.attempt = a.attempt) AS photo_count,
                         (SELECT json_agg(json_build_object('photo_id', p.photo_id, 'kind', p.kind,
                                                            'has_image', EXISTS (SELECT 1 FROM media.photo_bytes b WHERE b.photo_id = p.photo_id))
                                          ORDER BY p.created_at)
                          FROM rm.veg_task_photo p WHERE p.task_id = a.task_id AND p.attempt = a.attempt) AS photos,
                         (SELECT count(*) FROM rm.veg_exception e WHERE e.task_id = a.task_id AND e.attempt = a.attempt) AS exception_count
                  FROM rm.veg_audit a JOIN rm.veg_bay_task t ON t.task_id = a.task_id
                  LEFT JOIN rm.veg_completion c ON c.completion_id = a.completion_id
                  LEFT JOIN gld_crew g ON g.crew_id = c.crew_id
                  WHERE TRUE{where}
                  ORDER BY (a.result = 'pending') DESC, a.created_at DESC LIMIT %(n)s""", {**args, "n": int(limit)})
    _with_labels(rows, [("result", "veg_audit_result"), ("suggested_result", "veg_audit_result"), ("risk_tier_at_issue", "veg_tier")])
    for r in rows:   # the photos of the cut, shown with the audit
        r["photos"] = [dict(p, image_url=photo_url(p["photo_id"]) if p["has_image"] else None) for p in (r["photos"] or [])]
    counts = pq("SELECT result, count(*) AS n FROM rm.veg_audit GROUP BY result", ttl=0)
    _with_labels(counts, [("result", "veg_audit_result")])
    return dict(audits=rows, counts=counts)


def decide(audit_id, decision, notes, actor, crew_id=None):
    """Accept a compliant bay (closed) or reissue a non-compliant one to the contractor (a new attempt, back in the crew's list)."""
    if decision not in ("compliant", "non_compliant"):
        raise ValueError("The decision must be either compliant or non-compliant.")
    if decision == "non_compliant" and not (notes or "").strip():
        raise ValueError("Say why the bay is being reissued.")
    if crew_id:
        _crew(crew_id)

    def write(cur):
        cur.execute("SELECT * FROM rm.veg_audit WHERE audit_id = %(id)s FOR UPDATE", {"id": audit_id})
        rows = _cursor_rows(cur)
        if not rows:
            raise NotFound(f"Audit {audit_id} was not found.")
        a = rows[0]
        if a["result"] != "pending":
            raise ValueError("This bay has already been decided.")
        cur.execute("""UPDATE rm.veg_audit SET result = %(r)s, decided_by = %(by)s, decided_at = now(), decision_notes = %(n)s
                       WHERE audit_id = %(id)s""", dict(r=decision, by=actor, n=notes, id=audit_id))
        cur.execute("SELECT crew_id FROM rm.veg_completion WHERE completion_id = %(c)s", {"c": a["completion_id"]})
        from_crew = (_cursor_rows(cur) or [{}])[0].get("crew_id")
        if decision == "compliant":
            cur.execute("UPDATE rm.veg_bay_task SET status = 'closed', closed_at = now(), updated_at = now() WHERE task_id = %(t)s",
                        {"t": a["task_id"]})
            return dict(a, result=decision), None
        to_crew = crew_id or from_crew
        cur.execute("""INSERT INTO rm.veg_reissue (task_id, audit_id, from_attempt, to_attempt, from_crew_id, to_crew_id, reason, issued_by)
                       VALUES (%(t)s, %(a)s, %(fa)s, %(ta)s, %(fc)s, %(tc)s, %(r)s, %(by)s) RETURNING reissue_id""",
                    dict(t=a["task_id"], a=audit_id, fa=a["attempt"], ta=a["attempt"] + 1, fc=from_crew, tc=to_crew, r=notes, by=actor))
        reissue_id = _cursor_rows(cur)[0]["reissue_id"]
        cur.execute("""UPDATE rm.veg_bay_task SET status = 'issued', attempt = attempt + 1, crew_id = %(c)s, started_at = NULL,
                       completed_at = NULL, clearance_before_m = %(pc)s, updated_at = now() WHERE task_id = %(t)s""",
                    dict(c=to_crew, pc=a["post_cut_clearance_m"], t=a["task_id"]))
        return dict(a, result=decision), dict(reissue_id=reissue_id, to_crew_id=to_crew, to_attempt=a["attempt"] + 1)
    row, reissue = _tx(write)
    audit(actor, "veg_audit_" + decision, audit_id, dict(task=row["task_id"], notes=notes, reissue=reissue))
    return dict(row, reissue=reissue)


# ------------------------------------------------------------------ the office view: released work packages
def released():
    """Every released work package (not started, in progress or complete) with its status counts, and the exceptions still open on
    their bays."""
    pk = packages()
    open_exc = pq("""SELECT e.exception_id, e.kind, e.detail, e.reported_by, e.reported_at, e.attempt, t.task_id, t.bay_id, t.bay_label, t.crew_id,
                            p.title AS package_title
                     FROM rm.veg_exception e JOIN rm.veg_bay_task t ON t.task_id = e.task_id JOIN rm.veg_work_package p ON p.package_id = t.package_id
                     WHERE t.status <> 'closed' ORDER BY e.reported_at DESC LIMIT 60""", ttl=0)
    _with_labels(open_exc, [("kind", "veg_exception")])
    counts = {k: sum(1 for p in pk if p["status"] == k) for k in PACKAGE_STATUS}
    return dict(packages=pk, open_exceptions=open_exc, counts=counts, status_labels=PACKAGE_STATUS)


# ------------------------------------------------------------------ offline sync (the contractor queue, replayed)
def sync(crew_id, ops, actor):
    """Replay the actions a contractor queued offline. Each op is applied on its own: one failure does not stop the others. Every
    op carries a client_op_id, so a replay after a dropped connection does not repeat it."""
    crew = _crew(crew_id)
    who = _actor_label(actor, crew["crew_name"])
    results = []
    for op in ops or []:
        oid = op.get("client_op_id")
        try:
            kind = op.get("type")
            if kind == "start":
                start(op["task_id"], crew_id, who)
                results.append(dict(client_op_id=oid, ok=True, type=kind))
            elif kind == "exception":
                r = add_exception(op["task_id"], crew_id, op.get("kind"), op.get("detail"), who, oid)
                results.append(dict(client_op_id=oid, ok=True, type=kind, duplicate=r.get("duplicate", False)))
            elif kind == "photo":
                r = add_photo(op["task_id"], crew_id, op.get("image_b64"), op.get("note"), op.get("kind"), who, oid)
                results.append(dict(client_op_id=oid, ok=True, type=kind, photo_id=r.get("photo_id"), duplicate=r.get("duplicate", False)))
            elif kind == "complete":
                r = complete(op["task_id"], crew_id, op.get("notes"), who, oid)
                results.append(dict(client_op_id=oid, ok=True, type=kind, suggested=r.get("suggested_result"),
                                    duplicate=r.get("duplicate", False)))
            else:
                raise ValueError(f"The operation {kind} is not recognised.")
        except (ValueError, NotFound) as e:
            results.append(dict(client_op_id=oid, ok=False, type=op.get("type"), error=str(e)))
    return dict(results=results)


# ---------------------------------------------------------------- LiDAR view of one span (prototype)
def lidar(span_id):
    """What the LiDAR view of one overhead span is built from: the span (length and conductor), the poles at its two ends (height,
    material, lean), and the latest survey's results (clearance now and in 12 months, species, growth a year, hazard tree, bushfire-prone
    land). The point cloud the screen draws is built from these measured values; its shapes are illustrative."""
    v = pq("SELECT * FROM gld_vegetation_risk WHERE span_id = %(s)s", {"s": span_id}, ttl=300)
    if not v:
        return None
    v = v[0]
    span = pq("""SELECT asset_id, feeder_id, zs_id, rating AS length_m, material AS conductor, install_year, geom_wkt FROM gld_asset
                 WHERE asset_id = %(s)s""", {"s": span_id}, ttl=300)
    span = span[0] if span else dict(asset_id=span_id, feeder_id=v["feeder_id"], zs_id=v["zs_id"], length_m=None, conductor=None, geom_wkt=v["geom_wkt"])
    # the poles at the span's two ends, the start first (the span's line runs from one pole to the other)
    poles = pq("""WITH s AS (SELECT ST_GeomFromText(%(g)s, 4326) AS g)
                  SELECT a.asset_id, a.rating AS height_m, a.material, a.install_year,
                         CASE WHEN ST_DWithin(ST_GeogFromText(a.geom_wkt), ST_StartPoint(s.g)::geography, 3) THEN 'start' ELSE 'end' END AS end_at
                  FROM gld_asset a, s
                  WHERE a.asset_class = 'pole' AND (ST_DWithin(ST_GeogFromText(a.geom_wkt), ST_StartPoint(s.g)::geography, 3)
                                                    OR ST_DWithin(ST_GeogFromText(a.geom_wkt), ST_EndPoint(s.g)::geography, 3))
                  ORDER BY end_at DESC""", {"g": span["geom_wkt"]}, ttl=300) if span.get("geom_wkt") else []
    length = span.get("length_m") or (pq("SELECT ST_Length(ST_GeogFromText(%(g)s)) AS m", {"g": span["geom_wkt"]})[0]["m"] if span.get("geom_wkt") else 60)
    basis = "bushfire_prone" if v["bushfire_prone"] else "general"
    return dict(span_id=span_id, feeder_id=span.get("feeder_id"), zs_id=span.get("zs_id"), length_m=round(float(length), 1),
                conductor=span.get("conductor"), installed=span.get("install_year"), poles=poles,
                capture_date=v["capture_date"], clearance_m=v["clearance_m"], clearance_12m_m=v["clearance_12m_m"],
                required_m=REQUIRED_M[basis], basis_label=label("veg_basis", basis), category_label=v["category_label"],
                species=v["dominant_species"], growth_m_per_year=v["growth_m_per_year"], hazard_tree=bool(v["hazard_tree"]),
                bushfire_prone=bool(v["bushfire_prone"]), pole_lean_deg=v["pole_lean_deg"], customers_downstream=v["customers_downstream"],
                risk_score=v["risk_score"])


def region_feeders(area):
    """A region's feeders for the vegetation drill-down, most urgent first, with the zone substation each one leaves."""
    return pq("""SELECT v.feeder_id, v.zs_id, v.bays, v.current_intrusions, v.high_risk, v.cut_required, v.planned, v.bushfire_bays, v.hazard_trees,
                        v.customers_at_intrusion, v.feeder_status
                 FROM gld_veg_feeder v JOIN gld_feeder_state f USING (feeder_id) WHERE f.area = %(a)s
                 ORDER BY v.high_risk DESC, v.cut_required DESC, v.planned DESC, v.feeder_id""", {"a": area}, ttl=120)


def feeder_spans(fid):
    """Every bay (overhead span) on a feeder in line order (span numbers run pole to pole along the line), with its clearance and tier.
    The LiDAR view walks this order; the drill-down lists it most urgent first."""
    rows = pq("""SELECT bay_id, bay_label, feeder_id, zs_id, risk_tier, classification, clearance_m, clearance_12m_m, required_clearance_m,
                        clearance_gap_m, dominant_species, hazard_tree, bushfire_prone
                 FROM gld_veg_bay WHERE feeder_id = %(f)s ORDER BY bay_id""", {"f": fid}, ttl=120)
    _with_labels(rows, [("classification", "veg_class"), ("risk_tier", "veg_tier")])
    return rows


def map_spans():
    """Every surveyed span as a short line for the network map, coloured by its risk tier: [bay id, tier, clearance m, lat0, lon0, lat1, lon1]."""
    rows = pq("""SELECT b.bay_id, b.risk_tier, b.clearance_m, ST_Y(ST_StartPoint(g.geom)) AS la0, ST_X(ST_StartPoint(g.geom)) AS lo0,
                        ST_Y(ST_EndPoint(g.geom)) AS la1, ST_X(ST_EndPoint(g.geom)) AS lo1
                 FROM gld_veg_bay b JOIN (SELECT span_id, ST_GeomFromText(geom_wkt, 4326) AS geom FROM gld_vegetation_risk
                                          WHERE geom_wkt IS NOT NULL) g ON g.span_id = b.bay_id""", ttl=600)
    return dict(spans=[[r["bay_id"], r["risk_tier"], round(r["clearance_m"], 2), round(r["la0"], 6), round(r["lo0"], 6), round(r["la1"], 6), round(r["lo1"], 6)]
                       for r in rows], tiers={t: label("veg_tier", t) for t in ("high_risk", "cut_required", "planned", "no_action")})
