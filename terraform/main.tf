resource "juju_application" "loki_worker" {
  name               = var.app_name
  config             = var.config
  constraints        = var.constraints
  model              = var.model
  storage_directives = var.storage_directives
  trust              = true
  units              = var.units

  charm {
    name     = "loki-worker-k8s"
    channel  = var.channel
    revision = var.revision
  }
}
