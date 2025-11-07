import anyio
import pytest

from rpcx.client import InternalError, InvalidValue, RemoteError
from rpcx.message import (
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

pytestmark = pytest.mark.anyio


async def test_response(test_client):
    msg = Response(id=1, status=ResponseStatus.OK, value=1)
    await test_client.server_stream.send(message_to_bytes(msg))

    async with test_client.client as client:
        assert await client.request("any") == 1

    assert message_from_bytes(await test_client.server_stream.receive()) == Request(
        id=1, method="any", args=(), kwargs={}
    )


async def test_cancel(test_client):
    async with test_client.client as client:
        async with anyio.create_task_group() as task_group:

            async def cancel_soon():
                await anyio.wait_all_tasks_blocked()
                task_group.cancel_scope.cancel()

            task_group.start_soon(cancel_soon)
            await client.request("any")

    assert message_from_bytes(await test_client.server_stream.receive()) == Request(
        id=1, method="any", args=(), kwargs={}
    )
    assert message_from_bytes(await test_client.server_stream.receive()) == RequestCancel(id=1)


async def test_stream_response(test_client):
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="a")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="b")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="c")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamEnd(id=1)))
    await test_client.server_stream.send(message_to_bytes(Response(id=1, status=ResponseStatus.OK, value="1")))

    async with test_client.client as client:
        async with client.request_stream("any") as stream:
            assert message_from_bytes(await test_client.server_stream.receive()) == Request(
                id=1, method="any", args=(), kwargs={}
            )
            items = []
            async for item in stream:
                items.append(item)
            assert items == ["a", "b", "c"]
        assert await stream == "1"


async def test_stream_receive_data_after_task_ends(test_client, caplog):
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="a")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="b")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="c")))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamEnd(id=1)))
    await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="d")))
    await test_client.server_stream.send(message_to_bytes(Response(id=1, status=ResponseStatus.OK, value="1")))

    async with test_client.client as client:
        client.raise_on_error = False
        async with client.request_stream("any") as stream:
            items = []
            async for item in stream:
                items.append(item)
            assert items == ["a", "b", "c"]
        assert await stream == "1"
        assert "Client receive error: ResponseStreamChunk(id=1, value='d')" in caplog.text


async def test_stream_cancel(test_client):
    async with test_client.client as client:
        items = []
        async with anyio.create_task_group() as task_group:
            async with client.request_stream("any") as stream:

                async def cancel_soon():
                    await anyio.wait_all_tasks_blocked()
                    task_group.cancel_scope.cancel()
                    with anyio.CancelScope(shield=True):
                        await test_client.server_stream.send(message_to_bytes(ResponseStreamEnd(id=1)))

                await test_client.server_stream.send(message_to_bytes(ResponseStreamChunk(id=1, value="a")))
                task_group.start_soon(cancel_soon)

                async for item in stream:
                    items.append(item)

        assert items == ["a"]
        assert message_from_bytes(await test_client.server_stream.receive()) == Request(
            id=1, method="any", args=(), kwargs={}
        )
        assert message_from_bytes(await test_client.server_stream.receive()) == RequestCancel(id=1)


async def test_send_stream(test_client):
    async with test_client.client as client:
        async with client.request_stream("any") as stream:
            await stream.send("a")
            await stream.send("b")
            await stream.send("c")

            await test_client.server_stream.send(message_to_bytes(Response(id=1, status=ResponseStatus.OK, value="1")))

        assert await stream == "1"

        assert message_from_bytes(await test_client.server_stream.receive()) == Request(
            id=1, method="any", args=(), kwargs={}
        )
        assert message_from_bytes(await test_client.server_stream.receive()) == RequestStreamChunk(id=1, value="a")
        assert message_from_bytes(await test_client.server_stream.receive()) == RequestStreamChunk(id=1, value="b")
        assert message_from_bytes(await test_client.server_stream.receive()) == RequestStreamChunk(id=1, value="c")
        assert message_from_bytes(await test_client.server_stream.receive()) == RequestStreamEnd(id=1)


