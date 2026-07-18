import test from "node:test";
import assert from "node:assert/strict";
import {
  groupThreads, threadTitle, isProcessing, latestJob, userMessageOf, replyOf, snippetOf
} from "../src/chat/threads.js";

const job = (id, createdAt, metadata = {}, status = "completed", extra = {}) => ({
  id, status, created_at: createdAt, task_summary: `job ${id}`, metadata, ...extra
});

test("groups follow-ups under their root job, oldest first", () => {
  const jobs = [
    job(3, "2026-07-18T10:02:00Z", { parent_job_id: 1, workspace_message: "third" }),
    job(1, "2026-07-18T10:00:00Z", { workspace_message: "first" }),
    job(2, "2026-07-18T10:01:00Z", { parent_job_id: 1, workspace_message: "second" }),
    job(9, "2026-07-18T09:00:00Z", { workspace_message: "solo" })
  ];
  const threads = groupThreads(jobs);
  assert.equal(threads.length, 2);
  assert.equal(threads[0].rootId, 1);
  assert.deepEqual(threads[0].jobs.map(item => item.id), [1, 2, 3]);
  assert.equal(threads[1].rootId, 9);
});

test("threads sort by latest activity, newest thread first", () => {
  const jobs = [
    job(1, "2026-07-18T10:00:00Z", { workspace_message: "old root" }),
    job(2, "2026-07-18T08:00:00Z", { workspace_message: "other" }),
    job(3, "2026-07-18T11:00:00Z", { parent_job_id: 2, workspace_message: "revived" })
  ];
  assert.deepEqual(groupThreads(jobs).map(thread => thread.rootId), [2, 1]);
});

test("grandchild chains resolve to the original root", () => {
  const jobs = [
    job(1, "2026-07-18T10:00:00Z", { workspace_message: "root" }),
    job(2, "2026-07-18T10:01:00Z", { parent_job_id: 1 }),
    job(3, "2026-07-18T10:02:00Z", { parent_job_id: 2 })
  ];
  const threads = groupThreads(jobs);
  assert.equal(threads.length, 1);
  assert.equal(threads[0].rootId, 1);
});

test("orphan follow-up (parent outside window) still forms a thread", () => {
  const threads = groupThreads([job(5, "2026-07-18T10:00:00Z", { parent_job_id: 999 })]);
  assert.equal(threads.length, 1);
  assert.equal(threads[0].jobs[0].id, 5);
});

test("title truncates the first user message", () => {
  const thread = { rootId: 1, jobs: [job(1, "2026-07-18T10:00:00Z", { workspace_message: "x".repeat(100) })] };
  assert.equal(threadTitle(thread).length, 64);
  assert.ok(threadTitle(thread).endsWith("…"));
});

test("processing state and accessors", () => {
  const thread = {
    rootId: 1,
    jobs: [
      job(1, "2026-07-18T10:00:00Z", { workspace_message: "hi", final_response: "hello!" }),
      job(2, "2026-07-18T10:01:00Z", { parent_job_id: 1, workspace_message: "more" }, "running")
    ]
  };
  assert.equal(isProcessing(thread), true);
  assert.equal(latestJob(thread).id, 2);
  assert.equal(userMessageOf(thread.jobs[0]), "hi");
  assert.equal(replyOf(thread.jobs[0]), "hello!");
  assert.equal(snippetOf(thread), "more");
});
