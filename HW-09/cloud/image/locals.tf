locals {
  my_ip_cidr = length(regexall("/", var.my_ip)) > 0 ? var.my_ip : "${var.my_ip}/32"

  sber_specification_id = (
    var.sber_specification_id != null && var.sber_specification_id != ""
    ? var.sber_specification_id
    : data.cloudru_evolution_postgresql_specification_collection.specs.specifications[0].id
  )
}