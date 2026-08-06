# Agency Second Brain — Build Spec

**Version:** 0.1 (working draft)
**Owner:** the operator, the agency
**Status:** Pre-build design, ready for week-1 kickoff

---

## 0. Purpose of This Document

This is the working spec for Agency's internal Proactive Agency Brain — a long-running, multi-agent system that watches the agency's Workspace + Airtable + Vantage data, classifies inbound work using the Rule the Room methodology, surfaces risks before they become churn events, and produces daily/weekly artifacts that move operational synthesis off the team's plates.

It is opinionated. Decisions made here are decisions, not suggestions. Open questions are flagged explicitly in §14.

The document is structured to be read end-to-end once, then used as a reference during implementation. Section numbers are stable so collaborators can be pointed to specific sections.

---

## 1. Executive Summary

**What it is.** A central event bus + classification layer + agent ensemble that runs continuously inside Google Cloud, indexes Workspace and Airtable into a unified semantic layer, classifies every inbound signal using a defined taxonomy, and produces three kinds of outputs: proactive flags (things slipping), daily artifacts (morning brief, evening reflection), and on-demand answers to ad-hoc questions across the corpus.

**Why now.** Agency already runs predictive ML on client data via Vantage. This system applies the same discipline to operational data — the agency's own emails, files, CRM records, and project state. The thesis: the same data infrastructure that makes Vantage work for clients should work for Agency's own operations.

**What it is not.** It is not a chatbot. It is not a Zapier-style automation. It is not a generic RAG over Drive. It is a long-running agent ensemble whose primary job is *proactive monitoring* of structured operational state, with reactive query as a secondary capability.

**Build duration.** 12 weeks, with a working subset shipping at week 5 and capability layers added weekly thereafter. The system is useful from week 5 forward, not at the end.

**Anchoring constraints.**
- HIPAA clients live in a separate GCP project and the Brain must not touch them.
- Strict precision/recall tuning per signal category — high recall for client risk, high precision for synthesis.
- Drafts only, never auto-send for client communications.
- Leadership-view + owner-view notification model — the operator sees everything, team members see what they own.
- Three segment-specific Risk Profiles (e-commerce, local, agency-partner) — not one shared profile.

---

## 2. Architecture Overview

### 2.1 Conceptual data flow

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Source Systems │    │   Sync Layer    │    │  BigQuery (Brain │
│                 │───▶│                 │───▶│   Project)      │
│ • Airtable      │    │ • Application   │    │                 │
│ • Gmail         │    │   Integration   │    │ • Replicated    │
│ • Drive         │    │ • Workspace     │    │   Airtable      │
│ • Calendar      │    │   Connectors    │    │ • Triaged Items │
│ • Google Chat   │    │                 │    │ • Goal Records  │
│ • Vantage data  │    │                 │    │ • Risk Baselines│
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │ Knowledge       │
                                              │ Catalog         │
                                              │ (semantic layer)│
                                              └─────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │ Agent Ensemble  │
                                              │ (Agent Runtime) │
                                              │                 │
                                              │ • Triage        │
                                              │ • Risk Watcher  │
                                              │ • Goal Steward  │
                                              │ • Morning Brief │
                                              │ • Evening Refl. │
                                              │ • Knowledge     │
                                              │   Surfacer      │
                                              └─────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │ Routing Layer   │
                                              │                 │
                                              │ • Severity      │
                                              │ • Owner         │
                                              │ • Channel       │
                                              └─────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │ Delivery        │
                                              │                 │
                                              │ • Gemini Inbox  │
                                              │ • Email digest  │
                                              │ • Google Chat   │
                                              │ • Airtable view │
                                              │ • Gmail drafts  │
                                              └─────────────────┘
