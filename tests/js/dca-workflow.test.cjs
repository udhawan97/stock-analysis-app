const test = require("node:test");
const assert = require("node:assert/strict");

const { createDcaWorkflow } = require("../../static/js/dca-workflow.js");

function element(overrides = {}) {
    return {
        hidden: false,
        dataset: {},
        innerHTML: "",
        textContent: "",
        setAttribute() {},
        addEventListener() {},
        ...overrides,
    };
}

function fakeDocument() {
    const elements = new Map([
        ["dca-panel", element({ hidden: true })],
        ["dca-btn", element()],
        ["dca-load-status", element({ hidden: true })],
        ["dca-plans-section", element()],
        ["dca-plans-list", element()],
        ["dca-pending-section", element()],
        ["dca-pending-list", element()],
        ["dca-badge", element()],
        ["dca-history-list", element({ hidden: true })],
        ["dca-create-btn", element({ disabled: false })],
        ["dca-create-form", element({ reset() {} })],
        ["dca-start-date", element({ value: "", max: "" })],
        ["dca-frequency", element({ value: "weekly" })],
    ]);
    return {
        elements,
        activeElement: null,
        getElementById: id => elements.get(id) || null,
        addEventListener() {},
        querySelector() { return null; },
    };
}

function actionEvent(dataset) {
    return { target: { closest: () => ({ dataset }) } };
}

function emptyWorkspace(overrides = {}) {
    const workspace = {
        json: async url => url.includes("plans")
            ? { plans: [] }
            : { contributions: [] },
        response: async () => new Response("{}", { status: 200 }),
        ...overrides,
    };
    const read = workspace.json;
    workspace.json = async (...args) => {
        const payload = await read(...args);
        // Reconciliation fixtures specify transition fields; fill the remaining
        // API fields so the real renderer also runs instead of throwing silently.
        if (payload.plans) payload.plans = payload.plans.map(row => ({
            ticker: "TEST", amount: 50, frequency: "weekly", is_active: true,
            applied_count: 0, applied_amount: 0, applied_shares: 0,
            applied_avg_cost: null, ...row,
        }));
        if (payload.contributions) payload.contributions = payload.contributions.map(row => ({
            plan_id: 7, ticker: "TEST", shares: 0.5, price: 100, amount: 50,
            exec_date: "2026-09-01", ...row,
        }));
        return payload;
    };
    return workspace;
}

test("open is the navigation seam and loads the panel through the workspace", async () => {
    const document = fakeDocument();
    const requests = [];
    let managerOpens = 0;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            json: async url => {
                requests.push(url);
                return url.includes("plans") ? { plans: [] } : { contributions: [] };
            },
        }),
        document,
        openManager: () => { managerOpens += 1; },
    });

    assert.equal(workflow.open(), true);
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(managerOpens, 1);
    assert.equal(document.elements.get("dca-panel").hidden, false);
    assert.deepEqual(requests.slice(0, 2), [
        "/api/dca/plans",
        "/api/dca/contributions?status=all",
    ]);
});

test("plan undo control binds the exact applied contribution IDs", async () => {
    const document = fakeDocument();
    const plan = {
        id: 7,
        ticker: "VOO",
        amount: 50,
        frequency: "weekly",
        is_active: true,
        next_date: null,
        applied_count: 2,
        applied_amount: 100,
        applied_shares: 1,
        applied_avg_cost: 100,
        currency_status: "trusted",
    };
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            json: async url => url.includes("plans")
                ? { plans: [plan] }
                : { contributions: [
                    { id: 10, plan_id: 7, status: "applied" },
                    { id: 11, plan_id: 7, status: "applied" },
                ] },
        }),
        document,
    });

    workflow.open();
    await new Promise(resolve => setImmediate(resolve));

    const html = document.elements.get("dca-plans-list").innerHTML;
    assert.match(html, /data-dca-action="undo-all"/);
    assert.match(html, /data-cids="10,11"/);
    assert.match(html, /data-count="2"/);
});

test("one mutation in flight blocks a double apply", async () => {
    const document = fakeDocument();
    let resolveMutation;
    let mutationCalls = 0;
    const pending = new Promise(resolve => { resolveMutation = resolve; });
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => {
                mutationCalls += 1;
                return pending;
            },
        }),
        document,
    });
    const event = actionEvent({ dcaAction: "apply", cid: "3" });

    const first = workflow.handleAction(event);
    const second = await workflow.handleAction(event);
    assert.equal(second, null);
    assert.equal(mutationCalls, 1);

    resolveMutation(new Response(JSON.stringify({ message: "Applied" }), { status: 200 }));
    await first;
});

