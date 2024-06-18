#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service."""

import logging
import socket
from typing import Dict, List, Optional

from charms.loki_k8s.v1.loki_push_api import _PebbleLogClient
from cosl import JujuTopology
from loki import (
    CONTAINER_NAME,
    LOKI_CERT_FILE,
    Loki,
)
from loki_cluster import (
    ConfigReceivedEvent,
    LokiClusterRequirer,
    LokiRole,
)
from ops.charm import CharmBase, CollectStatusEvent
from ops.framework import BoundEvent, Object
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class LokiWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Loki."""

    _loki = None

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container(CONTAINER_NAME)
        self._loki = Loki(self._container, self._loki_roles, self.model)
        self._topology = JujuTopology.from_charm(self)
        self._loki_cluster = LokiClusterRequirer(self)
        self._log_forwarder = ManualLogForwarder(
            charm=self,
            loki_endpoints=self._loki_cluster.get_loki_endpoints(),
            refresh_events=[
                self.on["loki-cluster"].relation_joined,
                self.on["loki-cluster"].relation_changed,
            ],
        )
        self.unit.set_ports(self._loki.port)

        # === EVENT HANDLER REGISTRATION === #
        self.framework.observe(self.on.loki_pebble_ready, self._on_pebble_ready)  # pyright: ignore
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(
            self._loki_cluster.on.config_received, self._on_loki_config_received
        )
        self.framework.observe(self._loki_cluster.on.created, self._on_loki_cluster_created)
        self.framework.observe(
            self.on.loki_cluster_relation_departed, self._log_forwarder.disable_logging
        )
        self.framework.observe(
            self.on.loki_cluster_relation_broken, self._log_forwarder.disable_logging
        )

    # === EVENT HANDLERS === #
    def _on_pebble_ready(self, _):
        self.unit.set_workload_version(
            self._loki.version or ""  # pyright: ignore[reportOptionalMemberAccess]
        )
        self._update_config()

    def _on_config_changed(self, _):
        # if the user has changed the roles, we might need to let the coordinator know
        self._update_loki_cluster()

        # if we have a config, we can start loki
        if self._loki_cluster.get_loki_config():
            # determine if a workload restart is necessary
            self._update_config()

    def _on_upgrade_charm(self, _):
        self._update_loki_cluster()

    def _on_collect_status(self, e: CollectStatusEvent):
        if not self._container.can_connect():
            e.add_status(WaitingStatus("Waiting for `loki` container"))
        if not self.model.get_relation("loki-cluster"):
            e.add_status(
                BlockedStatus("Missing loki-cluster relation to a loki-coordinator charm")
            )
        elif not self._loki_cluster.relation:
            e.add_status(WaitingStatus("Loki-Cluster relation not ready"))
        if not self._loki_cluster.get_loki_config():
            e.add_status(WaitingStatus("Waiting for coordinator to publish a loki config"))
        if not self._loki_roles:
            e.add_status(BlockedStatus("No roles assigned: please configure some roles"))
        e.add_status(ActiveStatus(""))

    def _on_loki_cluster_created(self, _):
        self._update_loki_cluster()

    def _on_loki_config_received(self, _e: ConfigReceivedEvent):
        self._update_config()

    # === PROPERTIES === #

    @property
    def _loki_roles(self) -> List[LokiRole]:
        """Return a set of the roles this Loki worker should take on."""
        roles: List[LokiRole] = [role for role in LokiRole if self.config[role] is True]
        return roles

    @property
    def server_cert_path(self) -> Optional[str]:
        """Server certificate path for tls tracing."""
        return LOKI_CERT_FILE

    # === UTILITY METHODS === #

    def _update_loki_cluster(self):
        """Share via loki-cluster all information we need to publish."""
        self._loki_cluster.publish_unit_address(socket.getfqdn())
        if self.unit.is_leader() and self._loki_roles:
            logger.info(f"Publishing Loki roles: {self._loki_roles} to relation databag.")
            self._loki_cluster.publish_app_roles(self._loki_roles)

    def _update_config(self):
        """Update the loki config and restart the workload if necessary."""
        cert_secrets = self._loki_cluster.get_cert_secret_ids()
        loki_config = self._loki_cluster.get_loki_config()
        restart = any(
            [
                self._loki.update_tls_certificates(  # pyright: ignore[reportOptionalMemberAccess]
                    cert_secrets
                ),
                self._loki.update_config(  # pyright: ignore[reportOptionalMemberAccess]
                    loki_config
                ),
                self._set_pebble_layer(),
            ]
        )

        if restart:
            self._loki.restart()  # pyright: ignore[reportOptionalMemberAccess]

    def _set_pebble_layer(self) -> bool:
        """Set Pebble layer.

        Returns: True if Pebble layer was added, otherwise False.
        """
        if not self._container.can_connect():
            return False
        if not self._loki_roles:
            return False

        current_layer = self._container.get_plan()
        new_layer = self._loki.pebble_layer  # pyright: ignore[reportOptionalMemberAccess]

        if (
            "services" not in current_layer.to_dict()
            or current_layer.services != new_layer["services"]
        ):
            self._container.add_layer(CONTAINER_NAME, new_layer, combine=True)  # pyright: ignore
            return True

        return False


class ManualLogForwarder(Object):
    """Forward the standard outputs of all workloads to explictly-provided Loki endpoints."""

    def __init__(
        self,
        charm: CharmBase,
        *,
        loki_endpoints: Optional[Dict[str, str]],
        refresh_events: Optional[List[BoundEvent]] = None,
    ):
        _PebbleLogClient.check_juju_version()
        super().__init__(charm, "loki-cluster")
        self._charm = charm
        self._loki_endpoints = loki_endpoints
        self._topology = JujuTopology.from_charm(charm)

        if not refresh_events:
            return

        for event in refresh_events:
            self.framework.observe(event, self.update_logging)

    def update_logging(self, _=None):
        """Update the log forwarding to match the active Loki endpoints."""
        loki_endpoints = self._loki_endpoints

        if not loki_endpoints:
            logger.warning("No Loki endpoints available")
            loki_endpoints = {}

        for container in self._charm.unit.containers.values():
            _PebbleLogClient.disable_inactive_endpoints(
                container=container,
                active_endpoints=loki_endpoints,
                topology=self._topology,
            )
            _PebbleLogClient.enable_endpoints(
                container=container, active_endpoints=loki_endpoints, topology=self._topology
            )

    def disable_logging(self, _=None):
        """Disable all log forwarding."""
        # This is currently necessary because, after a relation broken, the charm can still see
        # the Loki endpoints in the relation data.
        for container in self._charm.unit.containers.values():
            _PebbleLogClient.disable_inactive_endpoints(
                container=container, active_endpoints={}, topology=self._topology
            )


if __name__ == "__main__":  # pragma: nocover
    main(LokiWorkerK8SOperatorCharm)
