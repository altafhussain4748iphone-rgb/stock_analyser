import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from technical_analysis.crypto import alerts as ca
from technical_analysis.common import emailing


def _breakout_hit(**overrides):
    hit = {
        "pair": "BTC/USD",
        "pair_key": "XBTUSD",
        "direction": "UP",
        "price_change_pct": 3.5,
        "close": 112.0,
        "breakout_level": 110.0,
        "breakout_strength_atr": 1.2,
        "body_ratio": 0.7,
        "body_atr": 0.9,
        "close_extreme_ratio": 0.9,
        "quote_volume": 150_000.0,
        "quote_volume_24h": 2_500_000.0,
        "volume_multiple": 4.2,
        "volume_robust_z": 5.0,
        "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        "signal_epoch": 1,
        "confirmation_time": None,
        "confirmation_close": None,
        "confirmed_by_next_candle": False,
        "alert_epoch": 1,
    }
    hit.update(overrides)
    return hit


def _momentum_surge_hit(**overrides):
    hit = {
        "pair": "ETH/USD",
        "pair_key": "ETHUSD",
        "direction": "UP",
        "price_change_pct": 5.5,
        "close": 3200.0,
        "average_signal_volume": 120_000.0,
        "average_baseline_volume": 90_000.0,
        "quote_volume_24h": 1_800_000.0,
        "volume_multiple": 1.33,
        "ema_fast": 0.4300,
        "ema_slow": 0.4100,
        # Drives the LIQUID/THIN badge; render_momentum_surge_item requires it.
        "volume_ok": True,
        "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        "signal_epoch": 1,
        "alert_epoch": 1,
    }
    hit.update(overrides)
    return hit


class EmailFormattingTests(unittest.TestCase):
    def test_combined_email_includes_only_sections_with_hits(self):
        hits_by_analysis = {"breakout": [_breakout_hit()], "momentum_surge": []}

        subject, body = ca.build_combined_email(hits_by_analysis, 15, False)

        self.assertIn("1 confirmed breakouts", subject)
        self.assertIn("BTC/USD", body)
        self.assertIn("UP breakout", body)
        self.assertNotIn("Momentum Surge Alerts", body)

    def test_combined_email_renders_both_sections(self):
        hits_by_analysis = {
            "breakout": [_breakout_hit()],
            "momentum_surge": [_momentum_surge_hit()],
        }

        subject, body = ca.build_combined_email(hits_by_analysis, 15, False)

        self.assertIn("Confirmed Breakouts", body)
        self.assertIn("Momentum Surge Alerts", body)
        self.assertIn("BTC/USD", body)
        self.assertIn("ETH/USD", body)

    def test_error_alert_email_is_well_formatted(self):
        subject, body = ca.build_error_email(
            15,
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

    def test_combined_alert_send_uses_shared_smtp_config(self):
        hits_by_analysis = {"breakout": [_breakout_hit()], "momentum_surge": []}

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
            ca.send_combined_alert(hits_by_analysis, 15)
            self.assertTrue(smtp_cls.called)


if __name__ == "__main__":
    unittest.main()
