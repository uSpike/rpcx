"""
Utilities for websockets
"""

from dataclasses import dataclass
from typing import cast

import anyio
from anyio.abc import ObjectStream
from websockets.client import WebSocketClientProtocol  # type: ignore[attr-defined]
from websockets.exceptions import ConnectionClosed


@dataclass
class WebSocketsStream(ObjectStream[bytes]):
    """
    Wrapper for WebSocketClientProtocol to appear as an anyio ObjectStream.
    """

    websocket: WebSocketClientProtocol

    async def receive(self) -> bytes:
        try:
            # assume we're always receiving binary frames
            return cast(bytes, await self.websocket.recv())
        except ConnectionClosed as exc:
            raise anyio.EndOfStream() from exc

    async def send(self, data: bytes) -> None:
        try:
            await self.websocket.send(data)
        except ConnectionClosed as exc:
            raise anyio.EndOfStream() from exc

    async def aclose(self) -> None:  # pragma: nocover
        raise NotImplementedError()

    async def send_eof(self) -> None:  # pragma: nocover
        raise NotImplementedError()
