# main.py — launch shim. The Discord bot now lives in surfaces/discord_bot/.
# pm2 runs `python main.py`; this keeps that entrypoint working unchanged while
# the entire legacy inline pipeline (mint/payment/QR/CDN/FFmpeg) has moved into
# the package and inverted onto lfg_service (Spine Plan 3, Task 4).
from surfaces.discord_bot.bot import main

if __name__ == "__main__":
    main()
