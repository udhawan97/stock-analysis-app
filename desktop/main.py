"""Desktop entry point for the packaged FolioOrb app.

Runs the existing FastAPI application in-process on a loopback port and shows it
in a native window (WKWebView on macOS, WebView2 on Windows) via pywebview.
Closing the window shuts the server down. This is the target frozen by
PyInstaller — the browser-launching ``run.py`` remains the source/dev entry.

Run with ``--smoke`` to boot the server, confirm ``/health``, print the version,
and exit 0. ``--smoke-duplicate-recovery`` exercises the bundled recovery
backend against a disposable conflicting database. CI runs both on frozen
binaries before an installer is ever published.
"""

import html
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

# PyInstaller's --windowed/console=False mode sets sys.stdout/sys.stderr to
# None (no console attached, no pipe to redirect to). Any print() call would
# then raise AttributeError and crash the app before it even gets to show a
# window. This is a documented PyInstaller gotcha, not specific to this app —
# guard it unconditionally so every print() below is safe on every platform.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # pylint: disable=consider-using-with
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # pylint: disable=consider-using-with

# When run from a source checkout (python desktop/main.py), the repo root isn't
# on sys.path, so the `app` package can't be imported. A frozen build gets its
# path set up by PyInstaller, so only patch this in the non-frozen case.
if not getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

HOST = "127.0.0.1"
PREFERRED_PORT = 8000
HEALTH_TIMEOUT_SECONDS = 40.0

# Holds the server thread's startup exception, if any. A dict (rather than a
# module-level name rebound via `global`) so _run_server can record into it
# without a global statement.
_STARTUP_STATE: dict = {"error": None}


def _startup_error_document(title: str, detail: str, recovery: str) -> str:
    """Build a self-contained, escaped startup explanation for the native shell."""
    safe_title = html.escape(title)
    safe_detail = html.escape(detail)
    safe_recovery = html.escape(recovery)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{safe_title}</title><style>
:root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
body {{ margin: 0; padding: 36px; background: Canvas; color: CanvasText; }}
main {{ max-width: 620px; margin: auto; }}
h1 {{ margin: 0 0 16px; font-size: 24px; letter-spacing: -.02em; }}
p {{ line-height: 1.55; }}
.detail {{ padding: 14px 16px; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
  border-radius: 10px; background: color-mix(in srgb, CanvasText 5%, Canvas); }}
