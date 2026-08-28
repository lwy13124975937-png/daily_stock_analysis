# Steady Income Module

## Purpose

The Steady Income module screens the complete active Shanghai/Shenzhen A-share universe for lower risk, sustainable cash dividends, and long-term total return. It is independent from current holdings, rule-based, makes no LLM calls, and does not predict short-term price direction.

Hard risk gates come first. Dividend yield and stability scores only rank stocks inside the same risk tier. A high yield cannot offset losses, negative operating cash flow, excessive drawdown, excessive volatility, or broken dividend continuity.

## Inputs and outputs

- Scope: all active Shanghai/Shenzhen A-share equities in the public stock index. Beijing, HK, US, B-share, index, bond, ETF, LOF, and other fund instruments are excluded.
- Two-stage screen: every stock receives a bulk dividend, profitability, listing-age, and ST-risk pre-screen; only the strongest finite seed set receives the slower long-history, cash-flow, and dividend-detail review.
- Income: trailing-12-month pre-tax cash dividend per share and dividend yield.
- Sustainability: consecutive dividend years, parent net profit, operating cash flow, and cash-flow coverage.
- Risk: annualized volatility and maximum drawdown from roughly seven years of daily data.
- Replay: the latest five returns between complete calendar year-ends, using the previous year-end as each baseline so cross-year price moves are not omitted. Forward-adjusted prices already reflect dividends and corporate actions.
- Context: PE, PB, and ROE are supporting evidence only.

The Pages module does not use holdings as its candidate universe and never returns account, position quantity, cost, market value, or P&L.

Dividend continuity counts only positive cash-dividend events on or before the evaluation date. TTM yield is recalculated from TTM cash dividend per share and the displayed portfolio quote so the price and yield use the same reference point.

## Risk tiers

`稳健` and `较稳健` are qualified low-risk candidates. `观察` is not qualified. `不纳入` means a hard failure was triggered. `数据不足` means the evidence is incomplete.

The displayed score is only a within-tier rules score. It is not an expected return and cannot promote a stock across a risk tier.

## Access and rollback

- GitHub Pages: `稳健收益` in the homepage report center, page `steady_income.html`.
- Public dataset: `site/data/steady_income.json`, including whole-universe, pre-screen, deep-review, and qualified counts.

The authenticated Web app keeps its existing current-portfolio evaluator under the explicit label `持仓稳健性` (Portfolio Stability), so it is no longer confused with the market-wide Pages screen.

The daily workflow runs `scripts/build_steady_income_report.py` against the public Shanghai/Shenzhen stock index and bulk dividend tables. It applies the market-wide pre-screen, reuses the same hard risk gates for deep evaluation, writes `site_data/steady_income.json`, and makes no LLM calls. The Pages guard blocks deployment when the universe is implausibly small, funnel counts disagree, or the page regresses to a current-holdings scope.

Reverting the Pages builder, market dataset script, HTML guard, workflow step, tests, and these docs removes the public market screen without affecting the existing analysis, portfolio, backtest, alert, or LLM pipelines.

Every Shanghai/Shenzhen equity enters the basic pre-screen. The deep-review cap is disclosed together with universe and pre-screen counts, so a finite deep review is never presented as an exhaustive per-stock review. Standardized debt structure, future payout policy, and special dividends are not available as hard gates; missing evidence is not inferred to be safe.
