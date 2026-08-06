# Mixed resources fixture: triage has a Template (passes), risk watcher has an
# RE but no Template (fails). Morning brief is non-required (passes silently).
# (Knowledge Surfacer was retired per ADR 0059; risk-watcher is the offender.)

resource "google_model_armor_template" "tb_agent_triage" {
  template_id = "asb-agent-triage"
  location    = "us-central1"
  parent      = "projects/agency-brain-demo/locations/us-central1"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
  }
}

resource "google_vertex_ai_reasoning_engine" "triage" {
  display_name = "asb-agent-triage"
  region       = "us-central1"
  project      = "agency-brain-demo"
}

resource "google_vertex_ai_reasoning_engine" "risk_watcher" {
  display_name = "asb-agent-risk-watcher"
  region       = "us-central1"
  project      = "agency-brain-demo"
}

resource "google_vertex_ai_reasoning_engine" "morning_brief" {
  display_name = "asb-agent-morning-brief"
  region       = "us-central1"
  project      = "agency-brain-demo"
}
