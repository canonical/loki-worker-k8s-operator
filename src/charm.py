#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service."""

import logging
import os
import re
import socket
from typing import Optional

from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from coordinated_workers.worker import CONFIG_FILE, Worker
from ops.charm import CharmBase
from ops.main import main
from ops.pebble import Layer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CONTAINER_NAME = "loki"
LOKI_PORT = 3100


@trace_charm(
    tracing_endpoint="_charm_tracing_endpoint",
    server_cert="_charm_tracing_cert",
    extra_types=[Worker],
)
class LokiWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Loki."""

    def __init__(self, *args):
        super().__init__(*args)
        self.worker = Worker(
            charm=self,
            name="loki",
            pebble_layer=self.pebble_layer,
            endpoints={"cluster": "loki-cluster"},
            readiness_check_endpoint=self.readiness_check_endpoint,
        )
        self._charm_tracing_endpoint, self._charm_tracing_cert = self.worker.charm_tracing_config()

        self._container = self.unit.get_container(CONTAINER_NAME)
        self.unit.set_ports(LOKI_PORT)

        # === EVENT HANDLER REGISTRATION === #
        self.framework.observe(self.on.loki_pebble_ready, self._on_pebble_ready)  # pyright: ignore

    # === EVENT HANDLERS === #

    def _on_pebble_ready(self, _):
        self.unit.set_workload_version(
            self.version or ""  # pyright: ignore[reportOptionalMemberAccess]
        )

    # === PROPERTIES === #

    @property
    def version(self) -> Optional[str]:
        """Return Loki workload version."""
        if not self._container.can_connect():
            return None

        version_output, _ = self._container.exec(["/bin/loki", "-version"]).wait_output()
        # Output looks like this:
        # Loki, version 2.4.0 (branch: HEAD, revision 32137ee)
        if result := re.search(r"[Vv]ersion:?\s*(\S+)", version_output):
            return result.group(1)
        return None

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
