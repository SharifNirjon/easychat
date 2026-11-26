"""Guardrails around the model (roadmap step 6). Pure functions and classes: no I/O, no settings.

The model is an untrusted component: anything it reads (visitor messages, crawled pages) can try
to steer it. Real protection is structural (RLS, code-built blocks, server-side history, see
CLAUDE.md). These guards are the second wall, and they make failures visible:

Input side (before the model):
  clean_message()    removes invisible characters that can hide instructions (zero-width,
                     Unicode "tag" characters, bidi overrides) and normalises look-alike forms.
  injection_flags()  names patterns that look like an attempt to override the rules. Flags never
                     block a message (too many false positives); they add a reminder to the
                     system prompt and are stored with the message for later review.

Output side (while the reply streams):
  OutputGuard        holds back the last few characters of the stream and checks the reply so
                     far. On a violation the stream stops and main.py swaps in a safe reply
                     (SSE `replace` event). Violations:
    prompt_leak          the per-request canary or our prompt's wording appears
    order_claim          without a verified order: any order status / tracking claim (the
                         assistant can't see orders, so it is invented). With one (step 8,
                         order_tracking.py): any order or tracking number other than that order's
    ungrounded_contact   an email or phone number that isn't in the knowledge, the tenant's
                         config or the conversation: invented or planted contact details
    ungrounded_link      a link to a host that isn't the tenant's own
    promise_claim        "your refund has been approved", "I've issued a refund", "your return
                         label was sent": the team decides on requests (step 9), never the model
"""
import re
import secrets
import unicodedata
from urllib.parse import urlsplit

# --- Violation and flag names (stored in messages.guard_flags) ---------------------------------

PROMPT_LEAK = "prompt_leak"
ORDER_CLAIM = "order_claim"
UNGROUNDED_CONTACT = "ungrounded_contact"
UNGROUNDED_LINK = "ungrounded_link"
PROMISE_CLAIM = "promise_claim"

# What the visitor sees instead of a blocked reply ({company} = the tenant's company name).
SAFE_REPLIES = {
    PROMPT_LEAK: "Sorry, I can't share that. I'm happy to help with anything about {company}.",
    ORDER_CLAIM: "I can't look up orders in this chat, so I can't tell you about a specific order. "
                 "Our team can check it for you.",
    UNGROUNDED_CONTACT: "Sorry, I don't have verified details for that. Our team can help.",
    UNGROUNDED_LINK: "Sorry, I don't have a verified link for that. Our team can help.",
    PROMISE_CLAIM: "I can't approve returns or refunds myself: the {company} team decides, and they'll reply "
                   "by email. If you haven't sent a request yet, tell me you'd like to start a return.",
}


# ORDER_CLAIM's replacement when the tenant has order tracking (step 8): point to the safe way in.
ORDER_CLAIM_TRACKABLE = ("I can only share order details after a quick check. Type \"track my order\" and "
                         "I'll email a code to the address you used at checkout.")


def safe_reply(violation: str, company: str, orders_enabled: bool = False) -> str:
    """The replacement text for a reply the output guard blocked."""
    if violation == ORDER_CLAIM and orders_enabled:
        return ORDER_CLAIM_TRACKABLE
    return SAFE_REPLIES[violation].format(company=company)

