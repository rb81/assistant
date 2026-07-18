import { renderMarkdown } from "./markdown.js";

const STATUS_LABELS = {
  queued: "Queued",
  running: "Working",
  waiting: "Waiting",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  needs_review: "Needs review"
};

export function relativeTime(value) {
  if (!value) return "";
  const then = new Date(value).getTime();
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(value).toLocaleDateString();
}

export function StatusPill({ status }) {
  if (!status || status === "completed") return null;
  return <span class={`status-pill status-${status}`}>{STATUS_LABELS[status] || status}</span>;
}

export function Bubble({ role, html, text, time }) {
  return (
    <div class={`bubble-row ${role}`}>
      <div class={`bubble ${role}`}>
        {html
          ? <div class="bubble-markdown" dangerouslySetInnerHTML={{ __html: html }} />
          : <p class="bubble-text">{text}</p>}
        {time ? <time class="bubble-time">{relativeTime(time)}</time> : null}
      </div>
    </div>
  );
}

function stepLabel(log) {
  if (log.tool_name) {
    return log.tool_action ? `${log.tool_name} · ${log.tool_action}` : log.tool_name;
  }
  return String(log.event_type || "step").replace(/_/g, " ");
}

function stepText(log) {
  const output = log.output_data || {};
  const input = log.input_data || {};
  for (const value of [output.message, output.summary, output.text, input.message, input.summary]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function ProgressSteps({ logs, running, done }) {
  if (!logs.length && !running) return null;
  const body = logs.map(log => (
    <li key={log.sequence}>
      <span class="step-label">{stepLabel(log)}</span>
      {stepText(log) ? <span class="step-text">{stepText(log)}</span> : null}
    </li>
  ));
  if (done) {
    return (
      <details class="steps done">
        <summary>{logs.length} step{logs.length === 1 ? "" : "s"}</summary>
        <ul>{body}</ul>
      </details>
    );
  }
  return (
    <details class="steps live" open>
      <summary>
        Working<span class="working-dots" aria-hidden="true"><i /><i /><i /></span>
      </summary>
      <ul>{body}</ul>
    </details>
  );
}

export function ThreadListItem({ thread, active, onSelect, title, snippet, processing, status }) {
  return (
    <button type="button" class={`thread-item ${active ? "active" : ""}`} onClick={onSelect}>
      <span class="thread-item-top">
        <strong class="thread-title">{title}</strong>
        <time>{relativeTime(thread.jobs[thread.jobs.length - 1].created_at)}</time>
      </span>
      <span class="thread-item-bottom">
        <span class="thread-snippet">{processing ? "Arqis is working…" : snippet}</span>
        <StatusPill status={status} />
      </span>
    </button>
  );
}

export function Composer({ disabled, busyLabel, onSend, draft, onDraft }) {
  const submit = event => {
    event.preventDefault();
    const text = draft.trim();
    if (text && !disabled) onSend(text);
  };
  const onKeyDown = event => {
    if (event.key === "Enter" && !event.shiftKey && window.matchMedia("(min-width: 900px)").matches) {
      submit(event);
    }
  };
  return (
    <form class="composer" onSubmit={submit}>
      <textarea
        value={draft}
        onInput={event => onDraft(event.currentTarget.value)}
        onKeyDown={onKeyDown}
        placeholder={disabled ? busyLabel || "Arqis is working…" : "Message Arqis"}
        disabled={disabled}
        rows={1}
      />
      <button type="submit" class="send-button" disabled={disabled || !draft.trim()} aria-label="Send">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path d="M2 10 18 2l-4 8 4 8-16-8Zm4.5 0L14 6.2 8.6 10 14 13.8 6.5 10Z" />
        </svg>
      </button>
    </form>
  );
}

export function Banner({ message, onRetry }) {
  if (!message) return null;
  return (
    <div class="banner" role="alert">
      <span>{message}</span>
      {onRetry ? <button type="button" onClick={onRetry}>Retry</button> : null}
    </div>
  );
}

export function EmptyState({ title, hint }) {
  return (
    <div class="empty-state">
      <p><strong>{title}</strong></p>
      {hint ? <p class="meta">{hint}</p> : null}
    </div>
  );
}

export function IconButton({ label, onClick, children }) {
  return (
    <button type="button" class="icon-button" aria-label={label} title={label} onClick={onClick}>
      {children}
    </button>
  );
}

export { renderMarkdown };
