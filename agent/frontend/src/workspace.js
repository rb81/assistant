import { commandsCtx, defaultValueCtx, Editor, editorViewCtx, rootCtx, serializerCtx } from "@milkdown/kit/core";
import { commonmark, emphasisSchema, inlineCodeSchema, strongSchema, toggleEmphasisCommand, toggleInlineCodeCommand, toggleStrongCommand } from "@milkdown/kit/preset/commonmark";
import {
  addColAfterCommand,
  addColBeforeCommand,
  addRowAfterCommand,
  addRowBeforeCommand,
  gfm,
  insertTableCommand,
  strikethroughSchema,
  toggleStrikethroughCommand
} from "@milkdown/kit/preset/gfm";
import { history } from "@milkdown/kit/plugin/history";
import { listener, listenerCtx } from "@milkdown/kit/plugin/listener";
import { deleteColumn as deleteMarkdownColumn, deleteRow as deleteMarkdownRow, isInTable } from "@milkdown/kit/prose/tables";
import { HighlightStyle, StreamLanguage, defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import { EditorView, drawSelection, highlightActiveLine, highlightActiveLineGutter, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import "./workspace.css";

// Import Material Design icons
import folderSvg from "material-icon-theme/icons/folder.svg?raw";
import folderOpenSvg from "material-icon-theme/icons/folder-open.svg?raw";
import markdownSvg from "material-icon-theme/icons/markdown.svg?raw";
import javascriptSvg from "material-icon-theme/icons/javascript.svg?raw";
import typescriptSvg from "material-icon-theme/icons/typescript.svg?raw";
import pythonSvg from "material-icon-theme/icons/python.svg?raw";
import jsonSvg from "material-icon-theme/icons/json.svg?raw";
import yamlSvg from "material-icon-theme/icons/yaml.svg?raw";
import cssSvg from "material-icon-theme/icons/css.svg?raw";
import htmlSvg from "material-icon-theme/icons/html.svg?raw";
import phpSvg from "material-icon-theme/icons/php.svg?raw";
import imageSvg from "material-icon-theme/icons/image.svg?raw";
import txtSvg from "material-icon-theme/icons/document.svg?raw";
import pdfSvg from "material-icon-theme/icons/pdf.svg?raw";
import wordSvg from "material-icon-theme/icons/word.svg?raw";
import powerpointSvg from "material-icon-theme/icons/powerpoint.svg?raw";
import fileSvg from "material-icon-theme/icons/file.svg?raw";

let currentFolder = ".";
let currentPath = null;
let untitledPath = null;
let currentMtimeNs = null;
let currentContent = "";
let activeMarkdown = "";
let dirty = false;
let editorMode = "write";
let milkdownEditor = null;
let codeEditor = null;
let editorGeneration = 0;
let unsupportedPath = null;
let unsupportedEntry = null;
let showHidden = false;
let searchDebounce = null;
let draftTimer = null;
let draftInFlight = false;
let pendingDraftSave = false;
let lastDraftContent = "";
let sidebarCollapsed = false;
let chatCollapsed = false;
let csvRows = [[""]];
let csvDelimiter = ",";
let activeCsvCell = null;
let historyStack = [];
let redoStack = [];
let applyingHistory = false;
let contextControlsWereDisabled = true;
let selectedWorkspaceJobId = null;
let workspaceJobs = [];
let selectedWorkspaceJob = null;
let workspaceJobPollTimer = null;
let workspaceChatTab = "jobs";
let openTabs = [];
let activeTabId = null;
let tabSequence = 0;
let workspaceVersionPollTimer = null;
let workspaceVersionInFlight = false;
let lastWorkspaceVersion = null;
let externalCheckInFlight = false;
let chatRenderState = {jobId: null, messageCount: 0};

const AUTOSAVE_INTERVAL_MS = 30000;
const WORKSPACE_JOB_POLL_MS = 2500;
const WORKSPACE_VERSION_POLL_MS = 4000;
const HISTORY_LIMIT = 100;
const STORAGE_KEYS = {
  folder: "workspace:currentFolder",
  file: "workspace:currentPath",
  showHidden: "workspace:showHidden",
  sidebarCollapsed: "workspace:sidebarCollapsed",
  chatCollapsed: "workspace:chatCollapsed",
  selectedJob: "workspace:selectedJob"
};

const BINARY_EXTENSIONS = new Set([
  ".7z",
  ".avif",
  ".bmp",
  ".doc",
  ".docx",
  ".dmg",
  ".eot",
  ".exe",
  ".gif",
  ".gz",
  ".ico",
  ".jpeg",
  ".jpg",
  ".mov",
  ".mp3",
  ".mp4",
  ".ods",
  ".odt",
  ".otf",
  ".pdf",
  ".png",
  ".ppt",
  ".pptx",
  ".rar",
  ".tar",
  ".tif",
  ".tiff",
  ".ttf",
  ".webm",
  ".webp",
  ".woff",
  ".woff2",
  ".xls",
  ".xlsm",
  ".xlsx",
  ".zip"
]);

const CODE_EXTENSIONS = new Set([
  ".bash",
  ".c",
  ".conf",
  ".cpp",
  ".cs",
  ".css",
  ".fish",
  ".go",
  ".h",
  ".hpp",
  ".htm",
  ".html",
  ".ini",
  ".java",
  ".js",
  ".json",
  ".jsx",
  ".less",
  ".mjs",
  ".php",
  ".py",
  ".rb",
  ".rs",
  ".sass",
  ".scss",
  ".sh",
  ".sql",
  ".ts",
  ".tsx",
  ".xml",
  ".yaml",
  ".yml",
  ".zsh"
]);

const TEXT_EXTENSIONS = new Set([".env", ".log", ".txt"]);
const TEXT_FILENAMES = new Set([".dockerignore", ".env", ".gitignore", "dockerfile", "makefile", "readme"]);
const CONVERTIBLE_DOCUMENT_EXTENSIONS = new Set([
  ".doc",
  ".docx",
  ".odp",
  ".ods",
  ".odt",
  ".pdf",
  ".ppt",
  ".pptx",
  ".xls",
  ".xlsm",
  ".xlsx"
]);

// Material icon mapping
const icons = {
  folder: folderSvg,
  folderOpen: folderOpenSvg,
  markdown: markdownSvg,
  javascript: javascriptSvg,
  typescript: typescriptSvg,
  python: pythonSvg,
  json: jsonSvg,
  yaml: yamlSvg,
  css: cssSvg,
  html: htmlSvg,
  php: phpSvg,
  image: imageSvg,
  txt: txtSvg,
  pdf: pdfSvg,
  word: wordSvg,
  powerpoint: powerpointSvg,
  file: fileSvg
};

const actionIcons = {
  newFile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M12 18v-6"/><path d="M9 15h6"/></svg>',
  newFolder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 10v6"/><path d="M9 13h6"/><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.2a2 2 0 0 1-1.6-.8L10 4a2 2 0 0 0-1.6-.8H4a2 2 0 0 0-2 2V18a2 2 0 0 0 2 2z"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>',
  uploadFolder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.2a2 2 0 0 1-1.6-.8L10 4a2 2 0 0 0-1.6-.8H4a2 2 0 0 0-2 2V18a2 2 0 0 0 2 2z"/><path d="M12 16V9"/><path d="M9 12l3-3 3 3"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 0-15.6-6.2L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 15.6 6.2L21 16"/><path d="M16 16h5v5"/></svg>',
  undo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a7 7 0 0 1 7 7v0a7 7 0 0 1-7 7h-1"/></svg>',
  redo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 14 5-5-5-5"/><path d="M20 9H10a7 7 0 0 0-7 7v0a7 7 0 0 0 7 7h1"/></svg>',
  save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>',
  saveAs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M17 3v6h6"/><path d="m14 10 7-7"/><path d="M7 13h6"/><path d="M7 17h10"/></svg>',
  rowAbove: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><path d="M12 10V2"/><path d="m8 6 4-4 4 4"/></svg>',
  rowBelow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><path d="M12 14v8"/><path d="m8 18 4 4 4-4"/></svg>',
  deleteRow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/></svg>',
  columnLeft: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4v16"/><path d="M12 4v16"/><path d="M18 4v16"/><path d="M10 12H2"/><path d="m6 8-4 4 4 4"/></svg>',
  columnRight: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4v16"/><path d="M12 4v16"/><path d="M18 4v16"/><path d="M14 12h8"/><path d="m18 8 4 4-4 4"/></svg>',
  deleteColumn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4v16"/><path d="M12 4v16"/><path d="M18 4v16"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/></svg>',
  table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 9v12"/><path d="M15 9v12"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
  convert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 7h10l-3-3"/><path d="M17 17H7l3 3"/><path d="M14 4h3v3"/><path d="M10 20H7v-3"/><path d="M6 12h12"/></svg>',
  zip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 2v6h4V2"/><path d="M4 6h16v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M12 12v4"/><path d="M10 14h4"/></svg>',
  unzip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 2v6h4V2"/><path d="M4 6h16v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M8 14h8"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
  move: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 9-3 3 3 3"/><path d="m19 9 3 3-3 3"/><path d="M2 12h20"/><path d="m9 5 3-3 3 3"/><path d="m9 19 3 3 3-3"/><path d="M12 2v20"/></svg>',
  rename: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>',
  job: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="14" x="3" y="5" rx="2"/><path d="M8 5V3h8v2"/><path d="M9 14l2 2 4-5"/></svg>',
  run: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.14v13.72a1 1 0 0 0 1.52.86l11.34-6.86a1 1 0 0 0 0-1.72L9.52 4.28A1 1 0 0 0 8 5.14Z"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>'
};

function iconMarkup(name) {
  return actionIcons[name] || "";
}

function setIconOnly(id, iconName, label) {
  const button = document.getElementById(id);
  if (!button) return;
  button.innerHTML = `${iconMarkup(iconName)}<span class="sr-only">${h(label)}</span>`;
  button.title = label;
  button.setAttribute("aria-label", label);
}

function setIconLabel(id, iconName, label) {
  const button = document.getElementById(id);
  if (!button) return;
  button.innerHTML = `${iconMarkup(iconName)}<span>${h(label)}</span>`;
  button.title = label;
}

function setupActionIcons() {
  setIconOnly("new-file", "newFile", "New file");
  setIconOnly("new-folder", "newFolder", "New folder");
  setIconOnly("upload-files", "upload", "Upload files");
  setIconOnly("upload-folder", "uploadFolder", "Upload folder");
  setIconOnly("open-trash", "trash", "Trash");
  setIconOnly("refresh-tree", "refresh", "Refresh");
  setIconOnly("undo-action", "undo", "Undo");
  setIconOnly("redo-action", "redo", "Redo");
  setIconOnly("csv-insert-row-above", "rowAbove", "Insert row above selected cell");
  setIconOnly("csv-insert-row-below", "rowBelow", "Insert row below selected cell");
  setIconOnly("csv-delete-row", "deleteRow", "Delete selected row");
  setIconOnly("csv-insert-column-left", "columnLeft", "Insert column left of selected cell");
  setIconOnly("csv-insert-column-right", "columnRight", "Insert column right of selected cell");
  setIconOnly("csv-delete-column", "deleteColumn", "Delete selected column");
  setIconLabel("md-insert-table", "table", "Insert Table");
  setIconOnly("md-insert-row-above", "rowAbove", "Insert row above selected table cell");
  setIconOnly("md-insert-row-below", "rowBelow", "Insert row below selected table cell");
  setIconOnly("md-delete-row", "deleteRow", "Delete selected table row");
  setIconOnly("md-insert-column-left", "columnLeft", "Insert column left of selected table cell");
  setIconOnly("md-insert-column-right", "columnRight", "Insert column right of selected table cell");
  setIconOnly("md-delete-column", "deleteColumn", "Delete selected table column");
  setIconLabel("save-as-file", "saveAs", "Save As");
  setIconLabel("save-file", "save", "Save");
  setIconLabel("run-active-file", "run", "Run");
  setIconLabel("new-workspace-job", "job", "New Job");
  setIconLabel("send-job-message", "send", "Send");
  setIconLabel("open-job-file", "open", "Open file");
}

function getFileIcon(entry) {
  if (entry.is_dir) return icons.folderOpen;
  const name = entry.name.toLowerCase();
  
  // Markdown
  if (/\.(md|markdown|mdown|mkdn)$/i.test(name)) return icons.markdown;
  
  // JavaScript/TypeScript
  if (/\.(js|jsx|mjs|cjs)$/i.test(name)) return icons.javascript;
  if (/\.(ts|tsx)$/i.test(name)) return icons.typescript;
  
  // Python
  if (/\.py$/i.test(name)) return icons.python;
  if (/\.php$/i.test(name)) return icons.php;
  
  // Config/Data
  if (/\.json$/i.test(name)) return icons.json;
  if (/\.(yaml|yml)$/i.test(name)) return icons.yaml;
  
  // Web
  if (/\.css$/i.test(name)) return icons.css;
  if (/\.(html|htm)$/i.test(name)) return icons.html;
  
  // Images
  if (/\.(jpg|jpeg|png|gif|svg|webp|ico|bmp)$/i.test(name)) return icons.image;
  
  // Documents
  if (/\.pdf$/i.test(name)) return icons.pdf;
  if (/\.(doc|docx|odt)$/i.test(name)) return icons.word;
  if (/\.(ppt|pptx|odp)$/i.test(name)) return icons.powerpoint;
  if (/\.(xls|xlsx|xlsm|ods)$/i.test(name)) return icons.word;
  
  // Text
  if (/\.txt$/i.test(name)) return icons.txt;
  
  // Default
  return icons.file;
}

const DEFAULT_ERROR_MESSAGE = "Something went wrong.";
const SERVER_UNAVAILABLE_MESSAGE = "Server is unavailable. Check that the assistant server is running and try again.";
const SERVER_UNAVAILABLE_STATUSES = new Set([502, 503, 504]);
const HTML_ERROR_PATTERN = /<\s*(?:!doctype|html|head|body|title|main|section|article|h[1-6]|p|div|pre|script|style)(?:\s|>|\/)/i;
const NETWORK_ERROR_PATTERN = /(?:\b(?:error|typeerror):\s*)?(?:failed to fetch|networkerror|network request failed|load failed|connection refused|err_connection_refused|err_connection_reset|econnrefused)/i;

async function api(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error(humanReadableError(error, SERVER_UNAVAILABLE_MESSAGE));
  }
  const text = await responseText(response);
  if (!response.ok) {
    throw new Error(errorMessageFromResponse(response, text));
  }
  if (!text) return null;
  const payload = parseJsonPayload(text);
  if (payload === undefined) {
    throw new Error(looksLikeHtml(text) ? summarizeHtmlError(text, response) : "Server returned an invalid response.");
  }
  return payload;
}

function fmt(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function compactText(value) {
  return fmt(value).replace(/\s+/g, " ").trim();
}

function truncateText(value, limit = 300) {
  const text = compactText(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 3))}...`;
}

function trimErrorPrefix(value) {
  return compactText(value).replace(/^(?:error|typeerror|syntaxerror):\s*/i, "").replace(/[:\s-]+$/g, "");
}

function decodeHtmlEntities(value) {
  const text = fmt(value);
  if (!/&(?:[a-z]+|#\d+|#x[\da-f]+);/i.test(text) || typeof document === "undefined") return text;
  const textarea = document.createElement("textarea");
  textarea.innerHTML = text;
  return textarea.value;
}

function htmlToText(value) {
  return compactText(
    decodeHtmlEntities(fmt(value)
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " "))
  );
}

function looksLikeHtml(value) {
  return HTML_ERROR_PATTERN.test(fmt(value));
}

function firstHtmlTagText(value, tagName) {
  const match = new RegExp(`<${tagName}[^>]*>([\\s\\S]*?)<\\/${tagName}>`, "i").exec(fmt(value));
  return match ? htmlToText(match[1]) : "";
}

function responseStatusLabel(response) {
  if (!response) return "";
  return `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
}

