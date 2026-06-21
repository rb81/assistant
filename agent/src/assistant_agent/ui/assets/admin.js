let selectedJobId = null;
let selectedReminderId = null;
let selectedProjectId = null;
let selectedMemoryId = null;
let selectedNoteId = null;
let selectedContactId = null;
let selectedEntityId = null;
let currentView = "jobs";
let currentScreen = "list";
let activeJobTab = "overview";
let jobPollTimer = null;
let lastPollSequence = 0;
let memorySearchTimer = null;
let noteSearchTimer = null;
let contactSearchTimer = null;
let expandedDetailsIds = new Set();

const PAGE_SIZE = 20;
const LIST_LIMIT = 200;
const JOB_POLL_MS = 3000;
const LIST_REFRESH_MS = 15000;
const trashIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';

const rowsByView = {
  jobs: [],
  reminders: [],
  projects: [],
  memories: [],
  notes: [],
  contacts: [],
  entities: []
};

const pageByView = {
  jobs: 1,
  reminders: 1,
  projects: 1,
  memories: 1,
  notes: 1,
  contacts: 1,
  entities: 1
};

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit"
});

const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit"
});

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    let message = text || `HTTP ${response.status}`;
    try {
      const data = JSON.parse(text);
      message = data.detail || data.message || message;
    } catch {
      if (/<html[\s>]/i.test(message) || message.length > 240) message = `HTTP ${response.status}`;
    }
    throw new Error(message);
  }
  return response.json();
}

function fmt(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function h(value) {
  return fmt(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function shortDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fmt(value);
  return dateFormatter.format(date);
}

function shortTime(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return timeFormatter.format(date);
}

function formatCost(value) {
  const amount = Number(value || 0);
  if (!Number.isFinite(amount) || amount <= 0) return "$0.00";
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(4)}`;
  return `$${amount.toFixed(6)}`;
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function dateTimeLocalValue(value) {
  if (!value) return "";
  return fmt(value).slice(0, 16);
}

function truncateText(value, limit = 96) {
  const clean = fmt(value).replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  return `${clean.slice(0, Math.max(0, limit - 3)).trim()}...`;
}

function statusClass(status) {
  return `status-${fmt(status).toLowerCase().replace(/[^a-z_]/g, "")}`;
}

function statusPill(status) {
  return `<span class="status ${statusClass(status)}">${h(status)}</span>`;
}

function bodyFor(email) {
  return fmt((email && (email.body_text || email.body_html)) || "");
}

function compactBody(body, limit = 1800) {
  if (body.length <= limit) return body;
  return `${body.slice(0, limit)}\n[truncated: ${body.length - limit} more chars]`;
}

function splitTags(value) {
  return fmt(value)
    .split(",")
    .map(tag => tag.trim())
    .filter(Boolean);
}

function emptyState(text) {
  return `<div class="empty-state">${h(text)}</div>`;
}

function friendlyError(error) {
  return truncateText(fmt(error && error.message ? error.message : error), 220);
}

function setScreen(screen) {
  currentScreen = screen;
  document.getElementById("list-view").classList.toggle("hidden", screen !== "list");
  document.getElementById("detail-view").classList.toggle("hidden", screen !== "detail");
  document.getElementById("form-view").classList.toggle("hidden", screen !== "form");
  if (screen !== "detail") stopJobPoll();
}

function captureExpandedDetails() {
  expandedDetailsIds.clear();
  document.querySelectorAll("details.event-row[open], details.log-row[open]").forEach(details => {
    const summary = details.querySelector("summary");
    if (summary) {
      const pillMatch = summary.textContent.match(/#(\d+)/);
      if (pillMatch) expandedDetailsIds.add(pillMatch[1]);
    }
  });
}

function restoreExpandedDetails() {
  if (expandedDetailsIds.size === 0) return;
  document.querySelectorAll("details.event-row, details.log-row").forEach(details => {
    const summary = details.querySelector("summary");
    if (summary) {
      const pillMatch = summary.textContent.match(/#(\d+)/);
      if (pillMatch && expandedDetailsIds.has(pillMatch[1])) {
        details.setAttribute("open", "");
      }
    }
  });
}

function setDetail(html) {
  captureExpandedDetails();
  document.getElementById("detail").innerHTML = html;
  requestAnimationFrame(() => restoreExpandedDetails());
}

function setDetailTitle(html) {
  document.getElementById("detail-title").innerHTML = html;
}

function setDetailActions(html = "") {
  document.getElementById("detail-actions").innerHTML = html;
}

function jobIdFromUrl() {
  const value = new URLSearchParams(window.location.search).get("job");
  if (!value || !/^\d+$/.test(value)) return null;
  return Number(value);
}

function updateJobUrl(jobId = null) {
  const url = new URL(window.location.href);
  if (jobId) {
    url.searchParams.set("job", jobId);
  } else {
    url.searchParams.delete("job");
  }
  window.history.replaceState(null, "", url);
}

function selectedIdForView(view = currentView) {
  if (view === "jobs") return selectedJobId;
  if (view === "reminders") return selectedReminderId;
  if (view === "projects") return selectedProjectId;
  if (view === "memories") return selectedMemoryId;
  if (view === "notes") return selectedNoteId;
  return selectedContactId;
}

function viewLabel(view = currentView) {
  if (view === "reminders") return "Reminder";
  if (view === "projects") return "Project";
  if (view === "memories") return "Memory";
  if (view === "notes") return "Note";
  if (view === "contacts") return "Contact";
  if (view === "entities") return "Entity";
  return "Job";
}

function tableColumnsFor(view) {
  if (view === "reminders") return ["Status", "Title", "Run At", "Recurrence"];
  if (view === "projects") return ["Status", "Project", "Tasks", "Updated"];
  if (view === "memories") return ["Content", "Tags", "Updated"];
  if (view === "notes") return ["Title", "Tags", "Updated"];
  if (view === "contacts") return ["Name", "Email", "Company", "Updated"];
  if (view === "entities") return ["Name", "Description", "Objects", "Created"];
  return ["Status", "Task", "Cost", "Updated"];
}

function setActiveViewButton(view) {
  currentView = view;
  for (const id of ["jobs", "reminders", "projects", "memories", "notes", "contacts", "entities"]) {
    document.getElementById(`${id}-view-button`).classList.toggle("active", id === view);
  }
  document.getElementById("status-filter").classList.toggle("hidden", view !== "jobs");
  document.getElementById("reminder-status-filter").classList.toggle("hidden", view !== "reminders");
  document.getElementById("project-status-filter").classList.toggle("hidden", view !== "projects");
  document.getElementById("memory-filters").classList.toggle("hidden", view !== "memories");
  document.getElementById("note-filters").classList.toggle("hidden", view !== "notes");
  document.getElementById("note-query").classList.toggle("hidden", view !== "notes");
  document.getElementById("contact-query").classList.toggle("hidden", view !== "contacts");
  const createButton = document.getElementById("create-record");
  createButton.classList.toggle("hidden", view === "projects");
  createButton.textContent = `New ${viewLabel(view)}`;
}

function memoryQueryString() {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const query = document.getElementById("memory-query").value.trim();
  const tag = document.getElementById("memory-tag").value.trim();
  const scope = document.getElementById("memory-scope").value.trim();
  const kind = document.getElementById("memory-kind").value.trim();
  const pinned = document.getElementById("memory-pinned").value;
  if (query) params.set("query", query);
  if (tag) params.set("tag", tag);
  if (scope) params.set("scope", scope);
  if (kind) params.set("kind", kind);
  if (pinned) params.set("pinned", pinned);
  if (document.getElementById("memory-include-expired").checked) params.set("include_expired", "true");
  return `?${params.toString()}`;
}

function noteQueryString() {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const query = document.getElementById("note-query").value.trim();
  const tag = document.getElementById("note-tag").value.trim();
  if (query) params.set("query", query);
  if (tag) params.set("tag", tag);
  return `?${params.toString()}`;
}

function contactQueryString() {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const query = document.getElementById("contact-query").value.trim();
  if (query) params.set("query", query);
  return `?${params.toString()}`;
}

async function loadJobs(options = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const status = document.getElementById("status-filter").value;
  if (status) params.set("status", status);
  const data = await api(`/api/jobs?${params.toString()}`);
  rowsByView.jobs = data.jobs || [];
  if (!options.preservePage) pageByView.jobs = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadReminders(options = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const status = document.getElementById("reminder-status-filter").value;
  if (status) params.set("status", status);
  const data = await api(`/api/reminders?${params.toString()}`);
  rowsByView.reminders = data.reminders || [];
  if (!options.preservePage) pageByView.reminders = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadProjects(options = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  const status = document.getElementById("project-status-filter").value;
  if (status) params.set("status", status);
  const data = await api(`/api/projects?${params.toString()}`);
  rowsByView.projects = data.projects || [];
  if (!options.preservePage) pageByView.projects = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadMemories(options = {}) {
  const data = await api(`/api/memories${memoryQueryString()}`);
  rowsByView.memories = data.memories || [];
  if (!options.preservePage) pageByView.memories = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadNotes(options = {}) {
  const data = await api(`/api/notes${noteQueryString()}`);
  rowsByView.notes = data.notes || [];
  if (!options.preservePage) pageByView.notes = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadContacts(options = {}) {
  const data = await api(`/api/contacts${contactQueryString()}`);
  rowsByView.contacts = data.contacts || [];
  if (!options.preservePage) pageByView.contacts = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadEntities(options = {}) {
  const data = await api("/api/entities");
  rowsByView.entities = data.entities || [];
  if (!options.preservePage) pageByView.entities = 1;
  renderTable({resetScroll: !options.preservePage || Boolean(options.resetScroll)});
}

async function loadCurrentList(options = {}) {
  if (currentView === "reminders") return loadReminders(options);
  if (currentView === "projects") return loadProjects(options);
  if (currentView === "memories") return loadMemories(options);
  if (currentView === "notes") return loadNotes(options);
  if (currentView === "contacts") return loadContacts(options);
  if (currentView === "entities") return loadEntities(options);
  return loadJobs(options);
}

function jobPreview(job) {
  const metadata = job.metadata || {};
  return truncateText(metadata.final_response || job.last_error || job.trigger_from_address || job.thread_id || job.created_at, 80);
}

function jobRow(job) {
  const active = job.id === selectedJobId ? " active" : "";
  const title = truncateText(job.task_summary || job.trigger_subject || job.thread_id || "Untitled job", 120);
  return `
    <tr class="table-row${active}" data-view="jobs" data-id="${h(job.id)}" tabindex="0">
      <td>${statusPill(job.status)}</td>
      <td><strong>${h(title)}</strong></td>
      <td class="muted-cell">${h(formatCost(job.cost_total))}</td>
      <td class="muted-cell">${h(shortDate(job.updated_at || job.created_at))}</td>
    </tr>
  `;
}

function recurrenceLabel(reminder) {
  if (!reminder.recurrence_unit) return "One-time";
  const interval = Number(reminder.recurrence_interval || 1);
  const unit = fmt(reminder.recurrence_unit);
  return `Every ${interval} ${unit}${interval === 1 ? "" : "s"}`;
}

function reminderRow(reminder) {
  const active = reminder.id === selectedReminderId ? " active" : "";
  return `
    <tr class="table-row${active}" data-view="reminders" data-id="${h(reminder.id)}" tabindex="0">
      <td>${statusPill(reminder.status)}</td>
      <td><strong>${h(truncateText(reminder.title || `Reminder #${reminder.id}`, 120))}</strong></td>
      <td class="muted-cell">${h(shortDate(reminder.run_at))}</td>
      <td class="muted-cell">${h(recurrenceLabel(reminder))}</td>
    </tr>
  `;
}

function projectRow(project) {
  const active = project.id === selectedProjectId ? " active" : "";
  const total = Number(project.task_count || 0);
  const completed = Number(project.completed_task_count || 0);
  const failed = Number(project.failed_task_count || 0);
  const taskText = failed ? `${completed}/${total} done, ${failed} failed` : `${completed}/${total} done`;
  return `
    <tr class="table-row${active}" data-view="projects" data-id="${h(project.id)}" tabindex="0">
      <td>${statusPill(project.status)}</td>
      <td><strong>${h(truncateText(project.title || `Project #${project.id}`, 120))}</strong></td>
      <td class="muted-cell">${h(taskText)}</td>
      <td class="muted-cell">${h(shortDate(project.updated_at || project.created_at))}</td>
    </tr>
  `;
}

function memoryPills(memory) {
  const pills = [
    `<span class="pill">#${h(memory.id)}</span>`,
    `<span class="pill">${h(memory.kind || "fact")}</span>`,
    `<span class="pill">${h(memory.scope || "global")}</span>`,
    `<span class="pill">I${h(memory.importance)}</span>`,
    `<span class="pill">C${h(memory.confidence)}</span>`
  ];
  if (memory.pinned) pills.unshift(`<span class="status status-running">Pinned</span>`);
  if (memory.expired) pills.unshift(`<span class="status status-failed">Expired</span>`);
  return pills.join("");
}