```

### 2.2 Component responsibilities

| Component | Responsibility |
|-----------|----------------|
| Source systems | Ground truth. Never written to by the Brain except via drafts. |
| Sync layer | One-way replication from sources into BigQuery on a defined cadence. |
| BigQuery (Brain project) | Canonical operational warehouse. Houses replicated data + agent-generated tables (triaged items, goals, risk baselines, briefs). |
| Knowledge Catalog | Semantic layer over BigQuery + Workspace data stores. Provides MCP-accessible context graph for agents. |
| Agent Runtime | Hosts the long-running agents. Manages Memory Bank state per client/per agent. |
| Routing layer | Application Integration flow that reads agent outputs from BigQuery and routes to delivery channels based on severity, owner, and category. |
| Delivery | The user-facing surfaces. The agents do not push directly — the routing layer does. |

### 2.3 Why this shape

The separation between agents (which classify and reason) and the routing layer (which decides what to do with classifications) is load-bearing. It means tuning notification behavior never requires touching the agents, and tuning agent behavior never breaks notifications. Both can iterate independently.

---

## 3. Airtable Schema

The Airtable schema is the operational spine. Every other component depends on it being right. This is the piece you should be working on now during the build's prep weeks, before any agent is built.

### 3.1 Tables

**Clients**
The root entity. One record per client organization.

| Field | Type | Notes |
|-------|------|-------|
| Client Name | Single-line text | |
| Segment | Single select | E-commerce / Local / Agency Partner |
| Stage | Single select | Onboarding / Active / Mature / Renewal Window / Churned |
| Account Manager | Linked record (Team) | Primary delivery owner |
| Salesperson | Linked record (Team) | Primary commercial owner |
| Engagement Start Date | Date | |
| Engagement End Date | Date | Empty if active |
| HIPAA | Checkbox | If checked, this client is **excluded** from the Brain. Critical. |
| Risk Baseline | Long text (JSON) | Maintained by the Risk Watcher; human-readable record of "what's normal for this client" |
| Vantage Tenant ID | Single-line text | Pointer to the client's data location for cross-reference |
| Last Activity Timestamp | Last modified time | Computed |

**Projects**
A unit of work delivered to a client. A client typically has one active project at a time but can have several.

| Field | Type | Notes |
|-------|------|-------|
| Project Name | Single-line text | |
| Client | Linked record (Clients) | |
| Project Type | Single select | Onboarding / Strategy / Retainer / One-off / Renewal |
| Status | Single select | Not Started / Active / Blocked / Complete / Cancelled |
| Health | Single select | Green / Yellow / Red — set manually OR by Risk Watcher |
| Health Reason | Long text | Required when Health = Yellow or Red |
| Owner | Linked record (Team) | Inherits from Client.Account Manager unless overridden |
| Start Date | Date | |
| Target Completion Date | Date | |
| Actual Completion Date | Date | |
| Scope Document | Linked record (Drive Files) | The signed scope/SOW |
| Expected Files | Multi-select | Standard list per Project Type — used by File Watcher |

**Tasks** (the operational task list)
This is where triaged items become work. Modeled on Rule the Room's task structure.

| Field | Type | Notes |
|-------|------|-------|
| Task Name | Single-line text | |
| Source | Single select | Manual / Triage Agent / Risk Watcher / Goal Steward / Other |
| Source Reference | URL | Link back to the originating Gmail thread / Drive doc / etc. |
| Project | Linked record (Projects) | |
| Client | Linked record (Clients) | Lookup via Project, but stored for query speed |
| Owner | Linked record (Team) | |
| Action Type | Single select | Do It Now / Delegate / Defer / Schedule / Wait |
| Category | Single select | Calls / Computer / Errands / Office / Schedule / Team Meeting / Staff / Waiting For / Home |
| Task Type | Single select | Task / Project (one-step vs multi-step) |
| Wait Reason | Single-line text | Required if Action Type = Wait. Describes who/what we're waiting on. |
| Wait Started | Date | Required if Action Type = Wait. Used for aging. |
| Goal Linked | Linked record (Goals) | If task advances a specific goal |
| Status | Single select | Open / In Progress / Done / Cancelled |
| Created | Created time | |
| Due Date | Date | |
| Completed Date | Date | |

**Goals**
Modeled on Rule the Room's 12-Week Template (Appendix C of the book). Hierarchical: long-horizon goals contain shorter-horizon goals.

| Field | Type | Notes |
|-------|------|-------|
| Goal Name | Single-line text | |
| Owner | Linked record (Team) | |
| Horizon | Single select | 10-year / 3-year / 1-year / Quarterly / Weekly |
| Parent Goal | Linked record (Goals) | Quarterly rolls up to 1-year, 1-year to 3-year, etc. |
| Area of Focus | Single select | Revenue / Team / Product / Personal / Education / Other |
| Description | Long text | What the goal is in human language |
| Tactics | Long text | The "how" — the specific moves planned to achieve it |
| Metrics | Long text | The numeric or observable definition of success |
| Status | Single select | Active / Achieved / Abandoned / Paused |
| Last Reviewed | Date | Updated by Goal Steward weekly |

**Goal Scores** (separate table — one row per goal per week)

| Field | Type | Notes |
|-------|------|-------|
| Goal | Linked record (Goals) | |
| Week Of | Date | Monday of the week being scored |
| Score | Number | 1-10 self-assessment |
| Notes | Long text | Why this score |
| Scorer | Linked record (Team) | |

**Team**
Members of Agency with access to the system.

| Field | Type | Notes |
|-------|------|-------|
| Name | Single-line text | |
| Email | Email | Their Workspace email — used for routing |
| Role | Single select | Leadership / Account Manager / Salesperson / Specialist |
| Active | Checkbox | |

**Risk Profiles**
One row per client segment. Maintained by the operator, read by Risk Watcher.

| Field | Type | Notes |
|-------|------|-------|
| Segment | Single select | E-commerce / Local / Agency Partner |
| Healthy Signals | Long text (JSON) | The patterns that define "good" |
| Trouble Signals | Long text (JSON) | Per-pattern: signal definition, threshold, urgency |
| Data Sources | Multi-select | Which sources to monitor for this segment |

**Triaged Items** (agent-generated; can live in BigQuery only if you prefer)
Every classified inbound signal lands here. The Risk Watcher and brief composers read from this table.

| Field | Type | Notes |
|-------|------|-------|
| Source | Single select | Gmail / Drive / Airtable / Calendar / Chat / Vantage |
| Source URL | URL | |
| Triaged At | Date/time | |
| Actionable | Checkbox | |
| Positive Goal-Achieving | Checkbox | |
| the operator's Job vs Delegate | Single select | the operator / Delegate / N/A |
| Action Type | Single select | (same as Tasks) |
| Category | Single select | (same as Tasks) |
| Owner | Linked record (Team) | |
| Severity | Single select | Critical / High / Medium / Low / Info |
| Confidence | Number | 0-1 — agent's confidence in classification |
| Reasoning | Long text | Why the agent classified this way |
| Routed To | Multi-select | Channels the routing layer pushed it to |

### 3.2 Validation rules to enforce now

- Every Client must have an Account Manager and a Salesperson (can be the same person).
- Every Project must have an Owner (defaults from Client.Account Manager).
- Every active Project must have a Scope Document linked.
- HIPAA checkbox cannot be edited by the Brain — manual control only.
- Goals must have Parent Goal set unless Horizon = 10-year.

These rules need to be enforced in Airtable (formula fields, required fields, scripting block) before week-3 agents go live. Otherwise the Brain inherits inconsistent ownership data and the routing layer fails silently.

---

## 4. The Goals Framework

The Goal Steward agent walks the operator (and team members who opt in) through a structured intake based on Session 1 of the Rule the Room methodology. Goals are not invented by the agent — they are elicited from the human and recorded in structured form.

### 4.1 The intake sequence

Run during week 1-2 of the build, before any other agent goes live. Done as a guided conversation between the operator and the Goal Steward (or initially, between the operator and Claude in chat with manual transcription to Airtable).

1. Bucket list — what do you want to do/be/have?
2. Mission statement — one sentence, why you're doing what you're doing
3. 10-year vision across 7 life areas (work, family, health, spiritual, financial, emotional, intellectual)
4. Principles — 3-5 values from the Principle Word Table
5. Strengths — what you do well that gives you energy
6. Target market — who you energize working with
7. Business process — your repeatable methodology
8. 3-year goals across: Revenue, Team Makeup, Education, Ideal Client, Service Model, Life Role Integration
9. 1-year commitment — what you commit to deliver this year
10. Quarterly building blocks — 2-3 starred items for the next 12 weeks
11. Playing-field reality — honest current-state assessment

The Goals table holds steps 8-10 as structured records. Steps 1-7 live as long-form text in a Goal Steward Memory Bank entry tied to the operator's identity — they're context the agent uses for classification, not records the agent updates.

### 4.2 Weekly cadence

Every Friday, the Goal Steward:
1. Surfaces each active Quarterly goal and asks for a 1-10 score
2. Asks for a one-sentence "why this score"
3. Records both in Goal Scores
4. Computes week-over-week trend
5. Flags any goal that dropped 2+ points week-over-week
6. Flags any goal not scored in 14+ days
7. Includes the goal review in the Friday weekly synthesis

### 4.3 Quarterly cadence

Every 12 weeks (end of quarter), the Goal Steward:
1. Reviews completion rate of quarterly goals
2. Prompts the operator to set the next quarter's building blocks
3. Suggests carry-overs for goals that didn't complete but are still relevant
4. Updates the 1-year commitment if needed
5. Produces a quarterly retrospective document

---

## 5. The Agent Roster

Six agents, deployed sequentially over weeks 3-11. Each is defined here at the level of: what it does, what it reads, what it writes, when it runs, and what its precision/recall posture is.

### 5.1 Triage Agent

**Job:** Classify every inbound signal using the Rule the Room taxonomy plus goal-relevance plus delegation-eligibility.

**Reads:** New Gmail threads, Drive doc creates/updates, Airtable record changes, Calendar invites, Google Chat messages. Cross-references against Clients, Projects, Goals.

**Writes:** A row in Triaged Items for every signal. May create a row in Tasks if Action Type ≠ "Do It Now".

**Triggers:** Event-driven — fires within 5 minutes of source change. Application Integration polls sources at appropriate cadence (Gmail every 5 min, Drive every 15 min, Airtable webhook on change, Calendar on invite).

**Posture:** High recall on classification (always classifies), but flags low-confidence cases for human review rather than acting.

**Output schema:** All Triaged Items fields populated, including Confidence and Reasoning.

### 5.2 Risk Watcher

**Job:** Continuously evaluate each active client against their segment's Risk Profile, detect anomalies, raise flags.

**Reads:** Triaged Items (rolling 30 days), Risk Baselines per client, Risk Profiles per segment, Vantage KPI outputs (where accessible). Maintains its own per-client Memory Bank entry tracking baselines.

**Writes:** Flags into Triaged Items with Severity = High or Critical and Source = Risk Watcher. Updates the Project.Health field when health changes.

**Triggers:** Runs once per hour during business hours, once per day off-hours. Acknowledgment Gap detection runs nightly across all active clients.

**Posture:** High recall. the operator explicitly chose to tolerate false positives in this category.

**Segment-specific implementations:** See §6.

### 5.3 Goal Steward

**Job:** Maintain the goal corpus. Run intake, weekly reviews, quarterly retrospectives. Provide goal-relevance lookups to other agents.

**Reads:** Goals, Goal Scores, conversation history with the operator/team members.

**Writes:** Goals (during intake/quarterly review), Goal Scores (weekly), Tasks (for goal-related actions like "review goals this Friday").

**Triggers:** Weekly cadence (Friday), quarterly cadence (end of 12-week period), on-demand when the operator invokes.

**Posture:** Patient — never aggressive, always asks rather than assumes when adjusting goals.

### 5.4 Morning Brief Composer

**Job:** Produce a personalized morning brief for each team member, delivered before their workday starts.

**Reads:** Triaged Items (last 24 hours), open Tasks owned by the recipient, today's Calendar, Risk Watcher flags raised overnight, drafts prepared by other agents awaiting review.

**Writes:** A markdown document delivered to the recipient via their preferred channel (email by default, configurable).

**Triggers:** 5 minutes before each team member's defined morning processing time (per Rule the Room — usually 7:30 AM).

**Posture:** Ruthlessly brief. Five items max for owner views, ten max for the leadership view (which groups by owner). Anything below the cut goes into a "rest of the queue" link, not the brief itself.

**Two output modes:**
- Owner view: Sarah sees only her items.
- Leadership view: the operator sees all items, grouped by owner, with cross-team patterns highlighted.

### 5.5 Evening Reflection Composer

**Job:** Produce a backward-looking reflection at end of day. Different intent than morning brief.

**Reads:** Tasks completed today, Triaged Items resolved or dismissed today, new commitments made (extracted from the operator's sent mail today), goal-relevant activity, calendar of what was attended.

**Writes:** A reflective document delivered to the recipient.

**Triggers:** 5 minutes before evening processing time (usually 5:30 PM).

**Posture:** High precision. This is the reflective artifact — it should make the operator think, not generate noise. If there's nothing worth reflecting on, it says so honestly rather than padding.

**Format:** What happened, what it might mean, what's worth carrying into tomorrow. Three sections, not bullet lists.

### 5.6 Knowledge Surfacer

**Job:** Answer ad-hoc queries against the full corpus. The reactive query interface.

**Reads:** Knowledge Catalog (federated across BigQuery + Workspace).

**Writes:** Nothing persistent. Returns answers with citations.

**Triggers:** On-demand — invoked from Gemini app, Chat, or a future custom UI.

**Posture:** High precision. Always cites. Refuses to answer when context is insufficient rather than hallucinating.

### 5.7 Weekly Synthesizer

**Job:** Run Friday afternoon. Produce the weekly report-style outputs: client status across the portfolio, goal scores, time-audit summary, capacity planning notes.

**Reads:** Everything from the past week — Triaged Items, Tasks, Goal Scores, Calendar.

**Writes:** A weekly synthesis document, delivered to the operator (and optionally team members) Friday afternoon.

**Triggers:** Friday 3:00 PM (configurable), aligned with the Rule the Room weekly review cadence.

**Posture:** High precision. This document needs to be reliable — it's the basis for the operator's planning the next week.

---

## 6. Risk Watcher: Three Profiles

The single most important agent-design decision is segment-specific Risk Profiles. Defined below based on your operational knowledge.

### 6.1 E-commerce / DTC Profile

**Healthy signals (baseline):**
- Email response latency to weekly reports: ≤ 48 hours
- Question type ratio: forward-looking ("what should we do") > backward-looking ("why did this happen")
- Vantage KPIs (ROAS, CAC, email revenue) stable or trending positive
- Client introduces additional contacts within first 60 days
- Client sends data proactively (≥ 1 unprompted data event per month)

**Trouble signals (with thresholds):**

| Pattern | Signal definition | Severity |
|---------|-------------------|----------|
| Acknowledgment Gap | Vantage ROAS drops ≥ 20% week-over-week AND no client communication mentions it within 5 days | Critical |
| Silent After Deliverable | Weekly report sent, no client response within 5 business days | High |
| Late Contract Curiosity | Client asks "what does the retainer cover" 60+ days after start | High |
| Klaviyo List Decline | Client's Klaviyo list shrinks ≥ 5% month-over-month | Medium (early warning of business contraction) |

**Data sources:** Vantage KPI table, Gmail (response latency + content classification), Airtable deliverable timestamps.

**Memory Bank state per client:** Rolling 8-week baseline of ROAS, response latency, list size, communication frequency, question-type distribution.

### 6.2 Local / Multi-Location Profile

**Healthy signals:**
- Owner or GM is the consistent point of contact (no rotation through staff)
- Approval turnaround on deliverables: ≤ 3 business days
- GBP metrics (calls, direction requests, impressions) stable or growing
- Client provides operational context unprompted (e.g., "we're running a promotion next month")
- Referral activity (introduces you to other local businesses)

**Trouble signals:**

| Pattern | Signal definition | Severity |
|---------|-------------------|----------|
| Stakeholder Change | New name appears in CC field or accepts a calendar invite without prior introduction | High |
| Approval Slowdown | Approval turnaround stretches from baseline to 2x baseline across 2 consecutive deliverables | High |
| Owner Disengagement | Owner stops attending recurring calls, delegates to coordinator | Critical |
| GBP Decline | GBP call volume or direction requests decline ≥ 25% month-over-month | Medium |
| Acknowledgment Gap | GBP performance metric drops AND client hasn't asked about it within 7 days | High |

**Data sources:** Gmail (CC field monitoring, threading patterns), Calendar (attendee patterns), Vantage GBP metrics, Airtable approval timestamps.

**Memory Bank state per client:** Stakeholder roster (who's on which type of communication), approval turnaround baseline, GBP metric baseline, call attendance pattern.

### 6.3 Agency Partner / White-Label Profile

**Healthy signals:**
- Report access within 3 days of generation
- Refinement-question-to-report ratio ≥ 0.3 (i.e., one question per 3 reports)
- Edge cases looped in proactively
- End-client count stable or growing
- Communication is proactive (not reactive)

**Trouble signals:**

| Pattern | Signal definition | Severity |
|---------|-------------------|----------|
| Refinement Silence | Agency stops asking refinement questions for 30+ days after a period of regular requests | High |
| Report Volume Decline | Reports generated/accessed drops ≥ 33% month-over-month | High |
| Raw Data Inquiry | Agency asks about export formats or raw data access | Critical (switching signal) |
| Refinement Ratio Drop | Refinement-question-to-report ratio falls below 0.1 even if volume is stable | High |
| Acknowledgment Gap | End-client count dropping AND agency hasn't mentioned it within 14 days | Medium |

**Data sources:** Drive access logs (download/view frequency), Gmail (refinement-question classification, edge-case threading), Vantage agency dashboard (end-client count, report generation volume).

**Memory Bank state per agency:** Report generation/access baseline, refinement-question-to-report ratio rolling 60 days, end-client roster, communication initiator pattern (who reaches out first).

### 6.4 The Acknowledgment Gap pattern

A standalone capability that runs across all three segments: the Risk Watcher continuously cross-references *what we know is happening in the client's data* against *what they're acknowledging in communication*. The gap between those two is where churn forms. This pattern is so high-leverage it should be tracked as a first-class signal type, with its own threshold tunable per segment.

---

## 7. The Triage Logic

The Triage Agent applies a three-axis classification to every inbound signal, codified directly from the Rule the Room methodology.

### 7.1 Axis 1: Actionable vs. Non-Actionable

The agent answers: "Will this help advance one of the active goals (Goals table, Status = Active) within the next two months?"

If yes → Actionable. Continue to Axis 2.
If no → Non-Actionable. Classify as Trash / File / Tickle.

### 7.2 Axis 2: Positive Goal-Achieving Strength

For Actionable items, the agent assesses how directly the item advances goals:
- Strongly PGA: Direct line to a Quarterly building block
- Moderately PGA: Advances a 1-year commitment but not this quarter's focus
- Weakly PGA: Maintenance work that keeps things from breaking but doesn't advance

### 7.3 Axis 3: Owner Type

- the operator's Job: Requires unique judgment, relationship, or expertise only the operator can provide
- Delegate: Could be done by another team member at lower cost
- N/A: Not applicable (e.g., already delegated, or non-personnel)

### 7.4 Action Type assignment

Per Rule the Room's five action types:
- Do It Now: < 2 minutes to complete
- Delegate: > 2 minutes, can be assigned
- Defer: > 2 minutes, no specific time required
- Schedule: > 2 minutes, requires specific time
- Wait: Blocked on someone else

The agent assigns Action Type based on its assessment of effort and ownership. Wait items get special treatment — they're tracked for aging.

### 7.5 Category assignment

Per Rule the Room's nine categories: Calls / Computer / Errands / Office / Schedule / Team Meeting / Staff / Waiting For / Home.

### 7.6 Confidence handling

Every classification carries a confidence score (0-1). Items with confidence < 0.7 are routed to the operator's review queue regardless of severity. Over time, the agent's classifications get tuned against the operator's corrections, improving the baseline.

---

## 8. Notification Routing

The routing layer is its own Application Integration flow. It reads new Triaged Items from BigQuery (via streaming insert or polling), applies routing rules, and pushes to channels.

### 8.1 Routing matrix

| Severity | Channel(s) | Cadence |
|----------|-----------|---------|
| Critical | Push notification (Gemini app) + immediate Google Chat DM | Immediate |
| High | Morning brief next morning + Google Chat DM if before 4pm | Daily morning + same-day if late |
| Medium | Morning brief next morning | Daily morning |
| Low | "Rest of the queue" link in morning brief | Daily morning, collapsed |
| Info | Airtable view only | Always available, no push |

### 8.2 Owner vs. Leadership routing

Every notification is generated in two views:

- **Owner view:** Routed to the Owner field of the Triaged Item. Filtered to items they own.
- **Leadership view:** Routed to the operator. All items, grouped by owner, with cross-team patterns surfaced.

Each user has a per-channel preference: where the operator gets the leadership view (his email, his Inbox, his Chat) is configurable per channel and per severity.

### 8.3 Channel-specific formatting

- **Gemini Inbox:** Structured, actionable, with category tags ("Needs your input", "Errors", "Completed")
- **Email digest:** Markdown-formatted, scannable, with links into Airtable for context
- **Google Chat DM:** Single-sentence summary with link for detail
- **Airtable view:** Filtered table view, no formatting overhead

---

## 9. Application Integration Flows

The actual GCP plumbing. Application Integration is the iPaaS layer; each named flow is a separate integration object.

### 9.1 Sync flows

**airtable_to_bq_sync**
- Trigger: Scheduled every 15 minutes
- Action: Pulls all Airtable tables incrementally (last-modified > last sync timestamp), writes to staging tables in BigQuery, runs MERGE into target tables
- Idempotent: Yes, via record IDs
- Schema drift handling: New columns logged to monitoring topic, surfaced to the operator; do not auto-add

**gmail_to_triage_queue**
- Trigger: Scheduled every 5 minutes
- Action: Lists new threads since last check, filters out HIPAA-client threads, publishes thread metadata to Pub/Sub topic `triage-input`
- Notes: Body content not pulled here — stays in Gmail until Triage Agent fetches via Workspace connector

**drive_to_triage_queue**
- Trigger: Scheduled every 15 minutes
- Action: Detects new or modified Drive files in monitored folders, publishes metadata to `triage-input`
- Notes: Same HIPAA filter

**calendar_to_triage_queue**
- Trigger: Scheduled every 30 minutes
- Action: Detects new invites, RSVPs, and calendar changes; publishes to `triage-input`

**chat_to_triage_queue**
- Trigger: Real-time webhook from Google Chat
- Action: Captures messages in monitored spaces, publishes to `triage-input`

**vantage_metrics_to_bq**
- Trigger: Scheduled hourly (during business hours)
- Action: Queries Vantage KPI tables (cross-project authorized dataset) for clients in the Brain's scope, writes daily snapshots to `vantage_kpi_snapshots`
- Notes: Cross-project authorized dataset is the recommended pattern; alternative is materialized views in the Brain project

### 9.2 Agent invocation flows

**triage_agent_invoke**
- Trigger: Pub/Sub subscription on `triage-input`
- Action: Invokes the Triage Agent (Vertex AI Agent Engine endpoint) with the queued item, receives classification, writes to Triaged Items table

**risk_watcher_hourly**
- Trigger: Cloud Scheduler, hourly during business hours
- Action: Invokes the Risk Watcher per active client, writes flags to Triaged Items where applicable

**risk_watcher_acknowledgment_gap**
- Trigger: Cloud Scheduler, daily at 11 PM
- Action: Cross-references Vantage KPI shifts against client communications, flags Acknowledgment Gaps

**goal_steward_weekly**
- Trigger: Cloud Scheduler, Fridays at 2 PM
- Action: Invokes Goal Steward to run the weekly review with the operator (and team members who opted in)

**morning_brief_per_user**
- Trigger: Cloud Scheduler, per-user time configuration
- Action: Invokes Morning Brief Composer for the user, delivers via their preferred channel

**evening_reflection_per_user**
- Trigger: Cloud Scheduler, per-user time configuration
- Action: Invokes Evening Reflection Composer

**weekly_synthesizer**
- Trigger: Cloud Scheduler, Fridays at 3 PM
- Action: Invokes Weekly Synthesizer, delivers via configured channel

### 9.3 Routing flow

**route_triaged_items**
- Trigger: BigQuery streaming insert on Triaged Items, or scheduled polling every 5 minutes
- Action: For each new item, applies routing matrix, pushes to channels
- Failure handling: Failed pushes go to dead-letter Pub/Sub topic `routing-failures` for manual review

---

## 10. Knowledge Catalog Setup

### 10.1 Data sources to register

- BigQuery: Brain project (auto-discovered)
- BigQuery: Vantage project (cross-project federation, scoped to non-HIPAA datasets only)
- Workspace data store: Drive (read-only, scoped to non-HIPAA folders)
- Workspace data store: Gmail (read-only, scoped via filter to exclude HIPAA-client domains)
- Workspace data store: Calendar (read-only)
- Workspace data store: Chat (read-only, monitored spaces only)

### 10.2 Aspects to define

- Client (aspect type) — applied to Airtable client tables, Gmail threads, Drive folders
- Project (aspect type) — applied to Airtable projects, scope docs
- HIPAA-Excluded (aspect type) — applied to anything that should never reach the Brain
- Owner (aspect type) — applied to Tasks, Triaged Items, Projects

### 10.3 Discovery scan cadence

Per the §15 cost-control discussion: discovery scans run twice daily (6 AM and 6 PM Pacific), not continuously. This keeps DCU consumption predictable within the 100 DCU-hour free tier.

### 10.4 Premium features explicitly disabled

- Data Profiling: Off
- Data Quality Scans: Off
- Data Lineage: Off (manual lineage doc maintained instead if needed)

---

## 11. Security & Isolation

### 11.1 HIPAA boundary

The Brain runs in its own GCP project (proposed: `agency-brain-demo`). HIPAA clients live in their separate project (existing). The HIPAA project is **not federated** into the Brain's Knowledge Catalog. The Brain's service account does not have IAM access to the HIPAA project.

Enforcement layers:
1. IAM at the project boundary (no cross-project grants)
2. Airtable-level: clients with HIPAA = checked are excluded from sync to Brain BigQuery
3. Gmail filter: configured at the Workspace connector to exclude HIPAA-client domains
4. Drive folder scoping: HIPAA-client folders not in the monitored set
5. Periodic audit: weekly script verifies no HIPAA-flagged client appears in any Triaged Item

If at any point a HIPAA-flagged client's data appears in the Brain's tables, the system raises a critical alert and blocks all agent execution until reviewed.

### 11.2 Domain-Wide Delegation

Required for the agents to read Workspace data on behalf of users. Configured with **narrowly scoped** OAuth scopes:
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/chat.spaces.readonly`

