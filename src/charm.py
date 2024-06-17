#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service."""

import json
import logging
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from charms.loki_k8s.v1.loki_push_api import _PebbleLogClient
from cosl import JujuTopology
from loki_cluster import (
    LOKI_CERT_FILE,
    LOKI_CLIENT_CA_FILE,
    LOKI_CONFIG_FILE,
    LOKI_KEY_FILE,
    ConfigReceivedEvent,
    LokiClusterRequirer,
    LokiRole,
)
from ops import pebble
from ops.charm import CharmBase, CollectStatusEvent
from ops.framework import BoundEvent, Object
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import PathError, ProtocolError

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class LokiWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Loki."""

    _name = "loki"
    _port = 3100

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container(self._name)
        self.topology = JujuTopology.from_charm(self)
        self.loki_cluster = LokiClusterRequirer(self)
        self.log_forwarder = ManualLogForwarder(
            charm=self,
            loki_endpoints=self.loki_cluster.get_loki_endpoints(),
            refresh_events=[
                self.on["loki-cluster"].relation_joined,
                self.on["loki-cluster"].relation_changed,
            ],
        )
        self.unit.set_ports(self._port)

        # === EVENT HANDLER REGISTRATION === #
        self.framework.observe(self.on.loki_pebble_ready, self._on_pebble_ready)  # pyright: ignore
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.loki_cluster.on.config_received, self._on_loki_config_received)
        self.framework.observe(self.loki_cluster.on.created, self._on_loki_cluster_created)
        self.framework.observe(
            self.on.loki_cluster_relation_departed, self.log_forwarder.disable_logging
        )
        self.framework.observe(
            self.on.loki_cluster_relation_broken, self.log_forwarder.disable_logging
        )

    # === EVENT HANDLERS === #
    def _on_pebble_ready(self, _):
        self.unit.set_workload_version(self._loki_version or "")
        self._update_config()

    def _on_config_changed(self, _):
        # if the user has changed the roles, we might need to let the coordinator know
        self._update_loki_cluster()

        # if we have a config, we can start loki
        if self.loki_cluster.get_loki_config():
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
        elif not self.loki_cluster.relation:
            e.add_status(WaitingStatus("Loki-Cluster relation not ready"))
        if not self.loki_cluster.get_loki_config():
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
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        targets = ",".join(sorted(self._loki_roles))

        return {
            "summary": "loki worker layer",
            "description": "pebble config layer for loki worker",
            "services": {
                "loki": {
                    "override": "replace",
                    "summary": "loki worker daemon",
                    "command": f"/bin/loki --config.file={LOKI_CONFIG_FILE} -target {targets} -auth.multitenancy-enabled=false",
                    "startup": "enabled",
                }
            },
        }

    @property
    def _loki_roles(self) -> List[LokiRole]:
        """Return a set of the roles this Loki worker should take on."""
        roles: List[LokiRole] = [role for role in LokiRole if self.config[role] is True]
        return roles

    @property
    def _loki_version(self) -> Optional[str]:
        if not self._container.can_connect():
            return None

        version_output, _ = self._container.exec(["/bin/loki", "-version"]).wait_output()
        # Output looks like this:
        # Loki, version 2.4.0 (branch: HEAD, revision 32137ee)
        if result := re.search(r"[Vv]ersion:?\s*(\S+)", version_output):
            return result.group(1)
        return None

    @property
    def server_cert_path(self) -> Optional[str]:
        """Server certificate path for tls tracing."""
        return LOKI_CERT_FILE

    # === UTILITY METHODS === #

    def _update_loki_cluster(self):
        """Share via loki-cluster all information we need to publish."""
        self.loki_cluster.publish_unit_address(socket.getfqdn())
        if self.unit.is_leader() and self._loki_roles:
            logger.info(f"Publishing Loki roles: {self._loki_roles} to relation databag.")
            self.loki_cluster.publish_app_roles(self._loki_roles)

    def _update_config(self):
        """Update the loki config and restart the workload if necessary."""
        restart = any(
            [
                self._update_tls_certificates(),
                self._update_loki_config(),
                self._set_pebble_layer(),
            ]
        )

        if restart:
            self.restart()

    def _set_pebble_layer(self) -> bool:
        """Set Pebble layer.

        Returns: True if Pebble layer was added, otherwise False.
        """
        if not self._container.can_connect():
            return False
        if not self._loki_roles:
            return False

        current_layer = self._container.get_plan()
        new_layer = self._pebble_layer

        if (
            "services" not in current_layer.to_dict()
            or current_layer.services != new_layer["services"]
        ):
            self._container.add_layer(self._name, new_layer, combine=True)  # pyright: ignore
            return True

        return False

    def _update_tls_certificates(self) -> bool:
        if not self._container.can_connect():
            return False

        ca_cert_path = Path("/usr/local/share/ca-certificates/ca.crt")

        if cert_secrets := self.loki_cluster.get_cert_secret_ids():
            cert_secrets = json.loads(cert_secrets)

            private_key_secret = self.model.get_secret(id=cert_secrets["private_key_secret_id"])
            private_key = private_key_secret.get_content().get("private-key")

            ca_server_secret = self.model.get_secret(id=cert_secrets["ca_server_cert_secret_id"])
            ca_cert = ca_server_secret.get_content().get("ca-cert")
            server_cert = ca_server_secret.get_content().get("server-cert")

            # Save the workload certificates
            self._container.push(LOKI_CERT_FILE, server_cert or "", make_dirs=True)
            self._container.push(LOKI_KEY_FILE, private_key or "", make_dirs=True)
            self._container.push(LOKI_CLIENT_CA_FILE, ca_cert or "", make_dirs=True)
            self._container.push(ca_cert_path, ca_cert or "", make_dirs=True)

            self._container.exec(["update-ca-certificates", "--fresh"]).wait()
            subprocess.run(["update-ca-certificates", "--fresh"])

            return True
        else:
            self._container.remove_path(LOKI_CERT_FILE, recursive=True)
            self._container.remove_path(LOKI_KEY_FILE, recursive=True)
            self._container.remove_path(LOKI_CLIENT_CA_FILE, recursive=True)
            self._container.remove_path(ca_cert_path, recursive=True)

            ca_cert_path.unlink(missing_ok=True)
            self._container.exec(["update-ca-certificates", "--fresh"]).wait()
            subprocess.run(["update-ca-certificates", "--fresh"])

            return True

    def _update_loki_config(self) -> bool:
        """Set Loki config.

        Returns: True if config has changed, otherwise False.
        Raises: BlockedStatusError exception if PebbleError, ProtocolError, PathError exceptions
            are raised by container.remove_path
        """
        loki_config = self.loki_cluster.get_loki_config()
        if not loki_config:
            logger.warning("cannot update loki config: coordinator hasn't published one yet.")
            return False

        if self._running_loki_config() != loki_config:
            config_as_yaml = yaml.safe_dump(loki_config)
            self._container.push(LOKI_CONFIG_FILE, config_as_yaml, make_dirs=True)
            logger.info("Pushed new Loki configuration")
            return True

        return False

    def _running_loki_config(self) -> Optional[dict]:
        """Return the Loki config as dict, or None if retrieval failed."""
        if not self._container.can_connect():
            logger.debug("Could not connect to Loki container")
            return None

        try:
            raw_current = self._container.pull(LOKI_CONFIG_FILE).read()
            return yaml.safe_load(raw_current)
        except (ProtocolError, PathError) as e:
            logger.warning(
                "Could not check the current Loki configuration due to "
                "a failure in retrieving the file: %s",
                e,
            )
            return None

    def restart(self):
        """Restart the pebble service or start if not already running."""
        if not self._container.exists(LOKI_CONFIG_FILE):
            logger.error("cannot restart loki: config file doesn't exist (yet).")

        if not self._loki_roles:
            logger.debug("cannot restart loki: no roles have been configured.")
            return

        try:
            if self._container.get_service(self._name).is_running():
                self._container.restart(self._name)
            else:
                self._container.start(self._name)
        except pebble.ChangeError as e:
            logger.error(f"failed to (re)start loki job: {e}", exc_info=True)
            return


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
