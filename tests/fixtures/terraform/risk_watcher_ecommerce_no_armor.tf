# Risk Watcher e-commerce profile is required-armor per PRD §4.4 but no
# google_model_armor_template covers `asb-agent-risk-watcher*`. Gate fails.
resource "google_vertex_ai_reasoning_engine" "risk_watcher_ecom" {
  display_name = "asb-agent-risk-watcher-ecommerce"
  region       = "us-central1"
  project      = "agency-brain-demo"
}
