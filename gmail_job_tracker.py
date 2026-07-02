#!/usr/bin/env python3
"""Read-only Gmail job-application status tracker.

The first run learns sender/subject patterns from matching full-history email,
then records only events within the configured recent lookback window.
Subsequent runs use a short overlap from the previous successful sync and
deduplicate by Gmail message ID.
"""

# Flow overview:
# 1. Authenticate to read-only Gmail and load local config/state.
# 2. Learn or reuse sender-pattern hints, then search for candidate mail.
# 3. Parse message bodies into application events and match them to jobs/leads.
# 4. Write append-only events plus the deduplicated applied-jobs ledger.

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_ACCOUNT = ""
DEFAULT_CONFIG = Path("gmail_tracker_config.json")
DEFAULT_PRIVATE_DIR = Path(".gmail_job_tracker")
PATTERN_VERSION = 1

BASE_QUERY = (
    "in:anywhere -in:spam -in:sent "
    "{subject:application subject:interview subject:candidate "
    '"thank you for applying" "application received" "application was viewed" '
    '"not selected" "not moving forward" "next steps" "offer of employment"}'
)

ATS_DOMAINS = {
    "applicantpro.com",
    "bamboohr.com",
    "csod.com",
    "edjoin.org",
    "greenhouse.io",
    "governmentjobs.com",
    "icims.com",
    "indeed.com",
    "jazzhr.com",
    "jobvite.com",
    "lever.co",
    "linkedin.com",
    "myworkday.com",
    "myworkdayjobs.com",
    "paylocity.com",
    "schooljobs.com",
    "smartrecruiters.com",
    "taleo.net",
    "workablemail.com",
    "workday.com",
    "ziprecruiter.com",
}

NOISE_PATTERNS = [
    r"\bjob alert\b",
    r"\brecommended jobs?\b",
    r"\bjobs? you may be interested in\b",
    r"\bnew jobs? (?:for|near)\b",
    r"\btop job picks\b",
    r"\bemployer wants you to apply\b",
    r"\bapply now\b.*\bjobs?\b",
]

STATUS_RULES: list[tuple[str, list[str]]] = [
    (
        "offer",
        [
            r"\boffer of employment\b",
            r"\bpleased to (?:extend|make) (?:you )?an offer\b",
            r"\bjob offer\b",
            r"\bwelcome to (?:the team|our team)\b",
        ],
    ),
    (
        "interview",
        [
            r"\bschedule (?:an|your) interview\b",
            r"\binterview invitation\b",
            r"\bwould like to interview\b",
            r"\binvite you to interview\b",
            r"\bphone screen\b",
            r"\bselect an interview time\b",
        ],
    ),
    (
        "assessment_or_action_required",
        [
            r"\bcomplete (?:the|your|an) assessment\b",
            r"\bskills? (?:test|assessment)\b",
            r"\btyping test\b",
            r"\bbackground check\b",
            r"\bprovide (?:your )?references\b",
            r"\badditional information (?:is )?required\b",
            r"\baction required\b",
            r"\bto move your application forward\b",
        ],
    ),
    (
        "rejected",
        [
            r"\bnot selected\b",
            r"\bnot moving forward\b",
            r"\bwill not be moving forward\b",
            r"\bdecided not to proceed\b",
            r"\bother candidates\b",
            r"\bposition has been filled\b",
            r"\bunable to offer you\b",
            r"\bwill not be considered\b",
            r"\bwe regret to inform\b",
        ],
    ),
    (
        "withdrawn",
        [
            r"\bapplication (?:has been|was) withdrawn\b",
            r"\bposition (?:has been|was) cancelled\b",
            r"\brequisition (?:has been|was) cancelled\b",
        ],
    ),
    (
        "viewed",
        [
            r"\bapplication was viewed\b",
            r"\bviewed your application\b",
            r"\bemployer viewed\b",
        ],
    ),
    (
        "under_review",
        [
            r"\bapplication is under review\b",
            r"\bapplication (?:is|remains) in review\b",
            r"\bcurrently reviewing (?:your )?application\b",
            r"\bapplication is being reviewed\b",
        ],
    ),
    (
        "application_received",
        [
            r"\bthank you for applying\b",
            r"\bapplication (?:has been )?received\b",
            r"\bapplication (?:has been )?submitted\b",
            r"\bsuccessfully submitted\b",
            r"\bwe (?:have )?received your application\b",
            r"\bapplication was sent to\b",
            r"\bthanks for your application\b",
            r"\bthank you for your application\b",
        ],
    ),
    (
        "status_update",
        [
            r"\bapplication status\b",
            r"\bupdate (?:on|regarding) your application\b",
            r"\bnext steps? in (?:the|your) application\b",
        ],
    ),
]

STATUS_LABELS = {
    "application_received": "Application received",
    "under_review": "Under review",
    "viewed": "Application viewed",
    "interview": "Interview",
    "assessment_or_action_required": "Assessment/action required",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn/cancelled",
    "status_update": "Application update",
}

