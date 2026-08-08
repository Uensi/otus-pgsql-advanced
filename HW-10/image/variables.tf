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
  description = "Твой публичный IP или CIDR, с которого разрешён SSH"
  type        = string
}

variable "vm_user" {
  description = "Имя пользователя для SSH на ВМ"
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "Публичный SSH-ключ для доступа к ВМ"
  type        = string
}

variable "vm_count" {
  description = "Количество одинаковых ВМ"
  type        = number
  default     = 2
}

variable "vm_name_prefix" {
  description = "Префикс имён ВМ"
  type        = string
  default     = "bench-vm"
}

variable "vm_platform_id" {
  description = "Платформа Yandex Compute"
  type        = string
  default     = "standard-v3"
}

variable "vm_cores" {
  description = "Количество vCPU на каждую ВМ"
  type        = number
  default     = 4
}

variable "vm_memory" {
  description = "Количество RAM в ГБ на каждую ВМ"
  type        = number
  default     = 8
}

variable "vm_disk_size" {
  description = "Размер диска каждой ВМ в ГБ"
  type        = number
  default     = 80
}

variable "vm_disk_type" {
  description = "Тип диска"
  type        = string
  default     = "network-ssd"
}

variable "vm_image_folder_id" {
  description = "Каталог стандартных образов Yandex Cloud"
  type        = string
  default     = "standard-images"
}

variable "vm_image_family" {
  description = "Family образа ОС"
  type        = string
  default     = "ubuntu-2204-lts"
}

variable "subnet_cidr" {
  description = "CIDR подсети"
  type        = string
  default     = "10.1.0.0/24"
}