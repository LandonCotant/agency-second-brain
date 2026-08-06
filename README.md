# Agency Second Brain

A multi-agent operations system on Google Cloud, built for a lean digital-marketing agency.
It triages inbound work, watches client-risk signals, and drafts daily briefs — **drafts only**,
never send/publish on its own.

**Start here:** open the [case study](marketing/case-study.html) (one-page PDF-style writeup).

This repository is a **sanitized portfolio copy** of a production system. Real project IDs,
Workspace domains, Drive folder IDs, Airtable base IDs, and operator identity are replaced with
placeholders. The architecture, agents, Terraform, ADRs, and tests are real.

## What it does

| Capability | How |
|---|---|
| Inbound triage | Classifies signals and drafts the next action for human approval |
| Client risk watching | Daily segment-specific churn / disengagement scoring |
| Institutional memory | Extracts commitments, facts, and relationships into BigQuery |
| Knowledge surfacing | Hybrid search + MCP tools so the corpus answers in plain English |
| Morning brief / evening reflection | Ranked daily synthesis off the founder's plate |

## Design invariants (interview-relevant)

- **Drafts-only boundary** — agents prepare Gmail drafts / Airtable suggestions; humans approve.
- **Sensitive-account isolation** — regulated / sensitive accounts are excluded at the CRM sync
  boundary via a cascade filter (lookup fields + `filterByFormula`), not bolted on later.
- **Least-privilege service accounts** — custom roles only; CI checks block predefined high-privilege roles.
- **Cost guardrails** — ~$50/mo budget posture with automated spend audits.
- **Decision trail** — 70+ ADRs in [`docs/adr/`](docs/adr/INDEX.md).

## Stack

Python 3.12 · Vertex AI / Gemini · Cloud Run Jobs · BigQuery · Terraform · Airtable replica ·
Cloudflare Workers (remote MCP) · pre-commit (ruff, tflint, secrets baseline)

## Repo layout

```
src/agency_brain/     # agents, sync, routing, audit, MCP server
terraform/            # foundation, agent_runtime, data_pipeline, security, observability
airtable/schema.json  # redacted Operations base shape
docs/adr/             # architecture decision records
docs/runbooks/        # curated operational docs (placeholders only)
marketing/            # recruiter-facing case study
tests/                # unit + security tests (should pass locally)
```

## Local setup

Requires Python 3.12, Terraform >= 1.7, and (for lint) tflint.

```bash
make install        # venv + pre-commit + dev deps
make test           # pytest
make lint           # pre-commit hooks (optional locally)
```

Copy `terraform/envs/prod/terraform.tfvars.example` → `terraform.tfvars` and fill placeholders
before any plan/apply against **your own** GCP project. Do not treat this repo as a deploy kit
for someone else's cloud.

## License

MIT — see [LICENSE](LICENSE). This is a portfolio demonstration derived from a private production
system; client data and live credentials are not included.
