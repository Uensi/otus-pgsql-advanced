locals {
  my_ip_cidr = length(regexall("/", var.my_ip)) > 0 ? var.my_ip : "${var.my_ip}/32"

  subnet_cidr = "10.1.0.0/24"

  # Yandex Managed PostgreSQL использует порт 6432.
  pg_port = 6432

  # Внутренний порт PostgreSQL может использоваться для служебного трафика.
  pg_internal_port = 5432

  ssh_port = 22
}