import logging
import math
import sys

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

if sys.version_info >= (3, 8):
    from unittest.mock import AsyncMock
else:
    from mock import AsyncMock


from rpcx import RPCManager, RPCServer
from rpcx.message import Message, Request, RequestCancel, Response, ResponseStatus, message_to_bytes

pytestmark = pytest.mark.anyio


async def test_bad_message():
    class BadMessage(Message):
        type = 100

    bad_msg = BadMessage(0)

    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())
    with pytest.raises(ValueError, match="Unknown message type: 100"):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message_to_bytes(bad_msg))


async def test_internal_error(mocker):
    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())

    mocker.patch.object(server, "handle_request", side_effect=Exception("boom!"))
    msg = Request(id=1, method="foo", args=(), kwargs={})

    with pytest.raises(Exception, match="boom!"):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message_to_bytes(msg))


async def test_invalid_id(caplog):
    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, RPCManager())

    msg = RequestCancel(id=1)

    with caplog.at_level(logging.WARNING):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message_to_bytes(msg))
            await anyio.wait_all_tasks_blocked()
            tg.cancel_scope.cancel()

    assert "Requested non-existing task" in caplog.text


async def test_unhandled_message(caplog):
    async def wait():
        await anyio.sleep_forever()

    rpc = RPCManager()
    rpc.register("wait", wait)

    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, rpc)

    # start a task that waits forever
    msg1 = Request(id=0, method="wait", args=(), kwargs={})
    # send a response which doesn't make sense for the server to receive
    msg2 = Response(id=0, status=ResponseStatus.OK, value=None)

    with caplog.at_level(logging.WARNING):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, True)
            await stream.send(message_to_bytes(msg1))
            await anyio.wait_all_tasks_blocked()
            await stream.send(message_to_bytes(msg2))
            await anyio.wait_all_tasks_blocked()
            tg.cancel_scope.cancel()

    assert "Received unhandled message" in caplog.text


async def test_token(mocker):
    rpc = RPCManager()

    stream = StapledObjectStream(*anyio.create_memory_object_stream(math.inf, item_type=bytes))
    server = RPCServer(stream, rpc, token=1)

    assert server.token == 1
    msg = Request(id=0, method="foo", args=(), kwargs={})

    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        mock = rpc.dispatch_request = AsyncMock(return_value=None)

        await stream.send(message_to_bytes(msg))
        await anyio.wait_all_tasks_blocked()

        mock.assert_called_once()
        assert mock.call_args[0][0] == 1  # token
        assert mock.call_args[0][1] == 0  # request_id

        tg.cancel_scope.cancel()