The service account configured for DWD:
- Cannot be assumed by any human user (no `iam.serviceAccounts.actAs`)
- Has all activity logged to Cloud Audit Logs with retention ≥ 1 year
- Is rotated annually
- Is monitored for anomalous access patterns

### 11.3 Cross-project Vantage access

The Brain queries Vantage KPI outputs via authorized datasets. The pattern:
1. In the Vantage project, create a dataset `vantage_kpi_exports` containing only aggregated, non-PII metrics per client
2. Authorize that dataset for the Brain project's service account
3. The Brain queries via federated `SELECT` — never reads raw Vantage data

This preserves Vantage's single-tenant client isolation guarantee while exposing the operational signals the Risk Watcher needs.

### 11.4 Drafts boundary

Agents may write Gmail drafts to the operator's drafts folder (and team members' drafts folders for items they own) but **never** send. The send action is always human-gated. This is enforced by scope: the Workspace integration uses `gmail.compose` (drafts only), not `gmail.send`.

### 11.5 Audit trail

Every agent action — every classification, every flag, every draft, every brief — is logged with:
- Timestamp
- Agent identity
- Inputs (with hashes for PII fields)
- Outputs
- Confidence score

Logs land in BigQuery `agent_audit_log` table with 18-month retention. the operator (or any leadership-role user) can query the audit trail directly.

