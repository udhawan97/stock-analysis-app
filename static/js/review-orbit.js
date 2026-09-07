/**
 * Review Orbit
 * One accessible workspace for review attention, provenance, reports,
 * research comparison, thesis cadence, and verified local backups.
 */
window.ReviewOrbit = (() => {
    const Logic = window.ReviewOrbitLogic;
    const REVIEW_TAB_KEY = "folioorb-review-tab-v1";
    const REVIEW_PERIOD_KEY = "folioorb-review-period-v1";
    const REVIEW_INBOX_FILTER_KEY = "folioorb-review-inbox-filter-v1";
    const REVIEW_TABS = new Set(["inbox", "trust", "report", "compare", "plan", "records", "backups"]);
    const REVIEW_PERIODS = new Set(["month", "quarter"]);
    const REVIEW_INBOX_FILTERS = new Set(["all", "urgent", "attention", "quiet"]);

    function savedChoice(key, allowed, fallback) {
        return Logic.readChoice(localStorage, key, allowed, fallback);
    }

    function rememberChoice(key, value) {
        Logic.writeChoice(localStorage, key, value);
    }

    const state = {
        open: false,
        tab: savedChoice(REVIEW_TAB_KEY, REVIEW_TABS, "inbox"),
        modal: null,
        loaded: new Set(),
        inbox: null,
        trust: null,
        report: null,
        reportPeriod: savedChoice(REVIEW_PERIOD_KEY, REVIEW_PERIODS, "month"),
        inboxFilter: savedChoice(REVIEW_INBOX_FILTER_KEY, REVIEW_INBOX_FILTERS, "all"),
        watchlist: null,
        plan: null,
        planRequestId: 0,
        targetDraftRevision: 0,
        targetSaveInFlight: false,
        planReadbackPending: false,
        overview: null,
        backups: null,
        rehearsalRequestId: 0,
        rehearsalSnapshot: null,
        thesisId: null,
        thesisReturnFocus: null,
        restoreReturnFocus: null,
        restoreStatusUnknown: false,
    };
    const restoreConfirmation = window.FolioInteractionState.createPendingConfirmation();

    const $ = id => document.getElementById(id);
    const orbit = () => $("review-orbit");
    const live = message => {
        const region = $("review-orbit-live");
        if (region) region.textContent = message;
    };
    const money = value => value === null || value === undefined
        ? "Unavailable"
        : new Intl.NumberFormat("en-US", {
            style: "currency", currency: "USD", maximumFractionDigits: 0,
        }).format(Number(value));
    const number = value => value === null || value === undefined
        ? "Unavailable"
        : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(Number(value));
    const pct = value => value === null || value === undefined
        ? "Unavailable"
        : `${number(value)}%`;
    const bpsPct = value => value === null || value === undefined
        ? "Not set"
        : `${number(Number(value) / 100)}%`;
    const preciseMoney = (value, digits = 2) => value === null || value === undefined
        ? "Unavailable"
        : new Intl.NumberFormat("en-US", {
            style: "currency", currency: "USD",
            minimumFractionDigits: digits, maximumFractionDigits: digits,
        }).format(Number(value));
    const dateTime = value => {
        if (!value) return "Unknown time";
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime())
            ? String(value)
            : parsed.toLocaleString(undefined, {
                year: "numeric", month: "short", day: "numeric",
                hour: "numeric", minute: "2-digit", timeZoneName: "short",
            });
    };
    const bytes = value => {
        const amount = Number(value || 0);
        if (amount < 1024) return `${amount} B`;
        if (amount < 1024 ** 2) return `${(amount / 1024).toFixed(1)} KB`;
        return `${(amount / 1024 ** 2).toFixed(1)} MB`;
    };

    function setLoading(id, label = "Loading local review data…") {
        const target = $(id);
        if (target) target.innerHTML = html`<div class="review-loading">${label}</div>`;
    }

    function setError(id, error) {
        const target = $(id);
        if (!target) return;
        target.innerHTML = html`
            <div class="review-empty">
                ${apiErrorMessage(error, "This review surface is temporarily unavailable.")}
            </div>`;
        target.classList.remove("review-refreshing", "review-stale");
        target.removeAttribute("aria-busy");
    }

    function clearRefreshState(id) {
        const target = $(id);
        if (!target) return;
        target.querySelectorAll(":scope > .review-refresh-notice").forEach(notice => notice.remove());
        target.classList.remove("review-refreshing", "review-stale");
        target.removeAttribute("aria-busy");
    }

    function beginLoad(id, label, retainLastSuccess) {
        clearRefreshState(id);
        const target = $(id);
        if (!target) return;
        if (!retainLastSuccess) {
            setLoading(id, label);
            return;
        }
        target.classList.add("review-refreshing");
        target.setAttribute("aria-busy", "true");
    }

    function markStale(id, error, detail = "Showing the last successful data.") {
        const target = $(id);
        if (!target) return;
        clearRefreshState(id);
        target.classList.add("review-stale");
        target.insertAdjacentHTML("afterbegin", html`
            <div class="review-refresh-notice" role="status">
                ${apiErrorMessage(error, "Could not refresh this section.")} ${detail}
            </div>`);
    }

    function markPlanStale(error, options = {}) {
        clearRefreshState("review-course-summary");
        markStale("review-course-card", error, Logic.planStaleDetail(options));
    }

    // Escape unwinds one step at a time: a live restore refuses to be interrupted,
    // an armed one disarms, and only a workspace with nothing pending closes.
    function onEscape() {
        const restoreConfirm = $("review-restore-confirm");
        if (restoreConfirmation.selection && restoreConfirm && !restoreConfirm.hidden) {
            if (restoreConfirmation.pending) live(restoreLockedMessage());
            else cancelRestore();
            return false;
        }
        close();
        return true;
    }

    async function open(tab = state.tab) {
        const root = orbit();
        if (!root) return;
        if (!state.open) {
            state.open = true;
            root.hidden = false;
            root.setAttribute("aria-hidden", "false");
            document.body.classList.add("review-orbit-open");
            $("review-orbit-trigger")?.setAttribute("aria-expanded", "true");
            state.modal = FolioModalSurface.open(root, {
                fallbackFocus: [() => $("review-orbit-trigger")],
                onEscape,
            });
            requestAnimationFrame(() => root.querySelector("[data-review-close]")?.focus());
        }
        activateTab(tab);
        if (!state.loaded.has("inbox")) loadInbox();
        if (!state.loaded.has("trust")) loadTrust();
    }

    function close() {
        const root = orbit();
        if (!root || !state.open) return false;
        if (restoreConfirmation.pending) {
            live(restoreLockedMessage());
            return false;
        }
        if (restoreConfirmation.selection) {
            clearRestoreConfirmation({ restoreFocus: false });
        }
        state.open = false;
        root.hidden = true;
        root.setAttribute("aria-hidden", "true");
        document.body.classList.remove("review-orbit-open");
        $("review-orbit-trigger")?.setAttribute("aria-expanded", "false");
        const modal = state.modal;
        state.modal = null;
        modal?.close();
        return true;
    }

    function activateTab(tab) {
        const button = document.querySelector(`[data-review-tab="${CSS.escape(tab)}"]`);
        const pane = document.querySelector(`[data-review-pane="${CSS.escape(tab)}"]`);
        if (!button || !pane) return;
        state.tab = tab;
        rememberChoice(REVIEW_TAB_KEY, tab);
        document.querySelectorAll("[data-review-tab]").forEach(item => {
            const selected = item === button;
            item.setAttribute("aria-selected", String(selected));
            item.tabIndex = selected ? 0 : -1;
        });
        document.querySelectorAll("[data-review-pane]").forEach(item => {
            item.hidden = item !== pane;
        });
        loadTab(tab);
    }

    function loadTab(tab) {
        if (state.loaded.has(tab)) return;
        if (tab === "inbox") loadInbox();
        if (tab === "trust") loadTrust();
        if (tab === "report") loadReport();
        if (tab === "compare") loadWatchlist();
        if (tab === "plan") loadPlan();
        if (tab === "records") loadRecords();
        if (tab === "backups") loadBackups();
    }

    function renderInbox() {
        const data = state.inbox;
        if (!data) return;
        const badge = $("review-inbox-badge");
        if (badge) {
            badge.hidden = data.count === 0;
            badge.textContent = data.count > 99 ? "99+" : String(data.count);
            badge.setAttribute("aria-label", `${data.count} review items`);
        }
        const tabCount = $("review-tab-count");
        if (tabCount) tabCount.textContent = data.count ? `· ${data.count}` : "";
        const count = $("review-orbit-count");
        if (count) count.textContent = data.count ? `${data.count} items` : "Clear";
        const asof = $("review-orbit-asof");
        if (asof) asof.textContent = `Review as of ${dateTime(data.generated_at)}`;

        const filters = [
            ["all", "All", data.count],
            ["urgent", "Data gaps", data.counts.urgent || 0],
            ["attention", "Needs review", data.counts.attention || 0],
            ["quiet", "On the radar", data.counts.quiet || 0],
        ];
        $("review-inbox-summary").innerHTML = filters.map(([tone, label, total]) => html`
            <button type="button" class="review-summary-cell" data-tone="${tone}"
                    data-inbox-filter="${tone}" aria-pressed="${state.inboxFilter === tone}">
                <strong>${total}</strong>
                <span>${label}</span>
            </button>`).join("");

        const target = $("review-inbox-list");
        const visible = Logic.filterInbox(data.items, state.inboxFilter);
        if (!visible.length) {
            const message = data.items.length
                ? "No review items match this filter."
                : "Nothing needs attention right now. Your review orbit is clear.";
            target.innerHTML = html`<div class="review-empty">${message}</div>`;
            return;
        }
        target.innerHTML = visible.map(item => html`
            <article class="review-inbox-item" data-tone="${item.tone}">
                <span class="review-inbox-dot" aria-hidden="true"></span>
                <div class="review-inbox-copy">
                    <strong>${item.title}</strong>
                    <span>${item.detail}</span>
                </div>
                <button class="review-inbox-action" type="button"
                        data-review-action="${item.action.kind}"
                        data-review-ticker="${item.ticker || ""}"
                        data-review-holding="${item.action.holding_id || ""}">
                    ${item.action.label}
                </button>
            </article>`).join("");
    }

    function setInboxFilter(tone, { restoreFocus = false } = {}) {
        if (!REVIEW_INBOX_FILTERS.has(tone) || tone === state.inboxFilter) return;
        state.inboxFilter = tone;
        rememberChoice(REVIEW_INBOX_FILTER_KEY, tone);
        renderInbox();
        if (restoreFocus) {
            requestAnimationFrame(() => Logic.restoreFilterFocus(document, tone));
        }
        const label = Logic.filterAnnouncement(tone);
        live(`Review inbox filtered to ${label}.`);
    }

    async function loadInbox(force = false) {
        if (!force && state.loaded.has("inbox")) return Logic.refreshOutcome("inbox", 1);
        const hadData = Boolean(state.inbox);
        beginLoad("review-inbox-list", "Loading local review data…", hadData);
        try {
            state.inbox = await PortfolioWorkspace.json("/api/review/inbox");
            state.loaded.add("inbox");
            renderInbox();
            clearRefreshState("review-inbox-list");
            return Logic.refreshOutcome("inbox", 1);
        } catch (error) {
            state.loaded.delete("inbox");
            if (hadData) markStale("review-inbox-list", error);
            else setError("review-inbox-list", error);
            return Logic.refreshOutcome("inbox", 0);
        }
    }

    function renderTrust() {
        const data = state.trust;
        if (!data) return;
        $("review-orbit-mark")?.setAttribute("data-quality", data.overall_quality);
        $("review-trust-principle").textContent = data.principle;
        $("review-trust-grid").innerHTML = data.areas.map(area => {
            const coverage = area.expected === null || area.expected === undefined
                ? `${area.covered} local records`
                : `${area.covered} of ${area.expected} covered`;
            const missing = area.missing?.length ? ` Missing: ${area.missing.join(", ")}.` : "";
            const foreign = area.foreign_currency_tickers?.length
                ? ` Foreign-priced and excluded from USD totals: ${area.foreign_currency_tickers.join(", ")}.`
                : "";
            return html`
                <article class="review-trust-card">
                    <div class="review-trust-card-head">
                        <h4>${area.label}</h4>
                        <span class="review-quality" data-quality="${area.quality}">${area.quality.replace("_", " ")}</span>
                    </div>
                    <p>${coverage}.${missing}${foreign}</p>
                    <p class="review-trust-source">${area.source}${area.latest ? ` · Latest ${area.latest}` : ""}</p>
                    ${area.caveat ? html`<p class="review-trust-source">${area.caveat}</p>` : ""}
                </article>`;
        }).join("");
    }

    async function loadTrust(force = false) {
        if (!force && state.loaded.has("trust")) return Logic.refreshOutcome("trust", 1);
        const hadData = Boolean(state.trust);
        beginLoad("review-trust-grid", "Checking coverage and source freshness…", hadData);
        try {
            state.trust = await PortfolioWorkspace.json("/api/review/trust");
            state.loaded.add("trust");
            renderTrust();
            clearRefreshState("review-trust-grid");
            return Logic.refreshOutcome("trust", 1);
        } catch (error) {
            state.loaded.delete("trust");
            if (hadData) markStale("review-trust-grid", error);
            else setError("review-trust-grid", error);
            return Logic.refreshOutcome("trust", 0);
        }
    }

    function renderReport() {
        const data = state.report;
        if (!data) return;
        const current = data.current;
        const activity = data.period_activity;
        $("review-report-summary").innerHTML = html`
            <article class="review-report-card"><span>Current value</span><strong>${money(current.total_value)}</strong></article>
            <article class="review-report-card"><span>Total return</span><strong>${money(current.total_return)} · ${pct(current.total_return_pct)}</strong></article>
            <article class="review-report-card"><span>Value change since ${data.observed_start || "no stored start"}</span><strong>${money(activity.value_change)}</strong></article>
            <article class="review-report-card"><span>Realized this period</span><strong>${money(activity.realized_gain)}</strong></article>
            <article class="review-report-card"><span>Stored snapshots</span><strong>${data.snapshot_count}</strong></article>
            <article class="review-report-card"><span>History coverage</span><strong>${data.data_quality.history}</strong></article>
            <article class="review-report-card"><span>Theses needing attention</span><strong>${data.thesis_attention.length}</strong></article>
            <article class="review-report-card"><span>Price coverage</span><strong>${data.data_quality.valuation}</strong></article>`;
    }

    function syncReportPeriodUi() {
        const period = REVIEW_PERIODS.has(state.reportPeriod) ? state.reportPeriod : "month";
        const title = $("review-report-title");
        if (title) {
            title.textContent = Logic.reportTitle(period);
        }
        document.querySelectorAll("[data-report-period]").forEach(button => {
            button.setAttribute(
                "aria-pressed",
                String(button.dataset.reportPeriod === period),
            );
        });
    }

    async function loadReport(force = false) {
        if (!force && state.loaded.has("report")) return Logic.refreshOutcome("report", 1);
        const hadData = Boolean(state.report);
        beginLoad("review-report-summary", "Building the review pack from stored history…", hadData);
        try {
            state.report = await PortfolioWorkspace.json(`/api/review/report?period=${encodeURIComponent(state.reportPeriod)}`);
            state.loaded.add("report");
            renderReport();
            clearRefreshState("review-report-summary");
            return Logic.refreshOutcome("report", 1);
        } catch (error) {
            state.loaded.delete("report");
            if (hadData) markStale("review-report-summary", error);
            else setError("review-report-summary", error);
            return Logic.refreshOutcome("report", 0);
        }
    }

    function browserSaveBinary(filename, content, mediaType) {
        const blob = new Blob([content], { type: mediaType });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
    }

    async function exportReport(format) {
        const endpoint = `/api/review/report/export?period=${encodeURIComponent(state.reportPeriod)}&format=${encodeURIComponent(format)}`;
        try {
            const response = await PortfolioWorkspace.response(endpoint);
            const result = await LocalTextExport.saveResponse(response, {
                fallbackFilename: `folioorb-${state.reportPeriod}-review.${format}`,
                mediaType: format === "csv"
                    ? "text/csv;charset=utf-8"
                    : "text/html;charset=utf-8",
            });
            if (result.status === "saved" && result.path) {
                showToast(`Saved ${result.filename}`, "success");
            }
        } catch (error) {
            showToast(apiErrorMessage(error, "Review export failed"), "danger");
        }
    }

    async function saveReviewBundle() {
        const button = $("review-bundle-export");
        const status = $("review-bundle-status");
        if (!button || !status) return;
        if (targetExportBlocked()) {
            showToast(state.planReadbackPending || state.targetSaveInFlight
                ? "Wait for the save and refresh the saved Plan before bundling its snapshot."
                : "Save the target course before bundling its snapshot.", "warning");
            return;
        }
        button.disabled = true;
        status.textContent = "Freezing one quote set and hashing the review receipts…";
        try {
            const api = window.pywebview && window.pywebview.api;
            if (api && typeof api.export_review_bundle === "function") {
                const result = await api.export_review_bundle(
                    PortfolioWorkspace.id,
                    state.reportPeriod,
                );
                status.textContent = result?.saved
                    ? "Review Bundle saved. Keep it somewhere private."
                    : result?.error
                        ? "Review bundle export failed; no complete ZIP was written."
                        : "Save cancelled; no file was written.";
                live(status.textContent);
                return;
            }
            const response = await PortfolioWorkspace.response(
                `/api/review/bundle?period=${encodeURIComponent(state.reportPeriod)}`,
            );
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const filename = Logic.reviewBundleFilename(
                state.reportPeriod,
                state.report?.period_end,
                PortfolioWorkspace.id,
            );
            browserSaveBinary(filename, await response.arrayBuffer(), "application/zip");
            status.textContent = (
                "Review Bundle download requested. "
                + "Your browser controls the destination."
            );
            live(status.textContent);
        } catch (error) {
            status.textContent = apiErrorMessage(
                error,
                "Review bundle export failed; no complete ZIP was written.",
            );
        } finally {
            button.disabled = false;
        }
    }

    function renderBundleVerification(result) {
        const status = $("review-bundle-verification");
        if (!status) return;
        const pending = result?.pending === true;
        const valid = result?.valid === true;
        const card = document.createElement("div");
        const stateName = pending ? "pending" : valid ? "valid" : "invalid";
        card.className = `review-bundle-verdict is-${stateName}`;

        const seal = document.createElement("span");
        seal.className = "review-bundle-seal";
        seal.textContent = pending ? "CHECKING" : valid ? "HASHES MATCH" : "CHECK FAILED";

        const copy = document.createElement("span");
        copy.className = "review-bundle-verdict-copy";
        const title = document.createElement("strong");
        title.textContent = pending
            ? "Checking bundle"
            : valid ? "Integrity check passed" : "Integrity check failed";
        const detail = document.createElement("span");
        if (valid && result.manifest) {
            const period = String(result.manifest.period || "review");
            const periodLabel = period.charAt(0).toUpperCase() + period.slice(1);
            detail.textContent = (
                `${result.checked_files}/${result.expected_files} receipts · `
                + `${periodLabel} · FolioOrb ${result.manifest.app_version} · `
                + dateTime(result.manifest.generated_at_utc)
            );
        } else {
            detail.textContent = result?.message || "The selected ZIP could not be verified.";
        }
        const note = document.createElement("small");
        note.textContent = pending
            ? "Nothing is imported or written to the portfolio."
            : valid
            ? (result.integrity_note || "Matching hashes do not authenticate the bundle creator.")
            : "Do not rely on this copy. Export a fresh bundle from the original profile if available.";
        copy.append(title, detail, note);
        card.append(seal, copy);
        status.replaceChildren(card);

        live(pending
            ? "Checking the selected Review Bundle."
            : valid
            ? "Review Bundle integrity check passed. Four receipts match the manifest."
            : `Review Bundle integrity check failed. ${detail.textContent}`
        );
    }

    async function verifyReviewBundle(file) {
        if (!file) return;
        const button = $("review-bundle-verify");
        const input = $("review-bundle-verify-input");
        if (!button || !input) return;
        button.disabled = true;
        renderBundleVerification({
            pending: true,
            valid: false,
            message: "Reading the selected ZIP and checking four receipt hashes…",
        });
        try {
            if (file.size > 8 * 1024 * 1024) {
                renderBundleVerification({
                    valid: false,
                    message: "The selected Review Bundle exceeds the 8 MiB safety limit.",
                });
                return;
            }
            const response = await fetch("/api/review/bundle/verify", {
                method: "POST",
                headers: { "Content-Type": "application/zip" },
                body: file,
                cache: "no-store",
            });
            const result = await response.json().catch(() => null);
            if (!response.ok) {
                throw new Error(result?.detail || `HTTP ${response.status}`);
            }
            renderBundleVerification(result);
        } catch (error) {
            renderBundleVerification({
                valid: false,
                message: apiErrorMessage(error, "The selected ZIP could not be verified."),
            });
        } finally {
            button.disabled = false;
            input.value = "";
        }
    }

    async function exportSnapshot(kind) {
        const exports = {
            trust: {
                endpoint: "/api/review/trust/export",
                fallback: "folioorb-data-health.csv",
                failure: "Data health export failed",
            },
            plan: {
                endpoint: "/api/review/plan/export",
                fallback: "folioorb-target-plan.csv",
                failure: "Target plan export failed",
            },
        };
        const config = exports[kind];
        if (!config) return;
        if (kind === "plan" && targetExportBlocked()) {
            showToast(state.planReadbackPending || state.targetSaveInFlight
                ? "Wait for the save and refresh the saved Plan before exporting its snapshot."
                : "Save the target course before exporting its snapshot.", "warning");
            return;
        }
        try {
            const response = await PortfolioWorkspace.response(config.endpoint);
            const result = await LocalTextExport.saveResponse(response, {
                fallbackFilename: config.fallback,
                mediaType: "text/csv;charset=utf-8",
            });
            if (result.status === "saved" && result.path) {
                showToast(`Saved ${result.filename}`, "success");
            }
        } catch (error) {
            showToast(apiErrorMessage(error, config.failure), "danger");
        }
    }

    function selectedWatchlist() {
        return Array.from(document.querySelectorAll(".review-watchlist-pick input:checked"));
    }

    function syncCompareButton() {
        const selected = selectedWatchlist();
        const button = $("review-compare-run");
        if (button) button.disabled = selected.length < 2 || selected.length > 3;
    }

    function renderWatchlist() {
        const items = state.watchlist?.items || [];
        const target = $("review-watchlist-picks");
        if (!items.length) {
            target.innerHTML = html`<div class="review-empty">Add at least two research-mode holdings in Manage to compare them here.</div>`;
            return;
        }
        target.innerHTML = items.map(item => html`
            <label class="review-watchlist-pick">
                <input type="checkbox" value="${item.ticker}" data-kind="${item.security_type}">
                <strong>${item.ticker}</strong>
                <span>${item.name}</span>
                <span class="review-watchlist-type">${item.security_type}</span>
            </label>`).join("");
        syncCompareButton();
    }

    async function loadWatchlist(force = false) {
        if (!force && state.loaded.has("compare")) return Logic.refreshOutcome("compare", 1);
        const hadData = Boolean(state.watchlist);
        beginLoad("review-watchlist-picks", "Loading research-mode holdings…", hadData);
        try {
            state.watchlist = await PortfolioWorkspace.json("/api/review/watchlist");
            state.loaded.add("compare");
            renderWatchlist();
            clearRefreshState("review-watchlist-picks");
            return Logic.refreshOutcome("compare", 1);
        } catch (error) {
            state.loaded.delete("compare");
            if (hadData) markStale("review-watchlist-picks", error);
            else setError("review-watchlist-picks", error);
            return Logic.refreshOutcome("compare", 0);
        }
    }

    function displayMetric(key, value) {
        if (value === null || value === undefined || value === "") return "Unavailable";
        if (key === "market_cap" || key === "aum") {
            return new Intl.NumberFormat("en-US", {
                notation: "compact", maximumFractionDigits: 1,
            }).format(Number(value));
        }
        if (["revenue_growth", "gross_margin", "operating_margin", "dividend_yield", "expense_ratio"].includes(key)) {
            return pct(Number(value) * 100);
        }
        if (key === "top_holdings") {
            return value.length ? value.map(item => item.ticker).join(", ") : "Unavailable";
        }
        return number(value);
    }

    function metricLabel(key) {
        return key.replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
    }

    function renderComparison(data) {
        const target = $("review-compare-results");
        target.innerHTML = data.items.map(item => html`
            <article class="review-compare-card">
                <header class="review-compare-card-head">
                    <h4>${item.ticker}</h4>
                    <span>${item.name} · ${money(item.current_price)} · ${pct(item.day_change_pct)} today</span>
                </header>
                <dl class="review-compare-metrics">
                    ${Object.entries(item.metrics).map(([key, value]) => html`
                        <div><dt>${metricLabel(key)}</dt><dd>${displayMetric(key, value)}</dd></div>
                    `)}
                    <div><dt>Thesis</dt><dd>${item.thesis.status.replace("_", " ")}</dd></div>
                </dl>
            </article>`).join("");
        if (data.overlap) {
            target.insertAdjacentHTML("beforeend", String(html`
                <p class="review-overlap-note">
                    Published-holdings overlap: <strong>${pct(data.overlap.overlap_pct)}</strong>
                    across ${data.overlap.shared_count} shared names. ${data.overlap.caveat}
                </p>`));
        }
    }

    async function runCompare() {
        const selected = selectedWatchlist();
        if (selected.length < 2 || selected.length > 3) return;
        const kinds = new Set(selected.map(input => input.dataset.kind));
        if (kinds.size !== 1 || !["STOCK", "ETF"].includes(selected[0].dataset.kind)) {
            showToast("Compare stocks with stocks or ETFs with ETFs.", "warning");
            return;
        }
        setLoading("review-compare-results", "Building a type-aware comparison…");
        try {
            const tickers = selected.map(input => input.value).join(",");
            const data = await PortfolioWorkspace.json(`/api/review/compare?tickers=${encodeURIComponent(tickers)}`);
            renderComparison(data);
        } catch (error) {
            setError("review-compare-results", error);
        }
    }

    function qualityText(value) {
        return String(value || "unavailable").replaceAll("_", " ");
    }

    function renderBookPulse() {
        const data = state.overview;
        if (!data) return;
        const target = $("review-book-pulse");
        const rows = data.items.map(item => html`
            <tr>
                <th scope="row">${item.name}</th>
                <td>${item.known_value_usd === null ? "Unavailable" : money(item.known_value_usd)}</td>
                <td><span class="review-quality" data-quality="${item.data_quality}">${qualityText(item.data_quality)}</span></td>
                <td>${item.error
                    ? "This book could not be valued; other books remain visible."
                    : item.missing_tickers.length || item.foreign_currency_tickers.length
                        ? `Missing ${item.missing_tickers.join(", ") || "none"}; foreign-priced ${item.foreign_currency_tickers.join(", ") || "none"}`
                        : item.data_quality === "empty" ? "No owned positions" : "Available USD coverage"}</td>
            </tr>`);
        target.innerHTML = html`
            <div class="review-book-pulse-head">
                <div><span>All portfolios · known USD only</span><strong>${money(data.known_value_usd)}</strong></div>
                <span class="review-quality" data-quality="${data.data_quality}">${qualityText(data.data_quality)}</span>
            </div>
            <div class="review-table-scroll">
                <table class="review-plan-table">
                    <caption>Known USD value and quote coverage for every saved portfolio</caption>
                    <thead><tr><th>Portfolio</th><th>Known value</th><th>Coverage</th><th>What is excluded</th></tr></thead>
                    <tbody>${rows.length ? rows : html`<tr><td colspan="4">No saved portfolios.</td></tr>`}</tbody>
                </table>
            </div>
            <p class="review-table-note">Foreign prices are not converted. No combined performance percentage is shown.</p>`;
    }

    function renderTargetPlan() {
        const data = state.plan;
        if (!data) return;
        const select = $("review-rehearsal-holding");
        const previousSelection = select?.value;
        const save = $("review-target-save");
        const exportButton = $("review-plan-export");
        const run = $("review-rehearsal-run");
        if (!data.items.length) {
            $("review-course-summary").innerHTML = html`
                <div class="review-empty">Add an owned position before setting a target course. Research-only and zero-share rows stay outside the plan.</div>`;
            $("review-target-table").innerHTML = "";
            if (select) select.innerHTML = '<option value="">No eligible positions</option>';
            if (save) save.disabled = true;
            if (exportButton) exportButton.disabled = true;
            if (run) run.disabled = true;
            return;
        }
        if (save) save.disabled = state.targetSaveInFlight;
        if (run) run.disabled = false;

        const driftItems = data.items.filter(item => item.drift_bps !== null);
        const focal = driftItems.sort((a, b) => Math.abs(b.drift_bps) - Math.abs(a.drift_bps))[0]
            || data.items[0];
        const ringAvailable = data.drift_available && focal.actual_weight_bps !== null
            && focal.target_weight_bps !== null;
        const ringLabel = ringAvailable
            ? `${focal.ticker}: actual ${bpsPct(focal.actual_weight_bps)}, target ${bpsPct(focal.target_weight_bps)}, ${Math.abs(focal.drift_bps)} basis points ${focal.drift_direction}`
            : data.complete
                ? `Drift unavailable because valuation coverage is ${qualityText(data.valuation_quality)}`
                : `Target course incomplete; ${Math.max(data.remaining_bps, 0)} basis points remain`;
        $("review-course-summary").innerHTML = html`
            <div class="review-course-summary">
                <div class="review-course-ring ${ringAvailable ? "" : "is-partial"}"
                     role="img" aria-label="${ringLabel}"
                     style="--target-angle:${Number(focal.target_weight_bps || 0) / 10000 * 360}deg;--actual-angle:${Number(focal.actual_weight_bps || 0) / 10000 * 360}deg">
                    <span><strong>${ringAvailable ? focal.ticker : data.complete ? "Partial" : "Draft"}</strong><small>${ringAvailable ? `${focal.drift_bps > 0 ? "+" : ""}${focal.drift_bps} bps` : `${number(data.target_total_bps)} / 10,000`}</small></span>
                </div>
                <div>
                    <p class="review-eyebrow">Target course</p>
                    <h4>${data.complete ? "Course saved" : "Finish the target mix"}</h4>
                    <p>${ringLabel}.</p>
                    <div class="review-course-legend"><span data-kind="actual">Actual</span><span data-kind="target">Target</span></div>
                </div>
            </div>`;
        const tableRows = data.items.map(item => html`
            <tr>
                <th scope="row">${item.ticker}</th>
                <td>${bpsPct(item.actual_weight_bps)}</td>
                <td><label><span class="visually-hidden">${item.ticker} target basis points</span><input class="review-target-input" type="number" min="0" max="10000" step="1" inputmode="numeric" data-holding-id="${item.holding_id}" value="${item.target_weight_bps ?? ""}" placeholder="bps"></label></td>
                <td>${item.drift_bps === null ? "Unavailable" : `${item.drift_bps > 0 ? "+" : ""}${item.drift_bps} bps · ${qualityText(item.drift_direction)}`}</td>
            </tr>`);
        $("review-target-table").innerHTML = html`
            <div class="review-table-scroll">
                <table class="review-plan-table">
                    <caption>Saved target, current allocation, and descriptive drift by held position</caption>
                    <thead><tr><th>Position</th><th>Actual</th><th>Target (bps)</th><th>Drift</th></tr></thead>
                    <tbody>${tableRows}</tbody>
                    <tfoot><tr><th scope="row">Target total</th><td></td><td>${number(data.target_total_bps)} / 10,000</td><td>${data.complete ? "Complete" : `${number(data.remaining_bps)} bps remaining`}</td></tr></tfoot>
                </table>
            </div>`;
        if (select) {
            select.innerHTML = data.items.map(item => html`
                <option value="${item.holding_id}">${item.ticker}</option>`).join("");
            if (data.items.some(item => String(item.holding_id) === previousSelection)) {
                select.value = previousSelection;
            }
        }
        syncTargetDraftState();
    }

    function targetCourseDirty() {
        if (!state.plan) return false;
        const inputs = Array.from(document.querySelectorAll(".review-target-input")).map(
            input => ({ holdingId: input.dataset.holdingId, value: input.value })
        );
        return Logic.targetCourseDirty(state.plan.items, inputs);
    }

    function targetExportBlocked() {
        return targetCourseDirty() || state.targetSaveInFlight || state.planReadbackPending;
    }

    function syncTargetDraftState({ announce = false } = {}) {
        const dirty = targetCourseDirty();
        const exportButton = $("review-plan-export");
        const status = $("review-target-status");
        if (exportButton) {
            exportButton.disabled = targetExportBlocked() || !state.plan?.items.length;
            exportButton.title = state.targetSaveInFlight
                ? "Wait for the target course save to finish before exporting."
                : state.planReadbackPending
                ? "Refresh the saved Plan before exporting."
                : dirty
                ? "Save the target course before exporting this draft."
                : "Save the persisted target course and a fresh current valuation.";
        }
        if (dirty && status) {
            status.textContent = "Unsaved target changes — save the course before exporting.";
        } else if (status?.textContent.startsWith("Unsaved target changes")) {
            status.textContent = "";
        }
        if (dirty && announce) {
            live("Target course changed; save it before exporting the plan snapshot.");
        }
    }

    function captureTargetDraft() {
        return Array.from(document.querySelectorAll(".review-target-input")).map(input => ({
            holdingId: input.dataset.holdingId,
            value: input.value,
        }));
    }

    function restoreTargetDraft(snapshot) {
        const values = new Map((snapshot || []).map(item => [String(item.holdingId), item.value]));
        document.querySelectorAll(".review-target-input").forEach(input => {
            if (values.has(String(input.dataset.holdingId))) {
                input.value = values.get(String(input.dataset.holdingId));
            }
        });
        syncTargetDraftState();
    }

    async function loadPlan(force = false, {
        savedDraftAwaitingRefresh = false,
        submittedRevision = null,
    } = {}) {
        // A refresh begun during PUT cannot establish the result of that save.
        if (state.targetSaveInFlight && !savedDraftAwaitingRefresh) {
            return Logic.refreshOutcome("plan", 0, 2);
        }
        if (!force && state.loaded.has("plan")) return Logic.refreshOutcome("plan", 2, 2);
        if (force) resetRehearsal();
        const hadPlan = Boolean(state.plan);
        const hadOverview = Boolean(state.overview);
        const requestId = ++state.planRequestId;
        const draftRevision = submittedRevision ?? state.targetDraftRevision;
        beginLoad(
            "review-book-pulse",
            "Valuing each saved portfolio independently…",
            hadOverview,
        );
        if (hadPlan) {
            beginLoad("review-course-card", "Loading target course and available USD quotes…", true);
        } else {
            beginLoad("review-course-summary", "Loading target course and available USD quotes…", false);
        }
        if (!hadPlan) $("review-target-table").innerHTML = "";
        const [planResult, overviewResult] = await Promise.allSettled([
            PortfolioWorkspace.json("/api/review/plan"),
            PortfolioWorkspace.json("/api/review/overview"),
        ]);
        if (requestId !== state.planRequestId) return Logic.refreshOutcome("plan", 0, 2);
        // Capture at completion, so even edits begun after this GET survive.
        const hadUnsavedDraft = hadPlan && targetCourseDirty();
        const preserveDraft = hadPlan && (
            state.targetDraftRevision !== draftRevision
            || (!savedDraftAwaitingRefresh && (hadUnsavedDraft || state.planReadbackPending))
        );
        const draftSnapshot = preserveDraft ? captureTargetDraft() : null;
        let succeeded = 0;
        if (planResult.status === "fulfilled") {
            const wasAwaitingReadback = state.planReadbackPending;
            state.planReadbackPending = false;
            state.plan = planResult.value;
            renderTargetPlan();
            if (draftSnapshot) restoreTargetDraft(draftSnapshot);
            if (wasAwaitingReadback && !savedDraftAwaitingRefresh && !targetCourseDirty()) {
                $("review-target-status").textContent = "Target course read back. No trade was placed.";
            }
            clearRefreshState("review-course-summary");
            clearRefreshState("review-course-card");
            succeeded += 1;
        } else {
            if (hadPlan) markPlanStale(planResult.reason, {
                hadUnsavedDraft,
                savedDraftAwaitingRefresh,
            });
            else setError("review-course-summary", planResult.reason);
        }
        if (overviewResult.status === "fulfilled") {
            state.overview = overviewResult.value;
            renderBookPulse();
            clearRefreshState("review-book-pulse");
            succeeded += 1;
        } else {
            if (hadOverview) markStale("review-book-pulse", overviewResult.reason);
            else setError("review-book-pulse", overviewResult.reason);
        }
        const outcome = {
            ...Logic.refreshOutcome("plan", succeeded, 2),
            planSucceeded: planResult.status === "fulfilled",
            overviewSucceeded: overviewResult.status === "fulfilled",
        };
        if (outcome.status === "complete" || hadPlan) state.loaded.add("plan");
        else state.loaded.delete("plan");
        return outcome;
    }

    async function saveTargets(event) {
        event.preventDefault();
        if (state.targetSaveInFlight) return;
        const status = $("review-target-status");
        const save = $("review-target-save");
        const submittedRevision = state.targetDraftRevision;
        const items = Array.from(document.querySelectorAll(".review-target-input")).map(input => ({
            holding_id: Number(input.dataset.holdingId),
            target_weight_bps: input.value === "" ? null : Number(input.value),
        }));
        state.targetSaveInFlight = true;
        state.planRequestId += 1; // Retire every GET begun before this save.
        clearRefreshState("review-course-summary");
        clearRefreshState("review-course-card");
        clearRefreshState("review-book-pulse");
        if (save) save.disabled = true;
        syncTargetDraftState();
        status.textContent = "Saving target course locally…";
        try {
            try {
                await PortfolioWorkspace.json("/api/review/plan/targets", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ items }),
                });
            } catch (error) {
                status.textContent = apiErrorMessage(error, "Could not save target weights.");
                return;
            }

            state.planReadbackPending = true;
            state.loaded.delete("plan");
            let message;
            try {
                const outcome = await loadPlan(true, {
                    savedDraftAwaitingRefresh: true, submittedRevision,
                });
                if (!outcome.planSucceeded) {
                    message = "Target course saved locally, but the saved Plan could not be read back. No trade was placed.";
                } else if (!outcome.overviewSucceeded) {
                    message = "Target course saved. Portfolio overview refresh is still retryable. No trade was placed.";
                } else {
                    message = "Target course saved. No trade was placed.";
                }
            } catch (error) {
                markPlanStale(error, { savedDraftAwaitingRefresh: true });
                message = "Target course saved locally, but the saved Plan could not be read back. No trade was placed.";
            }
            if (state.targetDraftRevision !== submittedRevision
                && (targetCourseDirty() || state.planReadbackPending)) {
                message += " Newer target changes remain unsaved.";
            }
            status.textContent = message;
            live(message);
        } finally {
            state.targetSaveInFlight = false;
            if (save) save.disabled = !state.plan?.items.length;
            // Update export eligibility without replacing the save/readback receipt.
            const message = status.textContent;
            syncTargetDraftState();
            status.textContent = message;
        }
    }

    function currentRehearsalSnapshot() {
        return {
            holdingId: $("review-rehearsal-holding")?.value || "",
            cashUsd: $("review-rehearsal-cash")?.value.trim() || "",
        };
    }

    function sameRehearsalSnapshot(left, right) {
        return Boolean(left && right &&
            left.holdingId === right.holdingId && left.cashUsd === right.cashUsd);
    }

    function renderRehearsalOutdated({ announce = true } = {}) {
        const target = $("review-rehearsal-result");
        if (!target) return;
        target.innerHTML = html`
            <div class="review-rehearsal-outdated" role="status">
                <strong>Preview outdated</strong>
                <span>Inputs changed. Run the rehearsal again before using this projection.</span>
            </div>`;
        if (announce) live("Buy rehearsal inputs changed; the previous preview is outdated.");
    }

    function resetRehearsal() {
        state.rehearsalRequestId += 1;
        state.rehearsalSnapshot = null;
        $("review-rehearsal-result")?.replaceChildren();
    }

    function invalidateRehearsal() {
        if (!state.rehearsalSnapshot ||
            sameRehearsalSnapshot(state.rehearsalSnapshot, currentRehearsalSnapshot())) return;
        state.rehearsalRequestId += 1;
        state.rehearsalSnapshot = null;
        renderRehearsalOutdated();
    }

    function renderRehearsal(data) {
        const allocation = data.allocation_available
            ? html`<div><span>Projected allocation</span><strong>${pct(data.projected_selected_allocation_pct)}</strong></div><div><span>Largest position</span><strong>${pct(data.projected_largest_position_pct)}</strong></div>`
            : html`<p class="review-rehearsal-partial">Projected allocation is unavailable because one or more required USD quotes are missing or foreign-priced.</p>`;
        $("review-rehearsal-result").innerHTML = html`
            <div class="review-rehearsal-ticket">
                <header><span>${data.ticker} · available USD quote</span><strong>${preciseMoney(data.available_quote_usd)}</strong></header>
                <div class="review-rehearsal-metrics">
                    <div><span>Fractional shares added</span><strong>${number(data.buy_shares)}</strong></div>
                    <div><span>Projected shares</span><strong>${number(data.projected_shares)}</strong></div>
                    <div><span>Projected average cost</span><strong>${preciseMoney(data.projected_avg_cost_usd, 4)}</strong></div>
                    ${allocation}
                </div>
                <p>Fully spends ${preciseMoney(data.cash_usd)}. Quote freshness is unknown. Fees and taxes are excluded. Nothing was written or ordered.</p>
            </div>`;
    }

    async function runRehearsal(event) {
        event.preventDefault();
        const snapshot = currentRehearsalSnapshot();
        const requestId = state.rehearsalRequestId + 1;
        state.rehearsalRequestId = requestId;
        state.rehearsalSnapshot = snapshot;
        setLoading("review-rehearsal-result", "Rehearsing in memory — no portfolio writes…");
        try {
            const data = await PortfolioWorkspace.json("/api/review/plan/rehearsal", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    holding_id: Number(snapshot.holdingId),
                    cash_usd: snapshot.cashUsd,
                }),
            });
            if (requestId !== state.rehearsalRequestId) return;
            if (!sameRehearsalSnapshot(snapshot, currentRehearsalSnapshot())) {
                invalidateRehearsal();
                return;
            }
            renderRehearsal(data);
            live(`Buy rehearsal complete for ${data.ticker}; nothing was written or ordered.`);
        } catch (error) {
            if (requestId !== state.rehearsalRequestId) return;
            if (!sameRehearsalSnapshot(snapshot, currentRehearsalSnapshot())) {
                invalidateRehearsal();
                return;
            }
            setError("review-rehearsal-result", error);
        }
    }

    function loadRecords() {
        if (state.loaded.has("records")) return;
        const year = $("review-recap-year");
        if (year && !year.value) {
            year.max = String(new Date().getUTCFullYear());
            year.value = year.max;
        }
        state.loaded.add("records");
    }

    async function saveRecap(event) {
        event.preventDefault();
        const status = $("review-records-status");
        const year = $("review-recap-year").value;
        status.textContent = "Building the average-cost recap from stored sale facts…";
        try {
            const response = await PortfolioWorkspace.response(`/api/review/records/realized.csv?year=${encodeURIComponent(year)}`);
            const result = await LocalTextExport.saveResponse(response, {
                fallbackFilename: `folioorb-average-cost-recap-${year}.csv`,
                mediaType: "text/csv;charset=utf-8",
            });
            status.textContent = result.status === "saved"
                ? `Saved ${result.filename}.`
                : "Save cancelled; no file was written.";
            live(status.textContent);
        } catch (error) {
            status.textContent = apiErrorMessage(
                error,
                "Average-cost recap export failed; no complete file was written.",
            );
        }
    }

    async function savePortableRecords() {
        const button = $("review-portable-export");
        const status = $("review-records-status");
        button.disabled = true;
        status.textContent = "Building one consistent, human-readable records ZIP…";
        try {
            const api = window.pywebview && window.pywebview.api;
            if (api && typeof api.export_portable_records === "function") {
                const result = await api.export_portable_records();
                status.textContent = result?.saved
                    ? "Portable records ZIP saved. Keep it somewhere private."
                    : result?.error
                        ? "Portable records export failed; no complete ZIP was written."
                        : "Save cancelled; no file was written.";
                live(status.textContent);
                return;
            }
            const response = await PortfolioWorkspace.response("/api/review/records/archive");
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            browserSaveBinary(
                "folioorb-portable-export.zip",
                await response.arrayBuffer(),
                "application/zip",
            );
            status.textContent = "Portable records ZIP saved. Keep it somewhere private.";
        } catch (error) {
            status.textContent = apiErrorMessage(error, "Portable records export failed.");
        } finally {
            button.disabled = false;
        }
    }

    function renderProtection() {
        const protection = state.backups?.protection;
        const target = $("review-protection-status");
        if (!protection || !target) return;
        const manual = protection.manual_freshness;
        const automatic = protection.automatic;
        const manualCopy = manual.status === "none"
            ? "No verified manual backup yet"
            : `${qualityText(manual.status)} · ${manual.age_days} day${manual.age_days === 1 ? "" : "s"} old`;
        const manualTime = manual.latest
            ? `Last verified ${dateTime(manual.latest.created_at)}`
            : "Create one before a major portfolio change";
        const auto = automatic.last_auto_backup;
        const autoCopy = !automatic.auto_backup_enabled
            ? "Off by default"
            : auto
                ? `${qualityText(auto.status)} · ${dateTime(auto.attempted_at_utc)}`
                : "On · first launch check pending";
        target.innerHTML = html`
            <article class="review-protection-card" data-status="${manual.status}">
                <div><p class="review-eyebrow">Manual-backup freshness</p><strong>${manualCopy}</strong><span>${manualTime}. Newer corrupt manual files are ignored.</span></div>
                <span class="review-quality" data-quality="${manual.status === "current" ? "complete" : manual.status === "none" || manual.status === "stale" ? "unavailable" : "partial"}">${manual.status}</span>
            </article>
            <article class="review-protection-card">
                <div><p class="review-eyebrow">Automatic local protection</p><strong>${autoCopy}</strong><span>Checked at launch, at most once per local day. Keeps seven verified auto snapshots only.</span></div>
                <button class="review-auto-switch" id="review-auto-switch" type="button" role="switch"
                        aria-checked="${automatic.auto_backup_enabled}"
                        aria-label="Automatic daily local backups">
                    <span aria-hidden="true"></span>
                </button>
            </article>
            <p class="review-protection-limit">Same-device protection only — copy an exported backup elsewhere for off-device recovery. Automatic snapshots never count as manual-backup freshness.</p>`;
    }

    function renderBackups() {
        const data = state.backups;
        renderProtection();
        const status = $("review-restore-status");
        const pending = data?.pending_restore;
        const restore = data?.last_restore;
        if (status && pending?.name) {
            status.hidden = false;
            status.textContent = `Restore ${pending.name} is queued for the next clean restart.`;
        } else if (status && restore) {
            status.hidden = false;
            status.textContent = restore.status === "restored"
                ? `Restored ${restore.name} successfully. A safety copy of the previous database was kept.`
                : `The restore of ${restore.name} failed before replacing the live database.`;
            if (restore.status === "restored" && restore.installer_status === "failed") {
                status.textContent += " The database was restored, but the previous installer could not be launched.";
            }
            if (restore.environment_status === "failed") {
                status.textContent += " Saved settings could not be restored.";
            }
        } else if (status) {
            status.hidden = true;
        }
        const items = data?.items || [];
        const target = $("review-backup-list");
        if (!items.length) {
            target.innerHTML = html`<div class="review-empty">No vault snapshots yet. Create one before a major portfolio change.</div>`;
            return;
        }
        target.innerHTML = items.map(item => html`
            <article class="review-backup-row">
                <div>
                    <strong>${item.name}</strong>
                    <span>${dateTime(item.created_at)} · ${bytes(item.size_bytes)} · ${item.holding_count} holding rows · ${item.verified ? "verified" : "failed verification"}</span>
                </div>
                <div class="review-backup-actions">
                    <button type="button" class="review-secondary-btn" data-backup-export="${item.name}" ${item.verified ? "" : "disabled"}>Export</button>
                    <button type="button" class="review-danger-btn" data-backup-restore="${item.name}" ${item.verified && !pending ? "" : "disabled"}>Restore…</button>
                </div>
            </article>`).join("");
    }

    async function loadBackups(force = false) {
        if (!force && state.loaded.has("backups")) return Logic.refreshOutcome("backups", 1);
        const hadData = Boolean(state.backups);
        beginLoad("review-backup-list", "Verifying the local vault…", hadData);
        try {
            state.backups = await PortfolioWorkspace.json("/api/review/backups");
            state.loaded.add("backups");
            renderBackups();
            clearRefreshState("review-backup-list");
            return Logic.refreshOutcome("backups", 1);
        } catch (error) {
            state.loaded.delete("backups");
            if (hadData) markStale("review-backup-list", error);
            else setError("review-backup-list", error);
            return Logic.refreshOutcome("backups", 0);
        }
    }

    async function createBackup() {
        const button = $("review-backup-create");
        if (button) button.disabled = true;
        try {
            const item = await PortfolioWorkspace.json("/api/review/backups", { method: "POST" });
            showToast(`Verified backup ${item.name}`, "success");
            state.loaded.delete("backups");
            await loadBackups(true);
        } catch (error) {
            showToast(apiErrorMessage(error, "Backup failed; nothing was changed"), "danger");
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function toggleAutoBackup() {
        const current = Boolean(state.backups?.protection?.automatic?.auto_backup_enabled);
        const button = $("review-auto-switch");
        if (button) button.disabled = true;
        try {
            const protection = await PortfolioWorkspace.json("/api/review/backups/policy", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: !current }),
            });
            state.backups.protection = protection;
            renderProtection();
            requestAnimationFrame(() => $("review-auto-switch")?.focus());
            live(`Automatic local backups ${!current ? "enabled" : "disabled"}.`);
        } catch (error) {
            showToast(apiErrorMessage(error, "Could not change automatic backup policy"), "danger");
            if (button) button.disabled = false;
        }
    }

    async function exportBackup(name) {
        try {
            const api = window.pywebview && window.pywebview.api;
            if (api && typeof api.export_backup === "function") {
                const result = await api.export_backup(name);
                if (result?.saved) showToast(`Saved ${name}`, "success");
                else if (result?.error) showToast("Backup export failed; no complete file was written.", "danger");
                return;
            }
            const anchor = document.createElement("a");
            anchor.href = `/api/review/backups/${encodeURIComponent(name)}/download`;
            anchor.download = name;
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
        } catch (error) {
            showToast(apiErrorMessage(error, "Backup export failed"), "danger");
        }
    }

    function askRestore(name, returnFocus = document.activeElement) {
        if (restoreConfirmation.pending) return;
        restoreConfirmation.select(name);
        state.restoreStatusUnknown = false;
        state.restoreReturnFocus = returnFocus;
        $("review-restore-title").textContent = `Restore ${name}?`;
        $("review-restore-confirm").hidden = false;
        requestAnimationFrame(() => $("review-restore-cancel")?.focus());
    }

    function restoreLockedMessage() {
        return state.restoreStatusUnknown
            ? "Restore status could not be confirmed. Reload FolioOrb and inspect Backup Vault before taking another restore action."
            : "Restore is already being queued; wait for that request to finish.";
    }

    function setRestorePendingUi(
        pending,
        name = restoreConfirmation.selection,
        { unknown = false } = {},
    ) {
        const confirm = $("review-restore-confirm");
        const description = $("review-restore-description");
        const cancel = $("review-restore-cancel");
        const accept = $("review-restore-accept");
        confirm?.setAttribute("aria-busy", String(pending && !unknown));
        document.querySelectorAll("[data-review-tab]").forEach(button => {
            button.disabled = pending;
        });
        if (cancel) cancel.disabled = pending;
        if (accept) {
            accept.disabled = pending;
            accept.textContent = unknown
                ? "Status unknown"
                : pending ? "Queuing…" : "Queue restore";
        }
        if (name) {
            $("review-restore-title").textContent = unknown
                ? `Could not confirm the restore status for ${name}`
                : pending ? `Queuing restore for ${name}…` : `Restore ${name}?`;
        }
        if (description) {
            description.textContent = unknown
                ? "Reload FolioOrb, reopen Backup Vault, and inspect its queued status before retrying."
                : pending
                    ? "Waiting for FolioOrb to record the request. It cannot be cancelled after submission."
                    : "FolioOrb first saves the current database, then restores after a clean restart.";
        }
    }

    function clearRestoreConfirmation({ restoreFocus = true } = {}) {
        const previous = state.restoreReturnFocus;
        const name = restoreConfirmation.selection;
        restoreConfirmation.clear();
        state.restoreStatusUnknown = false;
        state.restoreReturnFocus = null;
        setRestorePendingUi(false, name);
        $("review-restore-confirm").hidden = true;
        if (!restoreFocus) return;
        requestAnimationFrame(() => {
            if (previous?.isConnected) previous.focus();
            else document.querySelector("[data-review-tab='backups']")?.focus();
        });
    }

    function cancelRestore() {
        if (restoreConfirmation.pending) {
            live(restoreLockedMessage());
            return false;
        }
        clearRestoreConfirmation();
        return true;
    }

    async function reconcileRestoreAfterUnknownResponse(name) {
        try {
            const backups = await PortfolioWorkspace.json("/api/review/backups");
            state.backups = backups;
            state.loaded.add("backups");
            renderBackups();
            if (backups.pending_restore?.name === name) {
                clearRestoreConfirmation();
                const message = `Restore ${name} was queued, but its confirmation response was interrupted. Restart FolioOrb to finish.`;
                live(message);
                showToast(message, "warning");
                return;
            }
            restoreConfirmation.fail();
            state.restoreStatusUnknown = false;
            setRestorePendingUi(false, name);
            showToast("Restore was not queued; the status check found no pending restore.", "danger");
            live("Restore was not queued. You can retry or cancel this confirmation.");
            requestAnimationFrame(() => $("review-restore-accept")?.focus());
        } catch (_) {
            state.restoreStatusUnknown = true;
            setRestorePendingUi(true, name, { unknown: true });
            showToast(
                "Could not confirm whether the restore was queued. Reload FolioOrb and inspect Backup Vault before retrying.",
                "danger",
            );
            live(restoreLockedMessage());
        }
    }

    async function acceptRestore() {
        const name = restoreConfirmation.start();
        if (!name) return;
        state.restoreStatusUnknown = false;
        setRestorePendingUi(true, name);
        live(`Queuing restore for ${name}; this step can no longer be cancelled.`);
        try {
            const result = await PortfolioWorkspace.json("/api/review/backups/restore", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            clearRestoreConfirmation({ restoreFocus: !result.will_quit });
            live(result.message);
            showToast(result.message, "warning");
            state.loaded.delete("backups");
            if (!result.will_quit) await loadBackups(true);
        } catch (error) {
            if (!Number.isInteger(error?.status)) {
                await reconcileRestoreAfterUnknownResponse(name);
                return;
            }
            restoreConfirmation.fail();
            state.restoreStatusUnknown = false;
            setRestorePendingUi(false, name);
            showToast(apiErrorMessage(error, "Restore was not queued"), "danger");
            live("Restore was not queued. You can retry or cancel this confirmation.");
            requestAnimationFrame(() => $("review-restore-accept")?.focus());
        }
    }

    function openThesisEditor(holdingId) {
        const thesis = state.inbox?.theses?.find(item => item.holding_id === holdingId);
        if (!thesis) return;
        state.thesisId = holdingId;
        state.thesisReturnFocus = document.activeElement;
        $("review-thesis-title").textContent = `${thesis.ticker} thesis`;
        $("review-thesis-notes").value = thesis.notes || "";
        $("review-thesis-cadence").value = thesis.review_interval_days
            ? String(thesis.review_interval_days)
            : "";
        $("review-thesis-status").textContent = "";
        $("review-thesis-editor").hidden = false;
        requestAnimationFrame(() => $("review-thesis-notes")?.focus());
    }

    function closeThesisEditor() {
        const previous = state.thesisReturnFocus;
        state.thesisId = null;
        state.thesisReturnFocus = null;
        $("review-thesis-editor").hidden = true;
        requestAnimationFrame(() => {
            if (previous?.isConnected) previous.focus();
            else document.querySelector("[data-review-tab='inbox']")?.focus();
        });
    }

    async function saveThesis(event) {
        event.preventDefault();
        if (!state.thesisId) return;
        const status = $("review-thesis-status");
        status.textContent = "Saving locally…";
        const cadence = $("review-thesis-cadence").value;
        try {
            await PortfolioWorkspace.json(`/api/review/thesis/${state.thesisId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    notes: $("review-thesis-notes").value,
                    review_interval_days: cadence ? Number(cadence) : null,
                }),
            });
            status.textContent = "Saved and marked reviewed.";
            showToast("Thesis reviewed", "success");
            state.loaded.delete("inbox");
            await loadInbox(true);
            closeThesisEditor();
        } catch (error) {
            status.textContent = apiErrorMessage(error, "Could not save the thesis.");
        }
    }

    async function handleInboxAction(button) {
        const action = button.dataset.reviewAction;
        if (action === "trust") return activateTab("trust");
        if (action === "report") return activateTab("report");
        if (action === "thesis") return openThesisEditor(Number(button.dataset.reviewHolding));
        if (action === "manage-dca") {
            close();
            window.DcaWorkflow?.open?.();
            return;
        }
        if (action === "holding" && button.dataset.reviewTicker) {
            const ticker = button.dataset.reviewTicker;
            close();
            if (typeof setDashboardZone === "function") setDashboardZone("holdings");
            requestAnimationFrame(() => {
                if (typeof highlightHolding === "function") highlightHolding(ticker);
            });
        }
    }

    async function refresh() {
        const button = $("review-orbit-refresh");
        if (button?.disabled) return;
        if (button) {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
        }
        state.loaded.clear();
        const jobs = [
            ["inbox", loadInbox(true)],
            ["trust", loadTrust(true)],
        ];
        if (state.tab === "report") jobs.push(["report", loadReport(true)]);
        if (state.tab === "compare") jobs.push(["compare", loadWatchlist(true)]);
        if (state.tab === "plan") jobs.push(["plan", loadPlan(true)]);
        if (state.tab === "backups") jobs.push(["backups", loadBackups(true)]);
        try {
            const settled = await Promise.allSettled(jobs.map(([, job]) => job));
            const outcomes = settled.map((result, index) => (
                result.status === "fulfilled" && result.value
                    ? result.value
                    : Logic.refreshOutcome(jobs[index][0], 0)
            ));
            const summary = Logic.summarizeRefresh(outcomes);
            live(summary.message);
        } finally {
            if (button) {
                button.disabled = false;
                button.removeAttribute("aria-busy");
            }
        }
    }

    function bind() {
        document.querySelectorAll("[data-review-close]").forEach(button => {
            button.addEventListener("click", close);
        });
        document.querySelectorAll("[data-review-tab]").forEach(button => {
            button.addEventListener("click", () => activateTab(button.dataset.reviewTab));
            button.addEventListener("keydown", event => {
                if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
                event.preventDefault();
                const tabs = Array.from(document.querySelectorAll("[data-review-tab]"));
                const direction = event.key === "ArrowRight" ? 1 : -1;
                const next = tabs[(tabs.indexOf(button) + direction + tabs.length) % tabs.length];
                activateTab(next.dataset.reviewTab);
                next.focus();
            });
        });
        $("review-orbit-refresh")?.addEventListener("click", refresh);
        $("review-inbox-list")?.addEventListener("click", event => {
            const button = event.target.closest("[data-review-action]");
            if (button) handleInboxAction(button);
        });
        $("review-inbox-summary")?.addEventListener("click", event => {
            const button = event.target.closest("[data-inbox-filter]");
            if (button) {
                setInboxFilter(button.dataset.inboxFilter, { restoreFocus: true });
            }
        });
        $("review-thesis-editor")?.addEventListener("submit", saveThesis);
        $("review-thesis-cancel")?.addEventListener("click", closeThesisEditor);
        document.querySelectorAll("[data-report-period]").forEach(button => {
            button.addEventListener("click", () => {
                state.reportPeriod = button.dataset.reportPeriod;
                rememberChoice(REVIEW_PERIOD_KEY, state.reportPeriod);
                syncReportPeriodUi();
                state.loaded.delete("report");
                loadReport(true);
            });
        });
        syncReportPeriodUi();
        document.querySelectorAll("[data-report-export]").forEach(button => {
            button.addEventListener("click", () => exportReport(button.dataset.reportExport));
        });
        $("review-bundle-export")?.addEventListener("click", saveReviewBundle);
        $("review-bundle-verify")?.addEventListener("click", () => {
            $("review-bundle-verify-input")?.click();
        });
        $("review-bundle-verify-input")?.addEventListener("change", event => {
            verifyReviewBundle(event.target.files?.[0]);
        });
        document.querySelectorAll("[data-review-snapshot-export]").forEach(button => {
            button.addEventListener("click", () => exportSnapshot(button.dataset.reviewSnapshotExport));
        });
        $("review-watchlist-picks")?.addEventListener("change", event => {
            if (!event.target.matches("input[type='checkbox']")) return;
            const selected = selectedWatchlist();
            if (selected.length > 3) {
                event.target.checked = false;
                showToast("Choose no more than three research tickers.", "warning");
            }
            syncCompareButton();
        });
        $("review-compare-run")?.addEventListener("click", runCompare);
        $("review-target-form")?.addEventListener("submit", saveTargets);
        $("review-target-form")?.addEventListener("input", event => {
            if (event.target.matches(".review-target-input")) {
                state.targetDraftRevision += 1;
                syncTargetDraftState({ announce: true });
            }
        });
        const rehearsalForm = $("review-rehearsal-form");
        rehearsalForm?.addEventListener("submit", runRehearsal);
        rehearsalForm?.addEventListener("input", invalidateRehearsal);
        rehearsalForm?.addEventListener("change", invalidateRehearsal);
        $("review-recap-form")?.addEventListener("submit", saveRecap);
        $("review-portable-export")?.addEventListener("click", savePortableRecords);
        $("review-backup-create")?.addEventListener("click", createBackup);
        $("review-protection-status")?.addEventListener("click", event => {
            if (event.target.closest("#review-auto-switch")) toggleAutoBackup();
        });
        $("review-backup-list")?.addEventListener("click", event => {
            const exportButton = event.target.closest("[data-backup-export]");
            const restoreButton = event.target.closest("[data-backup-restore]");
            if (exportButton) exportBackup(exportButton.dataset.backupExport);
            if (restoreButton) askRestore(restoreButton.dataset.backupRestore, restoreButton);
        });
        $("review-restore-cancel")?.addEventListener("click", cancelRestore);
        $("review-restore-accept")?.addEventListener("click", acceptRestore);
    }

    document.addEventListener("DOMContentLoaded", bind);
    return { open, close, refresh, activateTab };
})();
