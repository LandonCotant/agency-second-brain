# Template exists for asb-agent-triage but its filter_config is empty.
# Gate fails because no enforcing filter sub-block is present.
resource "google_model_armor_template" "tb_agent_triage" {
  template_id = "asb-agent-triage"
  location    = "us-central1"
  parent      = "projects/agency-brain-demo/locations/us-central1"

  filter_config {
  }
}
