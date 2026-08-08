# =====================================================
# Сеть: по подсети в каждой зоне
# =====================================================
resource "yandex_vpc_network" "ha_network" {
  name = "ha-network-yc"
}

resource "yandex_vpc_subnet" "ha_subnet" {
  count = length(var.yandex_zones)

  name       = "ha-subnet-${var.yandex_zones[count.index]}"
  zone       = var.yandex_zones[count.index]
  network_id = yandex_vpc_network.ha_network.id

  v4_cidr_blocks = ["10.1.${count.index}.0/24"]
}

# =====================================================
# Security groups
# =====================================================

# Внутренний трафик кластера: etcd, PostgreSQL, Patroni, HAProxy
resource "yandex_vpc_security_group" "ha_internal_sg" {
  name       = "ha-internal-sg"
  network_id = yandex_vpc_network.ha_network.id

  ingress {
    protocol       = "ANY"
    description    = "All traffic inside cluster subnets"
    v4_cidr_blocks = [local.internal_cidr]
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

# SG виртуальных машин
resource "yandex_vpc_security_group" "ha_vm_sg" {
  name       = "ha-vm-sg"
  network_id = yandex_vpc_network.ha_network.id

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

  # Трафик и healthcheck'и от балансировщика Yandex.
  ingress {
    protocol       = "TCP"
    description    = "HAProxy rw/ro for NLB traffic and healthchecks"
    v4_cidr_blocks = ["0.0.0.0/0"]
    from_port      = local.haproxy_rw_port
    to_port        = local.haproxy_ro_port
  }

  # Прямой доступ к HAProxy stats для отладки
  ingress {
    protocol       = "TCP"
    description    = "HAProxy stats from my IP"
    v4_cidr_blocks = [local.my_ip_cidr]
    port           = local.haproxy_stats_port
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

# =====================================================
# 3 ВМ - по одной в каждой зоне
# =====================================================
data "yandex_compute_image" "ubuntu" {
  family    = "ubuntu-2204-lts"
  folder_id = "standard-images"
}

resource "yandex_compute_instance" "ha_pg" {
  count = var.vm_count

  name        = "ha-pg-0${count.index + 1}"
  platform_id = var.vm_platform_id
  zone        = var.yandex_zones[count.index]

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
    subnet_id = yandex_vpc_subnet.ha_subnet[count.index].id
    nat       = true
    security_group_ids = [
      yandex_vpc_security_group.ha_internal_sg.id,
      yandex_vpc_security_group.ha_vm_sg.id,
    ]
  }

  metadata = {
    ssh-keys           = "${var.vm_user}:${trimspace(var.ssh_public_key)}"
    serial-port-enable = 1
  }
}

# =====================================================
# Yandex Network Load Balancer
# =====================================================
resource "yandex_lb_target_group" "ha_tg" {
  name = "ha-pg-tg"

  dynamic "target" {
    for_each = yandex_compute_instance.ha_pg
    content {
      subnet_id = yandex_vpc_subnet.ha_subnet[target.key].id
      address   = target.value.network_interface[0].ip_address
    }
  }
}

resource "yandex_lb_network_load_balancer" "ha_nlb" {
  name = "ha-pg-nlb"

  listener {
    name        = "pg-rw"
    port        = local.pg_port         # снаружи 5432
    target_port = local.haproxy_rw_port # внутри 5000 (HAProxy rw)
    external_address_spec {
      ip_version = "ipv4"
    }
  }

  attached_target_group {
    target_group_id = yandex_lb_target_group.ha_tg.id

    healthcheck {
      name                  = "hc-haproxy"
      timeout               = 5
      interval              = 10
      healthy_threshold     = 2
      unhealthy_threshold   = 3
      tcp_options {
        port = local.haproxy_rw_port
      }
    }
  }
}