.recovery {{ font-weight: 600; }}
.close {{ opacity: .72; font-size: 13px; margin-top: 24px; }}
</style></head><body><main><h1>{safe_title}</h1>
<p class="detail">{safe_detail}</p><p class="recovery">{safe_recovery}</p>
<p class="close">Close this window after noting the recovery steps.</p>
</main></body></html>"""


def _surface_startup_error(title: str, detail: str, recovery: str) -> None:
    """Print a startup failure and show it in windowed frozen applications."""
    print(f"{title}: {detail} Recovery: {recovery}", file=sys.stderr)
    if not getattr(sys, "frozen", False):
        return
    try:
        import webview

        webview.create_window(
            title,
            html=_startup_error_document(title, detail, recovery),
            width=720,
            height=480,
            min_size=(560, 380),
            resizable=True,
        )
        webview.start()
    except Exception as exc:  # pylint: disable=broad-except
        # Keep the original startup error as the primary failure. This fallback
        # remains useful in console-enabled diagnostic builds.
        print(
            f"FolioOrb could not display its startup explanation: {type(exc).__name__}",
            file=sys.stderr,
        )


def _display_value(value) -> str:
    if value is None or value == "":
        return "Not recorded"
    return str(value)


def _duplicate_recovery_document(groups: list[dict]) -> str:
    """Build the local-only explicit-choice UI for legacy active duplicates."""
    fieldsets = []
    for group_index, group in enumerate(groups):
        choices = []
        for row in group["rows"]:
            holding_id = int(row["id"])
            notes = str(row.get("notes") or "").strip()
            details = [
                f"Company: {_display_value(row.get('company_name'))}",
                f"Shares: {_display_value(row.get('shares'))}",
                f"Average cost: {_display_value(row.get('avg_cost'))}",
                f"Class: {_display_value(row.get('hold_class'))}",
                f"Watchlist: {_display_value(row.get('is_watchlist'))}",
                f"Target weight (basis points): "
                f"{_display_value(row.get('target_weight_bps'))}",
                f"Thesis reviewed: {_display_value(row.get('thesis_reviewed_at'))}",
                f"Thesis review interval (days): "
                f"{_display_value(row.get('thesis_review_interval_days'))}",
                f"Added: {_display_value(row.get('added_at'))}",
            ]
            if notes:
                details.append(f"Notes: {notes}")
            detail = " · ".join(details)
            choices.append(
                f'<label class="choice"><input type="radio" '
                f'name="choice-{group_index}" value="{holding_id}"> '
                f'<span><strong>Row {holding_id} · '
                f'{html.escape(str(row.get("stored_ticker") or ""))}</strong>'
                f'<small>{html.escape(detail)}</small></span></label>'
            )
        fieldsets.append(
            f'<fieldset data-portfolio="{int(group["portfolio_id"])}" '
            f'data-ticker="{html.escape(str(group["ticker"]))}">'
            f'<legend>Portfolio {int(group["portfolio_id"])} · '
            f'{html.escape(str(group["ticker"]))}</legend>{"".join(choices)}</fieldset>'
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline' 'unsafe-eval'">
<title>Resolve duplicate holdings safely</title><style>
:root {{ color-scheme: light dark;
font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
body {{ margin: 0; padding: 32px; background: Canvas; color: CanvasText; }}
main {{ max-width: 760px; margin: auto; }} h1 {{ margin: 0 0 10px; font-size: 25px; }}
p {{ line-height: 1.5; }} fieldset {{
border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
border-radius: 12px; margin: 20px 0; padding: 12px; }}
legend {{ font-weight: 700; padding: 0 8px; }}
.choice {{ display: flex; gap: 10px; padding: 11px; border-radius: 9px; cursor: pointer; }}
.choice:has(input:checked) {{ background: color-mix(in srgb, Highlight 18%, Canvas); }}
.choice small {{ display: block; opacity: .76; margin-top: 4px; line-height: 1.4; }}
button {{ border: 0; border-radius: 9px; background: Highlight; color: HighlightText;
font: inherit; font-weight: 700; padding: 11px 16px; cursor: pointer; }}
button:disabled {{ opacity: .55; cursor: wait; }} #status {{ min-height: 24px; font-weight: 600; }}
.safety {{ padding: 13px 15px; border-radius: 10px;
background: color-mix(in srgb, Highlight 11%, Canvas); }}
</style></head><body><main><h1>Resolve duplicate holdings safely</h1>
<p>FolioOrb found active rows that normalize to the same ticker. Choose the one row
to keep active in each group.</p><p class="safety">After you confirm, FolioOrb first
creates a verified Backup Vault copy. It then archives the other rows without
recording a sale or changing their shares, cost basis, notes, or history.</p>
{"".join(fieldsets)}
<button id="resolve" type="button" disabled>Create backup and apply choices</button>
<p id="status" role="status" aria-live="polite">Preparing secure resolver…</p>
<script>
const button = document.getElementById('resolve');
const status = document.getElementById('status');
let bridgeReady = false;
const everyGroupChosen = () => Array.from(document.querySelectorAll('fieldset'))
  .every(group => group.querySelector('input:checked'));
const refreshAction = () => {{
  button.disabled = !(bridgeReady && everyGroupChosen());
  if (bridgeReady && !everyGroupChosen()) {{
    status.textContent = 'Choose one row to keep in every group.';
  }} else if (bridgeReady) {{
    status.textContent = '';
  }}
}};
const markReady = () => {{
  if (window.pywebview && window.pywebview.api &&
      typeof window.pywebview.api.resolve_duplicates === 'function') {{
    bridgeReady = true;
    refreshAction();
  }}
}};
window.addEventListener('pywebviewready', markReady);
document.querySelectorAll('input[type="radio"]').forEach(input =>
  input.addEventListener('change', refreshAction));
markReady();
button.addEventListener('click', async () => {{
  if (!everyGroupChosen()) {{ refreshAction(); return; }}
  const decisions = Array.from(document.querySelectorAll('fieldset')).map(group => ({{
    portfolio_id: Number(group.dataset.portfolio),
    ticker: group.dataset.ticker,
    keep_id: Number(group.querySelector('input:checked').value)
  }}));
  button.disabled = true; status.textContent = 'Creating a verified backup…';
  try {{
    const result = await window.pywebview.api.resolve_duplicates(decisions);
    if (!result.ok) {{
      status.textContent = result.backup
        ? `No holdings were changed. Verified Backup Vault copy: ${{result.backup}}. ` +
          `${{result.error || 'Close and retry.'}}`
        : `No holdings were changed. ${{result.error || 'Resolution failed.'}}`;
      button.disabled = !(bridgeReady && everyGroupChosen());
      return;
    }}
    status.textContent = `Done. ${{result.archived}} row(s) archived. ` +
      `Backup Vault copy: ${{result.backup}}. Close this window and reopen FolioOrb.`;
    button.hidden = true;
  }} catch (error) {{
    status.textContent = 'The resolver could not confirm completion. Reopen FolioOrb, ' +
      `inspect Backup Vault, and retry. ${{error.message}}`;
    button.disabled = !(bridgeReady && everyGroupChosen());
  }}
}});
</script></main></body></html>"""


