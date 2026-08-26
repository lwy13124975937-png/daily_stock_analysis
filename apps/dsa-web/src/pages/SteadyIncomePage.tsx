import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, CircleDollarSign, RefreshCw, ShieldCheck, TrendingDown, WalletCards } from 'lucide-react';
import { Link } from 'react-router-dom';
import { steadyIncomeApi } from '../api/steadyIncome';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Badge, Button, Card, EmptyState, InlineAlert, PageHeader } from '../components/common';
import { useStockIndex } from '../hooks/useStockIndex';
import type { SteadyIncomeCandidate } from '../types/steadyIncome';
import type { SteadyIncomeResponse } from '../types/steadyIncome';

const formatNumber = (value: number | null | undefined, suffix = ''): string => (
  value == null ? '数据不足' : `${value.toFixed(2)}${suffix}`
);

const riskVariant = (tier: SteadyIncomeCandidate['riskTier']): 'success' | 'info' | 'warning' | 'danger' | 'default' => {
  if (tier === '稳健') return 'success';
  if (tier === '较稳健') return 'info';
  if (tier === '观察') return 'warning';
  if (tier === '不纳入') return 'danger';
  return 'default';
};

interface MetricProps {
  label: string;
  value: string;
  hint?: string;
}

const Metric = ({ label, value, hint }: MetricProps) => (
  <div className="min-w-0 rounded-lg border border-border/55 bg-elevated/45 px-3 py-3">
    <p className="text-xs text-secondary-text">{label}</p>
    <p className="mt-1 break-words text-base font-semibold tabular-nums text-foreground">{value}</p>
    {hint ? <p className="mt-1 text-xs leading-5 text-secondary-text">{hint}</p> : null}
  </div>
);

interface CandidateCardProps {
  item: SteadyIncomeCandidate;
  name: string;
  compact?: boolean;
}

