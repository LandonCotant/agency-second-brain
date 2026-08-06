config {
  format = "compact"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "google" {
  enabled = true
  version = "0.27.1"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}

# Repo naming convention enforcement is handled by scripts/least_privilege_check.py
# and a project-level naming check (see scripts/naming_check.py once WS-B adds resources
# beyond the foundation). For WS-A, tflint covers HCL hygiene only.
