"""Verification / one-time code extractor.

This module is used by :mod:`app.services.messages` to surface the most likely
verification code (OTP / sign-in code / security code ...) in a freshly parsed
email. The goal is to extract the *correct* code in the vast majority of real
inbound verification mails while avoiding false positives on things like order
numbers, tracking numbers, currency and phone numbers.

The strategy is a lightweight scoring model:

1. Strip emails/URLs/long digit sequences that are clearly not OTPs.
2. Enumerate candidate tokens (pure digits, letters+digits, hex codes, and
   separator-joined groups like ``123-456`` or ``1 2 3 4 5 6``).
3. For each candidate, compute a score based on:
   - proximity to verification-context keywords in many languages,
   - subject-line context (subject is a strong signal),
   - typographic prominence (standing on its own line, inside `<strong>` /
     `<h1>` / a quote, surrounded by asterisks),
   - shape (6-digit all-digit code scores higher than an 8-char alphanum).
4. Keep the highest-scoring candidate whose score exceeds a threshold.

The extractor is intentionally stateless and self-contained so that it can be
exercised directly from unit tests without needing a runtime.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_verification_code(
    *,
    subject: str | None,
    sender: str | None,
    text_body: str | None,
    html_body: str | None,
    preview: str | None = None,
) -> str | None:
    """Return the most likely verification code from an email, or ``None``.

    The arguments accept raw fields coming straight out of the database / mail
    parser. ``html_body`` is stripped of scripts and tags before being fed into
    the analyzer, and attribute / style noise is aggressively discarded.
    """

    subject_text = _normalize_whitespace(subject or "")
    sender_text = (sender or "").strip()
    plain_text = _normalize_whitespace(text_body or "")
    html_plain = _normalize_whitespace(_html_to_text(html_body or ""))
    preview_text = _normalize_whitespace(preview or "")

    context_parts = [part for part in (subject_text, plain_text, html_plain) if part]
    if not plain_text and not html_plain and preview_text:
        context_parts.append(preview_text)
    context_text = "\n".join(context_parts)
    if not context_text:
        return None
    if not _looks_like_verification_message(sender_text, subject_text, context_text):
        return None

    cleaned_text = _strip_irrelevant_tokens(context_text)
    if not cleaned_text:
        return None

    candidates = _enumerate_candidates(cleaned_text)
    if not candidates:
        return None

    best: tuple[float, int, _Candidate] | None = None
    runner_up: tuple[float, int, _Candidate] | None = None
    for index, candidate in enumerate(candidates):
        score = _score_candidate(
            candidate,
            subject=subject_text,
            full_text=cleaned_text,
            html_plain=html_plain,
        )
        if score <= 0:
            continue
        key = (score, -index, candidate)
        if best is None or key > best:
            runner_up = best
            best = key
        elif runner_up is None or key > runner_up:
            runner_up = key

    if best is None:
        return None
    score, _, candidate = best
    if score < _SCORE_THRESHOLD:
        return None
    # If two candidates tie closely, treat the mail as ambiguous and abstain.
    if runner_up is not None:
        runner_score, _, runner_candidate = runner_up
        if runner_score >= _SCORE_THRESHOLD and abs(score - runner_score) < _TIE_MARGIN:
            if runner_candidate.code != candidate.code:
                return None
    # Disjunction guard: if the mail literally says "<code> or <code>" / "<code>
    # 或 <code>" and our pick is one of those two, bail out. Too ambiguous.
    if _is_in_disjunction(cleaned_text, candidate, candidates):
        return None
    return candidate.code


# ---------------------------------------------------------------------------
# Candidate definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A single verification-code candidate pulled out of the mail text."""

    code: str                 # canonical form (no separators, uppercase for alnum)
    display: str              # exact span that matched, used to re-locate context
    span: tuple[int, int]
    shape: str                # "digits", "alnum", "hex", "grouped-digits"
    length: int

    def __lt__(self, other: "_Candidate") -> bool:
        return self.span < other.span


# ---------------------------------------------------------------------------
# Context hints
#
# These hint bags are deliberately broad; they drive the "looks like a
# verification mail at all" gate and the proximity scoring. Hints use lower
# case and are matched against a lower-cased version of the text.
# ---------------------------------------------------------------------------


