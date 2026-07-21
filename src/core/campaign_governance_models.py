"""
P9.71 — Campaign governance persistent records (messaging ops, not marketing Campaign).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, JSON

from .database import Base


class CampaignGovernance(Base):
    """Governed messaging campaign spec and lifecycle state."""

    __tablename__ = "campaign_governance"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    scope_tier = Column(String(32), nullable=False)  # tiny_5 | small_10 | medium_20
    state = Column(String(32), nullable=False, default="draft", index=True)

    allowed_account_ids = Column(JSON, nullable=False, default=list)
    allowed_target_ids = Column(JSON, nullable=False, default=list)
    allowed_pair_map = Column(JSON, nullable=False, default=dict)

    approved_template_hash = Column(String(64), nullable=True)
    approved_template_body = Column(Text, nullable=True)

    created_by = Column(String(128), nullable=True)
    approved_by = Column(String(128), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    armed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    aborted_at = Column(DateTime, nullable=True)

    safety_notes = Column(Text, nullable=True)
    emergency_stop_reason = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<CampaignGovernance id={self.id} name={self.name!r} state={self.state}>"
