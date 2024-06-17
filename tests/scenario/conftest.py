import pytest
from charm import LokiWorkerK8SOperatorCharm
from scenario import Context, ExecOutput


g@pytest.fixture
def ctx():
    return Context(LokiWorkerK8SOperatorCharm)


LOKI_VERSION_EXEC_OUTPUT = ExecOutput(stdout="3.0.0")
