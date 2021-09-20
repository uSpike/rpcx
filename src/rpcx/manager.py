import inspect
import logging
import math
import sys
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple, get_type_hints

if sys.version_info >= (3, 9):  # pragma: nocover
    from collections.abc import AsyncIterator
else:  # pragma: nocover
    from typing import AsyncIterator

import anyio
from anyio import TASK_STATUS_IGNORED
from anyio.abc import TaskStatus

LOG = logging.getLogger(__name__)


@dataclass
class Stream(AsyncIterator["Stream"]):
    """
    An async-iterable stream object connected with callbacks to send stream messages.

    This object is passed to RPC methods which specify it as an argument:

        async def method_foo(stream: Stream):
            async for data in stream:
                await stream.send(data + 1)
    """

    send: Callable[[Any], Coroutine[None, None, None]]

    def __post_init__(self) -> None:
        self.stream_producer, self.stream_consumer = anyio.create_memory_object_stream(math.inf)

    def __aiter__(self) -> "Stream":
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

    def task_exists(self, request_id: int) -> bool:
        return request_id in self.tasks

    async def dispatch_request(
        self,
        request_id: int,
        method_name: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        send_stream_chunk: Callable[[Any], Coroutine[None, None, None]],
        task_status: TaskStatus = TASK_STATUS_IGNORED,
    ) -> Any:
        """
        Calls the method.
        """
        task_status.started()
        method = self.methods.get(method_name)
        if method is None:
            raise ValueError(f"Invalid method: '{method_name}'")

        task = self.tasks[request_id] = Task()
        stream = task.stream = Stream(send_stream_chunk)

        try:
            with task.cancel_scope, stream.stream_producer, stream.stream_consumer:
                if method.stream_arg is not None:
                    kwargs[method.stream_arg] = stream

                LOG.debug("Dispatch: %s %s %s", method_name, args, kwargs)
                return await method.func(*args, **kwargs)
        finally:
            del self.tasks[request_id]

    async def dispatch_stream_chunk(self, request_id: int, value: Any) -> None:
        task = self.tasks[request_id]
        if task.stream is not None:
            await task.stream.stream_producer.send(value)

    async def dispatch_stream_end(self, request_id: int) -> None:
        task = self.tasks[request_id]
        if task.stream is not None:
            await task.stream.stream_producer.aclose()

    def dispatch_cancel(self, request_id: int) -> None:
        task = self.tasks[request_id]
        task.cancel_scope.cancel()
