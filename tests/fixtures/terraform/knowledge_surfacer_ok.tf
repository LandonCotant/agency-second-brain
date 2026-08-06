resource "google_model_armor_template" "tb_agent_knowledge_surfacer" {
  template_id = "asb-agent-knowledge-surfacer"
  location    = "us-central1"
  parent      = "projects/agency-brain-demo/locations/us-central1"

  filter_config {
    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
  }
}
