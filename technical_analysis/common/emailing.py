import logging
import smtplib
import ssl
from email.mime.text import MIMEText

from technical_analysis.config import get_smtp_settings

log = logging.getLogger("kraken_alert")


def send_email_message(subject: str, body: str, *, html: bool = True, logger=None) -> None:
    settings = get_smtp_settings()
    if not settings["user"] or not settings["password"] or not settings["to"]:
        target_logger = logger or log
        target_logger.warning("SMTP credentials not configured -- skipping email, printing content instead.")
        target_logger.info(body)
        return

    msg = MIMEText(body, "html" if html else "plain")
    msg["Subject"] = subject
    msg["From"] = settings["user"]
    msg["To"] = settings["to"]

    try:
        with smtplib.SMTP(settings["host"], 587, timeout=15) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(settings["user"], settings["password"])
            server.sendmail(settings["user"], [settings["to"]], msg.as_string())
    except Exception as tls_exc:
        try:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=15) as server:
                server.login(settings["user"], settings["password"])
                server.sendmail(settings["user"], [settings["to"]], msg.as_string())
        except Exception as exc:
            (logger or log).warning(f"Email delivery failed: {exc}")
            if logger is None:
                log.debug(f"Primary SMTP TLS failure: {tls_exc}")
