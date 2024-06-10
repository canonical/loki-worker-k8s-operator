from unittest.mock import Mock, patch

import pytest
from charm import LokiWorkerK8SOperatorCharm
from scenario import Context, ExecOutput


@pytest.fixture(autouse=True)
def patch_all():
    # with patch("charm.LokiWorkerK8SOperatorCharm._current_mimir_config", PropertyMock(return_value={})):
    # with patch("charm.LokiWorkerK8SOperatorCharm._set_alerts", Mock(return_value=True)):
    with patch(
        "charms.observability_libs.v1.kubernetes_service_patch.KubernetesServicePatch.__init__",
        Mock(return_value=None),
    ):
        yield


@pytest.fixture
def ctx():
    return Context(LokiWorkerK8SOperatorCharm)


LOKI_VERSION_EXEC_OUTPUT = ExecOutput(stdout="3.0.0")
