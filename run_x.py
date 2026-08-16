# Launch shim for the X (Twitter) surface — mirrors run_telegram.py/main.py.
# Importing surfaces.x_bot.bot here (rather than `python -m surfaces.x_bot.bot`)
# ensures bot.py loads under its canonical module name exactly once. Running
# it as `-m` would execute it as __main__ AND import it again as
# surfaces.x_bot.bot — harmless today (no second consumer imports this module
# the way commands.py does for Telegram), but this shim keeps the same
# canonical-import posture as every other surface so a future import of
# surfaces.x_bot.bot from elsewhere can never observe two independent copies
# of its module-level state.
from surfaces.x_bot.bot import main

if __name__ == "__main__":
    main()
