# -*- coding: utf-8 -*-
"""Rule-based steady-income endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query

from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.steady_income import SteadyIncomeResponse
from src.services.steady_income_service import SteadyIncomeService


logger = logging.getLogger(__name__)
router = APIRouter()
_service = SteadyIncomeService()


@router.get(
    "/portfolio",
    response_model=SteadyIncomeResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Evaluate current A-share holdings for low-risk steady income",
)
def evaluate_portfolio(
    account_id: Optional[int] = Query(None, ge=1),
    refresh: bool = Query(False, description="Bypass the six-hour in-process result cache"),
) -> SteadyIncomeResponse:
    try:
        payload = _service.evaluate_portfolio(account_id=account_id, refresh=refresh)
        return SteadyIncomeResponse(**payload)
    except Exception as exc:
        logger.error("Steady-income portfolio evaluation failed: %s", exc, exc_info=True)
        raise api_error(500, "steady_income_failed", "稳健收益评估失败，请稍后重试。") from exc
