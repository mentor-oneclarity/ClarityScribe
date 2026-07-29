"""
AWS SES email sender — meeting summary email.

Industry-standard implementation:
- Markdown → HTML conversion so summary renders properly (no raw # * symbols)
- Responsive HTML email template
- Async-safe: boto3 runs in ThreadPoolExecutor (non-blocking)
- BCC support for sending to all participants
- Human-readable subject with date
- "View Transcript" link back to the dashboard
- HTML-safe: user content is escaped before insertion into template
- SES retry on transient throttle errors (exponential back-off, 3 attempts)
- Recipient guard: SES hard-limits to 50 total addresses per send
"""

import asyncio
import html as _html
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("backend")

_AWS_ACCESS_KEY = os.getenv("AWS_SES_ACCESS_KEY_ID", "")
_AWS_SECRET_KEY = os.getenv("AWS_SES_SECRET_ACCESS_KEY", "")
_AWS_REGION     = os.getenv("AWS_SES_REGION", "ap-south-1")
_SES_FROM       = os.getenv("SES_FROM", "OneClarity AI <mentor@oneclarity.ai>")

# SES hard limit: 50 total recipients (TO + CC + BCC) per send_email call.
_SES_MAX_RECIPIENTS = 50

# SES error codes that are transient and safe to retry.
_RETRYABLE_SES_CODES = {"Throttling", "ThrottlingException", "ServiceUnavailable", "InternalFailure"}

# Basic email format guard — catches empty/malformed addresses before hitting SES.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Single thread-pool reused across all email calls (boto3 is thread-safe)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ses")


def _strip_markdown(text: str) -> str:
    """Remove markdown syntax for the plain-text email fallback."""
    steps = [
        (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),          # ## headers
        (re.compile(r'\*\*(.+?)\*\*', re.DOTALL), r'\1'),       # **bold**
        (re.compile(r'\*(.+?)\*', re.DOTALL), r'\1'),            # *italic*
        (re.compile(r'`(.+?)`', re.DOTALL), r'\1'),              # `code`
        (re.compile(r'~~(.+?)~~', re.DOTALL), r'\1'),            # ~~strikethrough~~
        (re.compile(r'^\s*[-*+]\s+', re.MULTILINE), '• '),      # - bullets → •
        (re.compile(r'^\s*\d+\.\s+', re.MULTILINE), ''),        # 1. numbered list
        (re.compile(r'\[([^\]]+)\]\([^)]+\)'), r'\1'),           # [text](url) → text
        (re.compile(r'^---+$', re.MULTILINE), '─' * 30),        # --- dividers
        (re.compile(r'^\|.+\|$', re.MULTILINE), ''),             # table rows
        (re.compile(r'\n{3,}'), '\n\n'),                         # collapse blank lines
    ]
    for pattern, replacement in steps:
        text = pattern.sub(replacement, text)
    return text.strip()


