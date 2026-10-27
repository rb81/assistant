import re
from email.utils import parseaddr
from html import escape
from typing import Iterable, Optional

from .config import AppConfig, agent_email


DEFAULT_ORG_NAME = "Acme Inc."
HTML_DISCLOSURE_START = "<!-- assistant-ai-disclosure:start -->"
HTML_DISCLOSURE_END = "<!-- assistant-ai-disclosure:end -->"

DISCLOSURE_SENTENCE_RE = re.compile(
    r"^This\s+email\s+was\s+sent\s+by\s+a\s+semi-autonomous\s+AI\s+agent\s+created\s+by\s+.{1,80}?\.\s+"
    r"If\s+you\s+have\s+any\s+concerns\s+or\s+questions,\s+please\s+email\s+"
    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\.?$",
    re.IGNORECASE,
)
HTML_DISCLOSURE_MARKER_RE = re.compile(
    r"<!--\s*assistant-ai-disclosure:start\s*-->.*?<!--\s*assistant-ai-disclosure:end\s*-->",
    re.IGNORECASE | re.DOTALL,
)
HTML_DISCLOSURE_CLASS_RE = re.compile(
    r"""<div\b[^>]*class=["'][^"']*\bassistant-ai-disclosure\b[^"']*["'][^>]*>.*?</div>""",
    re.IGNORECASE | re.DOTALL,
)
HTML_DISCLOSURE_CONTAINER_RE = re.compile(
    r"""<(p|div|span)\b[^>]*>\s*(?:--\s*(?:<br\s*/?>)?\s*)?This\s+email\s+was\s+sent\s+by\s+a\s+semi-autonomous\s+AI\s+agent\s+created\s+by\s+.{1,120}?If\s+you\s+have\s+any\s+concerns\s+or\s+questions,\s+please\s+email\s+[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\.?\s*</\1>""",
    re.IGNORECASE | re.DOTALL,
)


def org_name(config: AppConfig) -> str:
    return str(config.get("agent.org.name", DEFAULT_ORG_NAME) or DEFAULT_ORG_NAME).strip() or DEFAULT_ORG_NAME


def disclosure_contact_email(config: AppConfig) -> str:
    candidates = [
        config.get("agent.org.security_email"),
        config.get("agent.admin.email"),
        agent_email(config),
    ]
    for candidate in candidates:
        address = parseaddr(str(candidate or "").strip())[1]
        if address:
            return address
    return ""


def disclosure_text(config: AppConfig) -> str:
    contact = disclosure_contact_email(config)
    base = "This email was sent by a semi-autonomous AI agent created by %s." % org_name(config)
    if contact:
        return "%s If you have any concerns or questions, please email %s." % (base, contact)
    return base


def disclosure_required_for_recipients(recipients: Iterable[str], config: AppConfig) -> bool:
    recipient_domains = [email_domain(recipient) for recipient in recipients]
    recipient_domains = [domain for domain in recipient_domains if domain]
    if not recipient_domains:
        return False

    internal_domains = [clean_domain(domain) for domain in config.get_list("agent.org.internal_email_domains")]
    internal_domains = [domain for domain in internal_domains if domain]
    if not internal_domains:
        return True

    return any(not domain_is_internal(domain, internal_domains) for domain in recipient_domains)


def append_disclosure_text(body: str, config: AppConfig) -> str:
    clean_body = strip_disclosure_text(body)
    footer = disclosure_text(config)
    if not footer:
        return clean_body
    if clean_body.strip():
        return "%s\n\n-- \n%s" % (clean_body.rstrip(), footer)
    return footer


def append_disclosure_html(html_body: str, config: AppConfig) -> str:
    clean_html = strip_disclosure_html(html_body)
    footer = disclosure_text(config)
    if not footer:
        return clean_html
    block = (
        "\n%s\n"
        '    <div class="assistant-ai-disclosure" style="margin-top: 16px; padding-top: 12px; '
        'border-top: 1px solid #d1d5db; color: #6b7280; font-size: 12px; line-height: 1.4;">'
        "<p>%s</p></div>\n%s\n"
    ) % (HTML_DISCLOSURE_START, escape(footer), HTML_DISCLOSURE_END)
    if re.search(r"</body\s*>", clean_html, flags=re.IGNORECASE):
        return re.sub(r"</body\s*>", "%s  </body>" % block, clean_html, count=1, flags=re.IGNORECASE)
    return "%s%s" % (clean_html.rstrip(), block)


def strip_disclosure_text(body: str) -> str:
    if not body:
        return body

    lines = str(body).splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        current = disclosure_candidate_line(lines[index])
        next_line: Optional[str] = None
        if index + 1 < len(lines):
            next_line = disclosure_candidate_line(lines[index + 1])

        if current == "--" and next_line and disclosure_line_matches(next_line):
            index += 2
            continue
        if disclosure_line_matches(current):
            index += 1
            continue

        kept.append(lines[index])
        index += 1
    return trim_excess_blank_lines("\n".join(kept))


def strip_disclosure_html(html_body: str) -> str:
    if not html_body:
        return html_body
    clean = HTML_DISCLOSURE_MARKER_RE.sub("", str(html_body))
    clean = HTML_DISCLOSURE_CLASS_RE.sub("", clean)
    clean = HTML_DISCLOSURE_CONTAINER_RE.sub("", clean)
    return clean.strip()


def email_domain(address: str) -> str:
    parsed = parseaddr(str(address or "").strip())[1]
    if "@" not in parsed:
        return ""
    return clean_domain(parsed.rsplit("@", 1)[-1])


def clean_domain(value: str) -> str:
    return str(value or "").strip().lower().lstrip("@.")


def domain_is_internal(domain: str, internal_domains: Iterable[str]) -> bool:
    clean = clean_domain(domain)
    return any(clean == internal or clean.endswith(".%s" % internal) for internal in internal_domains)


def disclosure_candidate_line(line: str) -> str:
    clean = str(line or "").strip()
    while clean.startswith(">") or clean.startswith("|"):
        clean = clean[1:].strip()
    return clean


def disclosure_line_matches(line: str) -> bool:
    return bool(DISCLOSURE_SENTENCE_RE.fullmatch(line.strip()))


def trim_excess_blank_lines(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value).strip()