function summarizeHtmlError(value, response = null) {
  if (response && SERVER_UNAVAILABLE_STATUSES.has(response.status)) return SERVER_UNAVAILABLE_MESSAGE;
  const title = firstHtmlTagText(value, "title") || firstHtmlTagText(value, "h1");
  const status = responseStatusLabel(response);
  if (title) return status ? `Server returned ${status}: ${title}` : `Server returned ${title}.`;
  if (status) return `Server returned ${status}.`;
  return truncateText(htmlToText(value) || "Server returned an HTML error page.", 180);
}

function parseJsonPayload(text) {
  if (!fmt(text).trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function detailText(detail) {
  if (Array.isArray(detail)) {
    return detail.map(item => {
      if (!item || typeof item !== "object") return compactText(item);
      const location = Array.isArray(item.loc) ? item.loc.filter(part => part !== "body").join(".") : "";
      const message = item.msg || item.message || item.detail || item.error || "";
      if (location && message) return `${location}: ${message}`;
      if (message) return message;
      return JSON.stringify(item);
    }).filter(Boolean).join("; ");
  }
  if (detail && typeof detail === "object") {
    return detailText(detail.detail || detail.message || detail.error || JSON.stringify(detail));
  }
  return compactText(detail);
}

function payloadErrorMessage(payload) {
  if (payload === null || payload === undefined) return "";
  if (typeof payload === "object" && !Array.isArray(payload)) {
    return detailText(payload.detail || payload.message || payload.error || payload.title || payload);
  }
  return detailText(payload);
}

async function responseText(response) {
  try {
    return await response.text();
  } catch {
    return "";
  }
}

function errorMessageFromResponse(response, text) {
  const payload = parseJsonPayload(text);
  const payloadMessage = payload === undefined ? "" : payloadErrorMessage(payload);
  if (payloadMessage) return humanReadableError(payloadMessage, responseStatusLabel(response));
  if (looksLikeHtml(text)) return summarizeHtmlError(text, response);
  return humanReadableError(text || responseStatusLabel(response), responseStatusLabel(response));
}

function humanReadableError(error, fallback = DEFAULT_ERROR_MESSAGE) {
  const raw = error instanceof Error ? error.message : error;
  const message = trimErrorPrefix(raw);
  if (!message) return fallback;
  const htmlMatch = HTML_ERROR_PATTERN.exec(message);
  if (htmlMatch) {
    const prefix = trimErrorPrefix(message.slice(0, htmlMatch.index));
    const summary = summarizeHtmlError(message.slice(htmlMatch.index));
    return prefix ? `${prefix}: ${summary}` : summary;
  }
  const networkMatch = NETWORK_ERROR_PATTERN.exec(message);
  if (networkMatch) {
    const prefix = trimErrorPrefix(message.slice(0, networkMatch.index));
    return prefix ? `${prefix}: ${SERVER_UNAVAILABLE_MESSAGE}` : SERVER_UNAVAILABLE_MESSAGE;
  }
  return truncateText(message);
}

function errorWithPrefix(prefix, error) {
  const detail = humanReadableError(error);
  return detail ? `${prefix}: ${detail}` : prefix;
}

function h(value) {
  return fmt(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderMarkdownInline(value) {
  const placeholders = [];
  let text = h(value).replace(/`([^`\n]+)`/g, (_match, code) => {
    const token = `@@CODE${placeholders.length}@@`;
    placeholders.push(`<code>${code}</code>`);
    return token;
  });
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) => {
    return `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`;
  });
  text = text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  placeholders.forEach((html, index) => {
    text = text.replace(`@@CODE${index}@@`, html);
  });
  return text;
}

function renderMarkdownBlocks(lines) {
  const blocks = [];
  let paragraph = [];
  let list = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${renderMarkdownInline(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!list.length) return;
    blocks.push(`<ul>${list.map(item => `<li>${renderMarkdownInline(item)}</li>`).join("")}</ul>`);
    list = [];
  };

  for (const rawLine of lines) {
    const line = fmt(rawLine);
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push(`<h${heading[1].length}>${renderMarkdownInline(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      list.push(bullet[1]);
      continue;
    }
    if (line.startsWith(">")) {
      flushParagraph();
      flushList();
      blocks.push(`<blockquote>${renderMarkdownInline(line.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }
    flushList();
    paragraph.push(line.trim());
  }
  flushParagraph();
  flushList();
  return blocks.join("");
}

function renderMarkdown(value) {
  const text = fmt(value);
  if (!text.trim()) return "";
  const parts = text.split(/```/);
  return parts
    .map((part, index) => {
      if (index % 2 === 1) {
        const lines = part.replace(/^\w+\n/, "");
        return `<pre><code>${h(lines.trim())}</code></pre>`;
      }
      return renderMarkdownBlocks(part.split(/\r?\n/));
    })
    .join("");
}

function bytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function isMarkdownPath(path) {
  return /\.(md|markdown|mdown|mkdn)$/i.test(path || "");
}

function extensionForPath(path) {
  const name = baseName(path).toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot) : "";
}

function isCsvPath(path) {
  return /\.(csv|tsv)$/i.test(path || "");
}

function isCodePath(path) {
  return CODE_EXTENSIONS.has(extensionForPath(path));
}

function isTextPath(path) {
  const name = baseName(path).toLowerCase();
  const extension = extensionForPath(path);
  return TEXT_EXTENSIONS.has(extension) || TEXT_FILENAMES.has(name) || (!extension && Boolean(name));
}

function isConvertibleDocumentPath(path) {
  return CONVERTIBLE_DOCUMENT_EXTENSIONS.has(extensionForPath(path));
}

function isConversionPendingEntry(entry) {
  if (!entry || entry.is_dir) return false;
  if (entry.conversion?.pending === true || entry.conversion_pending === true) return true;
  const status = entry.conversion?.status || entry.conversion_status || "";
  return status === "pending";
}

function conversionStatusLabel(entry) {
  if (isConversionPendingEntry(entry)) return "Converting";
  const status = entry?.conversion?.status || entry?.conversion_status || "";
  if (status === "failed") return "Conversion failed";
  if (status === "skipped") return "Conversion skipped";
  return "";
}

function editorKindForPath(path) {
  if (!path) return "empty";
  if (isMarkdownPath(path)) return "markdown";
  if (isCsvPath(path)) return "csv";
  if (isCodePath(path)) return "code";
  return "text";
}

function initialEditorModeForPath(path) {
  return isMarkdownPath(path) || !path ? "write" : "source";
}

function isEditableTextPath(path) {
  const extension = extensionForPath(path);
  return !BINARY_EXTENSIONS.has(extension) && (isMarkdownPath(path) || isCsvPath(path) || isCodePath(path) || isTextPath(path));
}

function isRunnableScriptPath(path) {
  return [".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".rb", ".php"].includes(extensionForPath(path));
}

function isWorkspaceConvertiblePath(path) {
  return isEditableTextPath(path) || isConvertibleDocumentPath(path);
}

function defaultCommandForPath(path) {
  const name = baseName(path);
  const extension = extensionForPath(path);
  if (extension === ".py") return `python ${quoteCommandPart(name)}`;
  if (extension === ".sh" || extension === ".bash") return `bash ${quoteCommandPart(name)}`;
  if (extension === ".zsh") return `zsh ${quoteCommandPart(name)}`;
  if ([".js", ".mjs", ".cjs"].includes(extension)) return `node ${quoteCommandPart(name)}`;
  if (extension === ".rb") return `ruby ${quoteCommandPart(name)}`;
  if (extension === ".php") return `php ${quoteCommandPart(name)}`;
  return "";
}

function quoteCommandPart(value) {
  const text = fmt(value);
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(text)) return text;
  return `"${text.replace(/\\/g, "\\\\").replace(/"/g, "\\\"")}"`;
}

function parseCommandLine(value) {
  const args = [];
  let current = "";
  let quote = null;
  let escaping = false;
  for (const char of fmt(value)) {
    if (escaping) {
      current += char;
      escaping = false;
      continue;
    }
    if (char === "\\") {
      escaping = true;
      continue;
    }
    if (quote) {
      if (char === quote) {
        quote = null;
      } else {
        current += char;
      }
      continue;
    }
    if (char === "\"" || char === "'") {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) {
        args.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }
  if (escaping) current += "\\";
  if (quote) throw new Error("Close the quoted command argument");
  if (current) args.push(current);
  return args;
}

function fenceLanguageForPath(path) {
  const extension = extensionForPath(path).replace(/^\./, "");
  if (!extension) return "text";
  if (extension === "mdown" || extension === "mkdn") return "markdown";
  if (extension === "yml") return "yaml";
  if (extension === "htm") return "html";
  return extension;
}

function folderForPath(path) {
  const parts = fmt(path).split("/").filter(Boolean);
  if (parts.length <= 1) return ".";
  return parts.slice(0, -1).join("/");
}

function activeEditorPath() {
  return currentPath || untitledPath;
}

function hasActiveDocument() {
  return Boolean(activeEditorPath());
}

function baseName(path) {
  const parts = fmt(path).split("/").filter(Boolean);
  return parts.pop() || "";
}

function joinPath(folder, name) {
  const cleanFolder = fmt(folder || ".").replace(/^\/+|\/+$/g, "") || ".";
  const cleanName = fmt(name).replace(/^\/+|\/+$/g, "");
  return cleanFolder === "." ? cleanName : `${cleanFolder}/${cleanName}`;
}

function pathContains(parent, child) {
  if (!parent || !child) return false;
  return child === parent || child.startsWith(`${parent}/`);
}

function isHiddenPath(path) {
  return fmt(path)
    .split("/")
    .filter(Boolean)
    .some(part => part.startsWith("."));
}

function isTrashPath(path) {
  const clean = fmt(path).replace(/^\/+|\/+$/g, "");
  return clean === ".trash" || clean.startsWith(".trash/");
}

function shouldShowFolderPath(path) {
  if (isTrashPath(path)) return isTrashPath(currentFolder);
  return showHidden || !isHiddenPath(path);
}

function replacePathPrefix(path, source, destination) {
  if (path === source) return destination;
  return `${destination}${path.slice(source.length)}`;
}

function newTabId() {
  tabSequence += 1;
  return `tab-${Date.now()}-${tabSequence}`;
}

function activeTab() {
  return openTabs.find(tab => tab.id === activeTabId) || null;
}

function tabPath(tab) {
  return tab?.currentPath || tab?.untitledPath || tab?.unsupportedPath || "";
}

function tabTitle(tab) {
  const path = tabPath(tab);
  return baseName(path) || path || "Untitled";
}

function tabMatchesPath(tab, path) {
  return Boolean(path && (tab?.currentPath === path || tab?.unsupportedPath === path));
}

function createTabState(values = {}) {
  return {
    id: newTabId(),
    currentPath: null,
    untitledPath: null,
    currentMtimeNs: null,
    currentContent: "",
    activeMarkdown: "",
    dirty: false,
    editorMode: "write",
    unsupportedPath: null,
    unsupportedEntry: null,
    lastDraftContent: "",
    historyStack: [],
    redoStack: [],
    externalConflict: false,
    externalMissing: false,
    externalNoticeShown: false,
    externalPrompted: false,
    ...values
  };
}

function captureActiveTab(options = {}) {
  const tab = activeTab();
  if (!tab) return;
  let content = activeMarkdown;
  if (!options.skipSync && activeEditorPath()) {
    content = syncMarkdownFromMode();
  }
  Object.assign(tab, {
    currentPath,
    untitledPath,
    currentMtimeNs,
    currentContent,
    activeMarkdown: content,
    dirty,
    editorMode,
    unsupportedPath,
    unsupportedEntry,
    lastDraftContent,
    historyStack: [...historyStack],
    redoStack: [...redoStack]
  });
}

function applyTabToGlobals(tab) {
  currentPath = tab.currentPath || null;
  untitledPath = tab.untitledPath || null;
  currentMtimeNs = tab.currentMtimeNs || null;
  currentContent = tab.currentContent || "";
  activeMarkdown = tab.activeMarkdown || "";
  dirty = Boolean(tab.dirty);
  editorMode = tab.editorMode || "write";
  unsupportedPath = tab.unsupportedPath || null;
  unsupportedEntry = tab.unsupportedEntry || null;
  lastDraftContent = tab.lastDraftContent || "";
  historyStack = [...(tab.historyStack || [])];
  redoStack = [...(tab.redoStack || [])];
  currentFolder = folderForPath(tabPath(tab)) || currentFolder || ".";
}

function updateActiveTabState(values = {}) {
  const tab = activeTab();
  if (!tab) return;
  Object.assign(tab, values);
  renderEditorTabs();
}

function renderEditorTabs() {
  const target = document.getElementById("editor-tab-strip");
  if (!target) return;
  target.classList.toggle("hidden", openTabs.length === 0);
  target.innerHTML = openTabs
    .map(tab => {
      const active = tab.id === activeTabId ? "active" : "";
      const changed = tab.externalConflict || tab.externalMissing ? " changed" : "";
      const dirtyMark = tab.dirty ? '<span class="editor-tab-dirty" aria-hidden="true"></span>' : "";
      const title = tab.externalMissing
        ? `${tabTitle(tab)} no longer exists`
        : tab.externalConflict
          ? `${tabTitle(tab)} changed externally`
          : tabTitle(tab);
      return `
        <button class="editor-tab ${active}${changed}" type="button" data-tab-id="${h(tab.id)}" title="${h(title)}">
          <span class="editor-tab-status" aria-hidden="true">${dirtyMark}</span>
          <span class="editor-tab-title">${h(tabTitle(tab))}</span>
          <span class="editor-tab-close" data-close-tab="${h(tab.id)}" role="button" aria-label="Close ${h(tabTitle(tab))}">×</span>
        </button>
      `;
    })
    .join("");
  target.querySelectorAll("[data-tab-id]").forEach(button => {
    button.onclick = event => {
      if (event.target.closest("[data-close-tab]")) return;
      activateTab(button.dataset.tabId);
    };
  });
  target.querySelectorAll("[data-close-tab]").forEach(button => {
    button.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      closeTab(button.dataset.closeTab);
    };
  });
}

function updateTrashButton() {
  const button = document.getElementById("open-trash");
  if (!button) return;
  button.classList.toggle("active", isTrashPath(currentFolder));
}

function syncShowHiddenButton() {
  const button = document.getElementById("show-hidden");
  if (!button) return;
  button.innerHTML = `${iconMarkup("eye")}<span class="sr-only">${showHidden ? "Hide hidden files" : "Show hidden files"}</span>`;
  button.title = showHidden ? "Hide hidden files" : "Show hidden files";
  button.setAttribute("aria-label", button.title);
  button.setAttribute("aria-pressed", String(showHidden));
  button.classList.toggle("active", showHidden);
}

function updateRunButtonState() {
  const button = document.getElementById("run-active-file");
  if (!button) return;
  const path = currentPath;
  button.disabled = !path || !isRunnableScriptPath(path);
}

function duplicateName(path, isDir) {
  const name = baseName(path);
  if (isDir) return `${name} copy`;
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return `${name} copy`;
  return `${name.slice(0, dot)} copy${name.slice(dot)}`;
}

function cleanEntryName(value) {
  const name = fmt(value).trim();
  if (!name || name === "." || name === ".." || name.includes("/")) {
    showError("Enter a valid name without slashes");
    return null;
  }
  return name;
}

function readStoredValue(key, fallback = null) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function writeStoredValue(key, value) {
  try {
    if (value === null || value === undefined || value === "") {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, value);
    }
  } catch {
    // Storage can be unavailable in hardened browser profiles.
  }
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  return {
    folder: params.get("folder"),
    file: params.get("file")
  };
}

function writeUrlState() {
  const url = new URL(window.location.href);
  if (currentFolder && currentFolder !== ".") {
    url.searchParams.set("folder", currentFolder);
  } else {
    url.searchParams.delete("folder");
  }
  if (currentPath) {
    url.searchParams.set("file", currentPath);
  } else {
    url.searchParams.delete("file");
  }
  window.history.replaceState(null, "", url);
}

function persistWorkspaceState() {
  writeStoredValue(STORAGE_KEYS.folder, currentFolder);
  writeStoredValue(STORAGE_KEYS.file, currentPath);
  writeUrlState();
}

function formatClock(date) {
  return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

function mtimeDate(mtimeNs) {
  try {
    return new Date(Number(BigInt(String(mtimeNs)) / 1000000n));
  } catch {
    return new Date();
  }
}

function mtimeIsNewer(left, right) {
  if (!left) return false;
  if (!right) return true;
  try {
    return BigInt(String(left)) > BigInt(String(right));
  } catch {
    return String(left) > String(right);
  }
}

function markdownTitleForContent(content) {
  let inFence = false;
  for (const rawLine of fmt(content).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const match = line.match(/^#\s+(.+?)\s*#*\s*$/);
    if (match) return match[1].trim();
  }
  return "";
}

function editorTitleForDocument(path, content) {
  if (!path) return "No file selected";
  if (isMarkdownPath(path)) return markdownTitleForContent(content) || baseName(path) || path;
  return baseName(path) || path;
}

function updateEditorTitle() {
  const target = document.getElementById("editor-file-name");
  if (!target) return;
  const path = activeEditorPath() || unsupportedPath;
  const title = unsupportedPath ? baseName(unsupportedPath) || unsupportedPath : editorTitleForDocument(path, activeMarkdown);
  target.textContent = title;
  target.title = title;
}

function setDraftStatus(message, tone = "idle") {
  const target = document.getElementById("draft-status");
  if (!target) return;
  target.textContent = message;
  target.className = `draft-status ${tone}`;
  target.style.display = hasActiveDocument() ? "inline-flex" : "none";
}

function updateContextControls() {
  const hasDocument = hasActiveDocument();
  for (const id of ["include-active-file", "include-file-content"]) {
    const input = document.getElementById(id);
    if (!input) continue;
    const label = input.closest(".check-row");
    if (!hasDocument) {
      input.checked = false;
      input.disabled = true;
      label?.classList.add("disabled");
      contextControlsWereDisabled = true;
    } else {
      input.disabled = false;
      label?.classList.remove("disabled");
      if (contextControlsWereDisabled) {
        input.checked = id === "include-active-file";
      }
    }
  }
  if (hasDocument) contextControlsWereDisabled = false;
}

function setDirty(value) {
  dirty = value;
  const save = document.getElementById("save-file");
  const saveAs = document.getElementById("save-as-file");
  const hasDocument = hasActiveDocument();
  updateEditorTitle();
  save.disabled = !hasDocument || (!dirty && Boolean(currentPath));
  if (saveAs) saveAs.disabled = !currentPath;
  updateRunButtonState();
  updateContextControls();
  if (!hasDocument) {
    setDraftStatus("No draft", "idle");
  } else if (!currentPath) {
    setDraftStatus("Unsaved", "pending");
  } else if (dirty) {
    setDraftStatus("Pending", "pending");
  } else {
    setDraftStatus("Saved", "idle");
  }
  updateUndoRedoButtons();
  const tab = activeTab();
  if (tab) {
    Object.assign(tab, {
      currentPath,
      untitledPath,
      currentMtimeNs,
      currentContent,
      activeMarkdown,
      dirty,
      editorMode,
      unsupportedPath,
      unsupportedEntry,
      lastDraftContent,
      historyStack: [...historyStack],
      redoStack: [...redoStack]
    });
    renderEditorTabs();
  }
}

function updateTopbarBreadcrumb() {
  const target = document.getElementById("topbar-breadcrumb");
  const path = activeEditorPath() || unsupportedPath;
  if (!path) {
    target.textContent = "Workspace";
    return;
  }
  const parts = path.split("/");
  const folder = parts.slice(0, -1).join(" / ");
  target.textContent = folder ? `Workspace / ${folder}` : "Workspace";
}

function updateBreadcrumb() {
  const target = document.getElementById("current-folder");
  if (currentFolder === ".") {
    target.innerHTML = '<a href="#" data-folder=".">Workspace</a>';
  } else {
    const parts = currentFolder.split("/");
    const breadcrumbs = ['<a href="#" data-folder=".">Workspace</a>'];
    for (let i = 0; i < parts.length; i++) {
      const path = parts.slice(0, i + 1).join("/");
      breadcrumbs.push(`<a href="#" data-folder="${h(path)}">${h(parts[i])}</a>`);
    }
    target.innerHTML = breadcrumbs.join(' <span class="breadcrumb-sep">›</span> ');
  }
  
  // Bind click handlers to breadcrumb links
  target.querySelectorAll("[data-folder]").forEach(link => {
    link.onclick = (e) => {
      e.preventDefault();
      loadTree(link.dataset.folder);
    };
  });
}

function syncMarkdownFromMode() {
  const path = activeEditorPath();
  if (!path) return "";
  const kind = editorKindForPath(path);
  if (kind === "csv") {
    activeMarkdown = csvContentFromTable();
  } else if (kind === "code" && codeEditor) {
    activeMarkdown = codeEditor.state.doc.toString();
  } else if (editorMode === "source" || kind === "text" || kind === "code") {
    activeMarkdown = document.getElementById("editor-text").value;
  } else if (milkdownEditor && isMarkdownPath(path)) {
    activeMarkdown = getMilkdownMarkdown();
  }
  return activeMarkdown;
}

function updateUndoRedoButtons() {
  const undo = document.getElementById("undo-action");
  const redo = document.getElementById("redo-action");
  const hasDocument = hasActiveDocument();
  if (undo) undo.disabled = !hasDocument || historyStack.length === 0;
  if (redo) redo.disabled = !hasDocument || redoStack.length === 0;
}

function resetEditorHistory() {
  historyStack = [];
  redoStack = [];
  updateUndoRedoButtons();
}

function recordHistorySnapshot(previousContent) {
  if (applyingHistory || !hasActiveDocument()) return;
  const previous = fmt(previousContent);
  if (historyStack[historyStack.length - 1] === previous) return;
  historyStack.push(previous);
  if (historyStack.length > HISTORY_LIMIT) {
    historyStack = historyStack.slice(historyStack.length - HISTORY_LIMIT);
  }
  redoStack = [];
  updateUndoRedoButtons();
}

async function replaceActiveEditorContent(content) {
  const path = activeEditorPath();
  if (!path) return;
  const nextContent = fmt(content);
  activeMarkdown = nextContent;
  const kind = editorKindForPath(path);
  if (kind === "csv") {
    renderCsvTable(nextContent, path);
  } else if (kind === "code" && codeEditor) {
    codeEditor.dispatch({
      changes: {from: 0, to: codeEditor.state.doc.length, insert: nextContent}
    });
  } else if (kind === "markdown" && editorMode === "write") {
    await createMilkdown(nextContent);
  } else {
    document.getElementById("editor-text").value = nextContent;
  }
  setDirty(nextContent !== currentContent);
}

async function undoEditorChange() {
  if (!hasActiveDocument() || historyStack.length === 0) return;
  const current = syncMarkdownFromMode();
  const previous = historyStack.pop();
  redoStack.push(current);
  applyingHistory = true;
  try {
    await replaceActiveEditorContent(previous);
  } finally {
    applyingHistory = false;
    updateUndoRedoButtons();
  }
}

async function redoEditorChange() {
  if (!hasActiveDocument() || redoStack.length === 0) return;
  const current = syncMarkdownFromMode();
  const next = redoStack.pop();
  historyStack.push(current);
  applyingHistory = true;
  try {
    await replaceActiveEditorContent(next);
  } finally {
    applyingHistory = false;
    updateUndoRedoButtons();
  }
}

function setEditorMode(mode) {
  const path = activeEditorPath();
  if (!path && mode !== "write") return;
  syncMarkdownFromMode();
  const kind = editorKindForPath(path);
  editorMode = kind === "markdown" || kind === "empty" ? mode : "source";
  persistWorkspaceState();
  
  // Update tab visibility and active state
  const tabs = document.getElementById("editor-mode-tabs");
  const writeBtn = document.getElementById("mode-write");
  const sourceBtn = document.getElementById("mode-source");
  
  if (kind === "markdown") {
    tabs.style.display = "grid";
    writeBtn.classList.toggle("active", editorMode === "write");
    sourceBtn.classList.toggle("active", editorMode === "source");
  } else {
    tabs.style.display = "none";
  }
  
  const surface = document.getElementById("editor-surface");
  surface.classList.remove("mode-write", "mode-source", "kind-empty", "kind-markdown", "kind-csv", "kind-code", "kind-text", "kind-unsupported");
  surface.classList.add(`mode-${editorMode}`, `kind-${kind}`);
  document.getElementById("editor-text").value = activeMarkdown;
  
  if (editorMode === "write" && path && kind === "markdown") {
    createMilkdown(activeMarkdown);
  }
  updateActiveTabState({editorMode, activeMarkdown});
  updateMarkdownToolbarState();
}

async function destroyMilkdown() {
  editorGeneration += 1;
  if (milkdownEditor) {
    await milkdownEditor.destroy();
    milkdownEditor = null;
  }
  document.getElementById("milkdown-editor").innerHTML = "";
  updateMarkdownToolbarState();
}

function getMilkdownMarkdown() {
  if (!milkdownEditor) return activeMarkdown;
  return milkdownEditor.action(ctx => {
    const view = ctx.get(editorViewCtx);
    const serializer = ctx.get(serializerCtx);
    return serializer(view.state.doc);
  });
}

const MARKDOWN_FORMAT_TOOLS = [
  {id: "md-bold", schema: strongSchema, command: toggleStrongCommand},
  {id: "md-italic", schema: emphasisSchema, command: toggleEmphasisCommand},
  {id: "md-strike", schema: strikethroughSchema, command: toggleStrikethroughCommand},
  {id: "md-code", schema: inlineCodeSchema, command: toggleInlineCodeCommand}
];

const MARKDOWN_TABLE_TOOL_IDS = [
  "md-insert-row-above",
  "md-insert-row-below",
  "md-delete-row",
  "md-insert-column-left",
  "md-insert-column-right",
  "md-delete-column"
];

function isMarkdownWriteMode() {
  const path = activeEditorPath();
  return Boolean(path && editorKindForPath(path) === "markdown" && editorMode === "write" && milkdownEditor);
}

function selectionHasMark(state, markType) {
  const {selection, storedMarks} = state;
  if (selection.empty) {
    return (storedMarks || selection.$from.marks()).some(mark => mark.type === markType);
  }
  return state.doc.rangeHasMark(selection.from, selection.to, markType);
}

function updateMarkdownToolbarState(ctx = null) {
  const available = isMarkdownWriteMode();
  let inTable = false;
  let activeMarks = new Set();
  if (available) {
    try {
      milkdownEditor.action(editorCtx => {
        const targetCtx = ctx || editorCtx;
        const view = targetCtx.get(editorViewCtx);
        inTable = isInTable(view.state);
        activeMarks = new Set(
          MARKDOWN_FORMAT_TOOLS
            .filter(tool => selectionHasMark(view.state, tool.schema.type(targetCtx)))
            .map(tool => tool.id)
        );
      });
    } catch {
      inTable = false;
      activeMarks = new Set();
    }
  }

  for (const tool of MARKDOWN_FORMAT_TOOLS) {
    const button = document.getElementById(tool.id);
    if (!button) continue;
    button.disabled = !available;
    button.classList.toggle("active", activeMarks.has(tool.id));
    button.setAttribute("aria-pressed", String(activeMarks.has(tool.id)));
  }

  const insertTable = document.getElementById("md-insert-table");
  if (insertTable) insertTable.disabled = !available;

  for (const id of MARKDOWN_TABLE_TOOL_IDS) {
    const button = document.getElementById(id);
    if (button) button.disabled = !available || !inTable;
  }
}

function runMarkdownAction(action) {
  if (!isMarkdownWriteMode()) return false;
  return milkdownEditor.action(ctx => {
    const view = ctx.get(editorViewCtx);
    const serializer = ctx.get(serializerCtx);
    const previousMarkdown = serializer(view.state.doc);
    const result = action(ctx, view, ctx.get(commandsCtx));
    if (!result) {
      updateMarkdownToolbarState(ctx);
      return false;
    }
    recordHistorySnapshot(previousMarkdown);
    activeMarkdown = serializer(ctx.get(editorViewCtx).state.doc);
    setDirty(activeMarkdown !== currentContent);
    updateMarkdownToolbarState(ctx);
    ctx.get(editorViewCtx).focus();
    return true;
  });
}

function runMarkdownCommand(command, payload) {
  return runMarkdownAction((_, __, commands) => commands.call(command.key, payload));
}

function bindMarkdownToolbarEvents() {
  const toolbar = document.getElementById("markdown-toolbar");
  if (!toolbar) return;
  toolbar.querySelectorAll("button").forEach(button => {
    button.addEventListener("mousedown", event => {
      event.preventDefault();
    });
  });

  for (const tool of MARKDOWN_FORMAT_TOOLS) {
    const button = document.getElementById(tool.id);
    if (button) button.onclick = () => runMarkdownCommand(tool.command);
  }

  document.getElementById("md-insert-table").onclick = () => runMarkdownCommand(insertTableCommand, {row: 3, col: 3});
  document.getElementById("md-insert-row-above").onclick = () => runMarkdownCommand(addRowBeforeCommand);
  document.getElementById("md-insert-row-below").onclick = () => runMarkdownCommand(addRowAfterCommand);
  document.getElementById("md-delete-row").onclick = () => runMarkdownAction((_, view) => deleteMarkdownRow(view.state, view.dispatch, view));
  document.getElementById("md-insert-column-left").onclick = () => runMarkdownCommand(addColBeforeCommand);
  document.getElementById("md-insert-column-right").onclick = () => runMarkdownCommand(addColAfterCommand);
  document.getElementById("md-delete-column").onclick = () => runMarkdownAction((_, view) => deleteMarkdownColumn(view.state, view.dispatch, view));
  updateMarkdownToolbarState();
}

const workspaceHighlightStyle = HighlightStyle.define([
  {tag: tags.keyword, color: "#9d174d", fontWeight: "600"},
  {tag: tags.atom, color: "#7c3aed"},
  {tag: tags.bool, color: "#7c3aed"},
  {tag: tags.number, color: "#0f766e"},
  {tag: tags.string, color: "#047857"},
  {tag: tags.comment, color: "#6b7280", fontStyle: "italic"},
  {tag: tags.variableName, color: "#1f2937"},
  {tag: tags.propertyName, color: "#1d4ed8"},
  {tag: tags.typeName, color: "#b45309"},
  {tag: tags.tagName, color: "#b91c1c"},
  {tag: tags.attributeName, color: "#1d4ed8"},
  {tag: tags.operator, color: "#7c3aed"},
  {tag: tags.punctuation, color: "#4b5563"},
  {tag: tags.meta, color: "#9333ea"}
]);

const CODE_KEYWORDS = new Set([
  "abstract",
  "and",
  "as",
  "async",
  "await",
  "break",
  "case",
  "catch",
  "class",
  "const",
  "continue",
  "def",
  "default",
  "defer",
  "do",
  "elif",
  "else",
  "enum",
  "export",
  "extends",
  "final",
  "finally",
  "for",
  "foreach",
  "from",
  "func",
  "function",
  "global",
  "go",
  "if",
  "implements",
  "import",
  "in",
  "include",
  "interface",
  "lambda",
  "let",
  "match",
  "namespace",
  "new",
  "not",
  "or",
  "package",
  "pass",
  "private",
  "protected",
  "public",
  "raise",
  "require",
  "return",
  "self",
  "static",
  "struct",
  "switch",
  "this",
  "throw",
  "trait",
  "try",
  "type",
  "use",
  "using",
  "var",
  "while",
  "with",
  "yield"
]);

const ATOM_WORDS = new Set(["false", "nil", "None", "null", "NULL", "true", "True", "False", "undefined"]);

function codeLineComment(path) {
  const extension = extensionForPath(path);
  if ([".py", ".rb", ".sh", ".bash", ".zsh", ".fish", ".conf", ".ini", ".yaml", ".yml"].includes(extension)) return "#";
  if (extension === ".sql") return "--";
  return "//";
}

function codeLanguageForPath(path) {
  const lineComment = codeLineComment(path);
  const extension = extensionForPath(path);
  const htmlLike = [".html", ".htm", ".xml"].includes(extension);
  return StreamLanguage.define({
    name: extension.replace(/^\./, "") || "text",
    startState: () => ({blockComment: false}),
    token(stream, state) {
      if (state.blockComment) {
        if (stream.skipTo("*/")) {
          stream.match("*/");
          state.blockComment = false;
        } else {
          stream.skipToEnd();
        }
        return "comment";
      }
      if (stream.eatSpace()) return null;
      if (htmlLike && stream.match(/^<!--/)) {
        if (stream.skipTo("-->")) stream.match("-->");
        else stream.skipToEnd();
        return "comment";
      }
      if (htmlLike && stream.match(/^<\/?[A-Za-z][\w:-]*/)) return "tagName";
      if (htmlLike && stream.match(/^\/?>/)) return "punctuation";
      if (extension === ".php" && stream.match(/^<\?(php)?|^\?>/i)) return "meta";
      if (lineComment && stream.match(lineComment)) {
        stream.skipToEnd();
        return "comment";
      }
      if (stream.match("/*")) {
        state.blockComment = true;
        return "comment";
      }
      const ch = stream.next();
      if (ch === "\"" || ch === "'" || ch === "`") {
        let escaped = false;
        while (!stream.eol()) {
          const next = stream.next();
          if (next === ch && !escaped) break;
          escaped = next === "\\" && !escaped;
          if (next !== "\\") escaped = false;
        }
        return "string";
      }
      if (/[0-9]/.test(ch)) {
        stream.eatWhile(/[0-9A-Fa-f_xX.]/);
        return "number";
      }
      if (/[A-Za-z_$-]/.test(ch)) {
        stream.eatWhile(/[A-Za-z0-9_$-]/);
        const word = stream.current();
        if (CODE_KEYWORDS.has(word)) return "keyword";
        if (ATOM_WORDS.has(word)) return "atom";
        if (stream.peek() === ":") return "propertyName";
        return htmlLike ? "attributeName" : "variableName";
      }
      if (/[{}()[\],.;:]/.test(ch)) return "punctuation";
      if (/[+\-*/%=!<>|&~^?@]/.test(ch)) {
        stream.eatWhile(/[+\-*/%=!<>|&~^?@]/);
        return "operator";
      }
      return null;
    }
  });
}

function destroyCodeEditor() {
  if (codeEditor) {
    codeEditor.destroy();
    codeEditor = null;
  }
  document.getElementById("code-editor").innerHTML = "";
}

function createCodeEditor(content, path) {
  destroyCodeEditor();
  const root = document.getElementById("code-editor");
  const updateListener = EditorView.updateListener.of(update => {
    if (!update.docChanged) return;
    recordHistorySnapshot(update.startState.doc.toString());
    activeMarkdown = update.state.doc.toString();
    setDirty(activeMarkdown !== currentContent);
  });
  codeEditor = new EditorView({
    parent: root,
    state: EditorState.create({
      doc: content || "",
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        drawSelection(),
        highlightActiveLine(),
        EditorState.tabSize.of(2),
        codeLanguageForPath(path),
        syntaxHighlighting(defaultHighlightStyle, {fallback: true}),
        syntaxHighlighting(workspaceHighlightStyle),
        EditorView.lineWrapping,
        updateListener
      ]
    })
  });
}

function delimiterForPath(path) {
  return /\.tsv$/i.test(path || "") ? "\t" : ",";
}

function parseDelimited(source, delimiter) {
  const rows = [[]];
  let row = rows[0];
  let cell = "";
  let inQuotes = false;
  const text = fmt(source);
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inQuotes) {
      if (char === "\"") {
        if (text[index + 1] === "\"") {
          cell += "\"";
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        cell += char;
      }
      continue;
    }
    if (char === "\"") {
      inQuotes = true;
    } else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      row.push(cell);
      cell = "";
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      row = [];
      rows.push(row);
    } else {
      cell += char;
    }
  }
  row.push(cell);
  if (rows.length > 1 && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === "") {
    rows.pop();
  }
  return rows.length ? rows : [[""]];
}

function serializeDelimited(rows, delimiter) {
  return rows
    .map(row =>
      row
        .map(value => {
          const text = fmt(value);
          if (text.includes("\"") || text.includes("\n") || text.includes("\r") || text.includes(delimiter)) {
            return `"${text.replace(/"/g, "\"\"")}"`;
          }
          return text;
        })
        .join(delimiter)
    )
    .join("\n");
}

function normalizeCsvRows(rows) {
  const width = Math.max(1, ...rows.map(row => row.length));
  return rows.map(row => {
    const next = row.slice();
    while (next.length < width) next.push("");
    return next;
  });
}

function columnLabel(index) {
  let value = index + 1;
  let label = "";
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
}

function csvContentFromTable() {
  const table = document.getElementById("csv-table");
  const rows = [];
  for (const tr of table.querySelectorAll("tbody tr")) {
    rows.push(Array.from(tr.querySelectorAll("input")).map(input => input.value));
  }
  return serializeDelimited(rows.length ? rows : [[""]], csvDelimiter);
}

function updateCsvActionState() {
  const hasTable = hasActiveDocument() && editorKindForPath(activeEditorPath()) === "csv";
  const width = Math.max(1, ...csvRows.map(row => row.length));
  const canDeleteRow = hasTable && csvRows.length > 1;
  const canDeleteColumn = hasTable && width > 1;
  const ids = [
    "csv-insert-row-above",
    "csv-insert-row-below",
    "csv-insert-column-left",
    "csv-insert-column-right"
  ];
  for (const id of ids) {
    const button = document.getElementById(id);
    if (button) button.disabled = !hasTable;
  }
  const deleteRow = document.getElementById("csv-delete-row");
  const deleteColumn = document.getElementById("csv-delete-column");
  if (deleteRow) deleteRow.disabled = !canDeleteRow;
  if (deleteColumn) deleteColumn.disabled = !canDeleteColumn;
}

function setActiveCsvCell(row, column) {
  const maxRow = Math.max(0, csvRows.length - 1);
  const width = Math.max(1, ...csvRows.map(item => item.length));
  activeCsvCell = {
    row: Math.max(0, Math.min(Number(row) || 0, maxRow)),
    column: Math.max(0, Math.min(Number(column) || 0, width - 1))
  };
  document.querySelectorAll("#csv-table input.active-cell").forEach(input => {
    input.classList.remove("active-cell");
  });
  const active = document.querySelector(`#csv-table input[data-row="${activeCsvCell.row}"][data-column="${activeCsvCell.column}"]`);
  if (active) active.classList.add("active-cell");
  updateCsvActionState();
}

function focusCsvCell(row, column) {
  setActiveCsvCell(row, column);
  const active = document.querySelector(`#csv-table input[data-row="${activeCsvCell.row}"][data-column="${activeCsvCell.column}"]`);
  if (active) active.focus();
}

function selectedCsvRow() {
  return activeCsvCell ? activeCsvCell.row : 0;
}

function selectedCsvColumn() {
  return activeCsvCell ? activeCsvCell.column : 0;
}

function handleCsvInput() {
  recordHistorySnapshot(activeMarkdown);
  activeMarkdown = csvContentFromTable();
  setDirty(activeMarkdown !== currentContent);
}

function renderCsvTable(content, path) {
  csvDelimiter = delimiterForPath(path);
  csvRows = normalizeCsvRows(parseDelimited(content, csvDelimiter));
  const width = Math.max(1, ...csvRows.map(row => row.length));
  if (!activeCsvCell) {
    activeCsvCell = {row: 0, column: 0};
  } else {
    activeCsvCell = {
      row: Math.max(0, Math.min(activeCsvCell.row, csvRows.length - 1)),
      column: Math.max(0, Math.min(activeCsvCell.column, width - 1))
    };
  }
  const table = document.getElementById("csv-table");
  const head = Array.from({length: width}, (_, index) => `<th scope="col">${columnLabel(index)}</th>`).join("");
  const body = csvRows
    .map((row, rowIndex) => {
      const cells = row
        .map((value, columnIndex) => `
          <td>
            <input class="${activeCsvCell.row === rowIndex && activeCsvCell.column === columnIndex ? "active-cell" : ""}" value="${h(value)}" data-row="${rowIndex}" data-column="${columnIndex}" spellcheck="false">
          </td>
        `)
        .join("");
      return `<tr><th scope="row">${rowIndex + 1}</th>${cells}</tr>`;
    })
    .join("");
  table.innerHTML = `<thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody>`;
  table.querySelectorAll("input").forEach(input => {
    input.addEventListener("input", handleCsvInput);
    input.addEventListener("focus", () => setActiveCsvCell(input.dataset.row, input.dataset.column));
    input.addEventListener("click", () => setActiveCsvCell(input.dataset.row, input.dataset.column));
  });
  updateCsvActionState();
}

function insertCsvRow(position) {
  if (editorKindForPath(activeEditorPath()) !== "csv") return;
  recordHistorySnapshot(csvContentFromTable());
  const width = Math.max(1, ...csvRows.map(row => row.length));
  const rowIndex = selectedCsvRow();
  const insertAt = position === "above" ? rowIndex : rowIndex + 1;
  csvRows.splice(insertAt, 0, Array.from({length: width}, () => ""));
  renderCsvTable(serializeDelimited(csvRows, csvDelimiter), activeEditorPath());
  focusCsvCell(insertAt, selectedCsvColumn());
  handleCsvInput();
}

function deleteCsvRow() {
  if (editorKindForPath(activeEditorPath()) !== "csv" || csvRows.length <= 1) return;
  recordHistorySnapshot(csvContentFromTable());
  const rowIndex = selectedCsvRow();
  csvRows.splice(rowIndex, 1);
  renderCsvTable(serializeDelimited(csvRows, csvDelimiter), activeEditorPath());
  focusCsvCell(Math.min(rowIndex, csvRows.length - 1), selectedCsvColumn());
  handleCsvInput();
}

function insertCsvColumn(position) {
  if (editorKindForPath(activeEditorPath()) !== "csv") return;
  recordHistorySnapshot(csvContentFromTable());
  const columnIndex = selectedCsvColumn();
  const insertAt = position === "left" ? columnIndex : columnIndex + 1;
  csvRows = csvRows.map(row => {
    const next = row.slice();
    next.splice(insertAt, 0, "");
    return next;
  });
  renderCsvTable(serializeDelimited(csvRows, csvDelimiter), activeEditorPath());
  focusCsvCell(selectedCsvRow(), insertAt);
  handleCsvInput();
}

function deleteCsvColumn() {
  if (editorKindForPath(activeEditorPath()) !== "csv") return;
  const width = Math.max(1, ...csvRows.map(row => row.length));
  if (width <= 1) return;
  recordHistorySnapshot(csvContentFromTable());
  const columnIndex = selectedCsvColumn();
  csvRows = csvRows.map(row => row.filter((_, index) => index !== columnIndex));
  renderCsvTable(serializeDelimited(csvRows, csvDelimiter), activeEditorPath());
  focusCsvCell(selectedCsvRow(), Math.min(columnIndex, width - 2));
  handleCsvInput();
}

function clearCsvTable() {
  csvRows = [[""]];
  activeCsvCell = null;
  document.getElementById("csv-table").innerHTML = "";
  updateCsvActionState();
}

async function createMilkdown(markdown) {
  const generation = editorGeneration + 1;
  await destroyMilkdown();
  editorGeneration = generation;
  const empty = document.getElementById("editor-empty");
  const root = document.getElementById("milkdown-editor");
  empty.classList.add("hidden");
  root.classList.remove("hidden");
  const next = await Editor.make()
    .config(ctx => {
      ctx.set(rootCtx, root);
      ctx.set(defaultValueCtx, markdown || "");
      ctx.get(listenerCtx).markdownUpdated((ctx, markdownValue, previousMarkdown) => {
        if (editorGeneration !== generation || markdownValue === previousMarkdown) return;
        recordHistorySnapshot(previousMarkdown);
        activeMarkdown = markdownValue;
        setDirty(activeMarkdown !== currentContent);
        updateMarkdownToolbarState(ctx);
      });
      ctx.get(listenerCtx).selectionUpdated(ctx => updateMarkdownToolbarState(ctx));
      ctx.get(listenerCtx).mounted(ctx => updateMarkdownToolbarState(ctx));
    })
    .use(commonmark)
    .use(gfm)
    .use(history)
    .use(listener)
    .create();
  milkdownEditor = next;
  if (editorGeneration !== generation) {
    await next.destroy();
  }
}

async function resetEditor(markdown, path, options = {}) {
  if (!options.preserveHistory) resetEditorHistory();
  unsupportedPath = null;
  unsupportedEntry = null;
  activeMarkdown = markdown || "";
  currentContent = options.baselineContent ?? activeMarkdown;
  const kind = editorKindForPath(path);
  const surface = document.getElementById("editor-surface");
  surface.classList.toggle("has-file", !!path);
  surface.classList.remove("kind-empty", "kind-markdown", "kind-csv", "kind-code", "kind-text", "kind-unsupported");
  surface.classList.add(`kind-${kind}`);
  document.getElementById("unsupported-pane").innerHTML = "";
  document.getElementById("editor-text").disabled = !path || kind === "csv" || kind === "code";
  document.getElementById("editor-text").value = activeMarkdown;
  document.getElementById("editor-empty").classList.toggle("hidden", !!path);
  document.getElementById("milkdown-editor").classList.toggle("hidden", kind !== "markdown");
  
  if (kind === "markdown") {
    destroyCodeEditor();
    clearCsvTable();
    editorMode = options.editorMode === "source" ? "source" : "write";
    await createMilkdown(activeMarkdown);
  } else if (kind === "csv") {
    await destroyMilkdown();
    destroyCodeEditor();
    renderCsvTable(activeMarkdown, path);
    editorMode = "source";
  } else if (kind === "code") {
    await destroyMilkdown();
    clearCsvTable();
    editorMode = "source";
    createCodeEditor(activeMarkdown, path);
  } else if (kind === "text") {
    await destroyMilkdown();
    destroyCodeEditor();
    clearCsvTable();
    editorMode = "source";
  } else {
    await destroyMilkdown();
    destroyCodeEditor();
    clearCsvTable();
    editorMode = "source";
  }
  setEditorMode(editorMode);
  lastDraftContent = activeMarkdown;
  setDirty(Boolean(options.dirty));
}

function shouldShowFile(entry) {
  const path = entry.relative_path || entry.name;
  if (isTrashPath(currentFolder)) return isTrashPath(path);
  if (isTrashPath(path)) return false;
  if (showHidden) return true;
  return !isHiddenPath(path);
}

function renderFileRows(entries) {
  const target = document.getElementById("file-list");
  const rows = [];
  
  // Filter entries based on hidden file toggle
  const filtered = entries.filter(shouldShowFile);
  
  if (currentFolder !== ".") {
    const parent = currentFolder.split("/").slice(0, -1).join("/") || ".";
    rows.push(`
      <button class="file-row parent-folder" type="button" data-folder="${h(parent)}">
        ${icons.folder}
        <span class="file-name">Parent folder</span>
        <span></span>
      </button>
    `);
  }
  
  for (const entry of filtered) {
    const pendingConversion = isConversionPendingEntry(entry);
    const icon = pendingConversion ? '<span class="file-loading" aria-hidden="true"></span>' : getFileIcon(entry);
    const attr = entry.is_dir
      ? `data-folder="${h(entry.relative_path)}"`
      : pendingConversion
        ? 'disabled aria-disabled="true" data-conversion-pending="true"'
        : `data-file="${h(entry.relative_path)}"`;
    const activePath = currentPath || unsupportedPath;
    const active = !entry.is_dir && entry.relative_path === activePath ? "active" : "";
    const status = conversionStatusLabel(entry);
    const size = entry.is_dir ? "" : (status || bytes(entry.size_bytes));
    const title = pendingConversion ? `${entry.name} is being converted to Markdown` : entry.relative_path;
    rows.push(`
      <button class="file-row ${active}${pendingConversion ? " converting" : ""}" type="button" ${attr} data-path="${h(entry.relative_path)}" data-name="${h(entry.name)}" data-kind="${entry.is_dir ? "folder" : "file"}" title="${h(title)}">
        ${icon}
        <span class="file-name">${h(entry.name)}</span>
        <span class="meta">${h(size)}</span>
      </button>
    `);
  }
  
  target.innerHTML = rows.length ? rows.join("") : '<div class="empty-state"><p>No files to display</p></div>';
  
  for (const button of target.querySelectorAll("[data-folder]")) {
    button.onclick = () => loadTree(button.dataset.folder);
  }
  for (const button of target.querySelectorAll("[data-file]")) {
    button.onclick = () => openFile(button.dataset.file);
  }
  for (const button of target.querySelectorAll("[data-path]")) {
    if (button.dataset.conversionPending === "true") continue;
    button.oncontextmenu = event => showItemContextMenu(event, {
      path: button.dataset.path,
      name: button.dataset.name,
      isDir: button.dataset.kind === "folder"
    });
  }
}

async function loadTree(path = currentFolder) {
  try {
    currentFolder = path || ".";
    persistWorkspaceState();
    updateBreadcrumb();
    updateTrashButton();
    const data = await api(`/api/workspace/tree?path=${encodeURIComponent(currentFolder)}&max_entries=500`);
    const entries = (data.entries || []).sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    renderFileRows(entries);
  } catch (error) {
    showError(errorWithPrefix("Failed to load directory", error));
  }
}

async function openTrash() {
  try {
    await api("/api/workspace/folders", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: ".trash"})
    });
    await loadTree(".trash");
  } catch (error) {
    showError(errorWithPrefix("Failed to open trash", error));
  }
}

