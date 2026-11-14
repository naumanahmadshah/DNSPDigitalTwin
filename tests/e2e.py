#!/usr/bin/env python3
"""End-to-end test suite for the Asset Atlas, Electricity Distribution Databricks App.

It drives the deployed app's /api endpoints (and the /field page) the way the UI does, with a bearer token from the Databricks CLI.
Every test asserts real behaviour: status codes AND content (non-empty rows, expected keys, numbers in range, and state changes
visible on a follow-up GET). Tests that change state restore it in a deferred cleanup that runs even when the test fails.

Usage:
    python3 tests/e2e.py --app https://<app-url> --profile <databricks-cli-profile> [--only <group>[,<group>]] [--keep-going] [--live]
                         [--full-planning]

Groups, in the order they run: meta, situations, network, scene, resources, field, hierarchy, notices, customers, operations,
fieldreports, assets, planning, vegetation, search, demo.
The demo group runs last because its reset clears runtime state (raised jobs, stand-downs, overrides, live outages).
The planning group drafts only the forecast step by default (Opus, about a minute) and deletes its programme at the end. --full-planning
drafts and accepts the four AI steps and releases two bundles of the works plan, which raises jobs in resource management that the app
cannot withdraw (see the planning group notes).
Records the new groups create (notices, planning runs, field reports, photos, vegetation packages) have no delete API: the tests
restore what they can through the API and report the residue in their notes.

Result codes:
    PASS    the assertions held
    FAIL    an assertion failed (the run stops at the first FAIL unless --keep-going is given)
    KNOWN   the test failed on an app bug listed in KNOWN_BUGS (reported, does not fail the run)
    SKIP    a precondition is absent in the live data (the reason is printed)
    INFO    an informational probe (never fails)

Exit status is 1 if any test FAILs, otherwise 0.

Standard library only, plus `requests` when it is installed (urllib is the fallback).
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests   # optional: faster keep-alive session
except ImportError:   # pragma: no cover - the urllib path is exercised instead
    requests = None


GROUP_ORDER = ["meta", "situations", "network", "scene", "resources", "field", "hierarchy", "notices", "customers", "operations",
               "fieldreports", "assets", "planning", "vegetation", "search", "demo"]
GROUP_TITLES = {
    "meta": "Meta and Command Centre",
    "situations": "Situations and recommendations",
    "network": "Network, maps, spatial and notify area",
    "scene": "3D scenes and speech",
    "resources": "Resource management",
    "field": "Field app",
    "hierarchy": "Transmission level and the network hierarchy",
    "notices": "Customer notices (notification engine)",
    "customers": "Customers: connection applications and existing",
    "operations": "Operations: restoration, live, learn",
    "fieldreports": "Field status reports",
    "assets": "Asset photos and review",
    "planning": "Plan and invest (AI aided, Opus)",
    "vegetation": "Vegetation management (office and contractor)",
    "search": "Search (Asset management)",
    "demo": "Demo controls: live data ingestion and reset (runs last)",
}

# Known application bugs. A test whose id is listed here (via known_bug=) is reported as KNOWN when it fails, not as FAIL.
KNOWN_BUGS = {}

# Expected top-level view keys for each situation type (from app/situations.py _detail_<type>).
VIEW_KEYS = {
    "trip": {"outage", "devices", "crews", "job", "calls", "meter_alarms", "backup"},
    "fire": {"fire", "bands", "feeders", "span_risk", "spatial_sql", "fire_danger"},
    "heat": {"history", "dga", "forecast", "ties", "state", "live"},
    "min_load": {"solar", "transformers"},
    "vegetation": {"packages", "spans"},
    "drone_defect": {"pole", "detection", "bundle", "image"},
    "fatigue": {"crews"},
    "records": {"meters"},
    "storm": {"days", "peak_day", "top_feeders", "capacity", "windows", "cells", "model_version", "ls_on_top"},
}
SEVERITIES = {"critical", "high", "medium", "info"}
SCENE_KINDS = {"Overhead street", "Underground estate", "Bushfire-prone line"}
REGION_IDS = ["BLK", "CST", "NAR", "PEN", "RNG"]
CUSTOMER_REGIONS = ["BLK", "PEN", "RNG"]   # regions used by the mutating tests (their state is restored)


# ============================================================================ HTTP client
class Resp:
    """One HTTP response: the status and the body (parsed JSON when it parses, otherwise text)."""

    def __init__(self, status, raw):
        self.status = status
        self.raw = raw
        try:
            self.body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            self.body = raw.decode("utf-8", "replace") if raw else None

    @property
    def ok(self):
        return 200 <= self.status < 300

    def __repr__(self):
        return f"Resp({self.status}, {brief(self.body, 160)})"


class Client:
    """Calls the app with a bearer token. The token comes from `databricks auth token` and is refreshed on a 401."""

    def __init__(self, app, profile, timeout=240):
        self.app = app.rstrip("/")
        self.profile = profile
        self.timeout = timeout
        self.calls = 0
        self.token = self._fetch_token()
        self.session = requests.Session() if requests is not None else None

    def _fetch_token(self):
        cmd = ["databricks", "auth", "token", "-p", self.profile, "-o", "json"]
        out = subprocess.run(cmd, env=dict(os.environ, DATABRICKS_AUTH_STORAGE="plaintext"), capture_output=True, text=True)   # the CLI v1.x file cache
        if out.returncode != 0:   # or a login kept in the system keychain (the CLI's default store)
            out = subprocess.run(cmd, env={k: v for k, v in os.environ.items() if k != "DATABRICKS_AUTH_STORAGE"}, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"databricks auth token failed for profile {self.profile}: {out.stderr.strip()[:300]}")
        return json.loads(out.stdout)["access_token"]

    def _send(self, method, path, body):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self.app + path
        if self.session is not None:
            r = self.session.request(method, url, headers=headers, data=data, timeout=self.timeout)
            return Resp(r.status_code, r.content)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read())

    def call(self, method, path, body=None):
        self.calls += 1
        for attempt in range(3):   # a dropped connection or a gateway error is retried twice (mutations are retried only when safe)
            try:
                resp = self._send(method, path, body)
            except Exception:
                if attempt == 2 or method != "GET":
                    raise
                time.sleep(2)
                continue
            if resp.status in (502, 503, 504) and attempt < 2 and method == "GET":
                time.sleep(2)
                continue
            break
        if resp.status == 401:   # the token is about an hour old: fetch a fresh one and retry once
            self.token = self._fetch_token()
            resp = self._send(method, path, body)
        return resp

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, {} if body is None else body)

    def delete(self, path):
        return self.call("DELETE", path)


# ============================================================================ assertions and helpers
class Fail(Exception):
    """An assertion about the app's behaviour did not hold."""


class Skip(Exception):
    """A precondition is not in the live data (for example no open trip to plan)."""


class Info(Exception):
    """An informational probe: recorded, never a failure."""


def brief(x, n=300):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "..."


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def want(resp, code=200, what="request"):
    """The body of a response that must have the given status; a Fail with the body otherwise."""
    if resp.status != code:
        raise Fail(f"{what}: HTTP {resp.status}, expected {code}: {brief(resp.body, 400)}")
    return resp.body


def q(text):
    """Percent-encode a path segment the way the UI does (encodeURIComponent), so ids with slashes route correctly."""
    return urllib.parse.quote(str(text), safe="")


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_count(text, pattern):
    m = re.search(pattern, text or "")
    return int(m.group(1).replace(",", "")) if m else None


TESTS = []


def test(group, known_bug=None):
    """Register a test function in a group. known_bug names an entry in KNOWN_BUGS when the test is expected to fail on an app bug."""
    def deco(fn):
        doc = (fn.__doc__ or fn.__name__).strip().split("\n")[0]
        TESTS.append(dict(group=group, name=fn.__name__, fn=fn, known_bug=known_bug, doc=doc))
        return fn
    return deco


class Ctx:
    """What a test sees: the client, the shared state, notes for the report, and deferred cleanups (run even on failure)."""

    def __init__(self, client, state):
        self.c = client
        self.S = state
        self.notes = []
        self._cleanups = []

    def note(self, text):
        self.notes.append(str(text))

    def defer(self, fn, label="cleanup"):
        self._cleanups.append((label, fn))

    def run_cleanups(self):
        errors = []
        for label, fn in reversed(self._cleanups):
            try:
                fn()
            except Exception as e:   # a failed restore is itself a failure of the test
                errors.append(f"{label}: {type(e).__name__}: {e}")
        self._cleanups = []
        return errors


# ============================================================================ shared lookups
def open_situations(x):
    return want(x.c.get("/api/situations"), 200, "situation list")


def find_sit(x, typ, pred=None):
    for s in open_situations(x):
        if s["type"] == typ and (pred is None or pred(s)):
            return s
    return None


def sit_detail(x, sid):
    return want(x.c.get(f"/api/situation/{q(sid)}"), 200, f"situation detail {sid}")


def sit_plan(x, sid):
    return want(x.c.post(f"/api/situation/{q(sid)}/plan"), 200, f"plan {sid}")


def sit_planned(x, sid):
    """The situation's detail with its recommendations, planning it first when it has none (as the UI does when it opens it)."""
    d = sit_detail(x, sid)
    return d if d["recommendations"] else sit_plan(x, sid)


def sit_decide(x, sid, rec_id, action, **kw):
    return x.c.post(f"/api/situation/{q(sid)}/decide", dict(rec_id=rec_id, action=action, **kw))


def sit_undo(x, sid, rec_id):
    return x.c.post(f"/api/situation/{q(sid)}/undo", dict(rec_id=rec_id))


def rec_by_kind(detail, kind):
    return next((r for r in detail["recommendations"] if r["kind"] == kind), None)


def rec_by_id(detail, rec_id):
    r = next((r for r in detail["recommendations"] if r["rec_id"] == rec_id), None)
    check(r is not None, f"recommendation {rec_id} is missing from the situation")
    return r


def outbox_total(detail):
    return sum(int(r["n"] or 0) for r in detail.get("outbox") or [])


def roster(x, region, limit=1000):
    return want(x.c.get(f"/api/rm/roster?region={region}&limit={limit}"), 200, f"roster {region}")


def find_jobs(x, title_prefix, regions=None):
    """Jobs whose title starts with the prefix, across the regions (assigned or on the deferred list)."""
    hits = []
    for reg in regions or REGION_IDS:
        r = roster(x, reg)
        for a in list(r["assignments"]) + list(r["deferred"]):
            if str(a.get("title", "")).startswith(title_prefix):
                hits.append((reg, a))
    return hits


def positions(x, region):
    return {p["crew_id"]: p for p in want(x.c.get(f"/api/rm/positions?region={region}"), 200, "positions")["crews"]}


def region_of_feeder(x, feeder_id):
    for reg in REGION_IDS:
        for f in want(x.c.get(f"/api/rm/feeders?region={reg}"), 200, "feeders")["feeders"]:
            if f["feeder_id"] == feeder_id:
                return reg, f
    return None, None


def settle(x, sid, rec_id):
    """Cleanup helper: take a recommendation back to proposed whatever state it is in (undo is a no-op on proposed)."""
    d = sit_detail(x, sid)
    r = next((r for r in d["recommendations"] if r["rec_id"] == rec_id), None)
    if r and r["status"] != "proposed":
        sit_undo(x, sid, rec_id)


def ids(x, region):
    """Crew ids on a region's crew roster."""
    return {c["crew_id"] for c in want(x.c.get(f"/api/rm/crew-roster?region={region}"), 200, "crew roster")["crew"]}


def poll_until(cond, tries=5, wait=1.0):
    """Read until the condition holds (state can take a moment to show on every read); returns the read count, or None."""
    for i in range(1, tries + 1):
        if cond():
            return i
        if i < tries:
            time.sleep(wait)
    return None


def get_policy_now(x, region):
    return want(x.c.get(f"/api/rm/policy?region={region}"), 200, "policy")["policy"]


# ============================================================================ helpers for the November 2026 groups
AI_KEYS = ("asset_class_seen", "matches_tag", "condition", "defects", "looks_fixed", "suggested_outcome", "reasons")
SPINE_STOPS = ["bulk_supply", "transmission_substations", "zone_substations", "hv_feeders", "distribution_lv", "customers_der"]
STAGE_CODES = ["apply", "assess", "offer", "design", "build", "commission", "connected"]
NOTICE_RULES = {"notice_period", "isolation_scope", "duplicate_24h", "safety_messages", "life_support_first", "quiet_hours",
                "channel_preference"}
NOTICE_CHANNELS = {"sms", "email", "app", "mail", "contact_centre"}
FR_OUTCOMES = ["fixed", "temporary_repair", "follow_up"]
FR_HAZARDS = {"live_wire", "tree", "flood", "fire", "traffic", "unstable_pole"}
FR_FAULT_KINDS = {"pole", "span", "cable", "transformer", "none"}
FR_MATERIALS = {"cross_arm", "conductor", "insulator", "fuse_link", "pole", "lv_cable", "tie_wire"}
PLAN_STEP_KEYS = ["forecast", "constraints", "optimise", "bundles", "release"]
PNG_SIG = b"\x89PNG\r\n\x1a\n"
DASHES = (chr(0x2014), chr(0x2013))   # em and en dashes, built from code points so this file contains none
STAMP = time.strftime("%Y%m%d-%H%M%S")   # names the records this run creates, so reruns never collide
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFECT_PNG = os.path.join(REPO, "app", "static", "drone", "defect.png")


def need(x, key):
    """The state an earlier test in the group left behind; a Skip (not a failure) when that earlier test did not run."""
    v = x.S.get(key)
    if not v:
        raise Skip(f"needs the earlier test that sets '{key}' (it was skipped or did not pass)")
    return v


def dash_free(text, what):
    check(not any(d in str(text) for d in DASHES), f"{what} contains an em or en dash")


def key_anywhere(obj, word):
    """True when some dict key anywhere in obj contains the word (used to prove that no confidence figure is returned)."""
    if isinstance(obj, dict):
        return any(word in str(k).lower() or key_anywhere(v, word) for k, v in obj.items())
    if isinstance(obj, list):
        return any(key_anywhere(v, word) for v in obj)
    return False


def wait_for(fn, timeout, interval=5.0):
    """Call fn until it returns a truthy value or the timeout passes. Returns (value or None, seconds waited)."""
    t0 = time.time()
    while True:
        v = fn()
        if v:
            return v, time.time() - t0
        if time.time() - t0 >= timeout:
            return None, time.time() - t0
        time.sleep(interval)


def png_b64():
    """The defect photo from app/static/drone as base64. The field app sends JPEG or PNG; this one is PNG (no image library here to re-encode)."""
    with open(DEFECT_PNG, "rb") as f:
        raw = f.read()
    check(raw[:8] == PNG_SIG, f"{DEFECT_PNG} is not a PNG")
    return base64.b64encode(raw).decode("ascii"), len(raw)


def photo_row(x, asset_id, photo_id):
    """One photo of an asset, from the asset's photo list (None when absent)."""
    rows = want(x.c.get(f"/api/assets/photos?asset_id={q(asset_id)}"), 200, f"photos of {asset_id}")["photos"]
    return next((p for p in rows if p["photo_id"] == photo_id), None)


def notice_detail(x, nid):
    return want(x.c.get(f"/api/notify/notices/{q(nid)}"), 200, f"notice {nid}")


def pick_fr_job(x):
    """A Blacktown job on the roster that has a crew. A job raised by a situation is preferred, so the situation filter can be checked."""
    jobs = [a for a in roster(x, "BLK")["assignments"] if a.get("job_id") and a.get("crew_id")]
    if not jobs:
        raise Skip("no Blacktown job with a crew on the roster")
    first = None
    for a in jobs[:15]:
        m = want(x.c.get(f"/api/fieldreport/meta?job_id={q(a['job_id'])}"), 200, "field report meta")
        sit = (m.get("job") or {}).get("situation_id")
        if sit:
            return a, sit
        first = first or a
    return first, None


def pick_candidate(x, channels="sms,email,app,mail"):
    """The first eligible candidate that reaches at least one customer on the channels: a released plan's planned outage without a notice,
    else a live fault. Returns (candidate id, its preview), or (None, None)."""
    tried = 0
    for kind in ("planned_work", "network_fault"):
        for c in want(x.c.get(f"/api/notify/candidates?kind={kind}"), 200, f"{kind} candidates")["items"]:
            if c.get("eligible") is False or c.get("notice"):
                continue
            p = want(x.c.get(f"/api/notify/candidate?id={q(c['id'])}&channels={channels}"), 200, "candidate preview")
            if p["counts"]["reached"] > 0 and not p.get("notice_id"):
                return c["id"], p
            tried += 1
            if tried >= 6:
                break
    return None, None


def ts_rows(x):
    return want(x.c.get("/api/network/transmission_substations"), 200, "transmission substations")["rows"]


def plan_draft_and_wait(x, rid, step, timeout):
    """Start an Opus draft of one planning step and poll its version until it is drafted. Returns (the step view, version, seconds)."""
    d = want(x.c.post(f"/api/planning/runs/{rid}/draft", {"step": step}), 200, f"draft {step}")
    ver = d["version"]
    check(d["status"] == "drafting", f"draft {step} returned status {d['status']}")

    def done():
        v = want(x.c.get(f"/api/planning/runs/{rid}/steps/{step}?version={ver}"), 200, f"{step} step")
        return v if v["status"] in ("drafted", "failed") else None
    v, secs = wait_for(done, timeout, 5.0)
    if v is None:
        raise Fail(f"{step} draft did not finish within {timeout} s")
    check(v["status"] == "drafted", f"{step} draft failed: {v.get('error')}")
    return v, ver, secs


def veg_cycle(x, crew_id, task_id, tag, exception_kind=None):
    """One contractor cycle on a bay, sent the way the contractor app sends it (one sync of the queued actions): start, a photo, an
    exception (optional), complete. Returns the audit row the completion wrote, as the office's audit list shows it."""
    png, _ = png_b64()
    ops = [dict(type="start", task_id=task_id, client_op_id=f"e2e-{STAMP}-{tag}-start"),
           dict(type="photo", task_id=task_id, image_b64=png, kind="after", note=f"E2E test photo {tag}", client_op_id=f"e2e-{STAMP}-{tag}-photo")]
    if exception_kind:
        ops.append(dict(type="exception", task_id=task_id, kind=exception_kind, detail=f"E2E test {tag}", client_op_id=f"e2e-{STAMP}-{tag}-exc"))
    ops.append(dict(type="complete", task_id=task_id, notes=f"E2E test {tag}: cut complete", client_op_id=f"e2e-{STAMP}-{tag}-done"))
    res = want(x.c.post("/api/vegetation/contractor/sync", {"crew_id": crew_id, "ops": ops}), 200, f"sync {tag}")["results"]
    check(len(res) == len(ops) and all(r["ok"] for r in res), f"sync {tag}: {brief(res, 300)}")
    ph = next(r for r in res if r["type"] == "photo")
    check(ph["photo_id"].startswith("VPH-") and ph["duplicate"] is False, f"photo {tag}: {brief(ph, 160)}")
    x.S["veg"]["photos"].append(ph["photo_id"])
    check(next(r for r in res if r["type"] == "complete")["suggested"] in ("compliant", "non_compliant"), f"complete {tag}: {brief(res, 200)}")
    d = want(x.c.get(f"/api/vegetation/contractor/task/{q(task_id)}?crew_id={q(crew_id)}"), 200, f"task {tag}")
    check(d["task"]["status"] == "awaiting_audit", f"task {tag} is {d['task']['status']} after complete")
    check(len(d["photos"]) >= 1, f"bay {tag} has no photo after the cut")
    if exception_kind:
        check(any(e["kind"] == exception_kind for e in d["exceptions"]), f"exception {exception_kind} is not on bay {tag}")
    pending = want(x.c.get("/api/vegetation/audits?result=pending"), 200, "pending audits")["audits"]
    audit = next((a for a in pending if a["task_id"] == task_id), None)
    check(audit is not None, f"no pending audit for bay {tag}")
    check(audit["suggested_result"] in ("compliant", "non_compliant"), f"audit {tag}: {brief(audit, 200)}")
    return audit


def veg_crew_bay(x, crew_id, bay_id, title):
    """The crew's list entry for one bay of this run's package (matched by the package title, which is unique per run; the list
    also holds closed tasks from earlier runs on the same bays)."""
    bays = want(x.c.get(f"/api/vegetation/contractor/bays?crew_id={q(crew_id)}"), 200, "crew bays")["bays"]
    return next((b for b in bays if b["bay_id"] == bay_id and b.get("package_title") == title), None)


# ============================================================================ meta and Command Centre
@test("meta")
def meta_engine_and_genie(x):
    """/api/meta reports the live engine state and a Genie link."""
    body = want(x.c.get("/api/meta"), 200, "meta")
    eng = body.get("engine") or {}
    check(eng.get("status") in ("paused", "starting", "running", "live", "stopping", "idle"), f"engine status {eng.get('status')!r}")
    check(str(body.get("genie", "")).startswith("https://"), f"genie link {body.get('genie')!r}")
    x.note(f"engine status {eng.get('status')}, phase {eng.get('phase')}")


@test("meta")
def meta_index_page(x):
    """The root page is served and boots the app (index.html references the static app script)."""
    r = x.c.get("/")
    check(r.status == 200, f"HTTP {r.status} for /")
    html = r.body if isinstance(r.body, str) else ""
    check("<html" in html.lower() and "app.js" in html, "index.html does not reference app.js")


@test("meta")
def cc_tiles(x):
    """/api/cc tiles: customers off, life support, active outages, situation badges, crews and reliability headline."""
    cc = want(x.c.get("/api/cc"), 200, "command centre")
    check(is_num(cc["customers_off"]) and cc["customers_off"] >= 0, f"customers_off {cc.get('customers_off')}")
    check(is_num(cc["life_support_off"]) and cc["life_support_off"] >= 0, "life_support_off not a non-negative number")
    check(is_num(cc["active_outages"]), "active_outages missing")
    check(set(cc["situations"]) <= SEVERITIES, f"unexpected severities {set(cc['situations'])}")
    crews = cc["crews"]
    check(crews["total"] > 0, f"no crews in the total: {crews}")
    check(0 <= crews["available"] <= crews["total"], f"available {crews['available']} outside 0..total {crews['total']}")
    rel = cc["reliability"]
    check(is_num(rel["saidi_all"]) and rel["saidi_all"] >= 0, f"saidi_all {rel.get('saidi_all')}")
    check(is_num(rel["saifi_all"]) and rel["saifi_all"] >= 0, f"saifi_all {rel.get('saifi_all')}")
    check(is_num(rel["saidi_target_ex_med"]) and rel["saidi_target_ex_med"] > 0, f"SAIDI target {rel.get('saidi_target_ex_med')}")
    x.note(f"customers off {cc['customers_off']:,}, crews {crews['available']} of {crews['total']} available")


@test("meta")
def cc_spine(x):
    """The network spine: one row per stop, red/amber/green counts that add up to the total, in stop order."""
    spine = want(x.c.get("/api/cc"), 200, "command centre")["spine"]
    check(len(spine) == 6, f"spine has {len(spine)} stops, expected 6 (the transmission level is the second)")
    stops = [s["stop"] for s in spine]
    for want_stop in ("bulk_supply", "transmission_substations", "zone_substations", "hv_feeders", "distribution_lv", "customers_der"):
        check(want_stop in stops, f"spine is missing stop {want_stop}")
    for s in spine:
        check(s["red"] + s["amber"] + s["green"] == s["total"], f"stop {s['stop']}: r+a+g != total ({s})")
        check(s["total"] > 0, f"stop {s['stop']} has no assets")
    ords = [s["ord"] for s in spine]
    check(ords == sorted(ords), "spine stops are not in order")


