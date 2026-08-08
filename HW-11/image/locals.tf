locals {
  my_ip_cidr = length(regexall("/", var.my_ip)) > 0 ? var.my_ip : "${var.my_ip}/32"

  internal_cidr = "10.1.0.0/22"

  ssh_port           = 22
  etcd_client_port   = 2379
  etcd_peer_port     = 2380
  pg_port            = 5432
  patroni_port       = 8008
  haproxy_rw_port    = 5000
  haproxy_ro_port    = 5001
  haproxy_stats_port = 7000
}