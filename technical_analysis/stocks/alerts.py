import logging

from technical_analysis.common.emailing import send_email_message

log = logging.getLogger(__name__)


def run_stock_scan() -> None:
    """Placeholder entrypoint for stock scanning workflows."""
    subject = "Stock Analysis Scan Triggered"
    body = "<html><body><h2>Stock workflow ready</h2><p>Extend this module with your stock analysis logic.</p></body></html>"
    send_email_message(subject, body)
    log.info("Stock scan placeholder executed")
