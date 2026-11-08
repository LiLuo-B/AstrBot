import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const operation = source.slice(
  source.indexOf("  async function performVectorRebuild("),
  source.indexOf("  async function confirmAndRebuildVector("),
);

for (const incremental of [true, false]) {
  test(`${
    incremental ? "incremental update" : "full rebuild"
  } submits the intended force flag and restores controls`, async () => {
    const requests = [];
    const restored = [];
    const increment = {};
    const rebuild = {};
    const status = {
      embedding_provider_ready: true,
      semantic_caption_complete: true,
      task_status: "idle",
    };
    const context = vm.createContext({
      vectorOperations: new Set(),
      managePageActive: true,
      activeManagePackId: "pack",
      packSupportsVectorStatus: () => true,
      latestManagePackVectorStatusId: "pack",
      latestManagePackVectorStatus: status,
      incrementPackVectorsBtn: increment,
      rebuildPackVectorsBtn: rebuild,
      showToast() {},
      setButtonBusy(button) {
        button.disabled = true;
      },
      restoreButton(button) {
        restored.push(button);
      },
      async apiPost(route, body) {
        requests.push({ route, body });
        return {};
      },
      async refreshManagePackSummaries() {},
      async refreshManagePackVectorStatus() {
        return status;
      },
    });
    vm.runInContext(operation, context);
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      true,
    );
    assert.equal(requests[0].route, "semantic/rebuild-index");
    assert.equal(requests[0].body.force, !incremental);
    assert.equal(restored[0], incremental ? increment : rebuild);
    status.semantic_caption_complete = false;
    requests.length = 0;
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      incremental,
    );
    assert.equal(requests.length, incremental ? 1 : 0);
    status.embedding_provider_ready = false;
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      false,
    );
  });
}

const refresh = source.slice(
  source.indexOf("  async function refreshManagePackVectorStatus("),
  source.indexOf("  async function performVectorRebuild("),
);

function statusContext(apiGet) {
  const rendered = [];
  const context = vm.createContext({
    vectorOperations: new Set(),
    managePageActive: true,
    activeManagePackId: "pack-a",
    managePackVectorStatusRequestId: 0,
    latestManagePackVectorStatusId: "",
    latestManagePackVectorStatus: null,
    packSupportsVectorStatus: () => true,
    hideManagePackVectorStatus() {},
    renderManagePackVectorStatus(packId, status, state) {
      rendered.push({ packId, status, state: state || "ready" });
    },
    apiGet,
    console: { warn() {} },
  });
  vm.runInContext(refresh, context);
  return { context, rendered };
}

const appearance = source.slice(
  source.indexOf("  function updateManagePackSemanticAppearance("),
  source.indexOf("  async function refreshManagePackSummaries("),
);
const summaries = source.slice(
  source.indexOf("  async function refreshManagePackSummaries("),
  source.indexOf("  function syncManagedPackQuery("),
);
const pageLifecycle = source.slice(
  source.indexOf('  window.addEventListener("pagehide",'),
  source.indexOf("  semanticReviewStats?.addEventListener("),
);

function summaryContext(apiGet, activePackId = "pack-a") {
  const vectorRequests = [];
  const exportRequests = [];
  const statusBadge = {
    hidden: false,
    classList: { toggle() {} },
    title: "",
  };
  const statusText = { textContent: "", title: "" };
  const content = { classList: { toggle() {} } };
  const context = vm.createContext({
    managePageActive: true,
    managePackSummaryRequestId: 0,
    managePackVectorStatusRequestId: 0,
    exportCapabilityRequestId: 0,
    activeManagePackId: activePackId,
    managePacksById: new Map(),
    managePackSelect: { options: [] },
    packSemanticStatus: statusBadge,
    packSemanticStatusText: statusText,
    document: {
      getElementById(id) {
        return id === "content" ? content : null;
      },
    },
    applySemanticReviewData() {},
    formatPackOptionLabel: (pack) => `${pack.name} (${pack.id})`,
    apiGet,
    refreshPackExportCapability: async (packId) => exportRequests.push(packId),
    refreshManagePackVectorStatus: async (packId) => vectorRequests.push(packId),
    console: { warn() {} },
  });
  vm.runInContext(`${appearance}\n${summaries}`, context);
  return { context, statusText, vectorRequests, exportRequests };
}

function packPayload(status, done, total, imageCount = total) {
  return [
    {
      id: "pack-a",
      name: "Pack A",
      image_count: imageCount,
      semantic_status: status,
      semantic_caption_done: done,
      semantic_caption_total: total,
    },
  ];
}