async function searchFiles(query) {
  if (!query.trim()) {
    await loadTree(currentFolder);
    return;
  }
  try {
    const data = await api(`/api/workspace/search?q=${encodeURIComponent(query)}&max_results=100`);
    const entries = data.entries || (data.matches || []).map(path => ({
      name: path.split("/").pop(),
      relative_path: path,
      is_dir: false,
      size_bytes: 0
    }));
    renderFileRows(entries);
  } catch (error) {
    showError(errorWithPrefix("Search failed", error));
  }
}

function debouncedSearch(query) {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => searchFiles(query), 300);
}

async function latestDraftFor(path, fileMtimeNs) {
  try {
    const data = await api(`/api/workspace/drafts/latest?path=${encodeURIComponent(path)}`);
    const draft = data.draft;
    if (!draft || draft.content === undefined) return null;
    if (fileMtimeNs && !mtimeIsNewer(draft.mtime_ns, fileMtimeNs)) return null;
    return draft;
  } catch (error) {
    setDraftStatus("Draft unavailable", "error");
    return null;
  }
}

async function deleteDraftFor(path) {
  if (!path) return;
  try {
    await api(`/api/workspace/drafts?path=${encodeURIComponent(path)}`, {method: "DELETE"});
    if (path === currentPath) lastDraftContent = "";
  } catch {
    // Draft deletion should not block the user's explicit discard action.
  }
}

