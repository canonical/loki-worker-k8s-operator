output "app_name" {
  value = juju_application.loki_worker.name
}

output "provides" {
  value = {
  }
}

output "requires" {
  value = {
    loki_cluster = "loki-cluster"
  }
}
