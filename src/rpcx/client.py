import logging
import math
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, Generator, Optional

if sys.version_info >= (3, 9):  # pragma: nocover
    from collections.abc import Awaitable
else:  # pragma: nocover
    from typing import Awaitable

import anyio
import asyncstdlib as astd
from anyio.abc import AnyByteStream
from anyio.streams.memory import MemoryObjectReceiveStream

from .message import (
    Message,
    Request,
    RequestCancel,
    RequestStreamChunk,
    RequestStreamEnd,
    Response,
    ResponseStreamChunk,
    ResponseStreamEnd,
    message_from_bytes,
    message_to_bytes,
)

LOG = logging.getLogger(__name__)


class ClientError(Exception):
    """
    Base exception for remote client errors
    """


class RemoteError(ClientError):
    """
    Remote method raised an exception, wrapped within
    """


class InvalidValue(ValueError, ClientError):
    """
    Invalid value(s) passed to RPC method.
    """


class InternalError(ClientError):
    """
    Server encountered an internal error.
    """


class _RequestTask:
    """
    Container for streams associated with a request.
    """

    def __init__(self) -> None:
        # There will only ever be one response, buffer size of 1 is appropriate
        self.response_producer, self.response_consumer = anyio.create_memory_object_stream(1, item_type=Response)
        # There may be many stream chunks, do not block on receiving them.
        self.stream_producer, self.stream_consumer = anyio.create_memory_object_stream(math.inf)

    @astd.lru_cache(maxsize=1)  # type: ignore[misc]
    async def get_response(self) -> Any:
        with self.response_producer, self.response_consumer:
            response = await self.response_consumer.receive()

            if response.status_is_ok:
                return response.value
            elif response.status_is_invalid:
                raise InvalidValue(response.value)
            elif response.status_is_internal:
                raise InternalError(response.value)
            else:
                raise RemoteError(response.value)


@dataclass(repr=False)
class RequestStream(Awaitable[Any], AsyncIterator["RequestStream"]):
    """
    Yielded from `RPCClient.request_stream()`
    """

    _get_response: Callable[[], Coroutine[None, None, None]]
    _stream_consumer: MemoryObjectReceiveStream[Any]
    send: Callable[[Any], Coroutine[None, None, None]]

    def __aiter__(self) -> "RequestStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._stream_consumer.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration()

    def __await__(self) -> Generator[None, None, Any]:
        return self._get_response().__await__()


class RPCClient:
    def __init__(self, stream: AnyByteStream, raise_on_error: bool = False) -> None:
        self.stream = stream
        self.raise_on_error = raise_on_error
        #: In-flight requests, key is request id
        self.tasks: Dict[int, _RequestTask] = {}
        self._next_id = 0
        # This represents the maximum number of concurrent requests
        # We don't want this to be huge so the message size stays small
        self._max_id = 2 ** 16 - 1
        self._timeout = 10

    @property
    def next_msg_id(self) -> int:
        if self._next_id >= self._max_id:  # pragma: nocover
            self._next_id = 0
        else:
            self._next_id += 1
        while self._next_id in self.tasks:  # pragma: nocover
            # Make sure task with this ID isn't already in-flight
            self._next_id += 1
        return self._next_id

    @asynccontextmanager
    async def _make_ctx(self) -> AsyncIterator["RPCClient"]:
        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(self.receive_loop)
                yield self
                task_group.cancel_scope.cancel()
        finally:
            self.tasks.clear()

    async def __aenter__(self) -> "RPCClient":
        self._ctx = self._make_ctx()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *args: Any) -> Optional[bool]:
        return await self._ctx.__aexit__(*args)

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        req = Request(id=self.next_msg_id, method=method, args=args, kwargs=kwargs)
        await self.send_msg(req)
        task = self.tasks[req.id] = _RequestTask()

        try:
            return await task.get_response()
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                await self.send_msg(RequestCancel(req.id))
            raise
        finally:
            del self.tasks[req.id]

    @asynccontextmanager
    async def request_stream(self, method: str, *args: Any, **kwargs: Any) -> AsyncIterator[RequestStream]:
        req = Request(id=self.next_msg_id, method=method, args=args, kwargs=kwargs)
        await self.send_msg(req)
        task = self.tasks[req.id] = _RequestTask()

        did_send_chunk = False

        async def send_stream_chunk(value: Any) -> None:
            nonlocal did_send_chunk
            did_send_chunk = True
            await self.send_msg(RequestStreamChunk(req.id, value))

        stream = RequestStream(
            _get_response=task.get_response,
            _stream_consumer=task.stream_consumer,
            send=send_stream_chunk,
        )

        try:
            with task.stream_producer, task.stream_consumer:
                yield stream
                if did_send_chunk:
                    # Sent a stream chunk, must send the stream end
                    await self.send_msg(RequestStreamEnd(req.id))
                await task.get_response()
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                await self.send_msg(RequestCancel(req.id))
            raise
        finally:
            del self.tasks[req.id]

    async def receive_loop(self) -> None:
        """
        Receives data on the websocket and runs handlers.
        """
        async for data in self.stream:
            msg = message_from_bytes(data)
            task = self.tasks.get(msg.id)

            # If we cancel a request, it is possible that responses could come after deleting the task.
            # In this case, we do not care about those responses.
            if task is not None:
                try:
                    if isinstance(msg, Response):
                        await task.response_producer.send(msg)
                    elif isinstance(msg, ResponseStreamChunk):
                        await task.stream_producer.send(msg.value)
                    elif isinstance(msg, ResponseStreamEnd):
                        task.stream_producer.close()
                    else:
                        LOG.warning("Received unhandled message: %s", msg)
                except anyio.get_cancelled_exc_class():  # pragma: nocover
                    raise
                except:
                    if self.raise_on_error:
                        raise
                    else:
                        LOG.exception("Client receive error: %s", msg)

    async def send_msg(self, msg: Message) -> None:
        await self.stream.send(message_to_bytes(msg))
