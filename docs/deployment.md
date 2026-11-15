# Deployment guide

One command installs a complete, independent copy of Asset Atlas into a Databricks workspace. A second command removes it. This
guide explains what you need, what the deploy does, how it chooses its AI models, and what to do when a workspace refuses something.

## What you need

**On your machine**
- The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install), version 0.283 or later.
- python3 (used to read the CLI's JSON output; no extra packages).
- A browser, for the one-time sign-in to each workspace.

**In the workspace**
- Unity Catalog, serverless compute (jobs, pipelines and SQL), Databricks Apps and Lakebase (Autoscaling projects).
- The Foundation Model API with at least one Claude model you are allowed to call (the deploy finds the newest one; see
  [Choosing the AI models](#choosing-the-ai-models)).
- Permission to create schemas, volumes, jobs, pipelines, an app, a Lakebase project and a Genie space.
- CREATE CATALOG on the metastore, only if the catalog you name does not exist yet.

**Optional**
- The Lakebase change data feed preview, and a catalog with its own managed storage location (not Default Storage). With both, the
  app's records are copied back into the lakehouse for Genie and the pipeline. Without them everything else works; see
  [The change data feed](#the-change-data-feed).

## Deploy

```bash
./deploy.sh --host https://<workspace-url> --catalog <catalog> [--schema <schema>]
```

Paste the workspace address as your browser shows it; a trailing `/?o=<workspace id>` is fine. The first time you deploy to a workspace,
the script opens a browser sign-in and saves a CLI profile for it. Later runs reuse that profile.

### Options

| Option | Default | What it does |
|---|---|---|
| `--host <url>` | required | The workspace address. |
| `--catalog <name>` | required | The Unity Catalog catalog. It is created if it does not exist. |
| `--schema <name>` | `atlas_electricity` | The medallion schema. `<schema>_serving` is created beside it. |
| `--name <name>` | from the schema, for example `atlas-electricity` | The deployment name: the app, the Lakebase project and the job names. |
| `--warehouse-id <id>` | the first serverless SQL warehouse | The warehouse Genie runs on. One is created if the workspace has none. |
| `--profile <name>` | the profile saved for `--host` | A Databricks CLI profile to use instead of signing in. |
| `--llm <endpoint>` | newest Claude Haiku that answers | The model for SMS drafts and the site-view speech. |
| `--llm-plan <endpoint>` | newest Claude Opus that answers | The model for AI-aided planning, notices and work packages. |
| `--llm-vision <endpoint>` | newest Claude Sonnet that answers | The model for the field photo checks. |
| `--code-only` | when the data exists | Redeploy the code and restart the app without rebuilding the data. |
| `--rebuild` | | Regenerate the data and rebuild everything even if it exists. |
| `--live <minutes>` | | Start the live engine for this many minutes after deploying. |
| `--destroy` | | Remove the deployment; the same as `./cleanup.sh`. |

`./deploy.sh --help` prints the same list.

### What the first deploy does

The first deploy takes about 35 minutes. It prints a line as each step starts:

| Step | About | What happens |
|---|---|---|
| Sign in | under 1 min | The CLI profile for the workspace is found or created. |
| Models and warehouse | under 1 min | The three AI models are chosen and the SQL warehouse is found or created. |
| Catalog, schemas, volumes | under 1 min | The catalog (if missing), `<schema>`, `<schema>_serving` and the `landing` and `feeds` volumes. |
| Deploy the bundle | 1 min | The jobs, the pipeline, the Lakebase project and the app, from `databricks.yml`. |
| Generate | 3 min | The synthetic network and every source system, written to the landing volume in native formats. |
| Medallion | 12 min | A full refresh of bronze, silver and gold, a settle run, and a gold rebuild. |
| ML | 2 min | The models are trained, registered in Unity Catalog and scored. |
| Planning marts | 1 min | The planning marts are refreshed with the ML scores. |
| Lakebase, sync, Genie | 10 min | Postgres schemas and tables, the gold tables synced into Lakebase, the change data feed, and the Genie space. |
| Start the app | 2 min | The app is deployed and started. Its address is printed at the end. |

Running the command again when the data already exists only redeploys the code, runs the Lakebase step and restarts the app, in about
6 minutes.

### What it creates

Everything is named after the deployment, so several deployments can share a workspace or a catalog.

| Where | What |
|---|---|
| Unity Catalog | `<schema>`: about 175 tables (bronze, silver, gold, ML scores), the `landing` and `feeds` volumes and two registered models. `<schema>_serving`: the gold tables synced into Lakebase. `<schema>_lb_atlas`, `_lb_rm`, `_lb_live`: the change history, only when the change data feed is set up. |
| Lakebase | A project named after the deployment, with the schemas `atlas`, `rm`, `app`, `live`, `media` and the synced gold. |
| Jobs and pipeline | Five jobs (setup, public feeds, ML, live engine, serving) and the medallion pipeline, all serverless. |
| App | The Databricks App, with its service principal granted what it needs. |
| Genie | A Genie space titled `Asset Atlas, Electricity (<name>)`. |
| ML | An MLflow experiment in your user folder. |
| Warehouse | A small serverless SQL warehouse named after the deployment, only if the workspace had none. |

Every catalog and schema the deploy creates carries the comment `Created by the Asset Atlas deploy (<name>)`. The cleanup removes
only those.

## Choosing the AI models

The app uses three models, one for each role. The deploy chooses them once and fixes them in the app's configuration, so the app never
decides at the moment a feature is used.

| Role | Preferred family | Then |
|---|---|---|
| SMS drafts and site-view speech | Claude Haiku | Claude Sonnet |
| AI-aided planning, notices and work packages | Claude Opus | Claude Sonnet |
| Field photo checks | Claude Sonnet | Claude Opus |

For each role the deploy:
1. lists the Claude endpoints the workspace serves (`databricks-claude-<family>-<version>`, version 4 or later);
2. orders each family newest first;
3. sends each one a one-line request, in that order, and uses the first that answers.

It asks rather than only looking the model up, because a workspace can list a model it then refuses: the newest models can need
account approval. The deploy prints its choice for each role, and any newer models it skipped:

```
model for AI-aided planning, notices and work packages: databricks-claude-opus-5 (newer, not available to this workspace: opus-5-5)
```

To use a specific endpoint, pass `--llm`, `--llm-plan` or `--llm-vision`. If that endpoint does not answer, the deploy says so and uses
the newest one that does. When a newer model becomes available to the workspace, run the deploy command again to switch to it.

## The catalog and Default Storage

- **The catalog exists:** it is used as it is.
- **The catalog is missing:** the deploy creates it.
  - It tries the catalogs API first, which needs the metastore to have a storage root.
  - A workspace on Default Storage has no storage root. There the deploy uses SQL `CREATE CATALOG` on the serverless warehouse
    instead, which places the catalog on Default Storage.
  - The empty `default` schema that every new catalog starts with is removed, since nothing uses it.
- **Neither route works:** the deploy stops with the workspace's own error. This is usually missing CREATE CATALOG on the metastore.

## The change data feed

The [Lakebase change data feed](https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync) copies every change to the app's
records in Lakebase (schemas `atlas`, `rm` and `live`) into the lakehouse, one `lb_<table>_history` table each.

**What uses it:**
- the pipeline's live bronze tables and its current-state silver tables (`slv_app_*`);
- the gold marts built on those;
- the Genie space's questions about decisions, field reports, notices and vegetation work.

The feed needs the preview enabled, and a destination catalog with its own managed storage location; Default Storage is not supported.

When the workspace refuses the feed:
- the Lakebase step reports it and carries on;
- the change-history schemas are not created;
- the deploy ends with a note.

The app works fully, because it reads and writes Lakebase directly. Only the lakehouse copy of its records is missing, so the pipeline
reads those history tables as empty. To add the feed later, enable the preview or move to a catalog on an external location, then run
the deploy with `--rebuild`.

## Running more than one deployment

- Use a different `--schema` (or `--name`) for each deployment in the same workspace; each gets its own schemas, Lakebase project,
  jobs, app and Genie space.
- Only one deploy or cleanup runs at a time from a folder. The bundle keeps its local state per folder, so the script refuses to start
  while another run holds the folder. To deploy to two workspaces at once, use two copies of the folder.

## Removing a deployment

```bash
./cleanup.sh --host https://<workspace-url> --catalog <catalog> [--schema <schema>] [--name <name>]
```

Pass the same options as the deploy. The script asks you to type the deployment name, then removes, in order:
1. the synced tables, the change data feeds and the Genie space;
2. the bundle: app, jobs, pipeline, Lakebase project and its workspace files;
3. anything of those the bundle no longer tracked, found by the deployment's names;
4. the Lakebase project, purged so the name can be used again at once;
5. the ML experiment, and the deployment's schemas with their tables, volumes and models;
6. the catalog, if this deployment created it and nothing else is in it (`--keep-catalog` keeps it);
7. the SQL warehouse, if the deploy created one.

It needs no local state, so it works from any copy of the folder and on a deployment that stopped part way. `--yes` skips the
confirmation. It ends by listing anything it could not remove.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| `Catalog ... could not be created` with `does not have CREATE CATALOG` | You cannot create catalogs in this metastore. | Ask an admin for CREATE CATALOG, or pass an existing catalog with `--catalog`. |
| `Note: the Lakebase change data feed is not set up` | The preview is off, or the catalog is on Default Storage. | Optional. See [The change data feed](#the-change-data-feed). |
| `model for ...: ... (newer, not available to this workspace: ...)` | The workspace has not been approved for the newest models. | Nothing to do; the deploy used the newest that answers. Run it again once access is approved. |
| `No Claude model answers in this workspace` | The Foundation Model API is off, or no Claude model is allowed. | Enable it or request model access, or pass an endpoint that answers with `--llm`, `--llm-plan` and `--llm-vision`. |
| `A removed Lakebase project named ... is still held` | A project of that name was soft-deleted recently; the name is reserved for 7 days. | Run `./cleanup.sh` with the same options (it purges it), or deploy under another `--name`. |
| `Another deploy or cleanup is running from this folder` | A second run started while the first holds the folder's state. | Wait for the first, or run from a separate copy of the folder. |
| The setup job fails with `[Errno 5] Input/output error` on a `/Workspace/...` file, and the run output shows `403 Forbidden ... Server Side Encryption with Customer provided keys` | The workspace's serverless compute cannot read its own workspace files. This is a fault in the workspace, not in the code; it hits random files on each run. | Check it with any small serverless job that reads a workspace file. Use another workspace, or raise it with Databricks support. |
| Every run asks you to sign in again | An older version of the script did not match an address ending in `/?o=...` to the saved profile. | Fixed: the script keeps only `https://<host>`. |
