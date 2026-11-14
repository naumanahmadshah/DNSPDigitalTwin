"""Crew overdue on an emergency job: every crew sent to an emergency is watched until it reports, and an alert is raised when it goes quiet.

When a crew is dispatched to an emergency job (rm.dispatch.dispatch), watch() opens a record in rm.emergency_watch: the expected finish
(dispatch plus travel plus the job's time) and a grace period (a quarter of that, at least GRACE_FLOOR_MIN). sweep() runs every minute:
- a new status from the crew (rm.field_status, from the field app, the live engine or a dispatch) is the crew's latest update. On site
  (in_progress) moves the expected finish to the arrival plus the job's time; done or parked closes the watch;
- past the due time with no update, it raises a "crew overdue" alert (atlas.situation SIT-OVERDUE-<job>, kind overdue) linked to the
  situation the crew was sent to, and records it on that situation's trail;
- an update from the crew, or the job taken over by another crew, recalled, or the outage restored, closes the alert.
A welfare call logged on the alert (welfare_check) gives the crew until the agreed time; still nothing by then escalates it to critical.
"""
import datetime as dt
import json
import threading
import time
import traceback

import core
from core import pg
from rm import data as RD

CHECK_EVERY_S = 60
GRACE_FLOOR_MIN = 30      # at least this many extra minutes before a crew counts as overdue
GRACE_SHARE = 0.25        # or a quarter of the expected time, whichever is longer
WELFARE_EXTEND_MIN = 30   # a welfare call gives the crew this long to report
CLOSING = {"done": "done", "parked": "parked"}
_lock = threading.Lock()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _t(v):
    """A timestamp from the database (the pool returns them as ISO text) as an aware datetime."""
    if v is None or isinstance(v, dt.datetime):
        return v
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def grace(expected_min):
    return int(max(GRACE_FLOOR_MIN, round(GRACE_SHARE * expected_min)))


def watch(job, crew_id, crew_name, travel_min, actor):
    """Start watching a crew sent to an emergency job (a later dispatch of the same job replaces the watch: the new crew is watched)."""
    if not job.get("is_emergency"):
        return
    sid = job.get("situation_id")
    s = pg.q("SELECT payload FROM atlas.situation WHERE situation_id = %s", (sid,), one=True) if sid else None
    outage = ((s or {}).get("payload") or {}).get("outage_id")
    dur = int(job.get("duration_min") or 60)
    g = grace(travel_min + dur)
    now = _now()
    expected = now + dt.timedelta(minutes=travel_min + dur)
    prev = pg.q("SELECT crew_id, crew_name, status, overdue_situation_id FROM rm.emergency_watch WHERE job_id = %s", (job["job_id"],), one=True)
    if prev and prev["status"] == "overdue":
        _close_alert(prev, f"{crew_name} was sent to take over from {prev['crew_name']}.", "taken over")
    pg.q("""INSERT INTO rm.emergency_watch (job_id, region_id, crew_id, crew_name, situation_id, outage_id, dispatched_at, dispatched_by, travel_min,
                                           duration_min, grace_min, expected_by, due_at, last_update_at, last_status, welfare_checks, status,
                                           overdue_situation_id, closed_at, closed_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'driving',0,'watching',NULL,NULL,NULL)
            ON CONFLICT (job_id) DO UPDATE SET crew_id = EXCLUDED.crew_id, crew_name = EXCLUDED.crew_name, situation_id = EXCLUDED.situation_id,
              outage_id = EXCLUDED.outage_id, dispatched_at = EXCLUDED.dispatched_at, dispatched_by = EXCLUDED.dispatched_by,
              travel_min = EXCLUDED.travel_min, duration_min = EXCLUDED.duration_min, grace_min = EXCLUDED.grace_min,
              expected_by = EXCLUDED.expected_by, due_at = EXCLUDED.due_at, last_update_at = EXCLUDED.last_update_at, last_status = 'driving',
              welfare_checks = 0, status = 'watching', overdue_situation_id = NULL, closed_at = NULL, closed_reason = NULL""",
         (job["job_id"], job["region_id"], crew_id, crew_name, sid, outage, now, actor, int(travel_min), dur, g, expected,
          expected + dt.timedelta(minutes=g), now))


def stop(job_id, reason, note):
    """The dispatch was taken back (recalled): the watch closes, and so does any overdue alert."""
    w = pg.q("SELECT * FROM rm.emergency_watch WHERE job_id = %s AND status <> 'closed'", (job_id,), one=True)
    if w:
        _close(w, reason, note)