test("cancelling a bulk action sends no mutation", async () => {
    let mutations = 0;
    let prompt = null;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => {
                mutations += 1;
                return new Response("{}", { status: 200 });
            },
        }),
        document: fakeDocument(),
        confirmAction: async value => { prompt = value; return null; },
    });

    const result = await workflow.handleAction(actionEvent({
        dcaAction: "apply-all",
        planId: "7",
        count: "2",
        cids: "10,11",
        total: "50",
        ticker: "AAPL",
    }));

    assert.equal(result, null);
    assert.equal(mutations, 0);
    assert.match(prompt.warning, /later sales or edits can block reversal/);
});

for (const action of ["apply-all", "skip-all", "undo-all"]) {
    test(`${action} sends only the exact reviewed contribution IDs`, async () => {
        let request = null;
        const workflow = createDcaWorkflow({
            workspace: emptyWorkspace({
                response: async (url, init) => {
                    request = { url, init };
                    return new Response(JSON.stringify({
                        ticker: "VOO",
                        applied: 2,
                        skipped: 2,
                        undone: 2,
                    }), { status: 200 });
                },
            }),
            document: fakeDocument(),
            confirmAction: async () => ({ confirmed: true }),
        });

        await workflow.handleAction(actionEvent({
            dcaAction: action,
            planId: "7",
            count: "2",
            cids: "10,11",
            total: "100",
            ticker: "VOO",
        }));

        assert.deepEqual(JSON.parse(request.init.body), {
            contribution_ids: [10, 11],
        });
        assert.equal(request.init.headers["Content-Type"], "application/json");
    });
}

test("failed action reports the error without refreshing holdings", async () => {
    const messages = [];
    let refreshes = 0;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => new Response(
                JSON.stringify({ detail: "Already applied" }),
                { status: 400 },
            ),
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        holdingsChanged: async () => { refreshes += 1; },
    });

    await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));

    assert.equal(refreshes, 0);
    assert.deepEqual(messages[0], ["Already applied", "danger"]);
});

test("legacy plan is visibly blocked and has no financial action controls", async () => {
    const document = fakeDocument();
    const legacy = {
        id: 9,
        ticker: "LEGACY",
        amount: 50,
        frequency: "weekly",
        is_active: true,
        next_date: null,
        applied_count: 0,
        applied_amount: 0,
        applied_shares: 0,
        applied_avg_cost: null,
        currency_status: "needs_currency",
        currency_message: "Undo applied buys if needed, then delete this plan. Create a replacement only after FolioOrb verifies an explicit USD quote.",
    };
    const pending = {
        id: 17,
        plan_id: 9,
        ticker: "LEGACY",
        exec_date: "2026-06-12",
        shares: 0.5,
        price: 100,
        amount: 50,
        status: "pending",
    };
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            json: async url => url.includes("plans")
                ? { plans: [legacy] }
                : { contributions: [pending] },
        }),
        document,
    });

    workflow.open();
    await new Promise(resolve => setImmediate(resolve));

    const planHtml = document.elements.get("dca-plans-list").innerHTML;
    const pendingHtml = document.elements.get("dca-pending-list").innerHTML;
    assert.match(planHtml, /Needs currency verification/);
    assert.match(planHtml, /Undo applied buys if needed, then delete this plan/);
    assert.match(planHtml, /Create a replacement only after FolioOrb verifies/);
    assert.doesNotMatch(planHtml, /Next buy/);
    assert.doesNotMatch(planHtml, /data-dca-action="edit-plan"/);
    assert.match(planHtml, /data-dca-action="delete-plan"/);
    assert.doesNotMatch(pendingHtml, /data-dca-action="apply"/);
    assert.doesNotMatch(pendingHtml, /data-dca-action="apply-all"/);
    assert.match(pendingHtml, /Currency verification required/);
    assert.match(pendingHtml, /data-dca-action="skip"/);
});

test("catch-up HTTP failure is reported instead of silently swallowed", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => new Response(
                JSON.stringify({ detail: "Catch-up unavailable" }),
                { status: 409 },
            ),
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
    });

    workflow.init();
    await new Promise(resolve => setImmediate(resolve));

    assert.deepEqual(messages[0], ["Catch-up unavailable", "danger"]);
});