---

## 12. Phased Build Plan

12 weeks, with a working subset shipping at week 5 and capability layers added weekly thereafter. Each week's deliverable is concrete and testable.

### Week 1: Foundation & goal definition
- Provision the Brain GCP project (`agency-brain-demo`)
- Configure IAM, set up service accounts, enable APIs
- Configure Domain-Wide Delegation with the four scopes
- Set up Knowledge Catalog basic registration of BigQuery
- **Run goal intake session** with the operator (Session 1 framework, ~3 hours)
- Populate Goals table with initial Quarterly building blocks

**Deliverable:** Brain project provisioned. Goals table populated. You can query "what are the operator's current quarterly goals" from BigQuery and get an answer.

### Week 2: Airtable schema completion + sync
- Finalize all Airtable tables per §3
- Implement validation rules
- Build `airtable_to_bq_sync` Application Integration flow
- Verify sync runs reliably for 48 hours
- Set up vantage cross-project authorized dataset
- Build `vantage_metrics_to_bq` flow

**Deliverable:** BigQuery has a clean, current replica of Airtable + Vantage KPI snapshots. Knowledge Catalog can answer questions across both.

### Week 3: Triage Agent v1
- Build Triage Agent in ADK
- Configure it to read from `triage-input` Pub/Sub topic
- Build `gmail_to_triage_queue` and `drive_to_triage_queue` flows
- Triage Agent writes to Triaged Items
- Manual review and tuning of first 100 classifications

