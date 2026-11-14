"""Routes for the field status reports (the field app files them; Situations and Resources read them) and the Operations screen.
Mounted by main.py with app.include_router(routes_fieldops.router)."""
from fastapi import APIRouter, Body, Request

import fieldreport as FR
import operations as OPS

router = APIRouter()


def _actor(req: Request, crew_id=None):
    """The signed-in person when the request carries one (the control room), else the crew on the phone."""
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or (f"crew {crew_id}" if crew_id else "field")


# ---------------------------------------------------------------- field status reports
@router.get("/api/fieldreport/meta")
def fr_meta(job_id: str = None):
    """The form's choices (with labels) and the job's context: its asset, outage, and the photos already taken for it."""
    return FR.meta(job_id)


@router.get("/api/fieldreport/list")
def fr_list(situation_id: str = None, job_id: str = None, crew_id: str = None, region_id: str = None, limit: int = 50):
    """Field reports for a situation, a job, a crew or a region, newest first, with labels."""
    return dict(reports=FR.reports(situation_id=situation_id, job_id=job_id, crew_id=crew_id, region_id=region_id, limit=limit))


@router.post("/api/fieldreport/submit")
def fr_submit(req: Request, body: dict = Body(...)):
    """File a report from the field app. Validation errors come back as 400 with the message the crew sees."""
    return FR.submit(body, _actor(req, body.get("crew_id")))


# ---------------------------------------------------------------- operations (restoration, live, learn)
@router.get("/api/ops/restoration")
def ops_restoration():
    return OPS.restoration()


@router.get("/api/ops/outage/{outage_id}")
def ops_outage(outage_id: str):
    """One outage in full for its detail window."""
    return OPS.outage(outage_id)


@router.get("/api/ops/off-supply")
def ops_off_supply(life_support: bool = False):
    """The premises off supply now (life support customers only when asked), for the restoration tiles' detail."""
    return dict(premises=OPS.off_supply(life_support))


@router.get("/api/ops/live")
def ops_live():
    return OPS.live()


@router.get("/api/ops/learn")
def ops_learn():
    return OPS.learn()