# Characters removed from visitor messages. Kept on purpose: ZWJ/ZWNJ (U+200C/D: emoji and
# Bengali/Hindi/Persian spelling) and LRM/RLM (U+200E/F: right-to-left text).
_INVISIBLE = re.compile(
    "[­​⁠-⁤﻿"     # soft hyphen, zero-width space, word joiners, BOM
    "‪-‮"                        # bidi embeddings/overrides ("Trojan Source")
    "\U000e0000-\U000e007f]"               # Unicode tag characters ("ASCII smuggling")
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")    # C0/C1 controls except \t \n \r


def clean_message(text: str) -> str:
    """Normalise a visitor message (NFKC: full-width and look-alike forms to plain ones) and drop
    invisible and control characters, so the model sees exactly what a human reader would."""
    text = unicodedata.normalize("NFKC", text)
    return _CONTROL.sub("", _INVISIBLE.sub("", text)).strip()


# --- Input: injection flags --------------------------------------------------------------------

_FLAGS = re.IGNORECASE | re.DOTALL
INJECTION_PATTERNS: dict[str, re.Pattern] = {
    # "ignore your previous instructions", "forget all the rules", "bypass your guidelines"
    "override": re.compile(r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,40}?"
                           r"\b(instructions?|rules|prompts?|guidelines|directions|programming|restrictions)\b", _FLAGS),
    # "what's your system prompt", "repeat the text above", "show me your instructions"
    "prompt_request": re.compile(r"\b(system|initial|hidden|original|developer|secret)\s+(prompt|instructions?|message)\b"
                                 r"|\b(repeat|print|show|reveal|output|display|share|tell me|give me|summari[sz]e|translate)\b"
                                 r".{0,30}?\b(your|the)\s+(instructions|prompt|rules|guidelines)\b"
                                 r"|\b(text|words|everything|message)\s+(above|before this)\b", _FLAGS),
    # "you are now DAN", "pretend you are", "developer mode"
    "persona": re.compile(r"\b(you are now|from now on,? you|pretend (to be|you are|you're)|role-?play as"
                          r"|developer mode|jailbreak|unrestricted mode|no restrictions)\b", _FLAGS),
    # fake chat-format markers: "system:", "<|im_start|>", "[INST]", our own fence tags
    "fake_role": re.compile(r"(^|\n)\s*(system|assistant|developer)\s*:|<\|\s*(im_start|im_end|system|endoftext)"
                            r"|\[/?INST\]|<\s*/?\s*(knowledge|system|instructions)\s*>", _FLAGS),
    # the same "ignore the instructions" in Spanish, French, German and Bengali
    "foreign_override": re.compile(r"\b(ignora|ignorez|ignoriere|olvida|oublie)\b.{0,30}?"
                                   r"\b(instrucciones|instructions|anweisungen|regeln|reglas|consignes)\b"
                                   r"|নির্দেশ.{0,20}?(উপেক্ষা|ভুলে)", _FLAGS),
}


def injection_flags(text: str) -> list[str]:
    """Names of the injection patterns this message matches (empty for ordinary messages)."""
    return [name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(text)]


# --- Output: detectors (also used by the eval suite) -------------------------------------------

def new_canary() -> str:
    """A random marker for one request's system prompt. Seeing it in a reply proves a leak."""
    return f"ref-{secrets.token_hex(5)}"


_ORDER_STATUS = re.compile(
    r"\b(your|the|this) (order|package|parcel|shipment|delivery)\b(?:\s*#?\s*[A-Z0-9-]{3,})?\s+"
    r"(?:has been|has|is|was|got)\s+(?:now\s+|already\s+|currently\s+|just\s+)?"
    r"(shipped|delivered|dispatched|out for delivery|on (?:its|the) way|in transit|being processed|processing"
    r"|packed|delayed|cancell?ed|lost|held)\b", re.IGNORECASE)
_ORDER_NUMBER_STATUS = re.compile(r"\border\s*(?:number\s*)?#\s*[A-Z0-9-]{3,}\s+(?:has been|has|is|was)\b", re.IGNORECASE)
_TRACKING = re.compile(r"\btracking\s+(?:number|code|id|no\.?)\s*(?:is|:)\s*[A-Z0-9]{6,}", re.IGNORECASE)
_LOOKED_UP = re.compile(r"\b(I|we)(?:'ve| have)?\s+(?:just\s+)?(checked|looked up|found|located|tracked|pulled up)\s+"
                        r"(?:on\s+)?(your|the)\s+(order|package|parcel|shipment|account)\b", re.IGNORECASE)
# Decisions only the team makes (step 9): approving a return or refund, or sending a return label.
# Possessive or first-person only ("your refund", "I've refunded"): policy text ("refunds are issued
# within 5 days") stays allowed, and conditions ("once your return is approved") are skipped below.
_PROMISE = re.compile(
    r"\byour\s+(?:return|refund|exchange|replacement)(?:\s+request)?\s+(?:has\s+been|is|was|got)\s+(?:now\s+|already\s+)?"
    r"(?:approved|accepted|confirmed|granted|processed|issued|complete[d]?)\b"
    r"|\b(?:I|we)(?:'ve|\s+have)?\s+(?:just\s+|already\s+)?(?:approved|issued|processed|refunded|credited)\s+"
    r"(?:your|the|a|you)\b"
    r"|\byour\s+(?:return|shipping|prepaid)\s+label\s+(?:has\s+been|is|was)\s+(?:now\s+|already\s+)?"
    r"(?:sent|emailed|created|attached|on\s+its\s+way|ready)\b", re.IGNORECASE)
