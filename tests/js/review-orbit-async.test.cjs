const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const Logic = require("../../static/js/review-orbit-logic.js");
const interactions = require("../../static/js/interaction-state.js");
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function plan(value) {
    return { items: [{ holding_id: 1, ticker: "TEST", target_weight_bps: value,
        actual_weight_bps: 10000, drift_bps: null }], target_total_bps: value,
        remaining_bps: 10000 - value, complete: false, drift_available: false };
}
function harness({ backups = null } = {}) {
    let inputs = [];
    const elements = new Map();
    const listeners = {};
    const makeElement = id => {
        let markup = "";
        const element = { id, value: "", textContent: "", hidden: false, disabled: false,
            dataset: {}, events: {}, classList: { add() {}, remove() {} },
            addEventListener(name, callback) { this.events[name] = callback; },
            setAttribute() {}, removeAttribute() {}, focus() {}, replaceChildren() {},
            querySelectorAll() { return []; },
            insertAdjacentHTML(_, value) { markup = value + markup; },
            get innerHTML() { return markup; },
            set innerHTML(value) {
                markup = value;
                if (id === "review-target-table") {
                    inputs = [...value.matchAll(/data-holding-id="(\d+)" value="([^"]*)"/g)]
                        .map(([, holdingId, target]) => ({ dataset: { holdingId }, value: target,
                            matches: selector => selector === ".review-target-input" }));
                }
            },
        };
        return element;
    };
    const get = id => {
        if (!elements.has(id)) elements.set(id, makeElement(id));
        return elements.get(id);
    };
    const document = {
        getElementById: get,
        querySelector: selector => selector.startsWith("[data-review-") ? get(selector) : null,
        querySelectorAll: selector => {
            if (selector === ".review-target-input") return inputs;
            if (selector === "[data-review-snapshot-export]") {
                get("review-plan-export").dataset.reviewSnapshotExport = "plan";
                return [get("review-plan-export")];
            }
            return [];
        },
        addEventListener: (name, callback) => { listeners[name] = callback; },
    };
    const reads = [], puts = [], exports = [], messages = [];
    const workspace = { json: (url, init) => {
        if (init?.method === "PUT") {
            const request = deferred(); puts.push({ ...request, body: JSON.parse(init.body) });
            return request.promise;
        }
        if (url === "/api/review/plan") {
            const request = deferred(); reads.push(request); return request.promise;
        }
        if (url === "/api/review/backups") return Promise.resolve(backups);
        if (url === "/api/review/overview") return Promise.resolve({ items: [], known_value_usd: 0 });
        return Promise.reject(new Error("Unused review surface"));
    }, response: async url => { exports.push(url); throw new Error("export reached"); } };
    const context = {
        window: { ReviewOrbitLogic: Logic, FolioInteractionState: interactions }, document,
        localStorage: { getItem() { return null; }, setItem() {} },
        PortfolioWorkspace: workspace, CSS: { escape: value => value },
        html: (parts, ...values) => parts.reduce((result, part, i) => result + part +
            (Array.isArray(values[i]) ? values[i].join("") : values[i] ?? ""), ""),
        apiErrorMessage: (error, fallback) => error?.message || fallback,
        Intl, requestAnimationFrame: callback => callback(),
        showToast: message => messages.push(message),
    };
    vm.runInNewContext(fs.readFileSync(require.resolve("../../static/js/review-orbit.js"), "utf8"), context);
    listeners.DOMContentLoaded();
    return {
        get, reads, puts, exports, messages, orbit: context.window.ReviewOrbit,
        value: () => inputs[0]?.value,
        edit: value => { inputs[0].value = value; get("review-target-form").events.input({ target: inputs[0] }); },
        save: () => get("review-target-form").events.submit({ preventDefault() {} }),
        start: async () => {
            context.window.ReviewOrbit.activateTab("plan");
            reads[0].resolve(plan(1000)); await flush();
        },
    };
}

test("delayed save and readback preserve edits typed during both requests", async () => {
    const h = harness(); await h.start();
    h.edit("2000"); const saving = h.save();
    assert.equal(h.puts[0].body.items[0].target_weight_bps, 2000);
    h.edit("3000"); h.puts[0].resolve({}); await flush();
    h.edit("4000"); h.reads[1].resolve(plan(2000)); await saving;
    assert.equal(h.value(), "4000");
    assert.equal(h.get("review-plan-export").disabled, true);
    assert.match(h.get("review-target-status").textContent, /unsaved/i);
    assert.match(h.get("review-target-status").textContent, /saved/i);
});

test("ordinary refresh preserves edits started or changed after its GET began", async () => {
    const h = harness(); await h.start();
    const refreshing = h.orbit.refresh();
    h.edit("3000"); h.reads[1].resolve(plan(1000)); await refreshing;
    assert.equal(h.value(), "3000");
    const again = h.orbit.refresh();
    h.edit("4000"); h.reads[2].resolve(plan(1000)); await again;
    assert.equal(h.value(), "4000");
    assert.equal(h.get("review-plan-export").disabled, true);
});

