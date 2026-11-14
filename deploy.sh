#!/usr/bin/env bash
# Deploy Asset Atlas, Electricity Distribution into a Databricks workspace: its own schemas, volumes, Lakebase project, jobs,
# pipeline, Genie space and app, independent of anything else deployed in the same workspace or catalog.
#
#   ./deploy.sh --host https://<workspace> --catalog <catalog> [--schema <schema>] [options]
#
# Options
#   --schema <s>        medallion schema (default atlas_electricity); created if missing, with <s>_serving beside it
#   --name <n>          deployment name for the app, the Lakebase project and the jobs (default from the schema, e.g. atlas-electricity)
#   --warehouse-id <id> SQL warehouse for Genie (default: the first serverless warehouse; one is created if there is none)
#   --profile <p>       Databricks CLI profile (default: a profile for --host, created with an OAuth login if needed)
#   --llm <endpoint>    model for SMS drafts and site-view speech (default: the newest Claude Haiku the workspace serves)
#   --llm-plan <e>      model for AI-aided planning, notices and work packages (default: the newest Claude Opus available)
#   --llm-vision <e>    model for the field photo checks (default: the newest Claude Sonnet available)
#   --code-only         redeploy code and restart the app without rebuilding data (default when the data already exists)
#   --rebuild           regenerate the data and rebuild everything even if it exists
#   --live <minutes>    start the live engine for this many minutes after deploying
#   --destroy           remove everything this deployment created (asks first); the same as ./cleanup.sh
set -euo pipefail
cd "$(dirname "$0")"
source ./deploy_lib.sh

WAREHOUSE="" LLM="" LLM_PLAN="" LLM_VISION="" MODE="auto" LIVE=0 DESTROY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST=$(norm_host "$2"); shift 2 ;;
    --catalog) CATALOG="$2"; shift 2 ;;
    --schema) SCHEMA="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --warehouse-id) WAREHOUSE="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --llm) LLM="$2"; shift 2 ;;
    --llm-plan) LLM_PLAN="$2"; shift 2 ;;
    --llm-vision) LLM_VISION="$2"; shift 2 ;;
    --code-only) MODE="code"; shift ;;
    --rebuild) MODE="rebuild"; shift ;;
    --live) LIVE="$2"; shift 2 ;;
    --destroy) DESTROY=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 2 ;;
  esac
done
if [[ $DESTROY == 1 ]]; then   # the removal is cleanup.sh's; hand over the options that name the deployment
  exec ./cleanup.sh --host "$HOST" --catalog "$CATALOG" --schema "$SCHEMA" ${NAME:+--name "$NAME"} ${PROFILE:+--profile "$PROFILE"}
fi
atlas_init

# ---------------------------------------------------------------- SQL warehouse (Genie)
if [[ -z "$WAREHOUSE" ]]; then
  WAREHOUSE=$(D warehouses list -o json | json "next((w['id'] for w in sorted(d, key=lambda w: w.get('state') != 'RUNNING') if w.get('enable_serverless_compute')), '')")
  if [[ -z "$WAREHOUSE" ]]; then
    step "create a serverless SQL warehouse"
    WAREHOUSE=$(D warehouses create --name "$NAME" --cluster-size "2X-Small" --enable-serverless-compute --max-num-clusters 1 --auto-stop-mins 10 \
      --warehouse-type PRO -o json | json "d['id']")
  fi
