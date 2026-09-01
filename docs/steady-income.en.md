# Steady Income Module

## Purpose

Steady Income screens the Shanghai/Shenzhen A-share universe with a risk-first, evidence-timed ruleset. It is independent from current holdings, makes no LLM calls, and does not predict short-term price direction. `Rule low risk A/B` is a screening label, not capital protection, guaranteed income, or a buy instruction.

## Evidence contract

- Cash dividends count only when implementation is explicit, an effective ex-dividend date exists, the date is on or before `as_of`, and cash per share is positive. Announcement dates never substitute for cash-event dates.
- Financial evidence records both `period_end` and `available_at`. Historical mode accepts it only when `available_at <= as_of`.
- Historical mode refuses to run when point-in-time universe, security status, disclosure availability, or dividend evidence is unavailable. It does not reuse today's universe as a no-lookahead backtest.
- Canonical industry data selects the sector model. Company-name substrings are not an industry classifier. Banks, insurers, and brokers fail closed when their dedicated regulatory evidence is unavailable; they never use the normal-corporate operating-cash-flow model.
- Price dates are checked against the latest completed official A-share session, so pre-close runs do not require a close that does not exist yet. Historical coverage reports expected and actual sessions, coverage ratio, boundaries, provider, and adjustment semantics.
- Until the Provider contract proves point-in-time total-return semantics, the UI calls the output a historical adjusted-price replay rather than a no-lookahead total return.

Schema, model, evaluator, sector-model, evidence, and price-model versions are included in payloads and cache keys. Missing trustworthy evidence produces `data insufficient`; values are not fabricated.

## Screening and display

Every universe security receives the cheap screen. A configurable, sector-stratified subset receives deep evidence evaluation. Structured output and Pages disclose selection_mode, deep budget, evaluated and unevaluated counts, and is_exhaustive. In shortlist mode, zero qualified means only that none of the evaluated shortlist passed; it is not a whole-market zero. Each requested security has exactly one terminal status; completed evaluation means qualified plus rule-rejected. A completed valid zero, a partial degraded run, and a provider outage are rendered as different outcomes.

Qualified candidates are shown by default. Excluded and data-insufficient securities are compact and collapsed. Ranking scores exist only inside an evidence-complete comparable set. The default page does not show dividend-implied observation prices because they are not intrinsic values or recommended entry prices.

The Pages dataset never exposes account, quantity, cost, market value, or P&L fields.

## Build and access

- GitHub Pages: `steady_income.html`.
- Public dataset: `site/data/steady_income.json`.
- The authenticated current-portfolio evaluator remains separately named `Portfolio Stability`.

`scripts/build_steady_income_report.py` produces versioned data only. `scripts/build_pages_report.py` is the sole site owner and builds a fresh staging tree, validates manifest hashes, build IDs, links, HTML safety, and cross-dataset dates, then transactionally promotes the validated tree while preserving the previous site on failure. No LLM is called.

When disclosure dates, historical security-state data, or financial regulatory metrics are unavailable from current Providers, the corresponding historical or financial-sector evaluation remains unavailable/insufficient by design.
