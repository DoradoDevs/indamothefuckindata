"""Structured-output contracts for the two Claude passes."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QuotedNumber(BaseModel):
    value: float
    metric_id: str


class Finding(BaseModel):
    finding_id: str = Field(description="Short unique id, e.g. F1")
    title: str
    type: Literal["pattern", "anomaly", "recommendation", "data_quality"]
    claim: str
    evidence: list[str] = Field(description="Metric IDs relied on")
    quoted_numbers: list[QuotedNumber]
    confidence: Literal["high", "medium", "low"]
    caveats: list[str] = []
    recommendation: str | None = None


class AnalystOutput(BaseModel):
    summary: str = Field(description="3-5 sentence executive summary")
    findings: list[Finding]


class Review(BaseModel):
    finding_id: str
    verdict: Literal["confirmed", "revise", "reject"]
    reason: str
    revised_claim: str | None = None


class ReviewerOutput(BaseModel):
    reviews: list[Review]
