"""Update checking against the project's GitHub Releases.

No new metadata feed is needed: the GitHub Releases API already carries the
version (tag), publish date, curated notes (the release body), and the platform
assets with their sizes and download URLs. This module wraps that API with:

* ETag/If-None-Match caching and a TTL, so repeated checks don't spend the
  unauthenticated 60/hour rate limit (a 304 costs nothing against it);
* strict numeric semver comparison against ``app.version.__version__``, so only
  genuinely newer versions are ever offered (no downgrades);
* per-platform asset selection (macOS arm64 DMG / Windows x64 installer);
* a small in-memory state machine that the API and UI poll.

Network egress is pinned to ``api.github.com`` / ``github.com`` for this repo.
The single HTTP seam (:func:`_http_get`) is injected in tests, so no test ever
touches the network.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.services.log_safety import sanitize_for_log
from app.version import __version__

logger = logging.getLogger(__name__)


def _build_ssl_context() -> ssl.SSLContext:
    """A TLS context that verifies against certifi's CA bundle.

    Critical for the PyInstaller-frozen app: the bundled OpenSSL's default
    verify paths point at a build-machine location (e.g. Homebrew's
    ``/opt/homebrew/etc/openssl@3/cert.pem``) that does not exist on a user's
    machine, so ``urlopen``'s default context loads NO CA certs and every HTTPS
    request fails cert verification — which the old code swallowed as "offline".
    certifi ships a real CA bundle and is collected into the app, so pointing the
    context at ``certifi.where()`` makes verification work everywhere.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pylint: disable=broad-except
        # Fall back to the platform default rather than disabling verification.
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()

REPO = os.getenv("FOLIO_UPDATE_REPO", "udhawan97/FolioOrb")
API_BASE = "https://api.github.com"
RELEASES_LATEST_URL = f"{API_BASE}/repos/{REPO}/releases/latest"
CACHE_TTL_SECONDS = 6 * 3600
_USER_AGENT = f"FolioOrb-Updater/{__version__}"
_SHASUMS_ASSET = "SHA256SUMS.txt"


class UpdateStatus(str, Enum):
    """Lifecycle states. Phase 2 reaches the first six; the rest are for later."""

    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    OFFLINE = "offline"
    ERROR = "error"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    BACKING_UP = "backing_up"
    READY = "ready"
    INSTALLING = "installing"
    ROLLBACK_PENDING = "rollback_pending"


class UpdateOffline(Exception):
    """Genuine network unreachability (no route, DNS failure, connection timeout).

    ``reason`` is a short machine code ("dns", "timeout", "unreachable") for
    diagnostics; the user still sees the friendly "You're offline" state.
    """

    def __init__(self, reason: str = "unreachable", detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class UpdateTLSError(Exception):
    """TLS/certificate verification failure — reachable but not securely.

    Distinct from offline: the network is up, but the certificate couldn't be
    validated (the classic frozen-app missing-CA-bundle case, or a MITM proxy).
    """

    reason = "tls"

    def __init__(self, detail: str = ""):
        super().__init__(detail or "tls")
        self.detail = detail


class UpdateRateLimited(Exception):
    """GitHub returned 403/429 — the unauthenticated rate limit was hit."""

    reason = "rate_limited"


class UpdateError(Exception):
    """Any other failure to obtain or parse release metadata.

    ``reason`` distinguishes "malformed" (bad JSON), "server" (5xx) and
    "unexpected" (other HTTP status) for the diagnostic surface.
    """

    def __init__(self, message: str, reason: str = "unexpected"):
        super().__init__(message)
        self.reason = reason


@dataclass
class UpdateInfo:  # pylint: disable=too-many-instance-attributes
    """A single available release, resolved for the current platform."""

    version: str
    name: str
    published_at: str | None
    notes_md: str
    channel: str  # "stable" or "dev"
    release_url: str | None
    download_url: str | None
    asset_name: str | None
    size_bytes: int | None
    sha256_url: str | None
    sha256_sig_url: str | None
    restart_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _State:  # pylint: disable=too-many-instance-attributes
    status: UpdateStatus = UpdateStatus.IDLE
    current_version: str = __version__
    available: UpdateInfo | None = None
    last_checked_at: str | None = None
    error: str | None = None
    # Machine-readable diagnostic for a failed check: "dns", "timeout",
    # "unreachable", "tls", "rate_limited", "malformed", "server", "local", etc.
    # None on success. Lets us distinguish failure modes without exposing detail.
    reason: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "current_version": self.current_version,
            "available": self.available.to_dict() if self.available else None,
            "last_checked_at": self.last_checked_at,
            "error": self.error,
            "reason": self.reason,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
        }


