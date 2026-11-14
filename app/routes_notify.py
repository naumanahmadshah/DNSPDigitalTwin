"""Customer notices API (the notification engine). Mounted by main.py with app.include_router(routes_notify.router).

Candidates are the planned outages of the released works plans, live faults and weather hazards; a planner can also describe a custom
notice. A notice is drafted by AI for the channels the planner chose and goes to customers only when the planner releases it (sent now, or
scheduled for its notice date). Errors are ValueError, which main.py returns as HTTP 400 with the reason in "detail".
"""
from fastapi import APIRouter, Request

import notify as N

router = APIRouter()


def _actor(req: Request):
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or "presenter"


@router.get("/api/notify/overview")
def overview():
    """The figures across the top: notices being designed, scheduled, and sent this week with the customers reached."""
    return N.overview()


@router.get("/api/notify/candidates")
def candidates(kind: str = "planned_work"):
    """Candidates by kind: planned_work (the released plans' planned outages, with customers and whether the notice period is met),
    network_fault (live outages), severe_weather (the forecast cells above the gust threshold)."""
    return N.candidates(kind)


@router.get("/api/notify/candidate")
def candidate(id: str, channels: str = ""):
    """What a candidate would reach on the chosen channels (comma separated), before anything is created."""
    return N.preview_candidate(id, [c for c in channels.split(",") if c] or None)


@router.get("/api/notify/feeders")
def feeders():
    """Feeders a custom notice can cover."""
    return {"feeders": N.feeders_for_project()}


@router.delete("/api/notify/notices/{nid}")
def delete_draft(nid: str, request: Request):
    """Delete a draft notice and everything about it (its messages, exclusions, drafting run, and its place on a works plan)."""
    return N.delete_draft(nid, _actor(request))


@router.post("/api/notify/notices")
def create(body: dict, request: Request):
    """Create a draft notice: body {"candidate": "<candidate id>", "channels": [...]} for a planned outage, a fault or a weather hazard; or
    {"notice_type": "project_work", "feeders": [...], "start": "YYYY-MM-DDTHH:MM", "end": "...", "title", "description", "channels": [...]}
    for a custom notice. AI drafting starts in the background; nothing goes to customers until the notice is released."""
    return N.create_notice(body, _actor(request))


@router.get("/api/notify/notices")
def notices(status: str = "", limit: int = 100):
    """Notices, newest first; status is draft (being designed) or released (scheduled or sent); empty for all."""
    return {"notices": N.notices(status or None, limit)}


@router.get("/api/notify/notices/{nid}")
def notice(nid: str):
    """One notice in full: record, AI run, rules, exclusions (by reason, first 200 listed), messages once sent."""
    return N.detail(nid)


@router.get("/api/notify/notices/{nid}/customers")
def notice_customers(nid: str):
    """The customers the notice reaches: address, suburb, feeder, life support and the channels each is reached on (first 500)."""
    return N.notice_customers(nid)


@router.post("/api/notify/notices/{nid}/drafts")
def save_drafts(nid: str, body: dict, request: Request):
    """The planner's edits: body {"edits": [{"audience": "customer"|"life_support", "channel": "...", "text": "..."}]}, each logged."""
    return N.save_drafts(nid, body.get("edits") or [], _actor(request))


@router.post("/api/notify/notices/{nid}/redraft")
def redraft(nid: str, body: dict, request: Request):
    """Ask the AI for the drafts again, with an optional note: body {"note": "..."}."""
    return N.redraft(nid, _actor(request), body.get("note") or "")


@router.post("/api/notify/notices/{nid}/release")
def release(nid: str, body: dict, request: Request):
    """Release the notice: body {"when": "now"|"scheduled", "confirm": true}. Now writes the messages to the outbox (SMS and calls wait for
    8 am in quiet hours); scheduled sends a planned interruption's notice at 9 am on its notice date."""
    if body.get("confirm") is not True:
        raise ValueError("Confirm before releasing. The messages go to customers.")
    return N.release_notice(nid, body.get("when") or "now", _actor(request))
