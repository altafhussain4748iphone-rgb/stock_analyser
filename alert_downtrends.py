#!/usr/bin/env python3
import os
import sys
import argparse
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf

def parse_args():
    p = argparse.ArgumentParser(description="Alert if any ticker has 7 consecutive down days.")
    p.add_argument("--tickers", type=str, default=os.getenv("TICKERS", ""),
                   help="Comma-separated list of tickers. If empty, will read tickers.txt if present.")
    p.add_argument("--days_check", type=int, default=7, help="Consecutive down days threshold.")
    p.add_argument("--days_fetch", type=int, default=30, help="Trading days history to fetch.")
    p.add_argument("--last_n_to_show", type=int, default=4, help="How many most-recent closes to show in the email table.")
    p.add_argument("--email_to", type=str, default=os.getenv("EMAIL_TO", "you@example.com"))
    p.add_argument("--email_from", type=str, default=os.getenv("EMAIL_FROM", os.getenv("SMTP_USER", "")))
    p.add_argument("--smtp_user", type=str, default=os.getenv("SMTP_USER", ""))
    p.add_argument("--smtp_pass", type=str, default=os.getenv("SMTP_PASS", ""))
    p.add_argument("--smtp_host", type=str, default=os.getenv("SMTP_HOST", "smtp.gmail.com"))
    p.add_argument("--smtp_port", type=int, default=int(os.getenv("SMTP_PORT", "465")))
    return p.parse_args()

def load_tickers(cli_tickers: str):
    tickers = []
    if cli_tickers.strip():
        tickers = [t.strip() for t in cli_tickers.split(",") if t.strip()]
    elif os.path.exists("tickers.txt"):
        with open("tickers.txt") as f:
            tickers = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        # Default example – edit/remove as you like
        tickers = ["AAPL", "MSFT", "TSLA", "AMZN", "GOOGL"]
    if not tickers:
        raise SystemExit("No tickers provided.")
    return sorted(set(tickers))

def is_consecutive_down(closes: pd.Series, n: int) -> bool:
    """
    True if last n trading sessions are strictly down vs the previous day.
    Requires at least n+1 data points.
    """
    closes = closes.dropna()
    if len(closes) < n + 1:
        return False
    # Take last n+1 closes, compute day-over-day diff, check all last n diffs < 0
    diffs = closes.tail(n + 1).diff().dropna()
    return (diffs < 0).all()

def build_email_html(matches, last_n_to_show):
    if not matches:
        return None

    # Build a table of matching tickers with their last N closes
    rows = []
    for tkr, series in matches:
        closes = series.dropna().tail(last_n_to_show)
        # format as date: price
        cells = "".join(
            f"<div>{idx.strftime('%Y-%m-%d')}: {val:,.2f}</div>" for idx, val in closes.items()
        )
        rows.append(f"<tr><td><b>{tkr}</b></td><td>{cells}</td></tr>")

    table = f"""
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
      <thead>
        <tr><th>Ticker</th><th>Last {last_n_to_show} closes</th></tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """
    return f"""
    <html>
    <body>
      <p>The following tickers have <b>7 consecutive down days</b> (by daily close):</p>
      {table}
      <p style="font-size:12px;color:#666;">
        Source: Yahoo Finance (via yfinance). Trading days only; weekends/holidays automatically skipped.
      </p>
    </body>
    </html>
    """

def send_email(subject, html_body, email_from, email_to, smtp_host, smtp_port, smtp_user, smtp_pass):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    text_fallback = "Your email client does not support HTML.\n\n" \
                    "Tickers matched 7 consecutive down days."
    msg.attach(MIMEText(text_fallback, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, [email_to], msg.as_string())

def main():
    args = parse_args()
    tickers = load_tickers(args.tickers)

    # Fetch a buffer of history (default ~30 trading days)
    # auto_adjust=True to use adjusted close (splits/dividends handled)
    data = yf.download(
        tickers=tickers,
        period=f"{args.days_fetch}d",
        interval="1d",
        auto_adjust=True,
        group_by="ticker",
        threads=True,
        progress=False,
    )

    # Normalize to a dict: {ticker: Series of Close}
    closes_by_ticker = {}
    if isinstance(data.columns, pd.MultiIndex):
        for t in tickers:
            if (t, "Close") in data.columns:
                closes_by_ticker[t] = data[(t, "Close")].dropna()
    else:
        # Single ticker shape
        closes_by_ticker[tickers[0]] = data["Close"].dropna()

    matches = []
    for t, series in closes_by_ticker.items():
        if is_consecutive_down(series, n=args.days_check):
            matches.append((t, series))

    if matches:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subject = f"[Stock Alert] {len(matches)} ticker(s) down {args.days_check} days in a row — {now_utc}"
        html_body = build_email_html(matches, args.last_n_to_show)
        if not args.smtp_user or not args.smtp_pass:
            print("WARNING: SMTP credentials not set; printing email to stdout instead.\n")
            print(subject)
            print(html_body or "")
            sys.exit(0)
        send_email(
            subject=subject,
            html_body=html_body,
            email_from=args.email_from,
            email_to=args.email_to,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user=args.smtp_user,
            smtp_pass=args.smtp_pass,
        )
        print(f"Email sent for {len(matches)} ticker(s).")
    else:
        print("No tickers with 7 consecutive down days today.")

if __name__ == "__main__":
    main()
