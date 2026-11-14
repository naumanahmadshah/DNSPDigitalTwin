# Shared by deploy.sh and cleanup.sh (sourced, not run): the option defaults, the deployment's names, the sign-in to the workspace,
# the one-run-per-folder lock and the CLI helpers. Both scripts set their options, then call atlas_init.

HOST="" CATALOG="" SCHEMA="atlas_electricity" NAME="" PROFILE=""

# The workspace address as scheme and host only: an address copied from the browser may carry /?o=<workspace id>.
norm_host() { echo "$1" | sed -E 's#^(https?://[^/?#]+).*#\1#'; }

step() { echo; echo "=== $(date '+%H:%M:%S') $*"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
D() { databricks "$@" -p "$PROFILE"; }
B() { databricks bundle "$@" -p "$PROFILE" "${BUNDLE_VARS[@]}"; }

atlas_init() {
  [[ -n "$HOST" && -n "$CATALOG" ]] || { echo "--host and --catalog are required (see --help)"; exit 2; }
  [[ "$SCHEMA" =~ ^[a-z][a-z0-9_]{0,40}$ ]] || { echo "--schema must be lower case letters, digits and underscores"; exit 2; }
  # The deployment name (app, Lakebase project, jobs, pipeline) comes from the schema unless given.
  if [[ -z "$NAME" ]]; then
    NAME="${SCHEMA//_/-}"; [[ "$NAME" == atlas* ]] || NAME="atlas-$NAME"
  fi
  NAME="${NAME:0:30}"; NAME="${NAME%-}"
  [[ "$NAME" =~ ^[a-z][a-z0-9-]{1,29}$ ]] || { echo "--name must be lower case letters, digits and hyphens, at most 30"; exit 2; }
  command -v databricks >/dev/null || { echo "Install the Databricks CLI first: https://docs.databricks.com/dev-tools/cli/install"; exit 1; }
  command -v python3 >/dev/null || { echo "python3 is needed (to read CLI output)"; exit 1; }

  # Sign-in: the CLI profile already saved for this host, or a new one created with a browser login.
  if [[ -z "$PROFILE" ]]; then
    PROFILE=$(python3 - "$HOST" <<'EOF'
import configparser, os, sys
c = configparser.ConfigParser(); c.read(os.path.expanduser("~/.databrickscfg"))
print(next((s for s in c.sections() if c[s].get("host", "").rstrip("/") == sys.argv[1]), ""))
EOF
)
    if [[ -z "$PROFILE" ]]; then
      PROFILE="atlas-$(echo "$HOST" | sed -E 's#https?://##; s#[^a-z0-9]+#-#g' | cut -c1-40)"
      step "sign in to $HOST (profile $PROFILE)"
      databricks auth login --host "$HOST" --profile "$PROFILE"
    fi
  fi
  D current-user me >/dev/null || { echo "Cannot authenticate with profile $PROFILE"; exit 1; }

  # One run at a time from this folder: the bundle keeps one local state per folder, and a second run started alongside would clear
  # and rewrite the state the first is using.
  mkdir -p .databricks
  LOCK=.databricks/deploy.pid
  if [[ -f $LOCK ]] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
    echo "Another deploy or cleanup is running from this folder (process $(cat "$LOCK")). Wait for it to finish, or run from a separate copy of the folder."; exit 1
  fi
  echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT
  # The local state cache is per folder, not per deployment: clear it so every command reads this deployment's own state from its
  # workspace root (~/.bundle/<bundle>/<name>); otherwise a second deployment from the same folder would act on the first one.
  rm -rf .databricks/bundle

  BUNDLE_VARS=(--var "catalog=$CATALOG" --var "schema=$SCHEMA" --var "name=$NAME")
  MARK="Created by the Asset Atlas deploy ($NAME)"   # the comment on every catalog and schema a deployment creates
  SCHEMAS=("$SCHEMA" "${SCHEMA}_serving" "${SCHEMA}_lb_atlas" "${SCHEMA}_lb_rm" "${SCHEMA}_lb_live")
}