test("catch-up response loss refreshes state and reports an unknown outcome", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("offline"); },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
    });

    workflow.init();
    await new Promise(resolve => setImmediate(resolve));

    assert.deepEqual(messages[0], [
        "DCA result is still unknown — review the refreshed state before retrying",
        "warning",
    ]);
});

test("lost apply response reconciles a committed contribution and holdings", async () => {
    const messages = [];
    let holdingsRefreshes = 0;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => {
                if (url.includes("plans")) return { plans: [] };
                if (url.includes("status=all")) {
                    return { contributions: [{ id: 3, status: "applied" }] };
                }
                return { contributions: [] };
            },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        holdingsChanged: async () => { holdingsRefreshes += 1; },
    });

    const result = await workflow.handleAction(
        actionEvent({ dcaAction: "apply", cid: "3" })
    );

    assert.equal(result, null);
    assert.equal(holdingsRefreshes, 1);
    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

test("lost apply response reports a proven unchanged contribution", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => {
                if (url.includes("plans")) return { plans: [] };
                if (url.includes("status=all")) {
                    return { contributions: [{ id: 3, status: "pending" }] };
                }
                return { contributions: [] };
            },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
    });

    await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));

    assert.deepEqual(messages.at(-1), [
        "DCA action did not complete — saved state is unchanged",
        "warning",
    ]);
});

test("lost bulk-apply response reconciles the plan counters and holdings", async () => {
    const messages = [];
    let holdingsRefreshes = 0;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{ id: 7, pending_count: 0, applied_count: 2 }] }
                : { contributions: [
                    { id: 10, status: "applied" },
                    { id: 11, status: "applied" },
                ] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        holdingsChanged: async () => { holdingsRefreshes += 1; },
        confirmAction: async () => ({ confirmed: true }),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "apply-all",
        planId: "7",
        count: "2",
        cids: "10,11",
        total: "100",
        ticker: "VOO",
    }));

    assert.equal(holdingsRefreshes, 1);
    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

test("lost apply-all response does not confuse skipped buys with a commit", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{ id: 7, pending_count: 0, applied_count: 0 }] }
                : { contributions: [
                    { id: 10, status: "dismissed" },
                    { id: 11, status: "dismissed" },
                ] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        confirmAction: async () => ({ confirmed: true }),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "apply-all",
        planId: "7",
        count: "2",
        cids: "10,11",
        total: "100",
        ticker: "VOO",
    }));

    assert.deepEqual(messages.at(-1), [
        "DCA result is still unknown — review the refreshed state before retrying",
        "warning",
    ]);
});

test("lost skip-all response requires every targeted buy to be dismissed", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{ id: 7, pending_count: 0, applied_count: 0 }] }
                : { contributions: [
                    { id: 10, status: "dismissed" },
                    { id: 11, status: "dismissed" },
                ] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        confirmAction: async () => ({ confirmed: true }),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "skip-all",
        planId: "7",
        count: "2",
        cids: "10,11",
        ticker: "VOO",
    }));

    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

for (const [name, contributions] of [
    ["applied targets", [
        { id: 10, status: "applied" },
        { id: 11, status: "applied" },
    ]],
    ["mixed pending and dismissed targets", [
        { id: 10, status: "pending" },
        { id: 11, status: "dismissed" },
    ]],
]) {
    test(`lost skip-all response stays unknown for ${name}`, async () => {
        const messages = [];
        const workflow = createDcaWorkflow({
            workspace: emptyWorkspace({
                response: async () => { throw new Error("response lost"); },
                json: async url => url.includes("plans")
                    ? { plans: [{ id: 7, pending_count: 0, applied_count: 2 }] }
                    : { contributions },
            }),
            document: fakeDocument(),
            notify: (...args) => messages.push(args),
            confirmAction: async () => ({ confirmed: true }),
        });

        await workflow.handleAction(actionEvent({
            dcaAction: "skip-all",
            planId: "7",
            count: "2",
            cids: "10,11",
            ticker: "VOO",
        }));

        assert.deepEqual(messages.at(-1), [
            "DCA result is still unknown — review the refreshed state before retrying",
            "warning",
        ]);
    });
}

test("lost undo-all response requires every targeted buy to return to pending", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{ id: 7, pending_count: 2, applied_count: 0 }] }
                : { contributions: [
                    { id: 10, status: "pending" },
                    { id: 11, status: "pending" },
                ] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        confirmAction: async () => ({ confirmed: true }),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "undo-all",
        planId: "7",
        count: "2",
        cids: "10,11",
        ticker: "VOO",
    }));

    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