class _DuplicateRecoveryBridge:  # pylint: disable=too-few-public-methods
    """Narrow native bridge: one explicit, backup-first duplicate decision."""

    def __init__(self, database_path, data_root, displayed_groups):
        self.database_path = database_path
        self.data_root = data_root
        self.displayed_groups = displayed_groups

    def resolve_duplicates(self, decisions: list[dict]) -> dict:
        try:
            from app.services import holding_conflict_recovery

            result = holding_conflict_recovery.resolve_duplicates(
                self.database_path,
                self.data_root,
                list(decisions or []),
                displayed_groups=self.displayed_groups,
            )
            return {"ok": True, **result}
        except Exception as exc:  # pylint: disable=broad-except
            return {
                "ok": False,
                "error": str(exc) or type(exc).__name__,
                "backup": getattr(exc, "backup_name", None),
            }


def _surface_duplicate_recovery(detail: str) -> bool:
    """Show explicit duplicate choices; return whether a native UI was shown."""
    print(f"FolioOrb needs a holdings decision: {detail}", file=sys.stderr)
    if not getattr(sys, "frozen", False):
        return False
    try:
        import webview

        from app.paths import resolve_runtime_profile
        from app.services import holding_conflict_recovery

        profile = resolve_runtime_profile()
        if profile.database_path is None:
            return False
        groups = holding_conflict_recovery.list_duplicate_groups(profile.database_path)
        if not groups:
            return False
        bridge = _DuplicateRecoveryBridge(
            profile.database_path, profile.data_root, groups
        )
        window = webview.create_window(
            "Resolve duplicate holdings safely",
            html=_duplicate_recovery_document(groups),
            width=860,
            height=720,
            min_size=(680, 520),
            resizable=True,
            js_api=bridge,
        )
        # Keep an explicit exposed-function registration alongside js_api. It
        # makes the recovery action available in frozen WKWebView builds where
        # object introspection can otherwise initialize an empty api object.
        window.expose(bridge.resolve_duplicates)
        webview.start()
        return True
    except Exception as exc:  # pylint: disable=broad-except
        print(
            f"FolioOrb could not display duplicate recovery: {type(exc).__name__}",
            file=sys.stderr,
        )
        return False


