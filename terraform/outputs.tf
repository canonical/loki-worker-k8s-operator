output "app_name" {
  value = juju_application.loki_worker.name
}

output "endpoints" {
  value = {
    loki_cluster = "loki-cluster"
  }
}
