import msgpack
import pytest

from rpcx import message

pytestmark = pytest.mark.anyio


def test_bad_message_bytes():
    with pytest.raises(ValueError):
        message.message_from_bytes(msgpack.packb((100, ("foo"))))


def test_response_status():
    response = message.Response(0, message.ResponseStatus.OK, None)
    assert response.status_is_ok

    response = message.Response(0, message.ResponseStatus.INVALID, None)
    assert response.status_is_invalid

    response = message.Response(0, message.ResponseStatus.ERROR, None)
    assert response.status_is_error

    response = message.Response(0, message.ResponseStatus.INTERNAL, None)
    assert response.status_is_internal


def test_eq():
    assert not message.Request(0, "foo", (), {}) == object()
