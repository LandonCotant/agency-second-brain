# State bucket created by scripts/bootstrap_tfstate.sh before first `terraform init`.
# See docs/adr/0003-tfstate-bucket-bootstrap.md for the chicken-and-egg.
terraform {
  backend "gcs" {
    bucket = "asb-tfstate-prod"
    prefix = "envs/prod"
  }
}
