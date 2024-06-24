#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Loki workload manager class."""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loki_cluster import (
    LokiRole,
)
from ops import Container, Model
from ops.pebble import ChangeError, PathError, ProtocolError

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CONTAINER_NAME = "loki"
LOKI_PORT = 3100
LOKI_CONFIG_FILE = "/etc/loki/loki-config.yaml"
LOKI_CERT_FILE = "/etc/loki/server.cert"
LOKI_KEY_FILE = "/etc/loki/private.key"
LOKI_CLIENT_CA_FILE = "/etc/loki/ca.cert"


class Loki:
    """Loki workload container facade."""

    _name = CONTAINER_NAME
    port = LOKI_PORT

    def __init__(self, container: Container, roles: List[LokiRole], model: Model):
        self._container = container
        self._roles = roles
        self._model = model

    @property
    def pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        targets = ",".join(sorted(self._roles))

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

    @property
    def running_config(self) -> Optional[dict]:
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

    def update_config(self, config: Dict[str, Any]) -> bool:
        """Set Loki config.

        Returns: True if config has changed, otherwise False.
        Raises: BlockedStatusError exception if PebbleError, ProtocolError, PathError exceptions
            are raised by container.remove_path
        """
        if not self._container.can_connect():
            logger.debug("Could not connect to Loki container")
            return False

        if not config:
            logger.warning("cannot update loki config: coordinator hasn't published one yet.")
            return False

        if self.running_config != config:
            config_as_yaml = yaml.safe_dump(config)
            self._container.push(LOKI_CONFIG_FILE, config_as_yaml, make_dirs=True)
            logger.info("Pushed new Loki configuration")
            return True

        return False

    def update_tls_certificates(self, cert_secrets) -> bool:
        """Update TLS certificates."""
        if not self._container.can_connect():
            return False

        ca_cert_path = Path("/usr/local/share/ca-certificates/ca.crt")

        if cert_secrets:
            cert_secrets = json.loads(cert_secrets)

            private_key_secret = self._model.get_secret(id=cert_secrets["private_key_secret_id"])
            private_key = private_key_secret.get_content().get("private-key")

            ca_server_secret = self._model.get_secret(id=cert_secrets["ca_server_cert_secret_id"])
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

    def restart(self):
        """Restart the pebble service or start if not already running."""
        if not self._container.exists(LOKI_CONFIG_FILE):
            logger.error("cannot restart loki: config file doesn't exist (yet).")

        if not self._roles:
            logger.debug("cannot restart loki: no roles have been configured.")
            return

        try:
            if self._container.get_service(CONTAINER_NAME).is_running():
                self._container.restart(CONTAINER_NAME)
            else:
                self._container.start(CONTAINER_NAME)
        except ChangeError as e:
            logger.error(f"failed to (re)start loki job: {e}", exc_info=True)
            return