_state = _State()
_state_lock = threading.Lock()
_cache: dict[str, Any] = {"etag": None, "fetched_at": 0.0, "payload": None}
_cache_lock = threading.Lock()
# Set once per process at startup: whether this launch is the first run on a
# newly-installed version, and what version preceded it.
_launch: dict[str, Any] = {
    "just_updated": False,
    "previous_version": None,
    # True on the first start after a macOS in-app update swap failed and the old
    # bundle was relaunched — the UI shows a "couldn't install" reassurance.
    "update_failed": False,
}


def note_launch() -> dict[str, Any]:
    """Detect a post-update first run by comparing __version__ to last-seen.

    Records the current version as last-seen so the confirmation shows only once.
    Also picks up a failed macOS bundle-swap marker. Called at startup; returns
    the launch info for convenience.
    """
    try:
        from app import app_settings

        stored = app_settings.load_settings()
        last = stored.get("last_seen_version")
        if last and last != __version__:
            _launch["just_updated"] = True
            _launch["previous_version"] = last
        if last != __version__:
            app_settings.save_settings({"last_seen_version": __version__})
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("note_launch failed: %s", type(exc).__name__)
    try:
        if current_platform_key() == "macos":
            from app.services import macos_updater

            if macos_updater.consume_failed_marker():
                _launch["update_failed"] = True
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("macOS update-marker check failed: %s", type(exc).__name__)
    return dict(_launch)


def launch_info() -> dict[str, Any]:
    return dict(_launch)


# --------------------------------------------------------------------------- #
# Version parsing
# --------------------------------------------------------------------------- #
def parse_version(value: str) -> tuple[int, int, int] | None:
    """Parse ``"4.3.4"`` or ``"v4.3.4"`` into ``(4, 3, 4)``; None if not numeric.

    Any pre-release/build suffix after the third component is ignored. A value
    that isn't a numeric dotted version (e.g. ``"latest-main"``) returns None and
    is treated as "not an upgrade", which keeps the comparison downgrade-safe.
    """
    if not value:
        return None
    cleaned = value.strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2].split("-")[0].split("+")[0]))
    except (ValueError, IndexError):
        return None


def is_newer(candidate: str, current: str) -> bool:
    """True only when ``candidate`` is a strictly higher numeric semver."""
    cand = parse_version(candidate)
    cur = parse_version(current)
    if cand is None or cur is None:
        return False
    return cand > cur


# --------------------------------------------------------------------------- #
# Platform / asset selection
# --------------------------------------------------------------------------- #
def current_platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "other"


def _select_asset(assets: list[dict[str, Any]], platform_key: str) -> dict[str, Any] | None:
    """Pick the installer asset for the given platform from a release's assets."""
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if platform_key == "macos" and name.endswith(".dmg") and "macos" in name:
            return asset
        if platform_key == "windows" and name.endswith(".exe") and "windows" in name:
            return asset
    return None


def _find_asset_url(assets: list[dict[str, Any]], filename: str) -> str | None:
    for asset in assets:
        if str(asset.get("name", "")) == filename:
            return asset.get("browser_download_url")
    return None


