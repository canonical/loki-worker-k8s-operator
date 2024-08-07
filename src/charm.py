#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service."""

import logging
import re
from typing import Optional

from cosl.coordinated_workers.worker import CERT_FILE, CONFIG_FILE, Worker
from ops.charm import CharmBase
from ops.main import main
from ops.pebble import Layer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CONTAINER_NAME = "loki"
LOKI_PORT = 3100


class LokiWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Loki."""

    def __init__(self, *args):
        super().__init__(*args)
        self._worker = Worker(
            charm=self,
            name="loki",
            pebble_layer=self.pebble_layer,
            endpoints={"cluster": "loki-cluster"},
        )
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
    def server_cert_path(self) -> Optional[str]:
        """Server certificate path for tls tracing."""
        return CERT_FILE

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        if self._worker.tracing.is_ready():
            return self._worker.tracing.get_endpoint(protocol="otlp_http")

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

    def pebble_layer(self, worker: Worker) -> Layer:
        """Return a dictionary representing a Pebble layer."""
        targets = ",".join(sorted(worker.roles))

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
                    }
                },
            }
        )


if __name__ == "__main__":  # pragma: nocover
    main(LokiWorkerK8SOperatorCharm)
