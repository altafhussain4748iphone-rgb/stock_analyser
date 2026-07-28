# technical_analysis

A modular automation project for crypto and stock analysis alerts.

## What is included
- Crypto workflow for Kraken breakout detection and email alerts
- Stock workflow scaffold for future analysis scripts
- Shared configuration and email helpers for easy expansion

## Project layout
- [technical_analysis/cli.py](technical_analysis/cli.py) — entrypoint for running workflows
- [technical_analysis/config.py](technical_analysis/config.py) — shared environment/config utilities
- [technical_analysis/common/emailing.py](technical_analysis/common/emailing.py) — shared email sending logic
- [technical_analysis/crypto/alerts.py](technical_analysis/crypto/alerts.py) — Kraken crypto alert workflow
- [technical_analysis/stocks/alerts.py](technical_analysis/stocks/alerts.py) — stock workflow scaffold

## Setup
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

## Run locally
```bash
python -m technical_analysis.cli crypto
python -m technical_analysis.cli stocks
```

## Test email locally
You can validate the SMTP configuration without running the full alert workflow:

```bash
python scripts/test_email_local.py --dry-run
python scripts/test_email_local.py
```

The script loads variables from .env if present and uses the shared email helper directly.

The older standalone script entrypoint is no longer used; the package-based CLI is now the supported path.

## Environment variables
Set these in your shell, or as Terraform variables when deploying (see below):
- SMTP_HOST
- SMTP_PORT
- SMTP_USER
- SMTP_PASS
- ALERT_EMAIL_TO or EMAIL_TO
- CANDLE_INTERVAL_MIN
- LOG_FILE (optional)
- LOG_LEVEL (optional)
- ALERT_STATE_FILE (optional)

## Deployment
Runs on Google Cloud Run Jobs + Cloud Scheduler, provisioned with Terraform. Deploys are always run locally via `scripts/deploy.sh` — no GitHub Actions, no CI/CD. See [DEPLOYMENT.md](DEPLOYMENT.md) for setup, how to deploy, how to add a new scheduled script, how to pause/resume a job's alerts, and how to view logs.