test("repeated submit is serialized and pre-save GET cannot replace newer saved state", async () => {
    const h = harness(); await h.start();
    const oldRefresh = h.orbit.refresh();
    h.edit("2000"); const saving = h.save();
    h.edit("3000"); h.save();
    assert.equal(h.puts.length, 1);
    h.puts[0].resolve({}); await flush();
    h.reads[2].resolve(plan(2000)); await saving;
    h.reads[1].resolve(plan(1000)); await oldRefresh;
    assert.equal(h.value(), "3000");
    h.edit("2000");
    assert.equal(h.get("review-plan-export").disabled, false);
    h.edit("4000"); const next = h.save();
    assert.equal(h.puts.length, 2);
    h.puts[1].resolve({}); await flush();
    h.reads[3].resolve(plan(4000)); await next;
    assert.equal(h.value(), "4000");
    assert.equal(h.get("review-plan-export").disabled, false);
});

test("failed readback retains newer draft and blocks export even when it equals the old saved value", async () => {
    const h = harness(); await h.start();
    h.edit("2000"); const saving = h.save();
    h.puts[0].resolve({}); await flush();
    h.edit("1000"); h.reads[1].reject(new Error("offline")); await saving;
    assert.equal(h.value(), "1000");
    assert.equal(h.get("review-plan-export").disabled, true);
    assert.match(h.get("review-target-status").textContent, /saved.*could not be read back/i);
    const retry = h.orbit.refresh(); h.reads[2].resolve(plan(2000)); await retry;
    assert.equal(h.value(), "1000");
    assert.equal(h.get("review-plan-export").disabled, true);
    h.edit("2000"); assert.equal(h.get("review-plan-export").disabled, false);
});


test("Plan and bundle export handlers refuse in-flight saves and unreadback saves", async () => {
    const h = harness(); await h.start();
    const checkExports = async () => {
        await h.get("review-plan-export").events.click();
        await h.get("review-bundle-export").events.click();
        assert.equal(h.exports.length, 0);
        assert.match(h.messages.at(-1), /refresh the saved Plan/);
    };
    // Saving an unchanged value still makes export wait for its result.
    const saving = h.save(); await checkExports();
    h.puts[0].resolve({}); await flush();
    h.reads[1].reject(new Error("offline")); await saving;
    await checkExports();
    const retry = h.orbit.refresh(); h.reads[2].resolve(plan(1000)); await retry;
    assert.equal(h.get("review-plan-export").disabled, false);
    assert.match(h.get("review-target-status").textContent, /read back/);
    assert.doesNotMatch(h.get("review-target-status").textContent, /could not/);
    await h.get("review-plan-export").events.click();
    assert.equal(h.exports.length, 1);
});

test("refresh during PUT leaves editable draft intact and readback retains only newer edits", async () => {
    const h = harness(); await h.start();
    h.edit("2000"); const saving = h.save();
    const refreshing = h.orbit.refresh(); await flush();
    assert.equal(h.reads.length, 1);
    await refreshing;
    assert.equal(h.value(), "2000");
    h.puts[0].resolve({}); await flush(); h.reads[1].resolve(plan(2000)); await saving;
    assert.equal(h.value(), "2000");
    assert.equal(h.get("review-plan-export").disabled, false);
    assert.doesNotMatch(h.get("review-target-status").textContent, /unsaved/i);
});


for (const [receipt, expected, absent] of [
    [{ status: "restored", installer_status: "failed", environment_status: "failed" },
        ["database was restored", "previous installer could not be launched", "Saved settings could not be restored"],
        ["failed before replacing"]],
    [{ status: "restored" }, ["Restored fixture successfully"],
        ["could not be launched", "settings could not"]],
    [{ status: "failed" }, ["failed before replacing the live database"],
        ["Restored fixture successfully"]],
]) {
    test("backup receipt distinguishes database, installer and settings outcomes: " + JSON.stringify(receipt), async () => {
        const h = harness({ backups: { items: [], last_restore: { name: "fixture", ...receipt } } });
        h.orbit.activateTab("backups"); await flush();
        const status = h.get("review-restore-status");
        assert.equal(status.hidden, false);
        for (const text of expected) assert.ok(status.textContent.includes(text), status.textContent);
        for (const text of absent) assert.ok(!status.textContent.includes(text), status.textContent);
    });
}


test("a failed save preserves newer input and permits an explicit retry", async () => {
    const h = harness(); await h.start();
    h.edit("2000"); const saving = h.save(); h.edit("3000");
    h.puts[0].reject(new Error("save unavailable")); await saving;
    assert.equal(h.value(), "3000");
    assert.equal(h.get("review-target-save").disabled, false);
    assert.equal(h.get("review-plan-export").disabled, true);
    assert.match(h.get("review-target-status").textContent, /save unavailable/);
    const retry = h.save(); h.puts[1].resolve({}); await flush();
    h.reads[1].resolve(plan(3000)); await retry;
    assert.equal(h.value(), "3000");
    assert.equal(h.get("review-plan-export").disabled, false);
});
