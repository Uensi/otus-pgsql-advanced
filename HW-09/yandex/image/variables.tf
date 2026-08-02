variable "yandex_cloud_id" {
  description = "ID облака в Yandex Cloud"
  type        = string
}

variable "yandex_folder_id" {
  description = "ID каталога в Yandex Cloud"
  type        = string
}

variable "yandex_token" {
  description = "OAuth или IAM токен Yandex Cloud"
  type        = string
  sensitive   = true
}

variable "yandex_zone" {
  description = "Зона доступности Yandex Cloud"
  type        = string
  default     = "ru-central1-a"
}

variable "my_ip" {
  description = "Публичный IP или CIDR."
  type        = string
}

variable "db_version" {
  description = "Версия PostgreSQL"
  type        = string
  default     = "18"
}

variable "db_public_access" {
  description = "Назначает хосту PostgreSQL публичный IP. Для доступа только через бастион"
  type        = bool
  default     = false
}

variable "db_user" {
  description = "Имя пользователя PostgreSQL"
  type        = string
  default     = "pg_admin"
}

variable "db_password" {
  description = "Пароль пользователя PostgreSQL"
  type        = string
  sensitive   = true
}

variable "db_name" {
  description = "Имя базы данных PostgreSQL"
  type        = string
  default     = "app_db"
}

variable "vm_user" {
  description = "Имя пользователя для SSH на ВМ-бастионе"
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "Публичный SSH-ключ для доступа к ВМ-бастиону"
  type        = string
}