fi
echo "warehouse $WAREHOUSE"
# The three foundation models the app calls, chosen now and fixed in the app's configuration (the app never switches model at run
# time). For each role: the families in order of preference, each family newest first among the Claude endpoints the workspace serves
# (databricks-claude-<family>-<major>[-<minor>], version 4 or later); the first that answers a one-line request is used. A workspace can
# list a model yet refuse it (the newest can need account approval), which is why each one is asked rather than only looked up.
ENDPOINTS=$(D serving-endpoints list -o json 2>/dev/null | json "' '.join(e['name'] for e in d)" 2>/dev/null || true)
newest() {   # <family>...: the families' endpoints, newest first within each family
  python3 -c '
import re, sys
names, out = sys.stdin.read().split(), []
for fam in sys.argv[1:]:
    found = [((int(m.group(1)), int(m.group(2) or 0)), n) for n in names
             for m in [re.fullmatch(r"databricks-claude-" + fam + r"-(\d+)(?:-(\d+))?", n)] if m and int(m.group(1)) >= 4]
    out += [n for _, n in sorted(found, reverse=True)]
print(" ".join(out))' "$@" <<< "$ENDPOINTS"
}
answers() { D api post "/serving-endpoints/$1/invocations" --json '{"messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":16}' 2>/dev/null | grep -q '"choices"'; }
pick_model() {   # <role> <bundle variable> <model chosen with an option, or empty> <family>...
  local role="$1" var="$2" want="$3" skipped=""; shift 3
  if [[ -n "$want" ]]; then
    if answers "$want"; then echo "model for $role: $want (chosen with an option)"; BUNDLE_VARS+=(--var "$var=$want"); return; fi
    echo "warning: $want does not answer in this workspace; using the newest model that does"
  fi
  for m in $(newest "$@"); do
    if answers "$m"; then
      echo "model for $role: $m${skipped:+ (newer, not available to this workspace:$skipped)}"; BUNDLE_VARS+=(--var "$var=$m"); return
    fi
    skipped="$skipped ${m#databricks-claude-}"
  done
  echo "No Claude model answers in this workspace for $role. Enable the Foundation Model API (or approve model access), or choose an"
  echo "endpoint that answers with --llm, --llm-plan or --llm-vision."; exit 1
}
pick_model "SMS drafts and site-view speech" llm_endpoint "$LLM" haiku sonnet
pick_model "AI-aided planning, notices and work packages" llm_plan_endpoint "$LLM_PLAN" opus sonnet
pick_model "field photo checks" llm_vision_endpoint "$LLM_VISION" sonnet opus
BUNDLE_VARS+=(--var "warehouse_id=$WAREHOUSE")

sql() {   # one statement on the SQL warehouse; a failure is reported and returned (under set -e it stops the deploy)
  local body; body=$(python3 -c 'import json,sys; print(json.dumps({"warehouse_id": sys.argv[1], "statement": sys.argv[2], "wait_timeout": "50s"}))' "$WAREHOUSE" "$1")
  local st; st=$(D api post /api/2.0/sql/statements --json "$body" -o json | json "d['status']['state'] + ' ' + d['status'].get('error', {}).get('message', '')")
  [[ "$st" == SUCCEEDED* ]] || { echo "SQL failed: $1: $st" >&2; return 1; }
}

# ---------------------------------------------------------------- catalog, schemas, volumes
step "catalog $CATALOG, schema $SCHEMA, deployment $NAME"
# A missing catalog is created. The catalogs API needs a metastore storage root; a workspace on Default Storage has none and accepts
# only SQL CREATE CATALOG on serverless compute, which places the catalog on Default Storage. So the API is tried first, then SQL.
if ! D catalogs get "$CATALOG" >/dev/null 2>&1; then
  D catalogs create "$CATALOG" --comment "$MARK" >/dev/null 2>&1 \
    || sql "CREATE CATALOG IF NOT EXISTS \`$CATALOG\` COMMENT '$MARK'" \
    || { echo "Catalog $CATALOG does not exist and could not be created (the reason is above): ask an admin for CREATE CATALOG, create it in Catalog Explorer, or pass an existing catalog with --catalog"; exit 1; }
  echo "created catalog $CATALOG"
  D schemas delete "$CATALOG.default" >/dev/null 2>&1 || true   # the empty schema every new catalog starts with; nothing here uses it
fi
# the medallion and serving schemas; the change-history schemas (<schema>_lb_*) are made by the Lakebase setup once their feed is set up
for s in "$SCHEMA" "${SCHEMA}_serving"; do
  D schemas get "$CATALOG.$s" >/dev/null 2>&1 || { D schemas create "$s" "$CATALOG" --comment "$MARK" >/dev/null; echo "created $CATALOG.$s"; }
