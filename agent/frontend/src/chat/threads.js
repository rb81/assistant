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
  return PROCESSING.has(latestJob(thread).status);
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
