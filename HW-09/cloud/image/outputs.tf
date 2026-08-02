output "sber_cluster_host" {
  description = "Хост PostgreSQL (только IP:порт)"
  value       = cloudru_evolution_postgresql_cluster.sber_pg_cluster.connection_string
}

output "sber_connection_string" {
  description = "Строка подключения PostgreSQL"
  value       = "postgresql://${var.db_user}:${var.db_password}@${replace(replace(cloudru_evolution_postgresql_cluster.sber_pg_cluster.connection_string, "postgresql://", ""), "/${var.db_name}", "")}:5432/${var.db_name}"
  sensitive   = true
}

output "sber_bastion_internal_ip" {
  description = "Внутренний IP ВМ-бастиона"
  value       = cloudru_evolution_compute_interface.sber_bastion_interface.ip_address
}

output "sber_bastion_ip" {
  description = "Публичный IP ВМ-бастиона"
  value = try(
    cloudru_evolution_compute_interface.sber_bastion_interface.attach_external_ip.ip_address,
    "IP ещё не привязан"
  )
}

output "sber_bastion_ssh" {
  description = "SSH команда"
  value = try(
    "ssh ${var.vm_user}@${cloudru_evolution_compute_interface.sber_bastion_interface.attach_external_ip.ip_address}",
    "IP ещё не привязан"
  )
}