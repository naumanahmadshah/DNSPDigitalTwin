-- Lakebase (Postgres) store of Asset Atlas, Electricity Distribution: every table the app or the live engine writes.
-- Schemas atlas, rm and live are fed to the lakehouse by the Lakebase change data feed (UC lb_atlas, lb_rm, lb_live); schema app is
-- working plumbing and stays here; schema serving holds the gold tables synced in from the lakehouse (ops/lakebase.py).
CREATE SCHEMA IF NOT EXISTS atlas;
COMMENT ON SCHEMA atlas IS 'Situations, the decisions people take on them, customer notices, live outages and the audit trail.';
SET search_path TO atlas;

CREATE TABLE IF NOT EXISTS situation (
  situation_id   TEXT PRIMARY KEY,
  type           TEXT NOT NULL,           -- storm, fire, trip, heat, min_load, vegetation, drone_defect, fatigue, records
  title          TEXT NOT NULL,
  severity       TEXT NOT NULL,           -- critical, high, medium, info
  status         TEXT NOT NULL,           -- detected, plan_ready, decided, executing, closed
  detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source         TEXT,                    -- detector
  feeders        TEXT[],
  customers      INT DEFAULT 0,
  life_support   INT DEFAULT 0,
  critical_sites INT DEFAULT 0,
  headline       TEXT,                    -- one line for the card
  payload        JSONB DEFAULT '{}'::jsonb,
  owner          TEXT
);
COMMENT ON TABLE situation IS 'Live situations raised by the detector and the live engine.';