@test("meta")
def cc_saidi_series(x):
    """The SAIDI series behind the reliability chart: daily values for this regulatory year and the year before, with the target."""
    cc = want(x.c.get("/api/cc"), 200, "command centre")
    days = cc["reliability_days"]
    prev = cc["reliability_prev"]
    check(len(days) >= 60, f"only {len(days)} days in the current series")
    check(len(prev) >= 300, f"only {len(prev)} days in the previous series")
    dates = [d["day"] for d in days]
    check(dates == sorted(dates), "current series is not in date order")
    check(all(is_num(d["saidi"]) and d["saidi"] >= 0 for d in days), "a daily SAIDI is negative or not a number")
    check(any(d["is_major_event_day"] for d in prev), "no major event days in the previous year (expected at least one)")
    check(is_num(cc["reliability"]["saidi_target_to_date"]), "no SAIDI target to date")
    x.note(f"{len(days)} days this year, {len(prev)} last year, SAIDI to date {cc['reliability']['saidi_all']:.1f} min")


@test("meta")
def cc_at_risk_and_outages(x):
    """Revenue at risk, long outages and the engine block on the Command Centre."""
    cc = want(x.c.get("/api/cc"), 200, "command centre")
    ar, lo = cc["at_risk"], cc["long_outages"]
    check(is_num(ar["revenue"]) and ar["revenue"] >= 0, f"at_risk revenue {ar.get('revenue')}")
    badges = sum(v for k, v in cc["situations"].items() if k in ("critical", "high", "medium"))   # at risk counts these three severities
    check(ar["situations"] == badges, f"at_risk situations {ar['situations']} != critical+high+medium badges {badges}")
    check(is_num(lo["customers"]) and lo["customers"] >= 0, "long_outages customers missing")
    check("status" in cc["engine"], "engine block has no status")


@test("meta")
def cc_open_outages_seeded(x):
    """The snapshot's outages in progress (gold gld_open_outage) are in the live register, anchored to now: the long ones are past 12 hours,
    so the Command Centre shows customers off supply and a guaranteed service level breach even with the live engine paused."""
    cc = want(x.c.get("/api/cc"), 200, "command centre")
    lo = cc["long_outages"]
    check(lo["outages"] >= 1 and lo["customers"] > 0, f"no outage past 12 hours: {lo}")
    check(cc["customers_off"] >= lo["customers"], f"customers off {cc['customers_off']} below the long outage customers {lo['customers']}")
    # an outage is raised as a trip situation from the snapshot (SIT-TRIP-OUT-...) or by the live engine (SIT-TRIP-LIVE-...)
    trips = [s for s in want(x.c.get("/api/situations"), 200, "situations") if s["situation_id"].startswith("SIT-TRIP-")]
    check(len(trips) >= lo["outages"], f"{len(trips)} open outage situations for {lo['outages']} long outages")
    x.note(f"{lo['outages']} outages past 12 hours, {lo['customers']} customers ({lo['life_support']} on life support); "
           f"{cc['customers_off']} off supply in all")


# ============================================================================ situations
@test("situations")
def sit_list_open(x):
    """/api/situations lists open situations, most severe first, each with an id, type, severity and status."""
    rows = open_situations(x)
    check(len(rows) >= 1, "no open situations in the live data")
    for r in rows:
        check(r["situation_id"] and r["type"] and r["status"] != "closed", f"bad row {brief(r, 160)}")
        check(r["severity"] in SEVERITIES, f"severity {r['severity']} on {r['situation_id']}")
    ranks = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    seq = [ranks[r["severity"]] for r in rows]
    check(seq == sorted(seq), "list is not ordered by severity")
    types = sorted({r["type"] for r in rows})
    x.note(f"{len(rows)} open: " + ", ".join(types))


@test("situations")
def sit_list_all_includes_closed(x):
    """status=all returns at least as many situations as the open list (closed ones included)."""
    opened = open_situations(x)
    allrows = want(x.c.get("/api/situations?status=all"), 200, "all situations")
    check(len(allrows) >= len(opened), f"all {len(allrows)} < open {len(opened)}")


@test("situations")
def sit_badges_match_list(x):
    """The Command Centre severity badges equal the counts of the open list."""
    cc = want(x.c.get("/api/cc"), 200, "command centre")["situations"]
    counts = {}
    for r in open_situations(x):
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    check(cc == counts, f"badges {cc} != list counts {counts}")


@test("situations")
def sit_detail_trip(x):
    """Feeder trip detail: the outage, the devices, the crews in range, the backup ties and the meter alarms."""
    s = find_sit(x, "trip")
    if not s:
        raise Skip("no open trip in the live data")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["trip"]) <= set(d["view"]), f"trip view keys {sorted(d['view'])}")
    o = d["view"]["outage"]
    check(o.get("outage_id") == (d["payload"] or {}).get("outage_id"), "outage id does not match the situation payload")
    check(is_num(o.get("customers_off")) and o["customers_off"] >= 0, f"customers_off {o.get('customers_off')}")
    check(o.get("cause_label"), "no cause label")
    check(isinstance(d["view"]["backup"]["ties"], list), "backup ties is not a list")
    check(isinstance(d["view"]["crews"], list), "crews is not a list")
    check(d["events"], "no decision trail events")
    x.note(f"{s['situation_id']}: {o.get('customers_off')} customers off, cause {o.get('cause_label')}")


@test("situations")
def sit_detail_fire(x):
    """Fire detail: the fire, the distance bands to the network, feeders affected, span risk and the NSW fire danger rows."""
    s = find_sit(x, "fire")
    if not s:
        raise Skip("no open fire situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["fire"]) <= set(d["view"]), f"fire view keys {sorted(d['view'])}")
    v = d["view"]
    check(v["fire"].get("title") or v["fire"].get("status"), "fire has no title or status")
    check(isinstance(v["fire_danger"], list), "fire_danger is not a list")
    if v["bands"]:
        check(all(b["band"] in ("0-1 km", "1-3 km", "3-5 km", "5-10 km") for b in v["bands"]), "unexpected distance band")
        check(all(b["spans"] > 0 for b in v["bands"]), "a band has no spans")
        check(v["spatial_sql"] and "ST_" in v["spatial_sql"], "spatial query panel has no PostGIS SQL")
    x.note(f"{s['situation_id']}: {len(v['bands'])} distance bands, {len(v['feeders'])} feeders")


@test("situations")
def sit_detail_heat(x):
    """Hot transformer detail: the 30-day hourly history, the DGA series, the zone forecast, the backup ties and the live state."""
    s = find_sit(x, "heat")
    if not s:
        raise Skip("no open heat situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["heat"]) <= set(d["view"]), f"heat view keys {sorted(d['view'])}")
    v = d["view"]
    check(v["history"], "no hourly history for the transformer")
    check({h["attribute"] for h in v["history"]} & {"MW", "TOP_OIL_TEMP"}, "history carries neither MW nor TOP_OIL_TEMP")
    check(v["state"].get("transformer_id"), "no transformer state")
    check(v["forecast"], "no zone load forecast")
    x.note(f"{s['situation_id']}: {len(v['history'])} history rows, {len(v['dga'])} DGA points, {len(v['ties'])} ties")


@test("situations")
def sit_detail_min_load(x):
    """Minimum load detail: the solar transformers with the export limit the plan would impose."""
    s = find_sit(x, "min_load")
    if not s:
        raise Skip("no open minimum load situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["min_load"]) <= set(d["view"]), f"min_load view keys {sorted(d['view'])}")
    tx = d["view"]["transformers"]
    check(len(tx) > 0, "no solar transformers listed")
    check(all(t["recommended_limit_kw"] in (1.5, 3.0) for t in tx), "an export limit is not 1.5 or 3.0 kW")
    check(all(is_num(t["est_midday_reverse_flow"]) for t in tx), "midday reverse flow is not a number")
    x.note(f"{len(tx)} transformers, limit set for {sum(1 for t in tx if t['recommended_limit_kw'] == 1.5)}")


@test("situations")
def sit_detail_vegetation(x):
    """Vegetation detail: the cutting packages by feeder, and the red spans they cover."""
    s = find_sit(x, "vegetation")
    if not s:
        raise Skip("no open vegetation situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["vegetation"]) <= set(d["view"]), f"vegetation view keys {sorted(d['view'])}")
    check(d["view"]["packages"], "no cutting packages")
    check(all(p["red"] > 0 for p in d["view"]["packages"]), "a package has no red spans")
    check(all(is_num(s_["clearance_m"]) for s_ in d["view"]["spans"]), "span clearance is not a number")
    x.note(f"{len(d['view']['packages'])} packages, {len(d['view']['spans'])} red spans listed")


@test("situations")
def sit_detail_drone_defect(x):
    """Drone defect detail: the pole, the image check result, the open defects behind its protective device."""
    s = find_sit(x, "drone_defect")
    if not s:
        raise Skip("no open drone defect situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["drone_defect"]) <= set(d["view"]), f"drone view keys {sorted(d['view'])}")
    check(d["view"]["pole"].get("asset_id"), "pole has no asset id")
    check(d["view"]["image"].startswith("/static/drone/"), f"image path {d['view']['image']}")
    x.note(f"{s['situation_id']}: pole {d['view']['pole'].get('asset_id')}, {len(d['view']['bundle'])} open defects behind it")


@test("situations")
def sit_detail_fatigue(x):
    """Crew fatigue detail: crews planned past the enterprise agreement cap."""
    s = find_sit(x, "fatigue")
    if not s:
        raise Skip("no open fatigue situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["fatigue"]) <= set(d["view"]), f"fatigue view keys {sorted(d['view'])}")
    crews = d["view"]["crews"]
    check(all(c["hours"] > 0 and c["crew_id"] for c in crews), "a fatigued crew has no hours or id")
    check(all(c["hours"] > 10 for c in crews), "a crew listed as fatigued is planned under 10 hours")
    x.note(f"{len(crews)} crews past the cap")


@test("situations")
def sit_detail_records(x):
    """Records detail: meters whose recorded transformer disagrees with the voltage correlation."""
    s = find_sit(x, "records")
    if not s:
        raise Skip("no open records situation")
    d = sit_detail(x, s["situation_id"])
    check(set(VIEW_KEYS["records"]) <= set(d["view"]), f"records view keys {sorted(d['view'])}")
    meters = d["view"]["meters"]
    check(meters, "no mismatched meters listed")
    check(all(re.fullmatch(r"\d{10}", str(m["nmi"])) for m in meters), "an NMI is not 10 digits")
    x.note(f"{len(meters)} meters with a mismatched transformer")


@test("situations")
def sit_detail_unknown_is_404(x):
    """An unknown situation id is answered with 404 (the UI shows it as not found)."""
    r = x.c.get(f"/api/situation/{q('SIT-DOES-NOT-EXIST-E2E')}")
    check(r.status == 404, f"HTTP {r.status} for an unknown situation (expected 404): {brief(r.body, 200)}")


@test("situations")
def sit_plan_heat(x):
    """Planning a hot transformer produces its three recommendations, each with a distinct id, and the situation moves to plan ready."""
    s = find_sit(x, "heat")
    if not s:
        raise Skip("no open heat situation")
    d = sit_plan(x, s["situation_id"])
    kinds = {r["kind"] for r in d["recommendations"] if r["status"] == "proposed"}
    check({"oil_sample", "substation_crew"} <= kinds, f"proposed kinds {kinds}")
    ids = [r["rec_id"] for r in d["recommendations"]]
    check(len(ids) == len(set(ids)), "duplicate recommendation ids")
    check(d["status"] in ("plan_ready", "decided", "executing", "detected"), f"status {d['status']}")
    x.note(f"proposed: {sorted(kinds)}")


# ----------------------------------------------------------------------------- recommendation lifecycles
def pick_trip(x):
    """An open trip with an active outage and a job; planned fresh so its recommendations exist."""
    s = find_sit(x, "trip", lambda r: r["status"] in ("detected", "plan_ready", "decided"))
    if not s:
        s = find_sit(x, "trip")
    if not s:
        raise Skip("no open trip in the live data")
    return s["situation_id"]


@test("situations")
def rec_trip_dispatch(x):
    """Dispatch on a feeder trip: accept sends the crew (positions show the job), undo recalls it, adjust sends another crew, reject leaves
    the crew where it was, and each step is undone."""
    sid = pick_trip(x)
    d = sit_plan(x, sid)
    rec = rec_by_kind(d, "dispatch")
    if not rec:
        raise Skip(f"no dispatch recommendation on {sid} (no active outage with a job and a qualified crew)")
    rid = rec["rec_id"]
    job_id = rec["payload"]["job_id"]
    crew_a = rec["payload"]["crew_id"]
    region = d["view"]["job"]["region_id"]
    x.defer(lambda: settle(x, sid, rid), "settle dispatch recommendation")

    # accept: the crew is sent and the job is first on its field day
    r = sit_decide(x, sid, rid, "accept", reason="E2E accept")
    body = want(r, 200, "accept dispatch")
    check(body["status"] == "accepted", f"status {body['status']}")
    check(body["effects"] and "dispatched" in body["effects"][0], f"effect {body['effects']}")
    pos = positions(x, region)
    check(pos[crew_a].get("job_id") == job_id, f"{crew_a} job_id {pos[crew_a].get('job_id')} after accept (expected {job_id})")
    check(pos[crew_a]["status"] in ("travelling", "on_site"), f"{crew_a} status {pos[crew_a]['status']} after accept")
    d2 = sit_detail(x, sid)
    check(rec_by_id(d2, rid)["status"] == "accepted", "detail does not show the recommendation accepted")
    check(any(e["action"] == "accepted" for e in d2["events"]), "no 'accepted' event in the decision trail")

    # undo: the dispatch is recalled and the crew is no longer on the job
    u = want(sit_undo(x, sid, rid), 200, "undo dispatch")
    check(u["status"] == "undone" and any("recalled" in e for e in u["effects"]), f"undo result {u}")
    pos = positions(x, region)
    check(pos[crew_a].get("job_id") != job_id, f"{crew_a} still on {job_id} after recall")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] == "proposed", "recommendation not back to proposed after undo")

    # adjust to another crew from the dispatch list
    crews = d2["view"]["crews"] or []
    other = next((c for c in crews if c["crew_id"] != crew_a and not c.get("blocked")), None)
    if other is None:
        other = next((c for c in roster_crews_in(x, region) if c["crew_id"] != crew_a), None)
    check(other is not None, "no other crew available to adjust the dispatch to")
    a = want(sit_decide(x, sid, rid, "adjust", reason="E2E adjust to another crew", crew_id=other["crew_id"]), 200, "adjust dispatch")
    check(a["status"] == "adjusted", f"adjust status {a['status']}")
    check(other["crew_id"] in json.dumps(rec_by_id(sit_detail(x, sid), rid)["payload"]), "adjusted crew not in the payload")
    pos = positions(x, region)
    check(pos[other["crew_id"]].get("job_id") == job_id, f"{other['crew_id']} not on {job_id} after adjust")
    want(sit_undo(x, sid, rid), 200, "undo adjusted dispatch")
    pos = positions(x, region)
    check(pos[other["crew_id"]].get("job_id") != job_id, f"{other['crew_id']} still on the job after recall")

    # reject: nothing is sent
    rj = want(sit_decide(x, sid, rid, "reject", reason="E2E reject"), 200, "reject dispatch")
    check(rj["status"] == "rejected", f"reject status {rj['status']}")
    pos = positions(x, region)
    check(pos[crew_a].get("job_id") != job_id and pos[other["crew_id"]].get("job_id") != job_id, "a crew was sent on a rejected dispatch")
    want(sit_undo(x, sid, rid), 200, "undo rejected dispatch")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] == "proposed", "not proposed after undoing the rejection")
    x.note(f"{sid}: {crew_a} sent then recalled; adjusted to {other['crew_id']} then recalled; reject sent nobody")


def roster_crews_in(x, region):
    """Crews of a region with their positions, for choosing an alternative crew."""
    return [dict(crew_id=p["crew_id"]) for p in positions(x, region).values()]


@test("situations")
def rec_trip_notify(x):
    """Restoration notice on a feeder trip: accept queues the SMS and shows the undo countdown, undo withdraws it, reject removes the
    drafts, and adjust sends the dispatcher's own wording to life support customers only."""
    sid = pick_trip(x)
    d = sit_plan(x, sid)
    rec = rec_by_kind(d, "notify")
    if not rec:
        raise Skip(f"no restoration notice on {sid}")
    rid = rec["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle notice recommendation")
    before = outbox_total(d)

    a = want(sit_decide(x, sid, rid, "accept", reason="E2E accept notice"), 200, "accept notice")
    n = parse_count(a["effects"][0], r"([\d,]+) SMS queued")
    check(n is not None and n > 0, f"no SMS queued: {a['effects']}")
    d2 = sit_detail(x, sid)
    left = rec_by_id(d2, rid).get("undo_left")
    check(left is not None and 1 <= left <= 60, f"undo_left {left} after accept (expected 1 to 60)")
    check(outbox_total(d2) == before + n, f"outbox total {outbox_total(d2)} != {before} + {n} queued")
    x.note(f"queued {n:,}; undo_left {left}s")

    u = want(sit_undo(x, sid, rid), 200, "undo notice")
    check(any("withdrawn" in e for e in u["effects"]), f"undo effects {u['effects']}")
    d3 = sit_detail(x, sid)
    check(outbox_total(d3) == before, f"outbox total {outbox_total(d3)} after undo (expected {before}): messages not withdrawn")
    check(rec_by_id(d3, rid)["status"] == "proposed", "notice not proposed after undo")

    rj = want(sit_decide(x, sid, rid, "reject", reason="E2E reject notice"), 200, "reject notice")
    check(rj["status"] == "rejected", f"reject status {rj['status']}")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] == "rejected", "detail does not show rejected")
    want(sit_undo(x, sid, rid), 200, "undo rejected notice")

    msg = f"E2E life support notice {int(time.time())}"
    ad = want(sit_decide(x, sid, rid, "adjust", reason="E2E adjust wording", message=msg, audience="life_support"), 200, "adjust notice")
    m = parse_count(ad["effects"][0], r"([\d,]+) of them to life support")
    t = parse_count(ad["effects"][0], r"([\d,]+) SMS queued")
    check(t is not None and m is not None and t == m, f"life support only should queue the same count twice: {ad['effects']}")
    p = rec_by_id(sit_detail(x, sid), rid)["payload"]
    check(p.get("body") == msg and p.get("audience") == "life_support", f"payload after adjust {brief(p, 200)}")
    want(sit_undo(x, sid, rid), 200, "undo adjusted notice")
    check(outbox_total(sit_detail(x, sid)) == before, "adjusted notice not withdrawn on undo")
    x.note(f"adjust to life support queued {t} with the dispatcher's wording")


@test("situations")
def rec_undo_after_window_is_refused(x):
    """An accepted notice whose 60 second undo window has passed cannot be undone (409 too_late) and stays accepted."""
    hit = None
    for s in open_situations(x):
        if s["type"] not in ("trip", "fire", "heat", "vegetation", "storm"):
            continue
        d = sit_detail(x, s["situation_id"])
        for r in d["recommendations"]:
            if r["kind"] == "notify" and r["status"] in ("accepted", "adjusted") and r.get("undo_left") == 0:
                hit = (s["situation_id"], r["rec_id"])
                break
        if hit:
            break
    if not hit:
        raise Skip("no accepted notice past its undo window in the live data")
    sid, rid = hit
    r = x.c.post(f"/api/situation/{q(sid)}/undo", dict(rec_id=rid))
    check(r.status == 409, f"undo after the window: HTTP {r.status} (expected 409): {brief(r.body, 200)}")
    check(isinstance(r.body, dict) and r.body.get("error") == "too_late", f"error {brief(r.body, 200)}")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] in ("accepted", "adjusted"), "a refused undo changed the notice status")
    x.note(f"{sid}: refused with 409 too_late, notice still accepted")


@test("situations")
def rec_fire_protection_patrol_notify_case(x):
    """Fire recommendations on the NSW fire: protection (undo restores auto-reclose), patrol jobs (withdrawn on undo), notify (queued and
    withdrawn), de-energisation case (recorded; rejected then undone)."""
    s = find_sit(x, "fire")
    if not s:
        raise Skip("no open fire situation")
    sid = s["situation_id"]
    d = sit_detail(x, sid)
    if not d["recommendations"]:
        d = sit_plan(x, sid)

    # protection
    prot = rec_by_kind(d, "protection")
    if prot:
        rid = prot["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle protection")
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept protection")
        check(a["effects"] and all("Protection setting change sent" in e for e in a["effects"]), f"effects {a['effects']}")
        u = want(sit_undo(x, sid, rid), 200, "undo protection")
        check(all("auto reclose on" in e for e in u["effects"]) and u["effects"], f"undo effects {u['effects']}")
        x.note(f"protection: {len(a['effects'])} feeders switched, then auto-reclose restored")
    # patrol jobs
    pat = rec_by_kind(d, "patrol")
    if pat:
        rid = pat["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle patrol")
        before = find_jobs(x, "Fire patrol ")
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept patrol")
        check(a["effects"] and "Patrol jobs on" in a["effects"][0], f"effects {a['effects']}")
        after = find_jobs(x, "Fire patrol ")
        check(len(after) > len(before), f"no patrol job appeared in the roster ({len(before)} before, {len(after)} after)")
        want(sit_undo(x, sid, rid), 200, "undo patrol")
        check(len(find_jobs(x, "Fire patrol ")) == len(before), "patrol jobs still on the roster after undo")
        x.note(f"patrol: {len(after) - len(before)} jobs raised and withdrawn")
    # notify
    nt = rec_by_kind(d, "notify")
    if nt:
        rid = nt["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle fire notice")
        before_n = outbox_total(sit_detail(x, sid))
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept fire notice")
        n = parse_count(a["effects"][0], r"([\d,]+) SMS queued")
        check(n is not None and n > 0, f"no SMS queued for the fire notice: {a['effects']}")
        check(outbox_total(sit_detail(x, sid)) == before_n + n, "outbox did not grow by the queued count")
        want(sit_undo(x, sid, rid), 200, "undo fire notice")
        check(outbox_total(sit_detail(x, sid)) == before_n, "fire notice messages not withdrawn")
        x.note(f"fire notice: {n:,} SMS queued then withdrawn")
    # de-energisation case: no system effect, reject then undo
    case = rec_by_kind(d, "deenergise_case")
    if case:
        rid = case["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle de-energisation case")
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept case")
        check(a["effects"] == ["Recorded"], f"case effects {a['effects']}")
        want(sit_undo(x, sid, rid), 200, "undo case")
        rj = want(sit_decide(x, sid, rid, "reject", reason="E2E reject case"), 200, "reject case")
        check(rj["status"] == "rejected", "case not rejected")
        want(sit_undo(x, sid, rid), 200, "undo rejected case")
        check(rec_by_id(sit_detail(x, sid), rid)["status"] == "proposed", "case not proposed after undo")
    if not (prot or pat or nt or case):
        raise Skip("the fire has no recommendations to exercise")