function memoryRow(memory) {
  const active = memory.id === selectedMemoryId ? " active" : "";
  const tags = (memory.tags || []).length ? (memory.tags || []).map(tag => `#${tag}`).join(" ") : "—";
  return `
    <tr class="table-row${active}" data-view="memories" data-id="${h(memory.id)}" tabindex="0">
      <td><strong>${h(truncateText(memory.content, 140))}</strong></td>
      <td class="tags-cell">${h(tags)}</td>
      <td class="muted-cell">${h(shortDate(memory.updated_at))}</td>
    </tr>
  `;
}

function noteRow(note) {
  const active = note.id === selectedNoteId ? " active" : "";
  const tags = (note.tags || []).length ? (note.tags || []).map(tag => `#${tag}`).join(" ") : "—";
  return `
    <tr class="table-row${active}" data-view="notes" data-id="${h(note.id)}" tabindex="0">
      <td><strong>${h(truncateText(note.title || "Untitled note", 120))}</strong></td>
      <td class="tags-cell">${h(tags)}</td>
      <td class="muted-cell">${h(shortDate(note.updated_at))}</td>
    </tr>
  `;
}

function contactName(contact) {
  const name = [contact.first_name, contact.last_name].map(fmt).filter(Boolean).join(" ").trim();
  return name || contact.email_address || contact.company || `Contact #${contact.id}`;
}

function contactPills(contact) {
  return [
    `<span class="pill">#${h(contact.id)}</span>`,
    `<span class="pill">${h(contact.source || "agent")}</span>`
  ].join("");
}

function contactRow(contact) {
  const active = contact.id === selectedContactId ? " active" : "";
  const company = contact.company || "—";
  return `
    <tr class="table-row${active}" data-view="contacts" data-id="${h(contact.id)}" tabindex="0">
      <td><strong>${h(contactName(contact))}</strong></td>
      <td class="muted-cell">${h(contact.email_address || "—")}</td>
      <td class="muted-cell">${h(truncateText(company, 90))}</td>
      <td class="muted-cell">${h(shortDate(contact.updated_at))}</td>
    </tr>
  `;
}

function entityRow(entity) {
  const active = entity.id === selectedEntityId ? " active" : "";
  const totalObjects = Number(entity.total_objects || 0);
  const desc = truncateText(entity.description || "—", 80);
  return `
    <tr class="table-row${active}" data-view="entities" data-id="${h(entity.id)}" tabindex="0">
      <td><strong>${h(entity.name)}</strong></td>
      <td class="muted-cell">${h(desc)}</td>
      <td class="muted-cell">${h(totalObjects)}</td>
      <td class="muted-cell">${h(shortDate(entity.created_at))}</td>
    </tr>
  `;
}

function rowHtmlFor(view, row) {
  if (view === "reminders") return reminderRow(row);
  if (view === "projects") return projectRow(row);
  if (view === "memories") return memoryRow(row);
  if (view === "notes") return noteRow(row);
  if (view === "contacts") return contactRow(row);
  if (view === "entities") return entityRow(row);
  return jobRow(row);
}

function attachTableRowHandlers() {
  document.querySelectorAll(".table-row[data-view][data-id]").forEach(row => {
    const open = () => openRecord(row.dataset.view, Number(row.dataset.id));
    row.onclick = open;
    row.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    };
  });
}

function resetTableScroll() {
  const shell = document.querySelector(".table-shell");
  if (!shell) return;
  shell.scrollTop = 0;
  shell.scrollLeft = 0;
}

function renderTable(options = {}) {
  const rows = rowsByView[currentView] || [];
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  pageByView[currentView] = Math.min(Math.max(pageByView[currentView], 1), totalPages);
  const page = pageByView[currentView];
  const start = (page - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);
  const table = document.querySelector(".data-table");
  const empty = document.getElementById("table-empty");
  const tableShell = document.querySelector(".table-shell");

  document.getElementById("table-head").innerHTML = `<tr>${tableColumnsFor(currentView).map(label => `<th>${h(label)}</th>`).join("")}</tr>`;
  document.getElementById("table-body").innerHTML = pageRows.map(row => rowHtmlFor(currentView, row)).join("");
  table.classList.toggle("hidden", total === 0);
  empty.classList.toggle("hidden", total !== 0);
  empty.textContent = `No ${currentView} found.`;
  tableShell.classList.toggle("empty", total === 0);

  const end = Math.min(start + PAGE_SIZE, total);
  document.getElementById("pagination-summary").textContent = total
    ? `${start + 1}-${end} of ${total} ${currentView}`
    : `0 ${currentView}`;
  document.getElementById("page-prev").disabled = page <= 1;
  document.getElementById("page-next").disabled = page >= totalPages;
  if (options.resetScroll) resetTableScroll();
  attachTableRowHandlers();
}

function showTableError(error) {
  document.querySelector(".data-table").classList.add("hidden");
  const empty = document.getElementById("table-empty");
  const tableShell = document.querySelector(".table-shell");
  empty.classList.remove("hidden");
  empty.textContent = `Failed to load ${currentView}: ${friendlyError(error)}`;
  tableShell.classList.add("empty");
  document.getElementById("pagination-summary").textContent = "";
  document.getElementById("page-prev").disabled = true;
  document.getElementById("page-next").disabled = true;
}

async function setView(view, options = {}) {
  stopJobPoll();
  const viewChanged = currentView !== view;
  setScreen("list");
  setActiveViewButton(view);
  updateJobUrl(null);
  if (options.resetPage) pageByView[view] = 1;
  try {
    await loadCurrentList({
      preservePage: !options.resetPage,
      resetScroll: viewChanged || Boolean(options.resetPage)
    });
  } catch (error) {
    showTableError(error);
  }
}

