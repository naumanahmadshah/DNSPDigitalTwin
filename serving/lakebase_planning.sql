-- Plan and invest (AI aided): the records of a planning programme. Schema atlas is fed to the lakehouse by the change data feed
-- (UC <schema>_lb_atlas), so these tables are the source of the app's planning silver. A run has one drafted-then-accepted output per
-- step; every planner change and approval is kept, and a release records the bundles raised as field jobs.

CREATE TABLE IF NOT EXISTS atlas.planning_run (
  run_id         TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  started_by     TEXT,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  status         TEXT NOT NULL DEFAULT 'open',      -- open (being planned), released (raised to field execution)
  current_step   TEXT,                              -- the step the planner is on: forecast, constraints, optimise, bundles, scenarios, release
  first_fy       INT,                               -- first financial year of the plan (FY27 is 2027)
  plan_years     INT,                               -- years in the demand projection and the ten year cost
  released_at    TIMESTAMPTZ,
  released_by    TEXT,
  release_id     TEXT
);
COMMENT ON TABLE atlas.planning_run IS 'One planning programme: from the demand forecast to the release of the works plan to field execution.';
COMMENT ON COLUMN atlas.planning_run.run_id IS 'Planning run id (PLN-...).';
COMMENT ON COLUMN atlas.planning_run.title IS 'The programme''s name as the planner gave it.';
COMMENT ON COLUMN atlas.planning_run.status IS 'open while it is being planned; released once its near-term works are raised to field execution.';
COMMENT ON COLUMN atlas.planning_run.current_step IS 'The step the planner is working on.';
COMMENT ON COLUMN atlas.planning_run.first_fy IS 'First financial year of the plan (2027 is FY27, July 2026 to June 2027).';

CREATE TABLE IF NOT EXISTS atlas.planning_step (
  run_id         TEXT NOT NULL,
  step_key       TEXT NOT NULL,                     -- forecast, constraints, optimise, bundles, scenarios, release
  version        INT NOT NULL,                      -- 1, 2, ... per step; each draft, edit or re-run is a new version
  status         TEXT NOT NULL,                     -- drafting, drafted, failed, accepted, superseded
  source         TEXT NOT NULL,                     -- opus (a model draft) or planner (an edit of the version before)
  instruction    TEXT,                              -- what the planner asked for in a re-run
  output         JSONB,                             -- the validated decisions and text of the step (facts are read live from the marts)
  narrative      TEXT,                              -- the step's narrative, for the screen and the release note
  model          TEXT,                              -- the model endpoint that drafted it
  input_version  INT,                               -- the accepted version of the previous step this draft was built from
  input_digest   TEXT,                              -- a short hash of the prompt data, to trace a draft to its inputs
  error          TEXT,                              -- why a draft failed
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by     TEXT,
  accepted_at    TIMESTAMPTZ,
  accepted_by    TEXT,
  PRIMARY KEY (run_id, step_key, version)
);
COMMENT ON TABLE atlas.planning_step IS 'Each version of each planning step: the model draft or the planner''s edit, and the accepted one that feeds the next step.';
COMMENT ON COLUMN atlas.planning_step.status IS 'drafting while the model works, drafted when ready, failed if it could not, accepted for the version the planner kept, superseded when a later one replaced it.';
COMMENT ON COLUMN atlas.planning_step.input_version IS 'Accepted version of the previous step this version was built from; a later change upstream makes this one stale.';

CREATE TABLE IF NOT EXISTS atlas.planning_release (
  release_id     TEXT PRIMARY KEY,                  -- REL-...
  run_id         TEXT NOT NULL,
  released_by    TEXT,
  released_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  scenario_key   TEXT,                              -- the delivery scenario released: A, B or A+B
  jobs_raised    INT NOT NULL DEFAULT 0,            -- field jobs raised in resource management
  job_ids        JSONB,                             -- the raised job ids
  payload        JSONB,                             -- the locked window, notice dates and any items that failed to raise
  status         TEXT NOT NULL DEFAULT 'released'
);
COMMENT ON TABLE atlas.planning_release IS 'A release of a planning programme''s near-term works to field execution: the scenario, the jobs raised and the notices due.';
COMMENT ON COLUMN atlas.planning_release.scenario_key IS 'The delivery scenario released: A, B or A+B.';
COMMENT ON COLUMN atlas.planning_release.jobs_raised IS 'Number of field jobs raised in resource management by this release.';

GRANT USAGE ON SCHEMA atlas TO PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON atlas.planning_run, atlas.planning_step, atlas.planning_release TO PUBLIC;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA atlas TO PUBLIC;

