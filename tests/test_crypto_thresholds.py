from datetime import datetime, timezone

from technical_analysis.crypto.alerts import build_email_content


def test_volume_and_price_threshold_alert_is_formatted():
    hits = [
        {
            "pair": "BTC/USD",
            "direction": "UP",
            "price_change_pct": 2.0,
            "body_ratio": 0.7,
            "range_low": 100.0,
            "range_high": 110.0,
            "volume": 1500.0,
            "volume_ratio": 3.2,
            "close": 112.0,
            "candle_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        }
    ]

    subject, body = build_email_content("crypto_alert", hits=hits, interval_minutes=15)

    assert "Kraken Crypto Alert" in subject
    assert "BTC/USD" in body
    assert "3.2x" in body
    assert "2.00%" in body
