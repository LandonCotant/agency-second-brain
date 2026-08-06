# Triage RE exists but no google_model_armor_template covers it. Gate fails
# because the `asb-agent-triage` pattern (PRD §4.4 required-armor) is uncovered.
resource "google_vertex_ai_reasoning_engine" "triage" {
  display_name = "asb-agent-triage"
  region       = "us-central1"
  project      = "agency-brain-demo"

  spec {
    service_account = "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com"
    source_code_spec {
      python_spec {
        entrypoint_module = "agent"
        entrypoint_object = "root_agent"
      }
    }
  }
}