def _surface_server_startup_error(error: str | None) -> None:
    """Translate captured server failures into a user-visible recovery path."""
    if error and error.startswith("DuplicateActiveHoldingsError:"):
        detail = error.removeprefix("DuplicateActiveHoldingsError:").strip()
        if _surface_duplicate_recovery(detail):
            return
        _surface_startup_error(
            "FolioOrb needs a holdings decision",
            detail,
            (
                "No portfolio rows or schema metadata were changed. Reopen FolioOrb "
                "from a packaged build to use its backup-first duplicate resolver, or "
                "follow the v5.16.0 recovery documentation. Do not use Remove as a "
                "substitute, because that action records a sale."
            ),
        )
        return
    _surface_startup_error(
        "FolioOrb could not start",
        error or "The local service did not become healthy within the startup timeout.",
        (
            "No automatic repair was attempted. Close this window, verify the profile "
            "and database are accessible, then retry."
        ),
    )


def _find_free_port(preferred: int) -> int:
    """Return the preferred port if free, otherwise an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # pylint: disable=broad-except
            time.sleep(0.25)
    return False


def _run_server(port: int) -> None:
    try:
        # Imported lazily and inside the thread so the CORS origin below is
        # already set in the environment before app.config builds its
        # settings singleton.
        import uvicorn
        from app.main import app

        uvicorn.run(app, host=HOST, port=port, log_level="warning")
    except Exception as exc:  # pylint: disable=broad-except
        # A daemon thread's default exception hook only prints to stderr,
        # which can be a devnull-backed guard on a windowed build (see the
        # sys.stderr None handling above) — capture it so main() can surface
        # a real reason instead of a generic "timed out" message.
        _STARTUP_STATE["error"] = f"{type(exc).__name__}: {exc}"


def _configure_smoke_environment() -> str:
    """Select an isolated data root before a smoke run imports app modules."""
    smoke_root = tempfile.mkdtemp(prefix="folioorb-smoke-")
    os.environ["FOLIOORB_SMOKE_TEST"] = "1"
    os.environ["FOLIOORB_DATA_DIR"] = smoke_root
    os.environ["ANTHROPIC_API_KEY"] = ""
    smoke_db = os.path.join(smoke_root, "portfolio.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{smoke_db}"
    return smoke_root


def _run_duplicate_recovery_smoke() -> int:
    """Exercise the bundled resolver against disposable financial workflow data."""
    import sqlite3

    from app.paths import resolve_runtime_profile
    from app.services import backup_service, holding_conflict_recovery

    profile = resolve_runtime_profile()
    database = profile.database_path
    if database is None:
        print("Duplicate recovery smoke requires a file SQLite database", file=sys.stderr)
        return 1

    try:
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                "CREATE TABLE holdings ("
                "id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
                "ticker VARCHAR(10) NOT NULL, company_name VARCHAR(200), "
                "shares FLOAT, avg_cost FLOAT, is_active BOOLEAN, "
                "is_watchlist BOOLEAN, hold_class VARCHAR(20), notes TEXT, "
                "thesis_reviewed_at DATETIME, thesis_review_interval_days INTEGER, "
                "target_weight_bps INTEGER, added_at DATETIME);"
                "CREATE TABLE realized_trades (id INTEGER PRIMARY KEY);"
                "INSERT INTO holdings VALUES "
                "(1, 1, 'AAPL', 'Apple old', 2, 100, 1, 0, 'anchor', "
                "'keep history', '2026-01-02', 90, 6000, '2025-01-01');"
                "INSERT INTO holdings VALUES "
                "(2, 1, ' aapl ', 'Apple current', 3, 120, 1, 0, 'trade', "
                "'current thesis', '2026-07-08', 30, 4000, '2025-02-02');"
            )
            connection.commit()
        finally:
            connection.close()

        displayed = holding_conflict_recovery.list_duplicate_groups(database)
        result = holding_conflict_recovery.resolve_duplicates(
            database,
            profile.data_root,
            [{"portfolio_id": 1, "ticker": "AAPL", "keep_id": 2}],
            displayed_groups=displayed,
        )
        backup = profile.data_root / backup_service.BACKUP_DIRNAME / result["backup"]
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT id, shares, avg_cost, is_active, target_weight_bps, notes "
                "FROM holdings ORDER BY id"
            ).fetchall()
            realized = connection.execute("SELECT COUNT(*) FROM realized_trades").fetchone()[0]
        finally:
            connection.close()

        expected = [
            (1, 2.0, 100.0, 0, 6000, "keep history"),
            (2, 3.0, 120.0, 1, 4000, "current thesis"),
        ]
        if (
            result["archived"] != 1
            or rows != expected
            or realized != 0
            or not backup_service.verify_vault_backup(backup)
            or holding_conflict_recovery.list_duplicate_groups(database)
        ):
            raise RuntimeError("duplicate recovery smoke verification failed")
        print("Packaged duplicate recovery smoke passed")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(
            f"Packaged duplicate recovery smoke failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        shutil.rmtree(profile.data_root, ignore_errors=True)


def main() -> int:  # pylint: disable=too-many-return-statements
    # Keep startup barriers together so their ordering remains reviewable.
    # pylint: disable=too-many-branches
    smoke = "--smoke" in sys.argv
    duplicate_recovery_smoke = "--smoke-duplicate-recovery" in sys.argv

    # A package smoke test must never open, migrate, restore, or record launch
    # state against the user's real FolioOrb data. CI normally starts from a
    # clean runner, but local release verification runs the same frozen binary
    # on a developer machine. Force a throwaway database and let app.main
    # suppress every nonessential startup side effect.
    if smoke or duplicate_recovery_smoke:
        _configure_smoke_environment()

    # Choose the desktop origin before *any* import can reach app.config. The
    # pending-restore and duplicate-preflight path imports app.database before
    # the server thread starts; setting this later freezes the settings singleton
    # at port 8000 and makes every mutation from a fallback-port window fail.
    port = _find_free_port(PREFERRED_PORT)
    base_url = f"http://{HOST}:{port}"
    os.environ["CORS_ALLOWED_ORIGINS"] = (
        f"http://127.0.0.1:{port},http://localhost:{port}"
    )

    # Resolve database ownership before pending restore, settings, launch-health,
    # migration, or the server import can create any writable state.
    try:
        from app.paths import ProfileConfigurationError, prepare_runtime_profile

        prepare_runtime_profile()
    except ProfileConfigurationError as exc:
        _surface_startup_error(
            "FolioOrb profile configuration is invalid",
            str(exc),
            (
                "No FolioOrb files were changed. Make DATABASE_URL point to a SQLite "
                "file inside FOLIOORB_DATA_DIR, or unset both variables to use the "
                "default profile, then relaunch."
            ),
        )
        return 1
    except OSError as exc:
        _surface_startup_error(
            "FolioOrb profile could not be prepared",
            f"{type(exc).__name__}: {exc}",
            (
                "Close FolioOrb, check that the profile location is writable and has "
                "free space, then retry. No portfolio migration was started; profile "
                "preparation may have created an empty directory or partial legacy copy."
            ),
        )
        return 1

    if duplicate_recovery_smoke:
        return _run_duplicate_recovery_smoke()

    # Apply an explicitly queued vault restore before the server thread imports
    # app.main and opens SQLAlchemy connections to the live database.
    if not smoke:
        from app.services import backup_service

        try:
            backup_service.acquire_profile_lock()
            restore_result = backup_service.apply_pending_restore()
        except backup_service.ProfileInUseError as exc:
            _surface_startup_error(
                "FolioOrb profile is already open", str(exc),
                "Quit the other FolioOrb process, then reopen this profile.",
            )
            return 1
        except backup_service.RestoreRecoveryError as exc:
            _surface_startup_error(
                "FolioOrb database recovery requires attention", str(exc),
                "Keep every database, staging and failed file intact. "
                "Do not remove the restore-in-progress marker or open the profile "
                "until the original data has been deliberately recovered.",
            )
            return 1
        if restore_result and restore_result.get("installer_status") == "installing":
            return 0

        # Uvicorn logs lifespan exceptions and returns normally instead of
        # propagating them to the server thread. Run the duplicate-only,
        # read-only schema guard here as well so a windowed app can present the
        # explicit resolver instead of timing out with no captured exception.
        from app.database import engine
        from app.schema_meta import (
            DuplicateActiveHoldingsError,
            preflight_active_holding_uniqueness,
        )

        try:
            preflight_active_holding_uniqueness(engine)
        except DuplicateActiveHoldingsError as exc:
            _surface_server_startup_error(
                f"DuplicateActiveHoldingsError: {exc}"
            )
            return 1

    # Count this launch so a run that dies before it's healthy (e.g. a bad
    # update that won't start) is detected and rollback can be offered. Skipped
    # in smoke mode so CI doesn't perturb the counter.
    if not smoke:
        try:
            from app.services import launch_health

            launch_health.record_launch_attempt()
        except Exception:  # pylint: disable=broad-except
            pass

    threading.Thread(target=_run_server, args=(port,), daemon=True).start()

    if not _wait_for_health(base_url, HEALTH_TIMEOUT_SECONDS):
        _surface_server_startup_error(_STARTUP_STATE["error"])
        return 1

    if smoke:
        from app.version import __version__

        print(f"FolioOrb {__version__} started and healthy at {base_url}")
        return 0

    return _launch_window(base_url)


def _safe_download_name(name: str) -> str:
    """Reduce a page-suggested download name to a bare, safe basename.

    The name comes from the web layer, so strip any directory components (an
    accidental or malicious ``../``) and fall back to a sensible default.
    """
    base = os.path.basename(str(name or "").strip())
    return base or "export.csv"


def _fsync_parent(path: str) -> None:
    """Persist an atomic destination swap on POSIX; Windows has no directory fsync."""
    if os.name == "nt":
        return
    parent = os.path.dirname(os.path.abspath(path)) or "."
    fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_binary_file(path: str, payload: bytes) -> str:
    """Write through one private sibling temp, then atomically replace the target."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}-", suffix=".tmp", dir=parent
    )
    try:
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        try:
            _fsync_parent(target)
        except OSError:
            # The complete, file-fsynced replacement is already visible. A
            # directory-fsync failure weakens crash durability but must not be
            # reported as "nothing was written" and invite a risky retry.
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def _write_text_file(path: str, content: str) -> str:
    """Write text as UTF-8, adding exactly one BOM for CSV files.

    Exported CSVs open cleanly in Excel only with a BOM. ``fetch().text()`` in
    the page strips the server's BOM, so content arriving here usually has none —
    write CSV as ``utf-8-sig`` to add one. HTML remains plain UTF-8, and content
    that already carries a BOM is written as-is so it never doubles.
    """
    # CSV gets a BOM for Excel. HTML and other text exports stay plain UTF-8.
    is_csv = str(path).lower().endswith(".csv")
    encoding = "utf-8-sig" if is_csv and not content.startswith("﻿") else "utf-8"
    return _write_binary_file(path, content.encode(encoding))


