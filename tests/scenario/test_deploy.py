# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import json

import pytest
from loki_cluster import LokiClusterRequirerAppData, LokiRole
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from scenario import Container, ExecOutput, Relation, State

from tests.scenario.conftest import LOKI_VERSION_EXEC_OUTPUT


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_cannot_connect_no_relation(ctx, evt):
    state_out = ctx.run(evt, state=State(containers=[Container("loki", can_connect=False)]))
    assert state_out.unit_status == BlockedStatus(
        "Missing loki-cluster relation to a loki-coordinator charm"
    )


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_cannot_connect(ctx, evt):
    container = Container(
        "loki",
        can_connect=False,
        exec_mock={
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        evt,
        state=State(
            config={"read": True},
            containers=[container],
            relations=[Relation("loki-cluster")],
        ),
    )
    assert state_out.unit_status == WaitingStatus("Waiting for `loki` container")


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_no_config(ctx, evt):
    state_out = ctx.run(
        evt,
        state=State(
            containers=[Container("loki", can_connect=True)],
            relations=[Relation("loki-cluster")],
        ),
    )
    assert state_out.unit_status == BlockedStatus("No roles assigned: please configure some roles")


@pytest.mark.parametrize(
    "roles",
    (
        ["read"],
        ["write"],
        ["backend"],
        ["all"],
    ),
)
def test_pebble_ready_plan(ctx, roles):
    expected_plan = {
        "services": {
            "loki": {
                "override": "replace",
                "summary": "loki worker daemon",
                "command": f"/bin/loki --config.file=/etc/loki/loki-config.yaml -target {','.join(sorted(roles))} -auth.multitenancy-enabled=false",
                "startup": "enabled",
            }
        },
    }

    loki_container = Container(
        "loki",
        can_connect=True,
        exec_mock={
            ("/bin/loki", "-version"): LOKI_VERSION_EXEC_OUTPUT,
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        loki_container.pebble_ready_event,
        state=State(
            config={role: True for role in roles},
            containers=[loki_container],
            relations=[
                Relation(
                    "loki-cluster",
                    remote_app_data={
                        "loki_config": json.dumps({"alive": "beef"}),
                    },
                )
            ],
        ),
    )

    loki_container_out = state_out.get_container(loki_container)
    assert loki_container_out.services.get("loki").is_running() is True
    assert loki_container_out.plan.to_dict() == expected_plan

    assert state_out.unit_status == ActiveStatus("")


@pytest.mark.parametrize(
    "roles_config, expected",
    (
        (["read"], [LokiRole.read]),
        (["write"], [LokiRole.write]),
        (["backend"], [LokiRole.backend]),
        (["all"], [LokiRole.all]),
    ),
)
def test_roles(ctx, roles_config, expected):
    out = ctx.run(
        "config-changed",
        state=State(
            leader=True,
            config={x: True for x in roles_config},
            containers=[Container("loki", can_connect=True)],
            relations=[Relation("loki-cluster")],
        ),
    )

    if expected:
        data = LokiClusterRequirerAppData.load(out.get_relations("loki-cluster")[0].local_app_data)
        assert set(data.roles) == set(expected)
    else:
        assert not out.get_relations("loki-cluster")[0].local_app_data