_HINTS_CORE = (
    # Chinese
    "验证码",
    "校验码",
    "动态密码",
    "动态码",
    "安全码",
    "授权码",
    "确认码",
    "确认代码",
    "操作确认码",
    "验证代码",
    "代码为",
    "您的代码",
    "你的代码",
    "openai 代码",
    "代码",
    "登录码",
    "登入码",
    "登录验证码",
    "注册码",
    "一次性密码",
    "短信验证码",
    "邮件验证码",
    "邮箱验证码",
    "验证您的",
    "验证你的",
    "以验证",
    "请使用以下",
    "请使用下方",
    "请输入下方",
    # English
    "verification code",
    "verify code",
    "verification pin",
    "security code",
    "login code",
    "sign in code",
    "sign-in code",
    "signin code",
    "access code",
    "authentication code",
    "authorization code",
    "confirmation code",
    "one-time code",
    "one time code",
    "one-time password",
    "one time password",
    "otp",
    "passcode",
    "pass code",
    "temporary code",
    "magic code",
    "activation code",
    "please use the code",
    "use the code",
    "use this code",
    "enter this code",
    "enter the code",
    "enter the following",
    "your code is",
    "code:",
    "code is",
    "code to verify",
    "code to confirm",
    "code to continue",
    "code to sign in",
    "confirm your sign-in",
    "confirm your sign in",
    "confirm your signin",
    "confirm your login",
    "confirm your identity",
    "verify your email",
    "verify your account",
    "confirm your email",
    "confirm your identity",
    "to complete the sign-in",
    "to complete sign-in",
    "to complete the signin",
    "to finish signing",
    # Japanese
    "認証コード",
    "確認コード",
    "検証コード",
    "一時検証コード",
    "一時的な認証コード",
    "ワンタイム",
    "コード",
    # Korean
    "인증 코드",
    "인증코드",
    "확인 코드",
    "임시 인증 코드",
    "코드는",
    "코드",
    # Other common romance/germanic variants
    "código de verificación",
    "codigo de verificacion",
    "código de verificação",
    "codigo de verificacao",
    "code de vérification",
    "code de verification",
    "votre code",
    "seu código",
    "seu codigo",
    "código do openai",
    "codigo do openai",
    "dein code",
    "code für openai",
    "code fur openai",
    "code openai",
    "your openai code",
    "bestätigungscode",
    "bestaetigungscode",
    "codice di verifica",
    "codice verifica",
)