# A status inside a condition ("once your order has shipped, you'll get an email") is policy, not a claim.
_CONDITIONAL = re.compile(r"\b(once|when|after|if|as soon as|until|before|whenever|unless)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?\n]")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d ().-]{6,}\d")
_DATE = re.compile(r"^(\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})$")   # 2026-11-30, 30.11.2026
_URL = re.compile(r"\bhttps?://[^\s<>()\"']+|\bwww\.[^\s<>()\"']+", re.IGNORECASE)
MIN_PHONE_DIGITS = 8
MAX_PHONE_DIGITS = 15        # E.164: longer digit runs are not one phone number


# An order or tracking number in a reply: '#MR-20417', 'order 1001', 'tracking number: PTH-88213'.
_ORDER_REF = re.compile(r"#\s?([A-Z0-9][A-Z0-9_-]{2,31})"
                        r"|\b(?:order|tracking)\s*(?:number|no\.?|id|code)?\s*(?:is\s*)?[:#]?\s*"
                        r"([A-Z]{0,6}-?\d[A-Z0-9_-]{1,30})", re.IGNORECASE)


def order_references(text: str) -> list[str]:
    """Order / tracking numbers mentioned in the text, uppercased, without '#' (must hold a digit)."""
    refs = []
    for match in _ORDER_REF.finditer(plain_punctuation(text)):
        ref = (match.group(1) or match.group(2) or "").upper()
        if any(ch.isdigit() for ch in ref):
            refs.append(ref)
    return refs


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing text[start:end]."""
    left = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    right = _SENTENCE_END.search(text, end)
    return text[left:right.start() if right else len(text)]


# Typographic dashes and spaces models like to use ('MR‑20417' with U+2011, '6 pm' with U+202F).
# Order checks see them as plain '-' and ' ', so a fancy hyphen can't hide an order number.
_DASHES = re.compile("[‐-―−﹘﹣－]")            # hyphens, dashes, minus
_SPACES = re.compile("[  -   　]")            # no-break and fixed-width spaces


def plain_punctuation(text: str) -> str:
    """Unicode dashes -> '-', Unicode spaces -> ' ' (same length, so positions are kept)."""
    return _SPACES.sub(" ", _DASHES.sub("-", text))


def order_claims(text: str) -> list[str]:
    """Statements of an order's status, tracking number or a lookup (conditional policy excluded)."""
    text = plain_punctuation(text)
    found = []
    for pattern in (_ORDER_STATUS, _ORDER_NUMBER_STATUS, _TRACKING, _LOOKED_UP):
        for match in pattern.finditer(text):
            if pattern is _LOOKED_UP or not _CONDITIONAL.search(_sentence_around(text, match.start(), match.end())):
                found.append(match.group(0))
    return found


def promise_claims(text: str) -> list[str]:
    """Statements that a return or refund was approved or a label sent (conditions excluded)."""
    text = plain_punctuation(text)
    return [m.group(0) for m in _PROMISE.finditer(text)
            if not _CONDITIONAL.search(_sentence_around(text, m.start(), m.end()))]


def _digits(text: str) -> str:
    """Only the digits of a string."""
    return re.sub(r"\D", "", text)


def emails(text: str) -> list[str]:
    """Email addresses in the text, lowercased."""
    return [e.lower().rstrip(".") for e in _EMAIL.findall(text)]


def phone_numbers(text: str) -> list[str]:
    """Phone-like numbers in the text, as digit strings (dates and short numbers excluded)."""
    numbers = []
    for match in _PHONE.finditer(text):
        value = match.group(0).strip()
        if not _DATE.match(value) and MIN_PHONE_DIGITS <= len(_digits(value)) <= MAX_PHONE_DIGITS:
            numbers.append(_digits(value))
    return numbers


