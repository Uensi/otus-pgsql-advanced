variable "cloudru_project_id" {
  type      = string
  sensitive = true
}

variable "cloudru_auth_key_id" {
  type      = string
  sensitive = true
}

variable "cloudru_auth_secret" {
  type      = string
  sensitive = true
}

variable "sber_zone" {
  type    = string
  default = "ru.AZ-2"
}

variable "sber_subnet_id" {
  type = string
}

variable "sber_subnet_cidr" {
  type    = string
  default = "10.1.0.0/24"
}

variable "sber_vm_flavor" {
  type    = string
  default = "lowcost10-2-4"
}

variable "sber_image_id" {
  type = string
}

variable "sber_disk_size" {
  type    = number
  default = 20
}

variable "ssh_public_key" {
  type      = string
  sensitive = true
}

variable "vm_user" {
  type    = string
  default = "ubuntu"
}

variable "my_ip" {
  type = string
}

variable "floating_ip_id" {
  type = string
}

variable "sber_pg_version" {
  type    = string
  default = "18"
}

variable "sber_specification_id" {
  type    = string
  default = ""
}

variable "sber_pg_data_gb" {
  type    = number
  default = 50
}

variable "db_name" {
  type    = string
  default = "sber_db"
}

variable "db_user" {
  type    = string
  default = "sber_admin"
}

variable "db_password" {
  type      = string
  sensitive = true
}

