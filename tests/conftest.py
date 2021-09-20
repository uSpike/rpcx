import math
from contextlib import asynccontextmanager

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

from rpcx import RPCClient, RPCManager, RPCServer


class TestClient:
    def __init__(self):
        server_send, server_receive = anyio.create_memory_object_stream(math.inf, item_type=bytes)
        client_send, client_receive = anyio.create_memory_object_stream(math.inf, item_type=bytes)

        self.server_stream = StapledObjectStream(client_send, server_receive)
        self.client_stream = StapledObjectStream(server_send, client_receive)

        self.stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
        self.client = RPCClient(self.client_stream)
        self.client.raise_on_error = True


@pytest.fixture
def test_client():
    return TestClient()


@pytest.fixture
def test_client_stack():
    """
    Run a client and server connected by streams.
    """

    @asynccontextmanager
    async def ctx(manager: RPCManager):
        test_client = TestClient()
        server = RPCServer(test_client.server_stream, manager)

        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)

            async with test_client.client as client:
                yield client
                # allow tasks to finish before we cancel
                await anyio.wait_all_tasks_blocked()
                tg.cancel_scope.cancel()

    return ctx
