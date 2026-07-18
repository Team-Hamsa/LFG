import argparse
import importlib

import pytest
from xrpl.utils import hex_to_str


def test_import_has_no_ledger_side_effect(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "xrpl.transaction.submit_and_wait", lambda *args, **kwargs: calls.append(1)
    )
    importlib.import_module("scripts.provision_batch_tickets")
    assert calls == []


def test_parser_requires_explicit_network():
    from scripts.provision_batch_tickets import build_parser

    args = build_parser().parse_args(["status", "--network", "testnet"])
    assert args.command == "status"
    assert args.network == "testnet"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["status"])


@pytest.mark.asyncio
async def test_status_never_submits(monkeypatch, capsys):
    from scripts import provision_batch_tickets as cli

    monkeypatch.setattr(cli.config, "assert_cli_network_match", lambda network: None)
    monkeypatch.setattr(
        cli.xrpl_actions,
        "list_ticket_sequences",
        lambda client, account: _async_value([10, 11]),
    )
    submitted = []
    monkeypatch.setattr(
        cli.xrpl_ops,
        "submit_backend_transaction",
        lambda *args, **kwargs: submitted.append(1),
    )
    result = await cli.run(
        argparse.Namespace(command="status", network="testnet")
    )
    assert result == 0
    assert "2 issuer Tickets" in capsys.readouterr().out
    assert submitted == []


@pytest.mark.asyncio
async def test_provision_submits_only_the_missing_ticket_count(monkeypatch):
    from scripts import provision_batch_tickets as cli

    monkeypatch.setattr(cli.config, "assert_cli_network_match", lambda network: None)
    monkeypatch.setattr(
        cli.xrpl_actions,
        "list_ticket_sequences",
        lambda client, account: _async_value([10, 11, 12]),
    )
    captured = {}

    async def fake_submit(tx, wallet, *, label):
        captured.update(tx=tx, wallet=wallet, label=label)
        return {"hash": "ABC"}

    monkeypatch.setattr(cli.xrpl_ops, "submit_backend_transaction", fake_submit)
    result = await cli.run(
        argparse.Namespace(command="provision", network="testnet", target=8)
    )
    assert result == 0
    tx = captured["tx"]
    assert captured["label"] == "TicketCreate"
    assert tx.account == cli.config.SIGNING_ACCOUNT
    assert tx.ticket_count == 5
    assert tx.source_tag == cli.config.SOURCE_TAG
    decoded = [
        (hex_to_str(row.memo_type), hex_to_str(row.memo_data))
        for row in tx.memos
    ]
    assert decoded == [
        ("initiator", "backend"),
        ("platform", "backend"),
        ("action", "batch-ticket-create"),
    ]


@pytest.mark.asyncio
async def test_provision_is_noop_when_target_is_met(monkeypatch, capsys):
    from scripts import provision_batch_tickets as cli

    monkeypatch.setattr(cli.config, "assert_cli_network_match", lambda network: None)
    monkeypatch.setattr(
        cli.xrpl_actions,
        "list_ticket_sequences",
        lambda client, account: _async_value([1, 2, 3]),
    )
    result = await cli.run(
        argparse.Namespace(command="provision", network="testnet", target=3)
    )
    assert result == 0
    assert "target already met" in capsys.readouterr().out


async def _async_value(value):
    return value
