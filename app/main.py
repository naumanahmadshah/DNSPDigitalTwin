"""Asset Atlas, Electricity Distribution: the Databricks App (FastAPI back end serving a vanilla JavaScript front end)."""
import json
import os
import threading
import time
import traceback

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import core
import open_outages
import overdue
import scene
import situations
import views
from core import pg
from rm import data as RD, dispatch as RX, field as RF, plan as RP, store
import routes_assets
import routes_customers
import routes_fieldops
import routes_notify
import routes_planning
import routes_vegetation

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Asset Atlas, Electricity Distribution")


@app.on_event("startup")
def _open_outages():
    """The outages in progress at the snapshot, written into the live register and kept anchored to now (open_outages.py)."""
    open_outages.start()
    overdue.start()   # crews sent to emergencies are watched until they report


for _r in (routes_assets, routes_customers, routes_fieldops, routes_notify, routes_planning, routes_vegetation):
    app.include_router(_r.router)   # each feature keeps its routes beside its logic
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
@app.middleware("http")
async def _cache_headers(req, call_next):
    """Scripts and styles are never cached (a cached module kept old code running after a redeploy); models and images cache for a week."""
    resp = await call_next(req)
    p = req.url.path
    if p.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=604800" if p.endswith((".glb", ".png", ".webp", ".jpg", ".svg")) else "no-store"
        if p == "/static/field-sw.js":   # the field app's pass-through worker controls /field, outside its own folder
            resp.headers["Service-Worker-Allowed"] = "/field"
    return resp


def actor(req: Request):
    return req.headers.get("x-forwarded-email") or req.headers.get("x-forwarded-preferred-username") or "presenter"


@app.exception_handler(situations.NotFound)
async def _not_found(req, exc):
    return JSONResponse(status_code=404, content=dict(error="not_found", detail=f"No such situation or recommendation: {exc}."))


@app.exception_handler(ValueError)
async def _bad_request(req, exc):
    return JSONResponse(status_code=400, content=dict(error="bad_request", detail=str(exc)))


@app.exception_handler(Exception)
async def _err(req, exc):
    traceback.print_exc()
    return JSONResponse(status_code=500, content=dict(error=type(exc).__name__, detail=str(exc)[:400]))


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/field")
def field():
    return FileResponse(os.path.join(HERE, "static", "field.html"), headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(HERE, "static", "icons", "favicon-32.png"), media_type="image/png")


@app.get("/api/meta")
def meta():
    return dict(engine=views.engine_state(), genie=core.genie_link())


# ------------------------------------------------------------------ Command Centre and situations
@app.get("/api/cc")
def cc():
    return views.command_centre()


@app.get("/api/finance")
def finance():
    return views.finance()


@app.get("/api/situations")
def sit_list(status: str = "open"):
    return situations.listing(status)


@app.get("/api/situation/{sid:path}")
def sit_detail(sid: str):
    return situations.detail(sid)


@app.post("/api/situation/{sid:path}/plan")
def sit_plan(sid: str, req: Request):
    return situations.plan(sid, actor(req))


@app.post("/api/situation/{sid:path}/undo")
def sit_undo(sid: str, req: Request, body: dict = Body(...)):
    try:
        return situations.undo(sid, body["rec_id"], actor(req))
    except ValueError as e:   # past the undo window: the messages have gone out
        return JSONResponse(status_code=409, content=dict(error="too_late", detail=str(e)))


@app.post("/api/situation/{sid:path}/decide")
def sit_decide(sid: str, req: Request, body: dict = Body(...)):
    return situations.decide(sid, body["rec_id"], body["action"], actor(req), body.get("reason"), body.get("crew_id"), body.get("message"),
                             body.get("audience"))


# ------------------------------------------------------------------ maps and spatial
@app.get("/api/map/network")
def m_network():
    return views.map_network()


@app.get("/api/map/live")
def m_live():
    return views.map_live()


@app.get("/api/map/fire")
def m_fire():
    return views.map_fire()


@app.get("/api/map/weather")
def m_weather(t: str = None):
    return views.map_weather(t)


# ------------------------------------------------------------------ Network, Operations, Plan, Customers, Explorer
@app.get("/api/network/{stop}")
def network(stop: str):
    return views.network(stop)


@app.get("/api/zone-customers/{zs}")
def zone_customers(zs: str):
    return views.zone_customers(zs)