test("periodic poll updates the pack badge when a background job finishes", async () => {
  let tick;
  let payload = packPayload("partial", 1, 2);
  const { context, statusText, vectorRequests } = summaryContext(
    async () => ({ packs: payload }),
  );
  Object.assign(context, {
    vectorStatusPollInFlight: false,
    vectorOperations: new Set(),
    collectionReview: { dialog: { open: false }, busy: false },
    loadCollectionReview() {},
    window: {
      addEventListener() {},
      setInterval(callback, interval) {
        assert.equal(interval, 30000);
        tick = callback;
      },
    },
  });
  context.document.hidden = false;
  vm.runInContext(pageLifecycle, context);
  await tick();
  assert.equal(context.managePacksById.get("pack-a").semantic_status, "partial");
  assert.match(statusText.textContent, /语义化进度：1\/2/);

  payload = packPayload("complete", 2, 2);
  await tick();
  assert.equal(
    context.managePacksById.get("pack-a").semantic_status,
    "complete",
  );
  assert.equal(statusText.textContent, "2 张图片已完成语义化");
  assert.deepEqual(vectorRequests, ["pack-a", "pack-a"]);
});

test("an older pack summary response cannot overwrite a newer map", async () => {
  const resolvers = [];
  const { context, statusText, vectorRequests } = summaryContext(
    () => new Promise((resolve) => resolvers.push(resolve)),
  );
  const first = context.refreshManagePackSummaries();
  const second = context.refreshManagePackSummaries();
  resolvers[1]({ packs: packPayload("complete", 2, 2) });
  await second;
  resolvers[0]({ packs: packPayload("partial", 1, 2) });
  await first;

  assert.equal(
    context.managePacksById.get("pack-a").semantic_status,
    "complete",
  );
  assert.equal(statusText.textContent, "2 张图片已完成语义化");
  assert.deepEqual(vectorRequests, ["pack-a"]);
});

test("a late summary response renders only the currently selected pack", async () => {
  let resolve;
  const { context, statusText, vectorRequests, exportRequests } = summaryContext(
    () => new Promise((done) => (resolve = done)),
  );
  const pending = context.refreshManagePackSummaries();
  context.activeManagePackId = "pack-b";
  resolve({
    packs: [
      ...packPayload("partial", 1, 2),
      {
        id: "pack-b",
        name: "Pack B",
        image_count: 5,
        semantic_status: "complete",
        semantic_caption_done: 5,
        semantic_caption_total: 5,
      },
    ],
  });
  await pending;

  assert.equal(statusText.textContent, "5 张图片已完成语义化");
  assert.deepEqual(vectorRequests, ["pack-b"]);
  assert.deepEqual(exportRequests, ["pack-b"]);
});

test("pagehide invalidates an in-flight pack summary", async () => {
  let resolve;
  const listeners = new Map();
  const { context, statusText, vectorRequests } = summaryContext(
    () => new Promise((done) => (resolve = done)),
  );
  context.window = {
    addEventListener(name, callback) {
      listeners.set(name, callback);
    },
    setInterval() {},
  };
  vm.runInContext(pageLifecycle, context);
  const pending = context.refreshManagePackSummaries();
  listeners.get("pagehide")();
  listeners.get("pageshow")();
  resolve({ packs: packPayload("complete", 2, 2) });
  await pending;

  assert.equal(context.managePacksById.size, 0);
  assert.equal(statusText.textContent, "");
  assert.deepEqual(vectorRequests, []);
});

test("pack summary request failure leaves the old map and recovers", async () => {
  let fail = true;
  const { context, statusText, vectorRequests } = summaryContext(async () => {
    if (fail) throw new Error("offline");
    return { packs: packPayload("complete", 2, 2) };
  });
  assert.equal(
    Array.from(await context.refreshManagePackSummaries()).length,
    0,
  );
  assert.equal(context.managePacksById.size, 0);
  assert.deepEqual(vectorRequests, []);

  fail = false;
  await context.refreshManagePackSummaries();
  assert.equal(
    context.managePacksById.get("pack-a").semantic_status,
    "complete",
  );
  assert.equal(statusText.textContent, "2 张图片已完成语义化");
  assert.deepEqual(vectorRequests, ["pack-a"]);
});

test("a late response returns its captured status without replacing the selected pack", async () => {
  let resolveFirst;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  const { context, rendered } = statusContext(async (_route, body) =>
    body.pack_id === "pack-a" ? first : { pack_id: body.pack_id },
  );
  const request = context.refreshManagePackVectorStatus("pack-a");
  context.activeManagePackId = "pack-b";
  await context.refreshManagePackVectorStatus("pack-b");
  resolveFirst({ pack_id: "pack-a" });
  assert.equal((await request).pack_id, "pack-a");
  assert.equal(context.latestManagePackVectorStatusId, "pack-b");
  assert.equal(rendered.at(-1).packId, "pack-b");
});

