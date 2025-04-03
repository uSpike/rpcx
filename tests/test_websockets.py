from unittest.mock import AsyncMock

import anyio
import pytest
from websockets.exceptions import ConnectionClosed

from rpcx.websockets import WebSocketsStream

pytestmark = pytest.mark.anyio


async def test_websockets_stream():
    stream = WebSocketsStream(AsyncMock())

    await stream.receive()
    stream.websocket.recv.assert_called_once()

    await stream.send(b"foo")
    stream.websocket.send.assert_called_once()

    stream.websocket.recv.side_effect = ConnectionClosed(1000, "foo")
    with pytest.raises(anyio.EndOfStream):
        await stream.receive()

    stream.websocket.send.side_effect = ConnectionClosed(1000, "foo")
    with pytest.raises(anyio.EndOfStream):
        await stream.send(b"foo")
