let currentFolder = ".";
let currentPath = null;
let currentMtimeNs = null;
let currentContent = "";
let dirty = false;
let editorMode = "write";

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
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

function bytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function markdownInline(value) {
  return h(value)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
}

function renderMarkdown(source) {
  const lines = fmt(source).split(/\r?\n/);
  const html = [];
  let inList = false;
  let inCode = false;
  let codeLines = [];

  function closeList() {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  }

  function closeCode() {
    if (inCode) {
      html.push(`<pre><code>${h(codeLines.join("\n"))}</code></pre>`);
      codeLines = [];
      inCode = false;
    }
  }

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) closeCode();
      else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (/^###\s+/.test(line)) {
      closeList();
      html.push(`<h3>${markdownInline(line.replace(/^###\s+/, ""))}</h3>`);
    } else if (/^##\s+/.test(line)) {
      closeList();
      html.push(`<h2>${markdownInline(line.replace(/^##\s+/, ""))}</h2>`);
    } else if (/^#\s+/.test(line)) {
      closeList();
      html.push(`<h1>${markdownInline(line.replace(/^#\s+/, ""))}</h1>`);
    } else if (/^[-*]\s+/.test(line)) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${markdownInline(line.replace(/^[-*]\s+/, ""))}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      html.push(`<p>${markdownInline(line)}</p>`);
    }
  }
  closeCode();
  closeList();
  return html.join("");
}

function setDirty(value) {
  dirty = value;
  const save = document.getElementById("save-file");
  save.disabled = !currentPath || !dirty;
  updateContextChips();
  updateMeta();
}

function updateMeta() {
  const meta = document.getElementById("editor-file-meta");
  if (!currentPath) {
    meta.textContent = "Select a Markdown or text file.";
    return;
  }
  meta.textContent = `${dirty ? "Unsaved changes · " : ""}${currentPath}`;
}

function updateContextChips() {
  const target = document.getElementById("workspace-context");
  const chips = [];
  chips.push(`<span class="stat">Folder ${h(currentFolder)}</span>`);
  if (currentPath) chips.push(`<span class="stat">File ${h(currentPath)}</span>`);
  if (dirty) chips.push('<span class="stat">Unsaved</span>');
  target.innerHTML = chips.join("");
}

function setEditorMode(mode) {
  editorMode = mode;
  for (const id of ["write", "preview", "source"]) {
    document.getElementById(`mode-${id}`).classList.toggle("active", id === mode);
  }
  const surface = document.getElementById("editor-surface");
  surface.classList.toggle("preview-only", mode === "preview");
  surface.classList.toggle("source-only", mode === "source");
}

function renderFileRows(entries) {
  const target = document.getElementById("file-list");
  const rows = [];
  if (currentFolder !== ".") {
    const parent = currentFolder.split("/").slice(0, -1).join("/") || ".";
    rows.push(`<button class="file-row" type="button" data-folder="${h(parent)}"><span class="file-type dir" aria-hidden="true"></span><span class="file-name">Parent folder</span><span></span></button>`);
  }
  for (const entry of entries) {
    const iconClass = entry.is_dir ? "dir" : "file";
    const attr = entry.is_dir ? `data-folder="${h(entry.relative_path)}"` : `data-file="${h(entry.relative_path)}"`;
    const active = !entry.is_dir && entry.relative_path === currentPath ? "active" : "";
    const size = entry.is_dir ? "" : bytes(entry.size_bytes);
    rows.push(`
      <button class="file-row ${active}" type="button" ${attr}>
        <span class="file-type ${iconClass}" aria-hidden="true"></span>
        <span class="file-name">${h(entry.name)}</span>
        <span class="meta">${h(size)}</span>
      </button>
    `);
  }
  target.innerHTML = rows.length ? rows.join("") : '<div class="empty-state">No files.</div>';
  for (const button of target.querySelectorAll("[data-folder]")) {
    button.onclick = () => loadTree(button.dataset.folder);
  }
  for (const button of target.querySelectorAll("[data-file]")) {
    button.onclick = () => openFile(button.dataset.file);
  }
}

async function loadTree(path = currentFolder) {
  currentFolder = path || ".";
  document.getElementById("current-folder").textContent = currentFolder;
  const data = await api(`/api/workspace/tree?path=${encodeURIComponent(currentFolder)}&max_entries=500`);
  const entries = (data.entries || []).sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  renderFileRows(entries);
  updateContextChips();
}