**Deliverable:** Every new Gmail thread and Drive doc is classified and appears in Triaged Items within 15 minutes. the operator can correct classifications and the agent learns from corrections.

### Week 4: Risk Watcher v1 (E-commerce profile only)
- Build Risk Watcher in ADK
- Implement E-commerce Risk Profile signal definitions
- Memory Bank setup for per-client baselines
- Run for 7 days, manual review of all flags

**Deliverable:** Risk Watcher catches E-commerce risk signals. the operator validates the flags are useful and tuning is appropriate.

### Week 5: Morning Brief Composer (the operator only)
- Build Morning Brief Composer in ADK
- Owner-view implementation only (the operator receives his items)
- Email delivery channel configured
- Daily delivery for 7 days, with the operator's feedback

**Deliverable:** the operator receives a useful morning brief every morning. **The system is now providing operational value daily.**

### Week 6: Evening Reflection + team rollout
- Build Evening Reflection Composer
- Extend Morning Brief Composer to support team members
- Onboard 1-2 team members onto the system
- Owner views configured per team member

**Deliverable:** the operator + 1-2 team members are receiving morning briefs and evening reflections. The team is using the system.

### Week 7: Risk Watcher (Local + Agency profiles) + Wait aging
- Implement Local Risk Profile
- Implement Agency Partner Risk Profile
- Implement Wait-item aging logic (Tasks where Action Type = Wait, Wait Started > N days)
- Implement the cross-segment Acknowledgment Gap detection