-- Column comments for every planning record (the change data feed carries them to the lakehouse)
COMMENT ON COLUMN atlas.planning_run.started_by IS 'Who started the programme (the signed-in planner).';
COMMENT ON COLUMN atlas.planning_run.started_at IS 'When the programme was started.';
COMMENT ON COLUMN atlas.planning_run.updated_at IS 'When the programme last changed (a draft, an edit, an acceptance or a release).';
COMMENT ON COLUMN atlas.planning_run.plan_years IS 'Years in the demand projection and the ten year cost (10).';
COMMENT ON COLUMN atlas.planning_run.released_at IS 'When the works were released to field execution (blank until then).';
COMMENT ON COLUMN atlas.planning_run.released_by IS 'Who released the works to field execution.';
COMMENT ON COLUMN atlas.planning_run.release_id IS 'The release record (REL-...) that raised the near-term works.';
COMMENT ON COLUMN atlas.planning_step.run_id IS 'The planning programme this step belongs to.';
COMMENT ON COLUMN atlas.planning_step.step_key IS 'Step: forecast, constraints, optimise, bundles, scenarios or release.';
COMMENT ON COLUMN atlas.planning_step.created_at IS 'When this version was created (drafted or edited).';
COMMENT ON COLUMN atlas.planning_step.created_by IS 'Who asked for the draft or made the edit.';
COMMENT ON COLUMN atlas.planning_step.accepted_at IS 'When the planner accepted this version (blank if it was not accepted).';
COMMENT ON COLUMN atlas.planning_step.accepted_by IS 'Who accepted this version.';
COMMENT ON COLUMN atlas.planning_release.run_id IS 'The planning programme released.';
COMMENT ON COLUMN atlas.planning_release.released_by IS 'Who released the works.';
COMMENT ON COLUMN atlas.planning_release.released_at IS 'When the works were released.';
COMMENT ON COLUMN atlas.planning_release.job_ids IS 'The field job ids raised by this release (JSON list).';
COMMENT ON COLUMN atlas.planning_release.status IS 'released: the release went ahead (failed items are listed in the payload).';
COMMENT ON COLUMN atlas.planning_step.version IS 'Version number of this step within the programme (1, 2, ...).';
COMMENT ON COLUMN atlas.planning_step.source IS 'opus: a model draft. planner: an edit the planner made to the version before.';
COMMENT ON COLUMN atlas.planning_step.instruction IS 'The note the planner gave when asking for a re-run (blank for a first draft).';
COMMENT ON COLUMN atlas.planning_step.output IS 'The validated decisions and text of the step, as JSON. Facts (scores, costs, schedules) are read live from the marts, not stored here.';
COMMENT ON COLUMN atlas.planning_step.narrative IS 'The step narrative shown to the planner.';
COMMENT ON COLUMN atlas.planning_step.model IS 'The model endpoint that drafted the version (ATLAS_LLM_PLAN).';
COMMENT ON COLUMN atlas.planning_step.input_digest IS 'A short hash of the data the draft was built from, to trace a draft to its inputs.';
COMMENT ON COLUMN atlas.planning_step.error IS 'Why the draft failed (blank otherwise).';
COMMENT ON COLUMN atlas.planning_release.release_id IS 'Release id (REL-...).';
COMMENT ON COLUMN atlas.planning_release.payload IS 'The locked window with notice dates, the bundles that failed to raise, and the selected scenario (JSON).';

-- Live progress of a draft: the stage it is in and when it entered it (the screen shows the elapsed time and the stage while it runs)
ALTER TABLE atlas.planning_step ADD COLUMN IF NOT EXISTS progress TEXT;
ALTER TABLE atlas.planning_step ADD COLUMN IF NOT EXISTS progress_at TIMESTAMPTZ;
COMMENT ON COLUMN atlas.planning_step.progress IS 'Stage of a running draft (reading the marts, asking the model, checking the draft); blank when finished.';
COMMENT ON COLUMN atlas.planning_step.progress_at IS 'When the draft entered its current stage.';

-- A programme the planner saved to come back to (from the new programme dialog, or "Save and continue later" on the release step).
ALTER TABLE atlas.planning_run ADD COLUMN IF NOT EXISTS saved_at TIMESTAMPTZ;
ALTER TABLE atlas.planning_run ADD COLUMN IF NOT EXISTS saved_by TEXT;
COMMENT ON COLUMN atlas.planning_run.saved_at IS 'When the planner last saved the programme to continue later (blank if never saved explicitly).';
COMMENT ON COLUMN atlas.planning_run.saved_by IS 'Who last saved the programme to continue later.';

