import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: true });

export function renderMarkdown(text) {
  const html = marked.parse(String(text || ""));
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