**Deliverable:** Risk Watcher covers all three segments. Wait items are aged and surfaced when they sit too long.

### Week 8: Leadership view + Gmail drafts
- Implement Leadership view in Morning Brief and Evening Reflection
- Build Gmail drafts capability for Triage Agent (when Action Type = Do It Now and email response is the action)
- the operator validates the drafts are usable and don't require heavy editing

**Deliverable:** the operator sees the full team picture in his morning brief. The agent can prepare email drafts that go to his Drafts folder.

### Week 9: Goal Steward
- Build Goal Steward in ADK
- Implement weekly review cadence
- Implement quarterly retrospective cadence
- Goal score trends surfaced in Risk Watcher and Weekly Synthesizer

**Deliverable:** Goals are alive — scored weekly, reviewed quarterly, surfaced in the morning brief when relevant.

### Week 10: Weekly Synthesizer
- Build Weekly Synthesizer in ADK
- Includes: portfolio status, goal scores, time-audit summary, capacity notes
- Friday afternoon delivery
- Iterate on format based on the operator's feedback

**Deliverable:** Friday afternoon, a useful weekly synthesis lands in the operator's email.

### Week 11: Knowledge Surfacer
- Configure Gemini Enterprise app with Brain MCP endpoint
- Test ad-hoc queries: "what did we tell ClientCo about onboarding timeline", "show me all clients with declining ROAS this quarter", etc.
- Tune query handling for citation quality

**Deliverable:** the operator and team can ask the system questions in natural language and get answers with citations.

### Week 12: Hardening + observability
- Implement self-monitoring: Acknowledgment Gap dismissal tracking, agent confidence trend monitoring, false-positive rate tracking per Risk Profile
- Implement on-call alerting for system failures (sync failures, agent timeouts, routing failures)
- Documentation pass on the spec (this doc) reflecting actual implementation
- Final security audit: HIPAA isolation verification, DWD scope review, audit log spot-check

**Deliverable:** The system is observable, alerting works, the spec reflects reality. The build is done.

### Shippable cuts at each milestone

If any week runs long, here's what gets cut without breaking the build:

