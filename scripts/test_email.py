#!/usr/bin/env python3
"""Send a one-off test email using the configured SMTP settings.

Usage:
    python scripts/test_email.py [recipient@example.com]

If no recipient is given, falls back to ALERT_EMAIL_TO / EMAIL_TO from the environment.
Exits non-zero if credentials are missing or sending fails.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from technical_analysis.common.emailing import send_email_message
from technical_analysis.config import get_smtp_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("test_email")


def main() -> int:
    settings = get_smtp_settings()
    if len(sys.argv) > 1:
        settings["to"] = sys.argv[1]

    if not settings["user"] or not settings["password"] or not settings["to"]:
        log.error(
            "Missing SMTP config. Need SMTP_USER, SMTP_PASS and a recipient "
            "(ALERT_EMAIL_TO/EMAIL_TO, or pass one as an argument)."
        )
        return 1

    log.info(f"Sending test email via {settings['host']}:{settings['port']} to {settings['to']}...")

    class _CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.failed = False

        def emit(self, record):
            if record.levelno >= logging.WARNING:
                self.failed = True

    capture = _CaptureHandler()
    email_log = logging.getLogger("kraken_alert")
    email_log.addHandler(capture)
    try:
        send_email_message(
            subject="Test email from stocks_analyser",
            body="<p>This is a test email confirming SMTP sending works.</p>",
            html=True,
            logger=email_log,
        )
    finally:
        email_log.removeHandler(capture)

    if capture.failed:
        log.error("Email sending failed -- see warnings above.")
        return 1

    log.info("Email sent successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
