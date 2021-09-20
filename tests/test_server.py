import logging
import math

import anyio
import asyncstdlib as astd
import msgpack
import pytest
from anyio.streams.stapled import StapledObjectStream

from rpcx import RPCClient, RPCManager, RPCServer, Stream, message
from rpcx.client import InternalError, InvalidValue, RemoteError

LOG = logging.getLogger(__name__)

pytestmark = pytest.mark.anyio


def test_manager_clear():
    rpc = RPCManager()
    rpc.register("foo", lambda: None)
    assert rpc.methods
    rpc.clear()
    assert not rpc.methods


async def test_manager_aclose():
    rpc = RPCManager()

    started = anyio.Event()
    cancelled = anyio.Event()

    async def long_task():
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    rpc.register("long_task", long_task)

    request = message.Request(id=0, method="long_task", args=(), kwargs={})

    async def dummy_sender(*args):
        pass

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            rpc.dispatch_request, request.id, request.method, request.args, request.kwargs, dummy_sender
        )
        await started.wait()
        await rpc.aclose()
        await cancelled.wait()
        assert not rpc.tasks


async def test_server_bad_message():
    class BadMessage(message.Message):
        type = 100

    bad_msg = BadMessage(0)

    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())
    with pytest.raises(ValueError, match="Unknown message type: 100"):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message.message_to_bytes(bad_msg))


async def test_server_internal_error(mocker):
    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())

    mocker.patch.object(server, "handle_request", side_effect=Exception("boom!"))
    msg = message.Request(id=1, method="foo", args=(), kwargs={})

    with pytest.raises(Exception, match="boom!"):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message.message_to_bytes(msg))


async def test_client_internal_error(mocker):
    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    client = RPCClient(stream)

    msg = message.Response(id=1, status=message.ResponseStatus.INTERNAL, value=None)
    await stream.send(message.message_to_bytes(msg))

    with pytest.raises(InternalError):
        async with client:
            await client.request("any")


async def test_server_invalid_id(caplog):
    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())

    msg = message.RequestCancel(id=1)

    with caplog.at_level(logging.WARNING):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message.message_to_bytes(msg))
            await anyio.wait_all_tasks_blocked()
            tg.cancel_scope.cancel()

    assert any(record.message.startswith("Requested non-existing task") for record in caplog.records), caplog.records


async def test_server_unhandled_message(caplog):
    async def wait():
        await anyio.sleep_forever()

    rpc = RPCManager()
    rpc.register("wait", wait)

    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, rpc)

    # start a task that waits forever
    msg1 = message.Request(id=0, method="wait", args=(), kwargs={})
    # send a response which doesn't make sense for the server to receive
    msg2 = message.Response(id=0, status=message.ResponseStatus.OK, value=None)

    with caplog.at_level(logging.WARNING):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message.message_to_bytes(msg1))
            await anyio.wait_all_tasks_blocked()
            await stream.send(message.message_to_bytes(msg2))
            await anyio.wait_all_tasks_blocked()
            tg.cancel_scope.cancel()

    assert any(record.message.startswith("Received unhandled message") for record in caplog.records), caplog.records


def test_manager_duplicate_name():
    manager = RPCManager()
    manager.register("foo", lambda: None)
    with pytest.raises(ValueError):
        manager.register("foo", lambda: None)


async def test_server_simple(test_client_stack):
    async def simple(a: int, b: int):
        return a + b

    rpc = RPCManager()
    rpc.register("simple", simple)

    async with test_client_stack(rpc) as client:
        response = await client.request("simple", 1, 2)
        assert response == 3

        assert not client.tasks
        assert not rpc.tasks


async def test_server_simple_error(test_client_stack):
    async def simple_error():
        raise Exception("Error!")

    rpc = RPCManager()
    rpc.register("simple_error", simple_error)

    async with test_client_stack(rpc) as client:
        with pytest.raises(RemoteError):
            await client.request("simple_error")