async function autosaveDraft(force = false) {
  if (!currentPath || !dirty) return;
  const path = currentPath;
  const content = syncMarkdownFromMode();
  if (!force && content === lastDraftContent) return;
  if (draftInFlight) {
    pendingDraftSave = true;
    return;
  }

  draftInFlight = true;
  setDraftStatus("Saving...", "saving");
  try {
    const data = await api("/api/workspace/drafts", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path, content})
    });
    lastDraftContent = content;
    updateActiveTabState({activeMarkdown: content, lastDraftContent});
    if (path === currentPath) {
      const savedAt = data.draft?.mtime_ns ? mtimeDate(data.draft.mtime_ns) : new Date();
      setDraftStatus(`${formatClock(savedAt)}`, "saved");
    }
  } catch (error) {
    if (path === currentPath) {
      setDraftStatus("Failed", "error");
    }
  } finally {
    draftInFlight = false;
    if (pendingDraftSave) {
      pendingDraftSave = false;
      await autosaveDraft(true);
    }
  }
}

function downloadWorkspacePath(path) {
  if (!path) return;
  const link = document.createElement("a");
  link.href = `/api/workspace/download?path=${encodeURIComponent(path)}`;
  link.download = baseName(path);
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function renderUnsupportedTab() {
  await destroyMilkdown();
  destroyCodeEditor();
  clearCsvTable();
  const path = unsupportedPath;
  currentFolder = folderForPath(path);
  const surface = document.getElementById("editor-surface");
  surface.classList.remove("mode-write", "mode-source", "kind-empty", "kind-markdown", "kind-csv", "kind-code", "kind-text");
  surface.classList.add("mode-source", "kind-unsupported", "has-file");
  document.getElementById("editor-empty").classList.add("hidden");
  document.getElementById("milkdown-editor").classList.add("hidden");
  document.getElementById("editor-text").value = "";
  document.getElementById("editor-text").disabled = true;
  updateEditorTitle();
  const unsupported = document.getElementById("unsupported-pane");
  const size = unsupportedEntry.size_bytes ? `<p class="meta">${h(bytes(unsupportedEntry.size_bytes))}</p>` : "";
  unsupported.innerHTML = `
    <div class="unsupported-card">
      <h3>${h(baseName(path) || path)}</h3>
      <p>This file type cannot be edited in the workspace yet.</p>
      ${size}
      <div class="unsupported-actions">
        <button class="button primary button-with-icon" id="download-unsupported-file" type="button">${iconMarkup("download")}<span>Download</span></button>
        <button class="button secondary button-with-icon" id="copy-unsupported-path" type="button">${iconMarkup("copy")}<span>Copy path</span></button>
      </div>
    </div>
  `;
  document.getElementById("download-unsupported-file").onclick = () => downloadWorkspacePath(path);
  document.getElementById("copy-unsupported-path").onclick = () => copyWorkspacePath(path);
  setDirty(false);
  persistWorkspaceState();
  updateTopbarBreadcrumb();
  renderEditorTabs();
}

async function showUnsupportedFile(path, entry = {}, options = {}) {
  const existing = openTabs.find(tab => tabMatchesPath(tab, path));
  if (existing && !options.forceReload) {
    await activateTab(existing.id);
    return;
  }
  captureActiveTab();
  const tab = createTabState({
    unsupportedPath: path,
    unsupportedEntry: entry || {},
    editorMode: "source"
  });
  openTabs.push(tab);
  activeTabId = tab.id;
  applyTabToGlobals(tab);
  await renderUnsupportedTab();
  await loadTree(currentFolder);
}

async function renderActiveTab(options = {}) {
  const tab = activeTab();
  if (!tab) {
    await clearEditorView({skipTabs: true, skipTree: options.skipTree});
    return;
  }
  applyTabToGlobals(tab);
  if (unsupportedPath) {
    await renderUnsupportedTab();
  } else {
    const path = activeEditorPath();
    updateEditorTitle();
    await resetEditor(activeMarkdown, path, {
      baselineContent: currentContent,
      dirty,
      editorMode,
      preserveHistory: true
    });
    historyStack = [...(tab.historyStack || [])];
    redoStack = [...(tab.redoStack || [])];
    updateUndoRedoButtons();
    if (tab.externalMissing) {
      setDraftStatus("Missing", "error");
    } else if (tab.externalConflict) {
      setDraftStatus("External change", "error");
    }
    persistWorkspaceState();
    updateTopbarBreadcrumb();
    renderEditorTabs();
  }
  if (!options.skipTree) {
    await loadTree(currentFolder);
  }
  if (tab.externalMissing || tab.externalConflict) {
    await promptExternalSaveAs(tab);
  }
}

async function activateTab(tabId, options = {}) {
  const tab = openTabs.find(item => item.id === tabId);
  if (!tab) return;
  if (activeTabId === tabId && !options.forceRender) {
    renderEditorTabs();
    return;
  }
  captureActiveTab();
  activeTabId = tab.id;
  await renderActiveTab(options);
}

async function clearEditorView(options = {}) {
  currentPath = null;
  untitledPath = null;
  currentMtimeNs = null;
  currentContent = "";
  activeMarkdown = "";
  unsupportedPath = null;
  unsupportedEntry = null;
  dirty = false;
  lastDraftContent = "";
  if (!options.skipTabs || !openTabs.length) activeTabId = null;
  updateEditorTitle();
  document.getElementById("draft-status").style.display = "none";
  await resetEditor("", null);
  persistWorkspaceState();
  updateTopbarBreadcrumb();
  renderEditorTabs();
  if (!options.skipTree) {
    await loadTree(currentFolder);
  }
}

async function closeTab(tabId = activeTabId) {
  if (!tabId) return;
  if (tabId === activeTabId) captureActiveTab();
  const index = openTabs.findIndex(tab => tab.id === tabId);
  if (index < 0) return;
  const tab = openTabs[index];
  if (tab.dirty && !confirm(`Discard unsaved changes to ${tabTitle(tab)}?`)) return;
  await deleteDraftFor(tab.currentPath);
  openTabs.splice(index, 1);
  if (tabId !== activeTabId) {
    renderEditorTabs();
    return;
  }
  const next = openTabs[Math.min(index, openTabs.length - 1)] || null;
  if (next) {
    activeTabId = next.id;
    await renderActiveTab();
  } else {
    await clearEditorView();
  }
}

async function refreshFileListForCurrentFilter() {
  const query = document.getElementById("file-search")?.value || "";
  if (query.trim()) {
    await searchFiles(query);
  } else {
    await loadTree(currentFolder);
  }
}

async function promptExternalSaveAs(tab) {
  if (!tab || !tab.dirty || tab.externalPrompted) return;
  tab.externalPrompted = true;
  if (tab.id !== activeTabId) return;
  const message = tab.externalMissing
    ? `${tabTitle(tab)} was deleted, moved, or renamed outside the editor. Save your edits as a new file?`
    : `${tabTitle(tab)} changed outside the editor. Save your edits as a new file?`;
  showError(tab.externalMissing ? "Open file no longer exists. Use Save As to keep your edits." : "Open file changed externally. Use Save As to keep your edits.");
  if (confirm(message)) {
    await saveFileAs();
  }
}

async function markTabMissing(tab) {
  tab.externalMissing = true;
  tab.externalConflict = true;
  if (!tab.externalNoticeShown) {
    tab.externalNoticeShown = true;
    showError(`${tabTitle(tab)} was deleted, moved, or renamed outside the editor.`);
  }
  if (tab.dirty) {
    if (tab.id === activeTabId) {
      setDraftStatus("Missing", "error");
      await promptExternalSaveAs(tab);
    }
    return;
  }
  const wasActive = tab.id === activeTabId;
  const index = openTabs.findIndex(item => item.id === tab.id);
  if (index >= 0) openTabs.splice(index, 1);
  if (wasActive) {
    const next = openTabs[Math.min(index, openTabs.length - 1)] || null;
    if (next) {
      activeTabId = next.id;
      await renderActiveTab();
    } else {
      await clearEditorView();
    }
  } else {
    renderEditorTabs();
  }
}

async function reloadCleanTab(tab, metadata) {
  const data = await api(`/api/workspace/file?path=${encodeURIComponent(tab.currentPath)}`);
  tab.currentMtimeNs = data.mtime_ns || metadata.mtime_ns;
  tab.currentContent = data.content || "";
  tab.activeMarkdown = tab.currentContent;
  tab.lastDraftContent = tab.currentContent;
  tab.dirty = false;
  tab.externalConflict = false;
  tab.externalMissing = false;
  tab.externalNoticeShown = false;
  tab.externalPrompted = false;
  if (tab.id === activeTabId) {
    applyTabToGlobals(tab);
    await renderActiveTab({skipTree: true});
  }
}

async function checkOpenTabsForExternalChanges() {
  if (externalCheckInFlight) return;
  externalCheckInFlight = true;
  try {
    captureActiveTab();
    for (const tab of [...openTabs]) {
      const path = tab.currentPath || tab.unsupportedPath;
      if (!path) continue;
      let metadata;
      try {
        const data = await api(`/api/workspace/metadata?path=${encodeURIComponent(path)}`);
        metadata = data.item;
      } catch (error) {
        if (/not found|404/i.test(humanReadableError(error))) {
          await markTabMissing(tab);
        }
        continue;
      }
      if (!tab.currentPath || !metadata?.mtime_ns || String(metadata.mtime_ns) === String(tab.currentMtimeNs || "")) {
        continue;
      }
      if (tab.dirty) {
        tab.externalConflict = true;
        tab.externalMissing = false;
        if (!tab.externalNoticeShown) {
          tab.externalNoticeShown = true;
          showError(`${tabTitle(tab)} changed outside the editor.`);
        }
        if (tab.id === activeTabId) {
          setDraftStatus("External change", "error");
          await promptExternalSaveAs(tab);
        }
      } else {
        await reloadCleanTab(tab, metadata);
        showSuccess(`${tabTitle(tab)} reloaded after external changes`);
      }
    }
    renderEditorTabs();
  } finally {
    externalCheckInFlight = false;
  }
}

async function pollWorkspaceVersion() {
  if (workspaceVersionInFlight) return;
  workspaceVersionInFlight = true;
  try {
    const data = await api("/api/workspace/version");
    const version = data.version || "";
    if (lastWorkspaceVersion === null) {
      lastWorkspaceVersion = version;
      return;
    }
    if (version && version !== lastWorkspaceVersion) {
      lastWorkspaceVersion = version;
      await refreshFileListForCurrentFilter();
      await checkOpenTabsForExternalChanges();
    }
  } catch {
    // The manual refresh button remains available if polling is temporarily unavailable.
  } finally {
    workspaceVersionInFlight = false;
  }
}

function startWorkspaceVersionPolling() {
  if (workspaceVersionPollTimer) clearInterval(workspaceVersionPollTimer);
  pollWorkspaceVersion();
  workspaceVersionPollTimer = setInterval(pollWorkspaceVersion, WORKSPACE_VERSION_POLL_MS);
}

function startAutosave() {
  if (draftTimer) clearInterval(draftTimer);
  draftTimer = setInterval(() => {
    autosaveDraft();
  }, AUTOSAVE_INTERVAL_MS);
}

async function loadFolderChoices() {
  try {
    const data = await api("/api/workspace/tree?path=.&recursive=true&max_entries=1000");
    const folders = new Set(["."]);
    if (shouldShowFolderPath(currentFolder)) folders.add(currentFolder);
    for (const entry of data.entries || []) {
      if (entry.is_dir && shouldShowFolderPath(entry.relative_path)) folders.add(entry.relative_path);
    }
    return Array.from(folders).filter(Boolean).sort((a, b) => {
      if (a === ".") return -1;
      if (b === ".") return 1;
      return a.localeCompare(b);
    });
  } catch (error) {
    showError(errorWithPrefix("Failed to load folders", error));
    return [currentFolder || "."];
  }
}

function openWorkspaceDialog({title, submitLabel, fields}) {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "workspace-modal";
    const fieldHtml = fields.map(field => {
      const value = h(field.value || "");
      if (field.type === "select") {
        const options = (field.options || []).map(option => {
          const selected = option === field.value ? " selected" : "";
          const label = option === "." ? "Workspace" : option;
          return `<option value="${h(option)}"${selected}>${h(label)}</option>`;
        }).join("");
        return `
          <label class="workspace-field">
            <span>${h(field.label)}</span>
            <select name="${h(field.id)}">${options}</select>
          </label>
        `;
      }
      return `
        <label class="workspace-field">
          <span>${h(field.label)}</span>
          <input name="${h(field.id)}" value="${value}" placeholder="${h(field.placeholder || "")}" autocomplete="off">
        </label>
      `;
    }).join("");
    overlay.innerHTML = `
      <form class="workspace-modal-card">
        <h3>${h(title)}</h3>
        <div class="workspace-modal-fields">${fieldHtml}</div>
        <div class="workspace-modal-actions">
          <button class="button secondary" type="button" data-cancel>Cancel</button>
          <button class="button primary" type="submit">${h(submitLabel)}</button>
        </div>
      </form>
    `;

    const finish = value => {
      overlay.remove();
      resolve(value);
    };
    overlay.addEventListener("mousedown", event => {
      if (event.target === overlay) finish(null);
    });
    overlay.querySelector("[data-cancel]").onclick = () => finish(null);
    overlay.querySelector("form").onsubmit = event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const result = {};
      for (const field of fields) {
        result[field.id] = fmt(data.get(field.id)).trim();
      }
      finish(result);
    };
    document.body.appendChild(overlay);
    const firstInput = overlay.querySelector("input, select");
    if (firstInput) {
      firstInput.focus();
      if (firstInput.select) firstInput.select();
    }
  });
}