for (const [name, contributions] of [
    ["dismissed target", [
        { id: 10, status: "pending" },
        { id: 11, status: "dismissed" },
    ]],
    ["missing target", [{ id: 10, status: "pending" }]],
    ["mixed applied and pending targets", [
        { id: 10, status: "pending" },
        { id: 11, status: "applied" },
    ]],
]) {
    test(`lost undo-all response stays unknown for a ${name}`, async () => {
        const messages = [];
        const workflow = createDcaWorkflow({
            workspace: emptyWorkspace({
                response: async () => { throw new Error("response lost"); },
                json: async url => url.includes("plans")
                    ? { plans: [{ id: 7, pending_count: 1, applied_count: 1 }] }
                    : { contributions },
            }),
            document: fakeDocument(),
            notify: (...args) => messages.push(args),
            confirmAction: async () => ({ confirmed: true }),
        });

        await workflow.handleAction(actionEvent({
            dcaAction: "undo-all",
            planId: "7",
            count: "2",
            cids: "10,11",
            ticker: "VOO",
        }));

        assert.deepEqual(messages.at(-1), [
            "DCA result is still unknown — review the refreshed state before retrying",
            "warning",
        ]);
    });
}

test("lost create response remains unknown even when a matching plan exists", async () => {
    const messages = [];
    const document = fakeDocument();
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{
                    id: 7,
                    ticker: "VOO",
                    amount: 50,
                    frequency: "weekly",
                    start_date: "2026-06-12",
                    is_active: false,
                    applied_count: 0,
                }] }
                : { contributions: [] },
        }),
        document,
        notify: (...args) => messages.push(args),
    });

    await workflow.submitPlan({
        ticker: "VOO",
        amount: 50,
        frequency: "weekly",
        start_date: "2026-06-12",
    });

    assert.equal(document.elements.get("dca-create-btn").disabled, false);
    assert.deepEqual(messages.at(-1), [
        "DCA result is still unknown — review the refreshed state before retrying",
        "warning",
    ]);
});

test("bulk undo copy does not guarantee reversal after later holding changes", async () => {
    let prompt = null;
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace(),
        document: fakeDocument(),
        confirmAction: async value => { prompt = value; return null; },
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "undo-all",
        planId: "7",
        count: "2",
        ticker: "VOO",
    }));

    assert.match(prompt.copy, /only if the linked holding still contains them/);
    assert.match(prompt.copy, /stops without changing/);
});

test("lost patch response reconciles the saved plan state", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [{ id: 7, is_active: false }] }
                : { contributions: [] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "toggle-plan",
        planId: "7",
        active: "true",
    }));

    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

test("lost delete response reconciles an absent plan as committed", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async url => url.includes("plans")
                ? { plans: [] }
                : { contributions: [] },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
        confirmAction: async () => ({ confirmed: true }),
    });

    await workflow.handleAction(actionEvent({
        dcaAction: "delete-plan",
        planId: "7",
        ticker: "VOO",
    }));

    assert.deepEqual(messages.at(-1), [
        "DCA action completed — refreshed from saved state",
        "success",
    ]);
});

test("failed reconciliation keeps a lost mutation outcome unknown", async () => {
    const messages = [];
    const workflow = createDcaWorkflow({
        workspace: emptyWorkspace({
            response: async () => { throw new Error("response lost"); },
            json: async () => { throw new Error("still offline"); },
        }),
        document: fakeDocument(),
        notify: (...args) => messages.push(args),
    });

    await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));

    assert.deepEqual(messages.at(-1), [
        "DCA result is unknown — reconnect and refresh before retrying",
        "warning",
    ]);
});


test("committed reconciliation with failed view refresh reports saved but stale and retry recovers", async () => {
    const document = fakeDocument();
    const messages = [];
    const pending = document.elements.get("dca-pending-list");
    let initial = true;
    let reads = 0;
    let retry = false;
    let mutations = 0;
    const workflow = createDcaWorkflow({
        document,
        notify: (...args) => messages.push(args),
        workspace: emptyWorkspace({
            response: async () => { mutations += 1; throw new Error("lost response"); },
            json: async url => {
                if (initial) return url.includes("plans") ? { plans: [] }
                    : { contributions: [{ id: 3, status: "pending" }] };
                reads += 1;
                if (reads > 2 && !retry) throw new Error("offline");
                return url.includes("plans") ? { plans: [] }
                    : { contributions: [{ id: 3, status: "applied" }] };
            },
        }),
    });
    workflow.open(); await new Promise(resolve => setImmediate(resolve));
    const previousCard = pending.innerHTML;
    assert.match(previousCard, /data-cid="3"/);
    initial = false;
    await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));
    assert.equal(pending.innerHTML, previousCard);
    assert.match(messages.at(-1)[0], /completed.*view.*stale/i);
    assert.equal(messages.at(-1)[1], "warning");
    const status = document.elements.get("dca-load-status");
    assert.equal(status.hidden, false);
    assert.match(status.innerHTML, /retry-panel/);
    assert.match(status.innerHTML, /view is stale/);
    retry = true;
    await workflow.handleAction(actionEvent({ dcaAction: "retry-panel" }));
    assert.equal(pending.innerHTML, "");
    assert.equal(status.hidden, true);
    assert.equal(mutations, 1);
});

