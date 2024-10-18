resource "juju_application" "loki_worker" {
  name = var.app_name
  # Coordinator and worker must be in the same model
  model = var.model_name
  trust = true # We always need this variable to be true in order to be able to apply resources limits. 

  charm {
    name     = "loki-worker-k8s"
    channel  = var.channel
    revision = var.revision
  }
  units  = var.units
  config = var.config
}