async function searchFiles(query) {
  if (!query.trim()) {
    await loadTree(currentFolder);
    return;
  }
  const data = await api(`/api/workspace/search?q=${encodeURIComponent(query)}&max_results=100`);
  const entries = (data.matches || []).map(path => ({
    name: path.split("/").pop(),
    relative_path: path,
    is_dir: false,
    size_bytes: 0
  }));
  renderFileRows(entries);
}

async function openFile(path) {
  if (dirty && !confirm("Discard unsaved changes?")) return;
  const data = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`);
  currentPath = data.relative_path;
  currentMtimeNs = data.mtime_ns;
  currentContent = data.content || "";
  document.getElementById("editor-file-name").textContent = currentPath.split("/").pop() || currentPath;
  document.getElementById("editor-text").disabled = false;
  document.getElementById("editor-text").value = currentContent;
  document.getElementById("editor-preview").innerHTML = renderMarkdown(currentContent);
  setDirty(false);
  await loadTree(currentFolder);
}

async function saveFile() {
  if (!currentPath) return;
  const content = document.getElementById("editor-text").value;
  const data = await api("/api/workspace/file", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      path: currentPath,
      content,
      expected_mtime_ns: currentMtimeNs
    })
  });
  currentMtimeNs = data.file.mtime_ns;
  currentContent = content;
  setDirty(false);
  await loadTree(currentFolder);
}

function addChatMessage(role, text) {
  const feed = document.getElementById("chat-feed");
  if (feed.querySelector(".meta")) feed.innerHTML = "";
  const item = document.createElement("div");
  item.className = "chat-message";
  item.innerHTML = `<strong>${h(role)}</strong><p>${h(text)}</p>`;
  feed.appendChild(item);
  feed.scrollTop = feed.scrollHeight;
}

function chatBody(message) {
  const parts = [message.trim()];
  if (document.getElementById("include-active-file").checked && currentPath) {
    parts.push(`\nActive workspace file: ${currentPath}`);
  }
  if (document.getElementById("include-file-content").checked && currentPath) {
    const content = document.getElementById("editor-text").value;
    if (content.length <= 25000) {
      parts.push(`\nActive file content:\n\n\`\`\`markdown\n${content}\n\`\`\``);
    } else {
      parts.push("\nThe active file is large. Use file_read on the active path instead of relying on pasted content.");
    }
  }
  return parts.join("\n");
}

async function pollJob(id) {
  document.getElementById("chat-status").textContent = `Job #${id}`;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const data = await api(`/api/jobs/${id}`);
    const status = data.job.status;
    const finalResponse = (data.job.metadata || {}).final_response || "";
    document.getElementById("chat-status").textContent = `Job #${id} · ${status}`;
    if (finalResponse) {
      addChatMessage("Agent", finalResponse);
      return;
    }
    if (["completed", "failed", "cancelled", "needs_review"].includes(status)) {
      addChatMessage("Agent", data.job.last_error || `Job ${status}.`);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
}

function bindEvents() {
  document.getElementById("refresh-tree").onclick = () => loadTree(currentFolder);
  document.getElementById("file-search").oninput = event => searchFiles(event.target.value);
  document.getElementById("editor-text").oninput = event => {
    document.getElementById("editor-preview").innerHTML = renderMarkdown(event.target.value);
    setDirty(event.target.value !== currentContent);
  };
  document.getElementById("save-file").onclick = saveFile;
  document.getElementById("mode-write").onclick = () => setEditorMode("write");
  document.getElementById("mode-preview").onclick = () => setEditorMode("preview");
  document.getElementById("mode-source").onclick = () => setEditorMode("source");
  document.getElementById("chat-form").onsubmit = async event => {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const message = input.value;
    input.value = "";
    addChatMessage("You", message);
    const data = await api("/api/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        subject: currentPath ? `Workspace: ${currentPath}` : "Workspace chat",
        body: chatBody(message)
      })
    });
    pollJob(data.job.id);
  };
}

async function boot() {
  bindEvents();
  setEditorMode("write");
  updateContextChips();
  try {
    await loadTree(".");
  } catch (error) {
    document.getElementById("file-list").innerHTML = `<div class="empty-state">${h(error.message)}</div>`;
  }
}

boot();