const CandidateCard = ({ item, name, compact = false }: CandidateCardProps) => (
  <Card className="overflow-hidden" padding="none">
    <div className="border-b border-border/50 px-4 py-4 sm:px-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-foreground">{name === item.code ? `A股 ${item.code}` : name}</h2>
            {name !== item.code ? <span className="font-mono text-sm text-secondary-text">{item.code}</span> : null}
          </div>
          <p className="mt-1 text-xs text-secondary-text">
            行情 {item.priceDate || '日期缺失'} · 数据完整度 {item.dataStatus}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={riskVariant(item.riskTier)} size="md">{item.riskTier}</Badge>
          {!compact ? (
            <span className="rounded-full border border-border/60 px-2.5 py-1 text-xs text-secondary-text">
              同层规则分 {item.score}
            </span>
          ) : null}
        </div>
      </div>
    </div>

    <div className="space-y-4 px-4 py-4 sm:px-5">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="当前价" value={formatNumber(item.currentPrice)} />
        <Metric label="TTM 税前股息率" value={formatNumber(item.ttmDividendYieldPct, '%')} />
        <Metric label="连续现金分红" value={`${item.consecutiveDividendYears} 年`} />
        <Metric label="分红可持续性" value={item.dividendSustainability} />
        <Metric label="近年最大回撤" value={formatNumber(item.maxDrawdownPct, '%')} />
        <Metric label="年化波动" value={formatNumber(item.annualizedVolatilityPct, '%')} />
      </div>

      {!compact ? (
        <>
          <div className="grid gap-3 lg:grid-cols-2">
            <section className="rounded-lg border border-success/15 bg-success/5 p-4">
              <h3 className="text-sm font-semibold text-foreground">成立依据</h3>
              {item.strengths.length ? (
                <ul className="mt-2 space-y-1.5 text-sm leading-6 text-secondary-text">
                  {item.strengths.map((text) => <li key={text}>· {text}</li>)}
                </ul>
              ) : <p className="mt-2 text-sm text-secondary-text">没有足够证据支持稳健结论。</p>}
            </section>
            <section className="rounded-lg border border-warning/15 bg-warning/5 p-4">
              <h3 className="text-sm font-semibold text-foreground">主要风险</h3>
              {item.risks.length ? (
                <ul className="mt-2 space-y-1.5 text-sm leading-6 text-secondary-text">
                  {item.risks.map((text) => <li key={text}>· {text}</li>)}
                </ul>
              ) : <p className="mt-2 text-sm text-secondary-text">当前硬门槛内未发现额外风险项。</p>}
            </section>
          </div>

          {item.priceBands ? (
            <section>
              <div className="mb-2 flex items-center gap-2">
                <CircleDollarSign className="h-4 w-4 text-cyan" />
                <h3 className="text-sm font-semibold text-foreground">股息率价格带</h3>
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Metric label="高股息观察价" value={formatNumber(item.priceBands.highIncomePrice)} hint="对应约 5% TTM 股息率" />
                <Metric label="平衡观察价" value={formatNumber(item.priceBands.balancedPrice)} hint="对应约 3.5% TTM 股息率" />
                <Metric label="低收益上沿" value={formatNumber(item.priceBands.lowIncomePrice)} hint="对应约 2.5% TTM 股息率" />
              </div>
            </section>
          ) : null}

          <section>
            <div className="mb-2 flex items-center gap-2">
              <Activity className="h-4 w-4 text-cyan" />
              <h3 className="text-sm font-semibold text-foreground">五期复权总回报</h3>
              <span className="text-xs text-secondary-text">含分红和拆并股影响</span>
            </div>
            {item.replayPeriods.length ? (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                {item.replayPeriods.map((period) => (
                  <div key={`${item.code}-${period.label}`} className="rounded-lg border border-border/55 px-3 py-2">
                    <p className="text-xs text-secondary-text">{period.label}</p>
                    <p className={`mt-1 font-semibold tabular-nums ${period.totalReturnPct >= 0 ? 'text-success' : 'text-danger'}`}>
                      {period.totalReturnPct > 0 ? '+' : ''}{period.totalReturnPct.toFixed(2)}%
                    </p>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-secondary-text">完整年度行情不足，暂不做历史稳定性判断。</p>}
          </section>
        </>
      ) : (
        <div className="rounded-lg border border-warning/15 bg-warning/5 px-3 py-2 text-sm leading-6 text-secondary-text">
          {item.risks[0] || '未通过当前低风险硬门槛。'}
        </div>
      )}
    </div>
  </Card>
);

const SteadyIncomePage = () => {
  const [data, setData] = useState<SteadyIncomeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const { index } = useStockIndex();

  const stockNames = useMemo(() => new Map(index.map((item) => [item.displayCode, item.nameZh])), [index]);

  const load = useCallback(async (refresh = false) => {
    if (refresh) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      setData(await steadyIncomeApi.getPortfolio({ refresh }));
    } catch (loadError) {
      setError(getParsedApiError(loadError));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    document.title = '稳健收益 - DSA';
    void load();
  }, [load]);

  return (
    <AppPage className="max-w-6xl space-y-5 pb-12 pt-6">
      <PageHeader
        eyebrow="LOW-RISK INCOME"
        title="稳健收益"
        description="先守住回撤和现金流底线，再比较股息、分红持续性与长期总回报。"
        actions={(
          <Button variant="secondary" onClick={() => void load(true)} isLoading={refreshing} loadingText="重新评估中">
            <RefreshCw className="h-4 w-4" />
            重新评估
          </Button>
        )}
      />

      <InlineAlert
        variant="info"
        title="风险层级是硬门槛"
        message="规则分只用于同一风险层内排序。高股息不能抵消负现金流、巨幅回撤或分红中断，也不代表可以直接买入。"
      />

      {error ? <ApiErrorAlert error={error} /> : null}

      {loading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {[0, 1, 2].map((item) => <div key={item} className="h-24 animate-pulse rounded-xl bg-elevated/70" />)}
        </div>
      ) : null}

      {!loading && data ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Card padding="sm">
              <p className="text-xs text-secondary-text">当前 A 股持仓</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-foreground">{data.evaluatedCount}</p>
            </Card>
            <Card padding="sm">
              <p className="text-xs text-secondary-text">通过低风险门槛</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-success">{data.qualifiedCount}</p>
            </Card>
            <Card padding="sm">
              <p className="text-xs text-secondary-text">评估基准日</p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">{data.asOf}</p>
            </Card>
          </div>

          {data.warnings.map((warning) => <InlineAlert key={warning} variant="warning" message={warning} />)}

          {data.candidates.length ? (
            <section className="space-y-3" aria-labelledby="qualified-heading">
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-5 w-5 text-success" />
                <h2 id="qualified-heading" className="text-lg font-semibold text-foreground">低风险候选</h2>
              </div>
              {data.candidates.map((item) => (
                <CandidateCard key={item.code} item={item} name={stockNames.get(item.code) || item.code} />
              ))}
            </section>
          ) : (
            <EmptyState
              icon={<TrendingDown className="h-7 w-7" />}
              title={data.evaluatedCount ? '当前没有股票通过低风险门槛' : '还没有可评估的 A 股持仓'}
              description={data.evaluatedCount ? '这是有效结果，不会为了凑数放宽回撤、现金流或分红持续性条件。' : '先在持仓页录入 A 股交易，再回来做稳健收益评估。'}
              action={data.evaluatedCount ? undefined : <Link className="btn-primary" to="/portfolio">前往持仓</Link>}
            />
          )}

          {data.excluded.length ? (
            <details className="rounded-xl border border-border/55 bg-card/55">
              <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-foreground">
                查看观察与不纳入项（{data.excluded.length}）
              </summary>
              <div className="space-y-3 border-t border-border/50 p-3 sm:p-4">
                {data.excluded.map((item) => (
                  <CandidateCard key={item.code} item={item} name={stockNames.get(item.code) || item.code} compact />
                ))}
              </div>
            </details>
          ) : null}

          <details className="rounded-xl border border-border/55 bg-card/55">
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-foreground">规则与口径</summary>
            <dl className="grid gap-3 border-t border-border/50 p-4 text-sm sm:grid-cols-2">
              {Object.entries(data.methodology).map(([key, value]) => (
                <div key={key} className="min-w-0">
                  <dt className="font-medium text-foreground">{{
                    priority: '排序原则',
                    dividend: '股息口径',
                    replay: '历史回放',
                    price_bands: '价格带口径',
                    limitations: '能力边界',
                  }[key] || key}</dt>
                  <dd className="mt-1 leading-6 text-secondary-text">{value}</dd>
                </div>
              ))}
            </dl>
          </details>

          <p className="flex items-center gap-2 text-xs leading-5 text-secondary-text">
            <WalletCards className="h-4 w-4 shrink-0" />
            本模块只使用公开行情与财务数据，不读取或展示持仓数量、成本、市值和盈亏。
          </p>
        </>
      ) : null}
    </AppPage>
  );
};

export default SteadyIncomePage;
