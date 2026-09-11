# NSE Death Cross Telegram Alert

Automated NSE Death Cross scanner.

## Signal

A Death Cross is detected when:

- Previous completed day: 50 SMA >= 200 SMA
- Latest completed day: 50 SMA < 200 SMA

## Timeframe

1 Day

## Universe

NSE Equity (EQ) stocks

## Schedule

Runs automatically every hour.

## Alert

Fresh Death Cross signals are sent to the configured Telegram bot.

## Important

Only fresh, verified crossovers are alerted.