@test("situations")
def rec_heat_switching_oil_crew(x):
    """Hot transformer: a switching instruction (undo opens the tie again), an oil sample (recorded), and a substation crew job that is raised
    in resource management and withdrawn on undo."""
    s = find_sit(x, "heat")
    if not s:
        raise Skip("no open heat situation")
    sid = s["situation_id"]
    d = sit_plan(x, sid)
    sw = rec_by_kind(d, "switching")
    if sw:
        rid = sw["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle switching")
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept switching")
        check(a["effects"] and "Switching instruction sent to" in a["effects"][0], f"effects {a['effects']}")
        u = want(sit_undo(x, sid, rid), 200, "undo switching")
        check(u["effects"] and "open" in u["effects"][0], f"undo effects {u['effects']}")
        x.note(f"switching: {a['effects'][0][:90]}")
    oil = rec_by_kind(sit_plan(x, sid), "oil_sample")
    check(oil is not None, "no oil sample recommendation")
    rid = oil["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle oil sample")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept oil sample")
    check(a["effects"] == ["Recorded"], f"oil effects {a['effects']}")
    want(sit_undo(x, sid, rid), 200, "undo oil sample")
    sc = rec_by_kind(sit_plan(x, sid), "substation_crew")
    check(sc is not None, "no substation crew recommendation")
    rid = sc["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle substation crew")
    tx = sc["payload"].get("asset")
    before = find_jobs(x, f"Inspect {tx}")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept substation crew")
    check(a["effects"] and "Substation crew job raised" in a["effects"][0], f"effects {a['effects']}")
    after = find_jobs(x, f"Inspect {tx}")
    check(len(after) > len(before), f"no substation job for {tx} in the roster after accept")
    check(any(j.get("job_type") == "SUBSTATION_MAINT" for _, j in after), "raised job is not a SUBSTATION_MAINT job")
    u = want(sit_undo(x, sid, rid), 200, "undo substation crew")
    check(any("withdrawn" in e for e in u["effects"]), f"undo effects {u['effects']}")
    check(len(find_jobs(x, f"Inspect {tx}")) == len(before), "substation job still in the roster after undo")
    x.note(f"substation job for {tx} raised and withdrawn")


@test("situations")
def rec_min_load_envelope(x):
    """Minimum load: publishing lower export envelopes is recorded in the trail, and undo returns export limits to their defaults."""
    s = find_sit(x, "min_load")
    if not s:
        raise Skip("no open minimum load situation")
    sid = s["situation_id"]
    rec = rec_by_kind(sit_plan(x, sid), "envelope")
    check(rec is not None, "no envelope recommendation")
    rid = rec["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle envelope")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept envelope")
    check(a["effects"] and "Export envelope sent to" in a["effects"][0], f"effects {a['effects']}")
    check(any(e["action"] == "accepted" and "Export envelope" in json.dumps(e["detail"]) for e in sit_detail(x, sid)["events"]),
          "the decision trail does not record the envelope effect")
    u = want(sit_undo(x, sid, rid), 200, "undo envelope")
    check(u["effects"] == ["Export limits back to their defaults"], f"undo effects {u['effects']}")
    x.note(a["effects"][0][:100])


@test("situations")
def rec_vegetation_package(x):
    """Vegetation: a cutting package adds a vegetation job to the crews' plan; undo withdraws it; the already accepted package is left alone."""
    s = find_sit(x, "vegetation")
    if not s:
        raise Skip("no open vegetation situation")
    sid = s["situation_id"]
    d = sit_detail(x, sid)
    pkg = next((r for r in d["recommendations"] if r["kind"] == "package" and r["status"] == "proposed"), None)
    if not pkg:
        d = sit_plan(x, sid)
        pkg = next((r for r in d["recommendations"] if r["kind"] == "package" and r["status"] == "proposed"), None)
    check(pkg is not None, "no proposed cutting package")
    rid = pkg["rec_id"]
    feeder = pkg["payload"]["feeder"]
    x.defer(lambda: settle(x, sid, rid), "settle package")
    before = find_jobs(x, f"Cutting package {feeder}")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept package")
    check(a["effects"] and "Cutting package for" in a["effects"][0], f"effects {a['effects']}")
    after = find_jobs(x, f"Cutting package {feeder}")
    check(len(after) > len(before), f"no cutting job on the roster for {feeder}")
    want(sit_undo(x, sid, rid), 200, "undo package")
    check(len(find_jobs(x, f"Cutting package {feeder}")) == len(before), "cutting job still on the roster after undo")
    x.note(f"package for {feeder}: job raised and withdrawn")


@test("situations")
def rec_drone_notification_and_bundle(x):
    """Drone defect: a priority 2 notification goes to the asset register (undo, then accept again to leave it accepted as found); a bundle
    of defects behind one protective device becomes one planned outage job when the plan offers it."""
    s = find_sit(x, "drone_defect")
    if not s:
        raise Skip("no open drone defect situation")
    sid = s["situation_id"]
    d = sit_planned(x, sid)
    rec = rec_by_kind(d, "notification")
    check(rec is not None, "no notification recommendation")
    rid = rec["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle notification")
    if rec["status"] != "proposed":
        want(sit_undo(x, sid, rid), 200, "undo notification")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] == "proposed", "notification not proposed after undo")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept notification")
    check(a["effects"] and "Notification raised for" in a["effects"][0], f"effects {a['effects']}")
    x.note("notification raised for the pole, kept accepted as found")
    # the bundle: its own situation (the plan adds it only when more than one open defect sits behind the device)
    for s2 in open_situations(x):
        if s2["type"] != "drone_defect":
            continue
        d2 = sit_plan(x, s2["situation_id"])
        b = rec_by_kind(d2, "bundle")
        if b:
            bid = b["rec_id"]
            x.defer(lambda: settle(x, s2["situation_id"], bid), "settle bundle")
            before = find_jobs(x, "Planned outage behind")
            ab = want(sit_decide(x, s2["situation_id"], bid, "accept"), 200, "accept bundle")
            check(ab["effects"] and "bundled behind" in ab["effects"][0], f"bundle effects {ab['effects']}")
            check(len(find_jobs(x, "Planned outage behind")) > len(before), "bundled planned outage not on the roster")
            want(sit_undo(x, s2["situation_id"], bid), 200, "undo bundle")
            x.note(f"bundle on {s2['situation_id']} raised as one outage and withdrawn")
            return
    x.note("no drone situation offers a bundle right now (the plan adds one only with more than one open defect behind a device)")


@test("situations")
def rec_fatigue_reroster(x):
    """Crew fatigue: stand the over-cap crews down and re-roster their stops (visible on the re-rostered list); undo restores them. The
    recommendation is left accepted with no crew stood down, as it was found."""
    s = find_sit(x, "fatigue")
    if not s:
        raise Skip("no open fatigue situation")
    sid = s["situation_id"]
    d = sit_planned(x, sid)
    rec = rec_by_kind(d, "reroster")
    check(rec is not None, "no re-roster recommendation")
    rid = rec["rec_id"]
    crews = [c["crew_id"] for c in rec["payload"].get("crews", [])]
    if not crews:
        raise Skip("the fatigue recommendation names no crews")

    def settle_fatigue():
        if rec_by_id(sit_detail(x, sid), rid)["status"] != "proposed":
            sit_undo(x, sid, rid)
        for c in rec["payload"].get("crews", []):
            x.c.post("/api/rm/restore-crew", dict(region=c["region_id"], crew_id=c["crew_id"]))
    x.defer(settle_fatigue, "settle fatigue")
    if rec["status"] != "proposed":
        want(sit_undo(x, sid, rid), 200, "undo re-roster")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept re-roster")
    check(a["effects"] and "stood down" in a["effects"][0], f"effects {a['effects']}")
    rer = {r["crew_id"] for r in want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["rerostered"]}
    check(set(crews) <= rer, f"crews {crews} not all on the re-rostered list ({sorted(rer)[:5]}...)")
    u = want(sit_undo(x, sid, rid), 200, "undo re-roster")
    check(u["effects"] and "back on the roster" in u["effects"][0], f"undo effects {u['effects']}")
    rer = {r["crew_id"] for r in want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["rerostered"]}
    check(not (set(crews) & rer), "crews still stood down after undo")
    a2 = want(sit_decide(x, sid, rid, "accept"), 200, "re-accept re-roster")
    check(a2["status"] == "accepted", "re-accept did not take")
    for c in rec["payload"].get("crews", []):
        want(x.c.post("/api/rm/restore-crew", dict(region=c["region_id"], crew_id=c["crew_id"])), 200, "restore crew")
    check(not (set(crews) & {r["crew_id"] for r in want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["rerostered"]}),
          "crews still stood down after restore")
    x.note(f"{len(crews)} crews stood down and restored; left accepted as found")


@test("situations")
def rec_records_correction(x):
    """Records: raising corrections to the governed master data workflow is recorded; accept, undo and reject all leave the record as found."""
    s = find_sit(x, "records")
    if not s:
        raise Skip("no open records situation")
    sid = s["situation_id"]
    rec = rec_by_kind(sit_plan(x, sid), "correction")
    check(rec is not None, "no correction recommendation")
    rid = rec["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle correction")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept correction")
    check(a["effects"] == ["Recorded"], f"effects {a['effects']}")
    want(sit_undo(x, sid, rid), 200, "undo correction")
    rj = want(sit_decide(x, sid, rid, "reject", reason="E2E reject correction"), 200, "reject correction")
    check(rj["status"] == "rejected", "correction not rejected")
    want(sit_undo(x, sid, rid), 200, "undo rejected correction")
    check(rec_by_id(sit_detail(x, sid), rid)["status"] == "proposed", "correction not proposed after undo")


# ============================================================================ network
@test("network")
def net_bulk_supply(x):
    """Bulk supply points: capacity, zones fed and live MW for each."""
    rows = want(x.c.get("/api/network/bulk_supply"), 200, "bulk supply")["rows"]
    check(len(rows) >= 1, "no bulk supply points")
    for r in rows:
        check(r["bsp_id"] and r["name"], f"bulk supply row {brief(r, 120)}")
        check(is_num(r["capacity_mva"]) and r["capacity_mva"] > 0, f"capacity {r['capacity_mva']} for {r['bsp_id']}")
        check(is_num(r["mw_now"]) and r["mw_now"] >= 0, f"mw_now for {r['bsp_id']}")
        check(r["zones"] >= 1, f"{r['bsp_id']} feeds no zone substations")
    x.note(f"{len(rows)} bulk supply points")


@test("network")
def net_zone_substations(x):
    """Zone substation transformers: utilisation, status and the live MW and top oil temperature."""
    rows = want(x.c.get("/api/network/zone_substations"), 200, "zone substations")["rows"]
    check(len(rows) >= 10, f"only {len(rows)} zone transformers")
    check(all(r["status"] in ("green", "amber", "red") for r in rows), "a status is not green, amber or red")
    check(all(is_num(r["peak_utilisation_30d"]) for r in rows), "peak utilisation is not a number")
    check(any(r["transformer_id"] == "ZS-KNV-TX1" for r in rows), "ZS-KNV-TX1 missing")
    x.note(f"{len(rows)} zone transformers, {sum(1 for r in rows if r['status'] == 'red')} red")


@test("network")
def net_hv_feeders(x):
    """HV feeders: status, customers, life support, live amps, and the tripped flag."""
    rows = want(x.c.get("/api/network/hv_feeders"), 200, "HV feeders")["rows"]
    check(len(rows) >= 40, f"only {len(rows)} feeders")
    check(all(r["customers"] > 0 for r in rows), "a feeder has no customers")
    check(all(isinstance(r["tripped"], bool) for r in rows), "tripped flag is not a boolean")
    check(all(r["status"] in ("green", "amber", "red") for r in rows), "a feeder status is not green, amber or red")
    x.note(f"{len(rows)} feeders, {sum(1 for r in rows if r['tripped'])} tripped")


@test("network")
def net_distribution_lv(x):
    """Distribution substations: the top 200 by utilisation or reverse flow, with the status summary across all of them."""
    body = want(x.c.get("/api/network/distribution_lv"), 200, "distribution LV")
    rows, summary = body["rows"], body["summary"]
    check(len(rows) > 0, "no distribution substations")
    total = sum(s["n"] for s in summary)
    check(total >= len(rows) and total > 500, f"summary total {total} for {len(rows)} rows")
    check(all(r["ds_id"] and is_num(r["kva"]) for r in rows), "a substation has no id or rating")
    x.note(f"{total:,} substations in the summary")


@test("network")
def net_customers_der(x):
    """Customers and distributed energy by zone substation; the zones add up to the whole customer base."""
    rows = want(x.c.get("/api/network/customers_der"), 200, "customers DER")["rows"]
    check(len(rows) >= 10, f"only {len(rows)} zones")
    total = sum(r["customers"] for r in rows)
    check(total > 10000, f"customers across zones {total}")
    check(all(is_num(r["solar_mw"]) and r["solar_mw"] >= 0 for r in rows), "solar MW not a non-negative number")
    x.note(f"{total:,} customers across {len(rows)} zones")


@test("network")
def net_unknown_stop_is_empty(x):
    """An unknown network stop returns no rows (the UI shows an empty table, not an error)."""
    r = want(x.c.get("/api/network/no_such_stop"), 200, "unknown stop")
    check(r["rows"] == [], f"unknown stop returned rows {brief(r, 120)}")


@test("network")
def net_zone_customers(x):
    """Zone customers drawer: customer types, the largest suburbs and the critical sites."""
    z = want(x.c.get("/api/zone-customers/ZS-KNV"), 200, "zone customers")
    check(sum(t["customers"] for t in z["types"]) > 100, "zone customer types add up to too few")
    check(len(z["suburbs"]) >= 1 and all(s["customers"] > 0 for s in z["suburbs"]), "suburbs empty or zero")
    check(isinstance(z["sites"], list), "critical sites not a list")
    x.note(f"{sum(t['customers'] for t in z['types']):,} customers, {len(z['sites'])} critical sites")


@test("network")
def net_series_transformer(x):
    """Transformer series: 14 days of hourly MW and top oil temperature, plus the live 3-hour SCADA trace (INFO if the engine is paused)."""
    z = want(x.c.get("/api/series/transformer/ZS-KNV-TX1"), 200, "transformer series")
    attrs = {h["attribute"] for h in z["history"]}
    check({"MW", "TOP_OIL_TEMP"} <= attrs, f"history attributes {attrs}")
    check(len(z["history"]) > 300, f"only {len(z['history'])} history points")
    check(all(is_num(h["v"]) for h in z["history"]), "a history value is not a number")
    check(isinstance(z["live"], list), "live is not a list")
    x.note(f"{len(z['history'])} hourly points; live points {len(z['live'])}")
    if not z["live"]:
        x.note("live SCADA trace is empty (the live engine is not running)")


@test("network")
def net_series_feeder(x):
    """Feeder series: 14 days of hourly amps, live amps, and recent outages on the feeder."""
    z = want(x.c.get("/api/series/feeder/BWT-F01"), 200, "feeder series")
    check(len(z["history"]) > 300, f"only {len(z['history'])} amps points")
    check(all(is_num(h["amps"]) and h["amps"] >= 0 for h in z["history"]), "negative or non-numeric amps")
    check(isinstance(z["outages"], list), "outages not a list")
    x.note(f"{len(z['history'])} amps points, {len(z['outages'])} recent outages, live {len(z['live'])}")


@test("network")
def drawer_asset_device(x):
    """Asset drawer for a circuit breaker: the record, its systems and the open defects and work orders."""
    a = want(x.c.get("/api/asset/RPR-F03-CB01"), 200, "asset RPR-F03-CB01")
    check(a["kind"] == "asset" and a["record"]["asset_id"] == "RPR-F03-CB01", "record is not the breaker")
    check(a["systems"] and all(s[1] for s in a["systems"]), "systems list is empty or has blanks")
    check(isinstance(a["defects"], list) and isinstance(a["orders"], list), "defects or orders not lists")


@test("network")
def drawer_asset_span(x):
    """Asset drawer for an overhead span: its class, vegetation and span fault probability."""
    st = want(x.c.get("/api/scene/street/BWT-F01"), 200, "street scene")
    spans = st["spans"]
    check(spans, "no spans in the street scene")
    sid = spans[0].get("span_id") or spans[0].get("asset_id")
    check(sid, f"span has no id: {brief(spans[0], 160)}")
    a = want(x.c.get(f"/api/asset/{q(sid)}"), 200, f"span {sid}")
    check(a["record"]["asset_class"] == "overhead_span", f"asset class {a['record']['asset_class']}")
    check(a["record"]["asset_id"] == sid, "wrong span returned")
    x.note(f"{sid}: {a['record'].get('asset_class_label')}, fault probability {a.get('fault_probability')}")


@test("network")
def drawer_asset_zone_transformer(x):
    """Asset drawer for a zone substation transformer: rating, year built and health."""
    a = want(x.c.get("/api/asset/ZS-KNV-TX1"), 200, "transformer ZS-KNV-TX1")
    rec = a["record"]
    check(rec["asset_class"] == "zone_transformer", f"asset class {rec['asset_class']}")
    check(is_num(rec["rating"]) and rec["rating"] > 0, f"rating {rec.get('rating')}")
    check(any("Health" in s[0] for s in a["systems"]), "no health line on the transformer")


@test("network")
def drawer_asset_distribution_substation(x):
    """Asset drawer for a distribution substation named in the spatial results: it resolves to an asset record."""
    body = want(x.c.post("/api/spatial/area", dict(lat=-33.67777681280282, lon=150.91863006583395, radius_m=800)), 200, "spatial")
    ds = body["distribution_substations"][0]["ds_id"]
    a = want(x.c.get(f"/api/asset/{q(ds)}"), 200, f"distribution substation {ds}")
    check(a["record"] and a["record"].get("asset_id") == ds, f"record for {ds} is {brief(a['record'], 120)}")


@test("network")
def drawer_nmi(x):
    """Customer (NMI) drawer: the customer record, supply and the 3-day meter intervals."""
    d = sit_detail(x, find_sit(x, "records")["situation_id"]) if find_sit(x, "records") else None
    if not d:
        raise Skip("no records situation to take an NMI from")
    nmi = d["view"]["meters"][0]["nmi"]
    a = want(x.c.get(f"/api/asset/{nmi}"), 200, f"NMI {nmi}")
    check(a["kind"] == "nmi" and a["record"]["nmi"] == nmi, "not the customer record")
    check(a["record"].get("zs_id", "").startswith("ZS-"), "customer has no zone substation")
    check(isinstance(a["intervals"], list), "intervals not a list")
    x.note(f"NMI {nmi}: {len(a['intervals'])} meter intervals in 3 days")


@test("network")
def drawer_unknown_asset_404(x):
    """An unknown asset id gets a 404 (the drawer says not found)."""
    r = x.c.get("/api/asset/NO-SUCH-ASSET-E2E")
    check(r.status == 404, f"HTTP {r.status} for an unknown asset (expected 404)")


@test("network")
def map_network_layer(x):
    """Network map layer: every feeder with its geometry and the zone substations."""
    m = want(x.c.get("/api/map/network"), 200, "map network")
    check(len(m["feeders"]) >= 40, f"only {len(m['feeders'])} feeders on the map")
    check(len(m["zones"]) >= 10, f"only {len(m['zones'])} zones on the map")
    x.note(f"{len(m['feeders'])} feeders, {len(m['zones'])} zones")


@test("network")
def map_live_layer(x):
    """Live layer: the active outages and the crew positions on the map."""
    m = want(x.c.get("/api/map/live"), 200, "map live")
    check(isinstance(m["outages"], list) and isinstance(m["crews"], list), "outages or crews not lists")
    check(len(m["crews"]) > 0, "no crew positions on the live map")
    x.note(f"{len(m['outages'])} outages, {len(m['crews'])} crews")


@test("network")
def map_fire_layer(x):
    """Fire layer: NSW fire incidents, DEA hotspots and attribution."""
    m = want(x.c.get("/api/map/fire"), 200, "map fire")
    check(isinstance(m["fires"], list) and isinstance(m["hotspots"], list), "fire layers not lists")
    check("exercise" not in m, "the fire layer still carries exercise fires")
    check(m["attribution"], "no attribution for the fire layers")
    x.note(f"{len(m['fires'])} fires, {len(m['hotspots'])} hotspots")


@test("network")
def map_weather_layer(x):
    """Weather layer: the current forecast hour is served without a time, and a chosen hour is served back."""
    w = want(x.c.get("/api/map/weather"), 200, "map weather")
    check(len(w["times"]) >= 6 and w["time"], "no forecast hours")
    check(len(w["cells"]) > 10, f"only {len(w['cells'])} weather cells")
    t = w["times"][2]
    w2 = want(x.c.get(f"/api/map/weather?t={q(t)}"), 200, "map weather at t")
    check(w2["time"] == t, f"served {w2['time']} for requested {t}")
    check(len(w2["cells"]) > 10, "no cells for the requested hour")
    x.note(f"{len(w['times'])} hours; served {t}")


def circle_at_feeder(x, feeder_id=None, radius=800):
    """A circle around a feeder's centre (the first feeder of the first region when none is named)."""
    reg = "BLK"
    f = want(x.c.get(f"/api/rm/feeders?region={reg}"), 200, "feeders")["feeders"]
    f = next((f for f in f if f["feeder_id"] == feeder_id), None) if feeder_id else f[0]
    return dict(shape="circle", lat=f["lat"], lon=f["lon"], radius_m=radius), f


@test("network")
def spatial_circle(x):
    """Spatial query, circle: customers, assets, distribution substations and the SQL panel for a 800 m circle over a feeder."""
    area, f = circle_at_feeder(x, "CTC-F01", 800)
    r = want(x.c.post("/api/spatial/area", area), 200, "spatial circle")
    t = r["totals"]
    check(r["area"]["shape"] == "circle" and r["area"]["radius_m"] == 800, f"area echoed as {r['area']}")
    check(t["customers"] > 0 and t["sms"] <= t["customers"], f"totals {brief(t, 200)}")
    check(t["sms"] <= t["customers"] and t["no_channel"] <= t["customers"], "channel totals exceed the customer count")
    check(r["suburbs"] and r["suburb_count"] >= 1, "no suburbs")
    check(len(r["sql"]) == 3 and all("ST_" in s_ for s_ in r["sql"]), "SQL panel does not show three PostGIS statements")
    x.note(f"{t['customers']:,} customers, {t['sms']:,} SMS opt-ins, {r['suburb_count']} suburbs")


@test("network")
def spatial_polygon(x):
    """Spatial query, drawn polygon: customers and assets inside a four-point polygon around the same feeder."""
    _, f = circle_at_feeder(x, "CTC-F01")
    pts = [[f["lat"] + 0.01, f["lon"] - 0.01], [f["lat"] + 0.01, f["lon"] + 0.01], [f["lat"] - 0.01, f["lon"] + 0.01],
           [f["lat"] - 0.01, f["lon"] - 0.01]]
    r = want(x.c.post("/api/spatial/area", dict(shape="polygon", points=pts)), 200, "spatial polygon")
    check(r["area"]["shape"] == "polygon" and r["area"]["points"] == 4, f"area {r['area']}")
    check(r["totals"]["customers"] > 0, "no customers inside the polygon")
    x.note(f"{r['totals']['customers']:,} customers in the polygon")


