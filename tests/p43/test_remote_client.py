import httpx
import pytest
from platform_contracts.service import ServiceFault
from platform_execution.adapters.remote import CredentialProvider, RemoteClient


def client(handler):
    value = RemoteClient(
        {"endpoint": "https://peer.test", "credential_ref": "peer"},
        CredentialProvider({"peer": "x" * 32}),
    )
    value.client.close()
    value.client = httpx.Client(
        base_url="https://peer.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return value


def test_plain_404_is_unknown_not_unexecuted():
    remote = client(lambda request: httpx.Response(404, json={"code": "RESOURCE_NOT_FOUND"}))
    try:
        assert remote.query("action") is None
    finally:
        remote.close()


def test_redirect_and_cross_action_output_are_rejected():
    remote = client(lambda request: httpx.Response(307, headers={"location": "https://other.test"}))
    try:
        with pytest.raises(ServiceFault):
            remote.query("action")
        with pytest.raises(ServiceFault):
            remote.output(
                "action",
                "/v1/actions/other/outputs/summary",
                "/tmp/unused",
                1024,
                "sha256:" + "0" * 64,
                1,
            )
    finally:
        remote.close()


def test_missing_credential_never_falls_back_to_environment_proxy():
    with pytest.raises(ServiceFault):
        RemoteClient(
            {"endpoint": "https://peer.test", "credential_ref": "missing"},
            CredentialProvider({}),
        )


def test_declared_oversized_output_is_rejected_before_write(tmp_path):
    remote = client(
        lambda request: httpx.Response(
            200,
            content=b"abcd",
            headers={"content-length": "4", "content-type": "application/json"},
        )
    )
    target = tmp_path / "summary.json"
    try:
        with pytest.raises(ServiceFault):
            remote.output(
                "action",
                "/v1/actions/action/outputs/summary",
                target,
                3,
                "sha256:" + "0" * 64,
                4,
            )
        assert not target.exists() and not target.with_suffix(".json.download").exists()
    finally:
        remote.close()


def test_streamed_output_stops_at_actual_limit(tmp_path):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ab"
            yield b"cd"

    remote = client(
        lambda request: httpx.Response(
            200,
            stream=Chunks(),
            headers={"content-type": "application/json"},
        )
    )
    target = tmp_path / "summary.json"
    try:
        with pytest.raises(ServiceFault):
            remote.output(
                "action",
                "/v1/actions/action/outputs/summary",
                target,
                3,
                "sha256:" + "0" * 64,
                4,
            )
        assert not target.exists() and not target.with_suffix(".json.download").exists()
    finally:
        remote.close()
