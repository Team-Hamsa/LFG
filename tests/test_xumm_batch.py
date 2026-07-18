import pytest

from lfg_core import xumm_ops

UUID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_batch_payload_enforces_buyer_and_submits(monkeypatch):
    captured = {}

    async def fake_post(payload):
        captured.update(payload)
        return {
            "qr_url": "q",
            "xumm_url": "x",
            "uuid": UUID,
            "pushed": False,
        }

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    result = await xumm_ops.create_batch_payload(
        {"TransactionType": "Batch", "Account": "rBuyer"},
        signer="rBuyer",
        return_url=None,
        user_token=None,
    )
    assert captured["options"]["submit"] is True
    assert captured["options"]["signer"] == "rBuyer"
    assert captured["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES
    assert result["uuid"] == UUID


@pytest.mark.asyncio
async def test_batch_payload_rejects_signer_different_from_outer_account():
    with pytest.raises(ValueError):
        await xumm_ops.create_batch_payload(
            {"TransactionType": "Batch", "Account": "rBuyer"},
            signer="rOther",
        )


class _Response:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta", "cancelled", "terminal"),
    [
        ({"signed": True}, False, True),
        ({"cancelled": True, "resolved": True}, True, True),
        ({"expired": True}, False, True),
        ({"opened": True}, False, False),
    ],
)
async def test_payload_status_normalizes_cancel_and_resolution(
    monkeypatch, meta, cancelled, terminal
):
    body = {"meta": meta, "response": {}, "application": {}}
    monkeypatch.setattr(
        xumm_ops.requests,
        "get",
        lambda *args, **kwargs: _Response(body),
    )
    status = await xumm_ops.get_payload_status(UUID, force=True)
    assert status["cancelled"] is cancelled
    assert status["resolved"] is bool(meta.get("resolved"))
    assert xumm_ops._terminal(status) is terminal
