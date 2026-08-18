"""Pre-integration DB reset: FK-safe table cleanup preserving seed data."""

import asyncio
import os
import selectors

import psycopg


async def main():
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://insightforge:change_me@127.0.0.1:5433/insightforge",
    )
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    conn = await psycopg.AsyncConnection.connect(dsn)
    cur = conn.cursor()
    # FK-safe order: leaf tables first, parents last
    # Preserves: issuer_domains, source_providers, companies (seed data)
    tables = [
        "backflow_human_review_decisions",
        "backflow_human_review_requests",
        "workflow_events",
        "task_events",
        "synthesis_claims",
        "claim_evidence_links",
        "synthesis_runs",
        "synthesis_results",
        "workflow_runs",
        "research_orchestration_runs",
        "claims",
        "evidence_cards",
        "extracted_financial_observations",
        "financial_calculations",
        "financial_comparisons",
        "macro_packs",
        "valuation_packs",
        "research_plans",
        "research_orchestrations",
        "research_tasks",
        "research_task_snapshots",
        "parsed_source_blocks",
        "source_records",
        "raw_artifacts",
        "documents",
        "document_chunks",
    ]
    for t in tables:
        await cur.execute(f"DELETE FROM {t}")
    await conn.commit()
    await conn.close()
    print("DB reset OK")


asyncio.run(
    main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
)
