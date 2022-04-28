import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, Generator, Optional, Union

import anyio
from anyio.abc import AnyByteStream

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


@dataclass
class _RequestTask:
    """
    Container for streams associated with a request.
    """

    _value: Any = object()
    _lock: anyio.Lock = field(default_factory=anyio.Lock)
    stream: Union["RequestStream", None] = None

    def __post_init__(self) -> None:
        # There will only ever be one response, buffer size of 1 is appropriate
        self.response_producer, self.response_consumer = anyio.create_memory_object_stream(1, item_type=Response)
        # There may be many stream chunks, do not block on receiving them.
        self.stream_producer, self.stream_consumer = anyio.create_memory_object_stream(math.inf)

    def __await__(self) -> Generator[None, None, Any]:
        return self.get_response().__await__()

    async def get_response(self) -> Any:
        async with self._lock:
            if self._value is not _RequestTask._value:
                return self._value

            with self.response_producer, self.response_consumer:
                response = await self.response_consumer.receive()
                self._value = response.value

                if response.status_is_ok:
                    return self._value
                elif response.status_is_invalid:
                    raise InvalidValue(response.value)
                elif response.status_is_internal:
                    raise InternalError(response.value)
                else:
                    raise RemoteError(response.value)


@dataclass(repr=False)
class RequestStream(AsyncIterator["RequestStream"]):
    """
    Yielded from `RPCClient.request_stream()`
    """

    task: _RequestTask
    req_id: int
    _send_msg: Callable[[Union[RequestStreamChunk, RequestStreamEnd]], Coroutine[None, None, None]]
    _did_send_chunk: bool = False
    _did_enter_ctx: bool = False

    async def send(self, chunk: Any) -> None:
        self._did_send_chunk = True
        await self._send_msg(RequestStreamChunk(self.req_id, chunk))

    async def __aenter__(self) -> "RequestStream":
        self._did_enter_ctx = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._did_send_chunk:
            # Sent a stream chunk, must send the stream end
            with anyio.CancelScope(shield=True):
                await self._send_msg(RequestStreamEnd(self.req_id))

    def __aiter__(self) -> "RequestStream":
        if not self._did_enter_ctx:
            raise RuntimeError("Stream must have entered context to iterate")
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.task.stream_consumer.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration()


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

    @asynccontextmanager
    async def _request_context(self, request: Request) -> AsyncIterator[_RequestTask]:
        """
        Send a request, create a task, and send cancellation if cancelled.
        """
        await self.send_msg(request)
        task = self.tasks[request.id] = _RequestTask()

        async def send_cancel() -> None:
            with anyio.CancelScope(shield=True):
                await self.send_msg(RequestCancel(request.id))

        with task.stream_producer, task.stream_consumer:
            try:
                yield task
            except anyio.get_cancelled_exc_class():
                await send_cancel()
                raise
            except anyio.ExceptionGroup as exc:
                if any(isinstance(e, anyio.get_cancelled_exc_class()) for e in exc.exceptions):
                    await send_cancel()
                raise
            finally:
                del self.tasks[request.id]

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        req = Request(id=self.next_msg_id, method=method, args=args, kwargs=kwargs)
        async with self._request_context(req) as task:
            return await task.get_response()

    @asynccontextmanager
    async def request_stream(self, method: str, *args: Any, **kwargs: Any) -> AsyncIterator[_RequestTask]:
        req = Request(id=self.next_msg_id, method=method, args=args, kwargs=kwargs)

        async with self._request_context(req) as task:
            task.stream = RequestStream(task=task, req_id=req.id, _send_msg=self.send_msg)
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(task.get_response)
                yield task

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
