"""Routes for asset photos (the field app sends them; Asset management reviews them). Mounted by main.py with app.include_router(routes_assets.router). Specific paths come before the {asset_id} wildcard."""
from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

import assets as A

router = APIRouter()


def _actor(req: Request):
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or "field"


@router.get("/api/assets/search")
def as_search(q: str = ""):
    """Assets by id or name, for tagging a photo (synced gold, read only)."""
    return dict(assets=A.search(q))


@router.get("/api/assets/photos")
def as_photos(asset_id: str = None, status: str = None, source: str = None, ref_id: str = None):
    """Photo metadata, newest first; the review queue is status=uploaded and status=ai_checked."""
    return dict(photos=A.photos(asset_id=asset_id, status=status, source=source, ref_id=ref_id))


@router.post("/api/assets/photo")
def as_submit(req: Request, body: dict = Body(...)):
    """Upload a photo (base64 JPEG or PNG, already resized on the phone). The vision check runs in the background."""
    return A.submit_photo(body.get("asset_id"), body.get("image_b64"), body.get("note") or "", _actor(req),
                          source=body.get("source") or "field", ref_id=body.get("ref_id") or None)


@router.get("/api/assets/photo/{photo_id}/image")
def as_image(photo_id: str):
    """The photo's bytes (404 once a rejected photo's bytes are deleted)."""
    got = A.image(photo_id)
    if not got:
        return Response(status_code=404, content=b"", media_type="text/plain")
    data, mime = got
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=300"})


@router.post("/api/assets/photo/{photo_id}/review")
def as_review(photo_id: str, req: Request, body: dict = Body(...)):
    """Accept as fixed, accept as updated photo, or reject (notes required)."""
    return A.review(photo_id, body.get("decision"), body.get("notes"), _actor(req))


@router.get("/api/assets/{asset_id}")
def as_asset(asset_id: str):
    """One asset: its register record, open defects, current accepted photo and photo history."""
    return A.asset_view(asset_id)