# --------------------------------------------------------------------------- #
# HTTP (single injectable seam)
# --------------------------------------------------------------------------- #
def classify_network_error(exc: BaseException) -> Exception:
    """Map a low-level urlopen/socket error to a typed update exception.

    Crucially separates a TLS/certificate failure (reachable-but-not-secure)
    from genuine unreachability (DNS/timeout/refused) — the old code lumped both
    into "offline", which is exactly why the frozen app (a pure cert problem)
    looked offline while online. Exposed (not underscored) so it can be
    unit-tested with synthetic exceptions, no network required.
    """
    # urllib wraps the underlying cause in URLError.reason.
    reason = getattr(exc, "reason", None)
    candidates = [exc]
    if isinstance(reason, BaseException):
        candidates.append(reason)

    for cand in candidates:
        if isinstance(cand, ssl.SSLError):  # incl. SSLCertVerificationError
            return UpdateTLSError(str(cand))
    for cand in candidates:
        if isinstance(cand, socket.gaierror):
            return UpdateOffline("dns", str(cand))
    for cand in candidates:
        if isinstance(cand, (TimeoutError, socket.timeout)):
            return UpdateOffline("timeout", str(cand))
    return UpdateOffline("unreachable", str(reason if reason is not None else exc))


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """Perform a GET and return ``(status, response_headers, body_bytes)``.

    Uses the certifi-backed TLS context so the frozen app can verify GitHub's
    certificate. Tests monkeypatch this so the network is never touched. A 304 is
    returned as a normal status (urllib raises for it, so it is caught here).
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # A real HTTP response (304/403/404/5xx) — surface the status to the
        # caller, which maps it (rate limit, server error, etc.).
        return exc.code, dict(exc.headers or {}), b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise classify_network_error(exc) from exc


def _fetch_latest_release(force: bool) -> dict[str, Any]:
    """Return the latest-release JSON, using the ETag/TTL cache when possible."""
    with _cache_lock:
        etag = _cache["etag"]
        fetched_at = _cache["fetched_at"]
        payload = _cache["payload"]

    fresh = payload is not None and (time.monotonic() - fetched_at) < CACHE_TTL_SECONDS
    if fresh and not force:
        return payload

    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag

    status, resp_headers, body = _http_get(RELEASES_LATEST_URL, headers)

    if status == 304 and payload is not None:
        with _cache_lock:
            _cache["fetched_at"] = time.monotonic()
        return payload
    if status == 200:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateError("Malformed release metadata", reason="malformed") from exc
        with _cache_lock:
            _cache["etag"] = resp_headers.get("ETag")
            _cache["fetched_at"] = time.monotonic()
            _cache["payload"] = parsed
        return parsed
    if status in (403, 429):
        raise UpdateRateLimited(f"GitHub rate limit (HTTP {status})")
    if status >= 500:
        raise UpdateError(f"GitHub server error (HTTP {status})", reason="server")
    raise UpdateError(f"Unexpected response status {status}", reason="unexpected")


def fetch_release_info(version: str) -> UpdateInfo | None:
    """Resolve the release for a specific version tag (used for rollback installers).

    Returns the platform's :class:`UpdateInfo` for ``v<version>``, or None if the
    tag or its asset can't be found / reached. Not cached — this is a rare path.
    """
    url = f"{API_BASE}/repos/{REPO}/releases/tags/v{version}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    try:
        status, _headers, body = _http_get(url, headers)
    except (UpdateOffline, UpdateTLSError):
        return None
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _release_to_info(payload)


def _release_to_info(payload: dict[str, Any]) -> UpdateInfo:
    """Convert a GitHub release payload into an :class:`UpdateInfo`."""
    tag = str(payload.get("tag_name", ""))
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    channel = "stable" if parse_version(tag) is not None else "dev"
    assets = payload.get("assets") or []
    asset = _select_asset(assets, current_platform_key())
    return UpdateInfo(
        version=version,
        name=str(payload.get("name") or tag),
        published_at=payload.get("published_at"),
        notes_md=str(payload.get("body") or ""),
        channel=channel,
        release_url=payload.get("html_url"),
        download_url=(asset or {}).get("browser_download_url"),
        asset_name=(asset or {}).get("name"),
        size_bytes=(asset or {}).get("size"),
        sha256_url=_find_asset_url(assets, _SHASUMS_ASSET),
        sha256_sig_url=_find_asset_url(assets, f"{_SHASUMS_ASSET}.minisig"),
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_state() -> dict[str, Any]:
    with _state_lock:
        return _state.snapshot()


def _set_state(**changes: Any) -> dict[str, Any]:
    with _state_lock:
        for key, value in changes.items():
            setattr(_state, key, value)
        return _state.snapshot()


def mark(status: "UpdateStatus", **fields: Any) -> dict[str, Any]:
    """Set the lifecycle status (and any extra state fields) from other services.

    Used by the download/install orchestrator to drive the state machine through
    downloading → verifying → backing_up → ready → installing.
    """
    return _set_state(status=status, **fields)


# Friendly, non-technical copy per failure reason. The reason code is also
# returned so the UI/diagnostics can distinguish cases; the message is what a
# user reads if the UI shows it.
_REASON_MESSAGES = {
    "tls": "Couldn't securely check for updates.",
    "rate_limited": "GitHub's rate limit was reached. Try again a little later.",
    "malformed": "Couldn't read the update information from GitHub.",
    "server": "GitHub had a problem. Try again a little later.",
    "unexpected": "Couldn't check for updates.",
    "local": "Couldn't check for updates.",
}


def check_for_updates(force: bool = False) -> dict[str, Any]:
    """Check GitHub for a newer release and update the shared state.

    Never raises. Genuine unreachability maps to ``offline``; a TLS/cert failure,
    rate limit, malformed metadata, server error, or a local exception map to
    ``error`` with a machine-readable ``reason`` and a sanitized friendly
    message. Every failure is logged (sanitized) to the update log.
    """
    from app.services import update_log

    _set_state(status=UpdateStatus.CHECKING, error=None, reason=None)
    try:
        payload = _fetch_latest_release(force)
        info = _release_to_info(payload)
        checked_at = datetime.now(timezone.utc).isoformat()
        _persist_last_checked(checked_at)

        if is_newer(info.version, __version__):
            update_log.event(f"check: available version={info.version}")
            return _set_state(
                status=UpdateStatus.AVAILABLE,
                available=info,
                last_checked_at=checked_at,
                error=None,
                reason=None,
            )
        update_log.event(f"check: up_to_date version={__version__}")
        return _set_state(
            status=UpdateStatus.UP_TO_DATE,
            available=None,
            last_checked_at=checked_at,
            error=None,
            reason=None,
        )
    except UpdateOffline as exc:
        update_log.event(
            f"check: offline reason={exc.reason} detail={sanitize_for_log(exc.detail)}"
        )
        return _set_state(status=UpdateStatus.OFFLINE, reason=exc.reason, error=None)
    except (UpdateTLSError, UpdateRateLimited, UpdateError) as exc:
        reason = getattr(exc, "reason", "unexpected")
        update_log.event(f"check: error reason={reason} detail={sanitize_for_log(exc)}")
        logger.warning("Update check failed (%s): %s", reason, sanitize_for_log(exc))
        return _set_state(
            status=UpdateStatus.ERROR,
            reason=reason,
            error=_REASON_MESSAGES.get(reason, _REASON_MESSAGES["unexpected"]),
        )
    except Exception as exc:  # pylint: disable=broad-except
        # A bug in our own check path is a local failure, not "offline".
        update_log.event(f"check: local error type={type(exc).__name__}")
        logger.exception("Unexpected error during update check")
        return _set_state(
            status=UpdateStatus.ERROR, reason="local", error=_REASON_MESSAGES["local"]
        )


def _persist_last_checked(iso_timestamp: str) -> None:
    try:
        from app import app_settings

        app_settings.save_settings({"last_checked_at": iso_timestamp})
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Could not persist last-checked time: %s", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Background scheduler
# --------------------------------------------------------------------------- #
_scheduler = {"started": False}
_INITIAL_DELAY_SECONDS = 30.0
_INTERVAL_SECONDS = 24 * 3600


def start_auto_check_scheduler() -> None:
    """Start a daemon thread that checks ~30 s after boot, then every 24 h.

    Respects the ``auto_check_updates`` setting on each tick and never crashes
    the app on a failed check. Disabled entirely by the
    ``FOLIO_DISABLE_UPDATE_SCHEDULER`` env var (used by tests/CI).
    """
    if _scheduler["started"] or os.getenv("FOLIO_DISABLE_UPDATE_SCHEDULER"):
        return
    _scheduler["started"] = True

    def _loop() -> None:
        time.sleep(_INITIAL_DELAY_SECONDS)
        while True:
            try:
                from app import app_settings

                if app_settings.load_settings().get("auto_check_updates", True):
                    check_for_updates(force=False)
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("Scheduled update check error: %s", type(exc).__name__)
            time.sleep(_INTERVAL_SECONDS)

    threading.Thread(target=_loop, name="update-scheduler", daemon=True).start()


def _reset_for_tests() -> None:
    """Reset module state so tests start from a clean slate."""
    with _cache_lock:
        _cache.update({"etag": None, "fetched_at": 0.0, "payload": None})
    with _state_lock:
        globals()["_state"] = _State()
    _scheduler["started"] = False
    _launch.update({"just_updated": False, "previous_version": None, "update_failed": False})
