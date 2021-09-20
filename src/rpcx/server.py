import logging
from typing import Any, Callable, Coroutine

import anyio
from anyio.abc import AnyByteStream, TaskStatus

from .manager import RPCManager
from .message import (
    Message,
    Request,
    RequestCancel,
    RequestStreamChunk,
    RequestStreamEnd,
    Response,
    ResponseStatus,
    ResponseStreamChunk,
    ResponseStreamEnd,
    message_from_bytes,
    message_to_bytes,
)

LOG = logging.getLogger(__name__)


class RPCServer:
    def __init__(self, stream: AnyByteStream, rpc: RPCManager) -> None:
        self.stream = stream
        self.rpc = rpc

    async def handle_request(self, request: Request, task_status: TaskStatus) -> None:
        """
        Handle a request call.
        """
        sent_stream_chunk = False

        async def send_stream_chunk_wrapper(value: Any) -> None:
            nonlocal sent_stream_chunk
            sent_stream_chunk = True
            await self.send_stream_chunk(request.id, value)

        try:
            result = await self.rpc.dispatch_request(
                request.id,
                request.method,
                request.args,
                request.kwargs,
                send_stream_chunk_wrapper,
                task_status,
            )
            if sent_stream_chunk:
                await self.send_stream_end(request.id)
            await self.send_response(request.id, ResponseStatus.OK, result)
        except (TypeError, ValueError) as exc:
            LOG.exception("Invalid request")
            await self.send_response(request.id, ResponseStatus.INVALID, repr(exc))
        except Exception as exc:
            LOG.warning("rpc error: %s", request, exc_info=exc)
            await self.send_response(request.id, ResponseStatus.ERROR, repr(exc))

    async def handle_event(self, msg: Message) -> None:
        """
        Handle an event for an in-progress request.  This includes:
          - Cancellation
          - Stream chunks
          - Stream end
        """
        if not self.rpc.task_exists(msg.id):
            LOG.warning("Requested non-existing task: %s", msg)
        elif isinstance(msg, RequestCancel):
            self.rpc.dispatch_cancel(msg.id)
        elif isinstance(msg, RequestStreamChunk):
            await self.rpc.dispatch_stream_chunk(msg.id, msg.value)
        elif isinstance(msg, RequestStreamEnd):
            await self.rpc.dispatch_stream_end(msg.id)
        else:
            LOG.warning("Received unhandled message: %s", msg)

    async def send_msg(self, message: Message) -> None:
        await self.stream.send(message_to_bytes(message))

    async def send_response(self, request_id: int, status: ResponseStatus, value: Any) -> None:
        await self.send_msg(Response(request_id, status, value))

    async def send_stream_chunk(self, request_id: int, value: Any) -> None:
        await self.send_msg(ResponseStreamChunk(request_id, value))

    async def send_stream_end(self, request_id: int) -> None:
        await self.send_msg(ResponseStreamEnd(request_id))

    async def serve(self, raise_on_error: bool = False) -> None:
        """
        This is the main receive loop for the server.
        """

        def log_error(exc: Exception) -> None:
            LOG.exception("Internal error", exc_info=exc)
            if raise_on_error:
                raise exc

        async def wrap_task(
            task: Callable[..., Coroutine[None, None, None]],
            *args: Any,
            task_status: TaskStatus,
        ) -> None:
            try:
                await task(*args, task_status=task_status)
            except Exception as exc:
                log_error(exc)

        async with anyio.create_task_group() as task_group:
            async for data in self.stream:
                try:
                    msg = message_from_bytes(data)
                    LOG.debug("Receive %s", msg)
                    if isinstance(msg, Request):
                        await task_group.start(wrap_task, self.handle_request, msg)
                    else:
                        await self.handle_event(msg)
                except anyio.get_cancelled_exc_class():  # pragma: nocover
                    raise
                except Exception as exc:  # internal error!
                    # If we're testing, we'll want to re-raise.
                    # For a production deployment, log it but keep the loop alive.
                    log_error(exc)
