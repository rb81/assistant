import test from "node:test";
import assert from "node:assert/strict";
import {
  groupThreads, threadTitle, isProcessing, latestJob, userMessageOf, replyOf, snippetOf,
  mergeConversations, sessionTitle, sessionSnippet, sessionStatus, sessionProcessing
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

test("a running job with its reply written counts as answered, not processing", () => {
  const thread = {
    rootId: 1,
    jobs: [
      job(1, "2026-07-18T10:00:00Z",
        { workspace_message: "hey", final_response: "Hey! How can I help?" },
        "running")
    ]
  };
  assert.equal(isProcessing(thread), false);
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

const session = (id, updatedAt, overrides = {}) => ({
  id, title: `Session ${id}`, created_at: updatedAt, updated_at: updatedAt,
  last_message_content: "", last_message_role: null, last_message_kind: null,
  last_message_job_id: null, last_message_at: updatedAt,
  last_job_status: null, last_job_metadata: null, last_job_last_error: null,
  ...overrides
});

test("mergeConversations interleaves sessions and job threads by latest activity", () => {
  const jobs = [job(1, "2026-07-20T10:00:00Z", { workspace_message: "old job" })];
  const sessions = [session(5, "2026-07-20T11:00:00Z"), session(6, "2026-07-20T09:00:00Z")];
  const merged = mergeConversations(sessions, jobs);
  assert.deepEqual(merged.map(item => item.id), ["session:5", "job:1", "session:6"]);
});

test("session item ids are stable and typed", () => {
  const merged = mergeConversations([session(3, "2026-07-20T10:00:00Z")], []);
  assert.equal(merged[0].type, "session");
  assert.equal(merged[0].session.id, 3);
});

test("sessionSnippet shows working state for a processing job_ref", () => {
  const s = session(1, "2026-07-20T10:00:00Z", {
    last_message_kind: "job_ref", last_message_content: "On it", last_job_status: "running"
  });
  assert.equal(sessionProcessing(s), true);
  assert.equal(sessionSnippet(s), "Arqis is working…");
  assert.equal(sessionStatus(s), "running");
});

test("sessionSnippet shows the reply text once the job_ref job is terminal", () => {
  const s = session(1, "2026-07-20T10:00:00Z", {
    last_message_kind: "job_ref", last_message_content: "On it", last_job_status: "completed"
  });
  assert.equal(sessionProcessing(s), false);
  assert.equal(sessionSnippet(s), "On it");
});

test("sessionSnippet falls back to the last chat message content", () => {
  const s = session(1, "2026-07-20T10:00:00Z", { last_message_kind: "chat", last_message_content: "hello there" });
  assert.equal(sessionSnippet(s), "hello there");
  assert.equal(sessionProcessing(s), false);
});

test("sessionTitle falls back to 'New chat' when blank", () => {
  assert.equal(sessionTitle(session(1, "2026-07-20T10:00:00Z", { title: "" })), "New chat");
});