_HINT_TOPIC_PATTERNS = (
    re.compile(r"verify[^.]{0,40}email", re.IGNORECASE),
    re.compile(r"verify[^.]{0,40}account", re.IGNORECASE),
    re.compile(r"your\s+[a-z]+\s+(?:verification|login|sign-in|sign in|passcode)\s+code", re.IGNORECASE),
    re.compile(r"temporary\s+[a-z]+\s+code", re.IGNORECASE),
    re.compile(r"(?:登录|注册|验证|绑定|找回|解绑|重置).{0,14}(?:验证码|动态密码|安全码)", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Regexes for enumeration & normalization
# ---------------------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_STYLE_ATTR_RE = re.compile(r"\s(?:style|class|id|src|href|data-[\w-]+)=\"[^\"]*\"", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_LONG_DIGIT_RE = re.compile(r"(?<!\d)\d{9,}(?!\d)")   # phone numbers, order #, card PANs
_CURRENCY_RE = re.compile(r"[¥$€£￥]\s?\d[\d,.]*")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_WHITESPACE_RE = re.compile(r"\s+")

# Pure digit OTP (4-8 digits).
_DIGIT_OTP_RE = re.compile(r"(?<![\w\d])(\d{4,8})(?![\w\d])")
# Digits separated by - or space into groups: 123-456, 123 456, 12 34 56.
_GROUPED_OTP_RE = re.compile(r"(?<![\w\d])(\d{2,4}(?:[-\s]\d{2,4}){1,3})(?![\w\d])")
# Letter + digit mixed, 4-10 chars, at least one digit. Excludes pure letters
# like a tracking id "USPS0000".
_ALNUM_OTP_RE = re.compile(r"(?<![\w\d])([A-Za-z][A-Za-z0-9]{3,9}|[A-Za-z0-9]{4,10})(?![\w\d])")


# Candidates that look like legitimate words/noise and should be dropped.
_STOP_WORDS = frozenset(
    {
        # Very short english words that match _ALNUM_OTP_RE by accident.
        "http", "https", "mail", "email", "gmail", "yahoo", "inbox", "code",
        "codes", "login", "signup", "subject", "header", "sender", "client",
        "please", "thanks", "regards", "replies", "unsubscribe", "support",
        "notice", "noreply", "account", "action", "update", "updates",
        "confirm", "verify", "welcome", "friend", "family", "expire",
        "expired", "expires", "token", "secret", "please", "minutes",
        "minute", "seconds", "hours", "today", "false", "true", "null",
        "color", "style", "width", "height", "table", "title", "class",
        "theme", "link", "links", "message", "messages", "content", "media",
        "report", "server", "tracking", "order", "orders", "shipment",
        "shipped", "delivered", "invoice", "receipt", "amount",
    }
)


# ---------------------------------------------------------------------------
# Scoring tuning knobs
# ---------------------------------------------------------------------------


_SCORE_THRESHOLD = 5.0
_CONTEXT_RADIUS = 140   # characters to the left/right to consider "near" a hint
_TIE_MARGIN = 1.0       # scores within this much are treated as tied


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _html_to_text(source: str) -> str:
    if not source:
        return ""
    # Drop script/style wholesale.
    cleaned = _SCRIPT_STYLE_RE.sub(" ", source)
    # Preserve line breaks around common block-level tags so codes on their own
    # "line" keep their isolation signal.
    cleaned = re.sub(
        r"</?(?:p|div|h[1-6]|li|ul|ol|tr|td|th|table|br|hr|blockquote|article|section)[^>]*>",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip attribute noise so we do not hit numeric tokens inside style="...".
    cleaned = _STYLE_ATTR_RE.sub(" ", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    return html.unescape(cleaned)


def _normalize_whitespace(source: str) -> str:
    if not source:
        return ""
    return _WHITESPACE_RE.sub(" ", source).strip()


def _strip_irrelevant_tokens(text: str) -> str:
    """Blank out tokens that would otherwise look like OTPs (urls, emails, ...)."""
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    cleaned = _CURRENCY_RE.sub(" ", cleaned)
    cleaned = _LONG_DIGIT_RE.sub(" ", cleaned)
    cleaned = _YEAR_RE.sub(" ", cleaned)
    return _normalize_whitespace(cleaned)


# ---------------------------------------------------------------------------
# Context detection
# ---------------------------------------------------------------------------


def _lower(text: str) -> str:
    return text.casefold()


def _looks_like_verification_message(sender: str, subject: str, text: str) -> bool:
    lowered = _lower(text)
    if any(hint in lowered for hint in _HINTS_CORE):
        return True
    lowered_subject = _lower(subject)
    if any(hint in lowered_subject for hint in _HINTS_CORE):
        return True
    if any(pattern.search(text) for pattern in _HINT_TOPIC_PATTERNS):
        return True
    lowered_sender = _lower(sender)
    sender_hints = (
        "verify",
        "verification",
        "otp",
        "noreply",
        "no-reply",
        "account",
        "security",
        "accounts",
    )
    subject_strong = any(
        keyword in lowered_subject
        for keyword in (
            "code",
            "otp",
            "verify",
            "verification",
            "confirm",
            "sign in",
            "sign-in",
            "signin",
            "登录",
            "验证",
            "确认",
            "代码",
            "验证码",
            "コード",
            "認証",
            "認証コード",
            "検証",
            "検証コード",
            "確認コード",
            "인증",
            "인증 코드",
            "코드",
            "código",
            "codigo",
            "vérification",
            "verificação",
            "bestätigung",
            "bestätigungscode",
            "codice",
            "verifica",
        )
    )
    if subject_strong and any(token in lowered_sender for token in sender_hints):
        return True
    return False


def _context_hit_nearby(text: str, lowered: str, start: int, end: int) -> tuple[bool, int]:
    """Return ``(has_hint, best_distance)`` for hints inside the candidate window."""
    window_start = max(0, start - _CONTEXT_RADIUS)
    window_end = min(len(lowered), end + _CONTEXT_RADIUS)
    window = lowered[window_start:window_end]
    best_distance = -1
    for hint in _HINTS_CORE:
        idx = window.find(hint)
        if idx < 0:
            continue
        absolute = window_start + idx
        if absolute <= end:
            distance = max(0, start - (absolute + len(hint)))
        else:
            distance = max(0, absolute - end)
        if best_distance < 0 or distance < best_distance:
            best_distance = distance
    return best_distance >= 0, best_distance


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def _canonicalize(token: str, shape: str) -> str:
    if shape in {"digits", "hex", "alnum"}:
        return re.sub(r"[^0-9A-Za-z]", "", token).upper() if shape != "digits" else re.sub(r"\D", "", token)
    if shape == "grouped-digits":
        return re.sub(r"\D", "", token)
    return token


def _enumerate_candidates(text: str) -> list[_Candidate]:
    seen_canonical: dict[str, _Candidate] = {}
    results: list[_Candidate] = []

    def _record(match: re.Match[str], shape: str) -> None:
        display = match.group(1) if match.groups() else match.group(0)
        canonical = _canonicalize(display, shape)
        if shape == "digits":
            if not (4 <= len(canonical) <= 8):
                return
        elif shape == "grouped-digits":
            if not (4 <= len(canonical) <= 10):
                return
        elif shape in {"alnum", "hex"}:
            if not (4 <= len(canonical) <= 10):
                return
            if canonical.isalpha():
                return                              # pure letters are not OTPs
            if canonical.isdigit():
                # already captured by the digit regex; skip the duplicate.
                return
            if canonical.lower() in _STOP_WORDS:
                return
        if canonical in seen_canonical:
            return
        candidate = _Candidate(
            code=canonical if shape != "alnum" else canonical,
            display=display,
            span=(match.start(1) if match.groups() else match.start(), match.end(1) if match.groups() else match.end()),
            shape=shape,
            length=len(canonical),
        )
        seen_canonical[canonical] = candidate
        results.append(candidate)

    # Order matters: grouped digits first so "123-456" isn't eaten as two codes.
    for match in _GROUPED_OTP_RE.finditer(text):
        _record(match, "grouped-digits")
    for match in _DIGIT_OTP_RE.finditer(text):
        _record(match, "digits")
    for match in _ALNUM_OTP_RE.finditer(text):
        shape = "hex" if re.fullmatch(r"[0-9a-fA-F]+", match.group(1)) else "alnum"
        _record(match, shape)
    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_candidate(candidate: _Candidate, *, subject: str, full_text: str, html_plain: str) -> float:
    lowered = _lower(full_text)
    start, end = candidate.span

    score = 0.0

    # Baseline "is there any verification hint nearby?" signal.
    has_hint, distance = _context_hit_nearby(full_text, lowered, start, end)
    if has_hint:
        score += 4.0
        if distance <= 6:
            score += 4.0
        elif distance <= 20:
            score += 2.0
        elif distance <= 60:
            score += 1.0
    # Subject line containing the code or a hint is a very strong signal.
    subject_lower = _lower(subject)
    if subject and candidate.display in subject:
        score += 2.5
    if any(hint in subject_lower for hint in _HINTS_CORE):
        score += 1.5

    # Shape preferences. 6-digit is the canonical OTP shape.
    if candidate.shape == "digits":
        if candidate.length == 6:
            score += 2.0
        elif candidate.length in (4, 5, 7):
            score += 1.0
        elif candidate.length == 8:
            score += 0.8
    elif candidate.shape == "grouped-digits":
        score += 1.8
    elif candidate.shape == "alnum":
        score += 0.6
    elif candidate.shape == "hex":
        score += 0.4

    # Standing alone on a line is a big stylistic hint.
    line_start = full_text.rfind("\n", 0, start) + 1
    line_end = full_text.find("\n", end)
    if line_end < 0:
        line_end = len(full_text)
    line = full_text[line_start:line_end].strip()
    if line == candidate.display:
        score += 2.5
    elif len(line) <= 24 and candidate.display in line:
        score += 1.0

    # Wrapped by asterisks, brackets, or quote marks (Markdown / plain-text emphasis).
    surrounding = full_text[max(0, start - 2):min(len(full_text), end + 2)]
    if surrounding.startswith("**") and surrounding.endswith("**"):
        score += 1.0
    elif surrounding.startswith(("[", "【", "「", "“", "\"")) and surrounding.endswith(("]", "】", "」", "”", "\"")):
        score += 0.6

    # Penalize if the candidate looks like a year / very round number.
    if candidate.shape == "digits":
        if candidate.length == 4 and 1900 <= int(candidate.code) <= 2100:
            score -= 1.0
        if candidate.code in {"0000", "00000", "000000", "1111", "111111", "123456", "12345678"}:
            score -= 2.0
        # Repdigit / simple sequence codes are almost always dummies.
        if len(set(candidate.code)) == 1:
            score -= 2.0

    # If the html body bolded / highlighted the candidate, reward it.
    if html_plain and candidate.display in html_plain:
        score += 0.2

    return score


_DISJUNCTION_RE = re.compile(
    r"(?:\s|^)(?:or|either|/|\\|、|或|或者|又或者)(?:\s|$)",
    re.IGNORECASE,
)


def _is_in_disjunction(text: str, chosen: _Candidate, candidates: list[_Candidate]) -> bool:
    """Return True if the chosen candidate appears to be one of several "X or Y"
    alternatives in the same sentence — which strongly suggests the mail is not
    delivering a single authoritative code.
    """

    # We only care about the case where we actually have peer candidates to
    # disambiguate against.
    peers = [c for c in candidates if c.code != chosen.code]
    if not peers:
        return False
    start, end = chosen.span
    window_start = max(0, start - 80)
    window_end = min(len(text), end + 80)
    window = text[window_start:window_end]
    if not _DISJUNCTION_RE.search(window):
        return False
    # Require at least one peer to also appear in the same window.
    for peer in peers:
        if peer.display in window and peer.display != chosen.display:
            return True
    return False
