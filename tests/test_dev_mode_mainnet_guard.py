# WEBAPP_DEV_MODE makes require_auth/require_wallet substitute a fixed dev
# user + wallet on every authed route. That must never reach a mainnet
# deployment: the service refuses to build its app when both are set.
import pytest

from lfg_core import config
from lfg_service import app as server


@pytest.mark.parametrize("network", ["mainnet", "prod", "devnet", ""])
def test_validate_dev_mode_refuses_any_non_testnet_network(network):
    # config treats every value but "testnet" as mainnet, so the guard must too
    with pytest.raises(ValueError, match="WEBAPP_DEV_MODE"):
        config.validate_dev_mode_config(True, network)


@pytest.mark.parametrize(
    ("dev_mode", "network"),
    [(True, "testnet"), (True, " Testnet "), (False, "mainnet"), (False, "prod")],
)
def test_validate_dev_mode_allows_everything_else(dev_mode, network):
    config.validate_dev_mode_config(dev_mode, network)


def test_create_app_refuses_dev_mode_on_mainnet(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")

    def must_not_run():
        raise AssertionError("create_app must refuse before touching any store")

    monkeypatch.setattr(server.identity_store, "ensure_identities_table", must_not_run)
    with pytest.raises(ValueError, match="WEBAPP_DEV_MODE"):
        server.create_app()