async function openEntryDialog({title, submitLabel, defaultFolder = currentFolder, defaultName = "", nameLabel = "Name"}) {
  const folders = await loadFolderChoices();
  const visibleDefaultFolder = shouldShowFolderPath(defaultFolder) ? defaultFolder : ".";
  if (!folders.includes(visibleDefaultFolder)) folders.push(visibleDefaultFolder);
  const result = await openWorkspaceDialog({
    title,
    submitLabel,
    fields: [
      {id: "folder", label: "Folder", type: "select", value: visibleDefaultFolder || ".", options: folders},
      {id: "name", label: nameLabel, value: defaultName}
    ]
  });
  if (!result) return null;
  const name = cleanEntryName(result.name);
  if (!name) return null;
  return {
    folder: result.folder || ".",
    name,
    path: joinPath(result.folder || ".", name)
  };
}

async function openNameDialog({title, submitLabel, defaultName, nameLabel = "Name"}) {
  const result = await openWorkspaceDialog({
    title,
    submitLabel,
    fields: [{id: "name", label: nameLabel, value: defaultName}]
  });
  if (!result) return null;
  return cleanEntryName(result.name);
}

async function openFolderDialog({title, submitLabel, defaultFolder = currentFolder}) {
  const folders = await loadFolderChoices();
  const visibleDefaultFolder = shouldShowFolderPath(defaultFolder) ? defaultFolder : ".";
  if (!folders.includes(visibleDefaultFolder)) folders.push(visibleDefaultFolder);
  const result = await openWorkspaceDialog({
    title,
    submitLabel,
    fields: [{id: "folder", label: "Folder", type: "select", value: visibleDefaultFolder || ".", options: folders}]
  });
  return result ? result.folder || "." : null;
}

