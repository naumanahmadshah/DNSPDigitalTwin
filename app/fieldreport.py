"""Field status reports: what a crew found and did on a job. The field app files them (submit); Situations show a situation's reports, Resources
shows a job's and a crew's reports, and Operations reads them. Records live in Lakebase schema rm (fed to the lakehouse).

Labels: every code is shown through its label. LABELS below is the reference (it also covers the codes of the asset photos and
the restoration screen); ref_label rows of the same domain, when the gold reference table carries them, override it."""
import datetime as dt
import json
import uuid

import core
from core import pg, pq

OUTCOMES = ("fixed", "temporary_repair", "follow_up")
ACCESS = ("clear", "restricted", "blocked")
FAULT_KINDS = ("pole", "span", "cable", "transformer", "none")
HAZARDS = ("live_wire", "tree", "flood", "fire", "traffic", "unstable_pole")
MATERIALS = ("cross_arm", "conductor", "insulator", "fuse_link", "pole", "lv_cable", "tie_wire")
MAX_QTY = 100000

# domain -> code -> label (the reference labels; ref_label rows of the same domain override them)
LABELS = {
    "field_outcome": {"fixed": "Fixed", "temporary_repair": "Temporary repair", "follow_up": "Needs follow up"},
    "field_access": {"clear": "Clear", "restricted": "Restricted", "blocked": "Blocked"},
    "field_fault_kind": {"pole": "Pole", "span": "Span", "cable": "Underground cable", "transformer": "Distribution transformer",
                         "none": "Not found"},
    "field_hazard": {"live_wire": "Live wire down", "tree": "Tree on the line", "flood": "Flood water", "fire": "Fire or smoke",
                     "traffic": "Traffic", "unstable_pole": "Unstable pole"},
    "field_material": {"cross_arm": "Cross arm", "conductor": "Conductor (m)", "insulator": "Insulator", "fuse_link": "Fuse link",
                       "pole": "Pole", "lv_cable": "LV cable (m)", "tie_wire": "Tie wire"},
    "ops_channel": {"sms": "SMS", "email": "Email", "app": "App or web", "contact_centre": "Contact centre", "mail": "Mail"},
    "ops_crew_status": {"travelling": "Travelling", "on_site": "On site", "available": "Available"},
    "ops_job_status": {"planned": "Planned", "driving": "Driving", "in_progress": "In progress", "done": "Done", "parked": "On hold",
                       "reassigning": "Re-rostering", "reassigned": "Re-rostered"},
    "ops_meter_event": {"POWER_OUTAGE": "Power off alarm", "POWER_RESTORE": "Power restored alarm"},
    "ops_switch_quality": {"good": "Good", "questionable": "Questionable", "comm_lost": "Communications lost", "substituted": "Substituted"},
    "ops_drone_defect": {"vegetation_contact": "Vegetation contact", "bird_nest": "Bird nest", "pole_damage": "Pole damage",
                         "damaged_insulator": "Damaged insulator", "cracked_crossarm": "Cracked cross arm"},
    "asset_photo_status": {"uploaded": "Uploaded, AI check running", "ai_checked": "AI checked, awaiting review", "fixed": "Accepted as fixed",
                           "updated": "Accepted as updated photo", "rejected": "Rejected"},
    "asset_photo_source": {"field": "Field crew", "vegetation": "Contractor bay"},
    "asset_review_decision": {"fixed": "Accept as fixed", "updated": "Accept as updated photo", "rejected": "Reject and delete"},
    "asset_ai_outcome": {"fixed": "Looks fixed", "updated": "Usable current photo", "rejected": "Does not match or unusable"},
    "asset_condition": {"good": "Good", "fair": "Fair", "poor": "Poor", "unclear": "Unclear"},
}


def labels(domain):
    """Code to label for one domain: the reference labels, overridden by any ref_label rows of the same domain."""
    merged = dict(LABELS.get(domain, {}))
    for r in pq("SELECT code, label FROM ref_label WHERE domain = %s", (domain,), ttl=600):
        merged[r["code"]] = r["label"]
    return merged


def label(domain, code):
    if code is None:
        return None
    return labels(domain).get(code, code)


def _options(domain, codes):
    lab = labels(domain)
    return [dict(code=c, label=lab.get(c, c)) for c in codes]


# ---------------------------------------------------------------- the job a report is filed against
def job_of(job_id):
    """A raised job (run time: an outage response or a dispatcher's job) or a planned work order (synced gold), or None."""
    r = pg.q("""SELECT job_id, region_id, situation_id, asset_id, feeder_id, title, job_type, depot_id, created_at
                FROM rm.raised_job WHERE job_id = %s""", (job_id,), one=True)
    if r is None:
        r = pg.q("""SELECT job_id, region_id, NULL::text AS situation_id, asset_id, feeder_id, title, job_type, depot_id,
                    NULL::timestamptz AS created_at FROM gld_field_job WHERE job_id = %s""", (job_id,), one=True)
    return r


def outage_of(job_id):
    """The live outage a job repairs (its id is the job id minus the JOB- prefix), or None."""
    oid = job_id[4:] if (job_id or "").startswith("JOB-") else None
    if oid and pg.q("SELECT 1 FROM atlas.live_outage WHERE outage_id = %s", (oid,), one=True):
        return oid
    return None