done
for v in landing feeds; do
  D volumes read "$CATALOG.$SCHEMA.$v" >/dev/null 2>&1 || { D volumes create "$CATALOG" "$SCHEMA" "$v" MANAGED --comment "$MARK" >/dev/null; echo "created volume $v"; }
done

# ---------------------------------------------------------------- code
step "deploy the bundle"
# a removed (soft-deleted) Lakebase project keeps its name for 7 days, and the bundle cannot then create the project under that name
if D postgres list-projects --show-deleted -o json | json "any(p['name'] == 'projects/$NAME' and p.get('delete_time') for p in d)" | grep -q True; then
  echo "A removed Lakebase project named $NAME is still held by the workspace. Run this command with --destroy to purge it, or deploy under another --name."; exit 1
fi
B deploy

# ---------------------------------------------------------------- data (first deploy, or --rebuild)
if [[ "$MODE" == "auto" ]]; then
  D tables get "$CATALOG.$SCHEMA.gld_asset" >/dev/null 2>&1 && MODE="code" || MODE="rebuild"
fi
if [[ "$MODE" == "rebuild" ]]; then
  # The planning marts read two ML scores, which the ML job writes after training on this pipeline's gold. On a first deploy they do
  # not exist yet, so empty tables with their columns stand in; the ML job overwrites them, and the planning marts are refreshed after it.
  step "placeholders for the ML scores the planning marts read"
  sql "CREATE TABLE IF NOT EXISTS \`$CATALOG\`.\`$SCHEMA\`.ml_zone_load_forecast (zs_id STRING, day DATE, peak_mw_forecast DOUBLE, peak_mw_lower DOUBLE, peak_mw_upper DOUBLE)"
  sql "CREATE TABLE IF NOT EXISTS \`$CATALOG\`.\`$SCHEMA\`.ml_span_risk (span_id STRING, feeder_id STRING, fault_probability DOUBLE, model_version STRING)"
  step "generate the network, source files and history";  B run atlas_setup
  step "medallion (full refresh)";                       B run atlas_medallion --full-refresh-all
  step "medallion settle (the feeder trace can finish after the marts that read it)"; B run atlas_medallion
  # every gold mart, the feature marts included (planning bundles read the customers behind each switch from the trace)
  step "gold rebuild (gold first read the trace while it was still empty)"
  B run atlas_medallion --full-refresh "$(grep -ho '^def gld_[a-z0-9_]*' pipelines/gold.py pipelines/planning.py pipelines/customers.py pipelines/vegetation.py pipelines/finance.py | sed 's/def //' | paste -sd, -)"
  step "ML (train, register, score)";                     B run atlas_ml
  # the planning marts read the ML scores (the zone load forecast and the span risk), which exist only once the ML job has run
  step "planning marts (now with the ML scores)"
  B run atlas_medallion --full-refresh "$(grep -o '^SQL\["gld_plan_[a-z_]*"\]' pipelines/planning.py | sed -E 's/SQL\["(.*)"\]/\1/' | grep -v transmission | paste -sd, -)"
fi
step "Lakebase setup, gold sync into Lakebase, Genie";    B run atlas_serving
step "start the app";                                     B run atlas_app
if [[ "$LIVE" -gt 0 ]]; then
  step "live engine for $LIVE minutes";                   B run atlas_live --params "minutes=$LIVE" --no-wait
fi
URL=$(D apps get "$NAME" -o json | json "d.get('url','')")
echo; echo "Asset Atlas is deployed: $URL"
# the change data feed (Lakebase to the lakehouse) is the one part the workspace can refuse; say so when it did
FEEDS_SET=$(D postgres list-cdf-configs "projects/$NAME/branches/production/databases/databricks-postgres" -o json 2>/dev/null | json "len(d)" 2>/dev/null || echo 0)
if [[ "$FEEDS_SET" -lt 3 ]]; then
  echo "Note: the Lakebase change data feed is not set up ($FEEDS_SET of 3 feeds). The app works, but its records, decisions and live streams"
  echo "are not copied to the lakehouse, so Genie and the medallion do not see them. The feed needs the Lakebase change data feed preview"
  echo "enabled and a catalog with its own managed storage location (Default Storage is not supported). Fix either, then run this command"
  echo "again with --rebuild."
fi
