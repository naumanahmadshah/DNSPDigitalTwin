"""API for Plan and invest (AI aided): /api/planning/... Drafts are asynchronous: a draft returns at once and the screen polls the step.
Mounted by main.py (app.include_router). Errors come back as 400 with the reason (the app's ValueError handler)."""
from fastapi import APIRouter, Body, Request

import planning as PL

router = APIRouter()


def _actor(req: Request):
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or "presenter"


# ---------------------------------------------------------------- programmes being designed
@router.get("/api/planning/runs")
def runs():
    """The programmes being designed (not yet released), most recently worked on first."""
    return dict(runs=PL.list_runs())


@router.post("/api/planning/runs")
def new_run(req: Request, body: dict = Body(default={})):
    return PL.run_state(PL.create_run(body.get("title", ""), _actor(req)))


@router.get("/api/planning/runs/{run_id}")
def run(run_id: str):
    return PL.run_state(run_id)


@router.post("/api/planning/runs/{run_id}/save")
def save(run_id: str, req: Request, body: dict = Body(default={})):
    return PL.save_run(run_id, _actor(req), body.get("step"))


@router.delete("/api/planning/runs/{run_id}")
def delete(run_id: str, req: Request):
    return PL.delete_run(run_id, _actor(req))


# ---------------------------------------------------------------- the steps
@router.post("/api/planning/runs/{run_id}/draft")
def draft(run_id: str, req: Request, body: dict = Body(...)):
    return PL.draft(run_id, body.get("step"), _actor(req), body.get("instruction") or "")


@router.get("/api/planning/runs/{run_id}/steps/{step}")
def step(run_id: str, step: str):
    return PL.step_view(run_id, step)


@router.post("/api/planning/runs/{run_id}/steps/{step}/edit")
def edit(run_id: str, step: str, req: Request, body: dict = Body(...)):
    return PL.edit(run_id, step, int(body.get("version")), body.get("edits") or [], _actor(req))


@router.post("/api/planning/runs/{run_id}/steps/{step}/accept")
def accept(run_id: str, step: str, req: Request, body: dict = Body(...)):
    return PL.accept(run_id, step, int(body.get("version")), _actor(req), body.get("reason") or "")


@router.post("/api/planning/runs/{run_id}/steps/{step}/discard")
def discard(run_id: str, step: str, req: Request):
    return PL.discard(run_id, step, _actor(req))


# ---------------------------------------------------------------- the release
@router.get("/api/planning/runs/{run_id}/release-preview")
def release_preview(run_id: str):
    return PL.release_preview(run_id)


@router.post("/api/planning/runs/{run_id}/selection")
def selection(run_id: str, req: Request, body: dict = Body(...)):
    """Save and continue later: {"included": {bundle_id: true or false}}."""
    return PL.save_selection(run_id, body.get("included") or {}, _actor(req))


@router.post("/api/planning/runs/{run_id}/prepare-notices")
def prepare_notices(run_id: str, req: Request, body: dict = Body(default={})):
    """Prepare and schedule the customer notices of the chosen bundles ({"bundle_ids": [...]}, default the kept ones)."""
    return PL.prepare_notices(run_id, body.get("bundle_ids"), _actor(req))


@router.post("/api/planning/runs/{run_id}/release")
def release(run_id: str, req: Request, body: dict = Body(default={})):
    """Release and schedule the plan: {"bundle_ids": [...], "notices": true} (default: the kept bundles, with their notices)."""
    return PL.release(run_id, _actor(req), body.get("bundle_ids"), bool(body.get("notices", True)))


# ---------------------------------------------------------------- released plans
@router.get("/api/planning/released")
def released():
    return PL.released_plans()


@router.get("/api/planning/released/{release_id}")
def released_plan(release_id: str):
    return PL.released_plan(release_id)


@router.delete("/api/planning/released/{release_id}")
def delete_released(release_id: str, req: Request):
    return PL.delete_release(release_id, _actor(req))