async function openRecord(view, id) {
  if (view === "reminders") return loadReminderDetail(id);
  if (view === "projects") return loadProjectDetail(id);
  if (view === "memories") return loadMemoryDetail(id);
  if (view === "notes") return loadNoteDetail(id);
  if (view === "contacts") return loadContactDetail(id);
  if (view === "entities") return loadEntityDetail(id);
  return loadJobDetail(id);
}

function dashboardSummaryHtml(data) {
  const costs = data.costs || {};
  const chips = [
    ["Lifetime", formatCost(costs.lifetime_total)],
    ["This month", formatCost(costs.month_total)],
    ["Avg/job", formatCost(costs.average_per_job)],
    ["API calls", formatCount(costs.api_call_count)],
    ["Jobs with cost", `${formatCount(costs.charged_job_count)} / ${formatCount(costs.job_count)}`]
  ];
  return chips
    .map(([label, value]) => `<span class="summary-chip"><span>${h(label)}</span><strong>${h(value)}</strong></span>`)
    .join("");
}

async function loadStats() {
  const data = await api("/api/stats");
  document.getElementById("dashboard-summary").innerHTML = dashboardSummaryHtml(data);
}

function memoryEventCard(event) {
  const payload = {
    input: event.input_data || {},
    output: event.output_data || {}
  };
  return `
    <details class="event-row">
      <summary>
        <span class="pill">#${h(event.id)}</span>
        <strong>${h(event.event_type)} · ${h(event.actor)}</strong>
        <span class="meta">${h(shortDate(event.created_at))}</span>
      </summary>
      <pre>${h(JSON.stringify(payload, null, 2))}</pre>
    </details>
  `;
}

function memoryPayloadFromForm(form) {
  let metadata = {};
  const metadataElement = form.elements.metadata;
  if (metadataElement && metadataElement.value.trim()) {
    metadata = JSON.parse(metadataElement.value);
  }
  const expiresAt = fmt(form.elements.expires_at.value).trim();
  return {
    content: form.elements.content.value,
    tags: splitTags(form.elements.tags.value),
    scope: form.elements.scope.value || "global",
    kind: form.elements.kind.value || "project_context",
    importance: Number(form.elements.importance.value || 3),
    confidence: Number(form.elements.confidence.value || 0.7),
    expires_at: expiresAt ? expiresAt : null,
    pinned: form.elements.pinned.checked,
    metadata
  };
}

function notePayloadFromForm(form) {
  let metadata = {};
  const metadataElement = form.elements.metadata;
  if (metadataElement && metadataElement.value.trim()) {
    metadata = JSON.parse(metadataElement.value);
  }
  return {
    title: fmt(form.elements.title.value).trim() || null,
    content: form.elements.content.value,
    tags: splitTags(form.elements.tags.value),
    metadata
  };
}

function contactPayloadFromForm(form) {
  const data = new FormData(form);
  return {
    first_name: fmt(data.get("first_name")),
    last_name: fmt(data.get("last_name")),
    email_address: fmt(data.get("email_address")),
    company: fmt(data.get("company")),
    title: fmt(data.get("title")),
    notes: fmt(data.get("notes"))
  };
}

function reminderPayloadFromForm(form) {
  const data = new FormData(form);
  const recurrenceUnit = fmt(data.get("recurrence_unit")).trim();
  const recurrenceInterval = fmt(data.get("recurrence_interval")).trim();
  return {
    title: fmt(data.get("title")).trim(),
    task: fmt(data.get("task")).trim(),
    run_at: fmt(data.get("run_at")).trim(),
    priority: Number(data.get("priority") || 0),
    recurrence_unit: recurrenceUnit || null,
    recurrence_interval: recurrenceUnit ? Number(recurrenceInterval || 1) : null
  };
}

function recurrenceFieldsHtml(reminder = {}) {
  const unit = reminder.recurrence_unit || "";
  const interval = reminder.recurrence_interval || 1;
  return `
    <div class="field-grid">
      <label>Recurrence
        <select name="recurrence_unit">
          <option value="" ${unit ? "" : "selected"}>One-time</option>
          <option value="hour" ${unit === "hour" ? "selected" : ""}>Hourly</option>
          <option value="day" ${unit === "day" ? "selected" : ""}>Daily</option>
          <option value="week" ${unit === "week" ? "selected" : ""}>Weekly</option>
          <option value="month" ${unit === "month" ? "selected" : ""}>Monthly</option>
        </select>
      </label>
      <label>Interval<input name="recurrence_interval" type="number" min="1" value="${h(interval)}"></label>
    </div>
  `;
}

function reminderFormHtml(reminder = {}) {
  return `
    <label class="full-field">Title<input name="title" value="${h(reminder.title || "")}" required></label>
    <label class="full-field">Task<textarea name="task" required>${h(reminder.task || "")}</textarea></label>
    <div class="field-grid">
      <label>Run At<input name="run_at" type="datetime-local" value="${h(dateTimeLocalValue(reminder.run_at_local || reminder.run_at))}" required></label>
      <label>Priority<input name="priority" type="number" value="${h(reminder.priority || 0)}"></label>
    </div>
    ${recurrenceFieldsHtml(reminder)}
  `;
}

function reminderDetailHtml(data) {
  const reminder = data.reminder;
  const linkedJob = data.job;
  const createdByJob = data.created_by_job;
  const linkedJobButton = linkedJob
    ? `<button class="button" type="button" onclick="openJob(${linkedJob.id})">Open Queued Job #${h(linkedJob.id)}</button>`
    : "";
  const createdByJobButton = createdByJob
    ? `<button class="button" type="button" onclick="openJob(${createdByJob.id})">Open Source Job #${h(createdByJob.id)}</button>`
    : "";
  const editBlock = reminder.status === "scheduled"
    ? `
      <div class="section-block">
        <div class="section-heading"><h2>Edit Reminder</h2></div>
        <form class="stack-form" onsubmit="saveReminder(event, ${reminder.id})">
          ${reminderFormHtml(reminder)}
          <div class="actions">
            <button class="button primary" type="submit">Save Reminder</button>
          </div>
        </form>
      </div>
    `
    : "";
  return `
    <div class="detail-card">
      <pre>${h(reminder.task)}</pre>
      <div class="summary-grid cols-3">
        <div class="summary-item"><span>ID</span><strong>#${h(reminder.id)}</strong></div>
        <div class="summary-item"><span>Status</span><strong>${h(reminder.status)}</strong></div>
        <div class="summary-item"><span>Run At</span><strong>${h(shortDate(reminder.run_at))}</strong></div>
        <div class="summary-item"><span>Recurrence</span><strong>${h(recurrenceLabel(reminder))}</strong></div>
        <div class="summary-item"><span>Priority</span><strong>${h(reminder.priority || 0)}</strong></div>
        <div class="summary-item"><span>Created By</span><strong>${h(reminder.created_by || "-")}</strong></div>
        <div class="summary-item"><span>Queued</span><strong>${h(shortDate(reminder.queued_at) || "-")}</strong></div>
        <div class="summary-item"><span>Completed</span><strong>${h(shortDate(reminder.completed_at) || "-")}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(reminder.updated_at))}</strong></div>
      </div>
      ${reminder.last_error ? `<pre>${h(reminder.last_error)}</pre>` : ""}
      <div class="actions">${linkedJobButton}${createdByJobButton}</div>
    </div>
    ${editBlock}
  `;
}

function noteDetailHtml(data) {
  const note = data.note;
  const sourceJob = data.source_job;
  const events = data.events || [];
  const tags = (note.tags || []).join(", ");
  const metadata = JSON.stringify(note.metadata || {}, null, 2);
  const sourceJobBlock = sourceJob
    ? `<button class="button" type="button" onclick="openJob(${sourceJob.id})">Open Source Job #${h(sourceJob.id)}</button>`
    : "";
  const eventList = events.length
    ? events.map(memoryEventCard).join("")
    : emptyState("No note events.");
  return `
    <div class="detail-card">
      <pre>${h(note.content)}</pre>
      <div class="summary-grid">
        <div class="summary-item"><span>ID</span><strong>#${h(note.id)}</strong></div>
        <div class="summary-item"><span>Embedding</span><strong>${h(note.embedding_model || "none")}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(note.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(note.updated_at))}</strong></div>
        <div class="summary-item"><span>Last Accessed</span><strong>${h(shortDate(note.last_accessed_at) || "-")}</strong></div>
      </div>
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Edit Note</h2></div>
      <form class="stack-form" onsubmit="saveNote(event, ${note.id})">
        <label class="full-field">Title<input name="title" value="${h(note.title || "")}"></label>
        <label class="full-field">Content<textarea name="content" required>${h(note.content)}</textarea></label>
        <label class="full-field">Tags<input name="tags" value="${h(tags)}"></label>
        <label class="full-field">Metadata<textarea name="metadata">${h(metadata)}</textarea></label>
        <div class="actions">
          <button class="button primary" type="submit">Save Note</button>
          <button class="button danger" type="button" onclick="deleteNote(${note.id})">Delete</button>
        </div>
      </form>
    </div>

    <div class="section-block">
      <div class="section-heading">
        <h2>Note Events</h2>
        <span class="meta">${h(events.length)} events</span>
      </div>
      <div class="event-list">${eventList}</div>
    </div>
  `;
}

