# Steady Income Module

## Purpose

The Steady Income module evaluates current A-share holdings for lower risk, sustainable cash dividends, and long-term total return. It is rule-based, makes no LLM calls, and does not predict short-term price direction.

Hard risk gates come first. Dividend yield and stability scores only rank stocks inside the same risk tier. A high yield cannot offset losses, negative operating cash flow, excessive drawdown, excessive volatility, or broken dividend continuity.

## Inputs and outputs

- Scope: Shanghai, Shenzhen, and Beijing listed A-share equities in the current portfolio. HK, US, B-share, index, bond, ETF, LOF, and other fund positions are excluded.
- Income: trailing-12-month pre-tax cash dividend per share and dividend yield.
- Sustainability: consecutive dividend years, parent net profit, operating cash flow, and cash-flow coverage.
- Risk: annualized volatility and maximum drawdown from roughly seven years of daily data.
- Replay: the latest five returns between complete calendar year-ends, using the previous year-end as each baseline so cross-year price moves are not omitted. Forward-adjusted prices already reflect dividends and corporate actions.
- Context: PE, PB, and ROE are supporting evidence only.

The API does not return position quantity, cost, market value, or P&L.

Dividend continuity counts only positive cash-dividend events on or before the evaluation date. TTM yield is recalculated from TTM cash dividend per share and the displayed portfolio quote so the price and yield use the same reference point.

## Risk tiers

`稳健` and `较稳健` are qualified low-risk candidates. `观察` is not qualified. `不纳入` means a hard failure was triggered. `数据不足` means the evidence is incomplete.

The displayed score is only a within-tier rules score. It is not an expected return and cannot promote a stock across a risk tier.

## Access and rollback

- Web: `稳健收益` in the sidebar, route `/steady-income`.
- API: `GET /api/v1/steady-income/portfolio`.
- GitHub Pages: `稳健收益` in the homepage report center, page `steady_income.html`.
- Set `refresh=true` to bypass the six-hour in-process cache.

The daily workflow runs `scripts/build_steady_income_report.py` after the sanitized holdings snapshot is available. The script reads only public-safe stock identity fields, reuses the same hard risk gates, writes `site_data/steady_income.json`, and makes no LLM calls. The Pages presentation check blocks deployment when the dataset does not cover every current A-share holding.

Reverting the service, API route, Web page and navigation, tests, and these docs removes the module without affecting the existing analysis, portfolio, backtest, alert, or LLM pipelines.

The module evaluates every current A-share equity instead of silently truncating large portfolios. Standardized debt structure, future payout policy, and special dividends are not available as hard gates; missing evidence is not inferred to be safe.
