#!/usr/bin/env python3
"""Policy helpers for the paused nightly technical-debt workflow.

The workflow deliberately stores its metadata inside the existing launch and
state payloads.  This module contains only deterministic policy and metadata
operations; it does not create schedules, worktrees, pull requests, or make
network calls.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from autoresearch_core import AutoresearchError, decimal_to_json_number, parse_decimal, utc_now


NIGHTLY_RUN_KIND = "nightly_technical_debt"
NIGHTLY_PROVIDER = "luna"
NIGHTLY_DEPTH = "deep"
NIGHTLY_TIMEZONE = "America/Los_Angeles"
NIGHTLY_START_TIME = "20:00"
NIGHTLY_CUTOFF_TIME = "05:30"
NIGHTLY_COST_CEILING_USD = Decimal("25")
NIGHTLY_REQUIRED_PR_STATE = "ready_for_review"


def create_goal_metadata(
    *,
    title: str,
    goal_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    title = str(title).strip()
    if not title:
        raise AutoresearchError("Nightly Goal title may not be empty.")
    return {
        "goal_id": str(goal_id or f"goal-{uuid4()}"),
        "title": title,
        "status": "active",
        "created_at": created_at or utc_now(),
        "completed_at": None,
        "completion_reason": None,
    }


def complete_goal_metadata(
    metadata: dict[str, Any],
    *,
    reason: str,
    completed_at: str | None = None,
) -> dict[str, Any]:
    updated = deepcopy(metadata)
    if not isinstance(updated, dict) or not updated.get("goal_id"):
        raise AutoresearchError("Cannot complete missing nightly Goal metadata.")
    completion_reason = str(reason).strip()
    if not completion_reason:
        raise AutoresearchError("Nightly Goal completion reason may not be empty.")
    updated["status"] = "complete"
    updated["completed_at"] = completed_at or utc_now()
    updated["completion_reason"] = completion_reason
    return updated


def build_nightly_run_metadata(
    *,
    title: str,
    primary_repo: Path,
    goal_id: str | None = None,
    created_at: str | None = None,
    candidate_worktree_root: Path | None = None,
) -> dict[str, Any]:
    primary_repo = primary_repo.resolve()
    worktree_root = (candidate_worktree_root or primary_repo.parent / f".{primary_repo.name}-nightly-candidates").resolve()
    return {
        "run_kind": NIGHTLY_RUN_KIND,
        "goal": create_goal_metadata(title=title, goal_id=goal_id, created_at=created_at),
        "routing": {
            "provider": NIGHTLY_PROVIDER,
            "depth": NIGHTLY_DEPTH,
            "enforced": True,
        },
        "schedule": {
            "timezone": NIGHTLY_TIMEZONE,
            "start_at": NIGHTLY_START_TIME,
            "cutoff_at": NIGHTLY_CUTOFF_TIME,
            "stop_launching_before_cutoff": True,
            "activation": "paused",
            "production_approved": False,
        },
        "cost": {
            "currency": "USD",
            "ceiling_usd": decimal_to_json_number(NIGHTLY_COST_CEILING_USD),
            "measured_usd": 0,
            "measurement_required": True,
        },
        "isolation": {
            "primary_repo": str(primary_repo),
            "candidate_worktree_root": str(worktree_root),
            "separate_candidate_worktrees": True,
            "separate_candidate_prs": True,
        },
        "review": {
            "required_pr_state": NIGHTLY_REQUIRED_PR_STATE,
            "authority": "ready_pr_only",
            "allow_merge": False,
            "allow_deploy": False,
            "allow_online_review": False,
        },
        "checkpoint": None,
    }


def validate_nightly_run_metadata(
    metadata: Any,
    *,
    primary_repo: Path | None = None,
) -> None:
    if not isinstance(metadata, dict) or metadata.get("run_kind") != NIGHTLY_RUN_KIND:
        raise AutoresearchError("Invalid nightly metadata: run_kind must be nightly_technical_debt.")

    routing = metadata.get("routing")
    if not isinstance(routing, dict) or routing.get("provider") != NIGHTLY_PROVIDER or routing.get("depth") != NIGHTLY_DEPTH:
        raise AutoresearchError("Nightly technical-debt runs require enforced Luna deep routing.")
    if routing.get("enforced") is not True:
        raise AutoresearchError("Nightly Luna deep routing must be enforced.")

    schedule = metadata.get("schedule")
    required_schedule = {
        "timezone": NIGHTLY_TIMEZONE,
        "start_at": NIGHTLY_START_TIME,
        "cutoff_at": NIGHTLY_CUTOFF_TIME,
    }
    if not isinstance(schedule, dict) or any(schedule.get(key) != value for key, value in required_schedule.items()):
        raise AutoresearchError("Nightly runs must use the 20:00-05:30 America/Los_Angeles window.")
    if schedule.get("stop_launching_before_cutoff") is not True:
        raise AutoresearchError("Nightly runs must stop launching candidates before cutoff.")

    cost = metadata.get("cost")
    if not isinstance(cost, dict) or parse_decimal(cost.get("ceiling_usd"), "nightly cost ceiling") != NIGHTLY_COST_CEILING_USD:
        raise AutoresearchError("Nightly runs require a measured USD 25 provider-cost ceiling.")
    measured = parse_decimal(cost.get("measured_usd", 0), "nightly measured provider cost")
    if measured < 0 or measured > NIGHTLY_COST_CEILING_USD:
        raise AutoresearchError("Nightly measured provider cost must remain between 0 and 25 USD.")

    review = metadata.get("review")
    required_review = {
        "required_pr_state": NIGHTLY_REQUIRED_PR_STATE,
        "authority": "ready_pr_only",
        "allow_merge": False,
        "allow_deploy": False,
        "allow_online_review": False,
    }
    if not isinstance(review, dict) or any(review.get(key) != value for key, value in required_review.items()):
        raise AutoresearchError("Nightly runs may produce ready-for-review PRs only; merge, deploy, and online review are disabled.")

    isolation = metadata.get("isolation")
    if not isinstance(isolation, dict) or isolation.get("separate_candidate_worktrees") is not True or isolation.get("separate_candidate_prs") is not True:
        raise AutoresearchError("Nightly candidates require separate worktrees and pull requests.")
    if primary_repo is not None and Path(str(isolation.get("primary_repo", ""))).resolve() != primary_repo.resolve():
        raise AutoresearchError("Nightly metadata primary repo does not match the runtime repo.")
    if primary_repo is not None and isolation.get("candidate_worktree_root"):
        validate_candidate_isolation(
            primary_repo=primary_repo,
            candidate_worktree=Path(str(isolation["candidate_worktree_root"])),
        )


def enforce_nightly_activation(metadata: dict[str, Any], approvals: dict[str, Any] | None = None) -> None:
    validate_nightly_run_metadata(metadata)
    schedule = metadata["schedule"]
    approvals = approvals if isinstance(approvals, dict) else {}
    production_approved = schedule.get("production_approved") is True and approvals.get("production") in {
        True,
        "true",
        "yes",
        "approved",
    }
    if schedule.get("activation") != "approved" or not production_approved:
        raise AutoresearchError(
            "Nightly technical-debt launch is paused until explicit production approval."
        )


def nightly_window_status(moment: datetime) -> str:
    """Return before_window, within_window, or cutoff_reached for Pacific time."""
    zone = ZoneInfo(NIGHTLY_TIMEZONE)
    local = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    current = local.time().replace(second=0, microsecond=0)
    start = time(20, 0)
    cutoff = time(5, 30)
    if current >= cutoff and current < start:
        return "cutoff_reached"
    if current >= start or current < cutoff:
        return "within_window"
    return "before_window"


def evaluate_nightly_launch_window(moment: datetime) -> dict[str, Any]:
    status = nightly_window_status(moment)
    return {
        "status": status,
        "allowed": status == "within_window",
        "stop_launching": status != "within_window",
        "checkpoint_active_candidate": status == "cutoff_reached",
        "timezone": NIGHTLY_TIMEZONE,
        "start_at": NIGHTLY_START_TIME,
        "cutoff_at": NIGHTLY_CUTOFF_TIME,
    }


def provider_cost_gate(metadata: dict[str, Any], additional_usd: Any = 0) -> dict[str, Any]:
    validate_nightly_run_metadata(metadata)
    current = parse_decimal(metadata["cost"].get("measured_usd", 0), "nightly measured provider cost")
    additional = parse_decimal(additional_usd, "provider cost")
    if additional < 0:
        raise AutoresearchError("Provider cost may not be negative.")
    projected = current + additional
    return {
        "allowed": projected <= NIGHTLY_COST_CEILING_USD
        and not (additional == 0 and current >= NIGHTLY_COST_CEILING_USD),
        "ceiling_usd": decimal_to_json_number(NIGHTLY_COST_CEILING_USD),
        "measured_usd": decimal_to_json_number(current),
        "additional_usd": decimal_to_json_number(additional),
        "projected_usd": decimal_to_json_number(projected),
        "remaining_usd": decimal_to_json_number(max(NIGHTLY_COST_CEILING_USD - current, Decimal("0"))),
    }


def record_provider_cost(
    metadata: dict[str, Any],
    *,
    cost_usd: Any,
    measured_at: str | None = None,
) -> dict[str, Any]:
    updated = deepcopy(metadata)
    gate = provider_cost_gate(updated, cost_usd)
    if not gate["allowed"]:
        raise AutoresearchError(
            f"Nightly provider-cost ceiling exceeded: {gate['projected_usd']} USD > {gate['ceiling_usd']} USD."
        )
    updated["cost"]["measured_usd"] = gate["projected_usd"]
    updated["cost"]["last_charge_usd"] = gate["additional_usd"]
    updated["cost"]["last_measured_at"] = measured_at or utc_now()
    return updated


def validate_candidate_isolation(*, primary_repo: Path, candidate_worktree: Path) -> None:
    primary = primary_repo.resolve()
    candidate = candidate_worktree.resolve()
    if candidate == primary or primary in candidate.parents:
        raise AutoresearchError("Candidate worktree must be separate from and outside the primary repository.")


def register_candidate(
    metadata: dict[str, Any],
    *,
    candidate_id: str,
    worktree: Path,
    branch: str,
    pr_state: str = "pending",
    pr_number: str | int | None = None,
) -> dict[str, Any]:
    updated = deepcopy(metadata)
    validate_nightly_run_metadata(updated)
    candidate_id = str(candidate_id).strip()
    branch = str(branch).strip()
    if not candidate_id or not branch:
        raise AutoresearchError("Candidate id and branch are required for isolation.")
    if pr_state == NIGHTLY_REQUIRED_PR_STATE and pr_number is None:
        raise AutoresearchError("A ready-for-review candidate must have its own pull request.")
    validate_candidate_isolation(
        primary_repo=Path(updated["isolation"]["primary_repo"]),
        candidate_worktree=worktree,
    )
    candidate_path = str(worktree.resolve())
    candidates = list(updated.get("candidates") or [])
    for existing in candidates:
        if candidate_id == existing.get("candidate_id") or candidate_path == existing.get("worktree") or branch == existing.get("branch"):
            raise AutoresearchError("Each nightly candidate must have a unique worktree and branch.")
        if pr_number is not None and str(pr_number) == str(existing.get("pr_number")):
            raise AutoresearchError("Each nightly candidate must have a separate pull request.")
    candidate = {
        "candidate_id": candidate_id,
        "worktree": candidate_path,
        "branch": branch,
        "pr_number": str(pr_number) if pr_number is not None else None,
        "pr_state": str(pr_state),
        "created_at": utc_now(),
    }
    candidates.append(candidate)
    updated["candidates"] = candidates
    updated["active_candidate"] = candidate
    return updated


def ready_pr_authority(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {"allowed": False, "reason": "missing_candidate"}
    pr = candidate.get("pull_request") if isinstance(candidate.get("pull_request"), dict) else candidate
    state = pr.get("pr_state") or pr.get("review_state")
    if state == NIGHTLY_REQUIRED_PR_STATE:
        return {"allowed": True, "reason": "ready_for_review"}
    return {
        "allowed": False,
        "reason": "pr_not_ready_for_review",
        "required_state": NIGHTLY_REQUIRED_PR_STATE,
        "observed_state": state or "missing",
    }


def checkpoint_run_metadata(
    metadata: dict[str, Any],
    *,
    reason: str,
    saved_at: str | None = None,
) -> dict[str, Any]:
    updated = deepcopy(metadata)
    validate_nightly_run_metadata(updated)
    updated["checkpoint"] = {
        "status": "paused",
        "reason": str(reason),
        "saved_at": saved_at or utc_now(),
        "candidate": deepcopy(updated.get("active_candidate")),
    }
    return updated
