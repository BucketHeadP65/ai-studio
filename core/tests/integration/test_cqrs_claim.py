"""The Postgres claim takes exactly the jobs the documented claim rule accepts, in the documented order."""

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from sqlalchemy import insert, select, text

from core.cqrs.contracts import Job, PostgresJobs, create_coordination_schema
from core.cqrs.domain import is_claimable
from core.cqrs.infrastructure import schema
from core.db import async_dsn, build_engine
from tests.integration.conftest import DATABASE_URL

THROWAWAY = "core_coordination_claim_test"
KIND = "claim-case"

CASES = (
    ("queued, due", "queued", "now() - interval '2 hours'", None, 300),
    ("queued, not due", "queued", "now() + interval '1 hour'", None, 300),
    ("failed, due", "failed", "now() - interval '3 hours'", None, 300),
    ("failed, not due", "failed", "now() + interval '2 hours'", None, 300),
    ("running, lease expired", "running", "now() - interval '1 hour'", "now() - interval '1 hour'", 60),
    ("running, lease live", "running", "now() - interval '4 hours'", "now()", 3600),
    ("done", "done", "now() - interval '5 hours'", None, 300),
    ("abandoned", "abandoned", "now() - interval '6 hours'", None, 300),
)
"""One row per case: its label, state, `available_at`, `locked_at` and `lease_seconds`, as SQL over the database's clock."""


def _named(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


@pytest_asyncio.fixture
async def throwaway_url() -> AsyncIterator[str]:
    """A database of its own for the test, dropped afterwards, or a skip when there is no Postgres."""
    try:
        admin = await asyncpg.connect(DATABASE_URL)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at {DATABASE_URL}: {type(exc).__name__}")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {THROWAWAY}")
        await admin.execute(f"CREATE DATABASE {THROWAWAY}")
        yield _named(DATABASE_URL, THROWAWAY)
        await admin.execute(f"DROP DATABASE IF EXISTS {THROWAWAY} WITH (FORCE)")
    finally:
        await admin.close()


async def test_the_postgres_claim_takes_exactly_the_claimable_jobs_in_order(throwaway_url: str) -> None:
    engine = build_engine(async_dsn(throwaway_url))
    try:
        await create_coordination_schema(engine)
        async with engine.begin() as connection:
            for label, state, available_at, locked_at, lease_seconds in CASES:
                await connection.execute(
                    insert(schema.jobs).values(
                        kind=KIND,
                        key=label,
                        state=state,
                        available_at=text(available_at),
                        locked_at=None if locked_at is None else text(locked_at),
                        locked_by=None if locked_at is None else "another-runner",
                        lease_seconds=lease_seconds,
                    )
                )
        async with engine.connect() as connection:
            now = (await connection.execute(text("SELECT now()"))).scalar_one()
            stored = [Job.model_validate(dict(row)) for row in (await connection.execute(select(schema.jobs))).mappings()]

        expected = [job.key for job in sorted((job for job in stored if is_claimable(job, now)), key=lambda job: (job.available_at, job.id))]
        jobs = PostgresJobs(engine)
        claimed = []
        while (job := await jobs.claim([KIND], worker="this-runner")) is not None:
            claimed.append(job.key)
    finally:
        await engine.dispose()

    assert expected == ["failed, due", "queued, due", "running, lease expired"]
    assert claimed == expected


async def test_a_runner_whose_lease_expired_cannot_finish_the_job_another_runner_holds(throwaway_url: str) -> None:
    engine = build_engine(async_dsn(throwaway_url))
    try:
        await create_coordination_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                insert(schema.jobs).values(
                    kind=KIND, key="held", state="running", available_at=text("now()"), locked_at=text("now()"), locked_by="new-holder", lease_seconds=300
                )
            )
        jobs = PostgresJobs(engine)
        async with engine.connect() as connection:
            job_id = (await connection.execute(select(schema.jobs.c.id))).scalar_one()

        await jobs.complete(job_id, result="late", claimed_by="stale-runner")
        await jobs.fail(job_id, error="late", retry_at=None, claimed_by="stale-runner")
        untouched = await jobs.read(job_id)
        await jobs.complete(job_id, result="done", claimed_by="new-holder")
        finished = await jobs.read(job_id)
    finally:
        await engine.dispose()

    assert untouched is not None
    assert (untouched.state, untouched.locked_by, untouched.result) == ("running", "new-holder", None)
    assert finished is not None
    assert finished.state == "done"