APPLIED_COLUMNS = [
    "application_key",
    "date_applied",
    "title",
    "company",
    "location",
    "source",
    "status",
    "response_signal",
    "posting_url",
    "notes",
    "next_action",
    "status_updated_at",
    "latest_email_date",
    "latest_email_subject",
    "latest_email_from",
    "gmail_message_id",
    "parse_confidence",
    "needs_review",
    "tracking_sources",
    "lead_match_type",
    "lead_date_found",
    "lead_priority",
    "lead_score",
    "lead_source",
    "company_careers_url",
    "remote_status",
    "commute_minutes_from_fontana",
    "pay_rate",
    "employment_type",
    "schedule",
    "start_speed_confidence",
    "job_family",
    "fit_tags",
    "resume_leverage_score",
    "why_fit",
    "top_resume_angle",
    "application_friction",
    "legitimacy_score",
    "chaos_risk_score",
    "artist_alley_compatibility",
    "energy_cost_score",
    "lead_confidence_score",
]

EVENT_COLUMNS = [
    "message_id",
    "thread_id",
    "email_date",
    "sender",
    "sender_domain",
    "subject",
    "status",
    "title",
    "company",
    "confidence",
    "needs_review",
    "matched_application",
    "gmail_url",
    "labels",
    "evidence",
]


@dataclass
class Config:
    """Runtime settings for mailbox access, local state, and CSV outputs."""

    account: str = DEFAULT_ACCOUNT
    credentials_file: str = str(DEFAULT_PRIVATE_DIR / "credentials.json")
    token_file: str = str(DEFAULT_PRIVATE_DIR / "token.json")
    state_file: str = str(DEFAULT_PRIVATE_DIR / "state.json")
    learned_patterns_file: str = str(DEFAULT_PRIVATE_DIR / "learned_patterns.json")
    job_leads_file: str = "job_leads.csv"
    applied_jobs_file: str = "applied_jobs.csv"
    events_file: str = "application_events.csv"
    initial_lookback_days: int = 30
    sync_overlap_days: int = 3
    max_history_messages: int = 5000
    max_sync_messages: int = 1000
    include_trash: bool = True


@dataclass
class ParsedMessage:
    """Normalized Gmail application-status event extracted from one message."""

    message_id: str
    thread_id: str
    email_date: str
    sender: str
    sender_domain: str
    subject: str
    status: str
    title: str
    company: str
    confidence: float
    needs_review: bool
    gmail_url: str
    labels: str
    evidence: str


def load_config(path: Path) -> Config:
    """Load optional JSON overrides for the tracker runtime configuration."""
    if not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    allowed = Config.__dataclass_fields__.keys()
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(unknown)}")
    return Config(**raw)


def save_config(path: Path, config: Config) -> None:
    """Persist runtime config so first-run setup does not require source edits."""
    save_json(path, asdict(config))


def prompt_for_account(existing: str = "") -> str:
    """Ask the user for the Gmail address to track during interactive setup."""
    default = existing.strip()
    prompt = "Gmail address to track"
    if default:
        prompt = f"{prompt} [{default}]"
    prompt += ": "
    value = input(prompt).strip()
    return value or default


def resolve_account(config: Config, config_path: Path) -> Config:
    """Fill in the Gmail account from config or interactive setup."""
    account = config.account.strip()
    if account:
        config.account = account
        return config

    if not sys.stdin.isatty():
        raise RuntimeError(
            f"No Gmail account configured in {config_path}. "
            "Add an 'account' value to the config file or run the script interactively once."
        )

    account = prompt_for_account()
    if not account:
        raise RuntimeError("A Gmail address is required to continue.")
    config.account = account
    save_config(config_path, config)
    print(f"Saved Gmail account to {config_path}.")
    return config


def decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 email header into readable text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def decode_base64url(value: str) -> str:
    """Decode Gmail's base64url payload fragments into UTF-8 text."""
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


class _TextExtractor:
    """Strip HTML mail bodies down to readable text for status parsing."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def feed(self, source: str) -> None:
        """Accumulate a chunk of HTML after removing tags that hurt parsing."""
        cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", source)
        cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
        cleaned = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])>", "\n", cleaned)
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        self.parts.append(html.unescape(cleaned))

    def text(self) -> str:
        """Return the accumulated text with Gmail-safe whitespace normalization."""
        return normalize_space("\n".join(self.parts), keep_newlines=True)


def html_to_text(source: str) -> str:
    """Convert HTML email content into plain text for classifier input."""
    parser = _TextExtractor()
    parser.feed(source)
    return parser.text()


def normalize_space(value: str, *, keep_newlines: bool = False) -> str:
    """Collapse noisy whitespace while preserving line breaks when needed."""
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    if keep_newlines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", value).strip()


def payload_text(payload: dict[str, Any]) -> str:
    """Extract the best available plain-text body from a Gmail message payload."""
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if data:
            decoded = decode_base64url(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    if plain:
        return normalize_space("\n".join(plain), keep_newlines=True)
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return ""


def message_headers(message: dict[str, Any]) -> dict[str, str]:
    """Build a lowercase header map so later parsing can stay order-agnostic."""
    result: dict[str, str] = {}
    for header in message.get("payload", {}).get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in result:
            result[name] = decode_header_value(header.get("value"))
    return result


def message_datetime(message: dict[str, Any], headers: dict[str, str]) -> datetime:
    """Pick the most reliable message timestamp, preferring Gmail's internal date."""
    internal = message.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    date_value = headers.get("date", "")
    try:
        parsed = parsedate_to_datetime(date_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc)


def sender_domain(sender: str) -> str:
    """Extract the sender's domain for ATS and recruiter classification."""
    address = parseaddr(sender)[1].lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def domain_is_known_ats(domain: str, learned_domains: set[str]) -> bool:
    """Treat known ATS and learned sender domains as high-signal application mail."""
    return any(domain == known or domain.endswith(f".{known}") for known in ATS_DOMAINS | learned_domains)