@test("network")
def spatial_polygon_too_small(x):
    """A polygon with fewer than three points is refused with a message (not a crash)."""
    r = x.c.post("/api/spatial/area", dict(shape="polygon", points=[[-33.7, 150.9], [-33.8, 150.9]]))
    check(r.status == 400, f"HTTP {r.status} (expected 400)")
    check(isinstance(r.body, dict) and "three points" in json.dumps(r.body), f"no message: {brief(r.body, 160)}")


@test("network")
def notify_area_matches_opt_ins(x):
    """Notify area dialog: a tiny circle over a feeder queues exactly one notice per SMS opt-in (everyone, then life support only)."""
    area, f = circle_at_feeder(x, "CTC-F01", 300)
    impact = want(x.c.post("/api/spatial/area", area), 200, "impact for notify")
    t = impact["totals"]
    check(t["customers"] > 0, "the tiny area has no customers; pick another feeder")
    r = want(x.c.post("/api/notify/area", dict(area=area, body="E2E area notice", channels=["sms"], audience="all")), 200, "notify area")
    check(r["sent"] == t["sms"], f"sent {r['sent']} != SMS opt-ins {t['sms']} in the area")
    check(r["by_channel"] == {"sms": t["sms"]}, f"by channel {r['by_channel']}")
    check(r["customers_in_area"] == t["customers"], f"customers in area {r['customers_in_area']} != impact {t['customers']}")
    check(r["customers_reached"] <= r["customers_in_area"], "more customers reached than live in the area")
    ls = want(x.c.post("/api/notify/area", dict(area=area, body="E2E life support notice", channels=["sms"], audience="life_support")), 200,
              "notify life support")
    check(ls["sent"] == t["ls_sms"], f"life support sent {ls['sent']} != life support SMS opt-ins {t['ls_sms']}")
    x.note(f"everyone: {r['sent']:,} SMS to {r['customers_reached']:,} customers; life support: {ls['sent']}")


@test("network")
def notify_area_refuses_empty_body(x):
    """Notify area with no message or no channel queues nothing."""
    area, _ = circle_at_feeder(x, "CTC-F01", 300)
    r = want(x.c.post("/api/notify/area", dict(area=area, body="", channels=["sms"], audience="all")), 200, "notify empty")
    check(r["sent"] == 0 and "Choose at least one channel" in r["message"], f"empty body sent {brief(r, 160)}")


# ============================================================================ 3D scenes
@test("scene")
def scene_sites_list(x):
    """3D site list: every zone substation with its feeders, each feeder opening a scene of one of the three kinds."""
    sites = want(x.c.get("/api/scene-sites"), 200, "scene sites")
    check(len(sites) >= 10, f"only {len(sites)} zones")
    kinds = set()
    for z in sites:
        if z.get("kind") == "transmission":   # transmission sites carry ts_id and their zones, not feeders
            check(str(z.get("ts_id", "")).startswith("TS-"), f"transmission site without a TS id: {brief(z, 120)}")
            kinds.add("transmission")
            continue
        check(z["zs_id"].startswith("ZS-") and z["feeders"], f"zone {z['zs_id']} has no feeders")
        for f in z["feeders"]:
            kinds.add(f["scene"])
    check(SCENE_KINDS <= kinds, f"scene kinds found {kinds}")
    check("transmission" in kinds, "the 3D site list has no transmission site")
    x.note(f"{len(sites)} zones; scenes: {sorted(kinds)}")


def first_feeder_with_scene(x, scene):
    for z in want(x.c.get("/api/scene-sites"), 200, "scene sites"):
        for f in z["feeders"]:
            if f["scene"] == scene:
                return f["feeder_id"]
    raise Skip(f"no feeder with the {scene} scene")


@test("scene")
def scene_zone(x):
    """Zone scene: the zone substation with its transformers and feeders."""
    s = want(x.c.get("/api/scene/zone/ZS-KNV"), 200, "zone scene")
    check(s["kind"] == "zone" and "zone substation" in s["title"].lower(), f"title {s['title']}")
    check(len(s["transformers"]) >= 1 and len(s["feeders"]) >= 1, "no transformers or feeders in the zone scene")


@test("scene")
def scene_street_feeder(x):
    """Street feeder scene (overhead, urban): poles, spans and customers."""
    fid = first_feeder_with_scene(x, "Overhead street")
    s = want(x.c.get(f"/api/scene/street/{q(fid)}"), 200, f"street {fid}")
    check(s["kind"] == "street" and s["feeder_id"] == fid, "wrong scene")
    check(len(s["poles"]) >= 3 and len(s["spans"]) >= 3, f"poles {len(s['poles'])}, spans {len(s['spans'])}")
    check(s["customers"]["n"] > 0, "no customers on the street scene")
    x.note(f"{fid}: {len(s['poles'])} poles, {len(s['spans'])} spans")


@test("scene")
def scene_rural_bushfire_feeder(x):
    """Rural bushfire scene: the bushfire-prone line with its fire rating, district and vegetation crew."""
    fid = first_feeder_with_scene(x, "Bushfire-prone line")
    s = want(x.c.get(f"/api/scene/rural/{q(fid)}"), 200, f"rural {fid}")
    check(s["kind"] == "rural" and "bushfire" in s["title"].lower(), f"title {s['title']}")
    check(len(s["poles"]) >= 3 and len(s["spans"]) >= 1, "no poles or spans in the rural scene")
    check(s["fire_rating_label"] and s["fire_district"], "no fire rating or district")
    x.note(f"{fid}: {s['fire_rating_label']} ({s['fire_district']})")


@test("scene")
def scene_underground_estate_feeder(x):
    """Underground estate scene: kiosk substations serving the estate, with the customers."""
    fid = first_feeder_with_scene(x, "Underground estate")
    s = want(x.c.get(f"/api/scene/estate/{q(fid)}"), 200, f"estate {fid}")
    check(s["kind"] == "estate" and s["feeder_id"] == fid, "wrong scene")
    check(len(s["kiosks"]) >= 1, "no kiosks in the estate")
    check(s["customers"]["n"] > 0, "no customers in the estate scene")
    x.note(f"{fid}: {len(s['kiosks'])} kiosks, {s['customers']['n']} customers")


@test("scene")
def scene_feeder_route_picks_scene(x):
    """The generic feeder route opens the right scene for the feeder's kind (bushfire line gives the rural scene)."""
    fid = first_feeder_with_scene(x, "Bushfire-prone line")
    s = want(x.c.get(f"/api/scene/feeder/{q(fid)}"), 200, f"feeder {fid}")
    check(s["kind"] == "rural", f"kind {s['kind']} for a bushfire-prone feeder")


@test("scene")
def scene_speech(x):
    """Speech for a 3D asset: the LLM briefing text, or a graceful fallback line when the model is unavailable."""
    body = want(x.c.post("/api/scene/speech", dict(asset_id="RPR-F03-CB01", facts=dict(label="RPR-F03-CB01", status="red"))), 200, "speech")
    text = body.get("text") or ""
    check(len(text) >= 20, f"speech text too short: {brief(body, 160)}")
    check("mate" not in text.lower(), "speech says 'mate'")
    if "boss" in text.lower() and "needs a crew" in text.lower():
        x.note("speech used the fallback line (the LLM was not available)")
    else:
        x.note("speech returned model text")


# ============================================================================ resource management
@test("resources")
def rm_meta_regions(x):
    """Resource management meta: the five regions and the planning day (no lenses or storm state: each region has one plan)."""
    m = want(x.c.get("/api/rm/meta"), 200, "rm meta")
    check({r["id"] for r in m["regions"]} == set(REGION_IDS), f"regions {[r['id'] for r in m['regions']]}")
    check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["day"] or ""), f"day {m['day']}")
    check("lenses" not in m and "storm" not in m, "meta still carries lenses or the storm state")
    x.note(f"day {m['day']}")


@test("resources")
def rm_roster_every_region(x):
    """The plan for every region: KPIs, assignments with crews and jobs, and deferred work."""
    total = 0
    for reg in REGION_IDS:
        r = roster(x, reg)
        k = r["kpis"]
        check(r["region"] == reg, f"echo mismatch for {reg}")
        check(k["jobs_assigned"] > 0 and k["crew_deployed"] > 0, f"{reg} has no work: {brief(k, 160)}")
        check("avg_utilisation_pct" not in k, f"{reg} still reports utilisation")
        check(k["total_cost_aud"] > 0 and k["total_travel_km"] >= 0, f"{reg} cost/travel implausible")
        check(r["assignments_total"] >= len(r["assignments"]) and len(r["assignments"]) > 0, f"{reg} assignments")
        a = r["assignments"][0]
        check(a["crew_id"] and a["job_id"] and a["job_type_label"], f"{reg} assignment missing fields")
        total += k["jobs_assigned"]
    x.note(f"{total:,} jobs assigned across 5 regions")


@test("resources")
def rm_crew_summary_and_feeders(x):
    """Crew summary (available crews by trade) and the feeders of Blacktown with customers and stress."""
    s = want(x.c.get("/api/rm/crew-summary?region=BLK"), 200, "crew summary")
    check(s["available"] > 0 and sum(s["by_persona"].values()) == s["available"], f"summary {brief(s, 200)}")
    check(0 <= s["contractors"] <= s["available"], "contractors out of range")
    f = want(x.c.get("/api/rm/feeders?region=BLK"), 200, "feeders")["feeders"]
    check(len(f) >= 5 and all(r["region_id"] == "BLK" and r["customers"] > 0 for r in f), "feeders not Blacktown or no customers")
    check(all(0 <= r["stress_level"] <= 2 for r in f), "stress level out of range")
    x.note(f"{s['available']} crews available, {len(f)} feeders")


@test("resources")
def rm_positions_and_crew_drawer(x):
    """Crew positions for Blacktown and the crew drawer (skills, tickets, stops and position) for one crew; unknown crews are 404."""
    p = want(x.c.get("/api/rm/positions?region=BLK"), 200, "positions")["crews"]
    check(len(p) > 0 and all(c["region_id"] == "BLK" for c in p), "positions not all Blacktown")
    check(all(c["status"] in ("not_started", "travelling", "on_site", "available", "finished", "stood_down", "leave", "training",
                              "incident") for c in p), f"status set {set(c['status'] for c in p)}")
    cid = p[0]["crew_id"]
    d = want(x.c.get(f"/api/rm/crew/{q(cid)}"), 200, f"crew {cid}")
    check(d["crew"]["crew_id"] == cid and d["crew"]["region_name"] == "Blacktown", "drawer is not the crew")
    check(isinstance(d["crew"]["skills"], list) and isinstance(d["tickets"], list) and isinstance(d["stops"], list), "drawer lists missing")
    check(d["hours_on_shift"] is not None, "no hours on shift")
    nf = x.c.get("/api/rm/crew/NO-SUCH-CREW-E2E")
    check(nf.status == 404, f"unknown crew HTTP {nf.status}")
    x.note(f"{cid}: {len(d['stops'])} stops, {len(d['tickets'])} tickets")


@test("resources")
def rm_parked_list(x):
    """On-hold list for Blacktown: each held job with its reason, and the count matches the list."""
    body = want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")
    check(body["count"] == len(body["parked"]), "count does not match the list")
    check(all("job_type_label" in p for p in body["parked"]), "a held job has no type label")
    x.note(f"{body['count']} on hold")


@test("resources")
def rm_hold_then_redispatch(x):
    """Put one planned stop on hold (it leaves the roster and joins the on-hold list), then re-dispatch it: back on a crew's day."""
    r = roster(x, "BLK")
    job = next((a for a in r["assignments"] if not a["is_emergency"]), None)
    check(job is not None, "no planned job to hold")
    jid, cid = job["job_id"], job["crew_id"]

    def unhold():
        parked = want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]
        if any(p["job_id"] == jid for p in parked):
            x.c.post("/api/rm/redispatch", dict(region="BLK", job_id=jid, crew_id=cid))
        if any(p["job_id"] == jid for p in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]):
            x.c.post("/api/field/unpark", dict(region="BLK", job_id=jid))
    x.defer(unhold, "release the held job")
    h = want(x.c.post("/api/rm/hold", dict(region="BLK", job_id=jid, crew_id=cid, reason_label="E2E test hold")), 200, "hold")
    check(h["ok"] and h["parked_count"] >= 1, f"hold {h}")
    parked = {p["job_id"]: p for p in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]}
    check(jid in parked and parked[jid]["reason_label"] == "E2E test hold", f"held job {jid} not on the list")
    check(jid not in {a["job_id"] for a in roster(x, "BLK")["assignments"]}, "held job still on the roster")
    rd = want(x.c.post("/api/rm/redispatch", dict(region="BLK", job_id=jid, crew_id=cid)), 200, "redispatch")
    check(rd["ok"] and rd["assigned_to"]["crew_id"], f"redispatch {rd}")
    check(jid not in {p["job_id"] for p in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]}, "job still on hold")
    x.note(f"{jid} held then re-dispatched to {rd['assigned_to']['crew_name']}")


@test("resources")
def rm_candidates_list(x):
    """Candidate crews for a job: the current crew, and the others with eligibility, load, distance and status (ineligible ones are listed with a reason)."""
    job = next(a for a in roster(x, "BLK")["assignments"] if not a["is_emergency"])
    c = want(x.c.get(f"/api/rm/candidates?region=BLK&job_id={q(job['job_id'])}"), 200, "candidates")
    check(c["job"]["job_id"] == job["job_id"] and c["current"]["crew_id"] == job["crew_id"], "job or current crew wrong")
    cands = c["candidates"]
    check(len(cands) >= 2, f"only {len(cands)} candidates")
    check(all(isinstance(x_["eligible"], bool) and x_["km"] >= 0 and x_["status"] for x_ in cands), "a candidate has no eligibility, distance or status")
    check(sum(1 for x_ in cands if x_["eligible"]) >= 2, "fewer than two eligible candidates")
    x.note(f"{len(cands)} candidates for {job['job_id']}, {sum(1 for x_ in cands if x_['eligible'])} eligible")


@test("resources")
def rm_plan_then_apply_assign(x):
    """Plan an assignment to another crew (feasible), apply it (the roster shows the new crew), then apply the original crew back."""
    job = next(a for a in roster(x, "BLK")["assignments"] if not a["is_emergency"])
    jid, orig = job["job_id"], job["crew_id"]
    cands = want(x.c.get(f"/api/rm/candidates?region=BLK&job_id={q(jid)}"), 200, "candidates")["candidates"]
    other = None
    for cand in cands:
        if cand["crew_id"] == orig:
            continue
        p = want(x.c.post("/api/rm/plan-assign", dict(region="BLK", job_id=jid, crew_id=cand["crew_id"])), 200, "plan-assign")
        if p.get("ok"):
            other = cand["crew_id"]
            break
    check(other is not None, "no alternative crew could take the job in plan")
    x.defer(lambda: x.c.post("/api/rm/apply-assign", dict(region="BLK", job_id=jid, crew_id=orig)), "return job to the original crew")
    a = want(x.c.post("/api/rm/apply-assign", dict(region="BLK", job_id=jid, crew_id=other, handoffs=[], override=False)), 200, "apply-assign")
    check(a["ok"] and any(c["job_id"] == jid and c["crew_id"] == other for c in a["changed"]), f"apply changed {a['changed']}")
    now = {x_["job_id"]: x_["crew_id"] for x_ in roster(x, "BLK")["assignments"]}
    check(now.get(jid) == other, f"roster shows {now.get(jid)} for {jid}, expected {other}")
    back = want(x.c.post("/api/rm/apply-assign", dict(region="BLK", job_id=jid, crew_id=orig)), 200, "apply-assign back")
    check(back["ok"], "could not return the job to its original crew")
    now = {x_["job_id"]: x_["crew_id"] for x_ in roster(x, "BLK")["assignments"]}
    check(now.get(jid) == orig, f"after restore roster shows {now.get(jid)}, expected {orig}")
    x.note(f"{jid}: {orig} -> {other} -> {orig}")


@test("resources")
def rm_policy_get_and_set(x):
    """Planning exceptions: read the policy, change it so the plan shows it, then restore the original policy."""
    before = get_policy_now(x, "BLK")
    check({"incident_all", "incident_allow", "fatigue_all", "fatigue_allow"} <= set(before), f"policy keys {before}")
    x.defer(lambda: x.c.post("/api/rm/policy", dict(region="BLK", **before)), "restore planning exceptions")
    changed = dict(before, fatigue_all=not before["fatigue_all"])
    want(x.c.post("/api/rm/policy", dict(region="BLK", **changed)), 200, "set policy")
    after = get_policy_now(x, "BLK")
    check(after["fatigue_all"] == changed["fatigue_all"], "policy change not visible on GET")
    want(x.c.post("/api/rm/policy", dict(region="BLK", **before)), 200, "restore policy")
    check(get_policy_now(x, "BLK") == before, "policy not restored")
    x.note("fatigue exception toggled and restored")


@test("resources")
def rm_exec_summary(x):
    """Executive summary: every region's field plan with jobs, cost and travel."""
    e = want(x.c.get("/api/rm/exec"), 200, "exec")
    check(len(e["regions"]) == 5 and e["day"], "exec summary not for five regions")
    check(all(r["jobs_assigned"] > 0 and r["total_cost_aud"] > 0 for r in e["regions"]), "a region has no work or cost")
    x.note(f"{sum(r['jobs_assigned'] for r in e['regions']):,} jobs across regions")


@test("resources")
def rm_expiring_tickets(x):
    """Expiring and expired tickets: a total of people with an expired ticket, and per-ticket counts (a person can hold several)."""
    e = want(x.c.get("/api/rm/expiring"), 200, "expiring")
    check(e["expired_total"] > 0 and e["by_ticket"], "no expired tickets listed")
    check(all(0 < t["n"] <= e["expired_total"] for t in e["by_ticket"]), "a ticket count is above the people total")
    check(sum(t["n"] for t in e["by_ticket"]) >= e["expired_total"], "ticket counts fall short of the people total")
    x.note(f"{e['expired_total']} people with an expired ticket; {sum(t['n'] for t in e['by_ticket'])} ticket records")


@test("resources")
def rm_expiring_detail(x):
    """Expiring-ticket detail for the largest ticket type: a page of crews with the scope and paging fields."""
    e = want(x.c.get("/api/rm/expiring"), 200, "expiring")
    code = e["by_ticket"][0]["ticket_code"]
    d = want(x.c.get(f"/api/rm/expiring/detail?ticket_code={q(code)}&scope=at_risk&days=45&offset=0&limit=200"), 200, "expiring detail")
    check(d["ticket_code"] == code and d["scope"] == "at_risk", "echo mismatch")
    check(d["returned"] == len(d["items"]) and d["total"] >= d["returned"], "paging counts wrong")
    x.note(f"{code}: {d['total']} at risk, page of {d['returned']}")


@test("resources")
def rm_rerostered_and_overrides(x):
    """The re-rostered and overrides lists are consistent with their counts."""
    rr = want(x.c.get("/api/rm/rerostered"), 200, "rerostered")
    check(rr["count"] == len(rr["rerostered"]), "re-rostered count mismatch")
    ov = want(x.c.get("/api/rm/overrides"), 200, "overrides")
    check(ov["count"] == len(ov["overrides"]), "overrides count mismatch")
    x.note(f"{rr['count']} re-rostered, {ov['count']} overrides")


@test("resources")
def rm_crew_roster_for_region(x):
    """The crew roster for a region: every crew in it with its jobs for the day and whether it was moved in."""
    d = want(x.c.get("/api/rm/crew-roster?region=PEN"), 200, "crew roster")
    check(d["count"] == len(d["crew"]) and d["count"] > 10, f"count {d['count']}")
    check(all(c["crew_id"].startswith("PEN") or c["moved"] for c in d["crew"]), "a crew with no move is not from Penrith")
    x.note(f"{d['count']} crews in Penrith")


@test("resources")
def rm_move_region_and_reset(x):
    """Move a Blacktown crew to Penrith (it appears on the Penrith list, flagged as moved), then reset it to Blacktown (back on its list)."""
    rost = want(x.c.get("/api/rm/crew-roster?region=BLK"), 200, "crew roster")["crew"]
    crew = next((c for c in rost if not c["moved"] and c["jobs_today"] > 0), None)
    check(crew is not None, "no Blacktown crew with work to move")
    cid = crew["crew_id"]
    x.defer(lambda: x.c.post("/api/rm/reset-region", dict(crew_id=cid)), "reset the crew's region")
    m = want(x.c.post("/api/rm/move-region", dict(crew_id=cid, to_region="PEN")), 200, "move region")
    check(m["ok"] and m["to_region"] == "Penrith", f"move {brief(m, 200)}")
    check(cid not in ids(x, "BLK"), "crew still on the Blacktown list after the move")
    pen = {c["crew_id"]: c for c in want(x.c.get("/api/rm/crew-roster?region=PEN"), 200, "roster")["crew"]}
    check(cid in pen and pen[cid]["moved"], "moved crew not shown on the Penrith list")
    r = want(x.c.post("/api/rm/reset-region", dict(crew_id=cid)), 200, "reset region")
    check(r["ok"] and r["restored_to"] == "Blacktown", f"reset {r}")
    polls = poll_until(lambda: cid in ids(x, "BLK"))
    check(polls is not None, "crew not back on the Blacktown list after reset (5 reads, 1 s apart)")
    x.note(f"{cid}: moved ({m['stops']} stops, {m['reassigned']} re-rostered, {m['parked']} held), reset; back after {polls} read(s)")


