**CONFIDENTIAL: Databricks Internal**

# Development guide

How to change Asset Atlas safely: the edit and test loop, the end-to-end tests, where things live, and the conventions the code keeps.

## The loop

1. Edit the code.
2. Redeploy with the same command you deployed with. When the data exists it redeploys the code, re-runs the Lakebase step and
   restarts the app, in about 6 minutes:
   ```bash
   ./deploy.sh --host https://<workspace-url> --catalog <catalog> [--schema <schema>]
   ```
3. Run the tests for what you changed, then the full suite (see below).

Some changes need more than a code redeploy:

| You changed | Also do |
|---|---|
| A pipeline table (`pipelines/*.py`) | Start a pipeline update (`databricks pipelines start-update <pipeline id> -p <profile>`), or redeploy with `--rebuild` to rebuild everything. |
| Removed a pipeline table | The pipeline marks it inactive but does not drop it. Drop it once with `DROP MATERIALIZED VIEW <catalog>.<schema>.<table>`. |
| The generator or `config/atlas.json` | Redeploy with `--rebuild`. |
| A Lakebase table (`serving/lakebase_*.sql`) | Nothing more: the Lakebase step runs the DDL on every deploy. A table you remove from the DDL is not dropped from existing deployments; drop it there once. |
| The synced gold list (`serving/tables*.py`) | Nothing more: new tables are synced, and tables no longer listed are removed, on the next deploy. |

## The tests

`tests/e2e.py` drives a running deployment's API the way the screens do, with a bearer token from the Databricks CLI. Every test
checks both the status and the content, and tests that change state put it back afterwards.

```bash
python3 tests/e2e.py --app https://<app-url> --profile <cli-profile> --keep-going
python3 tests/e2e.py --app https://<app-url> --profile <cli-profile> --only vegetation,assets
```

**Groups**

The groups run in this order:
- meta, situations, network, scene, resources, field, hierarchy, notices;
- customers, operations, fieldreports, assets, planning, vegetation, search;
- demo, always last.

**Run the demo group only on a deployment you are prepared to reset.** It clears runtime state: raised jobs, stand-downs, overrides
and live outages.

**Results**

| Result | Meaning |
|---|---|
| PASS | The assertions held. |
| FAIL | An assertion failed. The run stops at the first FAIL unless `--keep-going` is given. |
| SKIP | A precondition is absent in the live data. Most alert tests skip on a fresh deployment until live ingestion has raised alerts. |
| INFO | An informational probe that never fails. |

**Options**
- `--live` adds the tests that need the live engine running.
- `--full-planning` drafts and releases a whole planning programme. That raises field jobs that cannot be withdrawn, so use it
  only on a deployment you can reset.
- Some records the tests create have no delete API (notices, planning runs, field reports, photos, vegetation packages). The tests
  report what they leave behind in their notes.

**Expected results:** about 150 tests pass on a deployment with live data. On a fresh one, about 130 pass, none fail and about 20 skip.

## Where things live

| To change | Look in |
|---|---|
| A screen | `app/static/<screen>.js` (with `ui.js` for shared pieces and `glossary*.js` for help text), and its API in `app/<feature>.py` and `app/routes_<feature>.py`. |
| Field staff management or the field app | `app/rm/` (data, rostering solver, dispatch, field API) and `app/static/rm.js`, `field.js`. |
| The vegetation contractor app | `app/static/contractor.js` and the contractor section of `app/vegetation.py`. |
| What an alert shows | `app/situations.py`, the `_detail_<type>` function for its situation type. |
| When an alert is raised | `ops/detector.py` (detected situations) and `ops/live.py` (feeder trips). |
| A gold table | `pipelines/gold.py` and the domain files beside it (`customers.py`, `planning.py`, `vegetation.py`, `finance.py`). |
| What the app can read from gold | `serving/tables.py` and the `tables_*.py` files beside it. |
| The app's own tables | `serving/lakebase_*.sql`. |
| What Genie knows | `serving/build_genie.py` (its tables, instructions and example questions). |
| The network itself | `config/atlas.json` and `generator/`. |

## Conventions

- **No codes on screen.** Every code shown to a person goes through its label. The labels live in the gold reference table
  `ref_label`, built in the pipeline; the app modules keep a fallback copy. Never format a code for display in the front end.
- **Professional wording.** Text on screen is complete, punctuated sentences. Counts carry their noun ("1 crew", "3 crews"), and a
  list says how it is ordered.
- **Describe every structure.** Every Lakebase table and column has a `COMMENT ON`, and every gold table and column has a comment.
- **No dead code.** Remove a route, table, file or label when nothing uses it any more.
  - `ruff check --select F` must report only the pipeline's `spark` global.
  - Every route must be called by a screen.
  - Every table must be read by something.
- **Models are configuration.** Use `core.LLM`, `ATLAS_LLM_PLAN` and `ATLAS_LLM_VISION`; never write a model name in the code. The
  deploy chooses them.
- **The app reads Lakebase only.** It never queries the lakehouse directly. If a screen needs a gold table, add it to the synced list.

## Tools (local only, not deployed)

- **Phone demo.** `tools/field_demo.sh` puts the field app on an audience's phones. It runs `tools/field_relay.py` on your laptop
  behind a public tunnel. The relay forwards only the field app and its API, adding your Databricks login. The app's Field staff
  management screen shows the link and QR code it publishes.
- **Reference geography.** `tools/ref/build_ref.py` rebuilds `generator/ref/` (localities with postcodes, and a ground elevation
  grid) from public sources. The outputs are committed, so the setup job never needs the internet.
- **3D models and icons.** These were generated with Meshy and compressed with glTF-Transform into `app/static/models_web` and
  `app/static/icons`. The generation tooling and its source art are not part of this repository.