def classify_status(subject: str, body: str) -> tuple[str, str]:
    """Classify a message into a job-application status and supporting evidence."""
    subject_norm = normalize_space(subject).lower()
    text = normalize_space(f"{subject}\n{body}").lower()
    strong_match = False
    for status, patterns in STATUS_RULES:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                strong_match = True
                evidence = normalize_space(match.group(0))[:120]
                return status, evidence
    if not strong_match and any(re.search(pattern, subject_norm) for pattern in NOISE_PATTERNS):
        return "", ""
    return "", ""


def clean_entity(value: str) -> str:
    """Trim common formatting noise from extracted company and title fragments."""
    value = normalize_space(value).strip(" -–—:|,.\"'")
    value = re.sub(r"(?i)^the\s+", "", value).strip()
    value = re.sub(r"(?i)\b(application|job|position|role)\b$", "", value).strip()
    return value[:160]


def extract_title_company(subject: str, body: str, sender: str) -> tuple[str, str]:
    """Infer the job title and employer from the message text and sender name."""
    text = normalize_space(f"{subject}\n{body}")
    patterns = [
        re.compile(
            r"(?i)(?:application|candidacy) (?:for|to)\s+(?P<title>[^|\n]{2,100}?)\s+(?:at|with)\s+(?P<company>[^|\n]{2,100}?)(?:[.!|]|\s+-\s|$)"
        ),
        re.compile(
            r"(?i)(?:thank you for applying|thanks for applying)\s+(?:for|to)\s+(?:the\s+)?(?P<title>[^|\n]{2,100}?)\s+(?:position\s+)?(?:at|with)\s+(?P<company>[^|\n]{2,100}?)(?:[.!|]|\s+-\s|$)"
        ),
        re.compile(
            r"(?i)(?:regarding|update on)\s+(?:your\s+)?application\s+for\s+(?P<title>[^|\n]{2,100}?)(?:\s+(?:at|with)\s+(?P<company>[^|\n]{2,100}?))?(?:[.!|]|\s+-\s|$)"
        ),
        re.compile(
            r"(?i)(?:the\s+)?(?P<title>[^|\n]{2,100}?)\s+position\s+(?:at|with)\s+(?P<company>[^|\n]{2,100}?)(?:[.!|]|\s+-\s|$)"
        ),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            title = clean_entity(match.groupdict().get("title", "") or "")
            company = clean_entity(match.groupdict().get("company", "") or "")
            if title or company:
                return title or "Unknown", company or "Unknown"

    subject_patterns = [
        re.compile(r"(?i)^application (?:received|submitted)\s*[-:|]\s*(?P<title>.+?)\s+(?:at|with)\s+(?P<company>.+)$"),
        re.compile(r"(?i)^indeed application\s*[-:|]\s*(?P<title>.+)$"),
        re.compile(r"(?i)^application submitted\s*[-:|]\s*(?P<title>.+)$"),
        re.compile(r"(?i)^thank you for applying to\s+(?P<company>.+)$"),
        re.compile(r"(?i)^your application to\s+(?P<company>.+)$"),
    ]
    for pattern in subject_patterns:
        match = pattern.search(normalize_space(subject))
        if match:
            company = clean_entity(match.groupdict().get("company", "") or "")
            domain = sender_domain(sender)
            display_name = parseaddr(sender)[0]
            if (
                not company
                and display_name
                and not domain_is_known_ats(domain, set())
                and not re.search(r"(?i)\b(no.?reply|notification|recruiting|talent|jobs?)\b", display_name)
            ):
                company = clean_entity(display_name)
            return (
                clean_entity(match.groupdict().get("title", "") or "") or "Unknown",
                company or "Unknown",
            )

    display_name = parseaddr(sender)[0]
    if (
        display_name
        and not domain_is_known_ats(sender_domain(sender), set())
        and not re.search(r"(?i)\b(no.?reply|notification|recruiting|talent|jobs?)\b", display_name)
    ):
        return "Unknown", clean_entity(display_name)
    return "Unknown", "Unknown"


def parse_message(
    message: dict[str, Any],
    *,
    learned_domains: set[str] | None = None,
) -> ParsedMessage | None:
    """Turn one Gmail message into a ParsedMessage when it looks like job mail."""
    learned_domains = learned_domains or set()
    headers = message_headers(message)
    subject = normalize_space(headers.get("subject", ""))
    sender = normalize_space(headers.get("from", ""))
    body = payload_text(message.get("payload", {}))
    status, evidence = classify_status(subject, body)
    if not status:
        return None

    domain = sender_domain(sender)
    title, company = extract_title_company(subject, body, sender)
    confidence = 0.55
    if status != "status_update":
        confidence += 0.15
    if domain_is_known_ats(domain, learned_domains):
        confidence += 0.10
    if title != "Unknown":
        confidence += 0.10
    if company != "Unknown":
        confidence += 0.10
    confidence = min(round(confidence, 2), 0.99)
    needs_review = confidence < 0.75 or company == "Unknown" or title == "Unknown"
    dt = message_datetime(message, headers)
    message_id = str(message.get("id", ""))
    return ParsedMessage(
        message_id=message_id,
        thread_id=str(message.get("threadId", "")),
        email_date=dt.isoformat(),
        sender=sender or "Unknown",
        sender_domain=domain or "Unknown",
        subject=subject or "(no subject)",
        status=status,
        title=title,
        company=company,
        confidence=confidence,
        needs_review=needs_review,
        gmail_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        labels=";".join(sorted(message.get("labelIds", []) or [])),
        evidence=evidence,
    )


def learned_sender_terms(learned: dict[str, Any], limit: int = 20) -> list[str]:
    """Build extra Gmail query filters from previously seen sender domains."""
    counts = learned.get("sender_domains", {}) or {}
    ranked = sorted(counts.items(), key=lambda item: (-int(item[1]), item[0]))
    return [f"from:{domain}" for domain, count in ranked if int(count) >= 2][:limit]


def expand_query_with_learned_senders(base_query: str, learned: dict[str, Any] | None) -> str:
    """Inject learned sender-domain filters into the shared Gmail search query."""
    if not learned:
        return base_query
    terms = learned_sender_terms(learned)
    if not terms:
        return base_query
    filters, matches = base_query.split("{", 1)
    matches = matches.rsplit("}", 1)[0]
    return f"{filters.strip()} {{{matches} {' '.join(terms)}}}"


def query_for_initial(days: int, learned: dict[str, Any] | None = None) -> str:
    """Construct the first-run Gmail query bounded by the recent lookback window."""
    return f"{expand_query_with_learned_senders(BASE_QUERY, learned)} newer_than:{days}d"


def query_for_incremental(
    last_sync: datetime, overlap_days: int, learned: dict[str, Any] | None = None
) -> str:
    """Construct the follow-up Gmail query using the previous sync overlap."""
    start = (last_sync - timedelta(days=overlap_days)).date()
    return f"{expand_query_with_learned_senders(BASE_QUERY, learned)} after:{start:%Y/%m/%d}"


def list_message_ids(
    service: Any, query: str, limit: int, *, include_trash: bool = True
) -> Iterator[str]:
    """Page through Gmail search results and yield candidate message IDs."""
    page_token: str | None = None
    emitted = 0
    while True:
        request = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(500, max(1, limit - emitted)),
            pageToken=page_token,
            includeSpamTrash=include_trash,
        )
        response = execute_with_retry(request)
        for item in response.get("messages", []) or []:
            yield str(item["id"])
            emitted += 1
            if emitted >= limit:
                return
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def execute_with_retry(request: Any, attempts: int = 5) -> dict[str, Any]:
    """Retry transient Gmail API failures with exponential backoff."""
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Unreachable retry state")