@test("resources")
def rm_reset_releases_move_holds(x):
    """Undo of a move (reset-region) should release the stops the move put on hold; it leaves them on hold with the move's reason."""
    rost = want(x.c.get("/api/rm/crew-roster?region=BLK"), 200, "crew roster")["crew"]
    for crew in [c for c in rost if not c["moved"] and c["jobs_today"] > 0][:15]:
        cid = crew["crew_id"]
        x.defer(lambda cid=cid: x.c.post("/api/rm/reset-region", dict(crew_id=cid)), "reset the crew's region")
        m = want(x.c.post("/api/rm/move-region", dict(crew_id=cid, to_region="PEN")), 200, "move region")
        if m["parked"] == 0:
            want(x.c.post("/api/rm/reset-region", dict(crew_id=cid)), 200, "reset region")
            continue
        held = set(m["parked_job_ids"])
        want(x.c.post("/api/rm/reset-region", dict(crew_id=cid)), 200, "reset region")
        still = held & {h["job_id"] for h in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked BLK")["parked"]}
        for jid in held:   # release the holds this test created, whatever the outcome
            x.defer(lambda jid=jid: x.c.post("/api/field/unpark", dict(region="BLK", job_id=jid)), "release a move hold")
        check(not still, f"{len(still)} of {len(held)} stops still on hold after reset-region ({cid}), e.g. {sorted(still)[:3]}")
        x.note(f"{cid}: {len(held)} stops held by the move were released by the reset")
        return
    raise Skip("no Blacktown crew whose move leaves stops on hold in the live data")


@test("resources")
def rm_stand_down_then_restore(x):
    """Stand a Blacktown crew down (its stops are re-rostered, it is on the re-rostered list), then restore the crew to the roster."""
    rost = want(x.c.get("/api/rm/crew-roster?region=BLK"), 200, "crew roster")["crew"]
    crew = next((c for c in rost if not c["moved"] and c["jobs_today"] > 0), None)
    check(crew is not None, "no Blacktown crew with work")
    cid = crew["crew_id"]
    x.defer(lambda: x.c.post("/api/rm/restore-crew", dict(region="BLK", crew_id=cid)), "restore the stood-down crew")
    s = want(x.c.post("/api/rm/stand-down", dict(region="BLK", crew_id=cid, reason_label="E2E stand-down test")), 200, "stand down")
    check(s.get("count", 0) >= 1, f"stand-down {brief(s, 200)}")
    rer = {r["crew_id"]: r for r in want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["rerostered"]}
    check(cid in rer and rer[cid]["count"] == s["count"], f"crew not on the re-rostered list: {brief(rer.get(cid), 160)}")
    d = want(x.c.get(f"/api/rm/crew/{q(cid)}"), 200, "crew drawer")
    check(d["stood_down"] is not None, "crew drawer does not show the stand-down")
    r = want(x.c.post("/api/rm/restore-crew", dict(region="BLK", crew_id=cid)), 200, "restore crew")
    check(r["ok"], f"restore {r}")
    rer = {r_["crew_id"] for r_ in want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["rerostered"]}
    check(cid not in rer, "crew still on the re-rostered list after restore")
    check(want(x.c.get(f"/api/rm/crew/{q(cid)}"), 200, "crew drawer")["stood_down"] is None, "crew still stood down in the drawer")
    x.note(f"{cid}: {s['count']} stops re-rostered, then restored")


@test("resources")
def rm_raise_job(x):
    """Raise a service call on a Blacktown feeder: the job gets an id and a region and appears on the roster."""
    f = want(x.c.get("/api/rm/feeders?region=BLK"), 200, "feeders")["feeders"][0]
    body = dict(job_type="SERVICE_CALL", title=f"E2E service call {int(time.time())}", feeder_id=f["feeder_id"], priority=4, customers=1,
                duration_min=30, emergency=False)
    r = want(x.c.post("/api/rm/job", body), 200, "raise job")
    check(r["ok"] and r["job_id"].startswith("JOB-") and r["region"] == "BLK", f"raise {brief(r, 200)}")
    x.S["raised_job"] = r["job_id"]
    ro = roster(x, "BLK")
    listed = {a["job_id"] for a in ro["assignments"]} | {d["job_id"] for d in ro["deferred"]}
    check(r["job_id"] in listed, f"raised job {r['job_id']} is not on the Blacktown roster")
    x.note(f"{r['job_id']} raised on {f['feeder_id']} (withdraw is only possible through the situation undo; the final demo reset clears it)")


@test("resources")
def rm_nearest_crews(x):
    """Nearest crews for the raised job: sorted with the free crews first, with distance, ETA and why any are blocked."""
    jid = x.S.get("raised_job")
    if not jid:
        raise Skip("no raised job from the previous test")
    n = want(x.c.get(f"/api/rm/nearest?job_id={q(jid)}"), 200, "nearest")["crews"]
    check(len(n) >= 1, "no crews at all for the job")
    check(all(c["km"] >= 0 and c["eta_min"] >= 0 for c in n), "distance or ETA negative")
    free = [c for c in n if not c["blocked"]]
    check(not free or n[0]["blocked"] is None, "a free crew exists but the nearest list does not start with a free one")
    x.S["nearest_free"] = free[0]["crew_id"] if free else None
    x.note(f"{len(n)} crews; {len(free)} free")


@test("resources")
def rm_dispatch_to_raised_job(x):
    """Dispatch the nearest free crew to the raised job: the crew is on the job in positions (recall is the situation undo; see the report)."""
    jid, cid = x.S.get("raised_job"), x.S.get("nearest_free")
    if not jid or not cid:
        raise Skip("no raised job or no free crew to dispatch")
    d = want(x.c.post("/api/rm/dispatch", dict(job_id=jid, crew_id=cid)), 200, "dispatch")
    check(d["ok"] and d["crew_id"] == cid and d["eta_min"] >= 0, f"dispatch {brief(d, 200)}")
    p = positions(x, "BLK")[cid]
    check(p.get("job_id") == jid and p["status"] == "travelling", f"crew {cid} after dispatch: {p.get('status')} {p.get('job_id')}")
    x.note(f"{cid} dispatched, {d['km']} km, ETA {d['eta_min']} min; the live engine would drive it (not waited for)")


@test("resources")
def rm_phone_link_read(x):
    """The phone field link (read only): a dictionary with the url, the QR code and the time it was shared, or nothing yet."""
    p = want(x.c.get("/api/rm/phone-link"), 200, "phone link")
    check(isinstance(p, dict) and ({"url", "qr", "at"} <= set(p) or p == {"at": None}), f"keys {sorted(p)}")   # {"at": None}: never shared
    x.note("read only (POST not exercised)")


@test("resources")
def rm_job_types(x):
    """Job types for raising a job: codes with readable labels (no raw codes shown to users)."""
    t = want(x.c.get("/api/rm/job-types"), 200, "job types")["types"]
    codes = {j["code"] for j in t}
    check({"FAULT_RESPONSE", "SERVICE_CALL", "VEGETATION", "HV_MAINT"} <= codes, f"codes {sorted(codes)}")
    check(all(j["label"] and "_" not in j["label"] for j in t), "a job type label is a raw code")
    x.note(f"{len(t)} job types")


# ============================================================================ field app
@test("field")
def field_page_loads(x):
    """The phone field app page is served with its script and styles."""
    r = x.c.get("/field")
    check(r.status == 200, f"HTTP {r.status} for /field")
    html = r.body if isinstance(r.body, str) else ""
    check("<html" in html.lower() and "field.js" in html and "field.css" in html, "field.html does not reference its script and styles")


@test("field")
def field_meta(x):
    """Field app meta: the five regions and the default region."""
    m = want(x.c.get("/api/field/meta"), 200, "field meta")
    check({r["id"] for r in m["regions"]} == set(REGION_IDS) and m["default_region"] in REGION_IDS, f"meta {brief(m, 160)}")


@test("field")
def field_workers(x):
    """Workers picker for Blacktown: crews with a day, their name and job count."""
    w = want(x.c.get("/api/field/workers?region=BLK"), 200, "workers")["workers"]
    check(len(w) >= 5, f"only {len(w)} workers")
    check(all(c["crew_id"].startswith("BLK") and c["name"] and c["jobs"] >= 0 for c in w), "a worker is malformed")
    x.note(f"{len(w)} workers, {sum(1 for c in w if c['stood_down'])} stood down")


def pick_itinerary(x):
    """A Blacktown crew with at least three stops and not stood down: its itinerary."""
    for w in want(x.c.get("/api/field/workers?region=BLK"), 200, "workers")["workers"]:
        if w["jobs"] >= 3 and not w["stood_down"]:
            it = want(x.c.get(f"/api/field/itinerary?region=BLK&crew_id={q(w['crew_id'])}"), 200, "itinerary")
            if len(it["jobs"]) >= 3:
                return it
    raise Skip("no Blacktown crew with three or more stops")


@test("field")
def field_itinerary(x):
    """A crew's day in the field app: ordered stops with type, window, location, duration and status."""
    it = pick_itinerary(x)
    check(it["crew"]["id"] and it["home"]["lat"], "crew or home missing")
    check(it["max_shift_min"] > 0 and it["shift_start_min"] > 0, "shift limits missing")
    j = it["jobs"][0]
    for k in ("job_id", "type_label", "title", "lat", "lon", "duration_min", "window_start_min", "window_end_min", "fixed"):
        check(k in j, f"stop is missing {k}")
    check(all(s["window_start_min"] <= s["window_end_min"] for s in it["jobs"]), "a window ends before it starts")
    check(isinstance(it["job_status"], dict), "job_status not a dictionary")
    x.note(f"{it['crew']['name']}: {len(it['jobs'])} stops")


@test("field")
def field_reorder_then_restore(x):
    """Reorder a crew's stops (reversed), see the new order on the next read (non-emergency stops), then restore the original order."""
    it = pick_itinerary(x)
    crew, original = it["crew"]["id"], [j["job_id"] for j in it["jobs"]]
    x.defer(lambda: x.c.post("/api/field/reorder", dict(region="BLK", crew_id=crew, order=original)), "restore stop order")
    reversed_order = list(reversed(original))
    want(x.c.post("/api/field/reorder", dict(region="BLK", crew_id=crew, order=reversed_order)), 200, "reorder")
    after = want(x.c.get(f"/api/field/itinerary?region=BLK&crew_id={q(crew)}"), 200, "itinerary")
    got = [j["job_id"] for j in after["jobs"]]
    ne = [j for j in after["jobs"] if not j["is_emergency"]]
    ne_ids = [j["job_id"] for j in ne]
    expected = [j for j in reversed_order if j in set(ne_ids)]
    check(ne_ids == expected or len(ne_ids) < 2, f"non-emergency order {ne_ids} != reversed {expected}")
    check(sorted(got) == sorted(original), "reorder dropped or added stops")
    want(x.c.post("/api/field/reorder", dict(region="BLK", crew_id=crew, order=original)), 200, "restore order")
    back = [j["job_id"] for j in want(x.c.get(f"/api/field/itinerary?region=BLK&crew_id={q(crew)}"), 200, "itinerary")["jobs"]]
    check([j for j in back if j in set(ne_ids)] == [j for j in original if j in set(ne_ids)], "original order not restored")
    x.note(f"{crew}: {len(original)} stops reversed and restored")


@test("field")
def field_status_changes_restore(x):
    """Set a stop in progress, then done, then planned again: the status shows on each read and the stop ends planned."""
    it = pick_itinerary(x)
    crew = it["crew"]["id"]
    job = next(j for j in it["jobs"] if not j["is_emergency"] and it["job_status"].get(j["job_id"], "planned") == "planned")
    jid = job["job_id"]
    x.defer(lambda: x.c.post("/api/field/status", dict(region="BLK", job_id=jid, status="planned")), "restore planned")
    for st in ("in_progress", "done", "planned"):
        want(x.c.post("/api/field/status", dict(region="BLK", job_id=jid, status=st)), 200, f"status {st}")
        now = want(x.c.get(f"/api/field/itinerary?region=BLK&crew_id={q(crew)}"), 200, "itinerary")["job_status"].get(jid, "planned")
        check(now == st, f"status shows {now}, expected {st}")
    x.note(f"{jid}: in progress, done, back to planned")


@test("field")
def field_park_and_unpark(x):
    """Park a stop from the field (it leaves the day and joins the on-hold list), then unpark it (it returns to the day)."""
    it = pick_itinerary(x)
    crew = it["crew"]["id"]
    job = next(j for j in it["jobs"] if not j["is_emergency"])
    jid = job["job_id"]
    x.defer(lambda: x.c.post("/api/field/unpark", dict(region="BLK", job_id=jid)), "unpark the stop")
    p = want(x.c.post("/api/field/park", dict(region="BLK", crew_id=crew, job_id=jid, reason="blocked", reason_label="E2E blocked")), 200, "park")
    check(p["ok"] and p["parked_count"] >= 1, f"park {p}")
    held = {h["job_id"]: h for h in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]}
    check(jid in held and held[jid]["reason_label"] == "E2E blocked", "parked stop not on the on-hold list")
    want(x.c.post("/api/field/unpark", dict(region="BLK", job_id=jid)), 200, "unpark")
    check(jid not in {h["job_id"] for h in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]}, "still on hold after unpark")
    x.note(f"{jid} parked then unparked")


@test("field")
def field_reassign_then_restore(x):
    """Hand one stop to another crew from the field (job-level reassign, the crew stays on the roster), then return it to the original crew."""
    it = pick_itinerary(x)
    crew = it["crew"]["id"]
    job = next(j for j in it["jobs"] if not j["is_emergency"])
    jid = job["job_id"]
    orig = crew
    x.defer(lambda: (x.c.post("/api/rm/apply-assign", dict(region="BLK", job_id=jid, crew_id=orig)),
                     x.c.post("/api/field/unpark", dict(region="BLK", job_id=jid))), "return the stop to the original crew")
    r = want(x.c.post("/api/field/reassign", dict(region="BLK", crew_id=crew, job_ids=[jid], reason_label="E2E reassign", stand_down=False)),
             200, "reassign")
    res = r["reassigned"].get(jid)
    if res is None:
        check(jid in {h["job_id"] for h in want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["parked"]}, "stop neither moved nor held")
        x.note("no other crew had room; the stop went on hold (restored by the cleanup)")
    else:
        check(res["crew_id"] != crew, f"stop reassigned to the same crew {res}")
        now = {a["job_id"]: a["crew_id"] for a in roster(x, "BLK")["assignments"]}
        check(now.get(jid) == res["crew_id"], f"roster shows {now.get(jid)} for {jid}, expected {res['crew_id']}")
        x.note(f"{jid}: {crew} -> {res['crew_id']}, then back")


# ============================================================================ hierarchy: the transmission level
@test("hierarchy")
def hier_spine_six_stops(x):
    """The network spine has six stops in order: bulk supply, transmission substations, zone substations, HV feeders, distribution and LV, customers and DER."""
    spine = want(x.c.get("/api/cc"), 200, "command centre")["spine"]
    check([s["stop"] for s in spine] == SPINE_STOPS, f"spine stops {[s['stop'] for s in spine]}")
    ords = [s["ord"] for s in spine]
    check(ords == sorted(ords) and len(set(ords)) == len(ords), f"spine ords {ords}")
    tr = next(s for s in spine if s["stop"] == "transmission_substations")
    check(tr["total"] > 0, "the transmission stop counts no substations")
    x.note("stops: " + " > ".join(s["stop"] for s in spine))


@test("hierarchy")
def hier_transmission_rows(x):
    """Transmission stop rows: one per site with its status and 30-day peak utilisation, and its 132 kV transformers (TS-xx-TXn)."""
    rows = ts_rows(x)
    check(len(rows) >= 1, "no transmission substations")
    n_tx = 0
    for r in rows:
        check(str(r["ts_id"]).startswith("TS-"), f"substation id {r['ts_id']}")
        check(r["status"] in ("green", "amber", "red"), f"{r['ts_id']} status {r['status']}")
        check(r["peak_utilisation_30d"] is None or is_num(r["peak_utilisation_30d"]), f"{r['ts_id']} peak utilisation is not a number")
        for t in r.get("transformer_rows") or []:
            check(str(t["transformer_id"]).startswith(r["ts_id"] + "-TX"), f"transformer {t['transformer_id']} is not under {r['ts_id']}")
            n_tx += 1
    check(n_tx > 0, "no transmission substation lists its transformers")
    x.note(f"{len(rows)} substations, {n_tx} transformers")


@test("hierarchy")
def hier_transmission_substation_drawer(x):
    """Asset drawer for a transmission substation (TS-xx): the register record, the transmission site record and the systems."""
    rows = ts_rows(x)
    check(rows, "no transmission substations")
    ts = rows[0]["ts_id"]
    a = want(x.c.get(f"/api/asset/{q(ts)}"), 200, f"asset {ts}")
    check(a["kind"] == "asset" and a["record"]["asset_id"] == ts, f"record is not {ts}")
    check(a["record"]["asset_class"] == "transmission_substation", f"asset class {a['record']['asset_class']}")
    check((a.get("site") or {}).get("ts_id") == ts, "the drawer carries no transmission site record")
    check(a["systems"] and all(s[1] for s in a["systems"]), "systems list is empty or has blanks")
    x.note(f"{ts}: {a['record'].get('asset_class_label')}, {len(a['systems'])} systems")


@test("hierarchy")
def hier_transmission_transformer_drawer(x):
    """Asset drawer for a transmission transformer (TS-xx-TXn): its class, its loading record and its substation."""
    tx = None
    ts = None
    for r in ts_rows(x):
        if r.get("transformer_rows"):
            ts, tx = r["ts_id"], r["transformer_rows"][0]["transformer_id"]
            break
    if not tx:
        raise Skip("no transmission transformer listed")
    a = want(x.c.get(f"/api/asset/{q(tx)}"), 200, f"asset {tx}")
    check(a["record"]["asset_id"] == tx and a["record"]["asset_class"] == "transmission_transformer", f"record {brief(a['record'], 160)}")
    check((a.get("transformer") or {}).get("transformer_id") == tx, "the drawer carries no transformer record")
    x.note(f"{tx} in {ts}")


@test("hierarchy")
def hier_scene_transmission_site(x):
    """3D scene for a transmission site: its transformers, the 33 or 66 kV circuits out, the 132 kV circuits in and the zones it feeds."""
    tsites = [s for s in want(x.c.get("/api/scene-sites"), 200, "scene sites") if s.get("kind") == "transmission"]
    check(tsites, "no transmission sites in the 3D site list")
    ts = tsites[0]["ts_id"]
    sc = want(x.c.get(f"/api/scene/transmission/{q(ts)}"), 200, f"transmission scene {ts}")
    check(sc["kind"] == "transmission" and sc["ts_id"] == ts, f"scene kind {sc.get('kind')} for {ts}")
    clickable = len(sc["transformers"]) + len(sc["circuits"]) + len(sc["incoming"])
    check(clickable > 0, "the transmission scene has no clickable items")
    for t in sc["transformers"]:
        check(t["asset_id"] and t["label"], f"transformer item {brief(t, 120)}")
    for c in sc["circuits"]:
        check(c["asset_id"] and c["zs_id"], f"circuit item {brief(c, 120)}")
    check(isinstance(sc["zones"], list), "zones is not a list")
    x.note(f"{ts}: {len(sc['transformers'])} transformers, {len(sc['circuits'])} circuits out, {len(sc['incoming'])} in, {len(sc['zones'])} zones")


@test("hierarchy")
def hier_impact_includes_transmission(x):
    """Impact analysis carries the transmission level: the substations, their 132 kV lines, and the bulk supply points they connect to."""
    b = want(x.c.get("/api/impact"), 200, "impact")
    for k in ("transmission", "tlines", "bsps", "zones", "lines"):
        check(isinstance(b.get(k), list), f"impact {k} is not a list")
    check(len(b["transmission"]) >= 1, "impact has no transmission substations")
    check(len(b["tlines"]) >= 1, "impact has no transmission lines")
    check(all(l.get("geo") for l in b["tlines"]), "a transmission line has no geometry")
    x.note(f"{len(b['transmission'])} transmission substations, {len(b['tlines'])} lines")


@test("hierarchy")
def hier_map_includes_transmission(x):
    """The network map carries the transmission level: positioned substations and their 132 kV lines."""
    m = want(x.c.get("/api/map/network"), 200, "map network")
    check("transmission" in m and "tlines" in m, "map network has no transmission or tlines key")
    check(len(m["transmission"]) >= 1 and len(m["tlines"]) >= 1, "map has no transmission level")
    check(all(is_num(t["lat"]) and is_num(t["lon"]) for t in m["transmission"]), "a transmission substation has no position")
    check(all(l.get("geo") for l in m["tlines"]), "a transmission line on the map has no geometry")
    x.note(f"{len(m['transmission'])} substations, {len(m['tlines'])} lines on the map")


@test("hierarchy")
def hier_zone_names_transmission(x):
    """Zone supply names each zone substation's transmission substation, and every substation it names is on the transmission list."""
    imp = want(x.c.get("/api/impact"), 200, "impact")
    check("transmission" in imp and "tlines" in imp, "impact has no transmission key")
    on_list = {t["ts_id"] for t in imp["transmission"]}
    zones = imp["zones"]
    check(zones, "no zone supply rows")
    named = [z for z in zones if z.get("ts_ids")]
    check(named, "no zone supply names a transmission substation")
    for z in named:
        for t in z["ts_ids"]:
            check(t in on_list, f"{z['zs_id']} names {t}, which is not on the transmission list")
    x.S["zone_ts"] = {z["zs_id"]: list(z["ts_ids"]) for z in named}
    x.note(f"{len(named)} of {len(zones)} zones name a transmission substation")


@test("hierarchy")
def hier_zone_link_reaches_applications(x):
    """A connection application shows its supply chain, bulk supply point to premises, with the transmission substation that its zone is
    supplied from (the link the zone supply names)."""
    zone_ts = need(x, "zone_ts")
    apps = want(x.c.get("/api/customers/applications?limit=300"), 200, "applications")["rows"]
    hits = [a for a in apps if a.get("zs_id") in zone_ts]
    if not hits:
        raise Skip("no open connection application in a zone that names a transmission substation")
    a = want(x.c.get(f"/api/customers/application/{q(hits[0]['application_id'])}"), 200, "application")
    chain = a["application"].get("supply_chain") or []
    kinds = [n["kind"] for n in chain]
    check(kinds == ["bulk_supply", "transmission", "zone", "feeder", "distribution", "premises"], f"supply chain steps {kinds}")
    check(all(n.get("name") for n in chain), "a step of the supply chain has no name")
    got = chain[1].get("ref")
    expect = zone_ts[hits[0]["zs_id"]]
    check(got in expect, f"application {hits[0]['application_id']} in {hits[0]['zs_id']} shows transmission substation {got!r}, expected one of {expect}")
    x.note(" > ".join(n["name"] for n in chain))


# ============================================================================ notices (notification engine)
@test("notices")
def notify_overview(x):
    """Notice overview: notices being designed, scheduled, and sent this week with the customers reached."""
    o = want(x.c.get("/api/notify/overview"), 200, "notify overview")
    for k in ("draft", "scheduled", "sent_week", "reached_week", "notice_days"):
        check(is_num(o.get(k)) and o[k] >= 0, f"{k} is {o.get(k)!r}")
    check(o["notice_days"] == 4, f"notice period {o['notice_days']} business days")
    x.note(f"{o['draft']} draft, {o['scheduled']} scheduled, {o['sent_week']} sent this week to {o['reached_week']} customers")


@test("notices")
def notify_candidates_each_kind(x):
    """Candidates for each kind: the released plans' planned outages (window, customers, notice period), live faults and weather hazards."""
    pl = want(x.c.get("/api/notify/candidates?kind=planned_work"), 200, "planned candidates")
    check(pl["kind"] == "planned_work", f"kind {pl['kind']}")
    for c in pl["items"]:
        check(c["id"].startswith("plan|") and c["kind"] == "planned_work", f"planned id {c['id']}")
        check(c["jobs"] > 0 and c["customers"] >= 0 and c["plan_title"], f"{c['id']}: jobs {c['jobs']}, customers {c['customers']}")
        check(isinstance(c["eligible"], bool) and (c["eligible"] or c["blocked"]), f"{c['id']} eligibility {c['eligible']} / {c['blocked']}")
        check(c["window_start"] < c["window_end"], f"{c['id']} window does not run forward")
        dash_free(c["window_label"], "window label")
    fl = want(x.c.get("/api/notify/candidates?kind=network_fault"), 200, "fault candidates")
    for c in fl["items"]:
        check(c["id"].startswith("fault|") and c["outage_id"] and c["customers_off"] >= 0, f"fault candidate {brief(c, 160)}")
    wx = want(x.c.get("/api/notify/candidates?kind=severe_weather"), 200, "weather candidates")
    for c in wx["items"]:
        check(c["id"] == "weather|forecast" and c["cell_count"] >= 1 and c["max_gust_kmh"] >= 70, f"weather candidate {brief(c, 160)}")
    x.note(f"planned {len(pl['items'])} ({sum(1 for c in pl['items'] if c['eligible'])} eligible), faults {len(fl['items'])}, weather {len(wx['items'])}")


