import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SteadyIncomePage from '../SteadyIncomePage';

const { getPortfolio } = vi.hoisted(() => ({ getPortfolio: vi.fn() }));

vi.mock('../../api/steadyIncome', () => ({
  steadyIncomeApi: { getPortfolio },
}));

vi.mock('../../hooks/useStockIndex', () => ({
  useStockIndex: () => ({
    index: [{ displayCode: '600001', nameZh: '稳健样本' }],
    loading: false,
    error: null,
    fallback: false,
    loaded: true,
  }),
}));

const candidate = {
  schemaVersion: 5,
  modelVersion: 'steady-income-risk-v5',
  rulesetVersion: '4.0.0',
  evaluatorVersion: '5.0.0',
  sectorModelVersion: '1.0.0',
  evidenceVersion: '4.0.0',
  priceModelVersion: '2.0.0',
  code: '600001',
  sectorModel: 'normal_corporate' as const,
  riskTier: '稳健' as const,
  publicRiskLabel: '规则低风险 A' as const,
  qualified: true,
  rankingScore: 92,
  currentPrice: 50,
  priceDate: '2026-08-26',
  ttmDividendYieldPct: 4.2,
  ttmCashDividendPerShare: 2.1,
  consecutiveDividendYears: 5,
  dividendSustainability: '较强',
  cashFlowCoverageRatio: 1.3,
  roePct: 13,
  peRatio: 12,
  pbRatio: 1.5,
  maxDrawdownPct: 18.4,
  annualizedVolatilityPct: 21.2,
  positiveReplayPeriods: 4,
  replayPeriods: [
    { label: '2025', startDate: '2025-01-02', endDate: '2025-12-31', adjustedPriceReturnPct: 8.5 },
  ],
  historyCoverage: [],
  priceAdjustment: 'qfq-provider-current',
  strengths: ['TTM 税前股息率 4.20%', '可验证连续分红 5 年'],
  risks: [],
  dataStatus: '完整',
  failureCode: 'none',
  terminalStatus: 'evaluated_qualified' as const,
  evidenceIssues: [],
  evidenceStatus: { price: 'complete', dividend: 'complete', financial: 'complete', sector: 'complete', history: 'complete' },
  providerDiagnostics: [],
  evidence: {},
  dataNotes: [],
};

const payload = {
  schemaVersion: 5,
  modelVersion: 'steady-income-risk-v5',
  rulesetVersion: '4.0.0',
  evaluatorVersion: '5.0.0',
  sectorModelVersion: '1.0.0',
  evidenceVersion: '4.0.0',
  priceModelVersion: '2.0.0',
  generatedAt: '2026-08-26T10:00:00Z',
  asOf: '2026-08-26',
  source: 'current_portfolio',
  dataStatus: 'complete' as const,
  selectionMode: 'portfolio' as const,
  universeCount: 2,
  prefilterCount: 2,
  deepBudget: 2,
  deepEvaluatedCount: 2,
  unevaluatedCount: 0,
  isExhaustive: true,
  evaluatedCount: 2,
  qualifiedCount: 1,
  candidates: [candidate],
  excluded: [{ ...candidate, code: '600002', riskTier: '不纳入' as const, publicRiskLabel: '规则排除' as const, qualified: false, rankingScore: null, risks: ['经营现金流非正'] }],
  warnings: [],
  methodology: {
    priority: '风险硬门槛优先，评分仅在同一风险层内排序',
    dividend: 'TTM 税前现金分红/当前价格',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  getPortfolio.mockResolvedValue(payload);
});

describe('SteadyIncomePage', () => {
  it('shows concise risk-first income evidence and historical replay', async () => {
    render(<MemoryRouter><SteadyIncomePage /></MemoryRouter>);

    expect(await screen.findByRole('heading', { name: '持仓稳健性' })).toBeInTheDocument();
    expect(screen.getByText(/只检查当前 A 股持仓/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '稳健样本' })).toBeInTheDocument();
    expect(screen.getAllByText('4.20%')[0]).toBeInTheDocument();
    expect(screen.getAllByText('5 年')[0]).toBeInTheDocument();
    expect(screen.getAllByText('18.40%')[0]).toBeInTheDocument();
    expect(screen.queryByText('高股息观察价')).not.toBeInTheDocument();
    expect(screen.getByText('+8.50%')).toBeInTheDocument();
    expect(screen.getByText(/高股息不能抵消负现金流/)).toBeInTheDocument();
    expect(screen.queryByText(/持仓数量：/)).not.toBeInTheDocument();
  });

  it('keeps rejected candidates collapsed and refreshes without adding analysis calls', async () => {
    render(<MemoryRouter><SteadyIncomePage /></MemoryRouter>);

    await screen.findByRole('heading', { name: '持仓稳健性' });
    expect(screen.queryByText('经营现金流非正')).not.toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    await waitFor(() => expect(getPortfolio).toHaveBeenLastCalledWith({ refresh: true }));
    expect(getPortfolio).toHaveBeenCalledTimes(2);
  });

  it('does not weaken the rules when no stock qualifies', async () => {
    getPortfolio.mockResolvedValueOnce({
      ...payload,
      qualifiedCount: 0,
      candidates: [],
      excluded: [payload.excluded[0]],
    });

    render(<MemoryRouter><SteadyIncomePage /></MemoryRouter>);

    expect(await screen.findByText('当前没有股票通过低风险门槛')).toBeInTheDocument();
    expect(screen.getByText(/不会为了凑数放宽/)).toBeInTheDocument();
  });

  it('does not render a provider outage as a valid zero', async () => {
    getPortfolio.mockResolvedValueOnce({
      ...payload,
      dataStatus: 'provider_unavailable',
      qualifiedCount: 0,
      candidates: [],
    });
    render(<MemoryRouter><SteadyIncomePage /></MemoryRouter>);
    expect(await screen.findByText('数据源异常，不能判断是否存在合格标的')).toBeInTheDocument();
    expect(screen.getByText(/这不是合法的零候选结论/)).toBeInTheDocument();
  });
});
