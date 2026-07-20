export const PROCESSING = new Set(["queued", "running", "waiting"]);

function parentIdOf(job) {
  const value = Number((job.metadata || {}).parent_job_id);
  return Number.isInteger(value) && value > 0 ? value : null;
}

function rootIdOf(job, byId) {
  let current = job;
  const seen = new Set([current.id]);
  for (;;) {
    const parentId = parentIdOf(current);
    if (!parentId || seen.has(parentId)) return current.id;
    const parent = byId.get(parentId);
    if (!parent) return parentId;
    seen.add(parentId);
    current = parent;
  }
}

export function groupThreads(jobs) {
  const byId = new Map(jobs.map(job => [job.id, job]));
  const grouped = new Map();
  for (const job of jobs) {
    const rootId = rootIdOf(job, byId);
    if (!grouped.has(rootId)) grouped.set(rootId, []);
    grouped.get(rootId).push(job);
  }
  const threads = [];
  for (const [rootId, members] of grouped) {
    members.sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
    threads.push({ rootId, jobs: members });
  }
  threads.sort(
    (a, b) => new Date(latestJob(b).created_at) - new Date(latestJob(a).created_at)
  );
  return threads;
}

export function latestJob(thread) {
  return thread.jobs[thread.jobs.length - 1];
}

export function isProcessing(thread) {
  const latest = latestJob(thread);
  // A job with its reply already written is "answered" even while the backend
  // finishes wrap-up work — the chat should not present it as still working.
  return PROCESSING.has(latest.status) && !replyOf(latest);
}

export function userMessageOf(job) {
  const meta = job.metadata || {};
  return String(meta.workspace_message || job.task_summary || "").trim();
}

export function replyOf(job) {
  return String((job.metadata || {}).final_response || "").trim();
}

export function threadTitle(thread) {
  const text = userMessageOf(thread.jobs[0]).replace(/\s+/g, " ").trim();
  if (!text) return `Chat #${thread.rootId}`;
  return text.length > 64 ? `${text.slice(0, 63).trimEnd()}…` : text;
}

export function snippetOf(thread) {
  const latest = latestJob(thread);
  const text = (replyOf(latest) || userMessageOf(latest)).replace(/\s+/g, " ").trim();
  return text.length > 90 ? `${text.slice(0, 89).trimEnd()}…` : text;
}

export function sessionTitle(session) {
  const text = String(session.title || "").trim();
  return text || "New chat";
}

export function sessionProcessing(session) {
  return session.last_message_kind === "job_ref" && PROCESSING.has(session.last_job_status) && !(
    (session.last_job_metadata || {}).final_response
  );
}

export function sessionSnippet(session) {
  if (sessionProcessing(session)) return "Arqis is working…";
  const text = String(session.last_message_content || "").replace(/\s+/g, " ").trim();
  return text.length > 90 ? `${text.slice(0, 89).trimEnd()}…` : text;
}

export function sessionStatus(session) {
  return session.last_message_kind === "job_ref" ? session.last_job_status : null;
}

function activityOf(item) {
  if (item.type === "session") {
    return new Date(item.session.last_message_at || item.session.updated_at || item.session.created_at).getTime();
  }
  return new Date(latestJob(item.thread).created_at).getTime();
}

export function mergeConversations(sessions, jobs) {
  const jobItems = groupThreads(jobs).map(thread => ({ type: "job", id: `job:${thread.rootId}`, thread }));
  const sessionItems = sessions.map(session => ({ type: "session", id: `session:${session.id}`, session }));
  return [...jobItems, ...sessionItems].sort((a, b) => activityOf(b) - activityOf(a));
}
