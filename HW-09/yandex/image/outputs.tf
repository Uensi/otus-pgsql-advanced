output "yc_cluster_host" {
  description = "Хост Yandex Cloud PostgreSQL"
  value       = yandex_mdb_postgresql_cluster.yc_pg_cluster.host[0].fqdn
}

output "yc_cluster_port" {
  description = "Порт Yandex Cloud PostgreSQL"
  value       = local.pg_port
}

output "yc_connection_string" {
  description = "Строка подключения Yandex Cloud PostgreSQL"
  value       = "postgresql://${var.db_user}:${var.db_password}@${yandex_mdb_postgresql_cluster.yc_pg_cluster.host[0].fqdn}:${local.pg_port}/${var.db_name}?sslmode=require"
  sensitive   = true
}

output "yc_psql_command" {
  description = "Команда psql для подключения"
  value       = "psql \"host=${yandex_mdb_postgresql_cluster.yc_pg_cluster.host[0].fqdn} port=${local.pg_port} dbname=${var.db_name} user=${var.db_user} sslmode=require\""
}

output "yc_bastion_ip" {
  description = "Публичный IP ВМ-бастиона Yandex Cloud"
  value       = yandex_compute_instance.yc_bastion.network_interface[0].nat_ip_address
}

output "yc_bastion_ssh" {
  description = "SSH команда для подключения к бастиону Yandex Cloud"
  value       = "ssh ${var.vm_user}@${yandex_compute_instance.yc_bastion.network_interface[0].nat_ip_address}"
}