function contactDetailHtml(data) {
  const contact = data.contact;
  return `
    <div class="detail-card">
      <div class="summary-grid">
        <div class="summary-item"><span>ID</span><strong>#${h(contact.id)}</strong></div>
        <div class="summary-item"><span>Source</span><strong>${h(contact.source || "-")}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(contact.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(contact.updated_at))}</strong></div>
      </div>
      ${contact.notes ? `<pre>${h(contact.notes)}</pre>` : ""}
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Edit Contact</h2></div>
      <form class="stack-form" onsubmit="saveContact(event, ${contact.id})">
        <div class="field-grid">
          <label>First Name<input name="first_name" value="${h(contact.first_name || "")}"></label>
          <label>Last Name<input name="last_name" value="${h(contact.last_name || "")}"></label>
          <label>Email Address<input name="email_address" value="${h(contact.email_address || "")}"></label>
          <label>Company<input name="company" value="${h(contact.company || "")}"></label>
          <label>Title<input name="title" value="${h(contact.title || "")}"></label>
          <label>Source<input value="${h(contact.source || "")}" disabled></label>
        </div>
        <label class="full-field">Notes<textarea name="notes">${h(contact.notes || "")}</textarea></label>
        <div class="actions">
          <button class="button primary" type="submit">Save Contact</button>
          <button class="button danger" type="button" onclick="deleteContact(${contact.id})">Delete</button>
        </div>
      </form>
    </div>
  `;
}

function memoryDetailHtml(data) {
  const memory = data.memory;
  const sourceJob = data.source_job;
  const events = data.events || [];
  const tags = (memory.tags || []).join(", ");
  const metadata = JSON.stringify(memory.metadata || {}, null, 2);
  const sourceJobBlock = sourceJob
    ? `<button class="button" type="button" onclick="openJob(${sourceJob.id})">Open Source Job #${h(sourceJob.id)}</button>`
    : "";
  const eventList = events.length
    ? events.map(memoryEventCard).join("")
    : emptyState("No memory events.");
  return `
    <div class="detail-card">
      <pre>${h(memory.content)}</pre>
      <div class="summary-grid">
        <div class="summary-item"><span>ID</span><strong>#${h(memory.id)}</strong></div>
        <div class="summary-item"><span>Kind</span><strong>${h(memory.kind || "fact")}</strong></div>
        <div class="summary-item"><span>Scope</span><strong>${h(memory.scope || "global")}</strong></div>
        <div class="summary-item"><span>Importance</span><strong>${h(memory.importance)} / 5</strong></div>
        <div class="summary-item"><span>Confidence</span><strong>${h(memory.confidence)}</strong></div>
        <div class="summary-item"><span>Pinned</span><strong>${memory.pinned ? "Yes" : "No"}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(memory.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(memory.updated_at))}</strong></div>
      </div>
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Edit Memory</h2></div>
      <form class="stack-form" onsubmit="saveMemory(event, ${memory.id})">
        <label class="full-field">Content<textarea name="content" required>${h(memory.content)}</textarea></label>
        <label class="full-field">Tags<input name="tags" value="${h(tags)}"></label>
        <div class="field-grid">
          <label>Scope<input name="scope" value="${h(memory.scope || "global")}"></label>
          <label>Kind<input name="kind" value="${h(memory.kind || "fact")}"></label>
          <label>Importance<input name="importance" type="number" min="1" max="5" value="${h(memory.importance || 3)}"></label>
          <label>Confidence<input name="confidence" type="number" min="0" max="1" step="0.05" value="${h(memory.confidence || 0.7)}"></label>
          <label>Expires at<input name="expires_at" value="${h(memory.expires_at || "")}"></label>
          <label>Embedding<input value="${h(memory.embedding_model || "none")}" disabled></label>
        </div>
        <label class="check-row"><input name="pinned" type="checkbox" ${memory.pinned ? "checked" : ""}>Pinned</label>
        <label class="full-field">Metadata<textarea name="metadata">${h(metadata)}</textarea></label>
        <div class="actions">
          <button class="button primary" type="submit">Save Memory</button>
          <button class="button danger" type="button" onclick="deleteMemory(${memory.id})">Delete</button>
        </div>
      </form>
    </div>

    <div class="section-block">
      <div class="section-heading">
        <h2>Memory Events</h2>
        <span class="meta">${h(events.length)} events</span>
      </div>
      <div class="event-list">${eventList}</div>
    </div>
  `;
}

function projectTaskCard(task) {
  const jobButton = task.job_id
    ? `<button class="button" type="button" onclick="openJob(${task.job_id})">Open Job #${h(task.job_id)}</button>`
    : "";
  const payload = {
    task: task.task,
    result_summary: task.result_summary || "",
    last_error: task.last_error || task.job_last_error || "",
    metadata: task.metadata || {}
  };
  return `
    <details class="event-row" ${task.status === "running" || task.status === "failed" ? "open" : ""}>
      <summary>
        <span class="pill">#${h(task.sequence)}</span>
        <strong>${h(task.title)} · ${h(task.status)}</strong>
        <span class="meta">${h(task.job_id ? `Job #${task.job_id} · ${formatCost(task.job_cost_total)}` : "No job yet")}</span>
      </summary>
      <div class="task-detail-body">
        <div class="summary-grid cols-3">
          <div class="summary-item"><span>Task Status</span><strong>${h(task.status)}</strong></div>
          <div class="summary-item"><span>Job Status</span><strong>${h(task.job_status || "-")}</strong></div>
          <div class="summary-item"><span>Cost</span><strong>${h(formatCost(task.job_cost_total))}</strong></div>
          <div class="summary-item"><span>Queued</span><strong>${h(shortDate(task.queued_at) || "-")}</strong></div>
          <div class="summary-item"><span>Completed</span><strong>${h(shortDate(task.completed_at) || "-")}</strong></div>
          <div class="summary-item"><span>Updated</span><strong>${h(shortDate(task.updated_at))}</strong></div>
        </div>
        <pre>${h(JSON.stringify(payload, null, 2))}</pre>
        <div class="actions">${jobButton}</div>
      </div>
    </details>
  `;
}

function projectDetailHtml(data) {
  const project = data.project;
  const tasks = data.tasks || [];
  const completed = tasks.filter(task => task.status === "completed").length;
  const failed = tasks.filter(task => task.status === "failed" || task.status === "cancelled").length;
  const totalCost = tasks.reduce((sum, task) => sum + Number(task.job_cost_total || 0), 0);
  const originalJobButton = project.original_job_id
    ? `<button class="button" type="button" onclick="openJob(${project.original_job_id})">Open Original Job #${h(project.original_job_id)}</button>`
    : "";
  const taskList = tasks.length
    ? tasks.map(projectTaskCard).join("")
    : emptyState("No project tasks.");
  return `
    <div class="detail-card">
      <div class="summary-grid cols-3">
        <div class="summary-item"><span>ID</span><strong>#${h(project.id)}</strong></div>
        <div class="summary-item"><span>Status</span><strong>${h(project.status)}</strong></div>
        <div class="summary-item"><span>Tasks</span><strong>${h(completed)} / ${h(tasks.length)} complete</strong></div>
        <div class="summary-item"><span>Failed Tasks</span><strong>${h(failed)}</strong></div>
        <div class="summary-item"><span>Cost</span><strong>${h(formatCost(totalCost))}</strong></div>
        <div class="summary-item"><span>Priority</span><strong>${h(project.priority || 0)}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(project.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(project.updated_at))}</strong></div>
        <div class="summary-item"><span>Completed</span><strong>${h(shortDate(project.completed_at) || "-")}</strong></div>
      </div>
      ${project.result_summary ? `<pre>${h(project.result_summary)}</pre>` : ""}
      ${project.last_error ? `<pre>${h(project.last_error)}</pre>` : ""}
      <div class="actions">${originalJobButton}</div>
    </div>

    <div class="section-block">
      <div class="section-heading">
        <h2>Project Tasks</h2>
        <span class="meta">${h(tasks.length)} tasks</span>
      </div>
      <div class="event-list">${taskList}</div>
    </div>
  `;
}

function emailCard(email, options = {}) {
  if (!email) return emptyState("No email.");
  const outgoing = email.context_type === "outbound_email" || email.direction === "outbound";
  const body = compactBody(bodyFor(email));
  const label = options.label || (outgoing ? `Sent Email #${email.outbound_log_id || email.id}` : `Email #${email.id}`);
  const timeValue = outgoing ? (email.sent_at || email.created_at) : (email.received_at || email.created_at);
  const timeLabel = outgoing ? "Sent" : "Received";
  return `
    <article class="email-card ${options.trigger ? "trigger" : ""} ${outgoing ? "outgoing" : ""}">
      <div class="email-head">
        <h3>${h(email.subject || "(no subject)")}</h3>
        <div class="email-head-meta">
          <span class="pill">${h(label)}</span>
          <time class="meta">${h(shortDate(timeValue))}</time>
        </div>
      </div>
      <div class="email-meta">
        <div><strong>From</strong><br>${h(email.from_address)}</div>
        <div><strong>To</strong><br>${h((email.to_addresses || []).join(", "))}</div>
        <div><strong>Message-ID</strong><br>${h(email.message_id)}</div>
        <div><strong>${h(timeLabel)}</strong><br>${h(timeValue || "")}</div>
      </div>
      <pre class="email-body">${h(body || "(empty body)")}</pre>
    </article>
  `;
}

