-- Vegetation management records (feature 6): work packages issued to contractors, the bay tasks in them, photos linked to a task, the
-- exceptions and completions a contractor reports, the post-cut audits and the reissues. Schema rm, so the Lakebase change data feed
-- carries them to the lakehouse. Photo bytes never live here: a photo is referenced by its id from the asset photo pipeline (app/assets.py).
-- Applied by ops/lakebase.py after serving/lakebase_schema.sql. Idempotent (IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS rm;

-- ---------------------------------------------------------------- work packages
CREATE TABLE IF NOT EXISTS rm.veg_work_package (
  package_id    TEXT PRIMARY KEY,                -- VP-<yyyymmdd>-<n>
  title         TEXT NOT NULL,
  crew_id       TEXT NOT NULL,                   -- the contractor crew the package is issued to (gld_crew)
  crew_name     TEXT,
  region_id     TEXT,
  zs_id         TEXT,                            -- zone substation the bays sit behind
  priority_tier TEXT NOT NULL,                   -- high_risk, cut_required or planned: the most urgent tier in the package
  due_date      DATE,
  bay_count     INT NOT NULL DEFAULT 0,
  issued_by     TEXT,
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes         TEXT
);
COMMENT ON TABLE rm.veg_work_package IS 'A targeted package of vegetation bays issued to one contractor crew, by priority, with a due date.';
COMMENT ON COLUMN rm.veg_work_package.package_id IS 'Package id, VP- and the issue date and a sequence number.';
COMMENT ON COLUMN rm.veg_work_package.title IS 'Package title as the planner wrote it, e.g. the zone and tier.';
COMMENT ON COLUMN rm.veg_work_package.crew_id IS 'Contractor crew the package is issued to (serving.gld_crew).';
COMMENT ON COLUMN rm.veg_work_package.crew_name IS 'Crew leader name at issue.';
COMMENT ON COLUMN rm.veg_work_package.region_id IS 'Field region of the crew.';
COMMENT ON COLUMN rm.veg_work_package.zs_id IS 'Zone substation the bays sit behind, when the package covers one zone.';
COMMENT ON COLUMN rm.veg_work_package.priority_tier IS 'Most urgent risk tier in the package: high_risk, cut_required or planned (serving.ref_label domain veg_tier).';
COMMENT ON COLUMN rm.veg_work_package.due_date IS 'Date the package is due to be cut by.';
COMMENT ON COLUMN rm.veg_work_package.bay_count IS 'Number of bays in the package at issue.';
COMMENT ON COLUMN rm.veg_work_package.issued_by IS 'Who issued the package (the signed-in planner).';
COMMENT ON COLUMN rm.veg_work_package.issued_at IS 'When the package was issued.';
COMMENT ON COLUMN rm.veg_work_package.notes IS 'Free text from the planner for the contractor.';

-- ---------------------------------------------------------------- bay tasks
CREATE TABLE IF NOT EXISTS rm.veg_bay_task (
  task_id                TEXT PRIMARY KEY,       -- VT-<bay number>-<n>
  package_id             TEXT NOT NULL REFERENCES rm.veg_work_package (package_id),
  bay_id                 TEXT NOT NULL,          -- the span asset id (serving.gld_veg_bay)
  bay_label              TEXT,
  feeder_id              TEXT,
  zs_id                  TEXT,
  crew_id                TEXT NOT NULL,          -- the contractor crew working the bay now
  status                 TEXT NOT NULL DEFAULT 'issued',  -- issued, in_progress, awaiting_audit, closed (ref_label domain veg_task_status)
  risk_tier_at_issue     TEXT,                   -- high_risk, cut_required or planned when issued (the chip the crew sees)
  required_clearance_m   DOUBLE PRECISION,       -- the clearance the bay must reach
  clearance_before_m     DOUBLE PRECISION,       -- measured clearance when issued (LiDAR)
  attempt                INT NOT NULL DEFAULT 1, -- 1 for the first issue, +1 each time an audit reissues the bay
  due_date               DATE,
  issued_by              TEXT,
  issued_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at             TIMESTAMPTZ,
  completed_at           TIMESTAMPTZ,
  closed_at              TIMESTAMPTZ,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS veg_bay_task_crew ON rm.veg_bay_task (crew_id, status);
CREATE INDEX IF NOT EXISTS veg_bay_task_package ON rm.veg_bay_task (package_id);
-- A bay carries one open task at a time (closed bays may be issued again in a later cycle).
CREATE UNIQUE INDEX IF NOT EXISTS veg_bay_task_open_bay ON rm.veg_bay_task (bay_id) WHERE status <> 'closed';
COMMENT ON TABLE rm.veg_bay_task IS 'One bay of a work package, the contractor crew working it, its status, and the clearance numbers at issue.';
COMMENT ON COLUMN rm.veg_bay_task.task_id IS 'Task id, VT- and the bay number and a sequence number. Photos and exceptions link to it.';
COMMENT ON COLUMN rm.veg_bay_task.package_id IS 'Work package the task belongs to.';
COMMENT ON COLUMN rm.veg_bay_task.bay_id IS 'Bay id: the overhead span between two poles (the span asset id).';
COMMENT ON COLUMN rm.veg_bay_task.bay_label IS 'Bay label for the crews, e.g. BAY-213182.';
COMMENT ON COLUMN rm.veg_bay_task.feeder_id IS 'Feeder the bay sits on.';
COMMENT ON COLUMN rm.veg_bay_task.zs_id IS 'Zone substation the feeder is fed from.';
COMMENT ON COLUMN rm.veg_bay_task.crew_id IS 'Contractor crew working the bay (serving.gld_crew).';
COMMENT ON COLUMN rm.veg_bay_task.status IS 'issued, in_progress, awaiting_audit or closed (serving.ref_label domain veg_task_status).';
COMMENT ON COLUMN rm.veg_bay_task.risk_tier_at_issue IS 'The risk chip when the bay was issued: high_risk, cut_required or planned.';
COMMENT ON COLUMN rm.veg_bay_task.required_clearance_m IS 'Required minimum clearance for the bay, metres (3 m on bushfire-prone land, 2 m elsewhere).';
COMMENT ON COLUMN rm.veg_bay_task.clearance_before_m IS 'Measured clearance when the bay was issued (latest LiDAR survey), metres.';
COMMENT ON COLUMN rm.veg_bay_task.attempt IS 'Which attempt at the bay this is: 1 first, then one more for each reissue after a non-compliant audit.';
COMMENT ON COLUMN rm.veg_bay_task.due_date IS 'Date the bay is due to be cut by (from the work package).';
COMMENT ON COLUMN rm.veg_bay_task.issued_by IS 'Who issued the task.';
COMMENT ON COLUMN rm.veg_bay_task.issued_at IS 'When the task was issued (or reissued).';
COMMENT ON COLUMN rm.veg_bay_task.started_at IS 'When the crew marked the bay started.';
COMMENT ON COLUMN rm.veg_bay_task.completed_at IS 'When the crew marked the cut complete (latest attempt).';
COMMENT ON COLUMN rm.veg_bay_task.closed_at IS 'When the audit accepted the bay and closed it.';
COMMENT ON COLUMN rm.veg_bay_task.updated_at IS 'Last change to the task.';

-- ---------------------------------------------------------------- photos linked to a task (the bytes sit in the asset photo pipeline)
CREATE TABLE IF NOT EXISTS rm.veg_task_photo (
  link_id       BIGSERIAL PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES rm.veg_bay_task (task_id),
  photo_id      TEXT NOT NULL,                   -- the id returned by the asset photo pipeline submit_photo
  kind          TEXT NOT NULL,                   -- before, after or exception
  attempt       INT NOT NULL DEFAULT 1,
  note          TEXT,
  crew_id       TEXT,
  client_op_id  TEXT UNIQUE,                     -- the offline queue's id for this photo, so a replayed sync does not double it
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS veg_task_photo_task ON rm.veg_task_photo (task_id, attempt);
COMMENT ON TABLE rm.veg_task_photo IS 'Photos a contractor took of a bay (before or after the cut, or of an exception), reviewed with the cut in the post-cut audit. The picture is in media.photo_bytes under the same photo id; it is not an asset photo.';
COMMENT ON COLUMN rm.veg_task_photo.link_id IS 'Link id.';
COMMENT ON COLUMN rm.veg_task_photo.task_id IS 'Bay task the photo belongs to.';
COMMENT ON COLUMN rm.veg_task_photo.photo_id IS 'Photo id (VPH-...); the picture is held in media.photo_bytes under it.';
COMMENT ON COLUMN rm.veg_task_photo.kind IS 'before, after or exception.';
COMMENT ON COLUMN rm.veg_task_photo.attempt IS 'Attempt at the bay the photo was taken on.';
COMMENT ON COLUMN rm.veg_task_photo.note IS 'The crew''s note on the photo.';
COMMENT ON COLUMN rm.veg_task_photo.crew_id IS 'Crew that took the photo.';
COMMENT ON COLUMN rm.veg_task_photo.client_op_id IS 'Offline queue id of the upload, unique, so a replayed sync is ignored.';
COMMENT ON COLUMN rm.veg_task_photo.created_at IS 'When the photo was taken and sent.';

-- ---------------------------------------------------------------- exceptions the crew noted
CREATE TABLE IF NOT EXISTS rm.veg_exception (
  exception_id  BIGSERIAL PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES rm.veg_bay_task (task_id),
  attempt       INT NOT NULL DEFAULT 1,
  kind          TEXT NOT NULL,                   -- access_blocked, partial_cut, tree_too_large, hazard, landowner, equipment or other
  detail        TEXT,
  reported_by   TEXT,
  reported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  client_op_id  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS veg_exception_task ON rm.veg_exception (task_id, attempt);
COMMENT ON TABLE rm.veg_exception IS 'Exceptions a contractor noted on a bay: access blocked, partial cut, tree too large, hazard, and so on.';
COMMENT ON COLUMN rm.veg_exception.exception_id IS 'Exception id.';
COMMENT ON COLUMN rm.veg_exception.task_id IS 'Bay task the exception is on.';
COMMENT ON COLUMN rm.veg_exception.attempt IS 'Attempt at the bay the exception was noted on.';
COMMENT ON COLUMN rm.veg_exception.kind IS 'Exception kind (serving.ref_label domain veg_exception).';
COMMENT ON COLUMN rm.veg_exception.detail IS 'What the crew saw, in their words.';
COMMENT ON COLUMN rm.veg_exception.reported_by IS 'Crew that reported it.';
COMMENT ON COLUMN rm.veg_exception.reported_at IS 'When it was reported.';
COMMENT ON COLUMN rm.veg_exception.client_op_id IS 'Offline queue id, unique, so a replayed sync is ignored.';

-- ---------------------------------------------------------------- completions
CREATE TABLE IF NOT EXISTS rm.veg_completion (
  completion_id    BIGSERIAL PRIMARY KEY,
  task_id          TEXT NOT NULL REFERENCES rm.veg_bay_task (task_id),
  attempt          INT NOT NULL DEFAULT 1,
  crew_id          TEXT NOT NULL,
  completed_by    TEXT,
  completed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes            TEXT,
  exception_count  INT NOT NULL DEFAULT 0,
  photo_count      INT NOT NULL DEFAULT 0,
  client_op_id     TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS veg_completion_task ON rm.veg_completion (task_id, attempt);
COMMENT ON TABLE rm.veg_completion IS 'A contractor marking a bay cut complete, with the photos and exceptions of that attempt.';
COMMENT ON COLUMN rm.veg_completion.completion_id IS 'Completion id.';
COMMENT ON COLUMN rm.veg_completion.task_id IS 'Bay task completed.';
COMMENT ON COLUMN rm.veg_completion.attempt IS 'Attempt at the bay completed.';
COMMENT ON COLUMN rm.veg_completion.crew_id IS 'Crew that completed the bay.';
COMMENT ON COLUMN rm.veg_completion.completed_by IS 'Who on the crew marked it complete.';
COMMENT ON COLUMN rm.veg_completion.completed_at IS 'When the crew marked it complete.';
COMMENT ON COLUMN rm.veg_completion.notes IS 'Crew notes on the cut.';
COMMENT ON COLUMN rm.veg_completion.exception_count IS 'Exceptions noted on this attempt.';
COMMENT ON COLUMN rm.veg_completion.photo_count IS 'Photos linked to this attempt when it was completed.';
COMMENT ON COLUMN rm.veg_completion.client_op_id IS 'Offline queue id, unique, so a replayed sync is ignored.';

-- ---------------------------------------------------------------- post-cut audits
CREATE TABLE IF NOT EXISTS rm.veg_audit (
  audit_id                TEXT PRIMARY KEY,      -- VA-<task id>-<attempt>
  task_id                 TEXT NOT NULL REFERENCES rm.veg_bay_task (task_id),
  bay_id                  TEXT NOT NULL,
  attempt                 INT NOT NULL,
  completion_id           BIGINT REFERENCES rm.veg_completion (completion_id),
  required_clearance_m    DOUBLE PRECISION NOT NULL,
  clearance_before_m      DOUBLE PRECISION,      -- measured when issued
  post_cut_clearance_m    DOUBLE PRECISION,      -- measured by the post-cut capture after the crew's completion
  clearance_gain_m        DOUBLE PRECISION,      -- post-cut minus before
  post_capture_method     TEXT NOT NULL,         -- how the post-cut figure was taken (the demo simulates the capture)
  suggested_result        TEXT NOT NULL,         -- compliant or non_compliant, from the comparison (a suggestion only)
  result                  TEXT NOT NULL DEFAULT 'pending',  -- pending, compliant or non_compliant (ref_label domain veg_audit_result)
  decided_by              TEXT,
  decided_at              TIMESTAMPTZ,
  decision_notes          TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS veg_audit_result ON rm.veg_audit (result);
COMMENT ON TABLE rm.veg_audit IS 'The post-cut check of a completed bay: the clearance before and after against the required clearance, and the reviewer decision.';
COMMENT ON COLUMN rm.veg_audit.audit_id IS 'Audit id, VA- and the task and the attempt.';
COMMENT ON COLUMN rm.veg_audit.task_id IS 'Bay task audited.';
COMMENT ON COLUMN rm.veg_audit.bay_id IS 'Bay audited (the span asset id).';
COMMENT ON COLUMN rm.veg_audit.attempt IS 'Attempt at the bay audited.';
COMMENT ON COLUMN rm.veg_audit.completion_id IS 'Completion the post-cut capture followed.';
COMMENT ON COLUMN rm.veg_audit.required_clearance_m IS 'Required minimum clearance, metres.';
COMMENT ON COLUMN rm.veg_audit.clearance_before_m IS 'Clearance measured when the bay was issued, metres.';
COMMENT ON COLUMN rm.veg_audit.post_cut_clearance_m IS 'Clearance measured by the post-cut capture after the cut, metres.';
COMMENT ON COLUMN rm.veg_audit.clearance_gain_m IS 'Post-cut clearance minus the clearance before the cut, metres.';
COMMENT ON COLUMN rm.veg_audit.post_capture_method IS 'How the post-cut clearance was taken. The demo simulates the post-cut LiDAR or LiCAR capture deterministically.';
COMMENT ON COLUMN rm.veg_audit.suggested_result IS 'compliant when the post-cut clearance meets the requirement, otherwise non_compliant. The reviewer decides.';
COMMENT ON COLUMN rm.veg_audit.result IS 'pending, compliant (accepted and closed) or non_compliant (reissued to the contractor).';
COMMENT ON COLUMN rm.veg_audit.decided_by IS 'Reviewer who accepted or reissued the bay.';
COMMENT ON COLUMN rm.veg_audit.decided_at IS 'When the reviewer decided.';
COMMENT ON COLUMN rm.veg_audit.decision_notes IS 'Reviewer notes on the decision.';
COMMENT ON COLUMN rm.veg_audit.created_at IS 'When the post-cut capture was recorded.';

-- ---------------------------------------------------------------- reissues
CREATE TABLE IF NOT EXISTS rm.veg_reissue (
  reissue_id    BIGSERIAL PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES rm.veg_bay_task (task_id),
  audit_id      TEXT REFERENCES rm.veg_audit (audit_id),
  from_attempt  INT NOT NULL,
  to_attempt    INT NOT NULL,
  from_crew_id  TEXT,
  to_crew_id    TEXT NOT NULL,
  reason        TEXT,
  issued_by     TEXT,
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS veg_reissue_task ON rm.veg_reissue (task_id);
COMMENT ON TABLE rm.veg_reissue IS 'A bay sent back to a contractor after a non-compliant post-cut audit, with the reason and who sent it.';
COMMENT ON COLUMN rm.veg_reissue.reissue_id IS 'Reissue id.';
COMMENT ON COLUMN rm.veg_reissue.task_id IS 'Bay task reissued.';
COMMENT ON COLUMN rm.veg_reissue.audit_id IS 'Audit that found the cut non-compliant.';
COMMENT ON COLUMN rm.veg_reissue.from_attempt IS 'Attempt that failed the audit.';
COMMENT ON COLUMN rm.veg_reissue.to_attempt IS 'New attempt number.';
COMMENT ON COLUMN rm.veg_reissue.from_crew_id IS 'Crew of the failed attempt.';
COMMENT ON COLUMN rm.veg_reissue.to_crew_id IS 'Crew the bay is reissued to.';
COMMENT ON COLUMN rm.veg_reissue.reason IS 'Why it was reissued, in the reviewer''s words.';
COMMENT ON COLUMN rm.veg_reissue.issued_by IS 'Reviewer who reissued the bay.';
COMMENT ON COLUMN rm.veg_reissue.issued_at IS 'When it was reissued.';

-- ---------------------------------------------------------------- grants (the app's service principal reads and writes these tables)
DO $$ DECLARE t TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['veg_work_package', 'veg_bay_task', 'veg_task_photo', 'veg_exception', 'veg_completion', 'veg_audit', 'veg_reissue'] LOOP
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON rm.%I TO PUBLIC', t);
  END LOOP;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rm TO PUBLIC;
END $$;
