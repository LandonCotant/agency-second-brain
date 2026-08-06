# Operations Base — Seed Data

Initial rows for the configuration tables. Created manually by the operator after building the base per [`docs/runbooks/airtable_operations_base_setup.md`](../docs/runbooks/airtable_operations_base_setup.md).

## Service Catalog (8 rows)

Build these in order. Each row is one service offering; Projects link to one of them via `Projects.Service`.

### 1. Vantage Local Platform

| Field | Value |
|---|---|
| Service Name | Vantage Local Platform |
| Service Code | `vantage-local` |
| Category | Platform Subscription |
| Description | SaaS that analyzes a client's CRM and sales data and drafts personalized campaigns using Vertex AI. Local-tier — for single-location and small-business clients. Self-serve with email support. |
| Typical Engagement Length | Ongoing |
| Default Phase Sequence | Onboarding → Active → Renewal Window |
| Default Deliverables | Platform access · Onboarding session · Quarterly check-in |
| Standard Pricing Notes | Monthly subscription. Pricing per Sales/CRM. |
| Active | true |
| Notes | Platform Subscription category — no operational Project tracking required day-to-day. Onboarding may warrant a one-time Project at activation. |

### 2. Vantage Enterprise Platform

| Field | Value |
|---|---|
| Service Name | Vantage Enterprise Platform |
| Service Code | `vantage-enterprise` |
| Category | Platform Subscription |
| Description | SaaS that analyzes a client's CRM and sales data and drafts personalized campaigns using Vertex AI. Enterprise-tier — for multi-location and larger clients. Includes white-glove onboarding and a dedicated CSM. |
| Typical Engagement Length | Ongoing |
| Default Phase Sequence | Onboarding → Active → Quarterly Business Review → Renewal Window |
| Default Deliverables | Platform access · White-glove onboarding · QBRs · Dedicated CSM contact |
| Standard Pricing Notes | Annual contract. Pricing per Sales/CRM. |
| Active | true |
| Notes | Platform Subscription category — but Enterprise typically warrants a recurring Project to track the QBR cadence and CSM check-ins. |

### 3. Local SEO Lead Gen

| Field | Value |
|---|---|
| Service Name | Local SEO Lead Gen |
| Service Code | `local-seo-leadgen` |
| Category | Retainer |
| Description | Local search optimization and lead generation for service-area businesses. Ongoing keyword + GBP optimization, citation building, content production, lead tracking. |
| Typical Engagement Length | 6-12 months |
| Default Phase Sequence | Discovery → Build → Launch → Maintain |
| Default Deliverables | Keyword strategy · GBP optimization · Monthly content · Citation building · Monthly reporting |
| Standard Pricing Notes | Monthly retainer. |
| Active | true |
| Notes | Retainer category — one rolling Project per month per client. |

### 4. Website Design

| Field | Value |
|---|---|
| Service Name | Website Design |
| Service Code | `website-design` |
| Category | Project-Based |
| Description | Custom website design and build. Includes discovery, wireframes, visual design, development, and launch. |
| Typical Engagement Length | 3-6 months |
| Default Phase Sequence | Discovery → Design → Build → Launch → Closeout |
| Default Deliverables | Discovery brief · Wireframes · Visual designs · Built site · Launch checklist · Post-launch handoff |
| Standard Pricing Notes | Fixed-fee project. |
| Active | true |
| Notes | Project-Based category — one Project per Contract. |

### 5. Paid Ads Setup

| Field | Value |
|---|---|
| Service Name | Paid Ads Setup |
| Service Code | `paid-ads-setup` |
| Category | Project-Based |
| Description | Initial paid-ads campaign architecture and launch. Account structure, conversion tracking, audience segmentation, ad creative, launch QA. Hand-off to Paid Ads Management retainer post-launch. |
| Typical Engagement Length | 1-3 months |
| Default Phase Sequence | Discovery → Build → Launch → Closeout |
| Default Deliverables | Account structure · Conversion tracking setup · Audience segments · Initial ad creatives · Launch report |
| Standard Pricing Notes | Fixed-fee project. Paired with Paid Ads Management retainer for ongoing optimization. |
| Active | true |
| Notes | Project-Based — one Project per Contract. Typically followed by a Paid Ads Management contract. |

### 6. Paid Ads Management

| Field | Value |
|---|---|
| Service Name | Paid Ads Management |
| Service Code | `paid-ads-mgmt` |
| Category | Retainer |
| Description | Ongoing paid-ads optimization, A/B testing, budget management, monthly reporting. Continues from Paid Ads Setup. |
| Typical Engagement Length | Ongoing |
| Default Phase Sequence | Maintain (rolling) |
| Default Deliverables | Weekly optimization · Monthly performance report · Quarterly strategy review |
| Standard Pricing Notes | Monthly retainer. |
| Active | true |
| Notes | Retainer category — one rolling Project per month. |

### 7. Marketing Consulting Retainer

