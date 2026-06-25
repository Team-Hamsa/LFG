from surfaces.telegram_bot import render


def test_payment_caption_has_link():
    cap = render.payment_caption("https://xumm.app/sign/abc")
    assert "https://xumm.app/sign/abc" in cap
    assert "1 token" in cap.lower() or "pay" in cap.lower()


def test_offer_caption_has_number_and_link():
    cap = render.offer_caption({"nft_number": 3600, "accept_deeplink": "https://xumm.app/sign/xyz"})
    assert "3600" in cap
    assert "https://xumm.app/sign/xyz" in cap


def test_error_caption_passthrough():
    assert "boom" in render.error_caption("boom")


def test_photo_input_builds_inputfile():
    f = render.photo_input(b"\x89PNG", "x.png")
    assert f.filename == "x.png"
