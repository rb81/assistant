export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("Network unreachable — check your Tailscale connection.", 0);
  }
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : "";
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail || `Request failed (${response.status})`, response.status);
  }
  return response.json();
}

const json = body => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body)
});

export const listJobs = () => request("/api/workspace/jobs?limit=100");
export const createJob = message => request("/api/workspace/jobs", json({ message }));
export const sendFollowUp = (jobId, message) =>
  request(`/api/workspace/jobs/${jobId}/messages`, json({ message }));
export const pollJob = (jobId, afterSequence = 0) =>
  request(`/api/jobs/${jobId}/poll?after_sequence=${afterSequence}`);
