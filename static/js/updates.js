/* ==========================================================================
   updates.js — FolioOrb in-app Software Update surface.

   One calm, single-surface sheet (modelled on the macOS Software Update pane):
   a status region on top that morphs across the update lifecycle, and a
   preferences region below that is always present. A quiet nav pill and a menu
   row both open it. Nothing downloads or installs without an explicit click.

   Talks only to /api/system/* (see app/routers/system.py). All network and
   storage access is defensive so a backend that is mid-rollout (e.g. the
   download endpoint not yet present) degrades to a clear "get it from the
   releases page" fallback rather than breaking the dashboard.
   ========================================================================== */
(function () {
    "use strict";

    var RELEASES_URL = "https://github.com/udhawan97/FolioOrb/releases/latest";
    var POLL_MS = 800;
    var WORKING = { checking: 1, downloading: 1, verifying: 1, backing_up: 1, installing: 1 };
    // Reason-specific titles for a failed check. "offline" is a separate status
    // (real unreachability only) — a TLS/rate-limit/server failure is NOT offline.
    var ERROR_TITLES = {
        tls: "Couldn't securely check for updates",
        rate_limited: "GitHub rate limit reached",
        server: "GitHub had a problem",
        malformed: "Couldn't read update info",
        local: "Couldn't reach the app's update service"
    };

    var el = {};
    var state = null;          // latest state snapshot from the backend
    var settings = null;       // update preferences
    var versionInfo = null;    // { version, is_frozen, platform }
    var rollbackInfo = null;   // { can_rollback, previous_version, offer_rollback }
    var rollbackMode = false;  // showing the rollback-confirm view
    var pollTimer = null;
    var sheetOpen = false;
    var modal = null;

    function $(id) { return document.getElementById(id); }

    function cacheEls() {
        [
            "update-pill", "nav-update-version", "nav-update-dot",
            "update-backdrop", "update-sheet", "update-close",
            "update-mark", "update-spinner", "update-sheet-title", "update-sub",
            "update-progress", "update-progress-fill", "update-progress-meta",
            "update-notes-wrap", "update-notes", "update-trust", "update-trust-text",
            "update-rollback", "update-rollback-restore-data",
            "update-actions", "update-primary", "update-secondary",
            "update-tertiary", "update-notes-link", "update-skip", "update-releases-link",
            "update-pref-auto", "update-pref-notify", "update-version-line", "update-restore"
        ].forEach(function (id) { el[camel(id)] = $(id); });
    }

    function camel(id) {
        return id.replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
    }

    /* ---------------------------------------------------------------- fetch */
    function api(path, opts) {
        return fetch(path, opts).then(function (r) {
            if (!r.ok) { throw new Error("HTTP " + r.status); }
            return r.json();
        });
    }

    function getJSON(path) { return api(path, undefined); }

    function putJSON(path, body) {
        return api(path, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
    }

    function postJSON(path, body) {
        return api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {})
        });
    }

    /* -------------------------------------------------------------- helpers */
    function humanSize(bytes) {
        if (!bytes && bytes !== 0) { return null; }
        var mb = bytes / (1024 * 1024);
        if (mb >= 1024) { return (mb / 1024).toFixed(1) + " GB"; }
        return Math.round(mb) + " MB";
    }

    function relativeTime(iso) {
        if (!iso) { return "not yet"; }
        var then = Date.parse(iso);
        if (isNaN(then)) { return "recently"; }
        var secs = Math.max(0, (Date.now() - then) / 1000);
        if (secs < 60) { return "just now"; }
        var mins = Math.round(secs / 60);
        if (mins < 60) { return mins + (mins === 1 ? " minute ago" : " minutes ago"); }
        var hrs = Math.round(mins / 60);
        if (hrs < 24) { return hrs + (hrs === 1 ? " hour ago" : " hours ago"); }
        var days = Math.round(hrs / 24);
        return days + (days === 1 ? " day ago" : " days ago");
    }

    function show(node, visible) {
        if (node) { node.hidden = !visible; }
    }

    /* Minimal, safe markdown → HTML for release notes. Escapes everything first,
       then applies a tiny allow-list (headings, bold, bullets, links) so a
       malformed body can never inject markup. */
    function renderNotes(md) {
        // Quotes are escaped too, not just &/</>: the link rule below inserts
        // a captured URL into href="$2" — an unescaped " in the release body
        // (e.g. via a malicious PR title pulled into --generate-notes) could
        // otherwise break out of the attribute and inject an event handler.
        var escaped = String(md || "")
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
        var lines = escaped.split(/\r?\n/);
        var html = [];
        var inList = false;
        lines.forEach(function (line) {
            var t = line.trim();
            if (/^[-*]\s+/.test(t)) {
                if (!inList) { html.push("<ul>"); inList = true; }
                html.push("<li>" + inline(t.replace(/^[-*]\s+/, "")) + "</li>");
                return;
            }
            if (inList) { html.push("</ul>"); inList = false; }
            if (/^#{1,6}\s+/.test(t)) {
                html.push("<h4>" + inline(t.replace(/^#{1,6}\s+/, "")) + "</h4>");
            } else if (t === "") {
                html.push("");
            } else {
                html.push("<p>" + inline(t) + "</p>");
            }
        });
        if (inList) { html.push("</ul>"); }
        return html.join("\n");

        function inline(s) {
            return s
                .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
                .replace(/`([^`]+)`/g, "<code>$1</code>")
                .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
                    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        }
    }

    /* ---------------------------------------------------------- pill + menu */
    function refreshPassiveIndicators() {
        var avail = state && state.status === "available" && state.available;
        var notify = !settings || settings.notify_updates !== false;
        var skipped = settings && settings.skipped_version;
        var isSkipped = avail && skipped && state.available.version === skipped;
        var shouldFlag = !!(avail && notify && !isSkipped);

        show(el.updatePill, shouldFlag);
        show(el.navUpdateDot, shouldFlag);
        if (el.navUpdateVersion && versionInfo) {
            el.navUpdateVersion.textContent = "v" + versionInfo.version;
        }
    }

    /* --------------------------------------------------------- sheet render */
    var TONE_GLYPH = {
        neutral: null,
        ok: "M20 6 9 17l-5-5",
        alert: "M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
    };

    function setMark(tone, working) {
        if (el.updateMark) { el.updateMark.setAttribute("data-tone", tone); }
        show(el.updateSpinner, !!working);
    }

    function setPrimary(label, handler) {
        if (!el.updatePrimary) { return; }
        if (!label) { el.updatePrimary.hidden = true; el.updatePrimary.onclick = null; return; }
        el.updatePrimary.hidden = false;
        el.updatePrimary.textContent = label;
        el.updatePrimary.onclick = handler;
    }

    function setSecondary(label, handler) {
        if (!el.updateSecondary) { return; }
        if (!label) { el.updateSecondary.hidden = true; el.updateSecondary.onclick = null; return; }
        el.updateSecondary.hidden = false;
        el.updateSecondary.textContent = label;
        el.updateSecondary.onclick = handler;
    }

    function render() {
        if (rollbackMode) { renderRollbackConfirm(); return; }
        if (!state) { return; }
        var status = state.status;
        var cur = (versionInfo && versionInfo.version) || state.current_version || "";
        var avail = state.available;
        var working = !!WORKING[status];

        // Reset optional regions; each branch re-enables what it needs.
        show(el.updateProgress, false);
        show(el.updateNotesWrap, false);
        show(el.updateTrust, false);
        show(el.updateRollback, false);
        show(el.updateNotesLink, false);
        show(el.updateSkip, false);
        show(el.updateReleasesLink, false);
        show(el.updateTertiary, false);
        setPrimary(null); setSecondary(null);
        setMark("neutral", working);

        if (status === "checking") {
            el.updateSheetTitle.textContent = "Checking for updates…";
            el.updateSub.textContent = "FolioOrb " + cur;
        } else if (status === "up_to_date") {
            setMark("ok", false);
            el.updateSheetTitle.textContent = "You're up to date";
            el.updateSub.textContent = "FolioOrb " + cur + " is the latest version. Last checked "
                + relativeTime(state.last_checked_at) + ".";
            setSecondary("Check Again", function () { runCheck(true); });
        } else if (status === "available" && avail) {
            renderAvailable(cur, avail);
        } else if (status === "downloading" && avail) {
            el.updateSheetTitle.textContent = "Downloading FolioOrb " + avail.version;
            el.updateSub.textContent = "This continues if you close the window.";
            renderProgress();
            setSecondary("Cancel", cancelDownload);
        } else if (status === "verifying") {
            el.updateSheetTitle.textContent = "Verifying download…";
            el.updateSub.textContent = "Confirming the update is exactly what was published.";
        } else if (status === "backing_up") {
            el.updateSheetTitle.textContent = "Backing up your data…";
            el.updateSub.textContent = "Saving a safety copy of your portfolio.";
        } else if (status === "rollback_pending") {
            el.updateSheetTitle.textContent = "Rollback queued";
            el.updateSub.textContent = "Quit and reopen FolioOrb to restore the earlier data and install the previous version. Your current data is unchanged until restart.";
        } else if (status === "installing") {
            el.updateSheetTitle.textContent = "Installing…";
            el.updateSub.textContent = "FolioOrb will restart shortly.";
        } else if (status === "ready" && avail) {
            renderReady(avail);
        } else if (status === "offline") {
            el.updateSheetTitle.textContent = "You're offline";
            el.updateSub.textContent = "FolioOrb will check again when you're back online.";
            setSecondary("Try Again", function () { runCheck(true); });
        } else if (status === "error") {
            setMark("alert", false);
            // Title is specific to the failure reason so "offline" is never shown
            // for a TLS/rate-limit/server problem; the backend also supplies a
            // friendly `error` message we fall back to.
            el.updateSheetTitle.textContent = ERROR_TITLES[state.reason]
                || state.error || "Couldn't check for updates";
            el.updateSub.textContent = "You're still on FolioOrb " + cur
                + ". Your data is untouched.";
            setSecondary("Try Again", function () { runCheck(true); });
            show(el.updateTertiary, true);
            show(el.updateReleasesLink, true);
        } else {
            // idle / unknown — a neutral resting state.
            el.updateSheetTitle.textContent = "Software Update";
            el.updateSub.textContent = "FolioOrb " + cur;
            setSecondary("Check for Updates", function () { runCheck(true); });
        }

        renderPrefs();
        managePolling(working);
    }

    function renderAvailable(cur, avail) {
        var bits = ["You have " + cur];
        var size = humanSize(avail.size_bytes);
        if (size) { bits.push(size); }
        if (avail.restart_required) { bits.push("Requires relaunch"); }
        el.updateSheetTitle.textContent = "FolioOrb " + avail.version + " is available";
        el.updateSub.textContent = bits.join(" · ");

        if (avail.notes_md) {
            el.updateNotes.innerHTML = renderNotes(avail.notes_md);
            show(el.updateNotesWrap, true);
        }
        show(el.updateTrust, true);

        var canInstall = versionInfo && versionInfo.is_frozen && avail.download_url;
        if (canInstall) {
            setPrimary("Update Now", startDownload);
            setSecondary("Later", close);
        } else {
            // Source checkout or no asset: point at the releases page instead.
            el.updateSub.textContent = bits.join(" · ") + " · Update from the releases page";
            setSecondary("Later", close);
            show(el.updateReleasesLink, true);
        }

        show(el.updateTertiary, true);
        // Only ever assign a plain http(s) URL to href — this value comes from
        // the update-check API (ultimately GitHub's release metadata), so a
        // scheme check is cheap insurance against a "javascript:" URI.
        if (avail.release_url && /^https?:\/\//i.test(avail.release_url)) {
            el.updateNotesLink.href = avail.release_url;
            show(el.updateNotesLink, true);
        }
        show(el.updateSkip, true);
        el.updateSkip.onclick = function () { skipVersion(avail.version); };
    }

    function renderReady(avail) {
        setMark("ok", false);
        el.updateSheetTitle.textContent = "Ready to install";
        show(el.updateTrust, true);
        if (versionInfo && versionInfo.platform === "windows") {
            el.updateSub.textContent = "FolioOrb will close, update, and reopen. This takes under a minute.";
            setPrimary("Quit & Install", installNow);
        } else {
            el.updateSub.textContent = "FolioOrb will close and open the installer. Drag it to Applications to finish.";
            setPrimary("Quit & Open Installer", installNow);
        }
        setSecondary("Later", close);
    }

    function renderProgress() {
        show(el.updateProgress, true);
        var total = state.total_bytes || 0;
        var done = state.downloaded_bytes || 0;
        var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
        el.updateProgressFill.style.width = pct + "%";
        var meta = humanSize(done) || "0 MB";
        if (total > 0) { meta += " of " + humanSize(total); }
        el.updateProgressMeta.textContent = meta;
    }

    function renderPrefs() {
        if (el.updateVersionLine && versionInfo) {
            var line = "FolioOrb " + versionInfo.version;
            if (state && state.last_checked_at) {
                line += " · last checked " + relativeTime(state.last_checked_at);
            }
            el.updateVersionLine.textContent = line;
        }
        if (settings) {
            setSwitch(el.updatePrefAuto, settings.auto_check_updates !== false);
            setSwitch(el.updatePrefNotify, settings.notify_updates !== false);
        }
        // Prefer the live, file-existence-checked signal (rollbackInfo) over the
        // settings.json metadata alone — metadata can outlive a pruned/deleted
        // backup file, which would otherwise leave the button enabled for a
        // rollback that's guaranteed to fail server-side.
        var hasRollback = rollbackInfo
            ? !!rollbackInfo.can_rollback
            : !!(settings && settings.rollback_point);
        var busy = !!(state && WORKING[state.status]);
        if (el.updateRestore) { el.updateRestore.disabled = !hasRollback || busy; }
    }

    function setSwitch(node, on) {
        if (!node) { return; }
        node.setAttribute("aria-checked", on ? "true" : "false");
        node.classList.toggle("is-on", !!on);
    }

    /* ------------------------------------------------------------- actions */
    function runCheck(force) {
        setChecking();
        getJSON("/api/system/update/check" + (force ? "?force=true" : ""))
            .then(function (s) { state = s; render(); refreshPassiveIndicators(); })
            .catch(function () {
                // The backend check never throws — it returns a state with its
                // own offline/tls/etc. reason. Reaching here means OUR OWN local
                // API call failed (app service hiccup), which is a "local" error,
                // NOT network offline. Don't mislabel it.
                state = state || { current_version: (versionInfo && versionInfo.version) || "" };
                state.status = "error";
                state.reason = "local";
                state.error = null;
                render();
            });
    }

    function setChecking() {
        state = state || { current_version: versionInfo ? versionInfo.version : "" };
        state.status = "checking";
        render();
    }

    function startDownload() {
        // Phase 4 endpoint. Until it exists, a non-OK response degrades to the
        // releases-page fallback rather than a dead button.
        postJSON("/api/system/update/download")
            .then(function (s) { state = s; render(); })
            .catch(function () { showReleasesFallback(); });
    }

    function cancelDownload() {
        postJSON("/api/system/update/cancel")
            .then(function (s) { state = s; render(); })
            .catch(function () { runCheck(false); });
    }

    function installNow() {
        postJSON("/api/system/update/install")
            .then(function (s) {
                state = s;
                // If the pre-install backup succeeded but the OS launch then
                // failed, a rollback point now exists mid-session — refresh so
                // the Restore button reflects it without needing a reload.
                getJSON("/api/system/rollback/status")
                    .then(function (r) { rollbackInfo = r; renderPrefs(); })
                    .catch(function () {});
                render();
            })
            .catch(function () { showReleasesFallback(); });
    }

    function showReleasesFallback() {
        setMark("alert", false);
        el.updateSheetTitle.textContent = "Finish from the releases page";
        el.updateSub.textContent = "Automatic install isn't available here yet. You can download the "
            + "latest version directly.";
        setPrimary(null); setSecondary("Close", close);
        show(el.updateTertiary, true);
        show(el.updateReleasesLink, true);
        show(el.updateNotesWrap, false);
        show(el.updateProgress, false);
    }

    function skipVersion(version) {
        postJSON("/api/system/update/skip", { version: version })
            .then(function (s) { settings = s; refreshPassiveIndicators(); close(); })
            .catch(function () { close(); });
    }

    /* ------------------------------------------------------------ rollback */
    function openRollbackConfirm() {
        rollbackMode = true;
        // Always start unchecked: restoring pre-update data must be a choice
        // made fresh each time, never carried over from an earlier Cancel.
        if (el.updateRollbackRestoreData) { el.updateRollbackRestoreData.checked = false; }
        if (!sheetOpen) { open(); } else { renderRollbackConfirm(); }
    }

    function renderRollbackConfirm() {
        show(el.updateProgress, false);
        show(el.updateNotesWrap, false);
        show(el.updateTrust, false);
        show(el.updateTertiary, false);
        setMark("neutral", false);

        var prev = (rollbackInfo && rollbackInfo.previous_version) || "the previous version";
        el.updateSheetTitle.textContent = "Restore FolioOrb " + prev;
        el.updateSub.textContent = "A safety copy of your current data is saved first. Your "
            + "holdings will be exactly as they are now unless you choose to restore the "
            + "earlier snapshot too.";
        show(el.updateRollback, true);
        setPrimary("Restore Previous Version", doRollback);
        setSecondary("Cancel", function () { rollbackMode = false; render(); });
        renderPrefs();
    }

    function doRollback() {
        rollbackMode = false;
        var restore = !!(el.updateRollbackRestoreData && el.updateRollbackRestoreData.checked);
        postJSON("/api/system/rollback", { restore_data: restore })
            .then(function (s) { state = s; render(); })
            .catch(function () { showReleasesFallback(); });
    }

    function toggleSetting(key, node) {
        var next = node.getAttribute("aria-checked") !== "true";
        setSwitch(node, next);
        var body = {}; body[key] = next;
        putJSON("/api/system/update/settings", body)
            .then(function (s) { settings = s; refreshPassiveIndicators(); })
            .catch(function () { setSwitch(node, !next); });
    }

    /* ------------------------------------------------------------- polling */
    function managePolling(working) {
        var wantPoll = sheetOpen && working;
        if (wantPoll && !pollTimer) {
            pollTimer = setInterval(function () {
                getJSON("/api/system/update/status")
                    .then(function (s) { state = s; render(); refreshPassiveIndicators(); })
                    .catch(function () {});
            }, POLL_MS);
        } else if (!wantPoll && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    /* ------------------------------------------------------- open / close */
    function open() {
        if (sheetOpen) { return; }
        sheetOpen = true;
        el.updateBackdrop.hidden = false;
        el.updateSheet.hidden = false;
        el.updateSheet.setAttribute("aria-hidden", "false");
        // Force reflow so the transition runs from the hidden state.
        void el.updateSheet.offsetWidth;
        el.updateBackdrop.classList.add("is-open");
        el.updateSheet.classList.add("is-open");
        // The sheet opens from three places (nav pill, overflow menu, update
        // toast) and two of them are dismissed on the way in, so the trigger is
        // often already gone; the nav pill is the one that always comes back.
        modal = window.FolioModalSurface.open(el.updateSheet, {
            fallbackFocus: [function () { return document.getElementById("update-trigger"); }],
            onEscape: function () { close(); return true; }
        });
        if (el.updateClose) { el.updateClose.focus(); }

        // Refresh settings, then check unless we already have a fresh result.
        getJSON("/api/system/update/settings")
            .then(function (s) { settings = s; renderPrefs(); })
            .catch(function () {});

        if (rollbackMode) {
            renderRollbackConfirm();
            return;
        }
        if (!state || state.status === "idle" || !state.last_checked_at) {
            runCheck(false);
        } else {
            render();
        }
    }

    function openAndCheck() {
        open();
        runCheck(true);
    }

    function close() {
        if (!sheetOpen) { return; }
        sheetOpen = false;
        // Dismissing the sheet (X button, Escape, backdrop click) must not leave
        // rollback-confirm "sticky" — otherwise the next open() (e.g. from the
        // nav pill or menu) would jump straight back into it instead of showing
        // normal update status. Only the explicit Cancel action already clears
        // this separately, but dismissal paths bypass that handler entirely.
        rollbackMode = false;
        managePolling(false);
        el.updateBackdrop.classList.remove("is-open");
        el.updateSheet.classList.remove("is-open");
        el.updateSheet.setAttribute("aria-hidden", "true");
        window.setTimeout(function () {
            if (!sheetOpen) {
                el.updateBackdrop.hidden = true;
                el.updateSheet.hidden = true;
            }
        }, 200);
        var closing = modal;
        modal = null;
        if (closing) { closing.close(); }
    }

    /* ---------------------------------------------------------------- init */
    function init() {
        cacheEls();
        if (!el.updateSheet) { return; }

        el.updateClose.addEventListener("click", close);
        el.updateBackdrop.addEventListener("click", close);
        el.updatePrefAuto.addEventListener("click", function () {
            toggleSetting("auto_check_updates", el.updatePrefAuto);
        });
        el.updatePrefNotify.addEventListener("click", function () {
            toggleSetting("notify_updates", el.updatePrefNotify);
        });
        el.updateRestore.addEventListener("click", function () {
            if (!el.updateRestore.disabled) { openRollbackConfirm(); }
        });

        // Prime version + settings + any cached update state so the pill can
        // appear without opening the sheet.
        Promise.all([
            getJSON("/api/system/version").catch(function () { return null; }),
            getJSON("/api/system/update/settings").catch(function () { return null; }),
            getJSON("/api/system/update/status").catch(function () { return null; }),
            getJSON("/api/system/rollback/status").catch(function () { return null; })
        ]).then(function (r) {
            versionInfo = r[0];
            settings = r[1];
            state = r[2];
            rollbackInfo = r[3];
            if (el.navUpdateVersion && versionInfo) {
                el.navUpdateVersion.textContent = "v" + versionInfo.version;
            }
            refreshPassiveIndicators();
            if (versionInfo && versionInfo.just_updated) {
                showUpdatedToast(versionInfo.version);
            } else if (versionInfo && versionInfo.update_failed) {
                showUpdatedToast(versionInfo.version, true);
            }
            // The desktop shell opens with ?rollback=1 after repeated failed
            // launches; surface the rollback offer immediately.
            var wantsRollback = /[?&]rollback=1\b/.test(window.location.search)
                || (rollbackInfo && rollbackInfo.offer_rollback);
            if (wantsRollback && rollbackInfo && rollbackInfo.can_rollback) {
                openRollbackConfirm();
            }
        });
    }

    /* A calm, auto-dismissing confirmation on the first run after an update —
       or, when `failed` is set, that the update couldn't be installed. */
    function showUpdatedToast(version, failed) {
        var toast = document.createElement("div");
        toast.className = "fs-update-toast" + (failed ? " fs-update-toast--warn" : "");
        toast.setAttribute("role", "status");

        var mark = document.createElement("span");
        mark.className = "fs-update-toast-mark";
        mark.textContent = failed ? "!" : "✓";

        var body = document.createElement("div");
        var title = document.createElement("div");
        title.className = "fs-update-toast-title";
        title.textContent = failed
            ? "The update couldn't be installed"
            : "Updated to FolioOrb " + version;
        var sub = document.createElement("div");
        sub.className = "fs-update-toast-sub";
        sub.textContent = failed
            ? "You're still on FolioOrb " + version + " — your data is safe."
            : "Your holdings are intact — backup verified.";
        body.appendChild(title);
        body.appendChild(sub);
        toast.appendChild(mark);
        toast.appendChild(body);
        document.body.appendChild(toast);

        requestAnimationFrame(function () { toast.classList.add("is-in"); });
        window.setTimeout(function () {
            toast.classList.remove("is-in");
            window.setTimeout(function () { toast.remove(); }, 320);
        }, 6000);
    }

    window.FolioUpdates = { open: open, openAndCheck: openAndCheck, close: close };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
