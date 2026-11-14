-- Field status reports, asset photos and their AI and human review, defect closures, as-built records and the photo bytes, for the
-- November 2026 feature build. Schemas rm and atlas are fed to the lakehouse by the Lakebase change data feed (UC <schema>_lb_rm and
-- <schema>_lb_atlas), so they hold metadata only. Schema media holds the photo bytes and is NOT fed (no change feed, no bytes in the
-- lakehouse); a rejected photo's bytes are deleted from it.
CREATE SCHEMA IF NOT EXISTS media;
COMMENT ON SCHEMA media IS 'Photo bytes uploaded from the field app and the vegetation contractor app. Not fed to the lakehouse; the metadata lives in atlas.asset_photo (asset photos) or rm.veg_task_photo (bay photos).';

-- ---------------------------------------------------------------- field status reports (schema rm, fed)
CREATE TABLE IF NOT EXISTS rm.field_report (
  report_id          TEXT PRIMARY KEY,
  job_id             TEXT NOT NULL,
  situation_id       TEXT,
  outage_id          TEXT,
  region_id          TEXT,
  crew_id            TEXT,
  asset_id           TEXT,
  reported_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  reported_by        TEXT,
  arrived_at         TIMESTAMPTZ,
  response_note      TEXT,
  patrol_complete    BOOLEAN NOT NULL DEFAULT false,
  fault_located      BOOLEAN NOT NULL DEFAULT false,
  fault_kind         TEXT,
  fault_ref          TEXT,
  segments_total     INT,
  segments_cleared   INT,
  switching_executed BOOLEAN NOT NULL DEFAULT false,
  customers_restored INT NOT NULL DEFAULT 0,
  access_status      TEXT,
  hazards            TEXT[] NOT NULL DEFAULT '{}',
  materials          JSONB NOT NULL DEFAULT '[]'::jsonb,
  notes              TEXT,
  outcome            TEXT NOT NULL,
  photo_ids          TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS field_report_job ON rm.field_report (job_id, reported_at);
CREATE INDEX IF NOT EXISTS field_report_situation ON rm.field_report (situation_id);
CREATE INDEX IF NOT EXISTS field_report_crew ON rm.field_report (crew_id, reported_at);
COMMENT ON TABLE rm.field_report IS 'What a crew found and did on a job, filed from the field app: response, patrol, fault location, segments, switching, customers restored, access and hazards, materials, notes and the outcome. Insert only; a correction is a new report.';
COMMENT ON COLUMN rm.field_report.report_id IS 'Unique id of the report (FR- prefix).';
COMMENT ON COLUMN rm.field_report.job_id IS 'The resource management job reported on (planned work order or raised job, e.g. JOB-LIVE-... for an outage).';
COMMENT ON COLUMN rm.field_report.situation_id IS 'Situation the job belongs to, when the job was raised by a situation (null for planned work).';
COMMENT ON COLUMN rm.field_report.outage_id IS 'Live outage the job repairs, when the job is an outage response (the job id minus its JOB- prefix).';
COMMENT ON COLUMN rm.field_report.region_id IS 'Depot region of the job.';
COMMENT ON COLUMN rm.field_report.crew_id IS 'Crew that filed the report.';
COMMENT ON COLUMN rm.field_report.asset_id IS 'Asset the job was planned against (the work order or raised job asset).';
COMMENT ON COLUMN rm.field_report.reported_at IS 'When the report was filed.';
COMMENT ON COLUMN rm.field_report.reported_by IS 'Signed-in person who filed the report.';
COMMENT ON COLUMN rm.field_report.arrived_at IS 'When the crew arrived on site (crew entered), if given. Used for response time against the job creation time.';
COMMENT ON COLUMN rm.field_report.response_note IS 'How the crew responded in their own words (access route, delay, who attended).';
COMMENT ON COLUMN rm.field_report.patrol_complete IS 'True when the feeder or line patrol downstream of the device is complete.';
COMMENT ON COLUMN rm.field_report.fault_located IS 'True when the fault has been found on the patrol.';
COMMENT ON COLUMN rm.field_report.fault_kind IS 'Where the fault was found: pole, span or none (field code, labelled in the app).';
COMMENT ON COLUMN rm.field_report.fault_ref IS 'Asset id of the pole or span where the fault was located (matches gld_asset.asset_id when known).';
COMMENT ON COLUMN rm.field_report.segments_total IS 'Isolatable feeder segments on the patrol route (the NCM segments).';
COMMENT ON COLUMN rm.field_report.segments_cleared IS 'Segments confirmed clear of the fault and safe to re-energise.';
COMMENT ON COLUMN rm.field_report.switching_executed IS 'True when the switching plan has been carried out in the field.';
COMMENT ON COLUMN rm.field_report.customers_restored IS 'Customers restored by this report (by switching or re-energising), as the crew counts them.';
COMMENT ON COLUMN rm.field_report.access_status IS 'Access to the site: clear, restricted or blocked (field code, labelled in the app).';
COMMENT ON COLUMN rm.field_report.hazards IS 'Hazard codes seen on site (live wire, tree on the line, flood, fire, traffic, unstable pole); empty when none.';
COMMENT ON COLUMN rm.field_report.materials IS 'Materials used: a JSON list of {code, qty} (code from the material reference labels).';
COMMENT ON COLUMN rm.field_report.notes IS 'Free text from the crew, up to 2000 characters.';
COMMENT ON COLUMN rm.field_report.outcome IS 'The state the job is left in: fixed, temporary_repair or follow_up (field code, labelled in the app).';
COMMENT ON COLUMN rm.field_report.photo_ids IS 'Asset photos taken for this job (atlas.asset_photo photo_id values).';

-- ---------------------------------------------------------------- asset photos: metadata and review (schema atlas, fed)
CREATE TABLE IF NOT EXISTS atlas.asset_photo (
  photo_id        TEXT PRIMARY KEY,
  asset_id        TEXT NOT NULL,
  asset_name      TEXT,
  tag_kind        TEXT NOT NULL DEFAULT 'asset',
  source          TEXT NOT NULL DEFAULT 'field',
  ref_id          TEXT,
  note            TEXT,
  mime            TEXT NOT NULL DEFAULT 'image/jpeg',
  width_px        INT,
  height_px       INT,
  bytes_len       INT,
  status          TEXT NOT NULL DEFAULT 'uploaded',
  ai_status       TEXT NOT NULL DEFAULT 'pending',
  ai_result       JSONB,
  ai_error        TEXT,
  ai_at           TIMESTAMPTZ,
  submitted_by    TEXT,
  submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by     TEXT,
  reviewed_at     TIMESTAMPTZ,
  review_decision TEXT,
  review_notes    TEXT,
  image_deleted   BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS asset_photo_asset ON atlas.asset_photo (asset_id, submitted_at);
CREATE INDEX IF NOT EXISTS asset_photo_status ON atlas.asset_photo (status, submitted_at);
CREATE INDEX IF NOT EXISTS asset_photo_ref ON atlas.asset_photo (ref_id);
COMMENT ON TABLE atlas.asset_photo IS 'Metadata of every asset photo: what it is tagged to, who sent it, the AI check and the human review. The bytes are in media.photo_bytes until rejected.';
COMMENT ON COLUMN atlas.asset_photo.photo_id IS 'Unique id of the photo (PH- prefix).';
COMMENT ON COLUMN atlas.asset_photo.asset_id IS 'Asset or device tag the photo is tagged to (gld_asset.asset_id when it is a known asset).';
COMMENT ON COLUMN atlas.asset_photo.asset_name IS 'Name of the tagged asset at upload time, for display.';
COMMENT ON COLUMN atlas.asset_photo.tag_kind IS 'asset (found in the asset register) or device (a device tag not in the register).';
COMMENT ON COLUMN atlas.asset_photo.source IS 'Who sent the photo: field (the crew app). Bay photos from vegetation contractors are kept in rm.veg_task_photo instead.';
COMMENT ON COLUMN atlas.asset_photo.ref_id IS 'The job, work package or bay the photo belongs to.';
COMMENT ON COLUMN atlas.asset_photo.note IS 'The sender note.';
COMMENT ON COLUMN atlas.asset_photo.mime IS 'Image type of the bytes (image/jpeg after the client resize).';
COMMENT ON COLUMN atlas.asset_photo.width_px IS 'Image width in pixels, when it could be read.';
COMMENT ON COLUMN atlas.asset_photo.height_px IS 'Image height in pixels, when it could be read.';
COMMENT ON COLUMN atlas.asset_photo.bytes_len IS 'Size of the image in bytes.';
COMMENT ON COLUMN atlas.asset_photo.status IS 'uploaded, ai_checked, fixed, updated or rejected (field code, labelled in the app).';
COMMENT ON COLUMN atlas.asset_photo.ai_status IS 'pending, done or failed: the vision check in the background.';
COMMENT ON COLUMN atlas.asset_photo.ai_result IS 'Strict JSON from the vision model: asset class seen, whether it matches the tag, condition, defects, whether it looks fixed, suggested outcome, reasons. No confidence score is stored or shown.';
COMMENT ON COLUMN atlas.asset_photo.ai_error IS 'Why the vision check failed, when it did (the human review still works).';
COMMENT ON COLUMN atlas.asset_photo.ai_at IS 'When the vision check finished.';
COMMENT ON COLUMN atlas.asset_photo.submitted_by IS 'Person or contractor who uploaded the photo.';
COMMENT ON COLUMN atlas.asset_photo.submitted_at IS 'When the photo was uploaded.';
COMMENT ON COLUMN atlas.asset_photo.reviewed_by IS 'Reviewer who accepted or rejected the photo.';
COMMENT ON COLUMN atlas.asset_photo.reviewed_at IS 'When the review was made.';
COMMENT ON COLUMN atlas.asset_photo.review_decision IS 'fixed, updated or rejected.';
COMMENT ON COLUMN atlas.asset_photo.review_notes IS 'Reviewer notes (required for every decision).';
COMMENT ON COLUMN atlas.asset_photo.image_deleted IS 'True once a rejected photo has had its bytes deleted (the metadata stays for the audit).';

CREATE TABLE IF NOT EXISTS atlas.asset_current_photo (
  asset_id   TEXT PRIMARY KEY,
  photo_id   TEXT NOT NULL,
  set_by     TEXT,
  set_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE atlas.asset_current_photo IS 'The photo a reviewer accepted as the asset''s current picture (accept as updated photo). One row per asset.';
COMMENT ON COLUMN atlas.asset_current_photo.asset_id IS 'The asset whose picture is replaced.';
COMMENT ON COLUMN atlas.asset_current_photo.photo_id IS 'The accepted photo (atlas.asset_photo).';
COMMENT ON COLUMN atlas.asset_current_photo.set_by IS 'Reviewer who accepted it.';
COMMENT ON COLUMN atlas.asset_current_photo.set_at IS 'When it was accepted.';

-- ---------------------------------------------------------------- defect closures the app joins onto gld_open_defect (gold is never written)
CREATE TABLE IF NOT EXISTS atlas.defect_closure (
  notification_id TEXT PRIMARY KEY,
  asset_id        TEXT NOT NULL,
  photo_id        TEXT,
  closed_by       TEXT,
  closed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes           TEXT,
  method          TEXT NOT NULL DEFAULT 'photo_review'
);
CREATE INDEX IF NOT EXISTS defect_closure_asset ON atlas.defect_closure (asset_id);
COMMENT ON TABLE atlas.defect_closure IS 'Open defects closed in the app (a reviewer accepted a photo as fixed). The app anti-joins this table onto the synced gld_open_defect, so the gold table is never written.';
COMMENT ON COLUMN atlas.defect_closure.notification_id IS 'The defect notification closed (gld_open_defect.notification_id).';
COMMENT ON COLUMN atlas.defect_closure.asset_id IS 'The asset the defect is on.';
COMMENT ON COLUMN atlas.defect_closure.photo_id IS 'The photo that evidenced the fix (atlas.asset_photo).';
COMMENT ON COLUMN atlas.defect_closure.closed_by IS 'Reviewer who accepted the fix.';
COMMENT ON COLUMN atlas.defect_closure.closed_at IS 'When the defect was closed in the app.';
COMMENT ON COLUMN atlas.defect_closure.notes IS 'Reviewer notes that accompany the closure.';
COMMENT ON COLUMN atlas.defect_closure.method IS 'How it was closed (photo_review).';

-- ---------------------------------------------------------------- the photo bytes (schema media, not fed)
CREATE TABLE IF NOT EXISTS media.photo_bytes (
  photo_id    TEXT PRIMARY KEY,
  image       BYTEA NOT NULL,
  mime        TEXT NOT NULL DEFAULT 'image/jpeg',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE media.photo_bytes IS 'The image bytes of asset photos, keyed by photo id. Deleted when the photo is rejected. Not fed to the lakehouse.';
COMMENT ON COLUMN media.photo_bytes.photo_id IS 'The photo (atlas.asset_photo.photo_id).';
COMMENT ON COLUMN media.photo_bytes.image IS 'The image file as uploaded (JPEG after the client resize).';
COMMENT ON COLUMN media.photo_bytes.mime IS 'Image type of the bytes.';
COMMENT ON COLUMN media.photo_bytes.created_at IS 'When the bytes were stored.';

-- ---------------------------------------------------------------- grants: the app's service principal reads and writes these schemas
-- (this runs after the tables above exist, so the new tables are covered; lakebase_schema.sql grants only the tables it creates)
DO $$ DECLARE s TEXT; BEGIN
  FOREACH s IN ARRAY ARRAY['atlas', 'rm', 'media'] LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO PUBLIC', s);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO PUBLIC', s);
  END LOOP;
END $$;