@app.get("/api/series/transformer/{tx}")
def s_tx(tx: str):
    return views.series_transformer(tx)


@app.get("/api/series/feeder/{fid}")
def s_feeder(fid: str):
    return views.series_feeder(fid)


@app.get("/api/search")
def search(q: str = ""):
    return views.search(q)


@app.get("/api/asset/{aid}")
def asset(aid: str):
    a = views.asset(aid)
    return a or JSONResponse(status_code=404, content=dict(error="not found"))


# ------------------------------------------------------------------ 3D
@app.post("/api/spatial/area")
def spatial_area(body: dict):
    return views.area_impact(body)


@app.post("/api/notify/area")
def notify_area(body: dict, request: Request):
    return views.notify_area(body.get("area") or {}, body.get("body"), body.get("channels") or [], body.get("audience"), actor(request))


@app.get("/api/impact")
def impact_api():
    return views.impact()


@app.get("/api/impact/assets")
def impact_assets():
    return views.impact_assets()


@app.get("/api/scene-sites")
def scene_sites():
    return scene.sites()


@app.get("/api/scene/{kind}/{eid}")
def scene_get(kind: str, eid: str):
    return scene.for_entity(kind, eid)


@app.post("/api/scene/speech")
def scene_speech(body: dict = Body(...)):
    return dict(text=scene.speech(body.get("asset_id"), body.get("facts") or {}))


# ------------------------------------------------------------------ Resources (field staff management)
@app.get("/api/rm/meta")
def rm_meta():
    d = RD.data()
    return dict(regions=[dict(id=r["region_id"], name=r["name"], area=r["area"]) for r in d["regions"]], day=d["day"])


def _roster_out(region, r, limit):
    j = RD.jobs_by_id()
    for a in r["assignments"]:
        a["job_type_label"] = RD.label("job_type", a["job_type"])
        a["address"] = (j.get(a["job_id"]) or {}).get("address")
    return dict(region=region, kpis=r["kpis"], assignments=r["assignments"][:limit], assignments_total=len(r["assignments"]),
                deferred=[dict(job_id=x["job_id"], title=x["title"], job_type_label=RD.label("job_type", x["job_type"]), priority=x["priority"],
                               est_customer_minutes=x["est_customer_minutes"]) for x in r["deferred"][:200]], deferred_total=len(r["deferred"]))


@app.get("/api/rm/roster")
def rm_roster(region: str, limit: int = 1000):
    return _roster_out(region, RP.solve(region), limit)


@app.get("/api/rm/feeders")
def rm_feeders(region: str):
    return dict(feeders=[f for f in RD.data()["feeders"] if f["region_id"] == region])


@app.get("/api/rm/crew-summary")
def rm_crew_summary(region: str):
    c = RD.eng().crew_for_region(region)
    by = {}
    for x in c:
        by[x["persona"]] = by.get(x["persona"], 0) + 1
    return dict(region=region, available=len(c), contractors=sum(1 for x in c if x["employment_type"] == "Contractor"), by_persona=by)


@app.get("/api/rm/positions")
def rm_positions(region: str = None):
    return dict(crews=RP.positions(region))


@app.get("/api/rm/crew/{crew_id}")
def rm_crew(crew_id: str):
    d = RX.crew_detail(crew_id)
    return d or JSONResponse(status_code=404, content=dict(error="No crew with that ID is on the roster."))


@app.get("/api/rm/parked")
def rm_parked(region: str = None):
    items = store.parked_list(region)
    for p in items:
        p["job_type_label"] = RD.label("job_type", p.get("job_type"))
    return dict(parked=items, count=len(items))


@app.post("/api/rm/redispatch")
def rm_redispatch(req: Request, body: dict = Body(...)):
    return RX.redispatch(body["region"], body["job_id"], body.get("crew_id"), actor(req))


@app.get("/api/rm/candidates")
def rm_candidates(region: str, job_id: str):
    return RX.candidates(region, job_id)


@app.post("/api/rm/plan-assign")
def rm_plan_assign(body: dict = Body(...)):
    return RX.plan_assign(body["region"], body["job_id"], body["crew_id"])


@app.post("/api/rm/apply-assign")
def rm_apply_assign(req: Request, body: dict = Body(...)):
    return RX.apply_assign(body["region"], body["job_id"], body["crew_id"], body.get("handoffs") or [], bool(body.get("override")),
                           body.get("breach_reason"), actor(req))


