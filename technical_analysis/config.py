import os
from typing import Dict

DEFAULT_SMTP_PORT = 465
DEFAULT_INTERVAL_MINUTES = 15


def get_env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def get_smtp_settings() -> Dict[str, object]:
    port_value = get_env("SMTP_PORT", str(DEFAULT_SMTP_PORT))
    port = int(port_value) if port_value else DEFAULT_SMTP_PORT
    return {
        "host": get_env("SMTP_HOST", "smtp.gmail.com"),
        "port": port,
        "user": get_env("SMTP_USER"),
        "password": get_env("SMTP_PASS"),
        "to": get_env("ALERT_EMAIL_TO") or get_env("EMAIL_TO"),
    }


def get_interval_minutes(default: int = DEFAULT_INTERVAL_MINUTES) -> int:
    value = get_env("CANDLE_INTERVAL_MIN", str(default))
    try:
        return int(value)
    except ValueError:
        return default