class _NativeBridge:  # pylint: disable=too-few-public-methods
    """JS ↔ native bridge exposed to the page as ``window.pywebview.api``.

    The WebView has no download chrome: an ``<a download>`` or a blob-URL click
    just navigates and renders the file inline, stranding the user on a text page
    with no back button. ``save_file`` gives the page a real "Save As…" dialog so
    report exports and templates write actual files. The binary backup method
    uses the same native dialog without decoding SQLite. Real browsers never see
    this bridge and keep their own download path.
    """

    def save_file(self, filename: str, content: str) -> dict:
        """Prompt for a location and write ``content`` there.

        Returns ``{"saved": bool, "path": str|None}``; a cancelled dialog is a
        clean ``saved=False`` (not an error).
        """
        try:
            import webview

            window = webview.active_window()
            if window is None:
                return {"saved": False, "path": None}
            result = window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=_safe_download_name(filename)
            )
            # SAVE_DIALOG yields a path string (some builds: a 1-tuple) or None.
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"saved": False, "path": None}
            _write_text_file(path, content or "")
            return {"saved": True, "path": path}
        except Exception as exc:  # pylint: disable=broad-except
            return {"saved": False, "path": None, "error": type(exc).__name__}

    def export_backup(self, name: str) -> dict:
        """Copy one verified database vault item through a native Save dialog."""
        try:
            import webview

            from app.services import backup_service

            source = backup_service.resolve_backup_name(name)
            if not source.exists() or not backup_service.verify_vault_backup(source):
                return {"saved": False, "path": None, "error": "unverified_backup"}
            window = webview.active_window()
            if window is None:
                return {"saved": False, "path": None}
            result = window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=source.name
            )
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"saved": False, "path": None}
            shutil.copyfile(source, path)
            return {"saved": True, "path": path}
        except Exception as exc:  # pylint: disable=broad-except
            return {"saved": False, "path": None, "error": type(exc).__name__}

    def export_portable_records(self) -> dict:
        """Build and save the human-readable records ZIP without text decoding."""
        try:
            import webview

            from app.database import SessionLocal
            from app.services import portfolio_records

            with SessionLocal() as db:
                payload = portfolio_records.build_portable_archive(db)
            window = webview.active_window()
            if window is None:
                return {"saved": False, "path": None}
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename="folioorb-portable-export.zip",
            )
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"saved": False, "path": None}
            _write_binary_file(path, payload)
            return {"saved": True, "path": path}
        except Exception as exc:  # pylint: disable=broad-except
            return {"saved": False, "path": None, "error": type(exc).__name__}

    def export_review_bundle(self, portfolio_id: int, period: str) -> dict:
        """Build and save one Review Bundle without decoding its ZIP."""
        try:
            import webview

            from app.database import SessionLocal
            from app.services import review_bundle

            numeric_id = int(portfolio_id)
            selected_period = str(period)
            with SessionLocal() as db:
                payload = review_bundle.build_review_bundle(
                    db, numeric_id, selected_period
                )
            filename = review_bundle.bundle_filename(numeric_id, selected_period)
            window = webview.active_window()
            if window is None:
                return {"saved": False, "path": None}
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
            )
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"saved": False, "path": None}
            _write_binary_file(path, payload)
            return {"saved": True, "path": path}
        except Exception as exc:  # pylint: disable=broad-except
            return {"saved": False, "path": None, "error": type(exc).__name__}

    def open_url(self, url: str) -> dict:
        """Open an external http(s) link in the user's real browser.

        The WebView has no browser chrome, so a ``target="_blank"`` link strands
        the user in a frameless window (or does nothing). The page routes such
        links here so they open in the default system browser instead. Only
        http/https is allowed — never ``file:``, ``javascript:``, etc.
        """
        try:
            import webbrowser
            from urllib.parse import urlparse

            parsed = urlparse((url or "").strip())
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return {"opened": False, "error": "unsupported_scheme"}
            webbrowser.open(url)
            return {"opened": True}
        except Exception as exc:  # pylint: disable=broad-except
            return {"opened": False, "error": type(exc).__name__}


