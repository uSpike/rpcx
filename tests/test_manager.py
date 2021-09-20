import anyio
import pytest

from rpcx import RPCManager
from rpcx.message import Request

pytestmark = pytest.mark.anyio


def test_manager_clear():
    rpc = RPCManager()
    rpc.register("foo", lambda: None)
    assert rpc.methods
    rpc.clear()
    assert not rpc.methods


def test_manager_duplicate_name():
    manager = RPCManager()
    manager.register("foo", lambda: None)
    with pytest.raises(ValueError):
        manager.register("foo", lambda: None)


async def test_manager_aclose():
    rpc = RPCManager()

    started = anyio.Event()
    cancelled = anyio.Event()

    async def long_task():
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    rpc.register("long_task", long_task)

    request = Request(id=0, method="long_task", args=(), kwargs={})

    async def dummy_sender(*args):
        pass

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            rpc.dispatch_request, request.id, request.method, request.args, request.kwargs, dummy_sender
        )
        await started.wait()
        await rpc.aclose()
        await cancelled.wait()
        assert not rpc.tasks