async def test_server_stream(test_client_stack):
    async def server_stream(stream: Stream):
        for i in range(10):
            LOG.debug("server_stream %d", i)
            await stream.send(i)

        return "vincent loves strings"

    rpc = RPCManager()
    rpc.register("server_stream", server_stream)

    async with test_client_stack(rpc) as client:
        async with client.request_stream("server_stream") as stream:
            async for i, response in astd.enumerate(stream):
                assert response == i

            # There is a task in this context
            assert client.tasks

        # We've left the client stream context, no more tasks
        assert not client.tasks

        # Then we can still await a result from the stream
        assert await stream == "vincent loves strings"
        # And the server task has finished
        assert not rpc.tasks


async def test_client_stream(test_client_stack, caplog):
    async def client_stream(stream: Stream):
        total = 0
        async for i in stream:
            total += i
            LOG.debug("client_stream %d", i)
        return total

    rpc = RPCManager()
    rpc.register("client_stream", client_stream)

    async with test_client_stack(rpc) as client:
        async with client.request_stream("client_stream") as stream:
            for i in range(10):
                await stream.send(i)

        assert await stream == sum(range(10))


async def test_bidirectional_stream(test_client_stack):
    async def bidirectional_stream(stream: Stream):
        async for i in stream:
            val = i + 1
            LOG.debug("bidir stream %d->%d", i, val)
            await stream.send(val)

    rpc = RPCManager()
    rpc.register("bidirectional_stream", bidirectional_stream)

    async with test_client_stack(rpc) as client:
        async with client.request_stream("bidirectional_stream") as stream:
            i = 0
            await stream.send(i)
            async for response in stream:
                i += 1
                assert response == i
                await stream.send(i)
                if i >= 10:
                    break


async def test_invalid_name(test_client_stack):
    rpc = RPCManager()

    async with test_client_stack(rpc) as client:
        with pytest.raises(InvalidValue):
            await client.request("invalid")


async def test_bad_args(test_client_stack):
    async def simple(a: int, b: int):
        return a + b

    rpc = RPCManager()
    rpc.register("simple", simple)

    async with test_client_stack(rpc) as client:
        with pytest.raises(InvalidValue):
            await client.request("simple", 1, 2, 3, 4, 5)


async def test_cancel(test_client_stack):
    started_event = anyio.Event()
    cancelled_event = anyio.Event()

    async def simple_cancel():
        try:
            started_event.set()
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled_event.set()
            raise

    rpc = RPCManager()
    rpc.register("simple_cancel", simple_cancel)

    async with test_client_stack(rpc) as client:

        async def cancel_soon():
            await started_event.wait()
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_soon)
            await client.request("simple_cancel")

        with anyio.fail_after(1):
            await cancelled_event.wait()


async def test_cancel_stream(test_client_stack):
    started_event = anyio.Event()
    cancelled_event = anyio.Event()

    async def simple_cancel():
        try:
            started_event.set()
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled_event.set()
            raise

    rpc = RPCManager()
    rpc.register("simple_cancel", simple_cancel)

    async with test_client_stack(rpc) as client:

        async def cancel_soon():
            await started_event.wait()
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_soon)
            async with client.request_stream("simple_cancel"):
                await anyio.sleep_forever()

        with anyio.fail_after(1):
            await cancelled_event.wait()


def test_bad_message_bytes():
    with pytest.raises(ValueError):
        message.message_from_bytes(msgpack.packb((100, ("foo"))))


def test_response_status():
    response = message.Response(0, message.ResponseStatus.OK, None)
    assert response.status_is_ok

    response = message.Response(0, message.ResponseStatus.INVALID, None)
    assert response.status_is_invalid

    response = message.Response(0, message.ResponseStatus.ERROR, None)
    assert response.status_is_error

    response = message.Response(0, message.ResponseStatus.INTERNAL, None)
    assert response.status_is_internal
