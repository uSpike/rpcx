import enum
from typing import Any, ClassVar, Dict, Tuple

from msgpack import packb, unpackb


class MessageType(enum.IntEnum):
    REQUEST = 0  #: RPC request, client->server
    RESPONSE = 1  #: RPC response, server->client
    REQUEST_STREAM_CHUNK = 2  #: Stream part, client->server
    RESPONSE_STREAM_CHUNK = 3  #: Stream part, server->client
    REQUEST_STREAM_END = 4  #: Stream has ended, client->server
    RESPONSE_STREAM_END = 5  #: Stream has ended, server->client
    REQUEST_CANCEL = 6  #: Cancel in-flight request


class ResponseStatus(enum.IntEnum):
    OK = 0  #: Method completed normally
    INVALID = 1  #: Invalid request
    ERROR = 2  #: Method raised exception
    INTERNAL = 3  #: Internal error


def message_from_bytes(data: bytes) -> "Message":
    msg_data = unpackb(data, use_list=False)
    msg_type = msg_data[0]
    msg_args = msg_data[1:]
    if msg_type == MessageType.REQUEST:
        return Request(*msg_args)
    elif msg_type == MessageType.RESPONSE:
        return Response(*msg_args)
    elif msg_type == MessageType.REQUEST_STREAM_CHUNK:
        return RequestStreamChunk(*msg_args)
    elif msg_type == MessageType.RESPONSE_STREAM_CHUNK:
        return ResponseStreamChunk(*msg_args)
    elif msg_type == MessageType.REQUEST_STREAM_END:
        return RequestStreamEnd(*msg_args)
    elif msg_type == MessageType.RESPONSE_STREAM_END:
        return ResponseStreamEnd(*msg_args)
    elif msg_type == MessageType.REQUEST_CANCEL:
        return RequestCancel(*msg_args)
    else:
        raise ValueError(f"Unknown message type: {msg_type}")


def message_to_bytes(msg: "Message") -> bytes:
    return packb(msg.astuple())


class Message:
    """
    Base class for message types.
    """

    type: ClassVar[MessageType]

    __slots__ = ("id",)

    def __init__(self, id: int) -> None:
        self.id = id

    def astuple(self) -> Tuple[Any, ...]:
        attr_names = ("type",) + self.__slots__
        return tuple(getattr(self, attr) for attr in attr_names)

    def __eq__(self, other: object) -> bool:
        try:
            return all(getattr(other, attr) == getattr(self, attr) for attr in self.__slots__)
        except AttributeError:
            return False

    def __repr__(self) -> str:
        args = zip(self.__slots__, self.astuple()[1:])
        args_str = ", ".join(f"{name}={repr(val)}" for name, val in args)
        return f"{self.__class__.__name__}({args_str})"


class Request(Message):
    """
    Request to execute method, client->server.

    When a request is sent, a `Response` should be expected.
    """

    type = MessageType.REQUEST

    __slots__ = ("id", "method", "args", "kwargs")

    def __init__(self, id: int, method: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> None:
        super().__init__(id)
        self.method = method
        self.args = args
        self.kwargs = kwargs


class Response(Message):
    """
    Response from executed method, server->client.
    """

    type = MessageType.RESPONSE

    __slots__ = ("id", "status", "value")

    def __init__(self, id: int, status: ResponseStatus, value: Any) -> None:
        super().__init__(id)
        self.status = status
        self.value = value

    # shortcuts to avoid `message.status == ResponseStatus.OK`
    status_is_ok = property(lambda self: self.status == ResponseStatus.OK)
    status_is_invalid = property(lambda self: self.status == ResponseStatus.INVALID)
    status_is_error = property(lambda self: self.status == ResponseStatus.ERROR)
    status_is_internal = property(lambda self: self.status == ResponseStatus.INTERNAL)


class RequestStreamChunk(Message):
    """
    Stream data, client->server.

    If a `RequestStreamChunk` is sent, a `RequestStreamEnd` must be sent to signal the end of the stream.
    """

    type = MessageType.REQUEST_STREAM_CHUNK

    __slots__ = ("id", "value")

    def __init__(self, id: int, value: Any) -> None:
        super().__init__(id)
        self.value = value


class ResponseStreamChunk(RequestStreamChunk):
    """
    Stream data, server->client.

    If a `ResponseStreamChunk` is sent, a `ResponseStreamEnd` must be sent to signal the end of the stream.
    """

    type = MessageType.RESPONSE_STREAM_CHUNK


class RequestStreamEnd(Message):
    """
    Stream is finished, client->server.
    """

    type = MessageType.REQUEST_STREAM_END


class ResponseStreamEnd(Message):
    """
    Stream is finished, server->client.
    """

    type = MessageType.RESPONSE_STREAM_END


class RequestCancel(Message):
    """
    Cancel an in-progress request, client->server.
    """

    type = MessageType.REQUEST_CANCEL
