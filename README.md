# RPCx

[![CI](https://github.com/uSpike/rpcx/actions/workflows/main.yml/badge.svg)](https://github.com/uSpike/rpcx/actions/workflows/main.yml)

Asynchronous RPC server/client for Python 3.7+ with streaming support.

- Async backend implemented by `anyio` providing support for `asyncio` and `trio`.
- Generic stream support for transport includes Websockets.
- Messages are serialized with `msgpack`.

```python
import math

import anyio
from anyio.streams.stapled import StapledObjectStream
from rpcx import RPCClient, RPCManager, RPCServer, Stream


async def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b


async def fibonacci(n: int, stream: Stream) -> None:
    """Stream each number as computed in the Fibonacci sequence for a given starting number"""
    a, b = 0, 1

    for i in range(n):
        await stream.send(i)
        a, b = b, a + b


async def sum(stream: Stream) -> int:
    """Stream numbers from client and return the sum"""
    total = 0

    async for num in stream:
        total += num

    return total


manager = RPCManager()
manager.register("add", add)
manager.register("fibonacci", fibonacci)
manager.register("sum", sum)


async def main() -> None:
    # Create two connected stapled streams to simulate a network connection
    server_send, server_receive = anyio.create_memory_object_stream(math.inf, item_type=bytes)
    client_send, client_receive = anyio.create_memory_object_stream(math.inf, item_type=bytes)
    server_stream = StapledObjectStream(client_send, server_receive)
    client_stream = StapledObjectStream(server_send, client_receive)

    server = RPCServer(server_stream, manager)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(server.serve)

        async with RPCClient(client_stream) as client:
            # Simple method call
            assert await client.request("add", 1, 2) == 3

            # Streaming (server to client) example
            async with client.request_stream("fibonacci", 6) as stream:
                async for num in stream:
                    print(num)  # 1, 1, 2, 3, 5, 8

            # Streaming (client to server) example
            async with client.request_stream("sum") as stream:
                for num in range(10):
                    await stream.send(num)

            assert await stream == 45


anyio.run(main)
```

# Development

## Installation

Run `poetry install` to install the project.

## Execute tests

Execute `poetry run tox` to run tests for all python environments.

## Pre-commit hooks

Execute `poetry run pre-commit run -a` to run all linters, formatters, and checks.
