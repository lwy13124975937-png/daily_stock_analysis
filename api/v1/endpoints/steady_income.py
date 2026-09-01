# -*- coding: utf-8 -*-
"""Rule-based steady-income endpoints."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Query

from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.steady_income import SteadyIncomeResponse
from src.services.steady_income_service import SteadyIncomeService
from src.services.steady_income_contracts import SteadyIncomeDataError


logger = logging.getLogger(__name__)
router = APIRouter()
_service = SteadyIncomeService()


@router.get(
    "/portfolio",
    response_model=SteadyIncomeResponse,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Evaluate current A-share holdings for low-risk steady income",
)
def evaluate_portfolio(
    account_id: Optional[int] = Query(None, ge=1),
    as_of: Optional[date] = Query(None, description="A 股评估业务日期（YYYY-MM-DD）"),
    refresh: bool = Query(False, description="Bypass the six-hour in-process result cache"),
) -> SteadyIncomeResponse:
    try:
        payload = _service.evaluate_portfolio(account_id=account_id, as_of=as_of, refresh=refresh)
        return SteadyIncomeResponse(**payload)
    except ValueError as exc:
        raise api_error(400, "invalid_steady_income_request", str(exc)) from exc
    except SteadyIncomeDataError as exc:
        logger.warning("Steady-income dependency unavailable: %s", exc)
        raise api_error(503, "steady_income_dependency_unavailable", "稳健收益依赖数据当前不可用。") from exc
    except Exception as exc:
        logger.error("Steady-income portfolio evaluation failed: %s", exc, exc_info=True)
        raise api_error(500, "steady_income_failed", "稳健收益评估失败，请稍后重试。") from exc
