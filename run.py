import threading
import time
import webbrowser

import uvicorn

URL = "http://localhost:8000"
HOST = "127.0.0.1"


def _open_browser():
    time.sleep(2)
    webbrowser.open(URL)


if __name__ == "__main__":
    # Manual restores are swapped in before app.main imports SQLAlchemy and
    # opens the live SQLite file. The browser is opened only after that boundary.
    from app.paths import prepare_runtime_profile
    from app.services import backup_service

    prepare_runtime_profile()
    backup_service.acquire_profile_lock()
    restore_result = backup_service.apply_pending_restore()
    if restore_result and restore_result.get("installer_status") == "installing":
        raise SystemExit(0)
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=8000,
        reload=True,
    )