def fetch_message(service: Any, message_id: str) -> dict[str, Any]:
    """Fetch a full Gmail message so the parser can inspect headers and body."""
    request = service.users().messages().get(userId="me", id=message_id, format="full")
    return execute_with_retry(request)


def build_service(config: Config) -> Any:
    """Authenticate to Gmail read-only access and verify the configured account."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Gmail libraries are missing. Install requirements.txt first."
        ) from exc

    credentials_path = Path(config.credentials_file)
    token_path = Path(config.token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if not credentials_path.exists() and not token_path.exists():
        raise FileNotFoundError(
            f"Missing {credentials_path}. Follow the README OAuth setup before the first run."
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = execute_with_retry(service.users().getProfile(userId="me"))
    actual = str(profile.get("emailAddress", "")).lower()
    if actual != config.account.lower():
        raise RuntimeError(
            f"Authenticated Gmail account is {actual or 'Unknown'}, not {config.account}. "
            f"Delete {token_path} and authorize the correct account."
        )
    return service


def load_state(path: Path) -> dict[str, Any]:
    """Load the last successful sync metadata, or start clean if missing."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: Any) -> None:
    """Write JSON atomically so tracker state and learned patterns stay consistent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def learn_patterns(service: Any, config: Config) -> dict[str, Any]:
    """Scan historical candidate mail to summarize recurring senders and status hints."""
    domain_counts: Counter[str] = Counter()
    subject_prefix_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    scanned = 0
    matched = 0
    for message_id in list_message_ids(
        service, BASE_QUERY, config.max_history_messages, include_trash=config.include_trash
    ):
        scanned += 1
        parsed = parse_message(fetch_message(service, message_id))
        if not parsed:
            continue
        matched += 1
        if parsed.sender_domain != "Unknown":
            domain_counts[parsed.sender_domain] += 1
        prefix = re.sub(r"\s+", " ", parsed.subject.lower())[:80]
        subject_prefix_counts[prefix] += 1
        status_counts[parsed.status] += 1
        if scanned % 50 == 0:
            print(f"Learning patterns: inspected {scanned} candidate messages...", file=sys.stderr)
    return {
        "version": PATTERN_VERSION,
        "learned_at": datetime.now(timezone.utc).isoformat(),
        "query": BASE_QUERY,
        "messages_inspected": scanned,
        "messages_matched": matched,
        "sender_domains": dict(domain_counts.most_common()),
        "subject_examples": dict(subject_prefix_counts.most_common(100)),
        "status_counts": dict(status_counts),
    }


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV file into header names and row dictionaries."""
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    """Write a normalized CSV snapshot with a stable column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "Unknown") for column in columns})
    temp.replace(path)


def normalize_key(value: str) -> str:
    """Create a loose comparison key for matching titles and company names."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def normalize_title_key(value: str) -> str:
    """Normalize title text while ignoring common schedule qualifiers."""
    key = normalize_key(value)
    key = re.sub(r"\b(?:part|full)[ -]?time\b", " ", key)
    key = re.sub(r"\b(?:temporary|temp|contract)\b", " ", key)
    return normalize_space(key)


