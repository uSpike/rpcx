import logging
import math
import sys
from contextlib import contextmanager
from typing import Generator

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

from rpcx import RPCManager, RPCServer
from rpcx.message import Message, Request, RequestCancel, Response, ResponseStatus, message_to_bytes

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup

pytestmark = pytest.mark.anyio


@contextmanager
def collapse_excgroups() -> Generator[None, None, None]:
    try:
        yield
    except BaseException as exc:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        raise exc


async def test_bad_message():
    class BadMessage(Message):
        type = 100

    bad_msg = BadMessage(0)

    stream = StapledObjectStream(*anyio.create_memory_object_stream[bytes](math.inf))
    server = RPCServer(stream, RPCManager())
    with pytest.raises(ValueError, match="Unknown message type: 100"):
        with collapse_excgroups():
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve, True)
                await stream.send(message_to_bytes(bad_msg))


async def test_internal_error(mocker):
    stream = StapledObjectStream(*anyio.create_memory_object_stream[bytes](math.inf))
    server = RPCServer(stream, RPCManager())

    mocker.patch.object(server, "handle_request", side_effect=Exception("boom!"))
    msg = Request(id=1, method="foo", args=(), kwargs={})

    with pytest.raises(Exception, match="boom!"):
        with collapse_excgroups():
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve, True)
                await stream.send(message_to_bytes(msg))


async def test_invalid_id(caplog):
    stream = StapledObjectStream(*anyio.create_memory_object_stream[bytes](math.inf))
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

    stream = StapledObjectStream(*anyio.create_memory_object_stream[bytes](math.inf))
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
