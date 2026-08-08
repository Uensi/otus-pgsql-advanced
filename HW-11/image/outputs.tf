output "ha_vm_info" {
  description = "Ноды кластера: имя, зона, приватный и публичный IP"
  value = [
    for vm in yandex_compute_instance.ha_pg : {
      name       = vm.name
      zone       = vm.zone
      private_ip = vm.network_interface[0].ip_address
      public_ip  = vm.network_interface[0].nat_ip_address
    }
  ]
}