CREATE TABLE IF NOT EXISTS situation_event (
  id BIGSERIAL PRIMARY KEY, situation_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT, action TEXT NOT NULL, detail JSONB DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS situation_event_sid ON situation_event (situation_id, ts);

CREATE TABLE IF NOT EXISTS recommendation (
  rec_id TEXT PRIMARY KEY, situation_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
  payload JSONB DEFAULT '{}'::jsonb, status TEXT NOT NULL DEFAULT 'proposed',  -- proposed, accepted, adjusted, rejected
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_at TIMESTAMPTZ, decided_by TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS recommendation_sid ON recommendation (situation_id);

CREATE TABLE IF NOT EXISTS notification_outbox (
  msg_id TEXT PRIMARY KEY, situation_id TEXT, rec_id TEXT, nmi TEXT, audience TEXT, channel TEXT, body TEXT,
  status TEXT NOT NULL DEFAULT 'draft',   -- draft, queued, sent, delivered (delivery is simulated)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS outbox_sid ON notification_outbox (situation_id);
CREATE INDEX IF NOT EXISTS outbox_rec ON notification_outbox (rec_id, status);
COMMENT ON COLUMN notification_outbox.rec_id IS 'The notify recommendation these messages belong to; drafts are prepared in the background when it is proposed.';

COMMENT ON TABLE notification_outbox IS 'Customer messages per notice: drafted in the background when proposed, queued on Accept, sent after the undo window.';

CREATE TABLE IF NOT EXISTS live_outage (
  outage_id TEXT PRIMARY KEY, feeder_id TEXT, device_id TEXT, start_time TIMESTAMPTZ, status TEXT,  -- active, restored
  customers_off INT, customers_initial INT, life_support_off INT, cause_code TEXT, etr TIMESTAMPTZ, stages JSONB DEFAULT '[]'::jsonb,
  situation_id TEXT, crew_id TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION, restored_at TIMESTAMPTZ);
COMMENT ON TABLE live_outage IS 'Feeder outages in progress and restored, with FLISR and repair stages, written by the live engine.';
COMMENT ON TABLE situation_event IS 'The decision trail of every situation: detected, planned, accepted, undone, closed; who and when.';
COMMENT ON TABLE recommendation IS 'Actions proposed for a situation and the decision taken on each.';

-- ---------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), actor TEXT, action TEXT, entity TEXT,
  detail JSONB);
COMMENT ON TABLE audit_log IS 'Every change a person or the twin makes: who, what, on which entity, and the detail.';

-- ---------------------------------------------------------------- app plumbing (no change feed: none of it belongs in the lakehouse)
CREATE SCHEMA IF NOT EXISTS app;
COMMENT ON SCHEMA app IS 'Working state of the app and the live engine: live snapshots and engine status, cache revisions, stop order, planning exceptions, published plans, crew moves, planned minutes.';
CREATE TABLE IF NOT EXISTS app.live_state (key TEXT PRIMARY KEY, value JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

COMMENT ON TABLE app.live_state IS 'Latest live snapshots by key: feeder and zone transformer loads, engine status (live, stopping or paused), the phone link.';
CREATE TABLE IF NOT EXISTS app.region_rev (region_id TEXT PRIMARY KEY, rev BIGINT NOT NULL);
COMMENT ON TABLE app.region_rev IS 'Revision counters bumped on every change (per region, plus __global__, __crews__ and __jobs__) so caches refresh.';
CREATE TABLE IF NOT EXISTS app.field_seq (region_id TEXT NOT NULL, crew_id TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (region_id, crew_id));
COMMENT ON TABLE app.field_seq IS 'The stop order a crew chose on the field app, so every device shows it.';
CREATE TABLE IF NOT EXISTS app.planning_policy (region_id TEXT PRIMARY KEY, payload JSONB NOT NULL);
COMMENT ON TABLE app.planning_policy IS 'Supervisor exceptions to the hard guards per region (open safety incident, fatigue overtime), per crew or all.';
CREATE TABLE IF NOT EXISTS app.crew_region (crew_id TEXT PRIMARY KEY, payload JSONB NOT NULL);
COMMENT ON TABLE app.crew_region IS 'Supervisor moves of a crew to another region, with the original region (for undo) and awaiting-allocation flag.';
CREATE TABLE IF NOT EXISTS app.crew_day (crew_id TEXT PRIMARY KEY, region_id TEXT, planned_min DOUBLE PRECISION, updated_at TIMESTAMPTZ DEFAULT now());
COMMENT ON TABLE app.crew_day IS 'Each crew''s planned minutes today, written when a region''s field days are built, for the fatigue detector.';

-- ---------------------------------------------------------------- field resource management (schema rm)
-- The reference data (regions, crews, skills, tickets, leave, training, safety events, rules, planned work, storm faults, feeders,
-- labels) is synced gold in schema serving; these are the records the app and the live engine write.
CREATE SCHEMA IF NOT EXISTS rm;
COMMENT ON SCHEMA rm IS 'Field resource management records: raised jobs, holds, stand-downs, overrides, field status, live crew state.';

CREATE TABLE IF NOT EXISTS rm.raised_job (
  job_id TEXT PRIMARY KEY, job_date DATE, job_type TEXT, title TEXT, priority INT, duration_min INT, window_start_min INT, window_end_min INT,
  region_id TEXT, depot_id TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION, required_skill TEXT, required_ticket TEXT, funding TEXT,
  feeder_id TEXT, asset_id TEXT, nmi TEXT, address TEXT, suburb TEXT, customers INT, est_customer_minutes INT,
  source TEXT, situation_id TEXT, is_emergency BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS raised_job_region_date ON rm.raised_job (region_id, job_date);
COMMENT ON TABLE rm.raised_job IS 'Field work raised at run time (outage fault response, situations, dispatchers); the planned work orders are serving.gld_field_job.';
COMMENT ON COLUMN rm.raised_job.job_type IS 'Job type code (serving.ref_label domain job_type).';
COMMENT ON COLUMN rm.raised_job.priority IS 'Priority 1 (low) to 5 (urgent).';
COMMENT ON COLUMN rm.raised_job.duration_min IS 'On-site minutes.';
COMMENT ON COLUMN rm.raised_job.window_start_min IS 'Earliest start, minutes after midnight (booked start for appointments).';
COMMENT ON COLUMN rm.raised_job.window_end_min IS 'Latest finish, minutes after midnight.';
COMMENT ON COLUMN rm.raised_job.required_skill IS 'Skill code the crew must hold.';
COMMENT ON COLUMN rm.raised_job.required_ticket IS 'Ticket code the crew must hold, in date.';
COMMENT ON COLUMN rm.raised_job.funding IS 'Who pays: funding code (serving.ref_label domain funding).';
COMMENT ON COLUMN rm.raised_job.customers IS 'Customers downstream of the asset (1 for a customer premises).';
COMMENT ON COLUMN rm.raised_job.est_customer_minutes IS 'Expected customer minutes off supply if the job slips (the customer lens protects these).';
COMMENT ON COLUMN rm.raised_job.source IS 'Where the work came from (serving.ref_label domain source).';
COMMENT ON COLUMN rm.raised_job.situation_id IS 'Situation that raised the job, if any.';

CREATE TABLE IF NOT EXISTS rm.parked (region_id TEXT NOT NULL, job_id TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (region_id, job_id));
COMMENT ON TABLE rm.parked IS 'The dispatcher''s on-hold queue: stops parked by a crew or a dispatcher, with the reason, awaiting re-dispatch.';
CREATE TABLE IF NOT EXISTS rm.stood_down (region_id TEXT NOT NULL, crew_id TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (region_id, crew_id));
COMMENT ON TABLE rm.stood_down IS 'Crews stood down for the day, the reason, and where each of their stops went (snapshot of the pulled-back day).';
CREATE TABLE IF NOT EXISTS rm.assignment_override (region_id TEXT NOT NULL, job_id TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (region_id, job_id));
COMMENT ON TABLE rm.assignment_override IS 'Who owns a job after a manual assign, re-dispatch or re-roster, overlaid on the solved roster; breaches flagged.';
CREATE TABLE IF NOT EXISTS rm.field_status (region_id TEXT NOT NULL, job_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY (region_id, job_id));
COMMENT ON TABLE rm.field_status IS 'Live status of a stop: planned, driving, in_progress, done or parked.';
CREATE TABLE IF NOT EXISTS rm.crew_live (
  crew_id TEXT PRIMARY KEY, lat DOUBLE PRECISION, lon DOUBLE PRECISION, status TEXT, job_id TEXT, since TIMESTAMPTZ, updated_at TIMESTAMPTZ DEFAULT now());
COMMENT ON TABLE rm.crew_live IS 'Where a dispatched crew is now and what it is doing (travelling, on_site, available), moved by the live engine.';

-- ---------------------------------------------------------------- live streams (schema live), created from generator/records.py by ops/lakebase.py

-- ---------------------------------------------------------------- the app's service principal reads and writes every app schema
DO $$ DECLARE s TEXT; BEGIN
  FOREACH s IN ARRAY ARRAY['atlas', 'rm', 'app'] LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO PUBLIC', s);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO PUBLIC', s);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO PUBLIC', s);
  END LOOP;
END $$;