def _launch_window(base_url: str) -> int:
    """Create the native window (with menu + exit hook) and run the UI loop."""
    import webbrowser

    import webview

    # After several failed launches with a rollback available, open straight to
    # the rollback offer so a broken update is recoverable.
    offer_rollback = False
    try:
        from app.services import launch_health

        offer_rollback = launch_health.should_offer_rollback()
    except Exception:  # pylint: disable=broad-except
        pass

    # `?app=1` tells the dashboard it's running inside the native WebView so it
    # can switch to a lighter rendering profile (no backdrop-filter, fewer
    # ambient animations) for smooth scrolling. The in-browser experience is
    # unaffected. Tab switching is client-side, so this query persists.
    start_url = f"{base_url}/?app=1" + ("&rollback=1" if offer_rollback else "")
    window = webview.create_window(
        "FolioOrb",
        start_url,
        width=1440,
        height=920,
        min_size=(1024, 720),
        js_api=_NativeBridge(),
    )

    # The server is up and the window is created: this launch is healthy, so
    # clear the failed-launch counter.
    try:
        from app.services import launch_health

        launch_health.mark_launch_healthy()
    except Exception:  # pylint: disable=broad-except
        pass

    # Let the update installer quit the app so a launched installer can replace
    # files the running app would otherwise hold open. Falls back to a hard exit
    # if the window can't be destroyed cleanly.
    def _quit_app() -> None:
        try:
            window.destroy()
        except Exception:  # pylint: disable=broad-except
            _hard_exit(0)

    try:
        from app.services import update_installer

        update_installer.register_exit_hook(_quit_app)
    except Exception:  # pylint: disable=broad-except
        pass

    def _check_for_updates() -> None:
        # Drive the in-page update sheet from the native menu. Guarded inside JS
        # so it's a no-op if the page hasn't finished loading updates.js.
        try:
            window.evaluate_js("window.FolioUpdates && window.FolioUpdates.openAndCheck()")
        except Exception:  # pylint: disable=broad-except
            pass

    def _open_in_browser() -> None:
        try:
            webbrowser.open(f"{base_url}/")
        except Exception:  # pylint: disable=broad-except
            pass

    # A native menu with "Check for Updates…" (per the update-system design) and
    # an escape hatch to the default browser. pywebview cannot inject into the
    # standard macOS application menu, so these live under a custom top-level
    # menu. Wrapped defensively: a pywebview build without the menu API still
    # launches the window normally.
    try:
        import webview.menu as wm

        menu_items = [
            wm.Menu(
                "FolioOrb",
                [
                    wm.MenuAction("Check for Updates…", _check_for_updates),
                    wm.MenuSeparator(),
                    wm.MenuAction("Open in Browser", _open_in_browser),
                ],
            )
        ]
        webview.start(menu=menu_items)
    except (ImportError, AttributeError, TypeError):
        webview.start()

    # webview.start() has returned — the window was closed (by the user, or by
    # _quit_app for an install/rollback handoff). Return; __main__ terminates
    # the process via _hard_exit.
    return 0