function logCard(log) {
  const payload = log.output_data || log.input_data || {};
  const tool = log.tool_name ? ` · ${log.tool_name}` : "";
  const cost = log.tokens_used && log.tokens_used.cost !== undefined ? ` · ${formatCost(log.tokens_used.cost)}` : "";
  return `
    <details class="log-row">
      <summary>
        <span class="pill">#${h(log.sequence)}</span>
        <strong>${h(log.event_type)}${h(tool)}</strong>
        <span class="meta">${h(shortDate(log.created_at))}${h(cost)}</span>
      </summary>
      <pre>${h(JSON.stringify(payload, null, 2))}</pre>
    </details>
  `;
}

function reviewOverrideValue(job, defaults, key) {
  const metadata = job.metadata || {};
  const override = metadata.admin_review_override || {};
  if (override[key]) return override[key];
  const current = Number(defaults[key] || 0);
  const reason = fmt(job.last_error).toLowerCase();
  if (key === "max_tokens_per_task" && reason.includes("token budget")) return current ? current * 2 : "";
  if (key === "max_iterations_per_task" && reason.includes("max iterations")) return current ? current * 2 : "";
  return current || "";
}

function diagBarHtml(label, used, limit, pct) {
  const safePct = Math.min(100, Math.max(0, Math.round(Number(pct || 0))));
  const barColor = safePct >= 90 ? "var(--color-danger, #e53)" : safePct >= 70 ? "var(--color-warn, #f90)" : "var(--color-accent, #4a9)";
  return `
    <div class="diag-bar-row">
      <span class="diag-bar-label">${h(label)}</span>
      <div class="diag-bar" role="progressbar" aria-valuenow="${safePct}" aria-valuemin="0" aria-valuemax="100">
        <div class="diag-bar-fill" style="width:${safePct}%;background:${barColor}"></div>
      </div>
      <span class="diag-bar-meta meta">${h(used)} / ${h(limit)} (${safePct}%)</span>
    </div>
  `;
}

function jobReviewOverrideHtml(data) {
  const job = data.job;
  if (job.status !== "needs_review") return "";
  const defaults = data.review_defaults || {};
  const iterations = reviewOverrideValue(job, defaults, "max_iterations_per_task");
  const tokens = reviewOverrideValue(job, defaults, "max_tokens_per_task");

  // --- Diagnostics panel ---
  let diagHtml = "";
  const diag = data.review_diagnostics || null;
  if (diag) {
    const usage = diag.usage || {};
    const limits = diag.limits || {};
    const tokenPct = Number(usage.token_pct || 0);
    const iterPct = Number(usage.iteration_pct || 0);
    diagHtml = `
      <div class="diag-panel">
        <div class="diag-stop-reason">
          <strong>${h(diag.stop_reason || "unknown")}</strong>
          ${diag.explanation ? `<span class="meta"> — ${h(diag.explanation)}</span>` : ""}
        </div>
        <div class="diag-bars">
          ${diagBarHtml("Tokens", formatCount(usage.total_tokens), formatCount(limits.max_tokens), tokenPct)}
          ${diagBarHtml("Iterations", fmt(usage.iterations_used || 0), fmt(limits.max_iterations || 0), iterPct)}
          <div class="diag-bar-row">
            <span class="diag-bar-label">API Calls</span>
            <span class="diag-bar-meta meta">${h(formatCount(usage.api_calls))}</span>
          </div>
        </div>
      </div>
    `;
  }

  return `
    <div class="section-block review-panel" id="review-override-panel">
      <div class="section-heading"><h2>Admin Review Override</h2></div>
      ${diagHtml}
      <form class="stack-form" id="review-override-form" onsubmit="applyReviewOverride(event, ${job.id})">
        <div class="field-grid">
          <label>Max Iterations<input name="max_iterations_per_task" type="number" min="1" value="${h(iterations)}"></label>
          <label>Max Tokens<input name="max_tokens_per_task" type="number" min="1" value="${h(tokens)}"></label>
        </div>
        <label class="full-field">Decision / Instruction<textarea name="instruction" placeholder="Tell the agent how to proceed after review"></textarea></label>
        <label class="check-row"><input name="requeue" type="checkbox" checked>Requeue immediately</label>
        <div class="actions">
          <button class="button primary" type="submit">Apply Override</button>
        </div>
      </form>
    </div>
  `;
}

function jobOverviewHtml(data) {
  const job = data.job;
  const metadata = job.metadata || {};
  const usage = data.usage || {};
  const finalResponse = metadata.final_response || "";
  const triggerId = data.trigger_email ? data.trigger_email.id : null;
  const contextEmails = (data.thread_messages || data.thread_emails || data.emails || []).filter(email => {
    const outgoing = email.context_type === "outbound_email" || email.direction === "outbound";
    return outgoing || email.id !== triggerId;
  });
  const trigger = data.trigger_email
    ? emailCard(data.trigger_email, {trigger: true, label: "Trigger Email"})
    : emptyState("No trigger email recorded.");
  const context = contextEmails.length
    ? contextEmails.map(email => emailCard(email)).join("")
    : emptyState("No additional thread context.");
  const metadataBlock = Object.keys(metadata).length
    ? `<pre>${h(JSON.stringify(metadata, null, 2))}</pre>`
    : emptyState("No metadata.");

  return `
    <div class="detail-card">
      <div class="summary-grid cols-3">
        <div class="summary-item"><span>ID</span><strong>#${h(job.id)}</strong></div>
        <div class="summary-item"><span>Thread</span><strong>${h(job.thread_id)}</strong></div>
        <div class="summary-item"><span>Attempts</span><strong>${h(job.attempts || 0)} / ${h(job.max_attempts || 0)}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(job.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(job.updated_at))}</strong></div>
        <div class="summary-item"><span>Completed</span><strong>${h(shortDate(job.completed_at) || "-")}</strong></div>
        <div class="summary-item"><span>Cost</span><strong id="job-usage-cost">${h(formatCost(usage.cost_total))}</strong></div>
        <div class="summary-item"><span>API Calls</span><strong id="job-usage-api-calls">${h(formatCount(usage.api_call_count))}</strong></div>
        <div class="summary-item"><span>Tokens</span><strong id="job-usage-tokens">${h(formatCount(usage.total_tokens))}</strong></div>
      </div>
      <pre id="job-last-error" data-error-val="${h(job.last_error || "")}" style="${job.last_error ? "" : "display:none"}">${h(job.last_error || "")}</pre>
    </div>

    ${jobReviewOverrideHtml(data)}

    ${finalResponse ? `
      <div class="section-block">
        <div class="section-heading"><h2>Agent Response</h2></div>
        <pre>${h(finalResponse)}</pre>
      </div>
    ` : ""}

    <div class="section-block">
      <div class="section-heading"><h2>Metadata</h2></div>
      ${metadataBlock}
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Add Instruction</h2></div>
      <form class="instruction-form" onsubmit="addInstruction(event, ${job.id})">
        <input name="instruction" placeholder="Instruction for next model call" required>
        <button class="button" type="submit">Add</button>
      </form>
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Trigger Email</h2></div>
      ${trigger}
    </div>

    <div class="section-block">
      <div class="section-heading">
        <h2>Thread Context</h2>
        <span class="meta">${h(contextEmails.length)} additional emails</span>
      </div>
      <div class="email-list">${context}</div>
    </div>
  `;
}

function jobLogHtml(logs) {
  const latestFirst = [...(logs || [])].reverse();
  const logList = latestFirst.length
    ? latestFirst.map(logCard).join("")
    : emptyState("No logs.");
  return `
    <div class="section-block">
      <div class="section-heading">
        <h2>Live Job Log</h2>
        <span class="meta">Latest first · ${h(latestFirst.length)} events · Updated ${h(shortTime())}</span>
      </div>
      <div class="log-list" id="job-log-list">${logList}</div>
    </div>
  `;
}

function jobDetailHtml(data) {
  return `
    <div class="segmented segmented-2 detail-tabs" role="tablist" aria-label="Job detail sections">
      <button class="segment ${activeJobTab === "overview" ? "active" : ""}" type="button" onclick="showJobTab('overview')">Overview</button>
      <button class="segment ${activeJobTab === "logs" ? "active" : ""}" type="button" onclick="showJobTab('logs')">Live Log</button>
    </div>
    <div class="job-tab-panel ${activeJobTab === "overview" ? "" : "hidden"}" id="job-overview-panel">
      ${jobOverviewHtml(data)}
    </div>
    <div class="job-tab-panel ${activeJobTab === "logs" ? "" : "hidden"}" id="job-log-panel">
      ${jobLogHtml(data.logs || [])}
    </div>
  `;
}

function showJobTab(tab) {
  activeJobTab = tab;
  document.querySelectorAll(".detail-tabs .segment").forEach(button => {
    button.classList.toggle("active", button.textContent.trim() === (tab === "logs" ? "Live Log" : "Overview"));
  });
  const overview = document.getElementById("job-overview-panel");
  const logs = document.getElementById("job-log-panel");
  if (overview && logs) {
    overview.classList.toggle("hidden", tab !== "overview");
    logs.classList.toggle("hidden", tab !== "logs");
  }
}

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function stopJobPoll() {
  if (jobPollTimer) clearInterval(jobPollTimer);
  jobPollTimer = null;
}