test("initial DCA load failure has a visible error and read-only retry", async () => {
    const document = fakeDocument();
    let offline = true;
    let mutations = 0;
    const workflow = createDcaWorkflow({ document, workspace: emptyWorkspace({
        json: async url => {
            if (offline) throw new Error("offline");
            return url.includes("plans") ? { plans: [] } : { contributions: [] };
        },
        response: async () => { mutations += 1; return new Response("{}"); },
    }) });
    workflow.open();
    await new Promise(resolve => setImmediate(resolve));
    const status = document.elements.get("dca-load-status");
    assert.equal(status.hidden, false);
    assert.match(status.innerHTML, /could not load/i);
    assert.match(status.innerHTML, /retry-panel/);
    offline = false;
    await workflow.handleAction(actionEvent({ dcaAction: "retry-panel" }));
    assert.equal(status.hidden, true);
    assert.equal(mutations, 0);
});


for (const [status, outcome] of [["pending", "unchanged"], ["dismissed", "unknown"]]) {
    test("failed display refresh preserves " + outcome + " mutation guidance", async () => {
        let reads = 0;
        let mutations = 0;
        const messages = [];
        const workflow = createDcaWorkflow({
            document: fakeDocument(), notify: (...args) => messages.push(args),
            workspace: emptyWorkspace({
                response: async () => { mutations += 1; throw new Error("lost response"); },
                json: async url => {
                    if (++reads > 2) throw new Error("offline");
                    return url.includes("plans") ? { plans: [] }
                        : { contributions: [{ id: 3, status }] };
                },
            }),
        });
        await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));
        assert.match(messages.at(-1)[0], new RegExp(outcome));
        assert.match(messages.at(-1)[0], /view is stale/);
        assert.match(messages.at(-1)[0], /refresh before retrying/i);
        assert.equal(messages.at(-1)[1], "warning");
        assert.equal(mutations, 1);
    });
}

test("successful mutation and failed panel GET retain an explicit stale view", async () => {
    const document = fakeDocument();
    const messages = [];
    let offline = false;
    let mutations = 0;
    const workflow = createDcaWorkflow({
        document, notify: (...args) => messages.push(args),
        workspace: emptyWorkspace({
            json: async url => {
                if (offline) throw new Error("offline");
                return url.includes("plans") ? { plans: [] }
                    : { contributions: [{ id: 3, status: "pending" }] };
            },
            response: async () => { mutations += 1; return new Response('{"message":"Buy applied"}'); },
        }),
    });
    workflow.open(); await new Promise(resolve => setImmediate(resolve));
    offline = true;
    await workflow.handleAction(actionEvent({ dcaAction: "apply", cid: "3" }));
    assert.equal(messages.at(-1)[0], "Buy applied");
    assert.match(document.elements.get("dca-load-status").innerHTML, /view is stale/);
    assert.equal(mutations, 1);
});

test("visible DCA history renders from the same successful panel read", async () => {
    const document = fakeDocument();
    document.elements.get("dca-history-list").hidden = false;
    let ledgerReads = 0;
    const workflow = createDcaWorkflow({ document, workspace: emptyWorkspace({
        json: async url => {
            if (url.includes("plans")) return { plans: [] };
            if (url.includes("status=all") && ++ledgerReads > 1) throw new Error("second read failed");
            return { contributions: [{ id: 3, status: "applied" }] };
        },
    }) });
    workflow.open(); await new Promise(resolve => setImmediate(resolve));
    assert.match(document.elements.get("dca-history-list").innerHTML, /data-cid="3"/);
    assert.equal(document.elements.get("dca-load-status").hidden, true);
    assert.equal(ledgerReads, 1);
});