def _hard_exit(code: int) -> None:
    """Terminate the process immediately, bypassing interpreter finalization.

    Every exit path funnels through here. A normal ``SystemExit``/return would
    run ``Py_FinalizeEx``, which flushes stdout/stderr while the still-running
    daemon threads (uvicorn's server thread, the cache-warmup thread, the
    update-check scheduler) may be mid-write to those same buffered streams. If a
    daemon holds the buffer lock at that moment, CPython aborts with a fatal
    ``_enter_buffered_busy`` error — surfacing as a macOS "FolioOrb quit
    unexpectedly" crash dialog on every quit (reproduced deterministically in the
    frozen build). A desktop app being closed needs no graceful teardown: daemon
    threads die with the process and the OS reclaims the loopback socket, so we
    flush what we can and skip finalization entirely.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # pylint: disable=broad-except
            pass
    os._exit(code)  # pylint: disable=protected-access


def _run() -> int:
    """Run main(), converting ANY escaping exception into an exit code.

    An exception unwinding out of main() (e.g. webview.start() raising something
    other than the ImportError/AttributeError/TypeError we fall back on, a socket
    or thread-start failure, a WebKit init error) must not propagate to normal
    interpreter shutdown — that runs finalization and hits the same daemon-thread
    buffer-flush abort. Catching it here guarantees every exit still leaves via
    _hard_exit.
    """
    try:
        return main()
    except SystemExit as exc:  # an explicit sys.exit somewhere in startup
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:  # pylint: disable=broad-exception-caught
        try:
            print(f"FolioOrb exited on error: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # pylint: disable=broad-except
            pass
        return 1


if __name__ == "__main__":
    _hard_exit(_run())