def normalize_company_key(value: str) -> str:
    """Normalize company text while ignoring common legal suffixes."""
    key = normalize_key(value)
    key = re.sub(
        r"\b(?:incorporated|inc|llc|ltd|corp|corporation|company|co|career opportunities)\b",
        " ",
        key,
    )
    return normalize_space(key)


def canonical_application_key(title: str, company: str) -> str:
    """Build the canonical dedupe key used across applied and event rows."""
    title_key = (
        "unknown-title"
        if str(title or "").strip() in {"", "Unknown"}
        else normalize_title_key(title) or "unknown-title"
    )
    company_key = (
        "unknown-company"
        if str(company or "").strip() in {"", "Unknown"}
        else normalize_company_key(company) or "unknown-company"
    )
    return f"{company_key}|{title_key}"


def values_match(left: str, right: str, *, kind: str) -> bool:
    """Compare two titles or companies using the script's fuzzy normalization rules."""
    normalizer = normalize_title_key if kind == "title" else normalize_company_key
    left_key = normalizer(left)
    right_key = normalizer(right)
    if not left_key or not right_key or "unknown" in {left_key, right_key}:
        return False
    return left_key == right_key or left_key in right_key or right_key in left_key


def best_lead_match(
    *,
    title: str,
    company: str,
    location: str = "",
    leads: Sequence[dict[str, str]],
) -> tuple[int | None, str]:
    """Find the strongest `job_leads.csv` match for a row or Gmail event."""
    company_and_title: list[int] = []
    title_only: list[int] = []
    company_only: list[int] = []
    for index, lead in enumerate(leads):
        title_match = values_match(title, lead.get("title", ""), kind="title")
        company_match = values_match(company, lead.get("company", ""), kind="company")
        if title_match:
            title_only.append(index)
        if company_match:
            company_only.append(index)
        if title_match and company_match:
            company_and_title.append(index)
    if len(company_and_title) == 1:
        return company_and_title[0], "company+title"
    if company in {"", "Unknown"} and len(title_only) == 1:
        return title_only[0], "title-only"
    if title in {"", "Unknown"} and len(company_only) == 1:
        return company_only[0], "company-only"
    return None, ""


LEAD_TO_APPLICATION = {
    "date_found": "lead_date_found",
    "priority": "lead_priority",
    "score": "lead_score",
    "source": "lead_source",
    "company_careers_url": "company_careers_url",
    "remote_status": "remote_status",
    "commute_minutes_from_fontana": "commute_minutes_from_fontana",
    "pay_rate": "pay_rate",
    "employment_type": "employment_type",
    "schedule": "schedule",
    "start_speed_confidence": "start_speed_confidence",
    "job_family": "job_family",
    "fit_tags": "fit_tags",
    "resume_leverage_score": "resume_leverage_score",
    "why_fit": "why_fit",
    "top_resume_angle": "top_resume_angle",
    "application_friction": "application_friction",
    "legitimacy_score": "legitimacy_score",
    "chaos_risk_score": "chaos_risk_score",
    "artist_alley_compatibility": "artist_alley_compatibility",
    "energy_cost_score": "energy_cost_score",
    "confidence_score": "lead_confidence_score",
}


def is_unknown(value: Any) -> bool:
    """Treat blank cells and literal Unknown as missing data."""
    return value is None or str(value).strip() in {"", "Unknown"}


def add_tracking_source(row: dict[str, str], source: str) -> None:
    """Record one provenance label in the semicolon-separated tracking source field."""
    existing = [
        item.strip()
        for item in str(row.get("tracking_sources", "")).split(";")
        if item.strip() and item.strip() != "Unknown"
    ]
    if source not in existing:
        existing.append(source)
    row["tracking_sources"] = "; ".join(existing) if existing else "Unknown"


def enrich_application_from_lead(
    row: dict[str, str], lead: dict[str, str], match_type: str
) -> None:
    """Copy lead research fields into an application row and tag the origin."""
    row["lead_match_type"] = match_type
    if is_unknown(row.get("location")) and not is_unknown(lead.get("location")):
        row["location"] = lead["location"]
    if is_unknown(row.get("posting_url")) and not is_unknown(lead.get("posting_url")):
        row["posting_url"] = lead["posting_url"]
    for lead_column, application_column in LEAD_TO_APPLICATION.items():
        value = lead.get(lead_column, "Unknown")
        if not is_unknown(value):
            row[application_column] = value
    add_tracking_source(row, "Job leads")


