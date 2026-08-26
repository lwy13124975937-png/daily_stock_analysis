import apiClient from './index';
import { toCamelCase } from './utils';
import type { SteadyIncomeResponse } from '../types/steadyIncome';

type SteadyIncomeQuery = {
  accountId?: number;
  refresh?: boolean;
};

export const steadyIncomeApi = {
  async getPortfolio(query: SteadyIncomeQuery = {}): Promise<SteadyIncomeResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/steady-income/portfolio', {
      params: {
        account_id: query.accountId,
        refresh: query.refresh ?? false,
      },
    });
    return toCamelCase<SteadyIncomeResponse>(response.data);
  },
};
