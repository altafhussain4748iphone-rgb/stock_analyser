import importlib
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from technical_analysis.crypto import alerts as ca
from technical_analysis.common import emailing


class EmailFormattingTests(unittest.TestCase):
    def test_crypto_alert_email_is_well_formatted(self):
        hits = [
            {
                "pair": "BTC/USD",
                "direction": "UP",
                "price_change_pct": 3.5,
                "body_ratio": 0.7,
                "range_low": 100.0,
                "range_high": 110.0,
                "volume": 1500.0,
                "volume_ratio": 4.2,
                "close": 112.0,
                "candle_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
            }
        ]

        subject, body = ca.build_email_content("crypto_alert", hits=hits, interval_minutes=15)

        self.assertIn("Kraken Crypto Alert", subject)
        self.assertIn("BTC/USD", body)
        self.assertIn("UP breakout", body)
        self.assertIn("Summary", body)

    def test_error_alert_email_is_well_formatted(self):
        subject, body = ca.build_email_content(
            "error",
            error_message="Kraken OHLC request failed",
            context="pair=BTC/USD",
        )

        self.assertIn("Kraken API Error", subject)
        self.assertIn("Kraken OHLC request failed", body)
        self.assertIn("pair=BTC/USD", body)
        self.assertIn("Details", body)

    def test_blank_smtp_port_defaults_to_465(self):
        previous = os.environ.get("SMTP_PORT")
        os.environ["SMTP_PORT"] = ""
        try:
            from technical_analysis.config import get_smtp_settings

            settings = get_smtp_settings()
        finally:
            if previous is None:
                os.environ.pop("SMTP_PORT", None)
            else:
                os.environ["SMTP_PORT"] = previous

        self.assertEqual(settings["port"], 465)

    def test_email_send_failure_is_swallowed(self):
        with mock.patch.object(emailing, "get_smtp_settings", return_value={
            "host": "smtp.example.com",
            "port": 465,
            "user": "user@example.com",
            "password": "secret",
            "to": "to@example.com",
        }), mock.patch("technical_analysis.common.emailing.smtplib.SMTP") as smtp_cls, mock.patch("technical_analysis.common.emailing.smtplib.SMTP_SSL") as smtp_ssl_cls:
            smtp_cls.side_effect = Exception("boom")
            smtp_ssl_cls.side_effect = Exception("boom")
            emailing.send_email_message("subject", "body")

    def test_crypto_alert_send_uses_shared_smtp_config(self):
        dummy_hit = {
            "pair": "BTC/USD",
            "direction": "UP",
            "price_change_pct": 3.5,
            "body_ratio": 0.7,
            "range_low": 100.0,
            "range_high": 110.0,
            "volume": 1500.0,
            "volume_ratio": 4.2,
            "close": 112.0,
            "candle_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        }

        with mock.patch.object(emailing, "get_smtp_settings", return_value={
            "host": "smtp.example.com",
            "port": 465,
            "user": "cfg@example.com",
            "password": "secret",
            "to": "to@example.com",
        }), mock.patch("technical_analysis.common.emailing.smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value.ehlo.return_value = None
            smtp_cls.return_value.__enter__.return_value.starttls.return_value = None
            smtp_cls.return_value.__enter__.return_value.login.return_value = None
            smtp_cls.return_value.__enter__.return_value.sendmail.return_value = None
            ca.send_crypto_alert([dummy_hit], 15)
            self.assertTrue(smtp_cls.called)


if __name__ == "__main__":
    unittest.main()
