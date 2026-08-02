# =====================================================
# Data source: спецификации PostgreSQL
# =====================================================
data "cloudru_evolution_postgresql_specification_collection" "specs" {
  version_name = var.sber_pg_version
}

# =====================================================
# PostgreSQL
# =====================================================
resource "cloudru_evolution_postgresql_cluster" "sber_pg_cluster" {
  name        = "pg-cluster-sber"
  description = "PostgreSQL cluster for Sber project"
  project_id  = var.cloudru_project_id

  subnet_id  = var.sber_subnet_id
  subnet_ids = [var.sber_subnet_id]

  version          = var.sber_pg_version
  specification_id = local.sber_specification_id
  instances        = 1

  initial_database = var.db_name

  storage = {
    pg_data_gb = var.sber_pg_data_gb
  }

  backup = {
    schedule              = "0 0 * * 0"
    retention_policy_days = 14
  }
}

resource "cloudru_evolution_postgresql_user" "sber_admin" {
  name       = var.db_user
  password   = var.db_password
  cluster_id = cloudru_evolution_postgresql_cluster.sber_pg_cluster.id

  depends_on = [cloudru_evolution_postgresql_cluster.sber_pg_cluster]
}

# =====================================================
# Security Groups
# =====================================================

resource "cloudru_evolution_compute_security_group" "sber_pg_sg" {
  project_id  = var.cloudru_project_id
  name        = "pg-sg-sber"
  description = "Security group for PostgreSQL"

  zone = {
    name = var.sber_zone
  }
}

resource "cloudru_evolution_compute_security_group_rule" "sber_pg_allow_myip" {
  security_group_id = cloudru_evolution_compute_security_group.sber_pg_sg.id
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "5432:5432"
  remote_ip_prefix  = local.my_ip_cidr
  description       = "Allow PostgreSQL from my IP"
}

resource "cloudru_evolution_compute_security_group_rule" "sber_pg_allow_bastion" {
  security_group_id = cloudru_evolution_compute_security_group.sber_pg_sg.id
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "5432:5432"
  remote_ip_prefix  = var.sber_subnet_cidr
  description       = "Allow PostgreSQL from bastion subnet"
}

resource "cloudru_evolution_compute_security_group" "sber_bastion_sg" {
  project_id  = var.cloudru_project_id
  name        = "bastion-sg-sber"
  description = "Security group for bastion VM"

  zone = {
    name = var.sber_zone
  }
}

resource "cloudru_evolution_compute_security_group_rule" "sber_bastion_ssh" {
  security_group_id = cloudru_evolution_compute_security_group.sber_bastion_sg.id
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "22:22"
  remote_ip_prefix  = local.my_ip_cidr
  description       = "Allow SSH from my IP"
}

resource "cloudru_evolution_compute_security_group_rule" "sber_bastion_egress" {
  security_group_id = cloudru_evolution_compute_security_group.sber_bastion_sg.id
  direction         = "TRAFFIC_DIRECTION_EGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_ANY"
  port_range        = "any"
  remote_ip_prefix  = "0.0.0.0/0"
  description       = "Allow all outbound traffic"
}


# Диск с Ubuntu
resource "cloudru_evolution_compute_disk" "sber_bastion_disk" {
  project_id = var.cloudru_project_id
  name       = "bastion-sber-disk-v2"
  size       = var.sber_disk_size

  disk_type = {
    name = "SSD"
  }

  zone = {
    name = var.sber_zone
  }

  bootable = true

  image = {
    id = var.sber_image_id
  }
}

# ВМ
resource "cloudru_evolution_compute_vm" "sber_bastion" {
  project_id  = var.cloudru_project_id
  name        = "bastion-sber"
  description = "Bastion VM for PostgreSQL access"

  zone = {
    name = var.sber_zone
  }

  flavor = {
    name = var.sber_vm_flavor
  }

  disks = [
    {
      id = cloudru_evolution_compute_disk.sber_bastion_disk.id
    }
  ]

  image_metadata = {
    public_key = {
      string_value = var.ssh_public_key
    }
    name = {
      string_value = var.vm_user
    }
    hostname = {
      string_value = "bastion-sber"
    }
    guest_agent_ready = {
      bool_value = true
    }
  }

  lifecycle {
    ignore_changes = [
      network_interfaces
    ]
  }
}

# Интерфейс с публичным IP
resource "cloudru_evolution_compute_interface" "sber_bastion_interface" {
  project_id = var.cloudru_project_id
  name       = "bastion-sber-interface"

  zone = {
    name = var.sber_zone
  }

  vm = {
    id = cloudru_evolution_compute_vm.sber_bastion.id
  }

  subnet = {
    id = var.sber_subnet_id
  }

  interface_security_enabled = true

  security_groups = [
    {
      id = cloudru_evolution_compute_security_group.sber_bastion_sg.id
    }
  ]

  attach_external_ip = {
    id = var.floating_ip_id
  }

  type = "INTERFACE_TYPE_REGULAR"

  depends_on = [
    cloudru_evolution_compute_vm.sber_bastion
  ]
}