# webapp/server.py — launch-compatibility shim. The Activity service now lives in
# lfg_service.app; this keeps `python -m webapp.server` (the pm2 lfg-activity
# process) working unchanged. New code should import from lfg_service.app.
from lfg_service.app import main

if __name__ == "__main__":
    main()