| Field | Value |
|---|---|
| Service Name | Marketing Consulting Retainer |
| Service Code | `marketing-consulting` |
| Category | Retainer |
| Description | Strategic marketing consulting on a monthly retainer. Goal-setting, channel strategy, hiring/team support, agency selection, vendor management. |
| Typical Engagement Length | Ongoing |
| Default Phase Sequence | Active (rolling monthly) |
| Default Deliverables | Monthly strategy session · Async Slack support · Quarterly review |
| Standard Pricing Notes | Monthly retainer. |
| Active | true |
| Notes | Retainer category — one rolling Project per month per client (Project name convention: '{Client} — Marketing Consulting — {Mon YYYY}'). |

## Risk Profiles (initial set — extend as patterns emerge)

These are the trouble-signal patterns Risk Watcher (WS-G2) evaluates per segment. Start minimal; add rows as real patterns become clear from operating the agency. Each row maps to a Python evaluator the agent loads at runtime.

### E-commerce segment

| Pattern Name | Severity Default | Threshold Value | Threshold Unit | Window | Description |
|---|---|---|---|---|---|
| Acknowledgment Gap | High | 5 | business days | rolling 7 days | Client hasn't acknowledged a deliverable or message in N business days. |
| Silent After Deliverable | High | 5 | business days | since last delivery | We delivered something to the client and they've gone silent for N days — risk that the deliverable missed the mark. |
| Klaviyo List Decline | Medium | 10 | % | rolling 8 weeks | Client's email list size declined by >N% over the rolling window. |
| ROAS Trend Down | High | 20 | % | rolling 8 weeks | Client's blended ROAS declined by >N% vs the rolling baseline. |

### Local Service segment

| Pattern Name | Severity Default | Threshold Value | Threshold Unit | Window | Description |
|---|---|---|---|---|---|
| Acknowledgment Gap | High | 5 | business days | rolling 7 days | Same as E-commerce. |
| GBP Reviews Dropped | Medium | 2 | count | rolling 30 days | Client's GBP review velocity dropped by N or more reviews/month. |
| Lead Volume Decline | High | 25 | % | rolling 8 weeks | Tracked lead volume declined by >N% vs the rolling baseline. |

### Agency Partner segment

| Pattern Name | Severity Default | Threshold Value | Threshold Unit | Window | Description |
|---|---|---|---|---|---|
| Acknowledgment Gap | High | 5 | business days | rolling 7 days | Same as E-commerce. |
| White-label Hand-off Failed | Critical | 0 | — | per delivery | A white-label deliverable was returned for revision more than once — partner unhappy. |
| Stakeholder Change | Medium | 0 | — | per detection | New decision-maker detected at the partner's end (signature change, "John reports to Sarah now," etc.). |

For each row, set `Active = true`. Add `Notes` later as you gather context on specific clients triggering the pattern.

## Team (the operator + the operator)

Two starting rows. Add team members as you hire.

### Row 1: the operator (placeholder if the operator is a separate person)

| Field | Value |
|---|---|
| Name | the operator |
| Workspace Email | brian@example.com (placeholder — update with actual) |
| Role | Leadership |
| Active | true |
| Capacity Hours per Week | 40 |
| Slack Handle | (fill in) |
| Notes | Co-founder |

### Row 2: the operator

| Field | Value |
|---|---|
| Name | the implementer |
| Workspace Email | owner@example.com |
| Role | Leadership |
| Active | true |
| Capacity Hours per Week | 40 |
| Slack Handle | (fill in) |
| Notes | Co-founder, Brain build owner |

If "the operator" in the PRD is just the placeholder name for the operator (single-founder), delete Row 1 and update PRD §1 / §11 references accordingly.

## Goals (initial set — populate during goal intake)

Goals are populated during the Goal Steward intake session (PRD §5.6 week 1). The schema supports up to 5 horizons; expect ~3-5 goals per horizon for a 2-person agency.

Recommended initial set (placeholder — replace during intake):

- 1× 10-year goal (Vision-level — "Build a $10M agency that runs without us")
- 2-3× 3-year goals (Strategic outcomes — "Become the local-business AI platform of choice", "$5M ARR")
- 4-6× 1-year goals (Annual targets — broken down by Area of Focus: Revenue, Team, Product)
- 6-10× Quarterly goals (Current quarter — concrete commitments)
- Weekly goals are managed week-to-week; not seeded here

Don't bulk-create goals you don't actually believe in — empty goals dilute the signal Goal Steward provides. Better to start with 5-7 real ones and add as new commitments emerge.

## Goal Scores (no seed)

Goal Scores are written weekly by the Goal Steward agent (WS-G6) once it ships. No seed rows.

## Clients (no seed)

Clients are added as engagements activate. Each row mirrors a CRM Account by `recXXX...` ID. The first row to add: pick one or two existing CRM Accounts you want the Brain to actively monitor, get their `recXXX...` from the CRM URL, and create the corresponding Operations Clients row.