@test("notices")
def notify_candidate_preview(x):
    """Candidate preview on two channels before anything is created: the seven rules, the customers left out by reason (including those
    none of the chosen channels reach), and the messages on the chosen channels only (plus the life support calls)."""
    cid, p = pick_candidate(x, "sms,email")
    if not cid:
        raise Skip("no eligible candidate reaches a customer right now")
    check({r["code"] for r in p["rules"]} == NOTICE_RULES, f"rules {sorted(r['code'] for r in p['rules'])}")
    c = p["counts"]
    check(0 < c["reached"] <= c["in_scope"], f"reached {c['reached']} of in scope {c['in_scope']}")
    used = {ch for aud in c["messages"].values() for ch in aud}
    check(used <= {"sms", "email", "contact_centre"}, f"messages on channels not chosen: {sorted(used)}")
    ex = p["exclusions"]
    check(ex["total"] == sum(r["n"] for r in ex["by_reason"]), "exclusion total does not match its reasons")
    check(all(r["label"] for r in ex["by_reason"]), "an exclusion reason has no label")
    dash_free(p["title"], "candidate title")
    x.S["notice_candidate"] = cid
    x.note(f"{cid}: reaches {c['reached']} of {c['in_scope']} on SMS and email, {ex['total']} left out, {c['messages_total']} messages")


@test("notices")
def notify_create_from_candidate(x):
    """Create a draft notice from the candidate on SMS and email: its customers and exclusions are recorded, and the AI starts drafting."""
    cid = need(x, "notice_candidate")
    d = want(x.c.post("/api/notify/notices", {"candidate": cid, "channels": ["sms", "email"]}), 200, "create notice")
    n = d["notice"]
    check(n["notice_id"].startswith("NTC-") and n["status"] == "draft", f"notice {n['notice_id']} status {n['status']}")
    check(n["status_label"] and n["channels"] == ["sms", "email"], f"status label {n['status_label']!r}, channels {n['channels']}")
    check(d["ai"]["status"] in ("running", "done", "failed"), f"drafting status {d['ai']}")
    x.S["notice_id"] = n["notice_id"]
    x.note(f"created {n['notice_id']} from {cid}; residue: notice, exclusions and audit rows (no delete API)")


@test("notices")
def notify_opus_drafts(x):
    """The AI drafts the chosen channels in the background (polled up to 120 s): customer SMS and email, life support SMS, email and call."""
    nid = need(x, "notice_id")
    d, secs = wait_for(lambda: (lambda r: r if r["ai"]["status"] in ("done", "failed") else None)(notice_detail(x, nid)), 120, 5.0)
    if d is None:
        raise Fail("AI drafting did not finish within 120 s")
    check(d["ai"]["status"] == "done", f"drafting failed: {d['ai'].get('error')}")
    dr = d["notice"]["drafts"]
    check(set(dr.get("customer") or {}) == {"sms", "email"}, f"customer drafts {sorted(dr.get('customer') or {})}")
    check(set(dr.get("life_support") or {}) == {"sms", "email", "contact_centre"}, f"life support drafts {sorted(dr.get('life_support') or {})}")
    for aud in dr.values():
        for text in aud.values():
            dash_free(text, "a drafted message")
    check("recommendation" not in d["notice"], "the notice still carries an AI channel recommendation (each customer's channel is their preference on record)")
    x.note(f"drafted in {secs:.0f} s")


@test("notices")
def notify_edit_draft(x):
    """The planner edits the customer SMS: the edit is in the draft and logged with who and when; the original text is put back."""
    nid = need(x, "notice_id")
    before = notice_detail(x, nid)["notice"]["drafts"]["customer"]["sms"]
    new = "E2E test edit: planned work in your area, power may be off for a short time."
    d = want(x.c.post(f"/api/notify/notices/{q(nid)}/drafts", {"edits": [{"audience": "customer", "channel": "sms", "text": new}]}), 200, "edit draft")
    check(d["notice"]["drafts"]["customer"]["sms"] == new, "the edit is not in the draft")
    check(any(e["audience"] == "customer" and e["channel"] == "sms" and e.get("by") and e.get("at") for e in d["notice"]["edits"]),
          "the edit is not logged with who and when")
    back = want(x.c.post(f"/api/notify/notices/{q(nid)}/drafts", {"edits": [{"audience": "customer", "channel": "sms", "text": before}]}), 200,
                "restore draft")
    check(back["notice"]["drafts"]["customer"]["sms"] == before, "the original text was not put back")


@test("notices")
def notify_customers_list(x):
    """The customers the notice reaches: each with an address, a feeder and the channels it is reached on, life support first."""
    nid = need(x, "notice_id")
    d = want(x.c.get(f"/api/notify/notices/{q(nid)}/customers"), 200, "notice customers")
    check(d["total"] >= d["shown"] > 0 and d["customers"], f"total {d['total']}, shown {d['shown']}")
    for r in d["customers"][:50]:
        check(r["nmi"] and r["channels"] and r["feeder"], f"customer {brief(r, 160)}")
    x.note(f"{d['total']} customers, {d['life_support']} on life support")


@test("notices")
def notify_release_needs_confirm(x):
    """Release without confirm is refused (400): the notice stays a draft and no messages are written."""
    nid = need(x, "notice_id")
    r = x.c.post(f"/api/notify/notices/{q(nid)}/release", {"when": "now"})
    check(r.status == 400 and "Confirm" in str(r.body.get("detail", "")), f"release without confirm: HTTP {r.status} {brief(r.body, 160)}")
    d = notice_detail(x, nid)
    check(d["notice"]["status"] == "draft" and d["messages"] is None, f"a refused release changed the status to {d['notice']['status']}")


@test("notices")
def notify_release_now(x):
    """Release now with confirm: the customers are evaluated again, the messages go to the outbox on the chosen channels, and the notice is sent."""
    nid = need(x, "notice_id")
    d = want(x.c.post(f"/api/notify/notices/{q(nid)}/release", {"when": "now", "confirm": True}), 200, "release")
    n = d["notice"]
    check(n["status"] == "sent" and n["sent_at"], f"after release: status {n['status']}")
    msgs = d["messages"]
    check(msgs, "no outbox messages for the sent notice")
    total = 0
    for aud, chans in msgs.items():
        check(aud in ("customer", "life_support"), f"audience {aud}")
        for ch, v in chans.items():
            check(ch in {"sms", "email", "contact_centre"}, f"a message on {ch}, which was not chosen")
            check(0 <= v["sent"] <= v["messages"] and v["channel_label"], f"{aud} {ch}: sent {v['sent']} of {v['messages']}")
            total += v["messages"]
    check(total == d["counts"]["messages_total"], f"outbox {total} messages, counts say {d['counts']['messages_total']}")
    listed = want(x.c.get("/api/notify/notices?status=released&limit=100"), 200, "released notices")["notices"]
    check(any(r["notice_id"] == nid for r in listed), "the sent notice is not on the released list")
    x.S["sent_total"] = total
    x.note("outbox: " + ", ".join(f"{aud} {ch} {v['messages']}" for aud, chans in msgs.items() for ch, v in chans.items()))


@test("notices")
def notify_second_release_refused(x):
    """A second release of the same notice is refused (400) and writes no further messages."""
    nid = need(x, "notice_id")
    r = x.c.post(f"/api/notify/notices/{q(nid)}/release", {"when": "now", "confirm": True})
    check(r.status == 400 and "already been sent" in str(r.body.get("detail", "")), f"second release: HTTP {r.status} {brief(r.body, 160)}")
    total = sum(v["messages"] for chans in (notice_detail(x, nid)["messages"] or {}).values() for v in chans.values())
    check(total == x.S.get("sent_total"), f"outbox grew to {total} after a refused release (was {x.S.get('sent_total')})")


@test("notices")
def notify_residue_report(x):
    """What this run leaves in the notice tables (INFO): the notice, its exclusions, its outbox messages and audit rows. No delete API exists."""
    nid = x.S.get("notice_id")
    if not nid:
        raise Skip("no notice was created")
    raise Info(f"residue: notice {nid} (sent: its messages cannot be withdrawn), its exclusion rows, {x.S.get('sent_total', 0)} outbox messages, "
               "audit rows notice_create, notice_send. Reruns reach fewer customers on the same feeder for 24 h.")


# ============================================================================ customers (connection applications and existing)
@test("customers")
def cust_pipeline_kpis_and_stages(x):
    """Connection applications: the headline figures, and the pipeline by stage with open, stuck and blocked counts that add up to the open total."""
    p = want(x.c.get("/api/customers/pipeline"), 200, "pipeline")
    kpi = p["kpi"]
    for k in ("open", "stuck", "blocked", "awaiting_payment", "over_target"):
        check(is_num(kpi[k]) and kpi[k] >= 0, f"kpi {k} is {kpi.get(k)!r}")
    check(kpi["stuck"] <= kpi["open"] and kpi["blocked"] <= kpi["open"], f"stuck or blocked above open: {kpi}")
    codes = [s["stage"] for s in p["stages"]]
    check(set(codes) <= set(STAGE_CODES) and len(codes) >= 6, f"pipeline stages {codes}")
    check(all(s["stage_label"] for s in p["stages"]), "a stage has no label")
    check(sum(s["open_applications"] for s in p["stages"]) == kpi["open"], "open by stage does not add up to the open total")
    x.note(f"{kpi['open']} open, {kpi['stuck']} stuck, {kpi['blocked']} blocked, {kpi['awaiting_payment']} awaiting payment")


@test("customers")
def cust_applications_filters(x):
    """Applications list: stuck first, and each filter (stage, connection type, stuck only, applicant or address search) narrows it correctly."""
    base = want(x.c.get("/api/customers/applications?limit=300"), 200, "applications")
    rows = base["rows"]
    check(rows, "no open connection application")
    check(base["shown"] == len(rows) and base["total"] >= base["shown"], f"shown {base['shown']} total {base['total']}")
    stucks = [r["stuck"] for r in rows]
    check(stucks == sorted(stucks, reverse=True), "stuck applications are not listed first")
    stage = rows[0]["stage"]
    s = want(x.c.get(f"/api/customers/applications?stage={q(stage)}&limit=300"), 200, "stage filter")
    check(s["rows"] and all(r["stage"] == stage for r in s["rows"]), f"stage filter {stage} returned other stages")
    ctype = rows[0]["connection_type"]
    t = want(x.c.get(f"/api/customers/applications?type={q(ctype)}&limit=300"), 200, "type filter")
    check(t["rows"] and all(r["connection_type"] == ctype for r in t["rows"]), f"type filter {ctype} returned other types")
    st = want(x.c.get("/api/customers/applications?stuck=true&limit=300"), 200, "stuck filter")
    check(all(r["stuck"] for r in st["rows"]), "stuck filter returned a not stuck application")
    term = str(rows[0]["applicant_name"]).split()[0]
    qs = want(x.c.get(f"/api/customers/applications?q={q(term)}&limit=300"), 200, "search")
    check(any(r["application_id"] == rows[0]["application_id"] for r in qs["rows"]), f"search {term} does not find the first application")
    check(all(term.lower() in (str(r["applicant_name"]) + str(r["address"]) + str(r["suburb"]) + r["application_id"]).lower()
              for r in qs["rows"]), "a search result does not contain the term")
    x.S["app_id"] = rows[0]["application_id"]
    x.note(f"{base['total']} open; stage {stage} {len(s['rows'])}, type {ctype} {len(t['rows'])}, stuck {len(st['rows'])}")


@test("customers")
def cust_application_journey(x):
    """One connection application's journey: seven stages in order, the current stage marked, and no stage after the current one reached."""
    aid = x.S.get("app_id")
    if not aid:
        rows = want(x.c.get("/api/customers/applications?limit=5"), 200, "applications")["rows"]
        if not rows:
            raise Skip("no open connection application")
        aid = rows[0]["application_id"]
    j = want(x.c.get(f"/api/customers/application/{q(aid)}"), 200, f"application {aid}")
    check([s["stage"] for s in j["journey"]] == STAGE_CODES, f"journey stages {[s['stage'] for s in j['journey']]}")
    cur = [i for i, s in enumerate(j["journey"]) if s["current"]]
    check(len(cur) == 1 and j["journey"][cur[0]]["stage"] == j["application"]["stage"], "the current stage is not marked once")
    i = cur[0]
    check(all(s["reached"] for s in j["journey"][:i + 1]), "a stage before the current one is not reached")
    check(not any(s["reached"] for s in j["journey"][i + 1:]), "a stage after the current one is marked reached")
    check(j["application"]["application_id"] == aid and j["application"]["stage_label"], "application record is wrong")
    x.note(f"{aid}: stage {j['application']['stage_label']}, {sum(1 for s in j['journey'] if s['reached'])} of 7 reached")


@test("customers")
def cust_existing_summary(x):
    """Existing customers at a glance: customers, life support, critical sites, DER, notification preferences and supply off now."""
    k = want(x.c.get("/api/customers/existing/summary"), 200, "existing summary")["kpi"]
    for key in ("customers", "life_support", "critical_sites", "solar", "batteries", "ev_chargers", "sms", "email", "app_or_web", "no_digital",
                "feeders", "zone_substations", "off_supply_now", "outages_now", "life_support_off"):
        check(is_num(k.get(key)) and k[key] >= 0, f"{key} is {k.get(key)!r}")
    check(k["customers"] > 10000, f"only {k['customers']} premises")
    check(k["life_support"] <= k["customers"] and k["sms"] <= k["customers"], "a subset is larger than the customer base")
    check(k["zone_substations"] >= 10, f"only {k['zone_substations']} zone substations")
    x.S["outages_now"] = k["outages_now"]
    x.note(f"{k['customers']:,} premises, {k['life_support']:,} life support, {k['off_supply_now']:,} off supply now")


@test("customers")
def cust_existing_search_each_filter(x):
    """Premises search with each filter (all, life support, critical sites, DER, off supply); every row meets its filter, and NMI and suburb search work."""
    rules = {"all": lambda r: True, "life_support": lambda r: bool(r["life_support"]), "critical": lambda r: r["critical_site"] is not None,
             "der": lambda r: (r["solar_kw"] or 0) > 0 or r["has_battery"] == 1 or r["has_ev_charger"] == 1,
             "off_supply": lambda r: r["off_supply"] is True}
    first_all = None
    for flt, rule in rules.items():
        r = want(x.c.get(f"/api/customers/existing/search?q=&filter={flt}&limit=60"), 200, f"search {flt}")
        check(r["filter"] == flt and r["shown"] == len(r["rows"]) <= 60, f"search {flt} envelope")
        for row in r["rows"]:
            check(rule(row), f"{flt}: premises {row['nmi']} does not meet the filter")
        if flt == "all":
            check(r["rows"], "the all filter returned no premises")
            first_all = r["rows"][0]
        x.note(f"{flt} {len(r['rows'])}")
    by_nmi = want(x.c.get(f"/api/customers/existing/search?q={q(first_all['nmi'])}&filter=all"), 200, "search by NMI")
    check(by_nmi["rows"] and by_nmi["rows"][0]["nmi"] == first_all["nmi"], "NMI search does not return that premises first")
    sub = first_all["suburb"]
    if sub:
        by_sub = want(x.c.get(f"/api/customers/existing/search?q={q(sub)}&filter=all&limit=60"), 200, "search by suburb")
        check(by_sub["rows"], f"suburb search {sub} found nothing")
        check(all(sub.lower() in (str(r["suburb"]) + str(r["address"])).lower() for r in by_sub["rows"]), "a suburb search result is in another suburb")


@test("customers")
def cust_off_supply_matches_outages(x):
    """The off supply filter lists the premises whose protective device has an active outage, whenever outages are live."""
    n = x.S.get("outages_now")
    if n is None:
        n = want(x.c.get("/api/customers/existing/summary"), 200, "existing summary")["kpi"]["outages_now"]
    if not n:
        raise Skip("no live outage right now, so no premises should be off supply")
    r = want(x.c.get("/api/customers/existing/search?q=&filter=off_supply&limit=60"), 200, "off supply search")
    check(len(r["rows"]) >= 1, f"the off supply filter returned no premises while {n} outage(s) are live")
    check(all(row["off_supply"] and row["outage_id"] for row in r["rows"]), "an off supply row has no outage")


@test("customers")
def cust_premises_profile(x):
    """One premises profile: the register, supply now, the outages on its protective device and how it would be reached."""
    rows = want(x.c.get("/api/customers/existing/search?q=&filter=all&limit=5"), 200, "search")["rows"]
    check(rows, "no premises to profile")
    nmi = rows[0]["nmi"]
    p = want(x.c.get(f"/api/customers/existing/{q(nmi)}"), 200, f"premises {nmi}")
    check(p["premises"]["nmi"] == nmi, "the profile is for another premises")
    check(isinstance(p["supply"]["off_supply"], bool), "supply off flag is not a boolean")
    check(p["route"]["primary"] in ("sms", "email", "app", "mail") and p["route"]["channels"], f"route {p['route']}")
    check(isinstance(p["outages"], list) and isinstance(p["notices"], list), "outages or notices not lists")
    x.note(f"{nmi}: {p['route']['primary_label']}, {len(p['outages'])} outages in the year, {len(p['notices'])} notices")


# ============================================================================ operations (restoration, live, learn)
@test("operations")
def ops_restoration_shape(x):
    """Restoration board: the totals (customers off, restored, of total), and the outages with their status, alert and field report."""
    r = want(x.c.get("/api/ops/restoration"), 200, "restoration")
    t = r["totals"]
    for k in ("customers_total", "customers_off", "customers_restored", "active_outages", "restored_recent", "life_support_off", "crews_deployed",
              "crews_on_site", "field_customers_reported"):
        check(is_num(t[k]) and t[k] >= 0, f"totals {k} is {t.get(k)!r}")
    check(not {"notices_sent", "materials", "channels"} & (set(t) | set(r)), "a removed section's data is still returned")
    check(t["customers_restored"] <= t["customers_total"], f"restored {t['customers_restored']} above total {t['customers_total']}")
    check(t["customers_off"] <= t["customers_total"], "customers off above the total")
    check(t["customers_restored"] == max(0, t["customers_total"] - t["customers_off"]), "restored is not total minus off")
    check(t["crews_on_site"] <= t["crews_deployed"], "more crews on site than deployed")
    for o in r["outages"]:
        check(o["status"] in ("active", "restored"), f"outage status {o['status']}")
        check(o["customers_restored"] <= o["customers_initial"], f"{o['outage_id']} restored above its initial count")
    x.note(f"{t['customers_off']:,} of {t['customers_total']:,} off, {t['customers_restored']:,} restored, {t['active_outages']} active outages")


@test("operations")
def ops_off_supply(x):
    """The customers off supply behind the tiles: every one is in a live outage; the life support list is the life support customers of it."""
    a = want(x.c.get("/api/ops/off-supply"), 200, "off supply")["premises"]
    ls = want(x.c.get("/api/ops/off-supply?life_support=true"), 200, "life support off")["premises"]
    check(all(p["outage_id"] and p["nmi"] for p in a), "a premises off supply has no outage or NMI")
    check({p["nmi"] for p in ls} == {p["nmi"] for p in a if p["life_support"]}, "the life support list differs from the life support customers off")
    x.note(f"{len(a)} customers off supply, {len(ls)} on life support")


@test("operations")
def ops_live_shape(x):
    """The timeline: meter alarms, calls and switch positions; every call names its customer and carries a network check with a label and
    a note; a call's premises record lists its calls and its supply chain from the bulk supply point."""
    lv = want(x.c.get("/api/ops/live"), 200, "live")
    check(isinstance(lv["timeline"], list) and "switches" not in lv, "the timeline is missing, or the removed switch positions are returned")
    check(len(lv["timeline"]) <= 40, "timeline longer than its 40 rows")
    for e in lv["timeline"]:
        check(e["kind"] in ("alarm", "call", "switch") and e["kind_label"] and e["ts"], f"timeline row {brief(e, 120)}")
        if e["kind"] == "call":
            check(e["check"] in ("verified", "resolved", "not_verified", "attend") and e["check_label"] and e["check_note"], f"call check {brief(e, 160)}")
    call = next((e for e in lv["timeline"] if e["kind"] == "call"), None)
    if call:
        c = want(x.c.get(f"/api/customers/existing/{q(call['ref'])}"), 200, "the caller's record")
        check(any(k["call_id"] for k in c["calls"]), "the caller's record lists no call")
        check([n["kind"] for n in c["supply_chain"]][:1] == ["bulk_supply"] and c["supply_chain"][-1]["kind"] == "premises", "the supply chain does not run from the bulk supply point to the premises")
    x.note(f"{len(lv['timeline'])} timeline events, {sum(e['kind'] == 'call' for e in lv['timeline'])} calls")


@test("operations")
def ops_learn_shape(x):
    """Resource performance by crew, and damage by cause; the removed lessons (estimates, history, automation, field findings, regions)
    are not returned."""
    lr = want(x.c.get("/api/ops/learn"), 200, "learn")
    check(set(lr) == {"resources", "damage"} and set(lr["damage"]) == {"by_cause", "since"}, f"learn returns {sorted(lr)}")
    for c in lr["resources"]["by_crew"]:
        check(c["fixed"] + c["follow_up"] <= c["reports"], f"crew {c['crew_id']}: fixed and follow-up above reports")
    for d in lr["damage"]["by_cause"]:
        check(d["events"] >= 1 and (d["customer_minutes"] is None or d["customer_minutes"] >= 0), f"damage row {brief(d, 120)}")
    x.note(f"{len(lr['resources']['by_crew'])} crews, {len(lr['damage']['by_cause'])} causes")


@test("fieldreports")
def fr_meta_for_job(x):
    """The field report form for a real job: the choices with labels, and the job's asset, outage and situation context."""
    a, sit = pick_fr_job(x)
    m = want(x.c.get(f"/api/fieldreport/meta?job_id={q(a['job_id'])}"), 200, "field report meta")
    check([o["code"] for o in m["outcomes"]] == FR_OUTCOMES and all(o["label"] for o in m["outcomes"]), f"outcomes {m['outcomes']}")
    check({h["code"] for h in m["hazards"]} == FR_HAZARDS, "hazard choices differ from the list")
    check({f["code"] for f in m["fault_kinds"]} == FR_FAULT_KINDS, "fault kind choices differ")
    check({mt["code"] for mt in m["materials"]} == FR_MATERIALS, "material choices differ")
    check(m["job"] and m["job"]["job_id"] == a["job_id"] and m["job"]["title"], "the job did not resolve")
    x.S["fr_job"] = dict(job_id=a["job_id"], crew_id=a["crew_id"], situation_id=sit)
    x.note(f"job {a['job_id']} for crew {a['crew_id']}; situation {sit or 'none (the situation filter is not exercised)'}")


