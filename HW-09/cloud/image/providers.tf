provider "cloudru" {
  project_id  = var.cloudru_project_id
  auth_key_id = var.cloudru_auth_key_id
  auth_secret = var.cloudru_auth_secret

  endpoints = {
    iam_endpoint        = "iam.api.cloud.ru:443"
    compute_endpoint    = "compute.api.cloud.ru:443"
    postgresql_endpoint = "postgresql.api.cloud.ru:443"
  }
}