def welfare_check(job_id, actor):
    """A welfare call to the overdue crew was logged: it gets WELFARE_EXTEND_MIN more minutes to report before the alert escalates."""
    w = pg.q("SELECT * FROM rm.emergency_watch WHERE job_id = %s AND status <> 'closed'", (job_id,), one=True)
    if not w:
        return "The crew has already reported. There is nothing to check."
    due = _now() + dt.timedelta(minutes=WELFARE_EXTEND_MIN)
    pg.q("UPDATE rm.emergency_watch SET welfare_checks = welfare_checks + 1, due_at = %s WHERE job_id = %s", (due, job_id))   # the finish stays
    core.audit(actor, "crew_welfare_check", job_id, dict(crew=w["crew_id"], next_due=due.isoformat()))
    return f"Welfare call to {w['crew_name']} logged. The crew has until {_hhmm(due)} to report before the alert escalates."


def _mins(n):
    return f"{n} minute" if n == 1 else f"{n} minutes"


def _qty(n, one):
    """A count with its noun, singular for one: '1 customer', '1,268 customers'."""
    return f"{n:,} {one if n == 1 else one + 's'}"


def _still_off(n):
    """'1 customer is still off' or '1,268 customers are still off'."""
    return f"{_qty(n, 'customer')} {'is' if n == 1 else 'are'} still off"


def _hhmm(t):
    return t.astimezone(RD.SYD).strftime("%-I:%M %p").lower()


def _close(w, reason, note):
    pg.q("UPDATE rm.emergency_watch SET status = 'closed', closed_at = now(), closed_reason = %s WHERE job_id = %s", (reason, w["job_id"]))
    if w.get("overdue_situation_id"):
        _close_alert(w, note, reason)


def _close_alert(w, note, reason):
    sid = w["overdue_situation_id"]
    got = pg.q("""UPDATE atlas.situation SET status = 'closed', headline = %s, updated_at = now() WHERE situation_id = %s AND status <> 'closed'
                  RETURNING situation_id""", (note, sid))
    if got:
        pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,'detector','closed',%s::jsonb)",
             (sid, json.dumps(dict(reason=reason, note=note))))


def _context(w):
    """The job, the situation the crew was sent to and its outage, for the alert's words and figures."""
    job = pg.q("SELECT job_id, title, feeder_id, asset_id, lat, lon, customers FROM rm.raised_job WHERE job_id = %s", (w["job_id"],), one=True) or {}
    sit = pg.q("SELECT situation_id, title, status FROM atlas.situation WHERE situation_id = %s", (w["situation_id"],), one=True) if w["situation_id"] else None
    out = pg.q("SELECT status, customers_off, life_support_off FROM atlas.live_outage WHERE outage_id = %s", (w["outage_id"],), one=True) if w["outage_id"] else None
    return job, sit, out


