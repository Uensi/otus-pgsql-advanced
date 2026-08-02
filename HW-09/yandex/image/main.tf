# =====================================================
# Сеть и подсеть
# =====================================================

resource "yandex_vpc_network" "yc_network" {
  name = "pg-network-yc"
}

resource "yandex_vpc_subnet" "yc_subnet" {
  name           = "pg-subnet-yc"
  zone           = var.yandex_zone
  network_id     = yandex_vpc_network.yc_network.id
  v4_cidr_blocks = [local.subnet_cidr]
}

# =====================================================
# Security groups
# =====================================================

# Внутренняя группа безопасности.
# Разрешает весь трафик внутри подсети.
# Удобно для бастиона, кластера БД и будущих внутренних сервисов.
resource "yandex_vpc_security_group" "yc_internal_sg" {
  name       = "internal-sg-yc"
  network_id = yandex_vpc_network.yc_network.id

  ingress {
    protocol       = "ANY"
    description    = "Allow all traffic inside private subnet"
    v4_cidr_blocks = [local.subnet_cidr]
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

# Группа безопасности для ВМ-бастиона.
resource "yandex_vpc_security_group" "yc_bastion_sg" {
  name       = "bastion-sg-yc"
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

# Группа безопасности для PostgreSQL.
# Yandex Managed PostgreSQL использует порт 6432 [[1]].
resource "yandex_vpc_security_group" "yc_pg_sg" {
  name       = "pg-sg-yc"
  network_id = yandex_vpc_network.yc_network.id

  ingress {
    protocol          = "TCP"
    description       = "PostgreSQL from bastion security group"
    security_group_id = yandex_vpc_security_group.yc_bastion_sg.id
    port              = local.pg_port
  }

  ingress {
    protocol       = "TCP"
    description    = "PostgreSQL from private subnet"
    v4_cidr_blocks = [local.subnet_cidr]
    port           = local.pg_port
  }

  ingress {
    protocol       = "TCP"
    description    = "PostgreSQL internal traffic from private subnet"
    v4_cidr_blocks = [local.subnet_cidr]
    port           = local.pg_internal_port
  }

  # Публичный доступ к PostgreSQL нужен только если вы действительно
  # хотите подключаться напрямую из интернета.
  # Для безопасной работы лучше использовать бастион или VPN.
  dynamic "ingress" {
    for_each = var.db_public_access ? [1] : []

    content {
      protocol       = "TCP"
      description    = "PostgreSQL from my IP"
      v4_cidr_blocks = [local.my_ip_cidr]
      port           = local.pg_port
    }
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

# =====================================================
# Yandex Managed PostgreSQL
# =====================================================

resource "yandex_mdb_postgresql_cluster" "yc_pg_cluster" {
  name        = "pg-cluster-yc"
  environment = "PRODUCTION"
  network_id  = yandex_vpc_network.yc_network.id

  security_group_ids = [
    yandex_vpc_security_group.yc_pg_sg.id,
    yandex_vpc_security_group.yc_internal_sg.id,
  ]

  config {
    version = var.db_version

    resources {
      resource_preset_id = "s2.micro"
      disk_type_id       = "network-ssd"
      disk_size          = 10
    }
  }

  host {
    zone             = var.yandex_zone
    subnet_id        = yandex_vpc_subnet.yc_subnet.id
    assign_public_ip = var.db_public_access
  }
}

resource "yandex_mdb_postgresql_user" "yc_admin" {
  cluster_id = yandex_mdb_postgresql_cluster.yc_pg_cluster.id
  name       = var.db_user
  password   = var.db_password
}

resource "yandex_mdb_postgresql_database" "yc_db" {
  cluster_id = yandex_mdb_postgresql_cluster.yc_pg_cluster.id
  name       = var.db_name
  owner      = yandex_mdb_postgresql_user.yc_admin.name

  depends_on = [
    yandex_mdb_postgresql_user.yc_admin
  ]
}

# Образ Ubuntu и ВМ-бастион

data "yandex_compute_image" "ubuntu" {
  family    = "ubuntu-2204-lts"
  folder_id = "standard-images"
}

resource "yandex_compute_instance" "yc_bastion" {
  name        = "bastion-yc"
  platform_id = "standard-v2"
  zone        = var.yandex_zone

  allow_stopping_for_update = true

  resources {
    cores  = 2
    memory = 2
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.id
      size     = 20
      type     = "network-ssd"
    }
  }

  network_interface {
    subnet_id = yandex_vpc_subnet.yc_subnet.id
    nat       = true

    security_group_ids = [
      yandex_vpc_security_group.yc_bastion_sg.id,
      yandex_vpc_security_group.yc_internal_sg.id,
    ]
  }

  metadata = {
    ssh-keys           = "${var.vm_user}:${var.ssh_public_key}"
    serial-port-enable = 1
  }
}