@app.post("/api/rm/hold")
def rm_hold(req: Request, body: dict = Body(...)):
    return RX.park(body["region"], body["job_id"], body.get("crew_id"), "dispatcher_hold", body.get("reason_label") or "On hold (dispatcher)", actor(req))


@app.get("/api/rm/policy")
def rm_policy(region: str):
    return RX.get_policy(region)


@app.post("/api/rm/policy")
def rm_set_policy(req: Request, body: dict = Body(...)):
    pol = dict(incident_all=bool(body.get("incident_all")), incident_allow=body.get("incident_allow") or [],
               fatigue_all=bool(body.get("fatigue_all")), fatigue_allow=body.get("fatigue_allow") or [])
    return RX.set_policy(body["region"], pol, actor(req))


@app.get("/api/rm/exec")
def rm_exec():
    return RP.exec_summary()


@app.get("/api/rm/expiring")
def rm_expiring(days: int = 45):
    return RP.expiring(days)


@app.get("/api/rm/expiring/detail")
def rm_expiring_detail(ticket_code: str = None, scope: str = "at_risk", days: int = 45, offset: int = 0, limit: int = 200):
    return RP.expiring_detail(ticket_code, scope, days, offset, limit)


@app.get("/api/rm/rerostered")
def rm_rerostered():
    return RX.rerostered()


@app.get("/api/rm/overrides")
def rm_overrides():
    return RX.overrides()


@app.get("/api/rm/crew-roster")
def rm_crew_roster(region: str):
    return RX.crew_roster(region)


@app.post("/api/rm/move-region")
def rm_move(req: Request, body: dict = Body(...)):
    return RX.move_region(body["crew_id"], body["to_region"], actor(req))


@app.post("/api/rm/reset-region")
def rm_reset(req: Request, body: dict = Body(...)):
    return RX.reset_region(body["crew_id"], actor(req))


@app.post("/api/rm/stand-down")
def rm_stand_down(req: Request, body: dict = Body(...)):
    return RX.stand_down(body["region"], body["crew_id"], body.get("reason_label") or "Stood down by the supervisor", actor(req))


@app.post("/api/rm/restore-crew")
def rm_restore(req: Request, body: dict = Body(...)):
    return RX.restore_crew(body["region"], body["crew_id"], actor(req))


@app.post("/api/rm/job")
def rm_job(req: Request, body: dict = Body(...)):
    lat, lon = body.get("lat"), body.get("lon")
    if lat is None:   # a feeder or asset without coordinates: the feeder's centre, or the asset's location
        f = next((f for f in RD.data()["feeders"] if f["feeder_id"] == body.get("feeder_id")), None)
        a = None if f else core.pq("SELECT lat, lon FROM gld_asset WHERE asset_id = %(a)s", {"a": body.get("asset_id")}, ttl=3600)
        lat, lon = (f["lat"], f["lon"]) if f else (a[0]["lat"], a[0]["lon"]) if a else (None, None)
    if lat is None:
        return JSONResponse(status_code=400, content=dict(error="This job has no location. Give it coordinates, or a feeder or asset with a known location."))
    return RX.raise_job(body["job_type"], body["title"], float(lat), float(lon), actor(req), feeder_id=body.get("feeder_id"),
                        asset_id=body.get("asset_id"), nmi=body.get("nmi"), priority=body.get("priority", 4), customers=body.get("customers", 0),
                        emergency=bool(body.get("emergency")), duration_min=body.get("duration_min"))


@app.get("/api/rm/nearest")
def rm_nearest(job_id: str):
    return dict(crews=RX.nearest(job_id))


@app.post("/api/rm/dispatch")
def rm_dispatch(req: Request, body: dict = Body(...)):
    return RX.dispatch(body["job_id"], body["crew_id"], actor(req))


@app.get("/api/rm/phone-link")
def rm_phone_link():
    r = pg.q("SELECT value, updated_at FROM app.live_state WHERE key = 'phone_link'", one=True)
    return dict((r or {}).get("value") or {}, at=(r or {}).get("updated_at"))


