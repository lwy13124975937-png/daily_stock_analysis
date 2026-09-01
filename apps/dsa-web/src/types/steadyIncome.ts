export interface SteadyIncomeReplayPeriod {
  label: string;
  startDate: string;
  endDate: string;
  adjustedPriceReturnPct: number;
}

export interface SteadyIncomeHistoryCoverage {
  year: number;
  historyStart?: string | null;
  historyEnd?: string | null;
  actualSessions: number;
  expectedSessions: number;
  coverageRatio: number;
  complete: boolean;
}

export interface SteadyIncomeCandidate {
  schemaVersion: number;
  modelVersion: string;
  rulesetVersion: string;
  evaluatorVersion: string;
  sectorModelVersion: string;
  evidenceVersion: string;
  code: string;
  sectorModel: 'normal_corporate' | 'bank' | 'insurer' | 'broker' | 'unsupported_financial' | 'unknown';
  industry?: string | null;
  riskTier: '稳健' | '较稳健' | '观察' | '不纳入' | '数据不足';
  publicRiskLabel: '规则低风险 A' | '规则低风险 B' | '规则观察' | '规则排除' | '数据不足';
  qualified: boolean;
  rankingScore?: number | null;
  score?: number | null;
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
  historyCoverage: SteadyIncomeHistoryCoverage[];
  priceAdjustment: string;
  strengths: string[];
  risks: string[];
  dataStatus: string;
  failureCode: string;
  terminalStatus: 'evaluated_qualified' | 'evaluated_rejected' | 'insufficient_evidence' | 'unsupported_sector_model' | 'provider_failure' | 'internal_error';
  evidenceIssues: string[];
  evidenceStatus: Record<string, string>;
  providerDiagnostics: Array<Record<string, unknown>>;
  evidence: Record<string, unknown>;
  dataNotes: string[];
}

export interface SteadyIncomeResponse {
  schemaVersion: number;
  modelVersion: string;
  rulesetVersion: string;
  evaluatorVersion: string;
  sectorModelVersion: string;
  evidenceVersion: string;
  priceModelVersion: string;
  generatedAt: string;
  asOf: string;
  source: string;
  dataStatus: 'complete' | 'degraded' | 'valid_zero' | 'partial' | 'provider_unavailable' | 'source_schema_changed';
  selectionMode: 'portfolio' | 'fixed_shortlist' | 'adaptive_shortlist' | 'exhaustive';
  universeCount: number;
  prefilterCount: number;
  deepBudget: number;
  deepEvaluatedCount: number;
  unevaluatedCount: number;
  isExhaustive: boolean;
  evaluatedCount: number;
  qualifiedCount: number;
  candidates: SteadyIncomeCandidate[];
  excluded: SteadyIncomeCandidate[];
  warnings: string[];
  methodology: Record<string, string>;
  screeningStats?: Record<string, unknown>;
}
