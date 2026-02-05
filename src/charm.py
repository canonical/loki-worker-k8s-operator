#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service."""

import logging
import os
import socket

import tenacity
from coordinated_workers.worker import CONFIG_FILE, Worker
from ops.charm import CharmBase
from ops.main import main
from ops.pebble import Layer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CONTAINER_NAME = "loki"
LOKI_PORT = 3100

_LEGACY_WORKER_PORTS = [LOKI_PORT]


class LokiWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Loki."""

    def __init__(self, *args):
        super().__init__(*args)
        # Override waiting times from the generic worker
        Worker.SERVICE_START_RETRY_STOP = tenacity.stop_after_delay(60)
        Worker.SERVICE_START_RETRY_WAIT = tenacity.wait_fixed(5)

        self.worker = Worker(
            charm=self,
            # name of the container the worker is operating on AND of the executable
            name=CONTAINER_NAME,
            pebble_layer=self.pebble_layer,
            endpoints={"cluster": "loki-cluster"},
            readiness_check_endpoint=self.readiness_check_endpoint,
            resources_requests=lambda _: {"cpu": "50m", "memory": "200Mi"},
            # container we want to resource-patch
            container_name=CONTAINER_NAME,
        )

        self._container = self.unit.get_container(CONTAINER_NAME)

        # if the worker has received some ports from the coordinator,
        # it's in charge of ensuring they're opened.
        if not self.worker.cluster.get_worker_ports():
            # legacy behaviour fallback: older interface versions didn't tell us which
            # ports we should be opening, so we opened all of them.
            # This can happen when talking to an old coordinator revision, or
            # if the coordinator hasn't published its data yet.
            logger.info(
                "Cluster interface hasn't published a list of worker ports (yet?). "
                "If this issue persists after the cluster has settled, you should "
                "upgrade the coordinator to a newer revision. Falling back now to "
                "legacy behaviour and opening all ports."
            )
            self.unit.set_ports(*_LEGACY_WORKER_PORTS)

    # === UTILITY METHODS === #

    @staticmethod
    def readiness_check_endpoint(worker: Worker) -> str:
        """Endpoint for worker readiness checks."""
        scheme = "https" if worker.cluster.get_tls_data() else "http"
        return f"{scheme}://{socket.getfqdn()}:{LOKI_PORT}/ready"

    def pebble_layer(self, worker: Worker) -> Layer:
        """Return a dictionary representing a Pebble layer."""
        targets = ",".join(sorted(worker.roles))

        env = {}
        # add proxy variables
        env.update(
            {
                "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
                "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
                "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY", ""),
            }
        )
        # configure workload traces
        if tempo_endpoint := worker.cluster.get_workload_tracing_receivers().get(
            "jaeger_thrift_http", None
        ):
            topology = worker.cluster.juju_topology
            env.update(
                {
                    "JAEGER_ENDPOINT": (f"{tempo_endpoint}/api/traces?format=jaeger.thrift"),
                    "JAEGER_SAMPLER_PARAM": "1",
                    "JAEGER_SAMPLER_TYPE": "const",
                    "JAEGER_TAGS": f"juju_application={topology.application},juju_model={topology.model}"
                    + f",juju_model_uuid={topology.model_uuid},juju_unit={topology.unit},juju_charm={topology.charm_name}",
                },
            )
        return Layer(
            {
                "summary": "loki worker layer",
                "description": "pebble config layer for loki worker",
                "services": {
                    "loki": {
                        "override": "replace",
                        "summary": "loki worker daemon",
                        "command": f"/bin/loki --config.file={CONFIG_FILE} -target {targets}",
                        "startup": "enabled",
                        "environment": env,
                    }
                },
            }
        )


if __name__ == "__main__":  # pragma: nocover
    main(LokiWorkerK8SOperatorCharm)
