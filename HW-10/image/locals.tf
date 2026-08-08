locals {
  my_ip_cidr = length(regexall("/", var.my_ip)) > 0 ? var.my_ip : "${var.my_ip}/32"

  ssh_port = 22
}