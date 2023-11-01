import math
from contextlib import asynccontextmanager

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

from rpcx import RPCClient, RPCManager, RPCServer


class TestFixture:
    def __init__(self, manager: RPCManager):
        server_send, server_receive = anyio.create_memory_object_stream[bytes](math.inf)
        client_send, client_receive = anyio.create_memory_object_stream[bytes](math.inf)

        self.server_stream = StapledObjectStream(client_send, server_receive)
        self.client_stream = StapledObjectStream(server_send, client_receive)

        self.server = RPCServer(self.server_stream, manager)
        self.client = RPCClient(self.client_stream)
        self.client.raise_on_error = True


@pytest.fixture
def test_client():
    return TestFixture(RPCManager())


@pytest.fixture
def test_stack():
    """
    Run a client and server connected by streams.
    """

    @asynccontextmanager
    async def ctx(manager: RPCManager):
        fixture = TestFixture(manager)

        async with anyio.create_task_group() as tg:
            tg.start_soon(fixture.server.serve, True)

            async with fixture.client:
                yield fixture
                # allow tasks to finish before we cancel
                await anyio.wait_all_tasks_blocked()
                tg.cancel_scope.cancel()

    return ctx
