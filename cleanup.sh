#!/usr/bin/env bash
# Remove an Asset Atlas deployment from a Databricks workspace: everything deploy.sh created for it, and nothing else. It works from any
# machine with this folder (it needs no local state) and on a deployment that only got part way.
#
#   ./cleanup.sh --host https://<workspace> --catalog <catalog> [--schema <schema>] [options]
#
# Pass the same --host, --catalog, --schema (and --name, if you set one) as the deploy. It removes, in order:
#   the synced tables (gold in Lakebase), the change data feeds, the Genie space, the bundle (app, jobs, pipeline, Lakebase project and
#   its workspace files), anything of those the bundle no longer tracked (found by the deployment's names), the Lakebase project
#   (purged, so the name can be used again at once), the ML experiment, the deployment's schemas with their tables, volumes and models,
#   the catalog if this deployment created it and nothing else is in it, and the SQL warehouse if this deployment created it.
#
# Options
#   --schema <s>    medallion schema of the deployment (default atlas_electricity)
#   --name <n>      deployment name, if the deploy was given one (default from the schema, e.g. atlas-electricity)
#   --profile <p>   Databricks CLI profile (default: the profile for --host, created with an OAuth login if needed)
#   --keep-catalog  keep the catalog even if this deployment created it
#   --yes           do not ask for confirmation
set -euo pipefail
cd "$(dirname "$0")"
source ./deploy_lib.sh

KEEP_CATALOG=0 YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST=$(norm_host "$2"); shift 2 ;;
    --catalog) CATALOG="$2"; shift 2 ;;
    --schema) SCHEMA="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --keep-catalog) KEEP_CATALOG=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 2 ;;
  esac
done
atlas_init
BUNDLE_VARS+=(--var "warehouse_id=unused")   # the bundle requires it; removing the bundle does not touch the warehouse

if [[ $YES == 0 ]]; then
  echo "This removes deployment $NAME from $HOST: its app, jobs, pipeline, Lakebase project, Genie space, ML experiment, and the schemas"
  echo "it created in $CATALOG with all their data (the catalog too, if this deployment created it and nothing else is in it)."
  read -r -p "Type the deployment name to confirm: " ok
  [[ "$ok" == "$NAME" ]] || { echo "Not confirmed; nothing was removed."; exit 1; }
fi
LEFT=()   # anything that could not be removed, reported at the end
gone() { echo "removed $*"; }
fail() { LEFT+=("$*"); echo "could not remove $*"; }
DB="projects/$NAME/branches/production/databases/databricks-postgres"

step "synced tables (gold in Lakebase)"
for t in $(D tables list "$CATALOG" "${SCHEMA}_serving" -o json 2>/dev/null | json "' '.join(t['full_name'] for t in d)" 2>/dev/null); do
  D postgres delete-synced-table "synced_tables/$t" >/dev/null 2>&1 && gone "$t" || true   # a plain table here is dropped with its schema
done

step "change data feeds"
for c in $(D postgres list-cdf-configs "$DB" -o json 2>/dev/null | json "' '.join(c['name'] for c in d)" 2>/dev/null); do
  D postgres delete-cdf-config "$c" >/dev/null 2>&1 && gone "${c##*/}" || fail "change data feed $c"
done

step "Genie space"
for g in $(D api get /api/2.0/genie/spaces -o json 2>/dev/null | json "' '.join(x['space_id'] for x in d.get('spaces', []) if x.get('title') == 'Asset Atlas, Electricity ($NAME)')" 2>/dev/null); do
  D api delete "/api/2.0/genie/spaces/$g" >/dev/null 2>&1 && gone "Genie space $g" || fail "Genie space $g"
done

step "bundle (app, jobs, pipeline, Lakebase project, workspace files)"
B destroy --auto-approve || echo "The bundle could not be removed as a whole; what is left is removed by name below."

step "anything left, by the deployment's names"
if D apps get "$NAME" >/dev/null 2>&1; then
  # the app may already be on its way out (removing the bundle starts that): ask once, then wait up to three minutes for it to go
  D apps delete "$NAME" >/dev/null 2>&1 || true
  for _ in $(seq 36); do D apps get "$NAME" >/dev/null 2>&1 || break; sleep 5; done
  D apps get "$NAME" >/dev/null 2>&1 && fail "app $NAME" || gone "app $NAME"