async function openRunScriptDialog(path) {
  const result = await openWorkspaceDialog({
    title: "Run Script",
    submitLabel: "Run",
    fields: [
      {id: "command", label: "Command", value: defaultCommandForPath(path), placeholder: "python script.py"},
      {id: "workdir", label: "Workdir", value: folderForPath(path) || "."},
      {id: "timeout", label: "Timeout Seconds", value: "300"}
    ]
  });
  if (!result) return null;
  const commandText = fmt(result.command).trim();
  if (!commandText) {
    showError("Enter a command");
    return null;
  }
  let command;
  try {
    command = parseCommandLine(commandText);
  } catch (error) {
    showError(error);
    return null;
  }
  if (!command.length) {
    showError("Enter a command");
    return null;
  }
  const timeout = Number(result.timeout || 300);
  return {
    command,
    workdir: result.workdir || folderForPath(path) || ".",
    timeout_seconds: Number.isFinite(timeout) && timeout > 0 ? Math.floor(timeout) : 300
  };
}

async function runScript(path = currentPath) {
  if (!path || !isRunnableScriptPath(path)) {
    showError("Open a Python, shell, JavaScript, Ruby, or PHP script first");
    return;
  }
  if (path === currentPath && dirty) {
    if (!confirm("Save changes before running this script?")) return;
    await saveFile();
    if (dirty) return;
  }
  const run = await openRunScriptDialog(path);
  if (!run) return;
  const button = document.getElementById("run-active-file");
  const previousHtml = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = `${iconMarkup("run")}<span>Running...</span>`;
  }
  try {
    const data = await api("/api/workspace/script-runs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        path,
        command: run.command,
        workdir: run.workdir,
        timeout_seconds: run.timeout_seconds
      })
    });
    const outputPath = data.file?.relative_path;
    if (outputPath) {
      await loadTree(folderForPath(outputPath));
      await openFile(outputPath, {skipConfirm: true, restoreDraft: false});
    }
    const exitCode = data.result?.exit_code;
    if (data.error) {
      showError("Script run saved with sandbox error");
    } else if (exitCode === 0) {
      showSuccess("Script completed");
    } else {
      showError(`Script exited with code ${exitCode}`);
    }
  } catch (error) {
    showError(errorWithPrefix("Failed to run script", error));
  } finally {
    if (button && previousHtml) button.innerHTML = previousHtml;
    updateRunButtonState();
  }
}

