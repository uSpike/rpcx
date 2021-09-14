from __future__ import annotations

import inspect
import logging
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Optional, get_type_hints

import anyio

from .message import Request, ResponseStreamChunk, ResponseStreamEnd

LOG = logging.getLogger(__name__)


class Stream(AbstractAsyncContextManager, AsyncIterator):
    """
    An async-iterable stream object connected with callbacks to send stream messages.

    This object is passed to RPC methods which specify it as an argument:

        async def method_foo(stream: Stream):
            async for data in stream:
                ...
    """

    def __init__(self, request: Request, sender: Callable[..., Coroutine[None, None, None]]) -> None:
        super().__init__()
        self.stream_producer, self.stream_consumer = anyio.create_memory_object_stream(math.inf)
        self.request = request
        self._sender = sender
        self._sent_stream_chunk = False

    @asynccontextmanager
    async def _make_ctx(self) -> AsyncIterator[Stream]:
        with self.stream_producer, self.stream_consumer:
            try:
                yield self
            finally:
                with anyio.CancelScope(shield=True):
                    if self._sent_stream_chunk:
                        await self._sender(ResponseStreamEnd(self.request.id))

    async def __aenter__(self) -> Stream:
        self._ctx = self._make_ctx()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *args: Any) -> None:
        await self._ctx.__aexit__(*args)

    async def send(self, value: Any) -> None:
        """
        Send a stream chunk.
        """
        self._sent_stream_chunk = True
        await self._sender(ResponseStreamChunk(self.request.id, value))

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.stream_consumer.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration()


@dataclass
class RPCMethod:
    name: str
    func: Callable[..., Any]

    @property
    def signature(self) -> inspect.Signature:
        return inspect.signature(self.func)

    @property
    def stream_arg(self) -> Optional[str]:
        hints = get_type_hints(self.func)
        for name in self.signature.parameters:
            if hints.get(name) is Stream:
                return name
        return None


class Task:
    def __init__(self) -> None:
        self.cancel_scope = anyio.CancelScope()
        self.stream: Optional[Stream] = None
        #: signals task is ready to receive data
        self.ready = anyio.Event()


class RPCManager:
    def __init__(self) -> None:
        self.methods: Dict[str, RPCMethod] = {}
        self.tasks: Dict[int, Task] = {}

    def register(self, method_name: str, func: Callable[..., Any]) -> None:
        """
        Registers a method.
        """
        if method_name in self.methods:
            raise ValueError(f"Duplicate method '{method_name}'")
        self.methods[method_name] = RPCMethod(method_name, func)

    def clear(self) -> None:
        self.methods.clear()

    async def aclose(self) -> None:
        for task in self.tasks.values():
            task.cancel_scope.cancel()

    def task_exists(self, task_id: int) -> bool:
        return task_id in self.tasks

    async def dispatch_request(
        self,
        request: Request,
        sender: Callable[..., Coroutine[None, None, None]],
    ) -> Any:
        """
        Calls the method.
        """
        method = self.methods.get(request.method)
        if method is None:
            raise ValueError(f"Invalid method: '{request.method}'")

        task = self.tasks[request.id] = Task()
        try:
            with task.cancel_scope:
                async with AsyncExitStack() as stack:
                    if method.stream_arg is not None:
                        task.stream = request.kwargs[method.stream_arg] = Stream(request, sender)
                        await stack.enter_async_context(task.stream)

                    task.ready.set()

                    LOG.debug("Dispatch: %s %s %s", request.method, request.args, request.kwargs)
                    return await method.func(*request.args, **request.kwargs)
        finally:
            del self.tasks[request.id]

    async def dispatch_stream_chunk(self, task_id: int, value: Any) -> None:
        task = self.tasks[task_id]
        await task.ready.wait()
        if task.stream is not None:
            await task.stream.stream_producer.send(value)

    async def dispatch_stream_end(self, task_id: int) -> None:
        task = self.tasks[task_id]
        await task.ready.wait()
        if task.stream is not None:
            await task.stream.stream_producer.aclose()

    def dispatch_cancel(self, task_id: int) -> None:
        task = self.tasks[task_id]
        task.cancel_scope.cancel()