fi
for j in $(D jobs list -o json 2>/dev/null | json "' '.join(str(j['job_id']) for j in d if j.get('settings', {}).get('name', '').startswith('$NAME | '))" 2>/dev/null); do
  D jobs delete "$j" >/dev/null 2>&1 && gone "job $j" || fail "job $j"
done
for p in $(D pipelines list-pipelines -o json 2>/dev/null | json "' '.join(p['pipeline_id'] for p in d if p.get('name', '').startswith('$NAME | '))" 2>/dev/null); do
  D pipelines delete "$p" >/dev/null 2>&1 && gone "pipeline $p" || fail "pipeline $p"
done
ME=$(D current-user me -o json | json "d['userName']")
ROOT="/Workspace/Users/$ME/.bundle/electricity-asset-atlas/$NAME"
if D workspace get-status "$ROOT" >/dev/null 2>&1; then
  D workspace delete "$ROOT" --recursive >/dev/null 2>&1 && gone "workspace files $ROOT" || fail "workspace files $ROOT"
fi

step "Lakebase project"
# Removing the bundle only soft-deletes the project, which keeps its name taken for 7 days; purge it, whether active or soft-deleted.
if D postgres list-projects --show-deleted -o json 2>/dev/null | json "'projects/$NAME' in [p['name'] for p in d]" 2>/dev/null | grep -q True; then
  D postgres delete-project "projects/$NAME" --purge >/dev/null 2>&1 && gone "Lakebase project $NAME (purged)" || fail "Lakebase project $NAME"
fi

step "ML experiment"
EXP=$(D experiments get-by-name "/Users/$ME/atlas-models-$CATALOG-$SCHEMA" -o json 2>/dev/null | json "d['experiment']['experiment_id']" 2>/dev/null || true)
if [[ -n "$EXP" ]]; then
  D experiments delete-experiment "$EXP" >/dev/null 2>&1 && gone "experiment /Users/$ME/atlas-models-$CATALOG-$SCHEMA" || fail "experiment $EXP"
fi

step "schemas created by this deployment (with their tables, volumes and models)"
for s in "${SCHEMAS[@]}"; do
  c=$(D schemas get "$CATALOG.$s" -o json 2>/dev/null | json "d.get('comment', '')" 2>/dev/null || true)
  if [[ "$c" == "$MARK"* ]]; then
    D schemas delete "$CATALOG.$s" --force >/dev/null 2>&1 && gone "schema $CATALOG.$s" || fail "schema $CATALOG.$s"
  elif [[ -n "$c" ]] || D schemas get "$CATALOG.$s" >/dev/null 2>&1; then
    echo "kept $CATALOG.$s: it was not created by this deployment"
  fi
done

step "catalog"
c=$(D catalogs get "$CATALOG" -o json 2>/dev/null | json "d.get('comment', '')" 2>/dev/null || true)
if [[ "$c" == "$MARK" ]]; then
  others=$(D schemas list "$CATALOG" -o json 2>/dev/null | json "' '.join(s['name'] for s in d if s['name'] not in ('default', 'information_schema'))" 2>/dev/null || echo "?")
  if [[ $KEEP_CATALOG == 1 ]]; then
    echo "kept catalog $CATALOG (--keep-catalog)"
  elif [[ -n "$others" ]]; then
    echo "kept catalog $CATALOG: it holds other schemas ($others)"
  else
    D catalogs delete "$CATALOG" --force >/dev/null 2>&1 && gone "catalog $CATALOG" || fail "catalog $CATALOG"
  fi
else
  echo "kept catalog $CATALOG: it was not created by this deployment"
fi

step "SQL warehouse"
# deploy.sh creates a warehouse only when the workspace has no serverless one, and names it after the deployment
for w in $(D warehouses list -o json 2>/dev/null | json "' '.join(w['id'] for w in d if w.get('name') == '$NAME')" 2>/dev/null); do
  D warehouses delete "$w" >/dev/null 2>&1 && gone "SQL warehouse $NAME ($w)" || fail "SQL warehouse $w"
done

echo
if [[ ${#LEFT[@]} -eq 0 ]]; then
  echo "Deployment $NAME is removed."
else
  echo "Deployment $NAME is removed except for ${#LEFT[@]} item(s); run this command again, or remove them in the workspace:"
  printf '  %s\n' "${LEFT[@]}"; exit 1
fi