@test("fieldreports")
def fr_submit_valid_report(x):
    """File a valid field report on the job: it is accepted and gets a report id."""
    j = need(x, "fr_job")
    body = dict(job_id=j["job_id"], crew_id=j["crew_id"], response_note="E2E test: responded from the depot", patrol_complete=True,
                fault_located=True, fault_kind="span", fault_ref="E2E-TEST-SPAN", segments_total=5, segments_cleared=3,
                switching_executed=False, customers_restored=0, access_status="clear", hazards=["tree"],
                materials=[dict(code="lv_cable", qty=2)], notes=f"E2E test report {STAMP}, safe to ignore", outcome="follow_up", photo_ids=[])
    r = want(x.c.post("/api/fieldreport/submit", body), 200, "submit field report")
    check(r["ok"] and r["report_id"].startswith("FR-") and r["job_id"] == j["job_id"], f"submit returned {brief(r, 160)}")
    x.S["fr_report"] = r["report_id"]
    x.note(f"{r['report_id']} filed (no delete API: residue)")


@test("fieldreports")
def fr_list_by_job_crew_region_situation(x):
    """The report shows on its job, its crew, its region and its situation, with the labels the screens show."""
    j = need(x, "fr_job")
    rid = need(x, "fr_report")

    def has(rows):
        return next((r for r in rows if r["report_id"] == rid), None)
    by_job = has(want(x.c.get(f"/api/fieldreport/list?job_id={q(j['job_id'])}"), 200, "reports by job")["reports"])
    check(by_job is not None, "the report is not listed on its job")
    check(by_job["outcome_label"] == "Needs follow up", f"outcome label {by_job['outcome_label']!r}")
    check(by_job["hazard_labels"] == ["Tree on the line"], f"hazard labels {by_job['hazard_labels']}")
    check(by_job["materials"] == [dict(code="lv_cable", qty=2, label="LV cable (m)")], f"materials {by_job['materials']}")
    check(has(want(x.c.get(f"/api/fieldreport/list?crew_id={q(j['crew_id'])}"), 200, "reports by crew")["reports"]) is not None,
          "the report is not listed on its crew")
    check(has(want(x.c.get("/api/fieldreport/list?region_id=BLK&limit=200"), 200, "reports by region")["reports"]) is not None,
          "the report is not listed on its region")
    if j.get("situation_id"):
        check(has(want(x.c.get(f"/api/fieldreport/list?situation_id={q(j['situation_id'])}"), 200, "reports by situation")["reports"]) is not None,
              "the report is not listed on its situation")
    else:
        x.note("the job has no situation (planned work), so the situation list is not checked")


@test("fieldreports")
def fr_invalid_report_refused(x):
    """Invalid reports are refused with 400 and the message the crew sees; none of them is written."""
    j = need(x, "fr_job")
    base = dict(job_id=j["job_id"], crew_id=j["crew_id"], outcome="fixed", notes="E2E invalid case")
    cases = [(dict(base, outcome="bogus"), "Choose how the job is left"),
             (dict(base, segments_total=2, segments_cleared=5), "cannot be more than"),
             (dict(base, job_id="JOB-NO-SUCH-E2E"), "no longer on the plan"),
             (dict(base, access_status="open"), "Unknown access status"),
             (dict(base, materials=[dict(code="unobtainium", qty=1)]), "Unknown material"),
             (dict(base, customers_restored=-1), "between 0 and")]
    before = {r["report_id"] for r in want(x.c.get(f"/api/fieldreport/list?job_id={q(j['job_id'])}&limit=200"), 200, "reports")["reports"]}
    for body, msg in cases:
        r = x.c.post("/api/fieldreport/submit", body)
        check(r.status == 400 and msg in str(r.body.get("detail", "")), f"{msg!r}: HTTP {r.status} {brief(r.body, 160)}")
    after = {r["report_id"] for r in want(x.c.get(f"/api/fieldreport/list?job_id={q(j['job_id'])}&limit=200"), 200, "reports")["reports"]}
    check(after == before, f"a refused report was written: {sorted(after - before)}")
    x.note(f"{len(cases)} invalid reports refused")


# ============================================================================ asset photos and review
@test("assets")
def as_search_and_defect_asset(x):
    """Asset search by id finds the asset (an exact id first), and one of the matches has open defects for the fixed-defect test."""
    hits = want(x.c.get("/api/assets/search?q=RPR-F03"), 200, "asset search")["assets"]
    check(len(hits) >= 3, f"only {len(hits)} assets match RPR-F03")
    exact = want(x.c.get("/api/assets/search?q=RPR-F03-CB01"), 200, "exact asset search")["assets"]
    check(exact and exact[0]["asset_id"] == "RPR-F03-CB01", "an exact id match does not come first")
    found = None
    for h in hits[:12]:
        v = want(x.c.get(f"/api/assets/{q(h['asset_id'])}"), 200, f"asset {h['asset_id']}")
        if v["defects"]:
            found = (h["asset_id"], v["defects"])
            break
    if found:
        fixed, fixed_defects = found[0], [d["notification_id"] for d in found[1]]
    else:   # no open defect in the synced gold for these assets: the fixed decision is still exercised (it reports nothing to close)
        fixed, fixed_defects = hits[0]["asset_id"], []
    others = [h["asset_id"] for h in hits if h["asset_id"] != fixed]
    x.S["asset"] = dict(fixed=fixed, fixed_defects=fixed_defects, updated=others[0], rejected=others[1], has_defect=bool(found))
    x.note(f"fixed on {fixed} ({len(fixed_defects)} open defects); updated on {others[0]}; rejected on {others[1]}")


@test("assets")
def as_submit_tagged_photos(x):
    """Three field photos, each tagged to an asset (one for each review decision). Untagged, non-image and empty uploads are refused with 400."""
    a = need(x, "asset")
    png, _ = png_b64()
    refused = [({"asset_id": "", "image_b64": png}, "Tag the photo"),
               ({"asset_id": a["fixed"], "image_b64": base64.b64encode(b"hello world").decode("ascii")}, "Only JPEG or PNG"),
               ({"asset_id": a["fixed"], "image_b64": ""}, "empty")]
    for body, msg in refused:
        r = x.c.post("/api/assets/photo", dict(body, note="E2E test refused upload"))
        check(r.status == 400 and msg in str(r.body.get("detail", "")), f"refused upload gave {r.status} {brief(r.body, 160)}")
    photos = {}
    for role in ("fixed", "updated", "rejected"):
        p = want(x.c.post("/api/assets/photo", {"asset_id": a[role], "image_b64": png, "note": f"E2E test photo for {role} {STAMP}",
                                                 "source": "field"}), 200, f"submit photo {role}")
        check(p["photo_id"].startswith("PH-") and p["status"] == "uploaded" and p["ai_status"] == "pending", f"submit {role}: {brief(p, 160)}")
        check(p["tag_kind"] == "asset", f"photo {role} tagged as {p['tag_kind']}")
        photos[role] = dict(photo_id=p["photo_id"], asset_id=a[role])
    x.S["photos"] = photos
    x.note("photos " + ", ".join(f"{k} {v['photo_id']}" for k, v in photos.items()) + " (the rejected one is deleted at review)")


@test("assets")
def as_ai_check_findings(x):
    """The vision check runs in the background (polled up to 120 s): each photo has the findings keys and no confidence figure, and its image is served."""
    ph = need(x, "photos")
    deadline = time.time() + 120
    rows = {}
    while True:
        rows = {role: photo_row(x, p["asset_id"], p["photo_id"]) for role, p in ph.items()}
        if all(r and r["ai_status"] in ("done", "failed") for r in rows.values()) or time.time() >= deadline:
            break
        time.sleep(5)
    pending = [role for role, r in rows.items() if not r or r["ai_status"] not in ("done", "failed")]
    if pending:
        raise Fail(f"the AI check is still running after 120 s for {pending}")
    for role, r in rows.items():
        check(r["ai_status"] == "done", f"{role} AI check failed: {r.get('ai_error')}")
        res = r["ai_result"] or {}
        missing = sorted(set(AI_KEYS) - set(res))
        check(not missing, f"{role} findings lack {missing}")
        check(not key_anywhere(r, "confidence"), f"{role} record carries a confidence figure")
        check(res["suggested_outcome"] in (None, "fixed", "updated", "rejected"), f"{role} suggested outcome {res['suggested_outcome']!r}")
        check(isinstance(res["defects"], list) and isinstance(res["reasons"], list), f"{role} defects or reasons not lists")
        check(r["status"] == "ai_checked", f"{role} status is {r['status']} after the check")
        img = x.c.get(f"/api/assets/photo/{q(ph[role]['photo_id'])}/image")
        check(img.status == 200 and img.raw[:8] == PNG_SIG and len(img.raw) == r["bytes_len"],
              f"{role} image: HTTP {img.status}, {len(img.raw)} bytes")
    x.note("findings: " + "; ".join(f"{role} {r['ai_result'].get('suggested_outcome')} ({r['ai_result'].get('condition')})"
                                    for role, r in rows.items()))


@test("assets")
def as_review_refused_without_notes(x):
    """A review needs a note and a known decision: both are refused with 400, and the photo keeps its status."""
    ph = need(x, "photos")["fixed"]
    for body, msg in ((dict(decision="fixed", notes=""), "Add a note"), (dict(decision="approved", notes="E2E"), "must be fixed, updated or rejected")):
        r = x.c.post(f"/api/assets/photo/{q(ph['photo_id'])}/review", body)
        check(r.status == 400 and msg in str(r.body.get("detail", "")), f"review {body}: HTTP {r.status} {brief(r.body, 160)}")
    row = photo_row(x, ph["asset_id"], ph["photo_id"])
    check(row["status"] == "ai_checked", f"a refused review changed the status to {row['status']}")


@test("assets")
def as_review_fixed_closes_defects(x):
    """Accept as fixed: the asset's open defects close, leave its list, and the count drops by the number closed."""
    a = need(x, "asset")
    ph = need(x, "photos")["fixed"]
    before = want(x.c.get(f"/api/assets/{q(a['fixed'])}"), 200, "asset before")["defects"]
    r = want(x.c.post(f"/api/assets/photo/{q(ph['photo_id'])}/review", {"decision": "fixed", "notes": "E2E test: repaired, seen on site"}),
             200, "review fixed")
    check(r["status"] == "fixed", f"status {r['status']}")
    check(set(r["closed_defects"]) == {d["notification_id"] for d in before}, "closed defects differ from the open defects")
    if not before:
        check(r["message"] == "No open defect on this asset to close.", f"message {r['message']!r}")
        x.note(f"{a['fixed']} had no open defect: the decision closed nothing (no open defect in the synced gold for the assets searched)")
        return
    after = want(x.c.get(f"/api/assets/{q(a['fixed'])}"), 200, "asset after")
    still = {d["notification_id"] for d in after["defects"]}
    check(not (still & set(r["closed_defects"])), "a closed defect is still open on the asset")
    check(len(after["defects"]) == len(before) - len(r["closed_defects"]), f"open defects {len(before)} -> {len(after['defects'])}")
    check(photo_row(x, ph["asset_id"], ph["photo_id"])["status"] == "fixed", "the photo did not record the fixed decision")
    x.note(f"closed {len(r['closed_defects'])} defect(s) on {a['fixed']}; closures stay in atlas.defect_closure (no undo API)")


@test("assets")
def as_review_updated_sets_current_photo(x):
    """Accept as updated photo: it becomes the asset's current photo, in its history, and a second review is refused."""
    ph = need(x, "photos")["updated"]
    r = want(x.c.post(f"/api/assets/photo/{q(ph['photo_id'])}/review", {"decision": "updated", "notes": "E2E test: usable current photo"}),
             200, "review updated")
    check(r["status"] == "updated", f"status {r['status']}")
    v = want(x.c.get(f"/api/assets/{q(ph['asset_id'])}"), 200, "asset")
    check(v["current_photo"] and v["current_photo"]["photo_id"] == ph["photo_id"], "the asset's current photo is not this photo")
    check(any(p["photo_id"] == ph["photo_id"] and p["status"] == "updated" for p in v["photos"]), "the photo is not in the asset's history")
    again = x.c.post(f"/api/assets/photo/{q(ph['photo_id'])}/review", {"decision": "updated", "notes": "again"})
    check(again.status == 400 and "already been reviewed" in str(again.body.get("detail", "")), f"second review: HTTP {again.status}")
    x.note(f"current photo of {ph['asset_id']} is now {ph['photo_id']} (no undo API: residue)")


@test("assets")
def as_review_rejected_deletes_image(x):
    """Reject: the photo's bytes are deleted (its image is 404), its record stays with the note, and the list no longer offers an image."""
    ph = need(x, "photos")["rejected"]
    r = want(x.c.post(f"/api/assets/photo/{q(ph['photo_id'])}/review", {"decision": "rejected", "notes": "E2E test: does not show the tagged asset"}),
             200, "review rejected")
    check(r["status"] == "rejected", f"status {r['status']}")
    img = x.c.get(f"/api/assets/photo/{q(ph['photo_id'])}/image")
    check(img.status == 404, f"the rejected photo's image is HTTP {img.status}, expected 404")
    row = photo_row(x, ph["asset_id"], ph["photo_id"])
    check(row["status"] == "rejected" and row["image_deleted"] and row["image_url"] is None, f"rejected record {brief(row, 200)}")
    check(row["review_notes"], "the rejection kept no note")


# ============================================================================ planning (plan and invest, AI aided)
@test("planning")
def plan_create_run(x):
    """Create a works programme: only the first step is open, nothing is done, and it is on the programme list."""
    body = want(x.c.post("/api/planning/runs", {"title": f"E2E test run {STAMP}"}), 200, "create run")
    run = body["run"]
    check(run["run_id"].startswith("PLN-") and run["status"] == "open", f"run {brief(run, 160)}")
    check([s["key"] for s in body["steps"]] == PLAN_STEP_KEYS and not any(s["done"] for s in body["steps"]), "a step is done on a new run")
    check([s["enabled"] for s in body["steps"]] == [True, False, False, False, False], f"open steps {[s['enabled'] for s in body['steps']]}")
    runs = want(x.c.get("/api/planning/runs"), 200, "runs")["runs"]
    check(any(r["run_id"] == run["run_id"] for r in runs), "the new run is not on the programme list")
    x.S["plan_run"] = run["run_id"]
    x.note(f"run {run['run_id']} (deleted at the end of the group)")


@test("planning")
def plan_refusals_before_accept(x):
    """Before the steps before them are accepted, a later draft, the release preview and the release are each refused with 400."""
    rid = need(x, "plan_run")
    r = x.c.post(f"/api/planning/runs/{rid}/draft", {"step": "constraints"})
    check(r.status == 400 and "Forecast demand" in str(r.body.get("detail", "")), f"constraints draft before forecast: HTTP {r.status} {brief(r.body, 160)}")
    r = x.c.get(f"/api/planning/runs/{rid}/release-preview")
    check(r.status == 400 and "Complete the steps" in str(r.body.get("detail", "")), f"release preview before all steps: HTTP {r.status} {brief(r.body, 160)}")
    r = x.c.post(f"/api/planning/runs/{rid}/release", {})
    check(r.status == 400, f"release before all steps: HTTP {r.status} {brief(r.body, 160)}")
    r = x.c.post(f"/api/planning/runs/{rid}/draft", {"step": "release"})
    check(r.status == 400 and "not drafted by AI" in str(r.body.get("detail", "")), f"drafting the release: HTTP {r.status}")
    r = x.c.post(f"/api/planning/runs/{rid}/draft", {"step": "nonsense"})
    check(r.status == 400 and "No such planning step" in str(r.body.get("detail", "")), f"unknown step: HTTP {r.status}")


@test("planning")
def plan_forecast_opus(x):
    """The AI drafts the forecast in the background (polled up to 180 s): zones ranked by pressure, each with its reason, and a narrative."""
    rid = need(x, "plan_run")
    v, ver, secs = plan_draft_and_wait(x, rid, "forecast", 180)
    check(v["narrative"], "the forecast has no narrative")
    dash_free(v["narrative"], "the forecast narrative")
    rows = v["rows"]
    ranked = [r for r in rows if r.get("pressure")]
    check(rows and ranked, "no zone was ranked by pressure")
    check(all(r["pressure"] in ("high", "medium", "low") and r.get("why") for r in ranked), "a ranked zone has a bad pressure or no reason")
    x.S["plan_forecast_version"] = ver
    x.note(f"drafted in {secs:.0f} s; {len(ranked)} of {len(rows)} zones ranked")


@test("planning")
def plan_accept_forecast(x):
    """Accept the forecast: it is done, the next step opens, and an edit after it is a change waiting to be saved or discarded."""
    rid = need(x, "plan_run")
    ver = need(x, "plan_forecast_version")
    a = want(x.c.post(f"/api/planning/runs/{rid}/steps/forecast/accept", {"version": ver, "reason": "E2E test accept"}), 200, "accept forecast")
    check(a["status"] == "accepted" and a["version"] == ver and a["reset"] == [], f"accept returned {brief(a, 120)}")
    st = want(x.c.get(f"/api/planning/runs/{rid}"), 200, "run")
    by = {s["key"]: s for s in st["steps"]}
    check(by["forecast"]["done"] and by["constraints"]["enabled"] and not by["optimise"]["enabled"], "the steps did not open in order")
    view = want(x.c.get(f"/api/planning/runs/{rid}/steps/forecast"), 200, "forecast step")
    zone = next(r for r in view["rows"] if r.get("pressure"))
    other = next(p for p in ("high", "medium", "low") if p != zone["pressure"])
    want(x.c.post(f"/api/planning/runs/{rid}/steps/forecast/edit", {"version": ver, "edits": [{"item_id": zone["id"], "field": "pressure", "value": other}]}),
         200, "edit forecast")
    pend = want(x.c.get(f"/api/planning/runs/{rid}/steps/forecast"), 200, "forecast after edit")
    check(pend["pending"] and pend["status"] == "drafted", f"after the edit: pending {pend['pending']}, status {pend['status']}")
    want(x.c.post(f"/api/planning/runs/{rid}/steps/forecast/discard", {}), 200, "discard")
    back = want(x.c.get(f"/api/planning/runs/{rid}/steps/forecast"), 200, "forecast after discard")
    check(not back["pending"] and back["version"] == ver, f"after the discard: pending {back['pending']}, version {back['version']}")


@test("planning")
def plan_full_steps(x):
    """With --full-planning: draft and accept the other three AI steps (each polled up to 180 s), then release with notices, which raises
    the locked works as jobs on their outage days and puts the plan on the Released Plans board."""
    if not x.S.get("full_planning"):
        raise Skip("not requested (pass --full-planning)")
    rid = need(x, "plan_run")
    for step in PLAN_STEP_KEYS[1:4]:
        v, ver, secs = plan_draft_and_wait(x, rid, step, 180)
        want(x.c.post(f"/api/planning/runs/{rid}/steps/{step}/accept", {"version": ver, "reason": "E2E full run"}), 200, f"accept {step}")
        x.note(f"{step}: drafted in {secs:.0f} s and accepted")
    prev = want(x.c.get(f"/api/planning/runs/{rid}/release-preview"), 200, "release preview")
    check(prev["locked"], "nothing in the locked window")
    check(all(x2["notice_by"] >= want(x.c.get("/api/notify/overview"), 200, "overview")["today"] for x2 in prev["locked"][:1]), "a notice date is already past")
    rel = want(x.c.post(f"/api/planning/runs/{rid}/release", {"bundle_ids": [b["bundle_id"] for b in prev["locked"][:2]], "notices": True}), 200, "release")
    check(rel["release_id"].startswith("REL-") and rel["items"] == 2 and rel["notices"] == 2, f"release {brief(rel, 160)}")
    board = want(x.c.get(f"/api/planning/released/{rel['release_id']}"), 200, "released plan")
    check(len(board["items"]) == 2 and board["cost"]["total"] > 0, f"board {len(board['items'])} items, cost {board['cost']['total']}")
    x.S["plan_jobs"] = [i["job_id"] for i in board["items"] if i["job_id"]]
    x.S["plan_released"] = True
    x.note(f"{rel['release_id']}: {rel['jobs_raised']} jobs raised, {rel['notices']} notices scheduled")


@test("planning")
def plan_delete_run(x):
    """Delete the test programme (unless it was released): it and everything in it are gone from the programme list."""
    rid = x.S.get("plan_run")
    if not rid:
        raise Skip("no programme was created")
    if x.S.get("plan_released"):
        raise Info(f"residue: {rid} was released; {len(x.S.get('plan_jobs') or [])} jobs with source 'plan' remain on their days")
    want(x.c.delete(f"/api/planning/runs/{rid}"), 200, "delete run")
    runs = want(x.c.get("/api/planning/runs"), 200, "runs")["runs"]
    check(not any(r["run_id"] == rid for r in runs), "the deleted programme is still listed")


# ============================================================================ vegetation management
@test("vegetation")
def veg_capture(x):
    """Capture: survey coverage per campaign and per zone for the latest campaign, and the bay totals by risk tier."""
    c = want(x.c.get("/api/vegetation/capture"), 200, "capture")
    check(c["campaigns"] and c["latest_campaign"] == c["campaigns"][-1]["campaign_year"], "campaigns or the latest campaign missing")
    for k in c["campaigns"]:
        check(0 <= k["coverage_pct"] <= 100 and k["surveyed_spans"] <= k["overhead_spans"], f"coverage {brief(k, 160)}")
    check(c["zones"], "no zone coverage for the latest campaign")
    check(all(t["bays"] >= 1 and t["risk_tier_label"] for t in c["tiers"]), "a risk tier has no bays or no label")
    check(c["regions"] and sum(r["overhead_spans"] for r in c["regions"]) == sum(z["overhead_spans"] for z in c["zones"]),
          "the regions do not add up to the zones' overhead spans")
    x.note(f"{len(c['campaigns'])} campaigns, latest {c['latest_campaign']}, {len(c['regions'])} regions")


@test("vegetation")
def veg_risk(x):
    """The region summary's risk: feeders most urgent first and the riskiest bays for the map; bays that need no action are left out."""
    d = want(x.c.get("/api/vegetation/risk?limit=200"), 200, "risk")
    check(d["feeders"] and d["bays"], "the risk is missing its feeders or bays")
    check(x.c.get("/api/vegetation/identify").status == 404, "the removed identify step still answers")
    for b in d["bays"]:
        check(b["classification"] != "no_action", f"{b['bay_label']} needs no action but is listed")
        check(b["bay_label"].startswith("BAY-") and b["risk_tier_label"], f"bay {brief(b, 120)}")
    x.note(f"{len(d['bays'])} bays needing action; {len(d['feeders'])} feeders")


@test("vegetation")
def veg_candidates_free_of_tasks(x):
    """Candidates for a package: bays in the baseline that no open task holds, in plan order."""
    c = want(x.c.get("/api/vegetation/candidates?limit=300"), 200, "candidates")
    check(isinstance(c["bays"], list), "candidates is not a list")
    check(all(b["bay_id"] and b["risk_tier_label"] for b in c["bays"]), "a candidate has no id or tier label")
    seqs = [b["plan_seq"] for b in c["bays"]]
    check(seqs == sorted(seqs), "candidates are not in plan order")
    x.note(f"{len(c['bays'])} bays free for a package")