def meta(job_id=None):
    """Everything the report form needs: the choices with labels, and the job's context (its asset, outage, and the photos taken for it)."""
    out = dict(outcomes=_options("field_outcome", OUTCOMES), access=_options("field_access", ACCESS),
               fault_kinds=_options("field_fault_kind", FAULT_KINDS), hazards=_options("field_hazard", HAZARDS),
               materials=_options("field_material", MATERIALS), job=None, photos=[])
    if job_id:
        j = job_of(job_id)
        if j:
            asset = pq("SELECT asset_id, name, asset_class_label FROM gld_asset WHERE asset_id = %s", (j["asset_id"],), ttl=300) if j["asset_id"] else []
            out["job"] = dict(job_id=j["job_id"], title=j["title"], asset_id=j["asset_id"], asset_name=(asset[0]["name"] if asset else None),
                              asset_class=(asset[0]["asset_class_label"] if asset else None), outage_id=outage_of(job_id),
                              situation_id=j["situation_id"], region_id=j["region_id"])
        out["photos"] = pg.q("SELECT photo_id, asset_id, status, submitted_at FROM atlas.asset_photo WHERE ref_id = %s ORDER BY submitted_at DESC",
                             (job_id,))
    return out


# ---------------------------------------------------------------- validation of a submitted report (every error is a ValueError, shown to the crew)
def _int(v, name, lo, hi):
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number.")
    if n < lo or n > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return n


def _codes(values, allowed, name):
    out = []
    for c in values or []:
        if c not in allowed:
            raise ValueError(f"Unknown {name}.")
        if c not in out:
            out.append(c)
    return out


def _materials(items):
    out = []
    for m in items or []:
        code = (m or {}).get("code")
        if code not in MATERIALS:
            raise ValueError("Unknown material.")
        qty = _int((m or {}).get("qty"), "Material quantity", 0, MAX_QTY) or 0
        if qty > 0:
            out.append(dict(code=code, qty=qty))
    return out


def _when(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).isoformat()
    except ValueError:
        raise ValueError("The arrival time is not a valid time.")


def submit(body, actor):
    """File one report against a job. Returns the new report id."""
    job = job_of(str(body.get("job_id") or ""))
    if not job:
        raise ValueError("That job is no longer on the plan. Reload the day and report again.")
    outcome = body.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("Choose how the job is left: fixed, a temporary repair, or needs follow up.")
    access = body.get("access_status") or None
    if access and access not in ACCESS:
        raise ValueError("Unknown access status.")
    fault_kind = body.get("fault_kind") or None
    if fault_kind and fault_kind not in FAULT_KINDS:
        raise ValueError("Unknown fault kind.")
    total = _int(body.get("segments_total"), "Segments on the route", 0, 200)
    cleared = _int(body.get("segments_cleared"), "Segments cleared", 0, 200)
    if total is not None and cleared is not None and cleared > total:
        raise ValueError("Segments cleared cannot be more than the segments on the route.")
    restored = _int(body.get("customers_restored"), "Customers restored", 0, 10000000) or 0
    report_id = f"FR-{uuid.uuid4().hex[:10].upper()}"
    pg.q("""INSERT INTO rm.field_report (report_id, job_id, situation_id, outage_id, region_id, crew_id, asset_id, reported_by, arrived_at,
                response_note, patrol_complete, fault_located, fault_kind, fault_ref, segments_total, segments_cleared, switching_executed,
                customers_restored, access_status, hazards, materials, notes, outcome, photo_ids)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
         (report_id, job["job_id"], job["situation_id"], outage_of(job["job_id"]), job["region_id"], body.get("crew_id"), job["asset_id"],
          actor, _when(body.get("arrived_at")), (body.get("response_note") or "").strip()[:500] or None,
          bool(body.get("patrol_complete")), bool(body.get("fault_located")), fault_kind, (body.get("fault_ref") or "").strip()[:64] or None,
          total, cleared, bool(body.get("switching_executed")), restored, access,
          _codes(body.get("hazards"), HAZARDS, "hazard"), json.dumps(_materials(body.get("materials"))),
          (body.get("notes") or "").strip()[:2000] or None, outcome,
          [str(p) for p in (body.get("photo_ids") or [])][:20]))
    core.audit(actor, "field_report", job["job_id"], dict(report_id=report_id, outcome=outcome, customers_restored=restored))
    return dict(ok=True, report_id=report_id, job_id=job["job_id"])


# ---------------------------------------------------------------- reading reports (with labels, for every surface)
def shape(r):
    """A report row with its codes turned into labels: outcome, access, fault kind, hazards and materials."""
    r = dict(r)
    r["outcome_label"] = label("field_outcome", r.get("outcome"))
    r["access_label"] = label("field_access", r.get("access_status"))
    r["fault_label"] = label("field_fault_kind", r.get("fault_kind"))
    r["hazard_labels"] = [label("field_hazard", h) for h in (r.get("hazards") or [])]
    r["materials"] = [dict(code=m["code"], qty=m["qty"], label=label("field_material", m["code"])) for m in (r.get("materials") or [])]
    return r


def reports(situation_id=None, job_id=None, crew_id=None, region_id=None, outage_ids=None, limit=50):
    """Field reports, newest first, filtered by any of: situation, job, crew, region, or a list of outages."""
    where, args = [], {}
    for col, val in (("situation_id", situation_id), ("job_id", job_id), ("crew_id", crew_id), ("region_id", region_id)):
        if val:
            where.append(f"r.{col} = %({col})s")
            args[col] = val
    if outage_ids is not None:
        where.append("r.outage_id = ANY(%(outs)s)")
        args["outs"] = list(outage_ids)
    sql = """SELECT r.*, g.crew_name, g.persona FROM rm.field_report r LEFT JOIN gld_crew g ON g.crew_id = r.crew_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.reported_at DESC LIMIT %(lim)s"
    args["lim"] = min(max(int(limit), 1), 200)
    return [shape(x) for x in pg.q(sql, args)]
