output "vm_names" {
  description = "Имена созданных ВМ"
  value       = yandex_compute_instance.yc_vm[*].name
}

output "vm_public_ips" {
  description = "Публичные IP созданных ВМ"
  value       = yandex_compute_instance.yc_vm[*].network_interface[0].nat_ip_address
}

output "vm_private_ips" {
  description = "Приватные IP созданных ВМ"
  value       = yandex_compute_instance.yc_vm[*].network_interface[0].ip_address
}

output "vm_ssh_commands" {
  description = "Команды SSH для подключения к ВМ"
  value = [
    for vm in yandex_compute_instance.yc_vm :
    "ssh ${var.vm_user}@${vm.network_interface[0].nat_ip_address}"
  ]
}

output "vm_info" {
  description = "Информация о ВМ"
  value = [
    for vm in yandex_compute_instance.yc_vm : {
      name       = vm.name
      public_ip  = vm.network_interface[0].nat_ip_address
      private_ip = vm.network_interface[0].ip_address
    }
  ]
}