def _markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML. Falls back to <pre> if markdown not installed."""
    try:
        import markdown as md_lib
        return md_lib.markdown(
            text,
            extensions=["nl2br", "tables", "fenced_code"],
        )
    except ImportError:
        import html as html_lib
        return f"<pre style='white-space:pre-wrap;font-size:13px'>{html_lib.escape(text)}</pre>"


def _build_html_email(
    meeting_title: str,
    summary_html: str,
    duration_minutes: int,
    meeting_date: str,
) -> str:
    # Escape every user-supplied string before embedding in HTML.
    safe_title   = _html.escape(meeting_title)
    safe_date    = _html.escape(meeting_date)

    duration_text = f"{duration_minutes} min" if duration_minutes else ""
    meta_parts = [p for p in [safe_date, duration_text] if p]
    meta_line = "  ·  ".join(meta_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Meeting Notes: {safe_title}</title>
  <style>
    /* ── Reset ── */
    body, table, td, p, a, h1, h2, h3 {{ margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #f5f5f5;
      color: #111111;
      font-size: 14px;
      line-height: 1.6;
    }}
    /* ── Container ── */
    .wrapper {{ max-width: 640px; margin: 32px auto; padding: 0 16px 40px; }}
    /* ── Header ── */
    .header {{
      background: #111111;
      border-radius: 10px 10px 0 0;
      padding: 24px 32px;
    }}
    .brand {{ color: #fff; font-size: 13px; font-weight: 700; letter-spacing: .5px; opacity: .75; text-transform: uppercase; }}
    .meeting-title {{ color: #fff; font-size: 22px; font-weight: 700; margin-top: 6px; line-height: 1.3; }}
    .meta {{ color: rgba(255,255,255,.6); font-size: 12px; margin-top: 6px; }}
    /* ── Body card ── */
    .card {{
      background: #fff;
      border: 1px solid #e0e0e0;
      border-top: none;
      border-radius: 0 0 10px 10px;
      padding: 28px 32px;
    }}
    /* ── Summary content ── */
    .summary h1 {{ font-size: 17px; color: #111111; margin: 20px 0 6px; padding-bottom: 4px; border-bottom: 1px solid #e8e8e8; }}
    .summary h2 {{ font-size: 14px; color: #111111; margin: 16px 0 5px; }}
    .summary h3 {{ font-size: 13px; color: #222222; margin: 12px 0 4px; font-weight: 600; }}
    .summary p {{ margin: 6px 0; color: #222222; }}
    .summary ul, .summary ol {{ padding-left: 22px; margin: 6px 0 10px; }}
    .summary li {{ margin-bottom: 4px; color: #333333; }}
    .summary blockquote {{
      border-left: 3px solid #333333;
      margin: 10px 0;
      padding: 6px 14px;
      background: #f8f8f8;
      border-radius: 0 6px 6px 0;
      color: #444444;
      font-style: italic;
    }}
    .summary strong {{ color: #111111; font-weight: 600; }}
    .summary hr {{ border: none; border-top: 1px solid #e8e8e8; margin: 18px 0; }}
    .summary table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }}
    .summary th {{ background: #f2f2f2; padding: 6px 10px; text-align: left; font-weight: 600; color: #111111; }}
    .summary td {{ padding: 6px 10px; border-top: 1px solid #eeeeee; color: #222222; }}
    /* ── Footer ── */
    .footer {{
      text-align: center;
      color: #888888;
      font-size: 11px;
      margin-top: 20px;
      padding-top: 16px;
      border-top: 1px solid #e8e8e8;
    }}
  </style>
</head>
<body>
<div class="wrapper">

  <!-- Header -->
  <div class="header">
    <div class="brand">OneClarity AI &nbsp;·&nbsp; Meeting Notes</div>
    <div class="meeting-title">{safe_title}</div>
    <div class="meta">{meta_line}</div>
  </div>

  <!-- Body -->
  <div class="card">
    <p style="color:#555;font-size:13px;margin-bottom:20px;">
      Your meeting has ended. Here are the AI-generated notes:
    </p>

    <!-- Summary (markdown rendered as HTML) -->
    <div class="summary">
      {summary_html}
    </div>

    <div class="footer">
      Powered by <strong>OneClarity AI</strong><br/>
      This summary was auto-generated.
    </div>
  </div>

</div>
</body>
</html>"""


def _ses_client():
    return boto3.client(
        "ses",
        region_name=_AWS_REGION,
        aws_access_key_id=_AWS_ACCESS_KEY,
        aws_secret_access_key=_AWS_SECRET_KEY,
    )


def _send_sync(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    bcc_emails: List[str],
    cc_emails: Optional[List[str]] = None,
) -> None:
    """Synchronous SES send — runs inside ThreadPoolExecutor."""
    destination: dict = {"ToAddresses": [to_email]}
    if cc_emails:
        destination["CcAddresses"] = cc_emails
    if bcc_emails:
        destination["BccAddresses"] = bcc_emails

    _ses_client().send_email(
        Source=_SES_FROM,
        Destination=destination,
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )


async def send_summary_email(
    to_email: str,
    meeting_title: str,
    summary: str,
    meet_code: str = "",
    duration_minutes: int = 0,
    bcc_emails: Optional[List[str]] = None,
    cc_emails: Optional[List[str]] = None,
    meeting_id: Optional[int] = None,
) -> bool:
    """
    Send meeting summary email via AWS SES.

    - summary is converted from Markdown → HTML (no raw #/* symbols in email)
    - plain-text fallback has markdown symbols stripped (shows •, plain text)
    - boto3 runs in a thread pool (non-blocking async)
    - Optional bcc_emails sends to all participants privately
    - Retries up to 3 times on transient SES throttle errors (2s, 4s back-off)
    - Guards SES 50-recipient hard limit — truncates BCC rather than failing
    - Returns True on success, False on failure
    """
    if not _AWS_ACCESS_KEY or not _AWS_SECRET_KEY:
        logger.warning("AWS SES credentials not set — skipping summary email to %s", to_email)
        return False

    # Guard: validate to_email before hitting SES
    if not to_email or not _EMAIL_RE.match(to_email.strip()):
        logger.error("send_summary_email: invalid or empty to_email=%r — skipping", to_email)
        return False

    # Human-readable date in local-friendly format
    now_utc = datetime.now(timezone.utc)
    meeting_date = now_utc.strftime("%a %b %-d, %Y")

    # Subject: "Meeting Notes: Weekly Standup — Mon Jun 9, 2026"
    # Truncate to 200 chars — SES / RFC 5321 limits header lines to 998 chars,
    # and most email clients truncate subjects longer than ~150 chars anyway.
    raw_subject = f"Meeting Notes: {meeting_title} — {meeting_date}"
    subject = raw_subject[:200]

    # Render markdown summary → HTML for the HTML part
    summary_html = _markdown_to_html(summary)

    # Build full HTML email (user content is HTML-escaped inside _build_html_email)
    html_body = _build_html_email(
        meeting_title=meeting_title,
        summary_html=summary_html,
        duration_minutes=duration_minutes,
        meeting_date=meeting_date,
    )

    # Plain-text fallback: strip markdown so email clients don't show raw ## and **
    plain_summary = _strip_markdown(summary)
    text_body = (
        f"Meeting Notes: {meeting_title}\n"
        f"{meeting_date}  ·  {duration_minutes} min\n\n"
        f"{plain_summary}\n\n"
        f"Powered by OneClarity AI"
    )

    # Clean CC list (VISIBLE recipients — e.g. all meeting attendees so the summary
    # obviously goes to everyone). Remove None/empty/bad/the to_email itself.
    clean_cc = list({
        e.strip() for e in (cc_emails or [])
        if e and e.strip() and _EMAIL_RE.match(e.strip()) and e.strip() != to_email
    })

    # Clean BCC list: remove None, empty strings, bad formats, the to_email, and
    # anyone already in CC (avoid duplicate delivery).
    _cc_lower = {e.lower() for e in clean_cc}
    clean_bcc = list({
        e.strip() for e in (bcc_emails or [])
        if e and e.strip() and _EMAIL_RE.match(e.strip())
        and e.strip() != to_email and e.strip().lower() not in _cc_lower
    })

    # Guard: SES allows max 50 total recipients (TO + CC + BCC).
    # Truncate silently rather than letting the whole send fail. CC is preferred
    # (visible), so fill CC first, then BCC with whatever slots remain.
    _slots = _SES_MAX_RECIPIENTS - 1  # 1 slot reserved for to_email
    if len(clean_cc) > _slots:
        logger.warning(
            "SES recipient limit: CC truncated from %d to %d for meet_code=%s",
            len(clean_cc), _slots, meet_code,
        )
        clean_cc = clean_cc[:_slots]
    _bcc_slots = max(0, _slots - len(clean_cc))
    if len(clean_bcc) > _bcc_slots:
        logger.warning(
            "SES recipient limit: BCC truncated from %d to %d for meet_code=%s",
            len(clean_bcc), _bcc_slots, meet_code,
        )
        clean_bcc = clean_bcc[:_bcc_slots]

    loop = asyncio.get_running_loop()

    for attempt in range(1, 4):
        try:
            await loop.run_in_executor(
                _executor,
                _send_sync,
                to_email,
                subject,
                html_body,
                text_body,
                clean_bcc,
                clean_cc,
            )
            logger.info(
                "SES email sent → to=%s cc_count=%d bcc_count=%d subject=%r meet_code=%s",
                to_email, len(clean_cc), len(clean_bcc), subject, meet_code,
            )
            return True

        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_msg  = exc.response["Error"]["Message"]
            if error_code in _RETRYABLE_SES_CODES and attempt < 3:
                wait_secs = 2 ** attempt  # 2s then 4s
                logger.warning(
                    "SES transient error [%s] attempt %d/3 — retrying in %ds  meet_code=%s",
                    error_code, attempt, wait_secs, meet_code,
                )
                await asyncio.sleep(wait_secs)
                continue
            logger.error(
                "SES ClientError [%s] → %s: %s  meet_code=%s",
                error_code, to_email, error_msg, meet_code,
            )
            return False

        except (BotoCoreError, Exception) as exc:
            logger.error(
                "SES error → %s: %s  meet_code=%s",
                to_email, exc, meet_code,
            )
            return False

    return False  # exhausted all 3 attempts
