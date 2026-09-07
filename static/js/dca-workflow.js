/**
 * DCA workflow: one private owner for plan UI, pending actions, dialogs, and
 * refresh ordering. The browser-facing interface is deliberately only open().
 */
(function installDcaWorkflow(root, factory) {
    if (typeof module === "object" && module.exports) {
        module.exports = { createDcaWorkflow: factory };
        return;
    }
    const runtime = factory({
        workspace: root.PortfolioWorkspace,
        document: root.document,
        notify: root.showToast,
        // dashboard.js declares this as a top-level const, which is a shared
        // classic-script binding but intentionally not a window property.
        formatMoney: formatCurrency,
        escape: root.escapeHtml,
        openManager: root.openPortfolioManager,
        holdingsChanged: async () => {
            await root.loadManageHoldings({ preserveExisting: true });
            root.refreshDashboardData({ includeManageHoldings: false });
        },
        scheduleFrame: root.requestAnimationFrame.bind(root),
        scheduleIdle: root.scheduleWhenIdle,
        log: root.console,
        modals: root.FolioModalSurface,
    });
    root.DcaWorkflow = { open: runtime.open };
    root.document.addEventListener("DOMContentLoaded", runtime.init, { once: true });
})(typeof window !== "undefined" ? window : globalThis, function createDcaWorkflow({
    workspace,
    document,
    notify = () => {},
    formatMoney = value => String(value),
    escape = value => String(value),
    openManager = () => {},
    holdingsChanged = async () => {},
    scheduleFrame = callback => callback(),
    scheduleIdle = callback => callback(),
    log = { warn: () => {} },
    // The shared modal seam, absent in the node harness (which drives the
    // workflow through a fake document with no layout to contain).
    modals = null,
    confirmAction = null,
}) {
    let initialized = false;
    let mutationInFlight = false;
    let panelLoadId = 0;
    let panelLoaded = false;
    let dialogState = null;

    const byId = id => document.getElementById(id);

    function setPanel(open) {
        const panel = byId("dca-panel");
        const button = byId("dca-btn");
        if (!panel) return false;
        panel.hidden = !open;
        button?.setAttribute("aria-expanded", String(open));
        if (open) loadPanel();
        return true;
    }

    function open() {
        openManager();
        return setPanel(true);
    }

    function togglePanel() {
        const panel = byId("dca-panel");
        if (!panel) return;
        setPanel(panel.hidden);
    }

    function formDefaults() {
        const start = byId("dca-start-date");
        if (!start) return;
        const today = new Date().toISOString().slice(0, 10);
        start.max = today;
        start.value = today;
        const frequency = byId("dca-frequency");
        if (frequency) frequency.value = "weekly";
    }

    async function runCatchup() {
        const data = await mutate(
            "/api/dca/run",
            { method: "POST" },
            { failureMessage: "DCA catch-up failed" },
        );
        if (!data) return;
        const blocked = (data.plans || []).filter(
            plan => plan.status === "needs_currency"
        );
        if (blocked.length) {
            notify(
                `Currency verification required for ${blocked.map(plan => plan.ticker).join(", ")} — review the plan in Manage → DCA`,
                "warning",
            );
        }
        const unpriced = (data.plans || []).filter(plan => plan.price_data === false);
        if (unpriced.length) {
            notify(
                `Couldn't fetch prices for ${unpriced.map(plan => plan.ticker).join(", ")} — DCA buys not booked yet`,
                "warning",
            );
        }
        if (data.buys_added > 0) {
            notify(
                `${data.buys_added} DCA buy${data.buys_added === 1 ? "" : "s"} ready to review in Manage → DCA`,
                "info",
            );
        }
        updateBadge();
    }

    async function updateBadge() {
        const badge = byId("dca-badge");
        if (!badge) return;
        try {
            const data = await workspace.json("/api/dca/contributions?status=pending");
            const count = (data.contributions || []).length;
            badge.textContent = count > 99 ? "99+" : String(count);
            badge.hidden = count === 0;
        } catch (_) { /* cosmetic while offline */ }
    }

    function panelLoadFailed() {
        const status = byId("dca-load-status");
        if (!status) return;
        status.hidden = false;
        const message = panelLoaded
            ? "DCA view is stale — showing the last successful data."
            : "Could not load DCA data. The ledger is unavailable.";
        status.innerHTML = message + '<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="retry-panel">Retry refresh</button>';
    }

    async function loadPanel() {
        const requestId = ++panelLoadId;
        try {
            const [plans, contributionPayload] = await Promise.all([
                workspace.json("/api/dca/plans"),
                workspace.json("/api/dca/contributions?status=all"),
            ]);
            if (requestId !== panelLoadId) return false;
            const contributions = contributionPayload.contributions || [];
            renderPlans(plans.plans || [], contributions);
            renderPending(
                contributions.filter(row => row.status === "pending"),
                plans.plans || [],
            );
            updateBadge();
            const history = byId("dca-history-list");
            if (history && !history.hidden) await loadHistory(contributions);
            panelLoaded = true;
            const status = byId("dca-load-status");
            if (status) {
                status.hidden = true;
                status.innerHTML = "";
            }
            return true;
        } catch (error) {
            if (requestId !== panelLoadId) return false;
            log.warn("DCA panel load failed:", error);
            panelLoadFailed();
            return false;
        }
    }

    function renderPlans(plans, contributions = []) {
        const section = byId("dca-plans-section");
        const list = byId("dca-plans-list");
        if (!section || !list) return;
        section.hidden = plans.length === 0;
        list.innerHTML = plans.map(plan => {
            const appliedIds = contributions
                .filter(row => (
                    Number(row.plan_id) === Number(plan.id)
                    && row.status === "applied"
                ))
                .map(row => Number(row.id));
            const needsCurrency = plan.currency_status === "needs_currency";
            const applied = plan.applied_count
                ? `${formatMoney(plan.applied_amount)} → ${plan.applied_shares.toFixed(4)} sh @ ${formatMoney(plan.applied_avg_cost)}`
                : "nothing applied yet";
            const status = needsCurrency
                ? '<span class="dca-plan-flag">Needs currency verification</span>'
                : plan.is_active
                ? (plan.next_date
                    ? `<span class="dca-plan-next">Next buy ${escape(plan.next_date)}</span>`
                    : "")
                : '<span class="dca-plan-flag">Paused</span>';
            const currencyNotice = needsCurrency
                ? `<div class="dca-plan-sub">${escape(plan.currency_message || "Currency verification is required before future buys can be created or applied.")}</div>`
                : "";
            const toggle = plan.is_active
                ? `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="toggle-plan" data-plan-id="${plan.id}" data-active="true">Pause</button>`
                : (needsCurrency ? "" : `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="toggle-plan" data-plan-id="${plan.id}" data-active="false">Resume</button>`);
            return `
            <div class="dca-plan-card${plan.is_active && !needsCurrency ? "" : " dca-plan-card--paused"}" data-plan-id="${plan.id}">
                <div class="dca-plan-head">
                    <div class="dca-plan-id">
                        <span class="dca-plan-ticker">${escape(plan.ticker)}</span>
                        <span class="dca-plan-terms">${formatMoney(plan.amount)} · ${escape(plan.frequency)}</span>
                    </div>${status}
                </div>
                <div class="dca-plan-sub">Applied so far: ${applied}</div>
                ${currencyNotice}
                <div class="dca-plan-actions">
                    ${toggle}
                    ${needsCurrency ? "" : `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="edit-plan" data-plan-id="${plan.id}" data-amount="${plan.amount}">Edit amount</button>`}
                    ${appliedIds.length ? `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="undo-all" data-plan-id="${plan.id}" data-count="${appliedIds.length}" data-cids="${appliedIds.join(",")}" data-ticker="${escape(plan.ticker)}">Undo applied</button>` : ""}
                    <button type="button" class="btn btn-sm dca-chip-btn dca-chip-btn--danger" data-dca-action="delete-plan" data-plan-id="${plan.id}" data-ticker="${escape(plan.ticker)}">Delete</button>
                </div>
            </div>`;
        }).join("");
    }

    function buyRow(contribution, actions) {
        return `
        <div class="dca-buy-row" data-cid="${contribution.id}">
            <span class="dca-buy-date">${escape(contribution.exec_date)}</span>
            <span class="dca-buy-detail">
                <span class="dca-buy-shares">${contribution.shares.toFixed(4)} sh</span>
                <span class="dca-buy-meta">@ ${formatMoney(contribution.price)} · ${formatMoney(contribution.amount)}</span>
            </span>
            <span class="dca-buy-end">${actions}</span>
        </div>`;
    }

    function renderPending(pending, plans) {
        const section = byId("dca-pending-section");
        const list = byId("dca-pending-list");
        if (!section || !list) return;
        section.hidden = pending.length === 0;
        if (!pending.length) {
            list.innerHTML = "";
            return;
        }
        const plansById = Object.fromEntries(plans.map(plan => [plan.id, plan]));
        const groups = new Map();
        pending.forEach(contribution => {
            if (!groups.has(contribution.plan_id)) groups.set(contribution.plan_id, []);
            groups.get(contribution.plan_id).push(contribution);
        });
        list.innerHTML = [...groups.entries()].map(([planId, buys]) => {
            const plan = plansById[planId];
            const needsCurrency = plan?.currency_status === "needs_currency";
            const ticker = plan?.ticker || buys[0].ticker || "?";
            const terms = plan ? `${formatMoney(plan.amount)} ${escape(plan.frequency)}` : "";
            const total = buys.reduce((sum, contribution) => sum + contribution.amount, 0);
            const contributionIds = buys.map(contribution => contribution.id).join(",");
            const cap = 15;
            const rows = buys.slice(0, cap).map(contribution => buyRow(
                contribution,
                `${needsCurrency ? "" : `<button type="button" class="btn btn-sm btn-success dca-act-btn" data-dca-action="apply" data-cid="${contribution.id}">Apply</button>`}
                 <button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="skip" data-cid="${contribution.id}">Skip</button>`,
            )).join("") + (buys.length > cap
                ? `<div class="dca-more-note">…and ${buys.length - cap} more — use “Apply all ${buys.length}” or “Skip all” above.</div>`
                : "");
            const bulk = buys.length > 1 ? `
                <span class="dca-bulk-actions">
                    ${needsCurrency ? "" : `<button type="button" class="btn btn-sm btn-link dca-bulk-link" data-dca-action="apply-all" data-plan-id="${planId}" data-count="${buys.length}" data-cids="${contributionIds}" data-total="${total}" data-ticker="${escape(ticker)}">Apply all ${buys.length}</button>`}
                    <button type="button" class="btn btn-sm btn-link dca-bulk-link dca-bulk-skip" data-dca-action="skip-all" data-plan-id="${planId}" data-count="${buys.length}" data-cids="${contributionIds}" data-ticker="${escape(ticker)}">Skip all</button>
                </span>` : "";
            const blockedNotice = needsCurrency
                ? '<span class="dca-group-count">Currency verification required — Apply is unavailable.</span>'
                : "";
            return `
            <div class="dca-pending-group">
                <div class="dca-pending-group-head">
                    <span class="dca-group-ticker">${escape(ticker)}</span>
                    ${terms ? `<span class="dca-group-terms">${terms}</span>` : ""}
                    <span class="dca-group-count">${buys.length} buy${buys.length === 1 ? "" : "s"} awaiting</span>${blockedNotice}${bulk}
                </div>${rows}
            </div>`;
        }).join("");
    }

    async function toggleHistory() {
        const list = byId("dca-history-list");
        const button = byId("dca-history-toggle");
        if (!list || !button) return;
        const show = list.hidden;
        list.hidden = !show;
        button.textContent = show ? "Hide history" : "Show history";
        button.setAttribute("aria-expanded", String(show));
        if (show) loadHistory();
    }

    async function loadHistory(contributions = null) {
        const list = byId("dca-history-list");
        if (!list) return;
        try {
            const rows = (contributions ?? (
                await workspace.json("/api/dca/contributions?status=all")
            ).contributions ?? []).filter(item => item.status !== "pending");
            if (!rows.length) {
                list.innerHTML = '<div class="dca-history-empty">No applied or skipped buys yet.</div>';
                return;
            }
            const cap = 80;
            list.innerHTML = rows.slice(0, cap).map(contribution => {
                const action = contribution.status === "applied"
                    ? `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="undo" data-cid="${contribution.id}">Undo</button>`
                    : `<button type="button" class="btn btn-sm dca-chip-btn" data-dca-action="restore" data-cid="${contribution.id}">Restore</button>`;
                const ticker = contribution.ticker
                    ? `<span class="dca-buy-ticker">${escape(contribution.ticker)}</span>`
                    : "";
                return `
                <div class="dca-buy-row dca-buy-row--${contribution.status}" data-cid="${contribution.id}">
                    <span class="dca-buy-date">${ticker}${escape(contribution.exec_date)}</span>
                    <span class="dca-buy-detail"><span class="dca-buy-shares">${contribution.shares.toFixed(4)} sh</span><span class="dca-buy-meta">@ ${formatMoney(contribution.price)} · ${formatMoney(contribution.amount)}</span></span>
                    <span class="dca-buy-end"><span class="dca-buy-status dca-buy-status--${contribution.status}">${escape(contribution.status)}</span>${action}</span>
                </div>`;
            }).join("") + (rows.length > cap
                ? `<div class="dca-history-empty">Showing the latest ${cap} of ${rows.length}.</div>`
                : "");
        } catch (error) {
            log.warn("DCA history load failed:", error);
        }
    }

    function closeDialog(result = null) {
        const dialog = byId("dca-action-dialog");
        const state = dialogState;
        if (!dialog || !state) return;
        dialogState = null;
        dialog.hidden = true;
        dialog.setAttribute("aria-hidden", "true");
        document.querySelector("#portfolioModal > .portfolio-manager-panel")?.removeAttribute("inert");
        state.resolve(result);
        if (state.modal) { state.modal.close(); return; }
        if (state.previousFocus?.focus) scheduleFrame(() => state.previousFocus.focus());
    }

    function openDialog({ title, copy, confirmLabel, warning = "", value = null, danger = false }) {
        if (confirmAction) {
            return confirmAction({ title, copy, confirmLabel, warning, value, danger });
        }
        const dialog = byId("dca-action-dialog");
        const field = byId("dca-action-field");
        const input = byId("dca-action-input");
        if (!dialog || !field || !input || dialogState) return Promise.resolve(null);
        byId("dca-action-title").textContent = title;
        byId("dca-action-copy").textContent = copy;
        const warningElement = byId("dca-action-warning");
        warningElement.textContent = warning;
        warningElement.hidden = !warning;
        const hasValue = value !== null;
        field.hidden = !hasValue;
        input.value = hasValue ? String(value) : "";
        input.classList.remove("is-invalid");
        input.removeAttribute("aria-invalid");
        byId("dca-action-error").hidden = true;
        const submit = byId("dca-action-submit");
        submit.textContent = confirmLabel;
        submit.classList.toggle("btn-primary", !danger);
        submit.classList.toggle("btn-danger", danger);
        const previousFocus = document.activeElement;
        document.querySelector("#portfolioModal > .portfolio-manager-panel")?.setAttribute("inert", "");
        dialog.hidden = false;
        dialog.setAttribute("aria-hidden", "false");
        return new Promise(resolve => {
            // The plan row that carried the trigger is re-rendered by the action
            // this dialog confirms, so the DCA panel's own button is the landmark
            // that outlives it.
            const modal = modals?.open(dialog, {
                document,
                previousFocus,
                fallbackFocus: [() => byId("dca-btn")],
                onEscape: () => { closeDialog(); },
            }) || null;
            dialogState = { resolve, previousFocus, hasValue, modal };
            scheduleFrame(() => {
                const target = hasValue ? input : byId("dca-action-cancel");
                target?.focus();
                if (hasValue) input.select();
            });
        });
    }

    // Only reached when the shared modal seam is unavailable (the node harness).
    function handleDialogKeydown(event) {
        if (!dialogState || dialogState.modal) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeDialog();
        }
    }

    function initDialog() {
        const dialog = byId("dca-action-dialog");
        const form = byId("dca-action-form");
        if (!dialog || !form || form.dataset.bound) return;
        form.dataset.bound = "true";
        form.addEventListener("submit", event => {
            event.preventDefault();
            if (!dialogState) return;
            if (!dialogState.hasValue) {
                closeDialog({ confirmed: true });
                return;
            }
            const input = byId("dca-action-input");
            const amount = Number.parseFloat(input.value);
            if (!Number.isFinite(amount) || amount <= 0) {
                input.classList.add("is-invalid");
                input.setAttribute("aria-invalid", "true");
                byId("dca-action-error").hidden = false;
                input.focus();
                return;
            }
            closeDialog({ confirmed: true, value: amount });
        });
        byId("dca-action-cancel")?.addEventListener("click", () => closeDialog());
        dialog.addEventListener("mousedown", event => {
            if (event.target === dialog) closeDialog();
        });
        document.addEventListener("keydown", handleDialogKeydown, true);
    }

    async function readCanonicalDca() {
        workspace.invalidate?.();
        const [plansPayload, contributionsPayload] = await Promise.all([
            workspace.json("/api/dca/plans"),
            workspace.json("/api/dca/contributions?status=all"),
        ]);
        return {
            plans: plansPayload.plans || [],
            contributions: contributionsPayload.contributions || [],
        };
    }

    function contributionTransition(id, before, after) {
        return state => {
            const contribution = state.contributions.find(row => Number(row.id) === id);
            if (!contribution) return "unknown";
            if (contribution.status === after) return "committed";
            if (contribution.status === before) return "unchanged";
            return "unknown";
        };
    }

    function planPatchTransition(id, before, after) {
        const matches = (plan, expected) => Object.entries(expected).every(
            ([key, value]) => typeof value === "number"
                ? Number(plan[key]) === value
                : plan[key] === value
        );
        return state => {
            const plan = state.plans.find(row => Number(row.id) === id);
            if (!plan) return "unknown";
            if (matches(plan, after)) return "committed";
            if (matches(plan, before)) return "unchanged";
            return "unknown";
        };
    }

    function contributionSetTransition(ids, before, after) {
        return state => {
            if (!ids.length) return "unknown";
            const byId = new Map(
                state.contributions.map(row => [Number(row.id), row.status])
            );
            const statuses = ids.map(id => byId.get(id));
            if (statuses.every(status => status === after)) return "committed";
            if (statuses.every(status => status === before)) return "unchanged";
            return "unknown";
        };
    }

    function deleteTransition(id) {
        return state => state.plans.some(row => Number(row.id) === id)
            ? "unchanged"
            : "committed";
    }

    async function reconcileMutation(classify, { holdings = false } = {}) {
        let state;
        try {
            state = await readCanonicalDca();
        } catch (error) {
            log.warn("DCA reconciliation failed:", error);
            panelLoadFailed();
            notify(
                "DCA result is unknown — reconnect and refresh before retrying",
                "warning",
            );
            return "unknown";
        }
        let outcome = "unknown";
        try {
            outcome = classify?.(state) || "unknown";
        } catch (error) {
            log.warn("DCA reconciliation could not classify saved state:", error);
        }
        let refreshed = false;
        try {
            refreshed = holdings ? await afterHoldingsChange() : await loadPanel();
        } catch (error) {
            log.warn("DCA reconciliation UI refresh failed:", error);
        }
        const messages = {
            committed: refreshed
                ? ["DCA action completed — refreshed from saved state", "success"]
                : ["DCA action completed and saved locally — some views are stale. Refresh before using them.", "warning"],
            unchanged: [refreshed
                ? "DCA action did not complete — saved state is unchanged"
                : "DCA action did not complete — saved state is unchanged, but the view is stale. Refresh before retrying.", "warning"],
            unknown: [refreshed
                ? "DCA result is still unknown — review the refreshed state before retrying"
                : "DCA result is still unknown and the view is stale — reconnect and refresh before retrying",
                "warning",
            ],
        };
        notify(...messages[outcome]);
        return outcome;
    }

    function detailMessage(data, fallback) {
        const detail = data?.detail;
        if (typeof detail === "string") return detail;
        if (detail?.message) return detail.message;
        if (Array.isArray(detail)) {
            return detail.map(item => item?.msg || String(item)).join("; ") || fallback;
        }
        return fallback;
    }

    async function mutate(path, init, {
        successMessage = "",
        failureMessage = "DCA action failed",
        classify = null,
        holdings = false,
    } = {}) {
        if (mutationInFlight) return null;
        mutationInFlight = true;
        panelLoadId += 1; // Retire reads that predate this mutation.
        try {
            const response = await workspace.response(path, init);
            const data = await response.json().catch(() => null);
            if (!response.ok) {
                if (response.status >= 500) {
                    await reconcileMutation(classify, { holdings });
                } else {
                    notify(detailMessage(data, failureMessage), "danger");
                }
                return null;
            }
            if (data === null) {
                await reconcileMutation(classify, { holdings });
                return null;
            }
            if (successMessage) notify(successMessage, "success");
            return data;
        } catch (error) {
            log.warn("DCA mutation response was lost:", error);
            await reconcileMutation(classify, { holdings });
            return null;
        } finally {
            mutationInFlight = false;
        }
    }

    function post(path, options = {}) {
        const { payload, ...mutationOptions } = options;
        const init = { method: "POST" };
        if (payload !== undefined) {
            init.headers = { "Content-Type": "application/json" };
            init.body = JSON.stringify(payload);
        }
        return mutate(path, init, mutationOptions);
    }

    async function afterHoldingsChange() {
        const refreshed = await loadPanel();
        await holdingsChanged();
        return refreshed;
    }

    async function patchPlan(id, payload, successMessage, before) {
        const data = await mutate(
            `/api/dca/plans/${id}`,
            {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            },
            {
                successMessage,
                failureMessage: "Could not update plan",
                classify: planPatchTransition(id, before, payload),
            },
        );
        if (data) await loadPanel();
        return data;
    }

    async function handleAction(event) {
        const button = event.target?.closest?.("[data-dca-action]");
        if (!button) return null;
        const action = button.dataset.dcaAction;
        if (action === "toggle-panel") return togglePanel();
        if (action === "toggle-history") return toggleHistory();
        if (action === "retry-panel") return loadPanel();
        const id = Number(button.dataset.cid);
        const planId = Number(button.dataset.planId);
        const ticker = button.dataset.ticker || "";
        const count = Number(button.dataset.count);
        const contributionIds = (button.dataset.cids || "")
            .split(",")
            .map(value => Number(value))
            .filter(Number.isFinite);

        if (action === "apply") {
            const data = await post(`/api/dca/contributions/${id}/apply`, {
                classify: contributionTransition(id, "pending", "applied"),
                holdings: true,
            });
            if (data) {
                notify(data.message || "Buy applied", "success");
                await afterHoldingsChange();
            }
            return data;
        }
        if (action === "skip") {
            const data = await post(
                `/api/dca/contributions/${id}/skip`,
                {
                    successMessage: "Buy skipped — plan still active (pause it in Plans if needed)",
                    classify: contributionTransition(id, "pending", "dismissed"),
                },
            );
            if (data) loadPanel();
            return data;
        }
        if (action === "undo") {
            const data = await post(`/api/dca/contributions/${id}/undo`, {
                classify: contributionTransition(id, "applied", "pending"),
                holdings: true,
            });
            if (data) {
                notify(data.message || "Buy undone", "success");
                await afterHoldingsChange();
                loadHistory();
            }
            return data;
        }
        if (action === "restore") {
            const data = await post(
                `/api/dca/contributions/${id}/restore`,
                {
                    successMessage: "Buy restored to pending",
                    classify: contributionTransition(id, "dismissed", "pending"),
                },
            );
            if (data) {
                loadPanel();
                loadHistory();
            }
            return data;
        }
        if (action === "apply-all") {
            const choice = await openDialog({
                title: `Apply ${count} ${ticker} buys?`,
                copy: `${formatMoney(Number(button.dataset.total))} will be added to your holding using the recorded closes.`,
                warning: "Undo is available only while the linked holding still contains these shares and basis; later sales or edits can block reversal.",
                confirmLabel: "Apply all buys",
            });
            if (!choice?.confirmed) return null;
            const data = await post(`/api/dca/plans/${planId}/apply-pending`, {
                payload: { contribution_ids: contributionIds },
                classify: contributionSetTransition(
                    contributionIds, "pending", "applied"
                ),
                holdings: true,
            });
            if (data) {
                notify(`Applied ${data.applied} buys to ${data.ticker}`, "success");
                await afterHoldingsChange();
            }
            return data;
        }
        if (action === "skip-all") {
            const choice = await openDialog({
                title: `Skip ${count} pending ${ticker} buys?`,
                copy: "These buys won’t be applied and won’t reappear.",
                warning: "The plan stays active. Pause it separately to stop future buys.",
                confirmLabel: "Skip pending buys",
                danger: true,
            });
            if (!choice?.confirmed) return null;
            const data = await post(`/api/dca/plans/${planId}/skip-pending`, {
                payload: { contribution_ids: contributionIds },
                classify: contributionSetTransition(
                    contributionIds, "pending", "dismissed"
                ),
            });
            if (data) {
                notify(`Skipped ${data.skipped} buys for ${data.ticker}`, "success");
                loadPanel();
            }
            return data;
        }
        if (action === "undo-all") {
            const choice = await openDialog({
                title: `Undo ${count} applied ${ticker} buys?`,
                copy: "FolioOrb will reverse the recorded shares and basis only if the linked holding still contains them; otherwise this action stops without changing the holding or DCA ledger.",
                warning: "The buys return to the pending bucket and can be reviewed again.",
                confirmLabel: "Undo applied buys",
            });
            if (!choice?.confirmed) return null;
            const data = await post(`/api/dca/plans/${planId}/undo-applied`, {
                payload: { contribution_ids: contributionIds },
                classify: contributionSetTransition(
                    contributionIds, "applied", "pending"
                ),
                holdings: true,
            });
            if (data) {
                notify(`Reversed ${data.undone} buys for ${data.ticker}`, "success");
                await afterHoldingsChange();
                loadHistory();
            }
            return data;
        }
        if (action === "toggle-plan") {
            const wasActive = button.dataset.active === "true";
            return patchPlan(
                planId,
                { is_active: !wasActive },
                wasActive
                    ? "Plan paused — no new buys will book"
                    : "Plan resumed",
                { is_active: wasActive },
            );
        }
        if (action === "edit-plan") {
            const choice = await openDialog({
                title: "Change DCA amount",
                copy: "This amount applies to future intervals; recorded buys keep their original values.",
                confirmLabel: "Save amount",
                value: Number(button.dataset.amount),
            });
            if (choice?.confirmed) {
                return patchPlan(
                    planId,
                    { amount: choice.value },
                    "Plan amount updated",
                    { amount: Number(button.dataset.amount) },
                );
            }
            return null;
        }
        if (action === "delete-plan") {
            const choice = await openDialog({
                title: `Delete the ${ticker} DCA plan?`,
                copy: "Undo every applied buy before deleting this plan so its holding changes stay traceable.",
                warning: "After applied buys are undone, deleting removes pending and skipped buys. This cannot be undone.",
                confirmLabel: "Delete plan",
                danger: true,
            });
            if (!choice?.confirmed) return null;
            const data = await mutate(
                `/api/dca/plans/${planId}`,
                {
                    method: "DELETE",
                },
                {
                    successMessage: `${ticker} plan deleted`,
                    failureMessage: "Could not delete plan",
                    classify: deleteTransition(planId),
                },
            );
            if (data) await loadPanel();
            return data;
        }
        return null;
    }

    function hideBackfillConfirm() {
        const confirmation = byId("dca-backfill-confirm");
        if (confirmation) confirmation.hidden = true;
    }

    async function createPlan() {
        const ticker = byId("dca-ticker").value.trim().toUpperCase();
        const amount = Number.parseFloat(byId("dca-amount").value);
        const frequency = byId("dca-frequency").value;
        const startDate = byId("dca-start-date").value;
        if (!ticker || !Number.isFinite(amount) || amount <= 0 || !startDate) {
            notify("Fill in ticker, amount, and start date", "warning");
            return;
        }
        const today = new Date().toISOString().slice(0, 10);
        if (startDate < today) {
            let held = null;
            try {
                const owned = await workspace.json("/api/portfolio/holdings");
                held = (owned.holdings || []).find(row => row.ticker === ticker && row.shares > 0);
            } catch (_) { /* offline: backend still validates the plan */ }
            if (held) {
                const confirmation = byId("dca-backfill-confirm");
                const text = byId("dca-confirm-text");
                const heldShares = Number(held.shares.toFixed(4));
                text.textContent = `You already hold ${heldShares} ${ticker}. If that count already includes your past auto-invest buys, applying a backfill would double-count them. Track from today, or backfill anyway and review each buy before applying.`;
                confirmation.hidden = false;
                byId("dca-confirm-today").onclick = () => {
                    hideBackfillConfirm();
                    submitPlan({ ticker, amount, frequency, start_date: today });
                };
                byId("dca-confirm-backfill").onclick = () => {
                    hideBackfillConfirm();
                    submitPlan({ ticker, amount, frequency, start_date: startDate });
                };
                return;
            }
        }
        submitPlan({ ticker, amount, frequency, start_date: startDate });
    }

    async function submitPlan(payload) {
        const button = byId("dca-create-btn");
        button.disabled = true;
        try {
            const data = await mutate(
                "/api/dca/plans",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                },
                {
                    failureMessage: "Could not create plan",
                },
            );
            if (!data) return;
            byId("dca-create-form").reset();
            formDefaults();
            notify(
                data.buys_added > 0
                    ? `${payload.ticker} plan created — ${data.buys_added} buy${data.buys_added === 1 ? "" : "s"} ready to review`
                    : `${payload.ticker} plan created — first buy books on the next interval`,
                "success",
            );
            loadPanel();
        } finally {
            button.disabled = false;
        }
    }

    function init() {
        if (initialized) return;
        initialized = true;
        const form = byId("dca-create-form");
        if (form) {
            form.addEventListener("submit", event => {
                event.preventDefault();
                createPlan();
            });
        }
        document.addEventListener("click", handleAction);
        byId("dca-confirm-cancel")?.addEventListener("click", hideBackfillConfirm);
        formDefaults();
        initDialog();
        scheduleIdle(runCatchup);
    }

    // The browser exposes only open(); the factory returns its lifecycle/action
    // seams so runtime tests can exercise behavior without a browser framework.
    return { open, init, handleAction, submitPlan };
});