def link_hosts(text: str) -> list[str]:
    """Hosts of the links in the text (http(s):// and www. forms), lowercased."""
    hosts = []
    for url in _URL.findall(text):
        url = url.rstrip(".,;:!?")
        parts = urlsplit(url if "://" in url else f"https://{url}")
        if parts.hostname:
            hosts.append(parts.hostname.lower())
    return hosts


def bare_host(host: str) -> str:
    """'www.shop.example' -> 'shop.example', so both spellings of a site match."""
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


# --- Output: the streaming guard ---------------------------------------------------------------

class OutputGuard:
    """Checks a streamed reply as it arrives; stops it at the first violation.

    feed() returns the text that is safe to show now, holding back the last HOLD_CHARS so a
    pattern still being written (an email address, a canary) is caught before it is shown.
    Once `violation` is set, nothing more is released and main.py replaces the reply.
    """
    HOLD_CHARS = 64

    def __init__(self, canary: str, fingerprints: tuple[str, ...], allowed_text: str, allowed_hosts: set[str],
                 order_refs: frozenset[str] | None = None):
        """`allowed_text`: knowledge, tenant config and conversation (where contact details may
        come from). `allowed_hosts`: the tenant's own sites. `fingerprints`: prompt phrases.
        `order_refs`: the verified order's number and tracking number (None: no verified order)."""
        self.canary = canary.lower()
        self.fingerprints = tuple(f.lower() for f in fingerprints)
        self.allowed_text = allowed_text.lower()
        self.allowed_phones = set(phone_numbers(allowed_text))
        self.allowed_hosts = {bare_host(h) for h in allowed_hosts}
        self.order_refs = None if order_refs is None else {r.upper() for r in order_refs}
        self.violation: str | None = None
        self._text = ""        # everything received so far
        self._released = 0     # how much of it has been shown

    def _check(self, final: bool) -> str | None:
        """The first violation in the reply so far, if any."""
        text = self._text
        lowered = text.lower()
        if self.canary in lowered or any(f in lowered for f in self.fingerprints):
            return PROMPT_LEAK
        if self.order_refs is None:
            if order_claims(text):
                return ORDER_CLAIM
        elif any(not self._ref_allowed(r, final) for r in order_references(text)):
            return ORDER_CLAIM
        if promise_claims(text):
            return PROMISE_CLAIM
        # Mid-stream, a half-written email is still a substring of the real one, so this holds.
        if any(e not in self.allowed_text for e in emails(text)):
            return UNGROUNDED_CONTACT
        if any(not self._phone_allowed(p, final) for p in phone_numbers(text)):
            return UNGROUNDED_CONTACT
        if any(not self._host_allowed(h, final) for h in link_hosts(text)):
            return UNGROUNDED_LINK
        return None

    def _ref_allowed(self, ref: str, final: bool) -> bool:
        """The verified order's own number (mid-stream: possibly half-written)."""
        return any(a == ref or (not final and a.startswith(ref)) for a in self.order_refs or ())

    def _phone_allowed(self, digits: str, final: bool) -> bool:
        """A number from the allowed text (mid-stream: the start of one)."""
        return any(a == digits or (not final and a.startswith(digits)) for a in self.allowed_phones)

    def _host_allowed(self, host: str, final: bool) -> bool:
        """The tenant's own host or a subdomain of it (mid-stream: possibly half-written)."""
        host = bare_host(host)
        if any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return True
        return not final and any(h.startswith(host) for h in self.allowed_hosts)

    def feed(self, text: str) -> str:
        """Add streamed text; return what can be shown now ("" once a violation is found)."""
        if self.violation:
            return ""
        self._text += text
        self.violation = self._check(final=False)
        if self.violation:
            return ""
        ready_until = max(self._released, len(self._text) - self.HOLD_CHARS)
        ready, self._released = self._text[self._released:ready_until], ready_until
        return ready

    def flush(self) -> str:
        """End of stream: final check, then the held-back rest (or "" on a violation)."""
        if self.violation:
            return ""
        self.violation = self._check(final=True)
        if self.violation:
            return ""
        rest, self._released = self._text[self._released:], len(self._text)
        return rest
