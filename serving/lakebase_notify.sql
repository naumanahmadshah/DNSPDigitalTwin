-- Customer notices (the notification engine, Plan and Invest tab "Customer notices"). The notice record holds what the planner and the
-- reviewer work on: the trigger, the area, the business rules applied, the drafts for each audience and channel, the model's channel
-- recommendation and reason, reviewer edits and who did what when. The messages themselves are atlas.notification_outbox rows whose
-- rec_id is the notice id. Schema atlas feeds the lakehouse by change data feed (UC lb_atlas, silver slv_app_*); the drafting job's
-- working state is schema app and stays in Lakebase.
CREATE SCHEMA IF NOT EXISTS atlas;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS atlas.notice (
  notice_id TEXT PRIMARY KEY,                -- NTC- and eight hex characters; also the rec_id of its messages in notification_outbox
  notice_type TEXT NOT NULL,                 -- planned_work, network_fault, project_work or severe_weather (ref_label domain notice_type)
  trigger_kind TEXT NOT NULL,                -- planned_job, fault, project or weather (ref_label domain notice_trigger)
  trigger_ref TEXT,                          -- what set it off: the feeder and day of the planned jobs, the outage, the project code or the weather cells
  title TEXT NOT NULL,                       -- the headline the customer sees, in plain words
  description TEXT,                          -- the planner's note on the work
  area_code TEXT,                            -- area the notice covers (MET, RNG or CST)
  feeders TEXT[] NOT NULL DEFAULT '{}',      -- feeders the notice covers
  suburbs TEXT[] NOT NULL DEFAULT '{}',      -- suburbs the customers are in (for the drafts and the review)
  scope JSONB NOT NULL DEFAULT '{}'::jsonb,  -- how the customers were chosen: isolation points, outage device, weather cells or whole feeders
  window_start TIMESTAMPTZ,                  -- planned interruption start, Sydney time
  window_end TIMESTAMPTZ,                    -- planned interruption end, Sydney time
  status TEXT NOT NULL DEFAULT 'draft',      -- draft, in_review or sent
  rules JSONB NOT NULL DEFAULT '[]'::jsonb,  -- the business rules applied, each with the customers or jobs it changed
  counts JSONB NOT NULL DEFAULT '{}'::jsonb, -- customers in scope, excluded by reason, messages by audience and channel
  ai_recommendation JSONB,                   -- retired: no longer written (each customer's channel is the preference on their record)
  ai_reason TEXT,                            -- retired: no longer written
  ai_drafts JSONB,                           -- the drafts as the model wrote them, before any reviewer edit
  drafts JSONB,                              -- the drafts as they stand: audience, then channel, then the text
  edits JSONB NOT NULL DEFAULT '[]'::jsonb,  -- reviewer edits: who, when, which audience and channel
  model TEXT,                                -- the model that wrote the drafts
  send_at TIMESTAMPTZ,                       -- earliest time the SMS and contact centre messages go (after quiet hours)
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by TEXT,                          -- who sent it for review
  reviewed_at TIMESTAMPTZ,
  sent_by TEXT,                              -- who pressed Send
  sent_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS notice_status ON atlas.notice (status, created_at);
COMMENT ON TABLE atlas.notice IS 'Customer notices: the notification engine record for planned work, faults, project work and severe weather, from the trigger to the sent messages, with the rules applied, the drafts per audience and channel, the model''s channel recommendation and reason, reviewer edits, and who did what when.';
COMMENT ON COLUMN atlas.notice.notice_id IS 'Notice id (NTC- and eight hex characters); the rec_id of its messages in notification_outbox.';
COMMENT ON COLUMN atlas.notice.notice_type IS 'Kind of notice: planned_work, network_fault, project_work or severe_weather (ref_label domain notice_type).';
COMMENT ON COLUMN atlas.notice.trigger_kind IS 'What triggered it: planned_job, fault, project or weather (ref_label domain notice_trigger).';
COMMENT ON COLUMN atlas.notice.trigger_ref IS 'The trigger itself: feeder and day for planned jobs, the outage id for a fault, the project code, or the weather cells.';
COMMENT ON COLUMN atlas.notice.title IS 'Headline the customer sees, in plain words.';
COMMENT ON COLUMN atlas.notice.description IS 'The planner''s note on the work.';
COMMENT ON COLUMN atlas.notice.area_code IS 'Area the notice covers (MET, RNG or CST).';
COMMENT ON COLUMN atlas.notice.feeders IS 'Feeders the notice covers.';
COMMENT ON COLUMN atlas.notice.suburbs IS 'Suburbs the customers are in.';
COMMENT ON COLUMN atlas.notice.scope IS 'How the customers were chosen: isolation points per feeder, outage device, weather cells or whole feeders.';
COMMENT ON COLUMN atlas.notice.window_start IS 'Planned interruption start, Sydney time.';
COMMENT ON COLUMN atlas.notice.window_end IS 'Planned interruption end, Sydney time.';
COMMENT ON COLUMN atlas.notice.status IS 'draft (being prepared), in_review (sent to a reviewer) or sent (messages queued or delivered).';
COMMENT ON COLUMN atlas.notice.rules IS 'The business rules applied, each with the customers or jobs it changed.';
COMMENT ON COLUMN atlas.notice.counts IS 'Customers in scope, excluded by reason, and messages by audience and channel.';
COMMENT ON COLUMN atlas.notice.ai_recommendation IS 'Retired, no longer written: the AI once recommended a channel mix; each customer is now reached on the preferred channel on their record.';
COMMENT ON COLUMN atlas.notice.ai_reason IS 'Retired, no longer written: the reason the AI gave for its channel mix.';
COMMENT ON COLUMN atlas.notice.ai_drafts IS 'The drafts as the model wrote them, before any reviewer edit.';
COMMENT ON COLUMN atlas.notice.drafts IS 'The drafts as they stand now: audience, then channel, then the text.';
COMMENT ON COLUMN atlas.notice.edits IS 'Reviewer edits: who, when, and which audience and channel.';
COMMENT ON COLUMN atlas.notice.model IS 'The model that wrote the drafts.';
COMMENT ON COLUMN atlas.notice.send_at IS 'Earliest time the SMS and contact centre messages go: after quiet hours (8 pm to 8 am).';
COMMENT ON COLUMN atlas.notice.created_by IS 'Who created the notice.';
COMMENT ON COLUMN atlas.notice.created_at IS 'When the notice was created.';
COMMENT ON COLUMN atlas.notice.reviewed_by IS 'Who sent the notice for review.';
COMMENT ON COLUMN atlas.notice.reviewed_at IS 'When the notice was sent for review.';
COMMENT ON COLUMN atlas.notice.sent_by IS 'Who pressed Send.';
COMMENT ON COLUMN atlas.notice.sent_at IS 'When Send was pressed.';

CREATE TABLE IF NOT EXISTS atlas.notice_exclusion (
  notice_id TEXT NOT NULL,
  nmi TEXT NOT NULL,
  audience TEXT,                             -- customer or life_support
  reason TEXT NOT NULL,                      -- outside_isolation or duplicate_24h (ref_label domain exclusion_reason)
  PRIMARY KEY (notice_id, nmi));
COMMENT ON TABLE atlas.notice_exclusion IS 'Customers a notice left out, one row each, with the rule that excluded them. Customers in scope are messaged (notification_outbox).';
COMMENT ON COLUMN atlas.notice_exclusion.notice_id IS 'The notice.';
COMMENT ON COLUMN atlas.notice_exclusion.nmi IS 'National metering identifier of the customer left out.';
COMMENT ON COLUMN atlas.notice_exclusion.audience IS 'customer or life_support.';
COMMENT ON COLUMN atlas.notice_exclusion.reason IS 'Why the customer was left out: outside_isolation or duplicate_24h (ref_label domain exclusion_reason).';

-- working state of the drafting job (not in the lakehouse): one row per notice while Opus writes its drafts
CREATE TABLE IF NOT EXISTS app.notice_run (
  notice_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,                      -- running, done or failed
  model TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error TEXT);
COMMENT ON TABLE app.notice_run IS 'Working state of the GenAI drafting of each notice: running, done or failed, with the model and any error. Refreshed on every poll.';
COMMENT ON COLUMN app.notice_run.notice_id IS 'The notice being drafted.';
COMMENT ON COLUMN app.notice_run.status IS 'running, done or failed.';
COMMENT ON COLUMN app.notice_run.model IS 'The model serving the drafts.';
COMMENT ON COLUMN app.notice_run.started_at IS 'When drafting started.';
COMMENT ON COLUMN app.notice_run.finished_at IS 'When drafting finished.';
COMMENT ON COLUMN app.notice_run.error IS 'The error, when drafting failed.';

GRANT USAGE ON SCHEMA atlas, app TO PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON atlas.notice, atlas.notice_exclusion, app.notice_run TO PUBLIC;

-- The notices wizard: the channels the planner chose, the plan item a planned-work notice is for, and the release of the notice
-- (scheduled for a time, or sent at once). Status is draft, scheduled or sent.
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS channels TEXT[];
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS plan_run_id TEXT;
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS plan_bundle_id TEXT;
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS released_by TEXT;
ALTER TABLE atlas.notice ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ;
COMMENT ON COLUMN atlas.notice.channels IS 'Channels the planner chose for the notice (sms, email, app, mail); life support customers also get a contact centre call.';
COMMENT ON COLUMN atlas.notice.plan_run_id IS 'Planning programme of a planned-work notice prepared from the works plan.';
COMMENT ON COLUMN atlas.notice.plan_bundle_id IS 'Work bundle (planned outage) the notice is for.';
COMMENT ON COLUMN atlas.notice.release_id IS 'Released works plan the outage belongs to (atlas.planning_release), once released.';
COMMENT ON COLUMN atlas.notice.released_by IS 'Who released the notice (scheduled it or sent it).';
COMMENT ON COLUMN atlas.notice.released_at IS 'When the notice was released.';
COMMENT ON COLUMN atlas.notice.status IS 'draft (being prepared), scheduled (released, goes out at send_at) or sent (messages written to the outbox).';
COMMENT ON COLUMN atlas.notice.send_at IS 'When the messages go: the scheduled time, or after quiet hours for SMS and contact centre calls.';
CREATE INDEX IF NOT EXISTS notice_plan ON atlas.notice (plan_run_id, plan_bundle_id);
-- the review step is replaced by the release: a notice that was waiting in review is a draft again
UPDATE atlas.notice SET status = 'draft' WHERE status = 'in_review';
