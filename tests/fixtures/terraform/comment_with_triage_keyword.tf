# This goal_steward Reasoning Engine consumes already-classified records
# from the triage agent's output table; it does NOT ingest external content
# and therefore does not require Model Armor (PRD §4.4).
#
# This fixture verifies the gate doesn't false-positive on the keyword
# "triage" appearing in a comment of an unrelated resource. Goal Steward
# is not a required-armor pattern.
resource "google_vertex_ai_reasoning_engine" "goal_steward" {
  display_name = "asb-agent-goal-steward"
  region       = "us-central1"
  project      = "agency-brain-demo"
}
