# single-project Terraform

This configuration covers the **queueing, scheduling and telemetry** plumbing
only. It does **not** manage the three Cloud Run services that make up Hermes.

## What is managed here

| File | Resources | Applied? |
|---|---|---|
| `apis.tf` | `project_service` enablement (15) | yes |
| `worker.tf` | 5 Cloud Tasks queues, `hermes-tasks` invoker SA + its run.invoker binding, the hourly `hermes-discovery-tick` scheduler job, Artifact Registry repo + deployer IAM | yes |
| `telemetry.tf` | BigQuery telemetry dataset, `hermes-llm-cost` log sink and its dataset writer binding | yes |
| `telemetry.tf` | BigQuery connection, GenAI + feedback log sinks, `completions` external table, `gen_ai_client_...` table, `completions_view` | **no — never applied** |
| `storage.tf` | `…-hermes-logs` bucket | **no — never applied** |

The second group is real, intended configuration for the GCS-backed GenAI
completions pipeline, but it has never been applied: the bucket does not exist
and `LOGS_BUCKET_NAME` is unset on the live services, so nothing writes
completions. `terraform plan` will therefore propose creating those resources.
They are cheap and non-destructive; applying them is a deliberate choice to turn
that pipeline on, not a prerequisite for anything currently running.

## What is NOT managed here

**The Cloud Run services are deployed by hand and by CI, not by Terraform:**

- `hermes-api` — public (`--allow-unauthenticated`), serves the web API
- `hermes-web` — public, the Next.js frontend
- `hermes-worker` — private, runs the same image with `WORKER_MODE=1`

Their images come from `.github/workflows/ci.yml` on merge to `main`
(`gcloud run deploy --image …`, with no `--set-env-vars`, so env set by hand is
preserved across deploys). Their env vars, timeouts, scaling and the
`cpu-throttling` annotation were all set with `gcloud` and exist **only** on the
running revisions — this repo is not the source of truth for them. See
`docs/RUNNING.md` for the env contract each service needs.

The runtime service account (`hermes-runtime@`) and its role bindings are also
managed by hand.

## History

`service.tf`, `service_outputs.tf` and `iam.tf` were deleted in the Phase 0
cleanup. They defined a Cloud Run service named `hermes` (from
`var.project_name`), a `db-custom-1-3840` Cloud SQL instance, and a `hermes-app`
service account with a broad role bundle. **None of the three ever existed** —
`gcloud run services list` shows only the `hermes-api`/`-web`/`-worker` trio,
`gcloud sql instances list` returns nothing, and the service accounts in the
project are `hermes-runtime@`, `hermes-tasks@`, `github-deployer@`,
`firebase-adminsdk-…` and the default compute SA.

Running `terraform apply` with those files present would have created a fourth,
unused Cloud Run service plus a ~$50/month idle Postgres instance, and a sixth
service account. They were also a live source of confusion: an outside review
cited `service.tf`'s `max_instance_count = 10` and
`max_instance_request_concurrency = 8` as the running configuration, when
`hermes-api` actually runs `maxScale=20` / `containerConcurrency=80`.

`data "google_project" "project"` moved from `iam.tf` to `providers.tf`, and the
default `google` provider block moved from `storage.tf` to `providers.tf`. The
Terraform address of the data source is unchanged, so state is unaffected.

## State

State is **local** (`terraform.tfstate`, gitignored) — there is no remote
backend. Whoever runs `apply` needs the current state file; it is not shared.
Migrating to a GCS backend is worth doing before more than one person touches
this.
