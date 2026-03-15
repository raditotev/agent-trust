#!/usr/bin/env python
"""Seed the database with test agents for development.

Usage:
    uv run python scripts/seed_test_agents.py

Requires DATABASE_URL environment variable to be set.
Creates a few test agents with different trust profiles.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def seed() -> None:
    """Create test agents in the database."""
    from agent_trust.db.session import get_session
    from agent_trust.models.agent import Agent

    test_agents = [
        {
            "agent_id": uuid.uuid4(),
            "name": "Alice",
            "description": "High-trust test agent",
            "auth_source": "standalone",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
        {
            "agent_id": uuid.uuid4(),
            "name": "Bob",
            "description": "Medium-trust test agent",
            "auth_source": "standalone",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
        {
            "agent_id": uuid.uuid4(),
            "name": "Eve",
            "description": "Low-trust test agent (for adversarial testing)",
            "auth_source": "standalone",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    ]

    async with get_session() as session:
        for agent_data in test_agents:
            agent = Agent(**agent_data)
            session.add(agent)
        print(f"✅ Created {len(test_agents)} test agents")
        print("   Alice, Bob, Eve seeded successfully")


if __name__ == "__main__":
    asyncio.run(seed())
