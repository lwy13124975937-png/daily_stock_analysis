export interface SteadyIncomeReplayPeriod {
  label: string;
  startDate: string;
  endDate: string;
  totalReturnPct: number;
}

export interface SteadyIncomePriceBands {
  highIncomePrice: number;
  balancedPrice: number;
  lowIncomePrice: number;
}

export interface SteadyIncomeCandidate {
  code: string;
  riskTier: '稳健' | '较稳健' | '观察' | '不纳入' | '数据不足';
  qualified: boolean;
  score: number;
  currentPrice?: number | null;
  priceDate?: string | null;
  ttmDividendYieldPct?: number | null;
  ttmCashDividendPerShare?: number | null;
  consecutiveDividendYears: number;
  dividendSustainability: string;
  cashFlowCoverageRatio?: number | null;
  roePct?: number | null;
  peRatio?: number | null;
  pbRatio?: number | null;
  maxDrawdownPct?: number | null;
  annualizedVolatilityPct?: number | null;
  positiveReplayPeriods: number;
  replayPeriods: SteadyIncomeReplayPeriod[];
  priceBands?: SteadyIncomePriceBands | null;
  strengths: string[];
  risks: string[];
  dataStatus: string;
  dataNotes: string[];
}

export interface SteadyIncomeResponse {
  generatedAt: string;
  asOf: string;
  source: string;
  evaluatedCount: number;
  qualifiedCount: number;
  candidates: SteadyIncomeCandidate[];
  excluded: SteadyIncomeCandidate[];
  warnings: string[];
  methodology: Record<string, string>;
}