@app.post("/api/rm/phone-link")
def rm_set_phone_link(req: Request, body: dict = Body(...)):
    """The phone relay (tools/field_demo.sh) shares its public field-app link and QR code here for the Phone demo dialog."""
    pg.q("""INSERT INTO app.live_state (key, value) VALUES ('phone_link', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
         (json.dumps(dict(url=body.get("url"), qr=body.get("qr_png_b64"))),))
    core.audit(actor(req), "phone_link", body.get("url"), {})
    return dict(ok=True)


@app.get("/api/rm/job-types")
def rm_job_types():
    return dict(types=[dict(code=k, label=RD.label("job_type", k)) for k in sorted(RD.ensure()["specs"])])


# ------------------------------------------------------------------ field app
@app.get("/api/field/meta")
def f_meta():
    return RF.meta()


@app.get("/api/field/workers")
def f_workers(region: str):
    return RF.workers(region)


@app.get("/api/field/itinerary")
def f_itinerary(region: str, crew_id: str = None):
    return RF.itinerary(region, crew_id)


@app.post("/api/field/park")
def f_park(req: Request, body: dict = Body(...)):
    return RX.park(body["region"], body["job_id"], body.get("crew_id"), body.get("reason", "blocked"), body.get("reason_label"), actor(req))


@app.post("/api/field/unpark")
def f_unpark(body: dict = Body(...)):
    return RX.unpark(body["region"], body["job_id"])


@app.post("/api/field/reorder")
def f_reorder(body: dict = Body(...)):
    return RF.reorder(body["region"], body["crew_id"], body["order"])


@app.post("/api/field/status")
def f_status(req: Request, body: dict = Body(...)):
    return RF.status(body["region"], body["job_id"], body["status"], actor(req))


@app.post("/api/field/reassign")
def f_reassign(req: Request, body: dict = Body(...)):
    return RX.reassign(body["region"], body["crew_id"], body.get("job_ids") or [], body.get("reason_label"), bool(body.get("stand_down")), actor(req))


# ------------------------------------------------------------------ demo controls (the top left of the screen)
@app.post("/api/presenter/{cmd}")
def presenter(cmd: str, req: Request, body: dict = Body(default={})):
    """go_live starts the time-boxed live run (60 minutes unless asked otherwise); stop_live ends it early; reset puts the demo back to the
    snapshot's state."""
    a = actor(req)
    if cmd == "go_live":
        active = core.live_run()
        if active:   # one live run at a time: a second press while starting or live does nothing
            return dict(ok=True, run_id=active["run_id"], already=True)
        run = core.run_job(core.LIVE_JOB, {"minutes": str(body.get("minutes", 60))})
        core._live_run.update(at=time.time(), run=dict(run_id=run, state="PENDING", start_ms=None))
        if core.FEEDS_JOB:
            try:
                core.run_job(core.FEEDS_JOB)
            except Exception:  # noqa: BLE001 - feeds may already be running
                pass
        core.audit(a, "go_live", "engine", dict(run=run))
        return dict(ok=True, run_id=run)
    if cmd == "stop_live":
        # a ticking engine sees the flag, writes what it holds and ends its run; a run still starting up is cancelled at once, and a
        # run that has not ended 45 seconds after the flag is cancelled too
        ticking = views.engine_state()["phase"] == "live"
        run = core.live_run()
        pg.q("""UPDATE app.live_state SET value = value || '{"status": "stopping"}'::jsonb, updated_at = now()
                WHERE key = 'engine' AND value ->> 'status' = 'live'""")
        if run:
            threading.Timer(45 if ticking else 0, core.cancel_if_running, args=(run["run_id"],)).start()
        core.stop_feeds()
        core._live_run.update(at=0)
        core.audit(a, "stop_live", "engine", dict(run=run and run["run_id"]))
        return dict(ok=True)
    if cmd == "reset":
        pg.q("UPDATE atlas.situation SET status = 'closed' WHERE type = 'trip'")
        pg.q("DELETE FROM atlas.live_outage")
        pg.q("DELETE FROM rm.raised_job")
        for t in ("stood_down", "crew_live", "field_status", "assignment_override", "parked"):
            pg.q(f"DELETE FROM rm.{t}")
        store.bump("__jobs__")
        pg.q("DELETE FROM atlas.notification_outbox")
        open_outages.seed()   # back to the snapshot's outages in progress
        core.audit(a, "reset", "demo", {})
        return dict(ok=True)
    return JSONResponse(status_code=400, content=dict(error="That command is not recognised."))