def link_leads_to_applications(
    rows: list[dict[str, str]], leads: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    """Attach researched lead data to application rows without overwriting user data."""
    for row in rows:
        index, match_type = best_lead_match(
            title=row.get("title", ""),
            company=row.get("company", ""),
            location=row.get("location", ""),
            leads=leads,
        )
        if index is not None:
            enrich_application_from_lead(row, leads[index], match_type)
        row["application_key"] = canonical_application_key(
            row.get("title", ""), row.get("company", "")
        )
        source = row.get("source", "")
        notes = row.get("notes", "")
        directly_confirmed = bool(
            re.search(r"(?i)(confirmed directly)", notes)
        )
        if directly_confirmed and is_unknown(source):
            row["source"] = "Self-reported"
            source = "Self-reported"
        if directly_confirmed or (source and source not in {"Unknown", "Gmail"}):
            add_tracking_source(row, "Self-reported")
        if row.get("gmail_message_id") not in {"", "Unknown", None} or source == "Gmail":
            add_tracking_source(row, "Gmail")
    return rows


def row_richness(row: dict[str, str]) -> int:
    """Score how much usable information a row contains for merge selection."""
    score = sum(1 for value in row.values() if not is_unknown(value))
    if not is_unknown(row.get("date_applied")):
        score += 10
    if row.get("source") not in {"", "Unknown", "Gmail"}:
        score += 5
    return score


def merge_application_group(rows: Sequence[dict[str, str]]) -> dict[str, str]:
    """Collapse duplicate application rows while preserving the best facts from each."""
    primary = dict(max(rows, key=row_richness))
    latest = max(
        rows,
        key=lambda row: (
            "" if is_unknown(row.get("status_updated_at")) else row.get("status_updated_at", "")
        ),
    )
    for row in rows:
        for column in APPLIED_COLUMNS:
            if is_unknown(primary.get(column)) and not is_unknown(row.get(column)):
                primary[column] = row[column]
        for source in str(row.get("tracking_sources", "")).split(";"):
            if source.strip() and source.strip() != "Unknown":
                add_tracking_source(primary, source.strip())
    if not is_unknown(latest.get("status_updated_at")):
        for column in [
            "status",
            "status_updated_at",
            "latest_email_date",
            "latest_email_subject",
            "latest_email_from",
            "gmail_message_id",
            "parse_confidence",
            "needs_review",
            "next_action",
        ]:
            if not is_unknown(latest.get(column)):
                primary[column] = latest[column]
    known_dates = sorted(
        row.get("date_applied", "")
        for row in rows
        if not is_unknown(row.get("date_applied"))
    )
    if known_dates:
        primary["date_applied"] = known_dates[0]
    primary["application_key"] = canonical_application_key(
        primary.get("title", ""), primary.get("company", "")
    )
    return primary


def deduplicate_application_rows(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge duplicate application rows by normalized title and company identity."""
    known_groups: dict[str, list[dict[str, str]]] = {}
    unknown_company: list[dict[str, str]] = []
    for row in rows:
        key = canonical_application_key(row.get("title", ""), row.get("company", ""))
        row["application_key"] = key
        if key.startswith("unknown-company|"):
            unknown_company.append(row)
        else:
            known_groups.setdefault(key, []).append(row)

    merged = [merge_application_group(group) for group in known_groups.values()]
    for row in unknown_company:
        title_matches = [
            index
            for index, candidate in enumerate(merged)
            if values_match(row.get("title", ""), candidate.get("title", ""), kind="title")
        ]
        if len(title_matches) == 1:
            index = title_matches[0]
            merged[index] = merge_application_group([merged[index], row])
        else:
            merged.append(row)

    # A second exact-key pass catches rows whose missing company was filled by a merge.
    final_groups: dict[str, list[dict[str, str]]] = {}
    for row in merged:
        key = canonical_application_key(row.get("title", ""), row.get("company", ""))
        final_groups.setdefault(key, []).append(row)
    result = [merge_application_group(group) for group in final_groups.values()]
    result.sort(key=lambda row: (row.get("date_applied", "Unknown"), row.get("company", ""), row.get("title", "")))
    return result


def best_application_match(
    event: ParsedMessage, rows: Sequence[dict[str, str]]
) -> tuple[int | None, str]:
    """Find the best existing application row for a parsed Gmail event."""
    company_key = normalize_key(event.company)
    title_key = normalize_key(event.title)
    exact: list[int] = []
    company_only: list[int] = []
    title_only: list[int] = []
    for index, row in enumerate(rows):
        row_company = normalize_key(row.get("company", ""))
        row_title = normalize_key(row.get("title", ""))
        if title_key and title_key != "unknown" and (
            row_title == title_key or row_title in title_key or title_key in row_title
        ):
            title_only.append(index)
        if row_company and (row_company == company_key or row_company in company_key or company_key in row_company):
            company_only.append(index)
            if title_key and title_key != "unknown" and (
                row_title == title_key or row_title in title_key or title_key in row_title
            ):
                exact.append(index)
    if len(exact) == 1:
        return exact[0], "company+title"
    if len(company_only) == 1:
        return company_only[0], "company-only"
    if event.company == "Unknown" and len(title_only) == 1:
        return title_only[0], "title-only"
    return None, ""


def update_application_rows(
    existing_rows: list[dict[str, str]],
    events: Sequence[ParsedMessage],
    leads: Sequence[dict[str, str]] = (),
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Apply parsed Gmail events to the canonical application ledger."""
    matches: dict[str, str] = {}
    for event in sorted(events, key=lambda item: item.email_date):
        index, match_type = best_application_match(event, existing_rows)
        if index is None:
            lead_index, lead_match_type = best_lead_match(
                title=event.title,
                company=event.company,
                leads=leads,
            )
            if lead_index is not None:
                lead = leads[lead_index]
                row = {column: "Unknown" for column in APPLIED_COLUMNS}
                row.update(
                    {
                        "date_applied": (
                            event.email_date[:10]
                            if event.status == "application_received"
                            else "Unknown"
                        ),
                        "title": lead.get("title", event.title),
                        "company": lead.get("company", event.company),
                        "location": lead.get("location", "Unknown"),
                        "source": "Gmail",
                        "status": STATUS_LABELS[event.status],
                        "notes": "Created from Gmail and linked to job_leads.csv.",
                    }
                )
                enrich_application_from_lead(row, lead, lead_match_type)
                existing_rows.append(row)
                index = len(existing_rows) - 1
                match_type = f"lead:{lead_match_type}"
            elif event.needs_review or event.title == "Unknown" or event.company == "Unknown":
                matches[event.message_id] = ""
                continue
            else:
                row = {column: "Unknown" for column in APPLIED_COLUMNS}
                row.update(
                    {
                        "date_applied": (
                            event.email_date[:10]
                            if event.status == "application_received"
                            else "Unknown"
                        ),
                        "title": event.title,
                        "company": event.company,
                        "source": "Gmail",
                        "status": STATUS_LABELS[event.status],
                        "notes": "Created from a parsed Gmail application-status email.",
                    }
                )
                existing_rows.append(row)
                index = len(existing_rows) - 1
                match_type = "new-high-confidence"

        row = existing_rows[index]
        old_updated = row.get("status_updated_at", "")
        if old_updated not in {"", "Unknown"} and old_updated > event.email_date:
            matches[event.message_id] = match_type
            continue
        row.update(
            {
                "status": STATUS_LABELS[event.status],
                "status_updated_at": event.email_date,
                "latest_email_date": event.email_date,
                "latest_email_subject": event.subject,
                "latest_email_from": event.sender,
                "gmail_message_id": event.message_id,
                "parse_confidence": f"{event.confidence:.2f}",
                "needs_review": (
                    "Yes"
                    if event.needs_review
                    or match_type.endswith("company-only")
                    or match_type.endswith("title-only")
                    else "No"
                ),
                "next_action": next_action_for(event.status),
            }
        )
        add_tracking_source(row, "Gmail")
        row["application_key"] = canonical_application_key(
            row.get("title", ""), row.get("company", "")
        )
        matches[event.message_id] = match_type
    return existing_rows, matches


def next_action_for(status: str) -> str:
    """Translate a parsed status into a concise human follow-up note."""
    return {
        "application_received": "Monitor email and the employer portal.",
        "viewed": "Wait for employer contact; follow up if appropriate after 5 business days.",
        "under_review": "Monitor for interview or assessment instructions.",
        "interview": "Review the email and schedule or prepare for the interview.",
        "assessment_or_action_required": "Review requirements before completing any assessment or providing information.",
        "offer": "Review the written terms carefully before responding.",
        "rejected": "Close the application and retain the employer/role history.",
        "withdrawn": "Close the application; confirm whether the employer cancelled the role.",
        "status_update": "Read the email and manually confirm the new status.",
    }[status]


def existing_event_ids(path: Path) -> set[str]:
    """Collect already-recorded Gmail message IDs so syncs stay append-only."""
    _, rows = read_csv(path)
    return {row.get("message_id", "") for row in rows if row.get("message_id")}


def event_row(event: ParsedMessage, match: str) -> dict[str, Any]:
    """Convert a parsed Gmail event into the append-only CSV event row shape."""
    row = asdict(event)
    row["needs_review"] = "Yes" if event.needs_review or not match else "No"
    row["matched_application"] = match or "Unmatched"
    return row


def relink_event_rows(
    events: list[dict[str, str]], applications: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    """Refresh event-to-application links after the application ledger changes."""
    by_message_id = {
        row.get("gmail_message_id", ""): (
            row.get("application_key", ""),
            row.get("needs_review", "Unknown"),
        )
        for row in applications
        if not is_unknown(row.get("gmail_message_id"))
    }
    for event in events:
        message_id = event.get("message_id", "")
        if message_id in by_message_id:
            key, application_review = by_message_id[message_id]
            event["matched_application"] = key
            if application_review == "No":
                event["needs_review"] = "No"
            continue
        matches = [
            row
            for row in applications
            if (
                values_match(event.get("title", ""), row.get("title", ""), kind="title")
                and (
                    event.get("company") in {"", "Unknown"}
                    or values_match(
                        event.get("company", ""), row.get("company", ""), kind="company"
                    )
                )
            )
        ]
        if len(matches) == 1:
            event["matched_application"] = matches[0].get("application_key", "Unmatched")
        elif is_unknown(event.get("matched_application")):
            event["matched_application"] = "Unmatched"
    return events


def parse_iso_datetime(value: str) -> datetime:
    """Parse a stored ISO timestamp into a timezone-aware datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_sync(config: Config, *, dry_run: bool, relearn: bool) -> int:
    """Run the full Gmail sync pipeline, optionally without writing local files."""
    state_path = Path(config.state_file)
    learned_path = Path(config.learned_patterns_file)
    events_path = Path(config.events_file)
    applied_path = Path(config.applied_jobs_file)
    leads_path = Path(config.job_leads_file)
    state = load_state(state_path)
    service = build_service(config)

    if relearn or not learned_path.exists():
        learned = learn_patterns(service, config)
        if not dry_run:
            save_json(learned_path, learned)
    else:
        learned = json.loads(learned_path.read_text(encoding="utf-8"))
    learned_domains = set(learned.get("sender_domains", {}))

    seen_ids = existing_event_ids(events_path)
    legacy_zero_result_query = (
        "-in:spam -in:trash -in:sent" in str(state.get("initial_query", ""))
        and not seen_ids
    )
    initial_run = not bool(state.get("last_successful_sync")) or legacy_zero_result_query
    if initial_run:
        query = query_for_initial(config.initial_lookback_days, learned)
    else:
        query = query_for_incremental(
            parse_iso_datetime(state["last_successful_sync"]),
            config.sync_overlap_days,
            learned,
        )

    parsed_events: list[ParsedMessage] = []
    candidate_count = 0
    initial_cutoff = datetime.now(timezone.utc) - timedelta(days=config.initial_lookback_days)
    for message_id in list_message_ids(
        service, query, config.max_sync_messages, include_trash=config.include_trash
    ):
        candidate_count += 1
        if message_id in seen_ids:
            continue
        raw_message = fetch_message(service, message_id)
        labels = set(raw_message.get("labelIds", []) or [])
        blocked_labels = {"SPAM", "SENT"}
        if not config.include_trash:
            blocked_labels.add("TRASH")
        if labels & blocked_labels:
            continue
        parsed = parse_message(raw_message, learned_domains=learned_domains)
        if not parsed:
            continue
        if initial_run and parse_iso_datetime(parsed.email_date) < initial_cutoff:
            continue
        parsed_events.append(parsed)

    _, lead_rows = read_csv(leads_path)
    _, applied_rows = read_csv(applied_path)
    if not applied_rows and not applied_path.exists():
        applied_rows = []
    pre_dedupe_count = len(applied_rows)
    applied_rows = link_leads_to_applications(applied_rows, lead_rows)
    applied_rows = deduplicate_application_rows(applied_rows)
    duplicate_rows_removed = pre_dedupe_count - len(applied_rows)
    applied_rows, matches = update_application_rows(applied_rows, parsed_events, lead_rows)
    applied_rows = link_leads_to_applications(applied_rows, lead_rows)
    applied_rows = deduplicate_application_rows(applied_rows)

    _, old_event_rows = read_csv(events_path)
    new_event_rows = [event_row(event, matches.get(event.message_id, "")) for event in parsed_events]
    all_event_rows = old_event_rows + new_event_rows
    all_event_rows.sort(key=lambda row: row.get("email_date", ""))
    all_event_rows = relink_event_rows(all_event_rows, applied_rows)

    print(
        f"{'Initial' if initial_run else 'Incremental'} sync: "
        f"{candidate_count} candidate messages, {len(parsed_events)} new status events, "
        f"{sum(1 for event in parsed_events if event.needs_review or not matches.get(event.message_id))} need review, "
        f"{sum(1 for row in applied_rows if row.get('lead_match_type') not in {'', 'Unknown', None})} applications linked to job leads, "
        f"{max(0, duplicate_rows_removed)} duplicates removed."
    )
    if dry_run:
        for event in parsed_events:
            print(
                f"- {event.email_date[:10]} | {STATUS_LABELS[event.status]} | "
                f"{event.title} | {event.company} | confidence={event.confidence:.2f}"
            )
        return 0

    write_csv(applied_path, APPLIED_COLUMNS, applied_rows)
    write_csv(events_path, EVENT_COLUMNS, all_event_rows)
    now = datetime.now(timezone.utc).isoformat()
    save_json(
        state_path,
        {
            "account": config.account,
            "last_successful_sync": now,
            "initial_lookback_days": config.initial_lookback_days,
            "initial_query": query if initial_run else state.get("initial_query"),
            "pattern_version": PATTERN_VERSION,
        },
    )
    return 0


def run_diagnostic(config: Config, limit: int) -> int:
    """Print a no-write sample of candidate mail and parser decisions."""
    service = build_service(config)
    learned_path = Path(config.learned_patterns_file)
    learned = (
        json.loads(learned_path.read_text(encoding="utf-8"))
        if learned_path.exists()
        else {}
    )
    query = query_for_initial(config.initial_lookback_days, learned)
    print(f"Diagnostic query: {query}")
    for message_id in list_message_ids(
        service, query, limit, include_trash=config.include_trash
    ):
        message = fetch_message(service, message_id)
        headers = message_headers(message)
        body = payload_text(message.get("payload", {}))
        status, evidence = classify_status(headers.get("subject", ""), body)
        print(
            json.dumps(
                {
                    "message_id": message_id,
                    "subject": headers.get("subject", "")[:160],
                    "sender_domain": sender_domain(headers.get("from", "")),
                    "body_characters": len(body),
                    "detected_status": status or None,
                    "evidence": evidence or None,
                    "body_sample": normalize_space(body)[:240],
                },
                ensure_ascii=True,
            )
        )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Define the command-line interface for sync, dry-run, relearn, and diagnose modes."""
    parser = argparse.ArgumentParser(
        description="Track job-application submissions and status updates from Gmail."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Configuration JSON path (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and parse Gmail without changing local tracking files.",
    )
    parser.add_argument(
        "--relearn",
        action="store_true",
        help="Re-scan matching full-history mail and rebuild learned sender/subject patterns.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactively collect and save the Gmail account without running a sync.",
    )
    parser.add_argument(
        "--diagnose",
        type=int,
        metavar="N",
        help="Read and print a no-write diagnostic sample of N recent candidate messages.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the selected command-line mode and normalize top-level failures."""
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        config = resolve_account(config, args.config)
        if args.setup:
            return 0
        if args.diagnose:
            return run_diagnostic(config, args.diagnose)
        return run_sync(config, dry_run=args.dry_run, relearn=args.relearn)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
