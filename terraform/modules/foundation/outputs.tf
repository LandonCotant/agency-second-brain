output "brain_project_id" {
  description = "Project ID for the Brain workload — consumed by every other workstream module"
  value       = google_project.brain.project_id
}

output "brain_project_number" {
  description = "Project number (used to derive default service account emails)"
  value       = google_project.brain.number
}

output "region" {
  description = "Default region for downstream resources"
  value       = var.region
}

output "cloud_build_sa_email" {
  description = "Cloud Build default SA email (for downstream IAM grants)"
  value       = "${google_project.brain.number}@cloudbuild.gserviceaccount.com"
}
