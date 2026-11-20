# Repository audit — first pass

## Findings
- Python trading software targeting Deriv WebSocket and MetaTrader 5, with optional Anthropic integration.
- Do not deploy an automated live-trading process as a public Vercel app. Use a private worker/container host only after security and operational review.
- Keep broker/API credentials in secret storage, enforce demo/paper mode by default, and require explicit risk limits and human confirmation before live orders.
- Run the existing pytest suite and add deterministic tests for indicators, order sizing, reconnects, and market-data failures.

## Verification
`pyproject.toml` inspected; tests and broker connectivity were not run.