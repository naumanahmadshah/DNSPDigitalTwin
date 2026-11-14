-- Emergency dispatch watch: every crew sent to an emergency job is watched until it reports. If the crew sends no status update by the
-- expected finish (travel plus the job's time) and a grace period, the app raises a "crew overdue" alert linked to the situation the
-- crew was sent to (app/overdue.py). Schema rm feeds the lakehouse by change data feed (UC <schema>_lb_rm).

-- when each job's field status last changed (the crew's latest update), whichever surface set it: the field app, the live engine or a dispatch
ALTER TABLE rm.field_status ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
COMMENT ON COLUMN rm.field_status.updated_at IS 'When the status last changed: the crew''s latest update on the job (the emergency watch reads it).';

CREATE TABLE IF NOT EXISTS rm.emergency_watch (
  job_id                TEXT PRIMARY KEY,                 -- the emergency job (rm.raised_job)
  region_id             TEXT NOT NULL,
  crew_id               TEXT NOT NULL,                    -- the crew sent
  crew_name             TEXT,
  situation_id          TEXT,                             -- the situation the crew was sent to (atlas.situation), if any
  outage_id             TEXT,                             -- the outage the job repairs (atlas.live_outage), if any
  dispatched_at         TIMESTAMPTZ NOT NULL,
  dispatched_by         TEXT,
  travel_min            INT NOT NULL,                     -- planned travel to the job
  duration_min          INT NOT NULL,                     -- planned time on the job
  grace_min             INT NOT NULL,                     -- extra time allowed before the crew counts as overdue
  expected_by           TIMESTAMPTZ NOT NULL,             -- when the crew should be done
  due_at                TIMESTAMPTZ NOT NULL,             -- expected_by plus the grace, or the welfare call's deadline: past this with no update, the crew is overdue
  last_update_at        TIMESTAMPTZ,                      -- the crew's latest status update on the job
  last_status           TEXT,                             -- that update: driving, in_progress, done or parked
  welfare_checks        INT NOT NULL DEFAULT 0,           -- welfare calls logged while the crew was overdue
  status                TEXT NOT NULL DEFAULT 'watching', -- watching, overdue or closed
  overdue_situation_id  TEXT,                             -- the crew overdue alert raised (atlas.situation)
  closed_at             TIMESTAMPTZ,
  closed_reason         TEXT                              -- done, parked, recalled, taken over or restored
);
CREATE INDEX IF NOT EXISTS emergency_watch_open ON rm.emergency_watch (status, due_at);
COMMENT ON TABLE rm.emergency_watch IS 'Every crew sent to an emergency job, watched until it reports: the expected finish with a grace period, the crew''s latest update, and the crew overdue alert raised when the crew goes quiet past it.';
COMMENT ON COLUMN rm.emergency_watch.job_id IS 'The emergency job the crew was sent to (rm.raised_job).';
COMMENT ON COLUMN rm.emergency_watch.region_id IS 'Field region (depot) of the job.';
COMMENT ON COLUMN rm.emergency_watch.crew_id IS 'The crew sent.';
COMMENT ON COLUMN rm.emergency_watch.crew_name IS 'The crew''s name.';
COMMENT ON COLUMN rm.emergency_watch.situation_id IS 'The situation the crew was sent to (atlas.situation), if the job came from one.';
COMMENT ON COLUMN rm.emergency_watch.outage_id IS 'The outage the job repairs (atlas.live_outage), if any.';
COMMENT ON COLUMN rm.emergency_watch.dispatched_at IS 'When the crew was sent.';
COMMENT ON COLUMN rm.emergency_watch.dispatched_by IS 'Who sent the crew.';
COMMENT ON COLUMN rm.emergency_watch.travel_min IS 'Planned travel to the job, minutes.';
COMMENT ON COLUMN rm.emergency_watch.duration_min IS 'Planned time on the job, minutes.';
COMMENT ON COLUMN rm.emergency_watch.grace_min IS 'Extra minutes allowed past the expected finish before the crew counts as overdue (a quarter of the expected time, at least 30).';
COMMENT ON COLUMN rm.emergency_watch.expected_by IS 'When the crew should be done: dispatch plus travel plus the job time; on arrival, the arrival plus the job time.';
COMMENT ON COLUMN rm.emergency_watch.due_at IS 'Expected finish plus the grace (after a welfare call, the time the call gave the crew): past this with no update, the crew overdue alert is raised, or escalated.';
COMMENT ON COLUMN rm.emergency_watch.last_update_at IS 'The crew''s latest status update on the job (rm.field_status.updated_at).';
COMMENT ON COLUMN rm.emergency_watch.last_status IS 'That update: driving, in_progress, done or parked.';
COMMENT ON COLUMN rm.emergency_watch.welfare_checks IS 'Welfare calls logged while the crew was overdue.';
COMMENT ON COLUMN rm.emergency_watch.status IS 'watching (inside its time), overdue (an alert is open) or closed.';
COMMENT ON COLUMN rm.emergency_watch.overdue_situation_id IS 'The crew overdue alert raised for this dispatch (atlas.situation SIT-OVERDUE-...).';
COMMENT ON COLUMN rm.emergency_watch.closed_at IS 'When the watch closed.';
COMMENT ON COLUMN rm.emergency_watch.closed_reason IS 'Why it closed: done, parked, recalled, taken over by another crew, or the outage restored.';
ALTER TABLE rm.emergency_watch REPLICA IDENTITY FULL;
GRANT SELECT, INSERT, UPDATE, DELETE ON rm.emergency_watch TO PUBLIC;
