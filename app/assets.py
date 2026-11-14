"""Asset photos.

A field crew sends a photo tagged to an asset or device id. The photo's bytes go to
Lakebase schema media (not fed to the lakehouse); its metadata goes to atlas.asset_photo (fed). A vision model checks the photo in a background
thread (what it shows, whether it matches the tag, condition, defects, whether the defect looks fixed, a suggested outcome, and reasons; no
confidence number is produced or shown). A reviewer then accepts it as fixed (the asset's open defects are closed in atlas.defect_closure, which
the app joins onto the synced gld_open_defect; gold is never written), accepts it as the asset's updated photo, or rejects it (its bytes are
deleted and the metadata stays for the audit).

Public interface (used by the field app's routes; vegetation reuses _decode, _dims and image for its own bay photos, which are not asset photos):
  submit_photo(asset_id, image_b64, note, actor, source="field", ref_id=None) -> dict(photo_id, status, ai_status, ...)
  photos(asset_id=None, status=None, source=None) -> list of photo metadata
  review(photo_id, decision, notes, actor) -> dict(ok, status, closed_defects, ...)
"""
import base64
import json
import os
import struct
import threading
import uuid

import core
from core import audit, pg, pq
from fieldreport import label

VISION = os.environ.get("ATLAS_LLM_VISION", "databricks-claude-sonnet-5-5")
MAX_BYTES = 8 * 1024 * 1024
DECISIONS = ("fixed", "updated", "rejected")
SOURCES = ("field",)
STAGES = ("validate", "resolve", "promote")
AI_KEYS = ("asset_class_seen", "matches_tag", "condition", "defects", "looks_fixed", "suggested_outcome", "reasons")


