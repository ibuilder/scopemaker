"""Repeatable performance measurement for the pages that carry real load.

Builds a throwaway dataset, drives the application through its WSGI stack in
process, and reports latency percentiles alongside the *query count* for each
scenario. Query count is the number that matters: latency on a laptop with
SQLite says little about production, but "the editor issues 73 queries" is a
defect wherever it runs.

    python scripts/load_test.py                       # default: 12 scopes
    python scripts/load_test.py --scopes 40 --runs 20
    DATABASE_URL=postgresql://... python scripts/load_test.py --concurrency 8

Concurrency above 1 is only meaningful against PostgreSQL; SQLite serialises
writers and will report lock contention rather than application cost.

The dataset is created in its own organization and deleted afterwards unless
--keep is passed.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event

from scopemaker import create_app
from scopemaker.data.masterformat import DIVISIONS
from scopemaker.extensions import db
from scopemaker.models import ApiToken, BidPackage, Membership, Project, Scope
from scopemaker.services import library as library_service
from scopemaker.services.accounts import create_organization, create_user
from scopemaker.services.scope_builder import ScopeDraft, build_scope
from scopemaker.services.seeding import seed_library

PASSWORD = "load-test-not-a-real-password"


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    durations: list[float] = field(default_factory=list)
    queries: list[int] = field(default_factory=list)
    statuses: set[int] = field(default_factory=set)
    cache: dict[str, int] = field(default_factory=dict)

    def record(self, ms: float, queries: int, status: int, cache: str | None) -> None:
        self.durations.append(ms)
        self.queries.append(queries)
        self.statuses.add(status)
        if cache:
            self.cache[cache] = self.cache.get(cache, 0) + 1

    def percentile(self, fraction: float) -> float:
        ordered = sorted(self.durations)
        index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
        return ordered[index]

    def row(self) -> str:
        statuses = ",".join(str(s) for s in sorted(self.statuses))
        cache = " ".join(f"{k}={v}" for k, v in sorted(self.cache.items()))
        return (
            f"  {self.name:<22} {statuses:>7}  "
            f"p50 {self.percentile(0.50):6.0f} ms  "
            f"p95 {self.percentile(0.95):6.0f} ms  "
            f"max {max(self.durations):6.0f} ms  "
            f"{statistics.median(self.queries):5.0f} queries  {cache}"
        )


#: Statements executed, per thread. A single listener feeds this for the whole
#: run -- see install_query_counter.
_query_counts: dict[int, int] = defaultdict(int)


def install_query_counter(engine: Any) -> None:
    """Attach one listener for the lifetime of the run.

    Registering and removing a listener around each request looks tidier and is
    a genuine bug under --concurrency: SQLAlchemy keeps its listeners in a
    deque, and mutating that from one thread while another is iterating it to
    dispatch raises "deque mutated during iteration" from somewhere deep in the
    engine. That surfaced as intermittent 500s that looked like an application
    fault and were entirely the measuring apparatus.
    """

    def count(*args: Any, **kwargs: Any) -> None:
        _query_counts[threading.get_ident()] += 1

    event.listen(engine, "before_cursor_execute", count)


class QueryCounter:
    """Statements issued by this thread while the block runs."""

    def __enter__(self) -> QueryCounter:
        self._thread = threading.get_ident()
        self._start = _query_counts[self._thread]
        self.total = 0
        return self

    def __exit__(self, *exc: object) -> None:
        self.total = _query_counts[self._thread] - self._start


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def build_dataset(count: int) -> tuple[str, str, str, list[str]]:
    """Create an organization with ``count`` generated scopes."""
    seed_library()

    organization = create_organization("Load Test Contractors")
    user = create_user(
        email=f"load-{organization.slug}@example.com",
        full_name="Load Tester",
        password=PASSWORD,
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=user.id, role="admin")
    )
    db.session.commit()

    # The JSON API authenticates with bearer tokens, not the session cookie.
    token_record, api_token = ApiToken.issue(
        user=user,
        organization_id=organization.id,
        name="load test",
        scopes="read",
    )
    db.session.add(token_record)
    db.session.commit()

    project = Project(
        organization_id=organization.id,
        name="Load Test Tower",
        number="LT-001",
        address="1 Benchmark Way",
        city="Columbus",
        state="OH",
        owner_name="Benchmark Holdings",
        architect_name="Benchmark Architects",
        contractor_name=organization.name,
        delivery_method="CMAR",
    )
    db.session.add(project)
    db.session.commit()

    codes = [d.code for d in DIVISIONS]
    scope_ids: list[str] = []
    for index in range(count):
        code = codes[index % len(codes)]
        package = BidPackage(
            project_id=project.id,
            organization_id=organization.id,
            number=f"BP-{index + 1:03d}",
            name=f"Package {index + 1}",
            division_code=code,
        )
        db.session.add(package)
        db.session.commit()

        scope = build_scope(
            ScopeDraft(
                organization_id=organization.id,
                division_code=code,
                project_id=project.id,
                bid_package_id=package.id,
                clause_ids=library_service.default_clause_ids(organization.id, code),
                spec_section_ids=library_service.default_spec_section_ids(
                    organization.id, code
                ),
                created_by_id=user.id,
                base_bid_amount=1_000_000 + index * 25_000,
            )
        )
        scope_ids.append(scope.id)

    return organization.id, user.email, api_token, scope_ids


def teardown(organization_id: str) -> None:
    from scopemaker.models import Organization

    organization = db.session.get(Organization, organization_id)
    if organization is not None:
        db.session.delete(organization)
        db.session.commit()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenarios(scope_ids: list[str], project_id: str,
              api_token: str) -> list[tuple[str, str, dict[str, str]]]:
    first = scope_ids[0]
    bearer = {"Authorization": f"Bearer {api_token}"}
    return [
        ("dashboard", "/dashboard", {}),
        ("scopes list", "/scopes/", {}),
        ("editor", f"/scopes/{first}", {}),
        ("preview", f"/scopes/{first}/preview", {}),
        ("coverage", f"/projects/{project_id}/coverage", {}),
        ("export md", f"/exports/{first}.md", {}),
        ("export docx", f"/exports/{first}.docx", {}),
        ("api scopes", "/api/v1/scopes", bearer),
    ]


def sign_in(app, email: str):
    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD},
        follow_redirects=True,
    )
    if response.status_code != 200:
        raise SystemExit(f"Could not sign in the load-test user ({response.status_code}).")
    return client


def run_scenario(app, email: str, name: str, path: str,
                 headers: dict[str, str], runs: int, concurrency: int) -> Result:
    result = Result(name)
    local = threading.local()

    def client_for() -> object:
        """One signed-in client per worker thread.

        Signing in per request would measure the login path instead of the page
        under test, and a real user signs in once and then browses.
        """
        if concurrency == 1:
            return shared
        if not hasattr(local, "client"):
            local.client = sign_in(app, email)
        return local.client

    def one(_index: int) -> tuple[float, int, int, str | None]:
        client = client_for()
        # No application context is held around the request, deliberately.
        # Flask reuses an already-pushed context rather than making a new one,
        # which would share `g` -- and therefore Flask-Login's cached user --
        # across every request in the run. Letting each request push and pop
        # its own is what production does, and it also hands each one a fresh
        # session, so the query counts are the ones a real process would issue.
        with QueryCounter() as counter:
            started = time.perf_counter()
            response = client.get(path, headers=headers)
            response.get_data()
            elapsed = (time.perf_counter() - started) * 1000
        return elapsed, counter.total, response.status_code, response.headers.get(
            "X-Render-Cache"
        )

    shared = sign_in(app, email)

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for elapsed, queries, status, cache in pool.map(one, range(runs)):
                result.record(elapsed, queries, status, cache)
    else:
        for index in range(runs):
            result.record(*one(index))

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scopes", type=int, default=12,
                        help="How many scopes to generate (default 12).")
    parser.add_argument("--runs", type=int, default=10,
                        help="Requests per scenario (default 10).")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Parallel requests; needs PostgreSQL above 1.")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the generated data in the database.")
    args = parser.parse_args()

    # These have to be overrides, not post-hoc config writes: Flask-Limiter
    # latches `enabled` when init_app runs, so assigning the config afterwards
    # leaves the limiter on and the run dies at 429 partway through.
    app = create_app(
        "development", RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False
    )

    with app.app_context():
        db.create_all()
        print(f"Building {args.scopes} scopes...", flush=True)
        started = time.perf_counter()
        organization_id, email, api_token, scope_ids = build_dataset(args.scopes)
        project_id = db.session.get(Scope, scope_ids[0]).project_id
        items = sum(
            db.session.get(Scope, sid).item_count for sid in scope_ids
        )
        print(
            f"  {len(scope_ids)} scopes / {items} items in "
            f"{time.perf_counter() - started:.1f}s\n"
        )

        install_query_counter(db.engine)
        engine = db.engine
        print(f"Database: {engine.url.drivername}   "
              f"concurrency: {args.concurrency}   runs: {args.runs}\n")

    # Measured outside any application context -- see the note in one().
    failures: list[str] = []
    try:
        for name, path, headers in scenarios(scope_ids, project_id, api_token):
            result = run_scenario(
                app, email, name, path, headers,
                args.runs, args.concurrency,
            )
            if result.statuses - {200}:
                print(f"  ! {name} returned {sorted(result.statuses)}")
                failures.append(f"{name}: {sorted(result.statuses)}")
            print(result.row(), flush=True)
    finally:
        with app.app_context():
            if args.keep:
                print(f"\nKept organization {organization_id}.")
            else:
                teardown(organization_id)

    if failures:
        # Exiting non-zero matters: a 500 under load that only prints a warning
        # is a 500 nobody finds.
        print("\nFAILED -- these scenarios did not return 200:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