| Week | If running long, defer this | Result |
|------|---------------------------|--------|
| 3 | Calendar and Chat triage queues | Gmail + Drive triage works; add Calendar/Chat in week 7 |
| 4 | Memory Bank baselines | Risk Watcher works on simple thresholds initially; add baselines in week 7 |
| 5 | Email delivery channel | Brief lands in Airtable view; email in week 6 |
| 7 | Acknowledgment Gap | Add in week 9 alongside Goal Steward |
| 9 | Quarterly retrospective | Manual quarterly review for first quarter; automate later |
| 10 | Capacity planning notes | Weekly Synthesizer ships without; add when there's data to support it |
| 11 | Multi-user query auth | the operator-only Knowledge Surfacer initially; team rollout in v2 |

---

## 13. Observability & Self-Monitoring

The system tracks itself as carefully as it tracks the agency.

### 13.1 Metrics tracked per agent

- Invocation count (per hour, per day)
- Average latency
- Confidence score distribution
- Error rate
- Cost (token usage, infrastructure)

### 13.2 Metrics tracked per Risk Profile

- Flags raised (per week)
- Flag dismissal rate (the operator/team marked as not useful)
- Flag confirmation rate (the operator/team acted on it)
- Lead time (time between flag and confirming action)

### 13.3 The meta-signal