test("reading a non-selected pack cannot invalidate the selected pack request", async () => {
  let resolveFirst;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  const { context, rendered } = statusContext(async (_route, body) =>
    body.pack_id === "pack-a" ? first : { pack_id: body.pack_id },
  );
  const request = context.refreshManagePackVectorStatus("pack-a");
  assert.equal(
    (await context.refreshManagePackVectorStatus("imported-pack")).pack_id,
    "imported-pack",
  );
  resolveFirst({ pack_id: "pack-a" });
  await request;
  assert.equal(context.latestManagePackVectorStatusId, "pack-a");
  assert.ok(rendered.every(({ packId }) => packId === "pack-a"));
});

for (const failed of [false, true]) {
  test(`a ${
    failed ? "failed" : "completed"
  } update cannot restore the previous pack toolbar after switching`, async () => {
    let complete;
    const pending = new Promise((resolve) => {
      complete = resolve;
    });
    const { context, rendered } = statusContext(async (_route, body) => ({
      pack_id: body.pack_id,
    }));
    const status = {
      embedding_provider_ready: true,
      semantic_caption_complete: true,
      task_status: "idle",
    };
    const restored = [];
    Object.assign(context, {
      incrementPackVectorsBtn: {},
      rebuildPackVectorsBtn: {},
      setButtonBusy() {},
      restoreButton(button) {
        restored.push(button);
      },
      showToast() {},
      async apiPost() {
        await pending;
        if (failed) throw new Error("Embedding failed");
        return {};
      },
      async refreshManagePackSummaries() {},
    });
    vm.runInContext(operation, context);
    const running = context.performVectorRebuild("pack-a", status, true);
    assert.equal(
      await context.performVectorRebuild("pack-a", status, true),
      false,
    );
    context.activeManagePackId = "pack-b";
    await context.refreshManagePackVectorStatus("pack-b");
    complete();
    assert.equal(await running, !failed);
    assert.equal(context.latestManagePackVectorStatusId, "pack-b");
    assert.equal(rendered.at(-1).packId, "pack-b");
    assert.equal(restored.length, 0);
  });
}

test("periodic refresh follows pack summaries on the visible page and stops on page leave", async () => {
  let tick;
  const listeners = new Map();
  const summaryRefreshes = [];
  let failNext = false;
  let inboxRequests = 0;
  const context = vm.createContext({
    managePageActive: true,
    vectorStatusPollInFlight: false,
    vectorOperations: new Set(),
    console: { warn() {} },
    managePackVectorStatusRequestId: 0,
    managePackSummaryRequestId: 0,
    exportCapabilityRequestId: 0,
    activeManagePackId: "pack-a",
    document: { hidden: false },
    collectionReview: { dialog: { open: false }, busy: false },
    window: {
      addEventListener(name, callback) {
        listeners.set(name, callback);
      },
      setInterval(callback, interval) {
        assert.equal(interval, 30000);
        tick = callback;
      },
    },
    async loadCollectionReview() {
      inboxRequests++;
    },
    async refreshManagePackSummaries() {
      summaryRefreshes.push(true);
      if (failNext) {
        failNext = false;
        throw new Error("Temporary summary failure");
      }
    },
  });
  vm.runInContext(pageLifecycle, context);
  await tick();
  context.activeManagePackId = "pack-b";
  await tick();
  assert.equal(summaryRefreshes.length, 2);
  failNext = true;
  await tick();
  assert.equal(context.vectorStatusPollInFlight, false);
  await tick();
  context.vectorOperations.add("pack-b");
  await tick();
  assert.equal(summaryRefreshes.length, 4);
  context.vectorOperations.delete("pack-b");
  context.document.hidden = true;
  await tick();
  assert.equal(summaryRefreshes.length, 4);
  context.document.hidden = false;
  listeners.get("pagehide")();
  await tick();
  assert.equal(summaryRefreshes.length, 4);
  assert.equal(inboxRequests, 5);
  listeners.get("pageshow")();
  await tick();
  assert.equal(summaryRefreshes.length, 5);
});

test("a status response received after page leave cannot update the toolbar", async () => {
  let finish;
  const response = new Promise((resolve) => {
    finish = resolve;
  });
  const { context, rendered } = statusContext(async () => response);
  const pending = context.refreshManagePackVectorStatus("pack-a");
  context.managePageActive = false;
  context.managePackVectorStatusRequestId++;
  finish({ pack_id: "pack-a" });
  assert.equal((await pending).pack_id, "pack-a");
  assert.equal(context.latestManagePackVectorStatusId, "");
  assert.equal(rendered.filter(({ state }) => state === "ready").length, 0);
});
