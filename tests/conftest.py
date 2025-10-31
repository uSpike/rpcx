from __future__ import annotations

import math
from contextlib import asynccontextmanager, contextmanager

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

from rpcx import RPCClient, RPCManager, RPCServer


class TestFixture:
    server_stream: StapledObjectStream[bytes]
    client_stream: StapledObjectStream[bytes]
    server: RPCServer
    client: RPCClient

    def __init__(self, manager: RPCManager):
        self.manager = manager

    @classmethod
    @contextmanager
    def start(cls, manager: RPCManager):
        fixture = cls(manager)
        server_send, server_receive = anyio.create_memory_object_stream[bytes](math.inf)
        client_send, client_receive = anyio.create_memory_object_stream[bytes](math.inf)

        with server_send, server_receive, client_send, client_receive:
            fixture.server_stream = StapledObjectStream(client_send, server_receive)
            fixture.client_stream = StapledObjectStream(server_send, client_receive)

            fixture.server = RPCServer(fixture.server_stream, fixture.manager)
            fixture.client = RPCClient(fixture.client_stream)
            fixture.client.raise_on_error = True
            yield fixture


@pytest.fixture
def test_client():
    with TestFixture.start(RPCManager()) as fixture:
        yield fixture


@pytest.fixture
def test_stack():
    """
    Run a client and server connected by streams.
    """

    @asynccontextmanager
    async def ctx(manager: RPCManager):
        async with anyio.create_task_group() as tg:
            with TestFixture.start(manager) as fixture:
                tg.start_soon(fixture.server.serve, True)

                async with fixture.client:
                    yield fixture
                    # allow tasks to finish before we cancel
                    await anyio.wait_all_tasks_blocked()
                    tg.cancel_scope.cancel()

    return ctx
