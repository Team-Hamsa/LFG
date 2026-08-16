# Pure-function tests for the Discord mint render helpers (no SDK/XRPL). Covers
# the large standalone artwork embed (#86) and that the offer embed no longer
# carries a redundant small thumbnail.
from surfaces.discord_bot import render


def test_artwork_embed_built_when_image_present():
    embed = render.artwork_embed({"nft_number": 3600, "image_url": "https://cdn/x.png"})
    assert embed is not None
    assert "Your NFT" in embed.title
    assert "3600" in embed.title
    assert embed.image.url == "https://cdn/x.png"


def test_artwork_embed_none_without_image():
    assert render.artwork_embed({"nft_number": 3600}) is None


def test_offer_embed_has_no_thumbnail():
    embed = render.offer_embed(
        {"nft_number": 7, "image_url": "https://cdn/x.png", "accept_deeplink": "https://accept"},
        "https://cdn/qr.png",
    )
    # the artwork now shows large in its own embed; the offer embed must not
    # carry the redundant small thumbnail anymore.
    assert embed.thumbnail.url is None
    assert embed.image.url == "https://cdn/qr.png"
