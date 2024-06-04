#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""This module contains an endpoint wrapper class for the requirer side of the ``loki-cluster`` relation.

As this relation is cluster-internal and not intended for third-party charms to interact with `loki-coordinator-k8s`, its only user will be the loki-worker-k8s charm. As such, it does not live in a charm lib as most other relation endpoint wrappers do.
"""

import json
import logging
from enum import Enum, unique
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Set
from urllib.parse import urlparse

import ops
import pydantic
from ops import EventSource, Object, ObjectEvents, RelationCreatedEvent
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("loki_cluster")

DEFAULT_ENDPOINT_NAME = "loki-cluster"
BUILTIN_JUJU_KEYS = {"ingress-address", "private-address", "egress-subnets"}
LOKI_CONFIG_FILE = "/etc/loki/loki-config.yaml"
LOKI_CERT_FILE = "/etc/loki/server.cert"
LOKI_KEY_FILE = "/etc/loki/private.key"
LOKI_CLIENT_CA_FILE = "/etc/loki/ca.cert"


# TODO: inherit enum.StrEnum when jammy is no longer supported.
# https://docs.python.org/3/library/enum.html#enum.StrEnum
@unique
class LokiRole(str, Enum):
    """Loki component role names."""

    # Based on: https://grafana.com/docs/loki/latest/get-started/components/#loki-components
    compactor = "compactor"
    distributor = "distributor"
    index_gateway = "index-gateway"
    ingester = "ingester"
    querier = "querier"
    query_frontend = "query-frontend"
    query_scheduler = "query-scheduler"
    ruler = "ruler"

    # meta-roles
    read = "read"
    write = "write"
    backend = "backend"
    all = "all"


META_ROLES = {
    LokiRole.read: (LokiRole.query_frontend, LokiRole.querier),
    LokiRole.write: (LokiRole.distributor, LokiRole.ingester),
    LokiRole.backend: (
        LokiRole.query_scheduler,
        LokiRole.index_gateway,
        LokiRole.compactor,
        LokiRole.ruler,
    ),
    LokiRole.all: list(LokiRole),
}


def expand_roles(roles: Iterable[LokiRole]) -> Set[LokiRole]:
    """Expand any meta roles to their 'atomic' equivalents."""
    expanded_roles = set()
    for role in roles:
        if role in META_ROLES:
            expanded_roles.update(META_ROLES[role])
        else:
            expanded_roles.add(role)
    return expanded_roles


class ConfigReceivedEvent(ops.EventBase):
    """Event emitted when the "loki-cluster" provider has shared a new loki config."""

    config: Dict[str, Any]
    """The loki config."""

    def __init__(self, handle: ops.framework.Handle, config: Dict[str, Any]):
        super().__init__(handle)
        self.config = config

    def snapshot(self) -> Dict[str, Any]:
        """Used by the framework to serialize the event to disk.

        Not meant to be called by charm code.
        """
        return {"config": json.dumps(self.config)}

    def restore(self, snapshot: Dict[str, Any]):
        """Used by the framework to deserialize the event from disk.

        Not meant to be called by charm code.
        """
        self.relation = json.loads(snapshot["config"])


class LokiClusterError(Exception):
    """Base class for exceptions raised by this module."""


class DataValidationError(LokiClusterError):
    """Raised when relation databag validation fails."""


class DatabagAccessPermissionError(LokiClusterError):
    """Raised when a follower attempts to write leader settings."""


class JujuTopology(pydantic.BaseModel):
    """JujuTopology."""

    model: str
    unit: str
    # ...


class DatabagModel(BaseModel):
    """Base databag model."""

    model_config = ConfigDict(
        # Allow instantiating this class by field name (instead of forcing alias).
        populate_by_name=True,
        # Custom config key: whether to nest the whole datastructure (as json)
        # under a field or spread it out at the toplevel.
        _NEST_UNDER=None,
    )  # type: ignore
    """Pydantic config."""

    @classmethod
    def load(cls, databag: MutableMapping[str, str]):
        """Load this model from a Juju databag."""
        nest_under = cls.model_config.get("_NEST_UNDER")
        if nest_under:
            return cls.parse_obj(json.loads(databag[nest_under]))

        try:
            data = {k: json.loads(v) for k, v in databag.items() if k not in BUILTIN_JUJU_KEYS}
        except json.JSONDecodeError as e:
            msg = f"invalid databag contents: expecting json. {databag}"
            log.info(msg)
            raise DataValidationError(msg) from e

        try:
            return cls.parse_raw(json.dumps(data))  # type: ignore
        except pydantic.ValidationError as e:
            msg = f"failed to validate databag: {databag}"
            log.info(msg, exc_info=True)
            raise DataValidationError(msg) from e

    def dump(self, databag: Optional[MutableMapping[str, str]] = None, clear: bool = True):
        """Write the contents of this model to Juju databag.

        :param databag: the databag to write the data to.
        :param clear: ensure the databag is cleared before writing it.
        """
        if clear and databag:
            databag.clear()

        if databag is None:
            databag = {}
        nest_under = self.model_config.get("_NEST_UNDER")
        if nest_under:
            databag[nest_under] = self.json()

        dct = self.model_dump(by_alias=True)
        for key, field in self.model_fields.items():  # type: ignore
            value = dct[key]
            databag[field.alias or key] = json.dumps(value)
        return databag


class LokiClusterRequirerAppData(DatabagModel):
    """LokiClusterRequirerAppData."""

    roles: List[LokiRole]


class LokiClusterRequirerUnitData(DatabagModel):
    """LokiClusterRequirerUnitData."""

    juju_topology: JujuTopology
    address: str


class LokiClusterProviderAppData(DatabagModel):
    """LokiClusterProviderAppData."""

    loki_config: Dict[str, Any]
    loki_endpoints: Optional[Dict[str, str]] = None


class LokiClusterRemovedEvent(ops.EventBase):
    """Event emitted when the relation with the "loki-cluster" provider has been severed.

    Or when the relation data has been wiped.
    """


class LokiClusterRequirerEvents(ObjectEvents):
    """Events emitted by the LokiClusterRequirer "loki-cluster" endpoint wrapper."""

    config_received = EventSource(ConfigReceivedEvent)
    created = EventSource(RelationCreatedEvent)
    removed = EventSource(LokiClusterRemovedEvent)


class LokiClusterRequirer(Object):
    """``loki-cluster`` requirer endpoint wrapper."""

    on = LokiClusterRequirerEvents()  # type: ignore

    def __init__(
        self,
        charm: ops.CharmBase,
        key: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self.juju_topology = {"unit": self.model.unit.name, "model": self.model.name}
        relation = self.model.get_relation(endpoint)
        # filter out common unhappy relation states
        self.relation: Optional[ops.Relation] = (
            relation if relation and relation.app and relation.data else None
        )

        self.framework.observe(
            self._charm.on[endpoint].relation_changed, self._on_loki_cluster_relation_changed
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_created, self._on_loki_cluster_relation_created
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_broken, self._on_loki_cluster_relation_broken
        )

    def _on_loki_cluster_relation_broken(self, _event: ops.RelationBrokenEvent):
        self.on.removed.emit()

    def _on_loki_cluster_relation_created(self, event: ops.RelationCreatedEvent):
        self.on.created.emit(relation=event.relation, app=event.app, unit=event.unit)

    def _on_loki_cluster_relation_changed(self, _event: ops.RelationChangedEvent):
        # to prevent the event from firing if the relation is in an unhealthy state (breaking...)
        if self.relation:
            new_config = self.get_loki_config()
            if new_config:
                self.on.config_received.emit(new_config)

            # if we have published our data, but we receive an empty/invalid config,
            # then the remote end must have removed it.
            elif self.is_published():
                self.on.removed.emit()

    def is_published(self):
        """Verify that the local side has done all they need to do.

        - unit address is published
        - roles are published
        """
        relation = self.relation
        if not relation:
            return False

        unit_data = relation.data[self._charm.unit]
        app_data = relation.data[self._charm.app]

        try:
            LokiClusterRequirerUnitData.load(unit_data)
            LokiClusterRequirerAppData.load(app_data)
        except DataValidationError as e:
            log.info(f"invalid databag contents: {e}")
            return False
        return True

    def publish_unit_address(self, url: str):
        """Publish this unit's URL via the unit databag."""
        try:
            urlparse(url)
        except Exception as e:
            raise ValueError(f"{url} is an invalid url") from e

        databag_model = LokiClusterRequirerUnitData(
            juju_topology=self.juju_topology,  # type: ignore
            address=url,
        )
        relation = self.relation
        if relation:
            unit_databag = relation.data[self.model.unit]  # type: ignore # all checks are done in __init__
            databag_model.dump(unit_databag)

    def publish_app_roles(self, roles: Iterable[LokiRole]):
        """Publish this application's roles via the application databag."""
        if not self._charm.unit.is_leader():
            raise DatabagAccessPermissionError("only the leader unit can publish roles.")

        relation = self.relation
        if relation:
            deduplicated_roles = list(expand_roles(roles))
            databag_model = LokiClusterRequirerAppData(roles=deduplicated_roles)
            databag_model.dump(relation.data[self.model.app])

    def _get_data_from_coordinator(self) -> Optional[LokiClusterProviderAppData]:
        """Fetch the contents of the doordinator databag."""
        data: Optional[LokiClusterProviderAppData] = None
        relation = self.relation
        if relation:
            try:
                databag = relation.data[relation.app]  # type: ignore # all checks are done in __init__
                coordinator_databag = LokiClusterProviderAppData.load(databag)
                data = coordinator_databag
            except DataValidationError as e:
                log.info(f"invalid databag contents: {e}")

        return data

    def get_loki_config(self) -> Dict[str, Any]:
        """Fetch the loki config from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.loki_config
        return {}

    def get_loki_endpoints(self) -> Dict[str, str]:
        """Fetch the loki endpoints from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.loki_endpoints or {}
        return {}

    def get_cert_secret_ids(self) -> Optional[str]:
        """Fetch certificates secrets ids for the loki config."""
        if self.relation and self.relation.app:
            return self.relation.data[self.relation.app].get("secrets", None)