async function openFile(path, options = {}) {
  const existing = openTabs.find(tab => tabMatchesPath(tab, path));
  if (existing && !options.forceReload) {
    await activateTab(existing.id);
    return;
  }
  if (!isEditableTextPath(path)) {
    if (isConvertibleDocumentPath(path)) {
      try {
        const data = await api(`/api/workspace/metadata?path=${encodeURIComponent(path)}`);
        if (isConversionPendingEntry(data.item)) {
          showSuccess("Converting to Markdown");
          await loadTree(folderForPath(path));
          return;
        }
      } catch {
        // Fall through to the normal unsupported path handling; it will surface not-found errors.
      }
    }
    await showUnsupportedFile(path, {}, options);
    return;
  }
  captureActiveTab();
  try {
    const data = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`);
    const relativePath = data.relative_path;
    const mtimeNs = data.mtime_ns;
    const fileContent = data.content || "";
    const draft = options.restoreDraft === false ? null : await latestDraftFor(relativePath, mtimeNs);
    const restoredDraft = Boolean(draft && draft.content !== fileContent);
    const tab = createTabState({
      currentPath: relativePath,
      currentMtimeNs: mtimeNs,
      currentContent: fileContent,
      activeMarkdown: restoredDraft ? draft.content : fileContent,
      dirty: restoredDraft,
      editorMode: initialEditorModeForPath(relativePath),
      lastDraftContent: restoredDraft ? draft.content : fileContent
    });
    openTabs.push(tab);
    activeTabId = tab.id;
    applyTabToGlobals(tab);
    await renderActiveTab({skipTree: true});
    persistWorkspaceState();
    updateTopbarBreadcrumb();
    if (restoredDraft) {
      lastDraftContent = draft.content;
      setDraftStatus(`${formatClock(mtimeDate(draft.mtime_ns))}`, "saved");
      showSuccess("Restored autosaved draft");
    }
    await loadTree(currentFolder);
  } catch (error) {
    if (options.clearStoredPath) {
      writeStoredValue(STORAGE_KEYS.file, null);
      currentPath = null;
      persistWorkspaceState();
    }
    showError(errorWithPrefix("Failed to open file", error));
  }
}

async function saveFile() {
  if (!hasActiveDocument()) return;
  if (!currentPath) {
    await saveFileAs();
    return;
  }
  const tab = activeTab();
  if (tab?.externalConflict || tab?.externalMissing) {
    showError("This file changed outside the editor. Save As to keep your edits.");
    await saveFileAs();
    return;
  }
  try {
    const content = syncMarkdownFromMode();
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
    activeMarkdown = content;
    lastDraftContent = content;
    updateActiveTabState({
      currentMtimeNs,
      currentContent,
      activeMarkdown,
      lastDraftContent,
      externalConflict: false,
      externalMissing: false,
      externalNoticeShown: false
    });
    setDirty(false);
    await deleteDraftFor(currentPath);
    await loadTree(currentFolder);
    showSuccess("File saved");
  } catch (error) {
    if (/changed since it was opened|not found|path not found|409/i.test(humanReadableError(error))) {
      updateActiveTabState({externalConflict: true, externalNoticeShown: true});
      showError("The file changed outside the editor. Save As to keep your edits.");
      await saveFileAs();
    } else {
      showError(errorWithPrefix("Failed to save", error));
    }
  }
}

async function saveFileAs() {
  if (!hasActiveDocument()) return;
  const path = activeEditorPath();
  const sourcePath = currentPath;
  const choice = await openEntryDialog({
    title: currentPath ? "Save As" : "Save File",
    submitLabel: "Save",
    defaultFolder: folderForPath(path) || currentFolder,
    defaultName: currentPath ? duplicateName(currentPath, false) : (baseName(path) || "Untitled.md"),
    nameLabel: "File name"
  });
  if (!choice) return;
  if (!isEditableTextPath(choice.path)) {
    showError("Choose a text, Markdown, CSV, or code file extension");
    return;
  }
  try {
    const content = syncMarkdownFromMode();
    const data = await api("/api/workspace/file", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        path: choice.path,
        content,
        create_only: true
      })
    });
    currentPath = data.file.relative_path;
    untitledPath = null;
    currentMtimeNs = data.file.mtime_ns;
    currentContent = content;
    activeMarkdown = content;
    lastDraftContent = content;
    currentFolder = folderForPath(currentPath);
    updateEditorTitle();
    await resetEditor(content, currentPath, {
      baselineContent: content,
      dirty: false,
      editorMode: initialEditorModeForPath(currentPath)
    });
    updateActiveTabState({
      currentPath,
      untitledPath,
      currentMtimeNs,
      currentContent,
      activeMarkdown,
      lastDraftContent,
      externalConflict: false,
      externalMissing: false,
      externalNoticeShown: false,
      externalPrompted: false
    });
    setDirty(false);
    await deleteDraftFor(sourcePath);
    persistWorkspaceState();
    updateTopbarBreadcrumb();
    await loadTree(currentFolder);
    showSuccess("File saved");
  } catch (error) {
    showError(errorWithPrefix("Failed to save", error));
  }
}

async function startNewFile(folder = currentFolder) {
  captureActiveTab();
  currentFolder = folder || ".";
  currentPath = null;
  untitledPath = joinPath(currentFolder, "Untitled.md");
  unsupportedPath = null;
  unsupportedEntry = null;
  currentMtimeNs = null;
  currentContent = "";
  activeMarkdown = "";
  const tab = createTabState({
    untitledPath,
    currentContent: "",
    activeMarkdown: "",
    dirty: true,
    editorMode: "write"
  });
  openTabs.push(tab);
  activeTabId = tab.id;
  applyTabToGlobals(tab);
  updateEditorTitle();
  await resetEditor("", untitledPath, {baselineContent: "", dirty: true});
  persistWorkspaceState();
  updateTopbarBreadcrumb();
  renderEditorTabs();
  await loadTree(currentFolder);
}

async function createFolder(folder = currentFolder) {
  const choice = await openEntryDialog({
    title: "New Folder",
    submitLabel: "Create",
    defaultFolder: folder || ".",
    defaultName: "New folder",
    nameLabel: "Folder name"
  });
  if (!choice) return;
  try {
    await api("/api/workspace/folders", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: choice.path})
    });
    await loadTree(choice.folder);
    showSuccess("Folder created");
  } catch (error) {
    showError(errorWithPrefix("Failed to create folder", error));
  }
}

function applyPathChange(source, destination, item = null) {
  for (const tab of openTabs) {
    if (tab.currentPath && pathContains(source, tab.currentPath)) {
      tab.currentPath = replacePathPrefix(tab.currentPath, source, destination);
      tab.externalConflict = false;
      tab.externalMissing = false;
      tab.externalNoticeShown = false;
      tab.externalPrompted = false;
      if (item && !item.is_dir && tab.currentPath === item.relative_path) {
        tab.currentMtimeNs = item.mtime_ns;
      }
    }
    if (tab.untitledPath && pathContains(source, tab.untitledPath)) {
      tab.untitledPath = replacePathPrefix(tab.untitledPath, source, destination);
    }
    if (tab.unsupportedPath && pathContains(source, tab.unsupportedPath)) {
      tab.unsupportedPath = replacePathPrefix(tab.unsupportedPath, source, destination);
    }
  }
  if (currentPath && pathContains(source, currentPath)) {
    currentPath = replacePathPrefix(currentPath, source, destination);
    if (item && !item.is_dir && currentPath === item.relative_path) {
      currentMtimeNs = item.mtime_ns;
    }
    updateEditorTitle();
  }
  if (untitledPath && pathContains(source, untitledPath)) {
    untitledPath = replacePathPrefix(untitledPath, source, destination);
    updateEditorTitle();
  }
  if (unsupportedPath && pathContains(source, unsupportedPath)) {
    unsupportedPath = replacePathPrefix(unsupportedPath, source, destination);
    updateEditorTitle();
    const download = document.getElementById("download-unsupported-file");
    const copy = document.getElementById("copy-unsupported-path");
    if (download) download.onclick = () => downloadWorkspacePath(unsupportedPath);
    if (copy) copy.onclick = () => copyWorkspacePath(unsupportedPath);
  }
  if (currentFolder && pathContains(source, currentFolder)) {
    currentFolder = replacePathPrefix(currentFolder, source, destination);
  }
  persistWorkspaceState();
  updateTopbarBreadcrumb();
  renderEditorTabs();
}

async function movePath(source, destination, successMessage = "Moved") {
  if (source === destination) return;
  try {
    const data = await api("/api/workspace/path", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_path: source, destination_path: destination})
    });
    applyPathChange(source, data.item.relative_path, data.item);
    await loadTree(currentFolder);
    showSuccess(successMessage);
  } catch (error) {
    showError(errorWithPrefix("Failed to move", error));
  }
}

async function renameItem(path, isDir) {
  const name = await openNameDialog({
    title: "Rename",
    submitLabel: "Rename",
    defaultName: baseName(path),
    nameLabel: isDir ? "Folder name" : "File name"
  });
  if (!name) return;
  await movePath(path, joinPath(folderForPath(path), name), "Renamed");
}

async function moveItem(path) {
  const folder = await openFolderDialog({
    title: "Move To",
    submitLabel: "Move",
    defaultFolder: folderForPath(path)
  });
  if (!folder) return;
  await movePath(path, joinPath(folder, baseName(path)), "Moved");
}

async function duplicateItem(path, isDir) {
  const choice = await openEntryDialog({
    title: "Duplicate",
    submitLabel: "Duplicate",
    defaultFolder: folderForPath(path),
    defaultName: duplicateName(path, isDir),
    nameLabel: isDir ? "Folder name" : "File name"
  });
  if (!choice) return;
  try {
    await api("/api/workspace/copy", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_path: path, destination_path: choice.path})
    });
    await loadTree(currentFolder);
    showSuccess("Duplicated");
  } catch (error) {
    showError(errorWithPrefix("Failed to duplicate", error));
  }
}

async function deleteItem(path, isDir) {
  captureActiveTab();
  const affectedTabs = openTabs.filter(tab => pathContains(path, tabPath(tab)));
  const activeAffected = affectedTabs.some(tab => tab.id === activeTabId);
  const dirtyWarning = affectedTabs.some(tab => tab.dirty) ? " This will discard unsaved editor changes in open tabs." : "";
  const label = isDir ? "folder" : "file";
  const permanent = isTrashPath(path);
  if (!confirm(`${permanent ? "Permanently delete" : "Move"} this ${label}${permanent ? "" : " to trash"}?${dirtyWarning}`)) return;
  try {
    const result = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, {method: "DELETE"});
    if (affectedTabs.length) {
      openTabs = openTabs.filter(tab => !pathContains(path, tabPath(tab)));
      if (activeAffected) {
        const next = openTabs[0] || null;
        if (next) {
          activeTabId = next.id;
          await renderActiveTab({skipTree: true});
        } else {
          await clearEditorView({skipTree: true});
        }
      } else {
        renderEditorTabs();
      }
    }
    if (currentFolder && pathContains(path, currentFolder)) {
      currentFolder = folderForPath(path);
    }
    await loadTree(currentFolder);
    showSuccess(result.permanent ? "Deleted" : "Moved to trash");
  } catch (error) {
    showError(errorWithPrefix("Failed to delete", error));
  }
}

async function uploadFilesToFolder(files, folder = currentFolder) {
  const selectedFiles = Array.from(files || []);
  if (!selectedFiles.length) return;
  let uploaded = 0;
  const failures = [];
  for (const file of selectedFiles) {
    const relative = fmt(file.webkitRelativePath || file.name).replace(/^\/+|\/+$/g, "");
    if (!relative) continue;
    const targetPath = joinPath(folder || ".", relative);
    try {
      await api(`/api/workspace/upload?path=${encodeURIComponent(targetPath)}`, {
        method: "PUT",
        body: file
      });
      uploaded += 1;
    } catch (error) {
      failures.push(`${relative}: ${humanReadableError(error)}`);
    }
  }
  await loadTree(folder || currentFolder);
  if (uploaded) showSuccess(`Uploaded ${uploaded} ${uploaded === 1 ? "item" : "items"}`);
  if (failures.length) showError(`Upload failed: ${failures.slice(0, 2).join("; ")}`);
}

function chooseUploadFiles(folder = currentFolder) {
  const input = document.getElementById("upload-file-input");
  input.value = "";
  input.onchange = () => uploadFilesToFolder(input.files, folder);
  input.click();
}

function chooseUploadFolder(folder = currentFolder) {
  const input = document.getElementById("upload-folder-input");
  input.value = "";
  input.onchange = () => uploadFilesToFolder(input.files, folder);
  input.click();
}

async function zipItem(path) {
  try {
    const data = await api("/api/workspace/zip", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path})
    });
    await loadTree(folderForPath(data.file?.relative_path || path));
    showSuccess("ZIP created");
  } catch (error) {
    showError(errorWithPrefix("Failed to zip", error));
  }
}

async function unzipItem(path) {
  try {
    const data = await api("/api/workspace/unzip", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path})
    });
    await loadTree(data.folder?.relative_path || folderForPath(path));
    showSuccess("Extracted");
  } catch (error) {
    showError(errorWithPrefix("Failed to unzip", error));
  }
}

async function convertItem(path, outputFormat) {
  if (!path || !isWorkspaceConvertiblePath(path)) {
    showError("Choose a supported text, Markdown, PDF, or Office file");
    return;
  }
  if (path === currentPath && dirty) {
    if (!confirm("Save changes before converting this file?")) return;
    await saveFile();
    if (dirty) return;
  }
  const deleteOriginal = outputFormat === "markdown"
    ? confirm("Would you like to delete the original after conversion?")
    : false;
  try {
    const data = await api("/api/workspace/convert", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        path,
        output_format: outputFormat,
        delete_original: deleteOriginal
      })
    });
    const outputPath = data.file?.relative_path;
    await loadTree(folderForPath(outputPath || path));
    if (outputPath && isEditableTextPath(outputPath)) {
      await openFile(outputPath, {skipConfirm: true, restoreDraft: false});
    }
    showSuccess(`Created ${baseName(outputPath || "converted file")}`);
  } catch (error) {
    showError(errorWithPrefix("Failed to convert", error));
  }
}

async function copyWorkspacePath(path) {
  try {
    await navigator.clipboard.writeText(path);
    showSuccess("Path copied");
  } catch {
    showError("Clipboard unavailable");
  }
}

function updateLayoutClasses() {
  const layout = document.querySelector(".workspace-layout");
  layout.classList.toggle("sidebar-hidden", sidebarCollapsed);
  layout.classList.toggle("chat-hidden", chatCollapsed);
}

function toggleSidebar() {
  sidebarCollapsed = !sidebarCollapsed;
  writeStoredValue(STORAGE_KEYS.sidebarCollapsed, String(sidebarCollapsed));
  
  const sidebar = document.getElementById("workspace-sidebar");
  const expandBtn = document.getElementById("sidebar-expand");
  const toggleBtn = document.getElementById("toggle-sidebar");
  
  if (sidebarCollapsed) {
    sidebar.classList.add("collapsed");
    expandBtn.style.display = "flex";
    toggleBtn.textContent = "›";
    toggleBtn.title = "Show sidebar";
  } else {
    sidebar.classList.remove("collapsed");
    expandBtn.style.display = "none";
    toggleBtn.textContent = "‹";
    toggleBtn.title = "Close sidebar";
  }
  updateLayoutClasses();
}

function toggleChat() {
  chatCollapsed = !chatCollapsed;
  writeStoredValue(STORAGE_KEYS.chatCollapsed, String(chatCollapsed));
  
  const chat = document.getElementById("chat-pane");
  const expandBtn = document.getElementById("chat-expand");
  const toggleBtn = document.getElementById("toggle-chat");
  
  if (chatCollapsed) {
    chat.classList.add("collapsed");
    expandBtn.style.display = "flex";
    toggleBtn.textContent = "‹";
    toggleBtn.title = "Show chat";
  } else {
    chat.classList.remove("collapsed");
    expandBtn.style.display = "none";
    toggleBtn.textContent = "›";
    toggleBtn.title = "Close chat";
  }
  updateLayoutClasses();
}

function hideContextMenu() {
  document.querySelectorAll(".workspace-context-menu").forEach(menu => menu.remove());
}

function showContextMenu(event, items) {
  event.preventDefault();
  event.stopPropagation();
  hideContextMenu();
  const menu = document.createElement("div");
  menu.className = "workspace-context-menu";
  for (const item of items) {
    if (item.separator) {
      const separator = document.createElement("div");
      separator.className = "workspace-context-separator";
      menu.appendChild(separator);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.innerHTML = `${item.icon ? iconMarkup(item.icon) : ""}<span>${h(item.label)}</span>`;
    if (item.danger) button.classList.add("danger");
    button.onclick = () => {
      hideContextMenu();
      item.action();
    };
    menu.appendChild(button);
  }
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
  const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
  menu.style.left = `${Math.max(left, 8)}px`;
  menu.style.top = `${Math.max(top, 8)}px`;
}

function showItemContextMenu(event, item) {
  const openAction = item.isDir ? () => loadTree(item.path) : () => openFile(item.path);
  const deleteLabel = isTrashPath(item.path) ? "Delete permanently" : "Move to trash";
  const actions = [
    {label: item.isDir ? "Open folder" : "Open", icon: "open", action: openAction}
  ];
  if (!item.isDir && isRunnableScriptPath(item.path)) {
    actions.push({label: "Run", icon: "run", action: () => runScript(item.path)});
  }
  if (item.isDir) {
    actions.push(
      {label: "New file here", icon: "newFile", action: () => startNewFile(item.path)},
      {label: "New folder here", icon: "newFolder", action: () => createFolder(item.path)},
      {label: "Upload files here", icon: "upload", action: () => chooseUploadFiles(item.path)},
      {label: "Upload folder here", icon: "uploadFolder", action: () => chooseUploadFolder(item.path)}
    );
  }
  if (!item.isDir && /\.zip$/i.test(item.path)) {
    actions.push({label: "Extract here", icon: "unzip", action: () => unzipItem(item.path)});
  }
  if (!item.isDir && isWorkspaceConvertiblePath(item.path)) {
    actions.push(
      {separator: true},
      {label: "Convert to Markdown", icon: "convert", action: () => convertItem(item.path, "markdown")},
      {label: "Convert to PDF", icon: "convert", action: () => convertItem(item.path, "pdf")},
      {label: "Convert to HTML", icon: "convert", action: () => convertItem(item.path, "html")},
      {label: "Convert to DOCX", icon: "convert", action: () => convertItem(item.path, "docx")}
    );
  }
  actions.push(
    {separator: true},
    {label: "Download", icon: "download", action: () => downloadWorkspacePath(item.path)},
    {label: "Compress to ZIP", icon: "zip", action: () => zipItem(item.path)},
    {separator: true},
    {label: "Rename", icon: "rename", action: () => renameItem(item.path, item.isDir)},
    {label: "Move to...", icon: "move", action: () => moveItem(item.path)},
    {label: "Duplicate", icon: "copy", action: () => duplicateItem(item.path, item.isDir)},
    {label: "Copy path", icon: "copy", action: () => copyWorkspacePath(item.path)},
    {separator: true},
    {label: deleteLabel, icon: "trash", danger: true, action: () => deleteItem(item.path, item.isDir)}
  );
  showContextMenu(event, actions);
}

function showFolderBackgroundContextMenu(event, folder) {
  showContextMenu(event, [
    {label: "New file", icon: "newFile", action: () => startNewFile(folder)},
    {label: "New folder", icon: "newFolder", action: () => createFolder(folder)},
    {label: "Upload files", icon: "upload", action: () => chooseUploadFiles(folder)},
    {label: "Upload folder", icon: "uploadFolder", action: () => chooseUploadFolder(folder)},
    {separator: true},
    {label: "Download folder", icon: "download", action: () => downloadWorkspacePath(folder)},
    {label: "Copy folder path", icon: "copy", action: () => copyWorkspacePath(folder)}
  ]);
}

function showError(message) {
  const toast = document.createElement("div");
  toast.className = "toast toast-error";
  toast.textContent = humanReadableError(message);
  document.body.appendChild(toast);
  setTimeout(() => toast.classList.add("show"), 10);
  setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function showSuccess(message) {
  const toast = document.createElement("div");
  toast.className = "toast toast-success";
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.classList.add("show"), 10);
  setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 300);
  }, 2000);
}

function isProcessingStatus(status) {
  return ["queued", "running", "waiting"].includes(status);
}

function friendlyStatus(status) {
  return fmt(status || "unknown").replace(/_/g, " ");
}

function formatRelativeTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function workspaceJobMetadata(job) {
  return job?.metadata || {};
}

function workspaceJobTitle(job) {
  const metadata = workspaceJobMetadata(job);
  return metadata.workspace_message || job?.task_summary || job?.trigger_subject || `Job #${job?.id || ""}`;
}

function switchWorkspaceChatTab(tab) {
  workspaceChatTab = tab === "chat" ? "chat" : "jobs";
  document.getElementById("jobs-tab").classList.toggle("active", workspaceChatTab === "jobs");
  document.getElementById("chat-tab").classList.toggle("active", workspaceChatTab === "chat");
  document.getElementById("workspace-jobs-panel").classList.toggle("hidden", workspaceChatTab !== "jobs");
  document.getElementById("workspace-chat-panel").classList.toggle("hidden", workspaceChatTab !== "chat");
  updateWorkspaceChatStatus(selectedWorkspaceJob);
}

function dashboardJobUrl(jobId) {
  return `/admin?job=${encodeURIComponent(jobId)}`;
}

function updateWorkspaceChatStatus(job = selectedWorkspaceJob) {
  const target = document.getElementById("chat-status");
  if (!target) return;
  if (workspaceChatTab === "jobs") {
    target.textContent = "";
    return;
  }
  if (!job) {
    target.textContent = "Ready";
    return;
  }
  target.innerHTML = `<a class="chat-status-job-link" href="${h(dashboardJobUrl(job.id))}">Job #${h(job.id)}</a> · ${h(friendlyStatus(job.status))}`;
}

function renderWorkspaceJobs() {
  const list = document.getElementById("workspace-job-list");
  if (!workspaceJobs.length) {
    list.innerHTML = '<div class="empty-state compact"><p>No workspace jobs yet</p></div>';
    return;
  }
  list.innerHTML = workspaceJobs
    .map(job => {
      const metadata = workspaceJobMetadata(job);
      const active = String(job.id) === String(selectedWorkspaceJobId) ? "active" : "";
      const path = metadata.workspace_active_path ? `<span class="workspace-job-path">${h(metadata.workspace_active_path)}</span>` : "";
      return `
        <button class="workspace-job-row ${active}" type="button" data-job-id="${h(job.id)}">
          <span class="workspace-job-main">
            <strong>${h(workspaceJobTitle(job))}</strong>
            ${path}
          </span>
          <span class="workspace-job-meta">
            <span class="job-status ${h(job.status)}">${h(friendlyStatus(job.status))}</span>
            <span>${h(formatRelativeTime(job.updated_at || job.created_at))}</span>
          </span>
        </button>
      `;
    })
    .join("");
  list.querySelectorAll("[data-job-id]").forEach(button => {
    button.onclick = () => selectWorkspaceJob(button.dataset.jobId);
  });
}

async function loadWorkspaceJobs(options = {}) {
  try {
    const data = await api("/api/workspace/jobs?limit=100");
    workspaceJobs = data.jobs || [];
    if (options.selectLatest && workspaceJobs.length) {
      selectedWorkspaceJobId = workspaceJobs[0].id;
      selectedWorkspaceJob = workspaceJobs[0];
      writeStoredValue(STORAGE_KEYS.selectedJob, selectedWorkspaceJobId);
    }
    if (selectedWorkspaceJobId && !workspaceJobs.some(job => String(job.id) === String(selectedWorkspaceJobId))) {
      selectedWorkspaceJobId = null;
      selectedWorkspaceJob = null;
      writeStoredValue(STORAGE_KEYS.selectedJob, null);
    } else if (selectedWorkspaceJobId) {
      selectedWorkspaceJob = workspaceJobs.find(job => String(job.id) === String(selectedWorkspaceJobId)) || selectedWorkspaceJob;
    }
    renderWorkspaceJobs();
    updateWorkspaceChatStatus(selectedWorkspaceJob);
  } catch (error) {
    document.getElementById("workspace-job-list").innerHTML = `<div class="empty-state compact"><p>${h(humanReadableError(error))}</p></div>`;
  }
}

function jobEventLabel(log) {
  if (log.tool_name) {
    return log.tool_action ? `${log.tool_name}: ${log.tool_action}` : log.tool_name;
  }
  return friendlyStatus(log.event_type);
}

function jobEventText(log) {
  const output = log.output_data || {};
  const input = log.input_data || {};
  if (typeof output.message === "string") return output.message;
  if (typeof output.summary === "string") return output.summary;
  if (typeof output.text === "string") return output.text;
  if (typeof input.message === "string") return input.message;
  if (typeof input.summary === "string") return input.summary;
  return "";
}

function chatMarkdown(value) {
  const rendered = renderMarkdown(value);
  return `<div class="chat-markdown">${rendered || "<p>(empty)</p>"}</div>`;
}

function renderPinnedUserMessage(job, metadata, activePath) {
  return `
    <div class="chat-pinned-message">
      <div class="chat-message user">
        <div class="chat-message-head">
          <strong>Original request</strong>
          <time>${h(formatRelativeTime(job.created_at))}</time>
        </div>
        ${chatMarkdown(metadata.workspace_message || job.task_summary || "")}
        ${activePath ? `<p class="chat-path">File: ${h(activePath)}</p>` : ""}
      </div>
    </div>
  `;
}

function jobEventKey(log) {
  return fmt(log.id || log.sequence || `${log.created_at || ""}-${jobEventLabel(log)}`);
}

function renderJobEvent(log, openEventKeys = new Set()) {
  const text = jobEventText(log);
  const label = jobEventLabel(log);
  const time = formatRelativeTime(log.created_at);
  if (!text) {
    return `
      <div class="chat-event-row">
        <span>${h(label)}</span>
        <time>${h(time)}</time>
      </div>
    `;
  }
  return `
    <div class="chat-event-row has-body">
      <div class="chat-event-head">
        <span>${h(label)}</span>
        <time>${h(time)}</time>
      </div>
      ${chatMarkdown(text)}
    </div>
  `;
}

function updateWorkingIndicator(processing) {
  const indicator = document.getElementById("chat-working-indicator");
  if (!indicator) return;
  indicator.classList.toggle("hidden", !processing);
}

function renderWorkspaceJobChat(detail = null) {
  const feed = document.getElementById("chat-feed");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("send-job-message");
  const openFileButton = document.getElementById("open-job-file");
  if (!detail?.job) {
    selectedWorkspaceJob = null;
    feed.innerHTML = '<div class="chat-message meta">Start a new job or select one from the job list.</div>';
    chatRenderState = {jobId: null, messageCount: 0};
    updateWorkingIndicator(false);
    input.disabled = false;
    input.placeholder = "Start a workspace job";
    send.disabled = false;
    openFileButton.classList.add("hidden");
    updateWorkspaceChatStatus(null);
    return;
  }

  const job = detail.job;
  const metadata = workspaceJobMetadata(job);
  selectedWorkspaceJob = job;
  const processing = isProcessingStatus(job.status);
  const finalResponse = metadata.final_response || "";
  const activePath = metadata.workspace_active_path || "";
  const previousState = chatRenderState;
  const eventCount = (detail.logs || []).length + (finalResponse || job.last_error ? 1 : 0);
  const shouldScroll = String(previousState.jobId) !== String(job.id) || eventCount > previousState.messageCount;
  const openEventKeys = new Set(
    Array.from(feed.querySelectorAll(".chat-event-row[open][data-event-key]"))
      .map(row => row.dataset.eventKey)
      .filter(Boolean)
  );
  // Skip full re-render if job and event count unchanged (preserves details toggle state)
  if (String(previousState.jobId) === String(job.id) && eventCount === previousState.messageCount && !shouldScroll) {
    // Just update status-related UI without touching feed DOM
    updateWorkingIndicator(processing);
    input.disabled = processing;
    input.placeholder = processing ? "This job is processing" : "Reply to this job";
    send.disabled = processing;
    openFileButton.classList.toggle("hidden", !activePath);
    if (activePath) openFileButton.onclick = () => openFile(activePath);
    updateWorkspaceChatStatus(job);
    return;
  }
  const messages = [renderPinnedUserMessage(job, metadata, activePath)];
  for (const log of detail.logs || []) {
    messages.push(renderJobEvent(log, openEventKeys));
  }
  if (finalResponse) {
    messages.push(`
      <div class="chat-message agent">
        <div class="chat-message-head">
          <strong>Agent</strong>
          <time>${h(formatRelativeTime(job.completed_at || job.updated_at))}</time>
        </div>
        ${chatMarkdown(finalResponse)}
      </div>
    `);
  } else if (job.last_error) {
    messages.push(`
      <div class="chat-message system error">
        <div class="chat-message-head">
          <strong>System</strong>
          <time>${h(formatRelativeTime(job.updated_at))}</time>
        </div>
        ${chatMarkdown(job.last_error)}
      </div>
    `);
  } else if (!processing) {
    messages.push(`
      <div class="chat-message system">
        <div class="chat-message-head">
          <strong>System</strong>
          <time>${h(formatRelativeTime(job.updated_at))}</time>
        </div>
        <p>Job ${h(friendlyStatus(job.status))}.</p>
      </div>
    `);
  }
  feed.innerHTML = messages.join("");
  if (shouldScroll) feed.scrollTop = feed.scrollHeight;
  chatRenderState = {jobId: job.id, messageCount: eventCount};
  updateWorkingIndicator(processing);
  input.disabled = processing;
  input.placeholder = processing ? "This job is processing" : "Reply to this job";
  send.disabled = processing;
  openFileButton.classList.toggle("hidden", !activePath);
  if (activePath) {
    openFileButton.onclick = () => openFile(activePath);
  }
  updateWorkspaceChatStatus(job);
}

async function loadWorkspaceJobDetail(jobId = selectedWorkspaceJobId) {
  if (!jobId) {
    renderWorkspaceJobChat(null);
    return;
  }
  try {
    const detail = await api(`/api/workspace/jobs/${encodeURIComponent(jobId)}`);
    selectedWorkspaceJobId = detail.job.id;
    writeStoredValue(STORAGE_KEYS.selectedJob, selectedWorkspaceJobId);
    renderWorkspaceJobChat(detail);
    renderWorkspaceJobs();
  } catch (error) {
    selectedWorkspaceJobId = null;
    selectedWorkspaceJob = null;
    writeStoredValue(STORAGE_KEYS.selectedJob, null);
    renderWorkspaceJobChat(null);
    showError(errorWithPrefix("Failed to load job", error));
  }
}

async function selectWorkspaceJob(jobId) {
  selectedWorkspaceJobId = jobId;
  selectedWorkspaceJob = workspaceJobs.find(job => String(job.id) === String(jobId)) || null;
  writeStoredValue(STORAGE_KEYS.selectedJob, selectedWorkspaceJobId);
  switchWorkspaceChatTab("chat");
  renderWorkspaceJobs();
  await loadWorkspaceJobDetail(jobId);
}

function startNewWorkspaceJob() {
  selectedWorkspaceJobId = null;
  selectedWorkspaceJob = null;
  writeStoredValue(STORAGE_KEYS.selectedJob, null);
  switchWorkspaceChatTab("chat");
  document.getElementById("chat-input").value = "";
  renderWorkspaceJobChat(null);
  renderWorkspaceJobs();
}

function workspaceJobPayload(message) {
  const path = currentPath;
  const includeActiveFile = Boolean(document.getElementById("include-active-file").checked && path);
  const includeFileContent = Boolean(document.getElementById("include-file-content").checked && path);
  const payload = {
    message: message.trim(),
    active_path: includeActiveFile ? path : null,
    include_active_file: includeActiveFile,
    include_file_content: includeFileContent,
    file_content: null
  };
  if (includeFileContent) {
    const content = syncMarkdownFromMode();
    payload.file_content = content.length <= 25000 ? content : null;
  }
  return payload;
}

async function submitWorkspaceJobMessage(message) {
  const trimmed = message.trim();
  if (!trimmed) return;
  const selectedProcessing = selectedWorkspaceJob && isProcessingStatus(selectedWorkspaceJob.status);
  if (selectedProcessing) {
    showError("This job is still processing. Start a new job from the job list.");
    return;
  }
  const endpoint = selectedWorkspaceJobId ? `/api/workspace/jobs/${encodeURIComponent(selectedWorkspaceJobId)}/messages` : "/api/workspace/jobs";
  const data = await api(endpoint, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(workspaceJobPayload(trimmed))
  });
  selectedWorkspaceJobId = data.job.id;
  writeStoredValue(STORAGE_KEYS.selectedJob, selectedWorkspaceJobId);
  document.getElementById("chat-input").value = "";
  switchWorkspaceChatTab("chat");
  await loadWorkspaceJobs();
  await loadWorkspaceJobDetail(selectedWorkspaceJobId);
}

