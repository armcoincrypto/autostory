"""Negotiation intel surface (facts-derived)."""
from __future__ import annotations

from src.ai_agent.negotiation_intel import negotiation_rate_bucket


def test_negotiation_rate_bucket_reads_facts():
    assert negotiation_rate_bucket({"rate_evaluation": "excellent"}) == "excellent"
    assert negotiation_rate_bucket(None) is None
