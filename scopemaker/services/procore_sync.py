"""Map Procore records onto ScopeMaker projects and bid packages.

Sync is upsert-by-Procore-id and never destructive: a record that disappears
from Procore is left alone locally, because a scope may already reference it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ..data.masterformat import is_specifiable, normalize_code
from ..extensions import db
from ..models import BidPackage, Project
from ..models.base import utcnow
from .procore_client import ProcoreClient

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    projects_created: int = 0
    projects_updated: int = 0
    packages_created: int = 0
    packages_updated: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.projects_created} project(s) created, "
            f"{self.projects_updated} updated; "
            f"{self.packages_created} bid package(s) created, "
            f"{self.packages_updated} updated"
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nested_name(payload: dict[str, Any], *keys: str) -> str | None:
    """Procore returns some parties as an object and some as a bare string."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            name = value.get("name") or value.get("company_name")
            if name:
                return str(name).strip()
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return None


def sync_projects(
    client: ProcoreClient, organization_id: str, company_id: str
) -> SyncResult:
    """Import or refresh every project the connection can see."""
    result = SyncResult()
    remote_projects = client.projects(company_id)

    existing = {
        project.procore_project_id: project
        for project in db.session.scalars(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.procore_project_id.is_not(None),
            )
        )
    }

    for payload in remote_projects:
        remote_id = _text(payload.get("id"))
        if not remote_id:
            continue

        fields = {
            "name": _text(payload.get("name")) or f"Procore project {remote_id}",
            "number": _text(payload.get("project_number")) or _text(payload.get("number")),
            "address": _text(payload.get("address")),
            "city": _text(payload.get("city")),
            "state": _text(payload.get("state_code")) or _text(payload.get("state")),
            "postal_code": _text(payload.get("zip")),
            "owner_name": _nested_name(payload, "owner", "owner_company"),
            "architect_name": _nested_name(payload, "architect"),
            "procore_company_id": str(company_id),
        }

        project = existing.get(remote_id)
        if project is None:
            project = Project(
                organization_id=organization_id,
                procore_project_id=remote_id,
                **fields,
            )
            db.session.add(project)
            result.projects_created += 1
        else:
            changed = False
            for key, value in fields.items():
                # Never overwrite a populated local value with an empty remote
                # one -- someone may have filled in what Procore does not hold.
                if value and getattr(project, key) != value:
                    setattr(project, key, value)
                    changed = True
            if changed:
                result.projects_updated += 1

    db.session.commit()
    logger.info("Procore project sync: %s", result.summary())
    return result


def sync_bid_packages(
    client: ProcoreClient, project: Project
) -> SyncResult:
    """Import or refresh the bid packages for one project."""
    result = SyncResult()
    if not project.procore_project_id:
        result.warnings.append(f"{project.name} is not linked to a Procore project.")
        return result

    try:
        remote_packages = client.bid_packages(project.procore_project_id)
    except Exception as exc:
        logger.warning("Bid package sync failed for %s: %s", project.name, exc)
        result.warnings.append(f"Could not load bid packages: {exc}")
        return result

    existing = {
        package.procore_bid_package_id: package
        for package in project.bid_packages
        if package.procore_bid_package_id
    }

    for payload in remote_packages:
        remote_id = _text(payload.get("id"))
        if not remote_id:
            continue

        number = (
            _text(payload.get("number"))
            or _text(payload.get("bid_package_number"))
            or f"BP-{remote_id}"
        )
        name = _text(payload.get("title")) or _text(payload.get("name")) or number

        # Procore does not carry a CSI division on a bid package, so infer it
        # from a leading two-digit number in the package number ("BP-21A" -> 21)
        # and leave it blank rather than guessing when that fails.
        division = _infer_division(number, name)

        fields = {
            "number": number,
            "name": name,
            "division_code": division,
            "trade_name": _text(payload.get("trade")) or None,
        }

        package = existing.get(remote_id)
        if package is None:
            db.session.add(
                BidPackage(
                    project_id=project.id,
                    organization_id=project.organization_id,
                    procore_bid_package_id=remote_id,
                    **fields,
                )
            )
            result.packages_created += 1
        else:
            changed = False
            for key, value in fields.items():
                if value and getattr(package, key) != value:
                    setattr(package, key, value)
                    changed = True
            if changed:
                result.packages_updated += 1

    db.session.commit()
    return result


_DIVISION_PATTERN = None


def _infer_division(number: str, name: str) -> str | None:
    """Pull a CSI division out of a package number such as ``BP-21A``."""
    global _DIVISION_PATTERN
    if _DIVISION_PATTERN is None:
        import re

        _DIVISION_PATTERN = re.compile(r"(?<!\d)(\d{2})(?!\d)")

    for candidate in (number, name):
        if not candidate:
            continue
        for match in _DIVISION_PATTERN.finditer(candidate):
            code = normalize_code(match.group(1))
            # A two-digit run that happens to land on a number CSI reserves
            # (20, 24, 29...) is a coincidence, not a division. Leave the field
            # blank rather than tagging the package with a division that does
            # not exist.
            if code and is_specifiable(code):
                return code
    return None


def touch_connection(connection: Any, error: str | None = None) -> None:
    connection.last_sync_at = utcnow()
    connection.last_error = error
    db.session.commit()