function startWorkspaceJobPolling() {
  if (workspaceJobPollTimer) clearInterval(workspaceJobPollTimer);
  workspaceJobPollTimer = setInterval(async () => {
    await loadWorkspaceJobs();
    if (selectedWorkspaceJobId) {
      await loadWorkspaceJobDetail(selectedWorkspaceJobId);
    }
  }, WORKSPACE_JOB_POLL_MS);
}

function bindEvents() {
  setupActionIcons();
  bindMarkdownToolbarEvents();
  // Load preferences from localStorage
  showHidden = readStoredValue(STORAGE_KEYS.showHidden) === "true";
  syncShowHiddenButton();
  
  sidebarCollapsed = readStoredValue(STORAGE_KEYS.sidebarCollapsed) === "true";
  chatCollapsed = readStoredValue(STORAGE_KEYS.chatCollapsed) === "true";
  
  if (sidebarCollapsed) {
    document.getElementById("workspace-sidebar").classList.add("collapsed");
    document.getElementById("sidebar-expand").style.display = "flex";
    document.getElementById("toggle-sidebar").textContent = "›";
  }
  
  if (chatCollapsed) {
    document.getElementById("chat-pane").classList.add("collapsed");
    document.getElementById("chat-expand").style.display = "flex";
    document.getElementById("toggle-chat").textContent = "‹";
  }
  
  updateLayoutClasses();
  
  document.getElementById("show-hidden").onclick = () => {
    showHidden = !showHidden;
    writeStoredValue(STORAGE_KEYS.showHidden, String(showHidden));
    syncShowHiddenButton();
    loadTree(currentFolder);
  };
  
  document.getElementById("new-file").onclick = () => startNewFile(currentFolder);
  document.getElementById("new-folder").onclick = () => createFolder(currentFolder);
  document.getElementById("upload-files").onclick = () => chooseUploadFiles(currentFolder);
  document.getElementById("upload-folder").onclick = () => chooseUploadFolder(currentFolder);
  document.getElementById("open-trash").onclick = openTrash;
  document.getElementById("refresh-tree").onclick = () => loadTree(currentFolder);
  document.getElementById("file-search").oninput = (event) => debouncedSearch(event.target.value);
  document.getElementById("editor-text").oninput = (event) => {
    recordHistorySnapshot(activeMarkdown);
    activeMarkdown = event.target.value;
    setDirty(activeMarkdown !== currentContent);
  };
  document.getElementById("undo-action").onclick = undoEditorChange;
  document.getElementById("redo-action").onclick = redoEditorChange;
  document.getElementById("run-active-file").onclick = () => runScript(currentPath);
  document.getElementById("save-file").onclick = saveFile;
  document.getElementById("save-as-file").onclick = saveFileAs;
  document.getElementById("csv-insert-row-above").onclick = () => insertCsvRow("above");
  document.getElementById("csv-insert-row-below").onclick = () => insertCsvRow("below");
  document.getElementById("csv-delete-row").onclick = deleteCsvRow;
  document.getElementById("csv-insert-column-left").onclick = () => insertCsvColumn("left");
  document.getElementById("csv-insert-column-right").onclick = () => insertCsvColumn("right");
  document.getElementById("csv-delete-column").onclick = deleteCsvColumn;
  document.getElementById("mode-write").onclick = () => setEditorMode("write");
  document.getElementById("mode-source").onclick = () => setEditorMode("source");
  document.getElementById("toggle-sidebar").onclick = toggleSidebar;
  document.getElementById("sidebar-expand").onclick = toggleSidebar;
  document.getElementById("toggle-chat").onclick = toggleChat;
  document.getElementById("chat-expand").onclick = toggleChat;
  document.getElementById("file-list").oncontextmenu = event => {
    if (event.target.closest(".file-row")) return;
    showFolderBackgroundContextMenu(event, currentFolder);
  };
  document.getElementById("jobs-tab").onclick = () => switchWorkspaceChatTab("jobs");
  document.getElementById("chat-tab").onclick = () => switchWorkspaceChatTab("chat");
  document.getElementById("new-workspace-job").onclick = startNewWorkspaceJob;
  document.addEventListener("click", hideContextMenu);
  
  startAutosave();
  
  // Keyboard shortcuts for editor actions.
  document.addEventListener("keydown", (event) => {
    const shortcut = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (shortcut && key === "s") {
      event.preventDefault();
      if (event.shiftKey && currentPath) {
        saveFileAs();
      } else if (hasActiveDocument() && dirty) {
        saveFile();
      }
    } else if (shortcut && key === "z") {
      event.preventDefault();
      if (event.shiftKey) {
        redoEditorChange();
      } else {
        undoEditorChange();
      }
    } else if (shortcut && key === "y") {
      event.preventDefault();
      redoEditorChange();
    } else if (event.key === "Escape") {
      hideContextMenu();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      autosaveDraft(true);
    }
  });
  
  document.getElementById("chat-form").onsubmit = async event => {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const message = input.value;
    try {
      await submitWorkspaceJobMessage(message);
    } catch (error) {
      showError(errorWithPrefix("Failed to send message", error));
    }
  };
}

async function boot() {
  bindEvents();
  selectedWorkspaceJobId = readStoredValue(STORAGE_KEYS.selectedJob);
  const urlState = readUrlState();
  const restoredFile = urlState.file || readStoredValue(STORAGE_KEYS.file);
  const restoredFolder = urlState.folder || (restoredFile ? folderForPath(restoredFile) : readStoredValue(STORAGE_KEYS.folder, "."));
  if (!restoredFile) {
    setEditorMode("write");
  }
  updateBreadcrumb();
  updateTopbarBreadcrumb();
  setDraftStatus("No draft", "idle");
  updateContextControls();
  renderWorkspaceJobChat(null);
  await loadWorkspaceJobs();
  if (selectedWorkspaceJobId) {
    await loadWorkspaceJobDetail(selectedWorkspaceJobId);
  }
  startWorkspaceJobPolling();
  try {
    await loadTree(restoredFolder);
    if (restoredFile) {
      await openFile(restoredFile, {skipConfirm: true, clearStoredPath: true});
    }
    startWorkspaceVersionPolling();
  } catch (error) {
    document.getElementById("file-list").innerHTML = `<div class="empty-state">${h(humanReadableError(error))}</div>`;
    startWorkspaceVersionPolling();
  }
}

boot();
