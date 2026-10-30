"""DNS-based sender authentication for email claiming to come from the admin.

An email passes when either check passes for a domain aligned with its single
From address:

- DKIM: a DKIM-Signature verifies against the signer's public key in DNS.
- SPF: the connecting IP, taken from the topmost Received header that records
  one (added by the receiving mail server, above anything the sender supplied),
  is authorized by the SPF record of the Return-Path domain.

This check can only reject mail; a pass never grants admin trust (admin actions
still require an emailed OTP).
"""

import email
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import getaddresses
from typing import Any, Optional

from .config import AppConfig


LOGGER = logging.getLogger("assistant.email_authentication")
DNS_TIMEOUT_SECONDS = 10
MAX_DKIM_SIGNATURES = 5
_IP_TOKEN = re.compile(r"[\[(]\s*(?:IPv6:)?([0-9A-Fa-f:.]+)\s*[\])]")
_HELO_TOKEN = re.compile(r"\bhelo\s*=\s*\S+", re.IGNORECASE)


@dataclass(frozen=True)
class AuthenticationVerdict:
    passed: bool
    reason: str
    from_domain: str = ""
    results: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason, "from_domain": self.from_domain, "results": dict(self.results)}


def authentication_enabled(config: AppConfig) -> bool:
    return config.get_bool("agent.security.email_authentication.enabled", True)


def _domain(value: str) -> str:
    value = str(value or "").strip().strip("<>").lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    return value.strip(".")


def aligned(domain: str, from_domain: str) -> bool:
    """Relaxed DMARC-style alignment, approximated without a public-suffix list."""
    domain, from_domain = _domain(domain), _domain(from_domain)
    if not domain or not from_domain:
        return False
    return (
        domain == from_domain
        or domain.endswith("." + from_domain)
        or (from_domain.endswith("." + domain) and "." in domain)
    )


def _single_from_domain(message: Message) -> str:
    headers = message.get_all("From", [])
    addresses = [address for _, address in getaddresses(headers) if address]
    if len(headers) != 1 or len(addresses) != 1:
        return ""
    return _domain(addresses[0])


def _global_ip(value: str) -> Optional[str]:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address) if address.is_global else None


def connecting_hop(message: Message) -> tuple[Optional[str], str]:
    """Return (IP, HELO) from the topmost Received header whose from-clause records a public IP."""
    for raw in message.get_all("Received", []) or []:
        text = " ".join(str(raw).split())
        match = re.match(r"\s*from\s+(.*?)(?:\s+by\s+|;|$)", text, re.IGNORECASE)
        if not match:
            continue
        clause = match.group(1)
        helo = clause.split()[0] if clause.split() else ""
        # The HELO name is sender-controlled; only the IP the server observed counts.
        ips = [ip for ip in (_global_ip(token) for token in _IP_TOKEN.findall(_HELO_TOKEN.sub(" ", clause))) if ip]
        if ips:
            return ips[-1], helo.strip("[]")
    return None, ""


def _dkim_txt(name: bytes, timeout: int = DNS_TIMEOUT_SECONDS) -> Optional[bytes]:
    from dkim.dnsplug import get_txt

    return get_txt(name, timeout=timeout)


def _check_dkim(raw_bytes: bytes, message: Message, from_domain: str) -> tuple[bool, str]:
    import dkim

    count = min(len(message.get_all("DKIM-Signature", []) or []), MAX_DKIM_SIGNATURES)
    if not count:
        return False, "none"
    outcome = "fail"
    for index in range(count):
        verifier = dkim.DKIM(raw_bytes, timeout=DNS_TIMEOUT_SECONDS)
        try:
            valid = verifier.verify(index, dnsfunc=_dkim_txt)
        except Exception as exc:
            LOGGER.info("DKIM verification error: %s", exc)
            outcome = "temperror" if "dns" in type(exc).__name__.lower() else outcome
            continue
        signer = (verifier.domain or b"").decode("ascii", errors="replace")
        if valid and aligned(signer, from_domain):
            return True, "pass (d=%s)" % signer
        if valid:
            outcome = "pass, not aligned (d=%s)" % signer
    return False, outcome


def _check_spf(message: Message, from_domain: str) -> tuple[bool, str]:
    import spf

    return_paths = message.get_all("Return-Path", []) or []
    mail_from = next((address for _, address in getaddresses(return_paths[:1]) if address), "")
    if not mail_from:
        return False, "none (no Return-Path)"
    if not aligned(mail_from, from_domain):
        return False, "not aligned (Return-Path %s)" % _domain(mail_from)
    ip, helo = connecting_hop(message)
    if not ip:
        return False, "none (no connecting IP)"
    try:
        result, _explanation = spf.check2(i=ip, s=mail_from, h=helo or "unknown", timeout=DNS_TIMEOUT_SECONDS)
    except Exception as exc:
        return False, "temperror (%s)" % exc
    return result == "pass", "%s (ip=%s, domain=%s)" % (result, ip, _domain(mail_from))


def admin_domain_warning(config: AppConfig) -> Optional[str]:
    """Best-effort startup check that the admin domain publishes SPF or DMARC; DKIM selectors cannot be discovered."""
    domain = _domain(str(config.get("agent.admin.email") or ""))
    if not domain:
        return "admin email authentication is enabled but ADMIN_EMAIL is not set"
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5.0
    found = []
    for name, prefix in ((domain, "v=spf1"), ("_dmarc." + domain, "v=dmarc1")):
        try:
            records = [b"".join(item.strings).decode("utf-8", "replace") for item in resolver.resolve(name, "TXT")]
        except dns.resolver.NXDOMAIN:
            records = []
        except Exception as exc:
            return "could not look up DNS records for %s (%s); check that the container can reach DNS" % (domain, exc)
        found.extend(record for record in records if record.lower().startswith(prefix))
    if not found:
        return "%s publishes no SPF or DMARC record; admin email will pass only with an aligned DKIM signature" % domain
    return None


def evaluate(raw_bytes: bytes, config: AppConfig) -> AuthenticationVerdict:
    message = email.message_from_bytes(raw_bytes)
    from_domain = _single_from_domain(message)
    if not from_domain:
        return AuthenticationVerdict(False, "message must have exactly one From address")
    dkim_passed, dkim_result = _check_dkim(raw_bytes, message, from_domain)
    results = {"dkim": dkim_result}
    if dkim_passed:
        return AuthenticationVerdict(True, "aligned DKIM signature verified", from_domain, results)
    spf_passed, spf_result = _check_spf(message, from_domain)
    results["spf"] = spf_result
    if spf_passed:
        return AuthenticationVerdict(True, "aligned SPF passed", from_domain, results)
    return AuthenticationVerdict(False, "neither aligned DKIM nor aligned SPF passed", from_domain, results)
