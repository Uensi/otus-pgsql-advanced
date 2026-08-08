# =====================================================
# Сеть и подсеть
# =====================================================

resource "yandex_vpc_network" "yc_network" {
  name = "vm-network-yc"
}

resource "yandex_vpc_subnet" "yc_subnet" {
  name           = "vm-subnet-yc"
  zone           = var.yandex_zone
  network_id     = yandex_vpc_network.yc_network.id
  v4_cidr_blocks = [var.subnet_cidr]
}


# =====================================================
# Security groups
# =====================================================

# Внутренняя группа безопасности.
# Разрешает трафик внутри подсети между ВМ.
resource "yandex_vpc_security_group" "yc_internal_sg" {
  name       = "vm-internal-sg-yc"
  network_id = yandex_vpc_network.yc_network.id

  ingress {
    protocol       = "ANY"
    description    = "Allow all traffic inside private subnet"
    v4_cidr_blocks = [var.subnet_cidr]
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

# Группа безопасности для ВМ.
# Разрешает SSH только с твоего внешнего IP.
resource "yandex_vpc_security_group" "yc_vm_sg" {
  name       = "vm-public-sg-yc"
  network_id = yandex_vpc_network.yc_network.id

  ingress {
    protocol       = "TCP"
    description    = "SSH from my IP"
    v4_cidr_blocks = [local.my_ip_cidr]
    port           = local.ssh_port
  }

  ingress {
    protocol       = "ICMP"
    description    = "ICMP from my IP"
    v4_cidr_blocks = [local.my_ip_cidr]
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}


# =====================================================
# Образ Ubuntu
# =====================================================

data "yandex_compute_image" "ubuntu" {
  family    = var.vm_image_family
  folder_id = var.vm_image_folder_id
}


# =====================================================
# Две одинаковые ВМ
# =====================================================

resource "yandex_compute_instance" "yc_vm" {
  count = var.vm_count

  name        = "${var.vm_name_prefix}-${format("%02d", count.index + 1)}"
  platform_id = var.vm_platform_id
  zone        = var.yandex_zone

  allow_stopping_for_update = true

  resources {
    cores  = var.vm_cores
    memory = var.vm_memory
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.id
      size     = var.vm_disk_size
      type     = var.vm_disk_type
    }
  }

  network_interface {
    subnet_id = yandex_vpc_subnet.yc_subnet.id
    nat       = true

    security_group_ids = [
      yandex_vpc_security_group.yc_vm_sg.id,
      yandex_vpc_security_group.yc_internal_sg.id,
    ]
  }

  metadata = {
    ssh-keys           = "${var.vm_user}:${trimspace(var.ssh_public_key)}"
    serial-port-enable = 1
  }

  labels = {
    project = "otus"
    purpose = "benchmark"
  }
}