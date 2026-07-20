import { ApiError } from "./api.js";

export async function* parseSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLine = chunk.split("\n").find(line => line.startsWith("data:"));
      if (!dataLine) continue;
      const payload = dataLine.slice("data:".length).trim();
      if (!payload) continue;
      yield JSON.parse(payload);
    }
  }
}

export async function streamChatMessage(sessionId, message, onEvent) {
  let response;
  try {
    response = await fetch(`/api/chat/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message })
    });
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
  for await (const event of parseSSE(response.body)) {
    onEvent(event);
  }
}