async def test_receive_unhandled_message(test_client, caplog):
    async with test_client.client as client:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(client.request, "any")
            await anyio.wait_all_tasks_blocked()

            msg = RequestStreamChunk(id=1, value=None)
            await test_client.server_stream.send(message_to_bytes(msg))
            await anyio.wait_all_tasks_blocked()

            task_group.cancel_scope.cancel()

    assert "Received unhandled message: RequestStreamChunk(id=1, value=None)" in caplog.text


async def test_raise_on_error(test_client):
    client = test_client.client

    async def request_ended() -> None:
        with pytest.raises(anyio.EndOfStream):
            await client.request("any")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(request_ended)
        await anyio.wait_all_tasks_blocked()

        client.tasks[1].stream_producer.close()

        msg = ResponseStreamChunk(id=1, value=None)
        await test_client.server_stream.send(message_to_bytes(msg))

        with pytest.raises(anyio.ClosedResourceError):
            client.raise_on_error = True
            await client.receive_loop()
        task_group.cancel_scope.cancel()


async def test_remoteerror(test_client):
    msg = Response(id=1, status=ResponseStatus.ERROR, value=None)
    await test_client.server_stream.send(message_to_bytes(msg))

    async with test_client.client as client:
        with pytest.raises(RemoteError):
            await client.request("any")


async def test_invalidvalue(test_client):
    msg = Response(id=1, status=ResponseStatus.INVALID, value=None)
    await test_client.server_stream.send(message_to_bytes(msg))

    async with test_client.client as client:
        with pytest.raises(InvalidValue):
            await client.request("any")


async def test_internalerror(test_client):
    msg = Response(id=1, status=ResponseStatus.INTERNAL, value=None)
    await test_client.server_stream.send(message_to_bytes(msg))

    async with test_client.client as client:
        with pytest.raises(InternalError):
            await client.request("any")


async def test_request_server_stream_end(test_client):
    async def request_closed():
        with pytest.raises(anyio.EndOfStream):
            await test_client.client.request("any")

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tg:
            tg.start_soon(request_closed)
            tg.start_soon(test_client.client.receive_loop)
            await anyio.wait_all_tasks_blocked()
            await test_client.server_stream.send_stream.aclose()


async def test_request_stream_server_stream_end(test_client):
    async def request_stream_closed():
        with pytest.raises(anyio.EndOfStream):
            async with test_client.client.request_stream("any") as stream:
                async for _ in stream:
                    ...

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tg:
            tg.start_soon(request_stream_closed)
            tg.start_soon(test_client.client.receive_loop)
            await anyio.wait_all_tasks_blocked()
            await test_client.server_stream.send_stream.aclose()


async def test_request_after_server_close(test_client):
    msg = Response(id=1, status=ResponseStatus.OK, value=1)
    with anyio.fail_after(1):
        await test_client.server_stream.send(message_to_bytes(msg))
        async with test_client.client as client:
            # Valid response
            val = await client.request("any")
            assert val == msg.value
            await test_client.server_stream.send_stream.aclose()
            await anyio.wait_all_tasks_blocked()
            # Server closed
            with pytest.raises(RuntimeError):
                # Server closes before request
                await client.request("any")


async def test_request_stream_after_server_close(test_client):
    msg = Response(id=1, status=ResponseStatus.OK, value=1)
    with anyio.fail_after(1):
        await test_client.server_stream.send(message_to_bytes(msg))
        async with test_client.client as client:
            # Valid response
            val = await client.request("any")
            assert val == msg.value
            await test_client.server_stream.send_stream.aclose()
            await anyio.wait_all_tasks_blocked()
            # Server closed
            with pytest.raises(RuntimeError):
                # Server closes before request
                async with client.request_stream("any"):
                    ...
