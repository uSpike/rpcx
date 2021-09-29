import anyio
import pytest

from rpcx import RPCManager
from rpcx.message import Request
from rpcx.server import Dispatcher

pytestmark = pytest.mark.anyio


def test_clear():
    manager = RPCManager()
    manager.register("foo", lambda: None)
    assert manager.methods
    manager.clear()
    assert not manager.methods


def test_duplicate_name():
    manager = RPCManager()
    manager.register("foo", lambda: None)
    with pytest.raises(ValueError):
        manager.register("foo", lambda: None)


async def test_aclose():
    manager = RPCManager()

    started = anyio.Event()
    cancelled = anyio.Event()

    async def long_task():
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    manager.register("long_task", long_task)
    dispatcher = Dispatcher(manager)

    request = Request(id=0, method="long_task", args=(), kwargs={})

    async def dummy_sender(*args):
        pass

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            dispatcher.request, request.id, request.method, request.args, request.kwargs, dummy_sender
        )
        await started.wait()
        await dispatcher.aclose()
        await cancelled.wait()
        assert not dispatcher.tasks
