output "app_name" {
  value = juju_application.loki_worker.name
}

output "requires" {
  value = {
    loki_cluster = "loki-cluster"
  }
}
