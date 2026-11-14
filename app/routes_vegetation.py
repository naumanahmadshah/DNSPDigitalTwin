"""Vegetation management routes (feature 6): the office API under /api/vegetation, the offline-first contractor API under
/api/vegetation/contractor, and the contractor's mobile page at /contractor. main.py includes this router. Logic is in app/vegetation.py."""
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import vegetation as V

router = APIRouter()
HERE = os.path.dirname(os.path.abspath(__file__))


def _actor(req: Request):
    """The signed-in person (the same header the rest of the app reads)."""
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or "presenter"


def _guard(fn, *args, **kwargs):
    """Turn a missing record into a 404; bad input keeps its status (ValueError is a 400 in main)."""
    try:
        return fn(*args, **kwargs)
    except V.NotFound as e:
        raise HTTPException(status_code=404, detail=f"Not found: {e}")


# ------------------------------------------------------------------ the contractor's page (mobile)
@router.get("/contractor")
def contractor_page():
    return FileResponse(os.path.join(HERE, "static", "contractor.html"), headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ office: steps 1 to 5
@router.get("/api/vegetation/capture")
def capture():
    return V.capture()


@router.get("/api/vegetation/region/{area}/feeders")
def region_feeders(area: str):
    """A region's feeders, most urgent first, for the drill-down from step 1."""
    return dict(feeders=V.region_feeders(area))


@router.get("/api/vegetation/feeder/{feeder_id}/spans")
def feeder_spans(feeder_id: str):
    """Every bay on a feeder in line order, with its clearance and risk tier."""
    return dict(spans=V.feeder_spans(feeder_id))


@router.get("/api/vegetation/map-spans")
def map_spans():
    """Every surveyed span as a short line for the network map's vegetation layer."""
    return V.map_spans()


@router.get("/api/vegetation/lidar/{span_id}")
def lidar(span_id: str):
    """One span's measured values for the LiDAR view: the span, its two poles and the latest survey's clearance."""
    r = V.lidar(span_id)
    return r or JSONResponse(status_code=404, content=dict(error="This span has no survey result."))


@router.get("/api/vegetation/risk")
def risk(limit: int = 200):
    """The feeders by risk and the riskiest bays, for the region summary."""
    return V.risk(min(max(limit, 1), 1000))


@router.get("/api/vegetation/candidates")
def candidates(limit: int = 300):
    return dict(bays=V.candidates(min(max(limit, 1), 1000)))


@router.get("/api/vegetation/packages")
def packages():
    return dict(packages=V.packages())


@router.post("/api/vegetation/packages")
def create_package(body: dict, req: Request):
    return _guard(V.create_package, body, _actor(req))


@router.get("/api/vegetation/released")
def released():
    """Every released work package, whatever its status, and the exceptions open on their bays."""
    return V.released()


@router.get("/api/vegetation/packages/{package_id}")
def package(package_id: str):
    """One released package and every bay in it."""
    return _guard(V.package_detail, package_id)


@router.get("/api/vegetation/contractors")
def contractors():
    """Every vegetation contractor crew with the work it holds."""
    return dict(crews=V.contractor_workload())


@router.get("/api/vegetation/crew/{crew_id}/work")
def crew_work(crew_id: str):
    """A vegetation contractor crew's packages and open bays (its drawer in Field staff management)."""
    return _guard(V.crew_work, crew_id)


@router.get("/api/vegetation/photo/{photo_id}/image")
def photo_image(photo_id: str):
    """A bay photo's picture."""
    data, mime = _guard(V.photo_image, photo_id)
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/vegetation/audits")
def audits(result: str = None, limit: int = 200):
    return V.audits(result, min(max(limit, 1), 1000))


@router.post("/api/vegetation/audits/{audit_id}/decide")
def decide(audit_id: str, body: dict, req: Request):
    return _guard(V.decide, audit_id, body.get("decision"), body.get("notes"), _actor(req), body.get("crew_id"))


@router.get("/api/vegetation/task/{task_id}")
def task(task_id: str):
    return _guard(V.task_detail, task_id)


# ------------------------------------------------------------------ contractor: step 4 (the crew's own bays only)
@router.get("/api/vegetation/contractor/crews")
def contractor_crews():
    return dict(crews=V.contractor_crews())


@router.get("/api/vegetation/contractor/bays")
def contractor_bays(crew_id: str):
    return _guard(lambda: dict(bays=V.crew_bays(crew_id)))


@router.get("/api/vegetation/contractor/task/{task_id}")
def contractor_task(task_id: str, crew_id: str):
    return _guard(V.task_detail, task_id, crew_id)


@router.post("/api/vegetation/contractor/sync")
def contractor_sync(body: dict):
    return _guard(V.sync, body.get("crew_id"), body.get("ops") or [], _crew_actor(body))


def _crew_actor(body):
    """The contractor has no sign-in of its own: the crew's name (from the crew record) is the actor on every write it makes. The
    client's own claim of who it is is ignored."""
    try:
        return f"{V._crew(body.get('crew_id') or '')['crew_name']} (contractor)"
    except V.NotFound:
        raise HTTPException(status_code=404, detail="Not found: crew")