# ---------------------------------------------------------------- the image: validation and the size read from the header
def _decode(image_b64):
    """The raw bytes and their type from base64 (a data URL prefix is allowed). Only JPEG and PNG are accepted."""
    s = (image_b64 or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:  # noqa: BLE001 - any decode failure is the same message to the sender
        raise ValueError("The photo could not be read. Choose the photo again.")
    if not raw:
        raise ValueError("The photo is empty. Choose the photo again.")
    if len(raw) > MAX_BYTES:
        raise ValueError("The photo is larger than 8 MB. Choose a smaller photo.")
    if raw[:3] == b"\xff\xd8\xff":
        return raw, "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw, "image/png"
    raise ValueError("Only JPEG or PNG photos are accepted.")


def _dims(raw, mime):
    """Width and height from the file header (no image library needed): PNG IHDR, or the first JPEG start-of-frame marker."""
    try:
        if mime == "image/png":
            return struct.unpack(">II", raw[16:24])
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        pass
    return None, None


def image(photo_id):
    """(bytes, mime) of a photo still held, or None (a rejected photo's bytes are gone)."""
    r = pg.q("SELECT image, mime FROM media.photo_bytes WHERE photo_id = %s", (photo_id,), one=True)
    return (bytes(r["image"]), r["mime"]) if r else None


# ---------------------------------------------------------------- the asset register (synced gold, read only)
def _asset(asset_id):
    rows = pq("""SELECT asset_id, name, asset_class, asset_class_label, feeder_id, zs_id, lat, lon, customers_downstream, open_defects, sap_equnr
                 FROM gld_asset WHERE asset_id = %s""", (asset_id,), ttl=300)
    return rows[0] if rows else None


def search(q, limit=15):
    """Assets whose id or name contains the text (the field app's tag search). Exact id matches come first."""
    s = (q or "").strip()
    if len(s) < 2:
        return []
    return pq("""SELECT asset_id, name, asset_class, asset_class_label, feeder_id, lat, lon FROM gld_asset
                 WHERE asset_id ILIKE %(p)s OR name ILIKE %(p)s ORDER BY (asset_id = %(x)s) DESC, asset_id LIMIT %(n)s""",
              {"p": f"%{s}%", "x": s, "n": int(limit)}, ttl=300)


def open_defects(asset_id):
    """The asset's open defects from gold that no reviewer has closed in the app (atlas.defect_closure is anti-joined)."""
    return pg.q("""SELECT d.notification_id, d.defect_code, d.defect_label, d.description, d.priority, d.age_days, d.created_date
                   FROM gld_open_defect d WHERE d.asset_id = %s
                   AND NOT EXISTS (SELECT 1 FROM atlas.defect_closure c WHERE c.notification_id = d.notification_id)
                   ORDER BY d.priority, d.age_days DESC""", (asset_id,))


def _shape(p):
    """A photo's metadata with its codes turned into labels and the image route added."""
    p = dict(p)
    p["image_url"] = None if p.get("image_deleted") else f"/api/assets/photo/{p['photo_id']}/image"
    p["status_label"] = label("asset_photo_status", p.get("status"))
    p["source_label"] = label("asset_photo_source", p.get("source"))
    ai = p.get("ai_result") or {}
    p["ai_outcome_label"] = label("asset_ai_outcome", ai.get("suggested_outcome")) if ai.get("suggested_outcome") else None
    p["condition_label"] = label("asset_condition", ai.get("condition")) if ai.get("condition") else None
    return p


# ---------------------------------------------------------------- submit (the public interface)
def submit_photo(asset_id, image_b64, note, actor, source="field", ref_id=None):
    """Store a photo tagged to an asset or device id, then start the vision check in the background. Returns the new photo id."""
    aid = (asset_id or "").strip()
    if not aid or len(aid) > 64:
        raise ValueError("Tag the photo to an asset or device ID first.")
    if source not in SOURCES:
        raise ValueError("The photo source is not recognised.")
    raw, mime = _decode(image_b64)
    w, h = _dims(raw, mime)
    asset = _asset(aid)
    pid = f"PH-{uuid.uuid4().hex[:10].upper()}"
    pg.q("INSERT INTO media.photo_bytes (photo_id, image, mime) VALUES (%s, %s, %s)", (pid, raw, mime))
    pg.q("""INSERT INTO atlas.asset_photo (photo_id, asset_id, asset_name, tag_kind, source, ref_id, note, mime, width_px, height_px, bytes_len,
                status, ai_status, submitted_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'uploaded','pending',%s)""",
         (pid, aid, asset["name"] if asset else None, "asset" if asset else "device", source, ref_id,
          (note or "").strip()[:1000] or None, mime, w, h, len(raw), actor))
    audit(actor, "photo_submit", aid, dict(photo_id=pid, source=source, ref_id=ref_id))
    threading.Thread(target=_ai_check, args=(pid,), daemon=True).start()
    return dict(photo_id=pid, status="uploaded", ai_status="pending", asset_id=aid, tag_kind="asset" if asset else "device")


# ---------------------------------------------------------------- the vision check (background)
SYSTEM = ("You check photos that field crews and contractors take of electricity network assets for a distribution utility. "
          "Reply with one JSON object and nothing else: no markdown, no prose, and no dashes in any value.")


def _prompt(aid, asset, defects, note):
    where = f"{asset['asset_class_label']}, {asset['name']}" if asset else "unknown asset class (a device tag)"
    recorded = "; ".join(f"{d['defect_label']} ({d['description']})" for d in defects[:5]) if defects else "none recorded"
    return (f"The photo is tagged to asset {aid} ({where}). Open defects recorded on this asset: {recorded}. "
            f"The sender's note: {note or 'none'}.\n"
            "Return a JSON object with exactly these keys:\n"
            '- "asset_class_seen": the kind of equipment you can see, in a few words, or "unclear"\n'
            '- "matches_tag": true if the photo shows the tagged asset class, false if it clearly shows something else, null if it cannot tell\n'
            '- "condition": one of "good", "fair", "poor", "unclear"\n'
            '- "defects": a list of short phrases for visible defects (an empty list if none)\n'
            '- "looks_fixed": true if the recorded defects look repaired, false if they are still visible, null if none were recorded or it cannot tell\n'
            '- "suggested_outcome": one of "fixed" (the defect is repaired), "updated" (a usable current photo of the asset), '
            '"rejected" (the photo does not show the tagged asset or is unusable)\n'
            '- "reasons": up to three short plain sentences explaining the suggestion')


def _parse(text):
    """The JSON object in the model's reply, keeping only the agreed keys."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the vision model did not return JSON")
    obj = json.loads(text[start:end + 1])
    out = {k: obj.get(k) for k in AI_KEYS}
    if out["suggested_outcome"] not in DECISIONS:
        out["suggested_outcome"] = None
    if not isinstance(out["defects"], list):
        out["defects"] = []
    if not isinstance(out["reasons"], list):
        out["reasons"] = []
    return out


def _call_vision(image, mime, prompt):
    """One vision call (OpenAI-style image_url content part with a data URI), retried once with a fresh client."""
    b64 = base64.b64encode(image).decode()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]
    for attempt in range(2):
        try:
            r = core.llm_client().chat.completions.create(model=VISION, messages=msgs, max_tokens=700)
            return core.text_of(r.choices[0].message.content)
        except Exception:  # noqa: BLE001 - the client holds a token that expires: rebuild it once
            if attempt:
                raise
            core._llm = None


def _ai_check(pid):
    """Runs in a background thread: stores the model's findings, or the reason the check failed (the human review still works)."""
    try:
        row = pg.q("""SELECT p.asset_id, p.note, p.mime, b.image FROM atlas.asset_photo p JOIN media.photo_bytes b ON b.photo_id = p.photo_id
                      WHERE p.photo_id = %s""", (pid,), one=True)
        if not row:
            return   # rejected before the check ran: nothing to check
        asset = _asset(row["asset_id"])
        defects = open_defects(row["asset_id"])
        text = _call_vision(bytes(row["image"]), row["mime"], _prompt(row["asset_id"], asset, defects, row["note"]))
        result = _parse(text)
        pg.q("""UPDATE atlas.asset_photo SET ai_result = %s::jsonb, ai_status = 'done', ai_at = now(), ai_error = NULL,
                status = CASE WHEN status = 'uploaded' THEN 'ai_checked' ELSE status END WHERE photo_id = %s""",
             (json.dumps(result, default=str), pid))
    except Exception as e:  # noqa: BLE001 - recorded on the photo; the reviewer still sees it
        print(f"photo check failed for {pid}: {type(e).__name__}: {str(e)[:200]}", flush=True)
        pg.q("UPDATE atlas.asset_photo SET ai_status = 'failed', ai_at = now(), ai_error = %s WHERE photo_id = %s", (str(e)[:300], pid))


# ---------------------------------------------------------------- reading photos (the public interface)
def photos(asset_id=None, status=None, source=None, ref_id=None, limit=200):
    """Photo metadata, newest first (no bytes). Each filter is optional."""
    where, args = [], {"n": min(max(int(limit), 1), 500)}
    for col, val in (("asset_id", asset_id), ("status", status), ("source", source), ("ref_id", ref_id)):
        if val:
            where.append(f"{col} = %({col})s")
            args[col] = val
    sql = """SELECT photo_id, asset_id, asset_name, tag_kind, source, ref_id, note, mime, width_px, height_px, bytes_len, status, ai_status,
                    ai_result, ai_error, ai_at, submitted_by, submitted_at, reviewed_by, reviewed_at, review_decision, review_notes, image_deleted
             FROM atlas.asset_photo"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY submitted_at DESC LIMIT %(n)s"
    return [_shape(p) for p in pg.q(sql, args)]


# ---------------------------------------------------------------- review (the public interface)
def review(photo_id, decision, notes, actor):
    """Accept as fixed (closes the asset's open defects in the app), accept as the updated photo, or reject and delete the bytes.
    Notes are required for every decision."""
    if decision not in DECISIONS:
        raise ValueError("The decision must be fixed, updated or rejected.")
    text = (notes or "").strip()
    if not text:
        raise ValueError("Add a note for the decision.")
    p = pg.q("SELECT photo_id, asset_id, status FROM atlas.asset_photo WHERE photo_id = %s", (photo_id,), one=True)
    if not p:
        raise ValueError("The photo was not found.")
    if p["status"] in DECISIONS:
        raise ValueError("This photo has already been reviewed.")
    closed = []
    if decision == "fixed":
        for d in open_defects(p["asset_id"]):
            pg.q("""INSERT INTO atlas.defect_closure (notification_id, asset_id, photo_id, closed_by, notes)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT (notification_id) DO NOTHING""",
                 (d["notification_id"], p["asset_id"], photo_id, actor, text))
            closed.append(d["notification_id"])
    elif decision == "updated":
        pg.q("""INSERT INTO atlas.asset_current_photo (asset_id, photo_id, set_by) VALUES (%s,%s,%s)
                ON CONFLICT (asset_id) DO UPDATE SET photo_id = EXCLUDED.photo_id, set_by = EXCLUDED.set_by, set_at = now()""",
             (p["asset_id"], photo_id, actor))
    else:
        pg.q("DELETE FROM media.photo_bytes WHERE photo_id = %s", (photo_id,))
    pg.q("""UPDATE atlas.asset_photo SET status = %s, review_decision = %s, reviewed_by = %s, reviewed_at = now(), review_notes = %s,
            image_deleted = %s WHERE photo_id = %s""", (decision, decision, actor, text, decision == "rejected", photo_id))
    audit(actor, "photo_review", p["asset_id"], dict(photo_id=photo_id, decision=decision, closed_defects=closed, notes=text))
    msg = None
    if decision == "fixed":
        msg = f"Closed {len(closed)} open defect{'s' if len(closed) != 1 else ''} on this asset." if closed else "No open defect on this asset to close."
    return dict(ok=True, photo_id=photo_id, status=decision, closed_defects=closed, message=msg)


def asset_view(asset_id):
    """One asset for Asset management: its register record, open defects, the current accepted photo and the photo history."""
    a = _asset(asset_id)
    cur = pg.q("""SELECT c.photo_id, c.set_by, c.set_at FROM atlas.asset_current_photo c WHERE c.asset_id = %s""", (asset_id,), one=True)
    if cur:
        cur["image_url"] = f"/api/assets/photo/{cur['photo_id']}/image"
    return dict(asset=a, defects=open_defects(asset_id), current_photo=cur, photos=photos(asset_id=asset_id))