def _raise_alert(w, escalate=False):
    """A crew overdue alert for the watch: who, what they were sent to, how late, and who is still waiting for them."""
    job, sit, out = _context(w)
    now = _now()
    late = int((now - w["expected_by"]).total_seconds() // 60)
    heard = int((now - (w["last_update_at"] or w["dispatched_at"])).total_seconds() // 60)
    off = int((out or {}).get("customers_off") or 0) if (out or {}).get("status") == "active" else 0
    ls = int((out or {}).get("life_support_off") or 0) if (out or {}).get("status") == "active" else 0
    sev = "critical" if escalate or ls > 0 else "high"
    sid = f"SIT-OVERDUE-{w['job_id']}"
    what = (sit or {}).get("title") or job.get("title") or w["job_id"]
    head = (f"{w['crew_name']} was sent at {_hhmm(w['dispatched_at'])} to the situation '{what}' and was expected done by {_hhmm(w['expected_by'])}. "
            f"No update has been heard for {_mins(heard)} ({_mins(late)} past the expected finish, {_mins(w['grace_min'])} allowed)."
            + (f" {_still_off(off)}" + (f", {_qty(ls, 'customer')} on life support." if ls else ".") if off else "")
            + (f" There is still no report after the welfare call, which gave the crew until {_hhmm(w['due_at'])}." if escalate else ""))
    payload = dict(job_id=w["job_id"], crew_id=w["crew_id"], region_id=w["region_id"], linked_situation_id=w["situation_id"],
                   outage_id=w["outage_id"], lat=job.get("lat"), lon=job.get("lon"), asset_id=job.get("asset_id"))
    pg.q("""INSERT INTO atlas.situation (situation_id, type, title, severity, status, source, feeders, customers, life_support, critical_sites,
                                         headline, payload, detected_at, updated_at)
            VALUES (%s,'overdue',%s,%s,'detected','detector',%s,%s,%s,0,%s,%s::jsonb,now(),now())
            ON CONFLICT (situation_id) DO UPDATE SET status = CASE WHEN atlas.situation.status = 'closed' THEN 'detected' ELSE atlas.situation.status END,
              severity = EXCLUDED.severity, headline = EXCLUDED.headline, customers = EXCLUDED.customers, life_support = EXCLUDED.life_support,
              payload = EXCLUDED.payload, updated_at = now()""",
         (sid, f"No update from {w['crew_name']} on {what}", sev, [job["feeder_id"]] if job.get("feeder_id") else [], off, ls, head,
          json.dumps(payload, default=str)))
    pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,'detector',%s,%s::jsonb)",
         (sid, "escalated" if escalate else "detected", json.dumps(dict(trigger=head))))
    if w["situation_id"] and not escalate:   # the situation the crew was sent to shows it too
        pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,'detector','crew_overdue',%s::jsonb)",
             (w["situation_id"], json.dumps(dict(crew=w["crew_name"], alert=sid))))
    pg.q("UPDATE rm.emergency_watch SET status = 'overdue', overdue_situation_id = %s WHERE job_id = %s", (sid, w["job_id"]))


def sweep():
    """One pass over the open watches: take each crew's latest update, close what has finished, raise or escalate what is overdue."""
    with _lock:
        rows = pg.q("""SELECT w.*, f.status AS fs_status, f.updated_at AS fs_at, o.status AS outage_status
                       FROM rm.emergency_watch w
                       LEFT JOIN rm.field_status f ON f.job_id = w.job_id
                       LEFT JOIN atlas.live_outage o ON o.outage_id = w.outage_id
                       WHERE w.status <> 'closed'""")
        now = _now()
        for w in rows:
            for k in ("dispatched_at", "expected_by", "due_at", "last_update_at", "fs_at"):
                w[k] = _t(w[k])
            if w["outage_status"] and w["outage_status"] != "active":
                _close(w, "restored", f"The outage {w['crew_name']} was sent to is restored.")
                continue
            if w["fs_status"] is None:   # the dispatch was taken back and the job left the crew
                _close(w, "recalled", f"{w['crew_name']} was recalled from the job.")
                continue
            if w["fs_status"] in CLOSING:
                _close(w, CLOSING[w["fs_status"]], f"{w['crew_name']} reported the job {'done' if w['fs_status'] == 'done' else 'parked'}.")
                continue
            if w["fs_at"] and (w["last_update_at"] is None or w["fs_at"] > w["last_update_at"]):   # a new update from the crew
                expected = w["expected_by"]
                if w["fs_status"] == "in_progress" and w["last_status"] != "in_progress":   # on site: the job's time runs from the arrival
                    expected = w["fs_at"] + dt.timedelta(minutes=w["duration_min"])
                pg.q("""UPDATE rm.emergency_watch SET last_update_at = %s, last_status = %s, expected_by = %s, due_at = %s,
                        status = 'watching' WHERE job_id = %s""",
                     (w["fs_at"], w["fs_status"], expected, expected + dt.timedelta(minutes=w["grace_min"]), w["job_id"]))
                if w["status"] == "overdue":
                    _close_alert(w, f"{w['crew_name']} sent an update: {RD.label('ops_job_status', w['fs_status']).lower()}.", "updated")
                continue
            if now > w["due_at"]:
                if w["status"] == "watching":
                    _raise_alert(w)
                elif w["welfare_checks"] and not pg.q("SELECT 1 AS x FROM atlas.situation WHERE situation_id = %s AND severity = 'critical'",
                                                      (w["overdue_situation_id"],), one=True):
                    _raise_alert(w, escalate=True)   # the time agreed on the welfare call has run out too


def keep():
    """Background loop: one sweep a minute for the life of the app."""
    while True:
        try:
            sweep()
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_EVERY_S)


def start():
    threading.Thread(target=keep, name="emergency-watch", daemon=True).start()
