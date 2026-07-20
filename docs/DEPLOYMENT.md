# Deployment Guide

This covers deploying, replicating, updating, and tearing down the stack with
AWS SAM — the "infrastructure as code, one-command deploy" part of the project.

- [Prerequisites](#prerequisites)
- [What SAM does](#what-sam-does)
- [First deploy (guided)](#first-deploy-guided)
- [Subsequent deploys](#subsequent-deploys)
- [Replicating environments (dev / staging / prod)](#replicating-environments)
- [Finding your endpoints after deploy](#finding-your-endpoints-after-deploy)
- [Updating the stack](#updating-the-stack)
- [Tearing it down](#tearing-it-down)
- [CI/CD](#cicd)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| AWS account + credentials | Target for the stack | `aws configure` |
| AWS SAM CLI | Build & deploy | [docs](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) |
| Docker | Only for `sam build` container mode & `sam local` | [docs](https://docs.docker.com/get-docker/) |
| Python 3.12 | Runtime the functions target | python.org |

Your AWS identity needs permission to create the resources in `template.yaml`
(Lambda, API Gateway, DynamoDB, IAM roles, CloudWatch). For a personal account
that's typically administrator access; for shared accounts, scope a deploy role.

---

## What SAM does

`sam build` transforms this project into deployable artifacts:
1. Installs each function's dependencies (there are none beyond the runtime).
2. Builds the shared layer (`layers/common`) into the layout Lambda expects.
3. Writes everything to `.aws-sam/build/`.

`sam deploy` then:
1. Packages the build artifacts and uploads them to an S3 bucket SAM manages
   for you (`resolve_s3 = true`).
2. Expands the SAM `Transform` into full CloudFormation.
3. Creates/updates the CloudFormation **stack** — Lambda functions, the API,
   DynamoDB tables, IAM roles, the CloudWatch dashboard and alarm — as one
   atomic changeset.

Because the stack *is* the deployment, the whole environment is created,
updated, or destroyed as a single unit. That's what makes it reproducible.

---

## First deploy (guided)

```bash
sam build
sam deploy --guided
```
The guided prompts ask for a stack name, region, the `Environment` parameter,
and permission to create IAM roles. Your answers are saved to `samconfig.toml`
so you never have to repeat them.

Recommended answers for a first run:
- **Stack Name**: `url-shortener-dev`
- **Region**: `us-east-1` (or your preference)
- **Parameter Environment**: `dev`
- **Confirm changes before deploy**: `Y`
- **Allow SAM CLI IAM role creation**: `Y`
- **Save arguments to configuration file**: `Y`

---

## Subsequent deploys

Once `samconfig.toml` exists, deploying is a single command:
```bash
sam build && sam deploy
```
Or via the Makefile:
```bash
make deploy            # ENV defaults to dev
make deploy ENV=prod
```

---

## Replicating environments

`samconfig.toml` defines three named configurations. Each is a fully isolated
stack (its own tables, functions, API, dashboard) selected with `--config-env`:

```bash
sam deploy --config-env dev        # stack: url-shortener-dev
sam deploy --config-env staging    # stack: url-shortener-staging
sam deploy --config-env prod       # stack: url-shortener-prod
```

The **same build artifacts** deploy to every environment; only the
`Environment` parameter (and any overrides, e.g. `ClickRetentionDays=365` in
prod) differs. Resource names are suffixed with the environment
(`url-shortener-urls-prod`, etc.) so environments never collide, even in the
same account and region.

To stand up an environment in a **different account or region**, point your
credentials/`--region` there and run the same command — nothing in the code
changes. That is the "environment replication" property in one sentence.

---

## Finding your endpoints after deploy

SAM prints the stack Outputs at the end of every deploy. To fetch them again:
```bash
make outputs ENV=dev
# or
aws cloudformation describe-stacks --stack-name url-shortener-dev \
  --query 'Stacks[0].Outputs' --output table
```
Outputs include:
- `ApiBaseUrl` — base for all API calls and short links.
- `CreateUrlEndpoint` — `POST` here to create links.
- `DashboardUrl` — the CloudWatch dashboard.
- `UrlsTableName` / `ClicksTableName` — the DynamoDB tables.

---

## Updating the stack

Edit code or `template.yaml`, then redeploy — CloudFormation computes and
applies only the diff:
```bash
sam build && sam deploy
```
- Changing function code → new Lambda version, no downtime.
- Adding a resource → created and wired in.
- Removing a resource from the template → **deleted** on next deploy (mind
  stateful resources; see below).

> ⚠️ **Stateful resources.** The DynamoDB tables hold your data. Renaming them
> in the template, or deleting the stack, destroys that data. `UrlsTable` has
> point-in-time recovery enabled for safety, but treat table changes with care
> and take a backup first in prod.

---

## Tearing it down

```bash
make delete ENV=dev
# or
sam delete --stack-name url-shortener-dev
```
This removes the entire stack — functions, API, tables, dashboard, alarm, and
IAM roles. Because everything was created by CloudFormation, nothing is left
behind. (The SAM-managed artifact S3 bucket persists for reuse across stacks.)

---

## CI/CD

A minimal pipeline is just the same two commands with credentials from your CI
provider's OIDC role:

```yaml
# sketch — adapt to your CI system
steps:
  - run: pip install -r tests/requirements.txt
  - run: pytest                       # fail fast on logic regressions
  - run: sam build
  - run: sam deploy --config-env staging --no-confirm-changeset
  # ... promote to prod on approval:
  - run: sam deploy --config-env prod --no-confirm-changeset
```
Use per-environment deploy roles and require a manual approval gate before the
prod step. `--no-confirm-changeset` makes the deploy non-interactive.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `sam: command not found` | Install the SAM CLI (see prerequisites). |
| Build fails on the layer | Ensure Python 3.12 is available; try `sam build --use-container` (needs Docker). |
| `CREATE_FAILED` on IAM | Your identity lacks permission to create roles; deploy with an admin/deploy role. |
| Redirect works but country is always `UNKNOWN` | Expected behind a bare API Gateway URL — put CloudFront in front to get `CloudFront-Viewer-Country`. See [`ANALYTICS.md`](ANALYTICS.md#geography). |
| Dashboard shows no data | Metrics appear only after real traffic; generate a few creates/redirects and wait ~1–2 min. |
| Stuck `ROLLBACK_COMPLETE` stack | Delete the stack (`sam delete`) and redeploy; this state can't be updated in place. |