function startJobPoll(id) {
  stopJobPoll();
  if (TERMINAL_STATUSES.has(fmt(selectedJobId === id ?
    document.getElementById("job-status-pill")?.dataset?.status : ""))) {
    return;
  }
  jobPollTimer = setInterval(async () => {
    if (currentScreen !== "detail" || currentView !== "jobs" || selectedJobId !== id) {
      stopJobPoll();
      return;
    }
    try {
      await pollJobDetail(id);
    } catch {
      // silently ignore transient network errors
    }
  }, JOB_POLL_MS);
}

async function pollJobDetail(id) {
  const data = await api(`/api/jobs/${id}/poll?after_sequence=${lastPollSequence}`);
  const job = data.job;
  const usage = data.usage || {};
  const actions = data.actions || {};
  const newLogs = data.new_logs || [];

  // Append new logs to the top (latest-first display)
  if (newLogs.length) {
    lastPollSequence = newLogs[newLogs.length - 1].sequence;
    const logList = document.getElementById("job-log-list");
    if (logList) {
      const tmp = document.createElement("div");
      tmp.innerHTML = [...newLogs].reverse().map(logCard).join("");
      while (tmp.lastChild) logList.prepend(tmp.lastChild);
      const heading = logList.closest(".section-block")?.querySelector(".section-heading .meta");
      if (heading) {
        const total = logList.querySelectorAll(".log-row").length;
        heading.textContent = `Latest first · ${total} events · Updated ${shortTime()}`;
      }
    }
  }

  const statusPillEl = document.getElementById("job-status-pill");
  const prevStatus = statusPillEl?.dataset?.status ?? "";
  const newStatus = job.status;

  // If a needs_review job was approved (status changed), do a full reload so the
  // review override panel and diagnostics reflect the new state correctly.
  if (prevStatus === "needs_review" && newStatus !== "needs_review") {
    await loadJobDetail(id, {preserveTab: true});
    return;
  }

  // Patch status pill
  if (statusPillEl && prevStatus !== newStatus) {
    statusPillEl.className = `status ${statusClass(newStatus)}`;
    statusPillEl.textContent = newStatus;
    statusPillEl.dataset.status = newStatus;
  }

  // Patch last_error display
  const lastErrorEl = document.getElementById("job-last-error");
  if (lastErrorEl) {
    const newError = job.last_error || "";
    if (lastErrorEl.dataset.errorVal !== newError) {
      lastErrorEl.dataset.errorVal = newError;
      lastErrorEl.textContent = newError;
      lastErrorEl.style.display = newError ? "" : "none";
    }
  }

  // Patch usage chips
  const costEl = document.getElementById("job-usage-cost");
  if (costEl) costEl.textContent = formatCost(usage.cost_total);
  const apiEl = document.getElementById("job-usage-api-calls");
  if (apiEl) apiEl.textContent = formatCount(usage.api_call_count);
  const tokensEl = document.getElementById("job-usage-tokens");
  if (tokensEl) tokensEl.textContent = formatCount(usage.total_tokens);
  const titleCostEl = document.getElementById("job-title-cost");
  if (titleCostEl) titleCostEl.textContent = formatCost(usage.cost_total);

  // Patch action buttons if status changed
  if (prevStatus !== newStatus) {
    const actionButtons = [
      actions.can_review_override ? `<button class="button" onclick="document.getElementById('review-override-panel')?.scrollIntoView({behavior: 'smooth', block: 'start'})">Review Override</button>` : "",
      actions.can_requeue ? `<button class="button" onclick="requeue(${job.id})">Requeue</button>` : "",
      actions.can_cancel ? `<button class="button danger" onclick="cancelJob(${job.id})">Cancel</button>` : "",
      `<button class="icon-button danger-icon" type="button" onclick="eraseJob(${job.id})" title="Erase job data" aria-label="Erase job data">${trashIcon}</button>`
    ].join("");
    setDetailActions(actionButtons);
  }

  // Stop polling and refresh job list on terminal status
  if (TERMINAL_STATUSES.has(newStatus)) {
    stopJobPoll();
    await loadJobs({preservePage: true});
  }
}

async function loadJobDetail(id, options = {}) {
  currentView = "jobs";
  selectedJobId = id;
  lastPollSequence = 0;
  setActiveViewButton("jobs");
  updateJobUrl(id);
  if (!options.preserveTab) activeJobTab = "overview";
  const data = await api(`/api/jobs/${id}`);
  const job = data.job;
  const usage = data.usage || {};
  const actions = data.actions || {};
  // Seed the poll sequence from the full log list
  const logs = data.logs || [];
  if (logs.length) lastPollSequence = logs[logs.length - 1].sequence;
  const actionButtons = [
    actions.can_review_override ? `<button class="button" onclick="document.getElementById('review-override-panel')?.scrollIntoView({behavior: 'smooth', block: 'start'})">Review Override</button>` : "",
    actions.can_requeue ? `<button class="button" onclick="requeue(${job.id})">Requeue</button>` : "",
    actions.can_cancel ? `<button class="button danger" onclick="cancelJob(${job.id})">Cancel</button>` : "",
    `<button class="icon-button danger-icon" type="button" onclick="eraseJob(${job.id})" title="Erase job data" aria-label="Erase job data">${trashIcon}</button>`
  ].join("");
  setScreen("detail");
  setDetailTitle(`
    <div>
      <span id="job-status-pill" class="status ${statusClass(job.status)}" data-status="${h(job.status)}">${h(job.status)}</span>
      <strong>${h(job.task_summary || "Untitled job")}</strong>
      <span class="pill" id="job-title-cost">${h(formatCost(usage.cost_total))}</span>
    </div>
  `);
  setDetailActions(actionButtons);
  setDetail(jobDetailHtml(data));
  // Don't start polling for terminal jobs
  if (!TERMINAL_STATUSES.has(job.status)) startJobPoll(id);
  if (!options.preserveTab && !options.silent) await loadJobs({preservePage: true});
}