If a flag is raised and not acted on within 7 days, it's auto-marked as "stale" and contributes to a weekly meta-report:
- Are we ignoring real signals? (If dismissal rate is high but we're losing clients flagged → false negatives in our judgment, not the agent)
- Is the agent generating noise? (If confirmation rate is low → agent precision needs tuning)

### 13.4 Cost monitoring

Daily query against billing data. If daily DCU consumption trends toward exceeding 100 DCU-hours/month, alert. If Vertex AI agent costs exceed $X/day, alert. Thresholds set per the operator's tolerance.

---

## 14. Open Decisions

These are deliberate "decide during the build" items, not oversights.

1. **Vantage cross-project topology.** Whether the Brain runs in its own project or in the same project as Vantage. Decide week 1 based on IAM constraints.

2. **Agent model choice.** Default is Gemini Pro. May want to use Claude or other models for specific agents (Knowledge Surfacer might benefit from a more conservative model). Decide as we tune each agent.

3. **Memory Bank vs. BigQuery for baselines.** Memory Bank is the obvious choice but has cost and latency implications. May store per-client baselines in BigQuery and have agents query them directly. Decide week 4 based on Risk Watcher performance.

4. **Multi-user Knowledge Surfacer auth.** v1 is the operator-only. Team rollout requires per-user query authorization to ensure Sarah doesn't query into clients she doesn't own. Decide for v2.

5. **Drafts for client-facing communication.** v1 is internal drafts only (status updates, follow-ups). Whether the agent ever drafts client-facing emails (proposals, status updates to clients) is a v2 decision after watching v1 performance.

6. **Goal Steward intake for team members.** v1 runs intake with the operator only. Team members onboard with their own goals in v2 once the operator's experience validates the flow.

7. **Quarterly retrospective format.** v1 may be a the operator-led conversation transcribed; v2 may be an agent-driven structured artifact.

8. **Time audit visualization.** The data is captured in v1 as a side effect; whether it deserves its own dashboard (vs. inline in Weekly Synthesizer) is a UX decision deferred to v2.

---

## 15. Cost Model

For 10-team, ~20 active clients, ~50k Triaged Items/month projected:

| Component | Estimated monthly cost |
|-----------|------------------------|
| BigQuery storage (~5 GB) | ~$0.10 |
| BigQuery queries (under 1 TiB free tier) | $0 |
| Application Integration (Standard tier) | ~$300-500 |
| Knowledge Catalog (under 100 DCU-hour free tier) | $0 (with disciplined scan cadence) |
| Vertex AI Agent Engine | ~$200-500 (depends on agent invocation volume) |
| Gemini Enterprise licensing (per-user) | $21-30/user/month × team size |
| Pub/Sub, Cloud Scheduler, Cloud Functions | <$50 |
| **Total estimated infrastructure** | **~$500-1,200/month + Gemini Enterprise per-seat** |

This excludes the operator's time and any contractor labor. Labor cost will dominate the build phase but drop to maintenance levels post-week-12.

The Gemini Enterprise per-seat cost is the largest variable. Worth confirming whether your existing Workspace plan includes it or whether it's a separate add-on.

---

## 16. Success Metrics

Six weeks after launch (i.e., week 18 or so), the system is succeeding if:

- the operator receives the morning brief daily and reads it (open rate > 90%)
- Risk Watcher has flagged at least one client risk that the operator agrees was real and would have been missed otherwise
- Goal scores have been recorded weekly for 6 consecutive weeks
- At least one Gmail draft prepared by the agent has been sent (after review) without significant editing
- No HIPAA isolation breaches detected in audit logs
- Cost is within 20% of the model in §15
- At least one team member beyond the operator is using the system actively

If any of these fail, that's the priority for v2 work.

Twelve months after launch:
- The agency has at least one specific churn event the system caught early enough to save
- The team's "things slipping through cracks" rate is measurably down (need to define how this is measured)
- the operator's time spent on operational synthesis is measurably down
- The system is cited externally — case study, blog post, or sales conversation — at least once

---

## 17. What Comes After v1

Not part of this build, but worth naming so they don't accidentally creep in:

- Client-facing version: a stripped-down Brain that clients can query about their own engagement with Agency
- Vantage integration deepening: Risk Watcher reads not just KPI snapshots but actual model output trends
- Multi-agency white-label: the Brain itself becomes a product Agency sells to other agencies (eating own cooking → selling own cooking)
- Voice interface: morning brief delivered as audio, evening reflection captured via voice memo
- Predictive scoping: agent reviews proposals against historical project outcomes, flags scoping risks before contract signature

---

## Appendix A: Starter Prompts

Skeleton prompts for each agent, to be refined during the build.

### Triage Agent
```
You are the Triage Agent for the agency. You classify inbound operational signals using the Rule the Room methodology, the active goal context, and the team's ownership structure.

For each item, output structured JSON with:
- actionable: boolean (does this advance an active goal in the next 60 days?)
- positive_goal_achieving: enum [strong, moderate, weak, none]
- owner_type: enum [brian, delegate, na]
- action_type: enum [do_now, delegate, defer, schedule, wait]
- category: enum [calls, computer, errands, office, schedule, team_meeting, staff, waiting_for, home]
- task_or_project: enum [task, project]
- owner_email: string (from Clients/Projects ownership lookup)
- severity: enum [critical, high, medium, low, info]
- confidence: float (0-1)
- reasoning: string (one sentence)

Active goals are provided in context. Ownership lookup is provided in context.

If confidence < 0.7, flag for human review.
Never act on the item beyond classification.
```

### Risk Watcher (E-commerce variant)
```
You are the Risk Watcher for Agency's e-commerce/DTC clients. You monitor each active client against the E-commerce Risk Profile.

For each client, evaluate:
1. Vantage KPI trends (ROAS, CAC, email revenue) vs. baseline
2. Communication response latency vs. baseline
3. Question-type ratio (forward-looking vs. backward-looking)
4. Klaviyo list growth rate
5. Acknowledgment Gap: any KPI shifts not acknowledged in client communication

Baselines are provided per client.

Output: a list of flags, each with severity, signal_type, evidence (specific data points), and recommended_action.

If no flags, output an empty list — do not invent risks.

You are tuned for high recall. False positives are acceptable; false negatives are not.
```

(Similar prompts for Local and Agency Partner profiles.)

### Goal Steward
```
You are the Goal Steward. You help the operator and the Agency team maintain a coherent goal hierarchy from 10-year vision down to weekly tactics.

Your cadences:
- Friday weekly review: prompt for 1-10 score on each active Quarterly goal, capture brief why
- End of quarter: lead a retrospective conversation, suggest carry-overs
- On-demand: when invoked, walk through any layer of the goal hierarchy

You ask questions; you do not assume. When a goal isn't being scored or is dropping, you surface it gently.

You read from the Goals and Goal Scores tables. You write to Goal Scores (weekly) and Goals (during intake/retrospective only).

Tone: patient, reflective, never aggressive.
```

### Morning Brief Composer (Owner view)
```
You are the Morning Brief Composer. Your job is to produce a brief, scannable artifact for {owner_name} to read in 90 seconds at the start of their day.

Inputs:
- Triaged Items from last 24 hours, owned by {owner_name}
- Open Tasks owned by {owner_name}, sorted by Due Date
- Today's Calendar
- Risk flags raised overnight involving clients owned by {owner_name}
- Drafts prepared by other agents awaiting review

Output: a markdown document with these sections:
1. Today's Top 3 (the three most important items, in priority order)
2. Drafts Awaiting Review (each with one-sentence summary and link)
3. Risk Flags (only if any exist)
4. Calendar (today only, with prep notes for any meeting)
5. Rest of Queue (link to filtered Airtable view, no inline detail)

Be ruthlessly brief. Five items max in Top 3. If there's nothing to flag in a section, omit the section entirely.

Tone: sharp executive assistant.
```

### Evening Reflection Composer
```
You are the Evening Reflection Composer. Your job is to produce a thoughtful artifact for {owner_name} at end of day.

This is not a status report. This is reflective.

Inputs:
- Tasks completed today
- Triaged Items resolved or dismissed today
- New commitments made today (from sent mail)
- Goal-relevant activity today
- What was attended on the calendar

Output: prose, three sections:
1. What happened today (factual, brief)
2. What it might mean (your interpretation — patterns, meaningful changes, what's notable)
3. Worth carrying into tomorrow (one or two prompts, not a task list)

Tone: thoughtful coach. Honest. If today was unremarkable, say so honestly rather than padding.

If you have no genuine insight to offer, say "today was a normal day, here's what got done" and stop.
```

### Knowledge Surfacer
```
You are the Knowledge Surfacer. You answer ad-hoc queries against Agency's operational corpus.

You have access to:
- BigQuery (replicated Airtable, Triaged Items, Goals, etc.)
- Knowledge Catalog (semantic layer over Workspace)
- Vantage KPI snapshots (where authorized)

For every answer:
1. Cite specific sources (record IDs, document IDs, URLs)
2. Acknowledge confidence level
3. If you cannot answer with confidence, say so explicitly — do not infer

Never speculate. Never combine sources in ways the user did not ask for. Refuse politely if the query exceeds your authorized scope (e.g., HIPAA-flagged clients).

Tone: precise, helpful, slightly conservative.
```

### Weekly Synthesizer
```
You are the Weekly Synthesizer. Your job is to produce the operator's Friday afternoon weekly synthesis.

Inputs (last 7 days):
- All Triaged Items
- All Tasks completed and outstanding
- Goal Scores entered this week
- Calendar attendance
- Risk Watcher flags raised and resolved

Output: a markdown document with these sections:
1. Portfolio status (one sentence per active client, color-coded by Health field)
2. Goal scores (each Quarterly goal's score this week vs. last week, trend arrow)
3. Time audit (rough breakdown of where the week went, by category)
4. Capacity notes (any signals about over- or under-capacity)
5. Three things to consider for next week

Be precise. This is the planning document, not the morning brief. Length is acceptable; padding is not.
```

---

## Appendix B: Glossary

- **Acknowledgment Gap**: a state where Vantage data shows a client's business in trouble, but client communications do not acknowledge the trouble. Strongest leading indicator of churn.
- **Active goal**: a Goal record with Status = Active, Horizon = Quarterly, that is one of the current 12-week building blocks.
- **Brain**: shorthand for the entire Proactive Agency Brain system.
- **DWD**: Domain-Wide Delegation. The mechanism by which the agent's service account reads Workspace data on behalf of users.
- **Leadership view**: the morning brief / weekly synthesis variant the operator receives, containing all team members' items grouped by owner.
- **Owner view**: the morning brief variant a team member receives, filtered to only their owned items.
- **PGA**: Positive Goal-Achieving (Rule the Room terminology). A classification axis applied by the Triage Agent.
- **Risk Profile**: the segment-specific definition of healthy and trouble signals (one each for E-commerce, Local, Agency Partner).
- **Triaged Item**: a record in the Triaged Items table representing a classified inbound signal.
- **Wait item**: a Task with Action Type = Wait, blocked on someone else, tracked for aging.

---

*End of spec v0.1.*

*Next steps after the operator reviews: provision the Brain GCP project, finalize Airtable schema changes, schedule the goal intake session, kick off week 1.*
