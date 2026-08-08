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

variable "yandex_zones" {
  description = "Зоны для нод кластера"
  type        = list(string)
  default     = ["ru-central1-a", "ru-central1-b", "ru-central1-d"]
}

variable "my_ip" {
  description = "Мой ip"
  type        = string
}

variable "vm_user" {
  description = "Пользователь для SSH"
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "Публичный SSH-ключ"
  type        = string
}

variable "vm_count" {
  description = "Число нод кластера для кворума"
  type        = number
  default     = 3
}

variable "vm_platform_id" {
  description = "Платформа Yandex Compute"
  type        = string
  default     = "standard-v3"
}

variable "vm_cores" {
  description = "vCPU на каждую ВМ"
  type        = number
  default     = 2
}

variable "vm_memory" {
  description = "RAM в ГБ на каждую ВМ"
  type        = number
  default     = 4
}

variable "vm_disk_size" {
  description = "Размер диска в ГБ"
  type        = number
  default     = 32
}

variable "vm_disk_type" {
  description = "Тип диска"
  type        = string
  default     = "network-ssd"
}