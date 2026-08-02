terraform {
  required_version = ">= 1.3.0"

  required_providers {
    cloudru = {
      source  = "cloud-ru/cloud"
      version = "2.1.1"
    }
  }
}