async function loadReminderDetail(id) {
  stopJobPoll();
  currentView = "reminders";
  selectedReminderId = id;
  const response = await fetch(`/api/reminders/${id}`);
  if (response.status === 404) {
    selectedReminderId = null;
    await setView("reminders", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const reminder = data.reminder;
  setScreen("detail");
  setDetailTitle(`
    <div>
      ${statusPill(reminder.status)}
      <strong>${h(reminder.title || `Reminder #${reminder.id}`)}</strong>
    </div>
  `);
  setDetailActions(`<button class="button danger" type="button" onclick="deleteReminder(${reminder.id})">Delete Reminder</button>`);
  setDetail(reminderDetailHtml(data));
  await loadReminders({preservePage: true});
}

async function loadProjectDetail(id) {
  stopJobPoll();
  currentView = "projects";
  selectedProjectId = id;
  const response = await fetch(`/api/projects/${id}`);
  if (response.status === 404) {
    selectedProjectId = null;
    await setView("projects", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const project = data.project;
  setScreen("detail");
  setDetailTitle(`
    <div>
      ${statusPill(project.status)}
      <strong>${h(project.title || `Project #${project.id}`)}</strong>
    </div>
  `);
  setDetailActions(`<button class="button danger" type="button" onclick="deleteProject(${project.id})">Delete Project</button>`);
  setDetail(projectDetailHtml(data));
  await loadProjects({preservePage: true});
}

async function loadMemoryDetail(id) {
  stopJobPoll();
  currentView = "memories";
  selectedMemoryId = id;
  const response = await fetch(`/api/memories/${id}`);
  if (response.status === 404) {
    selectedMemoryId = null;
    await setView("memories", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const memory = data.memory;
  const sourceJobBlock = data.source_job
    ? `<button class="button primary" type="button" onclick="openJob(${data.source_job.id})">View Source Job #${h(data.source_job.id)}</button>`
    : "";
  const preview = truncateText(memory.content, 80);
  setScreen("detail");
  setDetailTitle(`
    <div>
      <strong>${h(preview)}</strong>
    </div>
  `);
  setDetailActions(sourceJobBlock);
  setDetail(memoryDetailHtml(data));
  await loadMemories({preservePage: true});
}

async function loadNoteDetail(id) {
  stopJobPoll();
  currentView = "notes";
  selectedNoteId = id;
  const response = await fetch(`/api/notes/${id}`);
  if (response.status === 404) {
    selectedNoteId = null;
    await setView("notes", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const note = data.note;
  const sourceJobBlock = data.source_job
    ? `<button class="button primary" type="button" onclick="openJob(${data.source_job.id})">View Source Job #${h(data.source_job.id)}</button>`
    : "";
  const preview = truncateText(note.title || note.content, 80);
  setScreen("detail");
  setDetailTitle(`
    <div>
      <strong>${h(preview)}</strong>
    </div>
  `);
  setDetailActions(sourceJobBlock);
  setDetail(noteDetailHtml(data));
  await loadNotes({preservePage: true});
}

async function loadContactDetail(id) {
  stopJobPoll();
  currentView = "contacts";
  selectedContactId = id;
  const response = await fetch(`/api/contacts/${id}`);
  if (response.status === 404) {
    selectedContactId = null;
    await setView("contacts", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const contact = data.contact;
  setScreen("detail");
  setDetailTitle(`
    <div>
      <strong>${h(contactName(contact))}</strong>
    </div>
  `);
  setDetailActions("");
  setDetail(contactDetailHtml(data));
  await loadContacts({preservePage: true});
}

function createJobFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      <label class="full-field">Subject<input id="subject" placeholder="Subject" required></label>
      <label class="full-field">Task<textarea id="body" placeholder="Task" required></textarea></label>
      <div class="actions">
        <button class="button primary" type="submit">Create Job</button>
      </div>
    </form>
  `;
}

function createMemoryFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      <label class="full-field">Content<textarea name="content" placeholder="Memory content" required></textarea></label>
      <label class="full-field">Tags<input name="tags" placeholder="Tags, comma-separated"></label>
      <div class="field-grid">
        <label>Scope<input name="scope" value="global"></label>
        <label>Kind<input name="kind" value="project_context"></label>
        <label>Importance<input name="importance" type="number" min="1" max="5" value="3"></label>
        <label>Confidence<input name="confidence" type="number" min="0" max="1" step="0.05" value="0.7"></label>
      </div>
      <label class="full-field">Expires at<input name="expires_at" placeholder="Expires at"></label>
      <label class="check-row"><input name="pinned" type="checkbox">Pinned</label>
      <div class="actions">
        <button class="button primary" type="submit">Create Memory</button>
      </div>
    </form>
  `;
}

function createReminderFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      ${reminderFormHtml({priority: 0})}
      <div class="actions">
        <button class="button primary" type="submit">Create Reminder</button>
      </div>
    </form>
  `;
}

function createNoteFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      <label class="full-field">Title<input name="title" placeholder="Note title"></label>
      <label class="full-field">Content<textarea name="content" placeholder="Note content" required></textarea></label>
      <label class="full-field">Tags<input name="tags" placeholder="Tags, comma-separated"></label>
      <div class="actions">
        <button class="button primary" type="submit">Create Note</button>
      </div>
    </form>
  `;
}

function createContactFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      <div class="field-grid">
        <label>First name<input name="first_name" placeholder="First name"></label>
        <label>Last name<input name="last_name" placeholder="Last name"></label>
        <label>Email address<input name="email_address" placeholder="Email address"></label>
        <label>Company<input name="company" placeholder="Company"></label>
        <label>Title<input name="title" placeholder="Title"></label>
      </div>
      <label class="full-field">Notes<textarea name="notes" placeholder="Notes"></textarea></label>
      <div class="actions">
        <button class="button primary" type="submit">Create Contact</button>
      </div>
    </form>
  `;
}

function showCreateForm() {
  if (currentView === "projects") return;
  stopJobPoll();
  setScreen("form");
  const label = viewLabel(currentView);
  document.getElementById("form-title").innerHTML = `<strong>New ${h(label)}</strong>`;
  const content = document.getElementById("form-content");
  if (currentView === "reminders") content.innerHTML = createReminderFormHtml();
  else if (currentView === "memories") content.innerHTML = createMemoryFormHtml();
  else if (currentView === "notes") content.innerHTML = createNoteFormHtml();
  else if (currentView === "contacts") content.innerHTML = createContactFormHtml();
  else if (currentView === "entities") content.innerHTML = createEntityFormHtml();
  else content.innerHTML = createJobFormHtml();
  document.getElementById("create-form").onsubmit = submitCreateForm;
}

async function submitCreateForm(event) {
  event.preventDefault();
  if (currentView === "reminders") {
    const data = await api("/api/reminders", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(reminderPayloadFromForm(event.target))
    });
    selectedReminderId = data.reminder.id;
    await setView("reminders", {resetPage: true});
  } else if (currentView === "memories") {
    let payload;
    try {
      payload = memoryPayloadFromForm(event.target);
    } catch (error) {
      alert(`Metadata must be valid JSON: ${error.message}`);
      return;
    }
    const data = await api("/api/memories", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    selectedMemoryId = data.memory.id;
    await setView("memories", {resetPage: true});
  } else if (currentView === "notes") {
    let payload;
    try {
      payload = notePayloadFromForm(event.target);
    } catch (error) {
      alert(`Metadata must be valid JSON: ${error.message}`);
      return;
    }
    const data = await api("/api/notes", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    selectedNoteId = data.note.id;
    await setView("notes", {resetPage: true});
  } else if (currentView === "contacts") {
    const data = await api("/api/contacts", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(contactPayloadFromForm(event.target))
    });
    selectedContactId = data.contact.id;
    await setView("contacts", {resetPage: true});
  } else if (currentView === "entities") {
    const form = event.target;
    const data = await api("/api/entities", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: form.elements.name.value.trim(),
        description: form.elements.description.value.trim() || null
      })
    });
    selectedEntityId = data.entity.id;
    await setView("entities", {resetPage: true});
  } else {
    const subject = document.getElementById("subject").value;
    const body = document.getElementById("body").value;
    const data = await api("/api/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({subject, body})
    });
    selectedJobId = data.job.id;
    await setView("jobs", {resetPage: true});
  }
}

async function requeue(id) {
  await api(`/api/jobs/${id}/requeue`, {method: "POST"});
  await loadJobDetail(id, {preserveTab: true});
}

async function cancelJob(id) {
  if (!confirm(`Cancel job #${id}?`)) return;
  await api(`/api/jobs/${id}/cancel`, {method: "POST"});
  await loadJobDetail(id, {preserveTab: true});
}

async function eraseJob(id) {
  if (!confirm(`Erase job #${id} and its associated database records?`)) return;
  const data = await api(`/api/jobs/${id}`, {method: "DELETE"});
  selectedJobId = null;
  const deleted = data.erased.deleted || {};
  const parts = Object.entries(deleted).map(([name, count]) => `${name} ${count}`);
  await setView("jobs", {resetPage: false});
  const empty = document.getElementById("table-empty");
  if (empty && !empty.classList.contains("hidden")) {
    empty.textContent = `Erased job #${id}. ${parts.join(", ") || "No dependent rows deleted."}`;
  }
}

async function addInstruction(event, id) {
  event.preventDefault();
  const instruction = new FormData(event.target).get("instruction");
  await api(`/api/jobs/${id}/instructions`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({instruction})
  });
  event.target.reset();
  await loadJobDetail(id, {preserveTab: true});
}

async function applyReviewOverride(event, id) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const iterations = fmt(data.get("max_iterations_per_task")).trim();
  const tokens = fmt(data.get("max_tokens_per_task")).trim();
  const payload = {
    instruction: fmt(data.get("instruction")).trim() || null,
    max_iterations_per_task: iterations ? Number(iterations) : null,
    max_tokens_per_task: tokens ? Number(tokens) : null,
    requeue: form.elements.requeue.checked
  };
  await api(`/api/jobs/${id}/review-override`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  await loadJobDetail(id, {preserveTab: true});
}

async function saveReminder(event, id) {
  event.preventDefault();
  await api(`/api/reminders/${id}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(reminderPayloadFromForm(event.target))
  });
  await loadReminderDetail(id);
}

async function deleteReminder(id) {
  if (!confirm(`Delete reminder #${id}?`)) return;
  await api(`/api/reminders/${id}`, {method: "DELETE"});
  selectedReminderId = null;
  await setView("reminders", {resetPage: false});
}

async function deleteProject(id) {
  if (!confirm(`Delete project #${id}? Active linked project jobs will be cancelled.`)) return;
  await api(`/api/projects/${id}`, {method: "DELETE"});
  selectedProjectId = null;
  await setView("projects", {resetPage: false});
}

async function saveMemory(event, id) {
  event.preventDefault();
  let payload;
  try {
    payload = memoryPayloadFromForm(event.target);
  } catch (error) {
    alert(`Metadata must be valid JSON: ${error.message}`);
    return;
  }
  await api(`/api/memories/${id}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  await loadMemoryDetail(id);
}

async function deleteMemory(id) {
  if (!confirm(`Delete memory #${id}?`)) return;
  await api(`/api/memories/${id}`, {method: "DELETE"});
  selectedMemoryId = null;
  await setView("memories", {resetPage: false});
}

async function saveNote(event, id) {
  event.preventDefault();
  let payload;
  try {
    payload = notePayloadFromForm(event.target);
  } catch (error) {
    alert(`Metadata must be valid JSON: ${error.message}`);
    return;
  }
  await api(`/api/notes/${id}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  await loadNoteDetail(id);
}

async function deleteNote(id) {
  if (!confirm(`Delete note #${id}?`)) return;
  await api(`/api/notes/${id}`, {method: "DELETE"});
  selectedNoteId = null;
  await setView("notes", {resetPage: false});
}

async function saveContact(event, id) {
  event.preventDefault();
  await api(`/api/contacts/${id}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(contactPayloadFromForm(event.target))
  });
  await loadContactDetail(id);
}

async function deleteContact(id) {
  if (!confirm(`Delete contact #${id}?`)) return;
  await api(`/api/contacts/${id}`, {method: "DELETE"});
  selectedContactId = null;
  await setView("contacts", {resetPage: false});
}

async function openJob(id) {
  await loadJobDetail(id);
}

function scheduleMemoryLoad() {
  clearTimeout(memorySearchTimer);
  memorySearchTimer = setTimeout(() => {
    pageByView.memories = 1;
    loadMemories().catch(showTableError);
  }, 250);
}

function scheduleNoteLoad() {
  clearTimeout(noteSearchTimer);
  noteSearchTimer = setTimeout(() => {
    pageByView.notes = 1;
    loadNotes().catch(showTableError);
  }, 250);
}

function scheduleContactLoad() {
  clearTimeout(contactSearchTimer);
  contactSearchTimer = setTimeout(() => {
    pageByView.contacts = 1;
    loadContacts().catch(showTableError);
  }, 250);
}

// --- Entity management ---

function entityObjectCard(obj) {
  return `
    <details class="event-row">
      <summary>
        <span class="pill">${h(obj.object_type)}</span>
        <strong>#${h(obj.object_id)}</strong>
        <span class="meta">Linked by ${h(obj.linked_by || "unknown")} · ${h(shortDate(obj.created_at))}</span>
      </summary>
      <pre>${h(JSON.stringify(obj, null, 2))}</pre>
    </details>
  `;
}

function entityDetailHtml(data) {
  const entity = data.entity;
  const objects = data.objects || [];
  const objectCounts = entity.object_counts || {};
  const countChips = Object.entries(objectCounts)
    .map(([type, count]) => `<span class="pill">${h(type)}: ${h(count)}</span>`)
    .join("") || `<span class="meta">No linked objects</span>`;

  // Group objects by type
  const grouped = {};
  for (const obj of objects) {
    const type = obj.object_type || "unknown";
    if (!grouped[type]) grouped[type] = [];
    grouped[type].push(obj);
  }
  const objectSections = Object.entries(grouped).map(([type, items]) => `
    <div class="section-block">
      <div class="section-heading">
        <h2>${h(type)}</h2>
        <span class="meta">${h(items.length)} linked</span>
      </div>
      <div class="event-list">${items.map(entityObjectCard).join("")}</div>
    </div>
  `).join("") || "";

  // Build merge select from all entities (loaded from list)
  const allEntities = rowsByView.entities || [];
  const mergeOptions = allEntities
    .filter(e => e.id !== entity.id)
    .map(e => `<option value="${h(e.id)}">${h(e.name)} (#${e.id})</option>`)
    .join("");

  return `
    <div class="detail-card">
      ${entity.description ? `<pre>${h(entity.description)}</pre>` : ""}
      <div class="summary-grid cols-3">
        <div class="summary-item"><span>ID</span><strong>#${h(entity.id)}</strong></div>
        <div class="summary-item"><span>Created By</span><strong>${h(entity.created_by || "-")}</strong></div>
        <div class="summary-item"><span>Total Objects</span><strong>${h(entity.total_objects || 0)}</strong></div>
        <div class="summary-item"><span>Created</span><strong>${h(shortDate(entity.created_at))}</strong></div>
        <div class="summary-item"><span>Updated</span><strong>${h(shortDate(entity.updated_at))}</strong></div>
      </div>
      <div style="margin-top:.75rem">${countChips}</div>
    </div>

    <div class="section-block">
      <div class="section-heading"><h2>Edit Entity</h2></div>
      <form class="stack-form" onsubmit="saveEntity(event, ${entity.id})">
        <label class="full-field">Name<input name="name" value="${h(entity.name)}" required></label>
        <label class="full-field">Description<textarea name="description">${h(entity.description || "")}</textarea></label>
        <div class="actions">
          <button class="button primary" type="submit">Save Entity</button>
        </div>
      </form>
    </div>

    ${mergeOptions ? `
    <div class="section-block">
      <div class="section-heading"><h2>Merge Into Another Entity</h2></div>
      <form class="stack-form" onsubmit="mergeEntity(event, ${entity.id})">
        <label class="full-field">Target Entity
          <select name="target_entity_id" required>
            <option value="">Select target…</option>
            ${mergeOptions}
          </select>
        </label>
        <p class="meta">All linked objects from this entity will be moved to the target. This entity will be deleted.</p>
        <div class="actions">
          <button class="button danger" type="submit">Merge &amp; Delete This Entity</button>
        </div>
      </form>
    </div>
    ` : ""}

    ${objectSections || emptyState("No linked objects.")}
  `;
}

async function loadEntityDetail(id) {
  stopJobPoll();
  currentView = "entities";
  selectedEntityId = id;
  setActiveViewButton("entities");
  const response = await fetch(`/api/entities/${id}`);
  if (response.status === 404) {
    selectedEntityId = null;
    await setView("entities", {resetPage: false});
    return;
  }
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  const entity = data.entity;
  setScreen("detail");
  setDetailTitle(`<div><strong>${h(entity.name)}</strong><span class="pill">#${h(entity.id)}</span></div>`);
  setDetailActions(`<button class="button danger" type="button" onclick="deleteEntity(${entity.id})">Delete Entity</button>`);
  setDetail(entityDetailHtml(data));
  await loadEntities({preservePage: true});
}

async function saveEntity(event, id) {
  event.preventDefault();
  const form = event.target;
  await api(`/api/entities/${id}`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      name: form.elements.name.value.trim(),
      description: form.elements.description.value.trim() || null
    })
  });
  await loadEntityDetail(id);
}

async function deleteEntity(id) {
  // Fetch delete preview first
  const preview = await api(`/api/entities/${id}/delete-preview`);
  const warning = preview.warning || `This will unlink ${preview.total_unlinks || 0} objects and delete ${preview.total_deletions || 0} records.`;
  if (!confirm(`Delete entity "${preview.entity?.name || id}"?\n\n${warning}`)) return;
  await api(`/api/entities/${id}`, {method: "DELETE"});
  selectedEntityId = null;
  await setView("entities", {resetPage: false});
}

async function mergeEntity(event, id) {
  event.preventDefault();
  const form = event.target;
  const targetId = Number(form.elements.target_entity_id.value);
  if (!targetId) return;
  if (!confirm(`Merge entity #${id} into entity #${targetId}? This entity will be deleted.`)) return;
  await api(`/api/entities/${id}/merge`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({target_entity_id: targetId})
  });
  selectedEntityId = targetId;
  await loadEntityDetail(targetId);
}

function createEntityFormHtml() {
  return `
    <form id="create-form" class="stack-form">
      <label class="full-field">Name<input name="name" placeholder="Entity name (person, project, company…)" required></label>
      <label class="full-field">Description<textarea name="description" placeholder="Optional description"></textarea></label>
      <div class="actions">
        <button class="button primary" type="submit">Create Entity</button>
      </div>
    </form>
  `;
}

function bindEvents() {
  document.getElementById("jobs-view-button").onclick = () => setView("jobs", {resetPage: false});
  document.getElementById("reminders-view-button").onclick = () => setView("reminders", {resetPage: false});
  document.getElementById("projects-view-button").onclick = () => setView("projects", {resetPage: false});
  document.getElementById("memories-view-button").onclick = () => setView("memories", {resetPage: false});
  document.getElementById("notes-view-button").onclick = () => setView("notes", {resetPage: false});
  document.getElementById("contacts-view-button").onclick = () => setView("contacts", {resetPage: false});
  document.getElementById("entities-view-button").onclick = () => setView("entities", {resetPage: false});
  document.getElementById("create-record").onclick = showCreateForm;
  document.getElementById("cancel-create").onclick = () => setView(currentView, {resetPage: false});
  document.getElementById("back-to-list").onclick = () => setView(currentView, {resetPage: false});
  document.getElementById("refresh").onclick = async () => {
    try {
      await loadStats();
      await loadCurrentList({preservePage: true});
    } catch (error) {
      showTableError(error);
    }
  };
  document.getElementById("page-prev").onclick = () => {
    pageByView[currentView] = Math.max(1, pageByView[currentView] - 1);
    renderTable({resetScroll: true});
  };
  document.getElementById("page-next").onclick = () => {
    pageByView[currentView] += 1;
    renderTable({resetScroll: true});
  };
  document.getElementById("status-filter").onchange = () => {
    pageByView.jobs = 1;
    loadJobs().catch(showTableError);
  };
  document.getElementById("reminder-status-filter").onchange = () => {
    pageByView.reminders = 1;
    loadReminders().catch(showTableError);
  };
  document.getElementById("project-status-filter").onchange = () => {
    pageByView.projects = 1;
    loadProjects().catch(showTableError);
  };
  for (const id of ["memory-tag", "memory-scope", "memory-kind", "memory-pinned", "memory-include-expired"]) {
    document.getElementById(id).onchange = () => {
      pageByView.memories = 1;
      loadMemories().catch(showTableError);
    };
  }
  document.getElementById("note-tag").onchange = () => {
    pageByView.notes = 1;
    loadNotes().catch(showTableError);
  };
  document.getElementById("memory-query").oninput = scheduleMemoryLoad;
  document.getElementById("note-query").oninput = scheduleNoteLoad;
  document.getElementById("contact-query").oninput = scheduleContactLoad;
}

async function refreshVisibleList() {
  if (currentScreen !== "list") return;
  await loadStats();
  await loadCurrentList({preservePage: true});
}

async function boot() {
  bindEvents();
  setActiveViewButton("jobs");
  const initialJobId = jobIdFromUrl();
  try {
    await loadStats();
    await loadJobs();
  } catch (error) {
    showTableError(error);
  }
  if (initialJobId) {
    try {
      await loadJobDetail(initialJobId);
    } catch (error) {
      updateJobUrl(null);
      showTableError(error);
    }
  }
  setInterval(() => {
    refreshVisibleList().catch(() => {});
  }, LIST_REFRESH_MS);
}

boot();