-- The planner's choice on the release step, kept until the release: which bundles of the locked window go to the field, and the customer
-- notice prepared for each. "Save and continue later" stores it; the release reads it.
CREATE TABLE IF NOT EXISTS atlas.planning_selection (
  run_id         TEXT NOT NULL,
  bundle_id      TEXT NOT NULL,                     -- a work bundle of the selected delivery option's locked window
  included       BOOLEAN NOT NULL DEFAULT true,     -- false when the planner leaves the bundle out of this release
  notice_id      TEXT,                              -- the customer notice prepared for the bundle's planned outage (atlas.notice)
  updated_by     TEXT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, bundle_id)
);
COMMENT ON TABLE atlas.planning_selection IS 'The planner''s choice on the release step of a programme: the bundles kept in or left out of the release, and the customer notice prepared for each.';
COMMENT ON COLUMN atlas.planning_selection.run_id IS 'The planning programme.';
COMMENT ON COLUMN atlas.planning_selection.bundle_id IS 'Work bundle id (BND-...) in the locked near-term window.';
COMMENT ON COLUMN atlas.planning_selection.included IS 'True when the bundle goes out with the release; false when the planner leaves it for later.';
COMMENT ON COLUMN atlas.planning_selection.notice_id IS 'Customer notice prepared and scheduled for the bundle''s planned outage (blank until prepared).';
COMMENT ON COLUMN atlas.planning_selection.updated_by IS 'Who last changed the choice.';
COMMENT ON COLUMN atlas.planning_selection.updated_at IS 'When the choice last changed.';

-- A released plan: its headline figures, so the Released Plans tab lists them without re-reading every item.
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS scenario_label TEXT;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS items INT;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS customers INT;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS life_support INT;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS crew_hours NUMERIC;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS cost_total_aud NUMERIC;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS first_outage DATE;
ALTER TABLE atlas.planning_release ADD COLUMN IF NOT EXISTS last_outage DATE;
COMMENT ON COLUMN atlas.planning_release.title IS 'The programme''s name at release.';
COMMENT ON COLUMN atlas.planning_release.scenario_label IS 'The delivery option released, in words (for example Packages A and B).';
COMMENT ON COLUMN atlas.planning_release.items IS 'Work bundles released (one planned outage each).';
COMMENT ON COLUMN atlas.planning_release.customers IS 'Customers off supply across the released outages.';
COMMENT ON COLUMN atlas.planning_release.life_support IS 'Life support customers off supply across the released outages.';
COMMENT ON COLUMN atlas.planning_release.crew_hours IS 'Crew hours of the released work.';
COMMENT ON COLUMN atlas.planning_release.cost_total_aud IS 'Estimated cost of the released plan, AUD (the sum of its items).';
COMMENT ON COLUMN atlas.planning_release.first_outage IS 'Date of the first planned outage in the release.';
COMMENT ON COLUMN atlas.planning_release.last_outage IS 'Date of the last planned outage in the release.';

