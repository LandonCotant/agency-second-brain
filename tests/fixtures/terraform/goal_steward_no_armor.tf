# Goal Steward is NOT in PRD §4.4's required-armor list — it consumes
# pre-classified records, doesn't ingest external content. With no Template
# present, the gate would fail for triage/knowledge-surfacer/risk-watcher
# patterns; this fixture isolates ONLY the goal-steward case so the test
# can verify it doesn't false-positive in isolation.
resource "google_vertex_ai_reasoning_engine" "goal_steward" {
  display_name = "asb-agent-goal-steward"
  region       = "us-central1"
  project      = "agency-brain-demo"
}
