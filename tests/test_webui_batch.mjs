import test from "node:test";
import assert from "node:assert/strict";
import { parseBatch, BatchQueue } from "../pages/subscriptions/batch.mjs";

test("split input, preserve first spelling and classify without changing existing options", () => {
  const result = parseBatch("@Alpha, beta，ALPHA; gamma；@Known\nwrong-name @@bad\t用户", ["known"]);
  assert.deepEqual(result.entries.map(({ username, status }) => [username, status]), [
    ["Alpha", "pending"], ["beta", "pending"], ["ALPHA", "duplicate"],
    ["gamma", "pending"], ["Known", "existing"], ["wrong-name", "invalid"],
    ["@bad", "invalid"], ["用户", "invalid"],
  ]);
  assert.equal(result.uniqueCount, 4);
  assert.equal(result.overLimit, false);
  assert.deepEqual(parseBatch(" \n,；").entries, []);
});

test("100 unique valid accounts accepted; 101 rejected without truncation", () => {
  const names = Array.from({ length: 101 }, (_, i) => `user${i}`);
  assert.equal(parseBatch(names.slice(0, 100).join("\n")).overLimit, false);
  const result = parseBatch(names.join("\n"));
  assert.equal(result.overLimit, true);
  assert.equal(result.entries.length, 101);
  assert.equal(parseBatch(Array(200).fill("same").join(" ")).overLimit, false);
  assert.equal(parseBatch("a".repeat(16)).entries[0].status, "invalid");
  assert.equal(parseBatch("a".repeat(15)).entries[0].status, "pending");
});

function queue(text, send, onChange) {
  return new BatchQueue({ entries: parseBatch(text).entries, umo: "bot:GroupMessage:100",
    r18: true, media_only: false, send, onChange });
}

test("serial requests retain target and flags, skip existing, continue after failure", async () => {
  const calls = [];
  let active = 0;
  let maximum = 0;
  const job = queue("One Two Three Existing @ONE invalid!", async (payload) => {
    maximum = Math.max(maximum, ++active);
    calls.push(payload);
    await Promise.resolve();
    active--;
    if (payload.username === "Two") throw new Error("模拟超时");
    return { saved: true };
  });
  await job.run(["existing"]);
  assert.equal(maximum, 1);
  assert.deepEqual(calls.map((item) => item.username), ["One", "Two", "Three"]);
  assert.ok(calls.every((item) => item.umo === "bot:GroupMessage:100" && item.r18 && !item.media_only));
  assert.deepEqual(job.entries.map((item) => item.status), ["added", "failed", "added", "existing", "duplicate", "invalid"]);
  assert.equal(job.entries[1].error, "模拟超时");
  assert.equal(job.running, false);
});

test("stop waits for current request; duplicate run cannot send twice", async () => {
  let finish;
  const calls = [];
  const job = queue("one two three", (payload) => {
    calls.push(payload.username);
    return new Promise((resolve) => { finish = resolve; });
  });
  const first = job.run();
  assert.equal(await job.run(), false);
  job.stop();
  assert.equal(job.running, true);
  finish({ saved: true });
  await first;
  assert.deepEqual(calls, ["one"]);
  assert.deepEqual(job.entries.map((item) => item.status), ["added", "pending", "pending"]);
});

test("retry reconciles uncertain and unsent accounts from fresh snapshot", async () => {
  const calls = [];
  const job = queue("one two three", async (payload) => {
    calls.push(payload.username);
    if (payload.username === "one") {
      job.stop();
      throw new Error("服务端可能已保存，但响应丢失");
    }
    return { saved: true };
  });
  await job.run();
  await job.run(["ONE", "two"]);
  assert.deepEqual(calls, ["one", "three"]);
  assert.deepEqual(job.entries.map((item) => item.status), ["existing", "existing", "added"]);
  assert.equal(job.entries[0].error, "");
});

test("failed item can be retried without resending successful entries", async () => {
  let attempt = 0;
  const calls = [];
  const job = queue("one two", async ({ username }) => {
    calls.push(username);
    if (username === "one" && attempt++ === 0) throw new Error("失败");
    return { saved: true };
  });
  await job.run();
  await job.run([]);
  assert.deepEqual(calls, ["one", "two", "one"]);
  assert.ok(job.entries.every((item) => item.status === "added"));
});

test("snapshot is independent of editable input and target cannot change", async () => {
  const parsed = parseBatch("one");
  let payload;
  const job = new BatchQueue({ entries: parsed.entries, umo: "original", r18: false, media_only: true,
    send: async (value) => { payload = value; return { saved: true }; } });
  parsed.entries[0].username = "changed";
  assert.throws(() => { job.target.umo = "other"; }, TypeError);
  await job.run();
  assert.deepEqual(payload, { umo: "original", r18: false, media_only: true, username: "one" });
});

test("missing save confirmation never marked successful", async () => {
  const job = queue("one", async () => ({}));
  await job.run();
  assert.equal(job.entries[0].status, "failed");
  assert.match(job.entries[0].error, /未确认/);
});