-- Each work bundle a release sent to the field: its planned outage, cost, the field job raised for it, the crews rostered to it and the
-- customer notice. The Released Plans board reads it; its status follows the date and the field.
CREATE TABLE IF NOT EXISTS atlas.planning_release_item (
  release_id         TEXT NOT NULL,
  bundle_id          TEXT NOT NULL,
  run_id             TEXT NOT NULL,
  package_key        TEXT,                          -- the package the bundle was in (A, B, C ...)
  region_id          TEXT,                          -- field region (depot) that does the work
  feeder_id          TEXT,
  feeder_name        TEXT,
  zs_name            TEXT,
  isolation_point    TEXT,                          -- the device opened for the outage (asset id)
  isolation_label    TEXT,                          -- the device in words
  work_types         TEXT,                          -- the kinds of work in the bundle, in words
  work_items         INT,                           -- planned jobs and open defects in the bundle
  outage_date        DATE NOT NULL,                 -- the day of the planned outage
  outage_start       TIMESTAMPTZ,                   -- planned interruption start, Sydney time
  outage_end         TIMESTAMPTZ,                   -- planned interruption end, Sydney time
  notice_by          DATE,                          -- the last day customers can be told (four business days before)
  crew_hours         NUMERIC,
  crews_needed       INT,                           -- crews on site to finish the work inside the outage day
  customers          INT,                           -- customers off supply for the outage
  life_support       INT,                           -- life support customers off supply
  cost_labour_aud    NUMERIC, cost_materials_aud NUMERIC, cost_plant_aud NUMERIC, cost_traffic_aud NUMERIC,
  cost_switching_aud NUMERIC, cost_customer_aud NUMERIC, cost_contingency_aud NUMERIC, cost_total_aud NUMERIC,
  job_id             TEXT,                          -- the field job raised in resource management (rm.raised_job)
  crew_ids           TEXT[],                        -- crews rostered to the work (blank until the roster reaches the day)
  crew_names         TEXT[],
  rostered_at        TIMESTAMPTZ,                   -- when the crews were rostered
  notice_id          TEXT,                          -- the customer notice for the outage (atlas.notice)
  primary_job_type   TEXT,                          -- work type of the longest item: the skill the rostered crews need
  PRIMARY KEY (release_id, bundle_id)
);
CREATE INDEX IF NOT EXISTS planning_release_item_date ON atlas.planning_release_item (outage_date);
COMMENT ON TABLE atlas.planning_release_item IS 'Each work bundle a released plan sent to the field: the planned outage, its estimated cost by category, the field job raised, the crews rostered and the customer notice.';
COMMENT ON COLUMN atlas.planning_release_item.release_id IS 'The release (REL-...).';
COMMENT ON COLUMN atlas.planning_release_item.bundle_id IS 'The work bundle (BND-...).';
COMMENT ON COLUMN atlas.planning_release_item.run_id IS 'The planning programme the release came from.';
COMMENT ON COLUMN atlas.planning_release_item.package_key IS 'The package the bundle was in (A, B, C and so on).';
COMMENT ON COLUMN atlas.planning_release_item.region_id IS 'Field region (depot) that does the work.';
COMMENT ON COLUMN atlas.planning_release_item.feeder_id IS 'Feeder the work is on.';
COMMENT ON COLUMN atlas.planning_release_item.feeder_name IS 'Feeder name.';
COMMENT ON COLUMN atlas.planning_release_item.zs_name IS 'Zone substation of the feeder.';
COMMENT ON COLUMN atlas.planning_release_item.isolation_point IS 'Device opened to isolate the work (asset id).';
COMMENT ON COLUMN atlas.planning_release_item.isolation_label IS 'The isolation device in words.';
COMMENT ON COLUMN atlas.planning_release_item.work_types IS 'Kinds of work in the bundle, in words.';
COMMENT ON COLUMN atlas.planning_release_item.work_items IS 'Planned jobs and open defects in the bundle.';
COMMENT ON COLUMN atlas.planning_release_item.outage_date IS 'Day of the planned outage.';
COMMENT ON COLUMN atlas.planning_release_item.outage_start IS 'Planned interruption start, Sydney time.';
COMMENT ON COLUMN atlas.planning_release_item.outage_end IS 'Planned interruption end, Sydney time.';
COMMENT ON COLUMN atlas.planning_release_item.notice_by IS 'Last day customers can be told: four business days before the outage.';
COMMENT ON COLUMN atlas.planning_release_item.crew_hours IS 'Crew hours the work needs.';
COMMENT ON COLUMN atlas.planning_release_item.crews_needed IS 'Crews on site to finish the work on the outage day.';
COMMENT ON COLUMN atlas.planning_release_item.customers IS 'Customers off supply for the planned outage.';
COMMENT ON COLUMN atlas.planning_release_item.life_support IS 'Life support customers off supply for the planned outage.';
COMMENT ON COLUMN atlas.planning_release_item.cost_labour_aud IS 'Estimated labour, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_materials_aud IS 'Estimated materials, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_plant_aud IS 'Estimated plant and equipment, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_traffic_aud IS 'Estimated traffic control, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_switching_aud IS 'Switching and outage management, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_customer_aud IS 'Customer notices and life support calls, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_contingency_aud IS 'Contingency on the direct cost, AUD.';
COMMENT ON COLUMN atlas.planning_release_item.cost_total_aud IS 'Estimated total cost of the item, AUD (2026 dollars, excluding GST).';
COMMENT ON COLUMN atlas.planning_release_item.job_id IS 'Field job raised in resource management for the work (rm.raised_job), on the outage day.';
COMMENT ON COLUMN atlas.planning_release_item.crew_ids IS 'Crews rostered to the work; blank until the roster (seven days ahead) reaches the outage day.';
COMMENT ON COLUMN atlas.planning_release_item.crew_names IS 'Names of the rostered crews, lead crew first.';
COMMENT ON COLUMN atlas.planning_release_item.rostered_at IS 'When the crews were rostered.';
COMMENT ON COLUMN atlas.planning_release_item.notice_id IS 'Customer notice for the planned outage (atlas.notice).';
COMMENT ON COLUMN atlas.planning_release_item.primary_job_type IS 'Work type of the longest item (ref_label domain job_type): the skill the rostered crews need.';

GRANT SELECT, INSERT, UPDATE, DELETE ON atlas.planning_selection, atlas.planning_release_item TO PUBLIC;