@test("vegetation")
def veg_create_package(x):
    """Issue a package of two bays to a contractor crew: the bays become tasks on the crew's list, and the package shows them issued."""
    crews = want(x.c.get("/api/vegetation/contractor/crews"), 200, "contractor crews")["crews"]
    check(crews, "no contractor crew")
    crew = crews[0]
    free = {b["bay_id"] for b in want(x.c.get("/api/vegetation/candidates?limit=1000"), 200, "candidates")["bays"]}
    actionable = want(x.c.get("/api/vegetation/risk?limit=1000"), 200, "risk")["bays"]
    picks = [b for b in actionable if b["bay_id"] in free][:2]
    if len(picks) < 2:
        raise Skip("fewer than two actionable bays are free of an open task")
    body = {"crew_id": crew["crew_id"], "bay_ids": [b["bay_id"] for b in picks], "title": f"E2E test package {STAMP}",
            "notes": "E2E test: created by tests/e2e.py"}
    p = want(x.c.post("/api/vegetation/packages", body), 200, "create package")
    check(p["package_id"].startswith("VP-") and p["bays"] == 2 and p["crew_name"] == crew["crew_name"], f"package {brief(p, 200)}")
    row = next((k for k in want(x.c.get("/api/vegetation/packages"), 200, "packages")["packages"] if k["package_id"] == p["package_id"]), None)
    check(row is not None, "the package is not on the packages list")
    check(row["bay_count"] == 2 and row["issued"] == 2 and row["closed"] == 0, f"package counts {brief(row, 200)}")
    check(row["status"] == "not_started" and row["status_label"] == "Not started", f"a new package is {row['status']}")
    x.S["veg"] = dict(package_id=p["package_id"], title=body["title"], crew_id=crew["crew_id"], crew_name=crew["crew_name"],
                      bays=[b["bay_id"] for b in picks], tasks={}, audits={}, photos=[])
    x.note(f"{p['package_id']} to {crew['crew_name']}: {', '.join(b['bay_label'] for b in picks)} (residue: package and tasks, closed at the end)")


@test("vegetation")
def veg_contractor_page_and_bays(x):
    """The contractor page loads, and the crew's own list shows the two bays of the package as issued tasks."""
    v = need(x, "veg")
    r = x.c.get("/contractor")
    check(r.status == 200 and "contractor.js" in (r.body if isinstance(r.body, str) else ""), f"/contractor HTTP {r.status}")
    bays = want(x.c.get(f"/api/vegetation/contractor/bays?crew_id={q(v['crew_id'])}"), 200, "crew bays")["bays"]
    mine = {b["bay_id"]: b for b in bays if b["bay_id"] in v["bays"] and b.get("package_title") == v["title"]}
    check(set(mine) == set(v["bays"]), f"the crew list shows {sorted(mine)} of {sorted(v['bays'])}")
    check(all(b["status"] == "issued" for b in mine.values()), f"statuses {[b['status'] for b in mine.values()]}")
    v["tasks"] = {bid: mine[bid]["task_id"] for bid in v["bays"]}


@test("vegetation")
def veg_contractor_cycle_bay_one(x):
    """The crew works the first bay: start, a photo through the contractor photo endpoint, an access exception, complete. The audit is pending."""
    v = need(x, "veg")
    bay = v["bays"][0]
    audit = veg_cycle(x, v["crew_id"], v["tasks"][bay], "bay-1", "access_blocked")
    v["audits"][bay] = audit["audit_id"]
    x.note(f"{bay}: post-cut {audit['post_cut_clearance_m']} m against {audit['required_clearance_m']} m, suggested {audit['suggested_result']}")


@test("vegetation")
def veg_contractor_cycle_bay_two(x):
    """The crew works the second bay: start, a photo, a partial-cut exception, complete. The audit is pending."""
    v = need(x, "veg")
    bay = v["bays"][1]
    audit = veg_cycle(x, v["crew_id"], v["tasks"][bay], "bay-2", "partial_cut")
    v["audits"][bay] = audit["audit_id"]
    x.note(f"{bay}: post-cut {audit['post_cut_clearance_m']} m against {audit['required_clearance_m']} m, suggested {audit['suggested_result']}")


@test("vegetation")
def veg_audits_list_pending(x):
    """The audit list shows both completed bays as pending, each with its before and after clearance, the required clearance and the photo of the cut."""
    v = need(x, "veg")
    listed = want(x.c.get("/api/vegetation/audits?result=pending"), 200, "pending audits")["audits"]
    by_id = {r["audit_id"]: r for r in listed}
    check(len(v["audits"]) == 2, "the two audits were not both recorded")
    for bay, aid in v["audits"].items():
        check(aid in by_id, f"{aid} is not in the pending audits")
        r = by_id[aid]
        check(r["result"] == "pending" and is_num(r["post_cut_clearance_m"]) and is_num(r["required_clearance_m"]), f"audit {brief(r, 200)}")
        check(r["photo_count"] >= 1 and r["suggested_result"] in ("compliant", "non_compliant"), f"audit {aid} photo or suggestion missing")
        check(r["photos"] and all(p["image_url"] and p["image_url"].startswith("/api/vegetation/photo/") for p in r["photos"]), f"audit {aid} photos {r['photos']}")


@test("vegetation")
def veg_photos_belong_to_vegetation(x):
    """The photos of a cut are vegetation records: their picture comes from the vegetation photo route, and none of them is an asset photo
    (so none reaches the Asset management review queue)."""
    v = need(x, "veg")
    check(v["photos"], "no photo was taken in this run")
    r = x.c.get(f"/api/vegetation/photo/{q(v['photos'][0])}/image")
    check(r.status == 200 and len(r.raw) > 1000, f"the bay photo's picture: HTTP {r.status}, {len(r.raw)} bytes")
    assets = {p["photo_id"] for p in want(x.c.get("/api/assets/photos"), 200, "asset photos")["photos"]}
    check(not assets & set(v["photos"]), "a bay photo is listed among the asset photos")
    check(x.c.get("/api/vegetation/photo/NOT-A-PHOTO/image").status == 404, "an unknown photo is not a 404")
    x.note(f"{len(v['photos'])} bay photos, none in Asset management")


@test("vegetation")
def veg_released_and_contractors(x):
    """Released work packages lists this run's package with its status and bays; every vegetation crew is a contractor, and the workload
    for Field staff management counts the package's bays."""
    v = need(x, "veg")
    d = want(x.c.get("/api/vegetation/released"), 200, "released")
    row = next((p for p in d["packages"] if p["package_id"] == v["package_id"]), None)
    check(row is not None and row["status"] in ("in_progress", "not_started", "complete"), f"package on the released list: {brief(row, 160)}")
    check(sum(d["counts"].values()) == len(d["packages"]), f"status counts {d['counts']} do not add up to {len(d['packages'])} packages")
    pk = want(x.c.get(f"/api/vegetation/packages/{q(v['package_id'])}"), 200, "package detail")
    check({b["bay_id"] for b in pk["bays"]} == set(v["bays"]), f"package bays {[b['bay_id'] for b in pk['bays']]}")
    crews = want(x.c.get("/api/vegetation/contractors"), 200, "contractors")["crews"]
    mine = next((c for c in crews if c["crew_id"] == v["crew_id"]), None)
    check(mine is not None and mine["open_packages"] >= 1, f"the crew's workload {brief(mine, 160)}")
    work = want(x.c.get(f"/api/vegetation/crew/{q(v['crew_id'])}/work"), 200, "crew work")
    check(any(p["package_id"] == v["package_id"] for p in work["packages"]), "the crew's work does not hold this run's package")
    rm = want(x.c.get(f"/api/rm/crew/{q(v['crew_id'])}"), 200, "the crew in Field staff management")["crew"]
    check(rm["employment_type"] == "Contractor", f"the vegetation crew is {rm['employment_type']}")
    x.note(f"{len(d['packages'])} released packages {d['counts']}; {len(crews)} vegetation contractor crews")


@test("vegetation")
def veg_decide_compliant_closes_bay(x):
    """Decide the first bay compliant: its task closes, and a second decision on the same audit is refused."""
    v = need(x, "veg")
    bay = v["bays"][0]
    aid = v["audits"][bay]
    d = want(x.c.post(f"/api/vegetation/audits/{q(aid)}/decide", {"decision": "compliant", "notes": "E2E test: post-cut clearance confirmed"}),
             200, "decide compliant")
    check(d["result"] == "compliant", f"result {d['result']}")
    t = want(x.c.get(f"/api/vegetation/task/{q(v['tasks'][bay])}"), 200, "task")
    check(t["task"]["status"] == "closed", f"the compliant bay's task is {t['task']['status']}, not closed")
    again = x.c.post(f"/api/vegetation/audits/{q(aid)}/decide", {"decision": "compliant", "notes": "again"})
    check(again.status == 400 and "already been decided" in str(again.body.get("detail", "")), f"second decision: HTTP {again.status}")


@test("vegetation")
def veg_decide_non_compliant_reissues(x):
    """Decide the second bay non-compliant: a reason is required; then it is reissued to the crew as attempt 2 and is back on the crew's list."""
    v = need(x, "veg")
    bay = v["bays"][1]
    aid = v["audits"][bay]
    r = x.c.post(f"/api/vegetation/audits/{q(aid)}/decide", {"decision": "non_compliant", "notes": ""})
    check(r.status == 400 and "Say why" in str(r.body.get("detail", "")), f"non-compliant without a reason: HTTP {r.status}")
    d = want(x.c.post(f"/api/vegetation/audits/{q(aid)}/decide", {"decision": "non_compliant", "notes": "E2E test: cut short, reissue",
                                                                  "crew_id": v["crew_id"]}), 200, "decide non-compliant")
    check(d["result"] == "non_compliant" and d["reissue"], f"decision {brief(d, 200)}")
    check(d["reissue"]["to_attempt"] == 2 and d["reissue"]["to_crew_id"] == v["crew_id"], f"reissue {d['reissue']}")
    t = want(x.c.get(f"/api/vegetation/task/{q(v['tasks'][bay])}"), 200, "task")
    check(t["task"]["status"] == "issued" and t["task"]["attempt"] == 2, f"task {t['task']['status']} attempt {t['task']['attempt']}")
    check(len(t["reissues"]) == 1, f"{len(t['reissues'])} reissues recorded")
    back = veg_crew_bay(x, v["crew_id"], bay, v["title"])
    check(back is not None and back["status"] == "issued", "the reissued bay is not back on the crew's list as issued")


@test("vegetation")
def veg_cleanup_second_cycle(x):
    """Clean up: the reissued bay is worked and decided compliant so no task of this run stays open."""
    v = need(x, "veg")
    bay = v["bays"][1]
    audit = veg_cycle(x, v["crew_id"], v["tasks"][bay], "bay-2-attempt-2")
    check(audit["attempt"] == 2, f"second cycle recorded attempt {audit['attempt']}")
    d = want(x.c.post(f"/api/vegetation/audits/{q(audit['audit_id'])}/decide", {"decision": "compliant", "notes": "E2E test cleanup: cleared"}),
             200, "decide attempt 2")
    check(d["result"] == "compliant", f"attempt 2 result {d['result']}")
    t = want(x.c.get(f"/api/vegetation/task/{q(v['tasks'][bay])}"), 200, "task")
    check(t["task"]["status"] == "closed", f"task after cleanup {t['task']['status']}")
    x.note(f"closed both bays (residue: package {v['package_id']}, its closed tasks and {len(v['photos'])} test photos stay on the record)")


# ============================================================================ search (Asset management, Search tab)
@test("search")
def asset_search(x):
    """Search: assets by full and partial ID (the exact match first), customers by NMI and by street, and crews; every row has an ID,
    a kind and a name; an empty search returns nothing."""
    nmi = "4913476228"
    crew = want(x.c.get("/api/search?q=BLK-CJ"), 200, "crew search")   # crew IDs are depot, persona and number (BLK-CJ-09)
    check(any(r["kind"] == "Crew" and r["id"].startswith("BLK-CJ") for r in crew), "a crew search found no crew")
    for q, hit in [("P204517", "P204517"), ("P20451", "P204517"), (nmi, nmi), ("KNV-F02-R01", "KNV-F02-R01")]:
        rows = want(x.c.get(f"/api/search?q={q}"), 200, f"search {q}")
        check(all(r["id"] and r["kind"] and r["name"] for r in rows), f"search {q!r} returned a row without an ID, kind or name")
        check(any(r["id"] == hit for r in rows), f"search {q!r} did not find {hit}")
        if q == hit:
            check(rows[0]["id"] == hit, f"the exact match {hit} is not listed first")
    addr = want(x.c.get(f"/api/asset/{nmi}"), 200, "sample premises")["record"]["address"]
    street = addr.split(",")[0].split(" ", 1)[-1]
    rows = [r for r in want(x.c.get(f"/api/search?q={street}"), 200, "street search") if r["kind"] == "Customer (NMI)"]
    check(rows and all(street.lower() in (r["name"] or "").lower() for r in rows), f"street search {street!r} returned a non-matching address")
    check(want(x.c.get("/api/search?q="), 200, "empty search") == [], "an empty search returned rows")
    check(x.c.get("/api/asset/NO-SUCH-ASSET").status == 404, "an unknown asset is not a 404")
    x.note(f"premises on {street}: {len(rows)} found; {sum(r['kind'] == 'Crew' for r in crew)} crews match BLK-CJ")


# ============================================================================ demo controls (runs last)
def demo(x, cmd, body=None):
    return want(x.c.post(f"/api/presenter/{cmd}", body if body is not None else {}), 200, f"demo {cmd}")


@test("demo")
def demo_removed_commands(x):
    """The situation-raising commands (trip, heat, minimum load, calm, storm and fire exercises) are gone: each is refused."""
    for cmd in ("trip", "heat", "min_load", "calm", "storm_exercise", "fire_exercise"):
        check(x.c.post(f"/api/presenter/{cmd}", {}).status == 400, f"{cmd} is still accepted")
    x.note("six removed commands refused")


@test("demo")
def demo_storm_view(x):
    """An open storm alert (raised by the detector from the live forecast): its view has the projection, capacity and top feeders."""
    s = find_sit(x, "storm")
    if not s:
        raise Skip("no storm alert is open (the forecast has no gusts of 70 km/h or more)")
    sid = s["situation_id"]
    x.S["storm_sid"] = sid
    d = sit_detail(x, sid)
    check(set(VIEW_KEYS["storm"]) <= set(d["view"]), f"storm view keys {sorted(d['view'])}")
    check(d["view"]["capacity"] and d["view"]["top_feeders"], "storm has no capacity or top feeders")
    x.note(f"{sid}: peak {d['view']['peak_day']}, {len(d['view']['top_feeders'])} top feeders")


def storm_sid(x):
    sid = x.S.get("storm_sid")
    if not sid:
        raise Skip("no storm alert is open")
    return sid


@test("demo")
def demo_storm_contractors_materials(x):
    """The storm plan: contractors move crews in (undo sends them back) and materials are recorded. There is no storm roster to publish."""
    sid = storm_sid(x)
    d = sit_plan(x, sid)
    kinds = {r["kind"]: r for r in d["recommendations"]}
    check("materials" in kinds, f"no materials recommendation in {sorted(kinds)}")
    check("standby" not in kinds, "the storm still offers a storm roster recommendation")
    ct = kinds.get("contractors")
    if ct:
        rid = ct["rec_id"]
        x.defer(lambda: settle(x, sid, rid), "settle contractors")
        a = want(sit_decide(x, sid, rid, "accept"), 200, "accept contractors")
        check(a["effects"] and "contractor line crew" in a["effects"][0], f"contractor effects {a['effects']}")
        moved = sit_detail(x, sid)
        moved_ids = [r for r in rec_by_id(moved, rid)["payload"].get("moved", [])]
        if moved_ids:
            crew = moved_ids[0]
            check(want(x.c.get(f"/api/rm/crew/{q(crew)}"), 200, "crew")["crew"]["crew_id"] == crew, "moved crew not found")
        u = want(sit_undo(x, sid, rid), 200, "undo contractors")
        check(u["effects"] and "sent back" in u["effects"][0], f"undo effects {u['effects']}")
        x.note(f"contractors: {len(moved_ids)} moved in and sent back")
    else:
        x.note("no contractor recommendation (no shortfall at P90)")
    mt = kinds["materials"]
    rid = mt["rec_id"]
    x.defer(lambda: settle(x, sid, rid), "settle materials")
    a = want(sit_decide(x, sid, rid, "accept"), 200, "accept materials")
    check(a["effects"] == ["Recorded"], f"materials effects {a['effects']}")
    want(sit_undo(x, sid, rid), 200, "undo materials")


@test("demo")
def demo_reset(x):
    """Reset demo: the trips close, and the raised jobs, stand-downs, overrides and on-hold jobs are cleared."""
    r = demo(x, "reset")
    check(r.get("ok") is True, f"reset {r}")
    check(not any(s["type"] == "trip" for s in open_situations(x)), "trips still open after reset")
    check(want(x.c.get("/api/rm/rerostered"), 200, "rerostered")["count"] == 0, "stand-downs survive the reset")
    check(want(x.c.get("/api/rm/overrides"), 200, "overrides")["count"] == 0, "overrides survive the reset")
    check(want(x.c.get("/api/rm/parked?region=BLK"), 200, "parked")["count"] == 0, "on-hold jobs survive the reset in Blacktown")
    x.note("trips closed; stand-downs, overrides and on-hold jobs cleared")


@test("demo")
def demo_live_switch(x):
    """Live data ingestion: switching on starts the run, and switching off ends it (only with --live, since it starts jobs)."""
    if not x.S.get("live_requested"):
        raise Skip("not requested (pass --live to start the live engine)")
    r = demo(x, "go_live", {"minutes": 5})
    check(r.get("ok") is True and (r.get("run_id") or r.get("already")), f"go_live {r}")
    phase = None
    for _ in range(60):   # the run takes about a minute to start ticking
        phase = want(x.c.get("/api/meta"), 200, "meta")["engine"]["phase"]
        if phase == "live":
            break
        time.sleep(5)
    check(phase == "live", f"the engine did not go live (phase {phase})")
    demo(x, "stop_live")
    for _ in range(36):
        phase = want(x.c.get("/api/meta"), 200, "meta")["engine"]["phase"]
        if phase == "idle":
            break
        time.sleep(5)
    check(phase == "idle", f"the engine did not switch off (phase {phase})")
    x.note(f"run {r.get('run_id')} went live and switched off")


# ============================================================================ runner
def run_one(t, client, state):
    """Run one test: PASS, FAIL, KNOWN, SKIP or INFO, with its duration and any cleanup failure."""
    x = Ctx(client, state)
    status, msg = "PASS", ""
    t0 = time.time()
    try:
        t["fn"](x)
    except Skip as e:
        status, msg = "SKIP", str(e)
    except Info as e:
        status, msg = "INFO", str(e)
    except Fail as e:
        status, msg = "FAIL", str(e)
    except Exception as e:   # an unexpected error in the app or the suite is a failure of the test, with its location
        tb = traceback.extract_tb(e.__traceback__)[-1]
        status, msg = "FAIL", f"{type(e).__name__}: {e} (at {os.path.basename(tb.filename)}:{tb.lineno})"
    cleanup_errors = x.run_cleanups()
    if cleanup_errors:
        if status in ("PASS", "INFO", "SKIP"):
            status = "FAIL"
        msg = (msg + "; " if msg else "") + "cleanup failed: " + " | ".join(cleanup_errors)
    if status == "FAIL" and t["known_bug"]:
        status = "KNOWN"
        msg = f"{t['known_bug']}: {KNOWN_BUGS.get(t['known_bug'], '')}. Observed: {msg}"
    dur = time.time() - t0
    return dict(group=t["group"], name=t["name"], status=status, msg=msg, secs=dur, notes=x.notes, doc=t["doc"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="End-to-end tests for the Asset Atlas app.")
    ap.add_argument("--app", required=True, help="app URL (https://<app>.databricksapps.com), as deploy.sh prints it")
    ap.add_argument("--profile", required=True, help="Databricks CLI profile for the workspace (used for the bearer token)")
    ap.add_argument("--only", default="", help="comma-separated groups to run: " + ", ".join(GROUP_ORDER))
    ap.add_argument("--keep-going", action="store_true", help="continue after a failure instead of stopping")
    ap.add_argument("--live", action="store_true", help="also switch live data ingestion on and off (starts the live engine)")
    ap.add_argument("--full-planning", action="store_true",
                    help="planning: draft and accept all six steps and release the works plan (raises jobs that cannot be withdrawn through the API)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every note as it is recorded")
    args = ap.parse_args(argv)

    only = [g.strip() for g in args.only.split(",") if g.strip()]
    bad = [g for g in only if g not in GROUP_ORDER]
    if bad:
        print(f"Unknown group(s): {', '.join(bad)}. Groups are {', '.join(GROUP_ORDER)}.")
        return 2

    print("Asset Atlas end-to-end suite")
    print(f"  app      {args.app}")
    print(f"  profile  {args.profile}")
    t_start = time.time()
    client = Client(args.app, args.profile)
    state = {"live_requested": args.live, "full_planning": args.full_planning}
    results = []
    stop = False
    for group in GROUP_ORDER:
        if only and group not in only:
            continue
        group_tests = [t for t in TESTS if t["group"] == group]
        if not group_tests:
            continue
        print(f"\n== {GROUP_TITLES[group]} ({group})")
        for t in group_tests:
            res = run_one(t, client, state)
            results.append(res)
            label = {"PASS": "PASS", "FAIL": "FAIL", "KNOWN": "KNOWN", "SKIP": "SKIP", "INFO": "INFO"}[res["status"]]
            print(f"  [{label:<5}] {res['name']:<42} {res['secs']:6.1f}s  {res['msg'] if res['status'] != 'PASS' else ''}".rstrip())
            if args.verbose or res["status"] == "PASS":
                for n in res["notes"]:
                    print(f"           . {n}")
            if res["status"] == "FAIL" and not args.keep_going:
                stop = True
                break
        if stop:
            print("\nStopped at the first failure (use --keep-going to continue).")
            break

    total = time.time() - t_start
    print("\n== Summary by group")
    print(f"  {'group':<12}{'pass':>6}{'fail':>6}{'known':>7}{'skip':>6}{'info':>6}{'time':>10}")
    counts = {}
    for g in GROUP_ORDER:
        rs = [r for r in results if r["group"] == g]
        if not rs:
            continue
        c = {k: sum(1 for r in rs if r["status"] == k) for k in ("PASS", "FAIL", "KNOWN", "SKIP", "INFO")}
        counts[g] = c
        print(f"  {g:<12}{c['PASS']:>6}{c['FAIL']:>6}{c['KNOWN']:>7}{c['SKIP']:>6}{c['INFO']:>6}{sum(r['secs'] for r in rs):>9.1f}s")
    tot = {k: sum(1 for r in results if r["status"] == k) for k in ("PASS", "FAIL", "KNOWN", "SKIP", "INFO")}
    print(f"  {'total':<12}{tot['PASS']:>6}{tot['FAIL']:>6}{tot['KNOWN']:>7}{tot['SKIP']:>6}{tot['INFO']:>6}{total:>9.1f}s")
    print(f"  HTTP calls {client.calls}, wall time {total:.0f}s")
    failed = [r for r in results if r["status"] == "FAIL"]
    if failed:
        print("\nFailures:")
        for r in failed:
            print(f"  - {r['group']}.{r['name']}: {r['msg']}")
    known = [r for r in results if r["status"] == "KNOWN"]
    if known:
        print("\nKnown app bugs (reported, not failing the run):")
        for r in known:
            print(f"  - {r['name']}: {r['msg']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
