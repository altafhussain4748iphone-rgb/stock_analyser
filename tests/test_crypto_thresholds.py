from datetime import datetime, timezone

from technical_analysis.crypto.alerts import build_combined_email


def test_volume_surge_alert_is_formatted():
    hits_by_analysis = {
        "breakout": [],
        "volume_surge": [
            {
                "pair": "BTC/USD",
                "pair_key": "XBTUSD",
                "direction": "UP",
                "price_change_pct": 2.0,
                "close": 112.0,
                "quote_volume": 90_000.0,
                "quote_volume_24h": 1_800_000.0,
                "volume_multiple": 3.2,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "1 volume surge alerts" in subject
    assert "BTC/USD" in body
    assert "3.2x" in body
    assert "+2.00%" in body


def test_momentum_surge_alert_is_formatted():
    hits_by_analysis = {
        "breakout": [],
        "volume_surge": [],
        "ema_trend_pullback": [],
        "momentum_surge": [
            {
                "pair": "ADA/USD",
                "pair_key": "ADAUSD",
                "direction": "UP",
                "price_change_pct": 5.5,
                "close": 0.4430,
                "average_signal_volume": 120_000.0,
                "average_baseline_volume": 90_000.0,
                "quote_volume_24h": 2_100_000.0,
                "volume_multiple": 1.33,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "1 momentum surge alerts" in subject
    assert "ADA/USD" in body
    assert "1.33x" in body
    assert "+5.50%" in body
