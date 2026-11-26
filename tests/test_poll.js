/* DrillPoller 确定性测试：注入虚拟时钟，无真实定时器。
 * 运行：node tests/test_poll.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "static", "poll.js"), "utf8");
new Function(src).call(globalThis);
const DrillPoller = globalThis.DrillPoller;

function makeClock() {
  let now = 1000;
  const timers = new Map();
  let id = 0;
  return {
    now: () => now,
    setTimeout(fn, ms) { const my = ++id; timers.set(my, { fn, at: now + ms }); return my; },
    clearTimeout(t) { timers.delete(t); },
    async advance(ms) {
      const deadline = now + ms;
      while (true) {
        const due = [...timers.entries()].filter(([, v]) => v.at <= deadline)
          .sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [tid, job] = due[0];
        timers.delete(tid);
        now = job.at;
        job.fn();
        await Promise.resolve(); // 排空回包微任务
      }
      now = deadline;
    },
  };
}

function makeHarness(responses, cfg) {
  const clock = makeClock();
  const calls = [];
  let stateCount = 0, notModCount = 0, errCount = 0;
  const idleCounts = [];
  const modes = [];
  const poller = new DrillPoller(Object.assign({
    activeIntervalMs: 2000,
    idleIntervalMs: 10000,
    idleWindowMs: 60000,
    idleWindowMaxRequests: 6,
    clock,
    request(ifNoneMatch, cb) {
      calls.push({ t: clock.now(), ifNoneMatch });
      const r = responses.shift();
      Promise.resolve().then(() => cb(r));
    },
    onState: () => stateCount++,
    onNotModified: () => notModCount++,
    onError: () => errCount++,
    onIdleCount: (n) => idleCounts.push(n),
    onModeChange: (m) => modes.push(m),
  }, cfg || {}));
  return { clock, calls, poller, modes,
    counts: () => ({ stateCount, notModCount, errCount, idleCounts }) };
}

let passed = 0;
const queue = [];
function test(name, fn) { queue.push({ name, fn }); }
async function run() {
  for (const t of queue) { await t.fn(); passed++; console.log("  ✓ " + t.name); }
  console.log(`\n全部 ${passed} 项 DrillPoller 测试通过`);
}

// 1. 活跃期每 2 秒一次，30 秒持续变化 → 不进入空闲
test("活跃期 2s 轮询且持续变化不进入空闲", async () => {
  const responses = [];
  for (let i = 0; i < 40; i++) responses.push({ data: { etag: `"g1-r${i}"`, revision: i } });
  const h = makeHarness(responses);
  h.poller.start();
  await h.clock.advance(30000);
  const ts = h.calls.map((c) => c.t);
  assert.ok(ts.length >= 15, `请求数应≥15，实际 ${ts.length}`);
  for (let i = 1; i < ts.length; i++) assert.strictEqual(ts[i] - ts[i - 1], 2000);
  assert.strictEqual(h.poller.isIdle(), false);
  assert.ok(h.calls.every((c) => c.ifNoneMatch === null), "活跃期不带条件头");
  assert.ok(!h.modes.includes("idle"));
});

// 2. 连续无变化满 60s 才进空闲；空闲后每个 60s 窗最多 6 次、节奏 10s
test("满60s无变化进空闲，且每个60s窗≤6次、节奏10s", async () => {
  const same = { data: { etag: '"g1-r1"', revision: 1 } };
  const h = makeHarness(Array.from({ length: 60 }, () => ({ ...same })));
  h.poller.start();
  await h.clock.advance(60000);
  assert.strictEqual(h.poller.isIdle(), true, "满60s应进入空闲");
  const idleStart = h.calls[h.calls.length - 1].t;
  // 进入空闲前应保持 2s 活跃节奏（无变化的前 60s 仍在确认）
  const activeTs = h.calls.filter((c) => c.t < idleStart).map((c) => c.t);
  for (let i = 1; i < activeTs.length; i++) {
    assert.strictEqual(activeTs[i] - activeTs[i - 1], 2000, "空闲前应保持2s");
  }
  // 再推进两个完整空闲窗
  await h.clock.advance(130000);
  const idleCalls = h.calls.filter((c) => c.t >= idleStart).map((c) => c.t);
  const win1 = idleCalls.filter((t) => t < idleStart + 60000);
  const win2 = idleCalls.filter((t) => t >= idleStart + 60000 && t < idleStart + 120000);
  assert.ok(win1.length <= 6, `首个空闲窗≤6，实际 ${win1.length}`);
  assert.ok(win2.length <= 6, `第二个空闲窗≤6，实际 ${win2.length}`);
  for (let i = 1; i < idleCalls.length; i++) {
    assert.strictEqual(idleCalls[i] - idleCalls[i - 1], 10000, "空闲节奏10s");
  }
  assert.ok(h.calls.some((c) => c.ifNoneMatch === '"g1-r1"'), "空闲复轮带ETag");
});

// 3. 空闲中版本变化 → 立即回活跃、恢复 2s
test("空闲中收到新版本立即恢复活跃", async () => {
  const responses = [];
  // 前 60s 无变化（约 31 个相同响应）→ 进空闲
  for (let i = 0; i < 32; i++) responses.push({ data: { etag: '"g1-r1"', revision: 1 } });
  for (let i = 0; i < 12; i++) responses.push({ data: { etag: `"g1-r${2 + i}"`, revision: 2 + i } });
  const h = makeHarness(responses);
  h.poller.start();
  await h.clock.advance(70000);
  assert.strictEqual(h.poller.isIdle(), true);
  const n = h.calls.length;
  await h.clock.advance(20000); // 期间出现新版本
  assert.strictEqual(h.poller.isIdle(), false, "变化后应退出空闲");
  const later = h.calls.slice(n).map((c) => c.t);
  const active = later.filter((_, i) => i > 0);
  for (let i = 1; i < active.length; i++) {
    assert.strictEqual(active[i] - active[i - 1], 2000, "恢复后2s节奏");
  }
});

// 4. 断线错误后重连继续工作
test("断线错误后重连继续工作", async () => {
  const responses = [];
  responses.push({ data: { etag: '"g1-r1"', revision: 1 } });
  for (let i = 0; i < 5; i++) responses.push({ error: true });
  for (let i = 0; i < 6; i++) responses.push({ data: { etag: `"g1-r${2 + i}"`, revision: 2 + i } });
  const h = makeHarness(responses);
  h.poller.start();
  await h.clock.advance(20000);
  const c = h.counts();
  assert.ok(c.errCount >= 5);
  assert.ok(c.stateCount >= 5);
  assert.strictEqual(h.poller.isPaused(), false);
});

// 5. 暂停/恢复
test("暂停期间不发请求，恢复立即拉取", async () => {
  const responses = Array.from({ length: 30 }, (_, i) => ({ data: { etag: `"g1-r${i}"` } }));
  const h = makeHarness(responses);
  h.poller.start();
  await h.clock.advance(4000);
  const n = h.calls.length;
  h.poller.pause();
  assert.strictEqual(h.poller.isPaused(), true);
  await h.clock.advance(30000);
  assert.strictEqual(h.calls.length, n);
  h.poller.resume();
  await h.clock.advance(100);
  assert.strictEqual(h.calls.length, n + 1);
});

// 6. 在途请求未回时不叠加
test("在途请求未回时不叠加请求", async () => {
  const clock = makeClock();
  const calls = [];
  let holdCb = null;
  const poller = new DrillPoller({
    activeIntervalMs: 2000, idleIntervalMs: 10000, idleWindowMs: 60000,
    idleWindowMaxRequests: 6, clock,
    request: (e, cb) => { calls.push(clock.now()); holdCb = cb; },
    onState() {}, onNotModified() {}, onError() {}, onIdleCount() {}, onModeChange() {},
  });
  poller.start();
  await clock.advance(20000);
  assert.strictEqual(calls.length, 1);
  holdCb({ data: { etag: '"g1-r1"', revision: 1 } });
  await clock.advance(2000);
  assert.strictEqual(calls.length, 2);
});

run().catch((e) => { console.error(e); process.exit(1); });
