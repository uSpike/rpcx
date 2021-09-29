import logging

import anyio
import asyncstdlib as astd
import pytest

from rpcx import RPCManager, Stream
from rpcx.client import InvalidValue, RemoteError

LOG = logging.getLogger(__name__)

pytestmark = pytest.mark.anyio


async def test_simple(test_stack):
    async def simple(a: int, b: int):
        return a + b

    manager = RPCManager()
    manager.register("simple", simple)

    async with test_stack(manager) as stack:
        response = await stack.client.request("simple", 1, 2)
        assert response == 3

        assert not stack.client.tasks
        assert not stack.server.dispatcher.tasks


async def test_simple_error(test_stack):
    async def simple_error():
        raise Exception("Error!")

    manager = RPCManager()
    manager.register("simple_error", simple_error)

    async with test_stack(manager) as stack:
        with pytest.raises(RemoteError):
            await stack.client.request("simple_error")


async def test_stream(test_stack):
    async def server_stream(stream: Stream):
        for i in range(10):
            LOG.debug("server_stream %d", i)
            await stream.send(i)

        return "vincent loves strings"

    manager = RPCManager()
    manager.register("server_stream", server_stream)

    async with test_stack(manager) as stack:
        async with stack.client.request_stream("server_stream") as stream:
            async for i, response in astd.enumerate(stream):
                assert response == i

            # There is a task in this context
            assert stack.client.tasks

        # We've left the client stream context, no more tasks
        assert not stack.client.tasks

        # Then we can still await a result from the stream
        assert await stream == "vincent loves strings"
        # And the server task has finished
        assert not stack.server.dispatcher.tasks


async def test_client_stream(test_stack, caplog):
    async def client_stream(stream: Stream):
        total = 0
        async for i in stream:
            total += i
            LOG.debug("client_stream %d", i)
        return total

    manager = RPCManager()
    manager.register("client_stream", client_stream)

    async with test_stack(manager) as stack:
        async with stack.client.request_stream("client_stream") as stream:
            for i in range(10):
                await stream.send(i)

        assert await stream == sum(range(10))


async def test_bidirectional_stream(test_stack):
    async def bidirectional_stream(stream: Stream):
        async for i in stream:
            val = i + 1
            LOG.debug("bidir stream %d->%d", i, val)
            await stream.send(val)

    manager = RPCManager()
    manager.register("bidirectional_stream", bidirectional_stream)

    async with test_stack(manager) as stack:
        async with stack.client.request_stream("bidirectional_stream") as stream:
            i = 0
            await stream.send(i)
            async for response in stream:
                i += 1
                assert response == i
                await stream.send(i)
                if i >= 10:
                    break


async def test_invalid_name(test_stack):
    manager = RPCManager()

    async with test_stack(manager) as stack:
        with pytest.raises(InvalidValue):
            await stack.client.request("invalid")


async def test_bad_args(test_stack):
    async def simple(a: int, b: int):
        return a + b

    manager = RPCManager()
    manager.register("simple", simple)

    async with test_stack(manager) as stack:
        with pytest.raises(InvalidValue):
            await stack.client.request("simple", 1, 2, 3, 4, 5)


async def test_cancel(test_stack):
    started_event = anyio.Event()
    cancelled_event = anyio.Event()

    async def simple_cancel():
        try:
            started_event.set()
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled_event.set()
            raise

    manager = RPCManager()
    manager.register("simple_cancel", simple_cancel)

    async with test_stack(manager) as stack:

        async def cancel_soon():
            await started_event.wait()
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_soon)
            await stack.client.request("simple_cancel")

        with anyio.fail_after(1):
            await cancelled_event.wait()


async def test_cancel_stream(test_stack):
    started_event = anyio.Event()
    cancelled_event = anyio.Event()

    async def simple_cancel():
        try:
            started_event.set()
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled_event.set()
            raise

    manager = RPCManager()
    manager.register("simple_cancel", simple_cancel)

    async with test_stack(manager) as stack:

        async def cancel_soon():
            await started_event.wait()
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_soon)
            async with stack.client.request_stream("simple_cancel"):
                await anyio.sleep_forever()

        with anyio.fail_after(1):
            await cancelled_event.wait()
