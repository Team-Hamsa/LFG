from surfaces.telegram_bot import render


def test_payment_caption_has_link():
    cap = render.payment_caption("https://xumm.app/sign/abc")
    assert "https://xumm.app/sign/abc" in cap
    assert "1 token" in cap.lower() or "pay" in cap.lower()


def test_offer_caption_has_number_and_link():
    cap = render.offer_caption({"nft_number": 3600, "accept_deeplink": "https://xumm.app/sign/xyz"})
    assert "3600" in cap
    assert "https://xumm.app/sign/xyz" in cap
    # default with_qr=True still mentions QR scanning
    assert "qr" in cap.lower() or "scan" in cap.lower()


def test_offer_caption_no_qr_omits_scan_instructions():
    cap = render.offer_caption(
        {"nft_number": 4242, "accept_deeplink": "https://xumm.app/sign/abc"},
        with_qr=False,
    )
    assert "4242" in cap
    assert "https://xumm.app/sign/abc" in cap
    # must NOT instruct scanning a QR
    assert "scan" not in cap.lower()
    assert "qr" not in cap.lower()


def test_artwork_caption_has_number():
    cap = render.artwork_caption({"nft_number": 3600, "image_url": "https://cdn/x.png"})
    assert "3600" in cap


def test_artwork_caption_missing_number():
    cap = render.artwork_caption({})
    assert "?" in cap


def test_error_caption_passthrough():
    assert "boom" in render.error_caption("boom")


def test_photo_input_builds_inputfile():
    f = render.photo_input(b"\x89PNG", "x.png")
    assert f.filename == "x.png"


def test_send_media_picks_video_for_mp4():
    import asyncio

    calls = []

    class _B:
        async def send_photo(self, chat_id, photo, caption=None):
            calls.append(("photo", chat_id, photo, caption))

        async def send_video(self, chat_id, video, caption=None, supports_streaming=False):
            calls.append(("video", chat_id, video, caption, supports_streaming))

    async def go():
        await render.send_media(_B(), 1, "https://cdn/a.mp4", "cap")
        await render.send_media(_B(), 1, "https://cdn/a.png", "cap")
        await render.send_media(_B(), 1, "https://cdn/a.MP4?v=2", "cap")

    asyncio.run(go())
    assert [c[0] for c in calls] == ["video", "photo", "video"]
    # Inline playback on mobile requires the streaming flag on every video.
    assert all(c[4] for c in calls if c[0] == "video")
