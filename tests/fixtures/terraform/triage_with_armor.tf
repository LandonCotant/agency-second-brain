resource "google_model_armor_template" "tb_agent_triage" {
  template_id = "asb-agent-triage"
  location    = "us-central1"
  parent      = "projects/agency-brain-demo/locations/us-central1"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
  }
}

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
