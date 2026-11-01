from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import AutoresearchScriptsTestBase, SCRIPTS_DIR

import sys

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from autoresearch_helpers import (
    build_nightly_run_metadata,
    build_state_payload,
    checkpoint_state,
    complete_goal_metadata,
    complete_state_goal,
    create_goal_metadata,
    evaluate_nightly_launch_window,
    provider_cost_gate,
    ready_pr_authority,
    record_provider_cost,
    register_candidate,
    validate_candidate_isolation,
)
from autoresearch_core import AutoresearchError, parse_decimal


class NightlyControlPlaneTest(AutoresearchScriptsTestBase):
    def test_goal_creation_and_completion_metadata(self) -> None:
        goal = create_goal_metadata(
            title="Reduce technical debt in the routing kit",
            goal_id="goal-routing-kit",
            created_at="2026-08-20T03:00:00Z",
        )
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["created_at"], "2026-08-20T03:00:00Z")

        completed = complete_goal_metadata(
            goal,
            reason="ready-for-review candidate PR prepared",
            completed_at="2026-08-20T05:20:00Z",
        )
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["completed_at"], "2026-08-20T05:20:00Z")
        self.assertEqual(goal["status"], "active")

    def test_checkpoint_and_goal_completion_extend_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            metadata = build_nightly_run_metadata(
                title="Nightly debt",
                primary_repo=repo,
                goal_id="goal-1",
                created_at="2026-08-20T03:00:00Z",
            )
            state_path = repo / "autoresearch-state.json"
            payload = build_state_payload(
                mode="loop",
                run_tag="nightly",
                config={
                    "goal": "Nightly debt",
                    "scope": "scripts/",
                    "metric": "issues",
                    "direction": "lower",
                    "verify": "python3 -c pass",
                    "run_metadata": metadata,
                },
                summary={
                    "iteration": 0,
                    "baseline_metric": parse_decimal("10"),
                    "best_metric": parse_decimal("10"),
                    "best_iteration": 0,
                    "current_metric": parse_decimal("10"),
                    "last_commit": "base",
                    "last_trial_commit": "base",
                    "last_trial_metric": parse_decimal("10"),
                    "current_labels": [],
                    "last_trial_labels": [],
                    "keeps": 0,
                    "discards": 0,
                    "crashes": 0,
                    "no_ops": 0,
                    "blocked": 0,
                    "consecutive_discards": 0,
                    "pivot_count": 0,
                    "last_status": "baseline",
                },
            )
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            checkpoint_state(state_path, reason="nightly_cutoff_reached", saved_at="2026-08-20T05:30:00Z")
            checkpointed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                checkpointed["state"]["run_metadata"]["checkpoint"]["reason"],
                "nightly_cutoff_reached",
            )

            complete_state_goal(
                state_path,
                reason="candidate PR ready",
                completed_at="2026-08-20T05:31:00Z",
            )
            completed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["run_metadata"]["goal"]["status"], "complete")
            self.assertEqual(completed["config"]["run_metadata"]["goal"]["status"], "complete")

    def test_cutoff_stops_launching_and_requests_checkpoint(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        before_window = evaluate_nightly_launch_window(datetime(2026, 8, 20, 19, 59, tzinfo=zone))
        active = evaluate_nightly_launch_window(datetime(2026, 8, 20, 23, 0, tzinfo=zone))
        cutoff = evaluate_nightly_launch_window(datetime(2026, 8, 21, 5, 30, tzinfo=zone))
        self.assertFalse(before_window["allowed"])
        self.assertTrue(active["allowed"])
        self.assertTrue(cutoff["stop_launching"])
        self.assertTrue(cutoff["checkpoint_active_candidate"])

    def test_cost_ceiling_is_measured_and_cumulative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata = build_nightly_run_metadata(title="Nightly debt", primary_repo=Path(tmp))
            metadata = record_provider_cost(metadata, cost_usd="20.00", measured_at="2026-08-20T03:00:00Z")
            self.assertEqual(provider_cost_gate(metadata)["remaining_usd"], 5)
            metadata = record_provider_cost(metadata, cost_usd="5.00", measured_at="2026-08-20T03:05:00Z")
            self.assertEqual(metadata["cost"]["measured_usd"], 25)
            with self.assertRaises(AutoresearchError):
                record_provider_cost(metadata, cost_usd="0.01")

    def test_candidates_use_fresh_worktrees_and_separate_prs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "routing-kit"
            primary.mkdir()
            metadata = build_nightly_run_metadata(title="Nightly debt", primary_repo=primary)
            candidate_path = root / "candidate-1"
            validate_candidate_isolation(primary_repo=primary, candidate_worktree=candidate_path)
            metadata = register_candidate(
                metadata,
                candidate_id="candidate-1",
                worktree=candidate_path,
                branch="nightly/candidate-1",
                pr_number=101,
            )
            with self.assertRaises(AutoresearchError):
                register_candidate(
                    metadata,
                    candidate_id="candidate-2",
                    worktree=root / "candidate-2",
                    branch="nightly/candidate-2",
                    pr_number=101,
                )
            with self.assertRaises(AutoresearchError):
                validate_candidate_isolation(primary_repo=primary, candidate_worktree=primary / "nested")

    def test_ready_for_review_pr_is_the_only_authority(self) -> None:
        self.assertFalse(ready_pr_authority(None)["allowed"])
        self.assertFalse(ready_pr_authority({"pr_state": "draft"})["allowed"])
        self.assertFalse(ready_pr_authority({"pr_state": "merged"})["allowed"])
        self.assertTrue(ready_pr_authority({"pr_state": "ready_for_review"})["allowed"])

    def test_nightly_manifest_is_paused_and_runtime_start_requires_production_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake_codex = repo / "fake-codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            created = self.run_script(
                "autoresearch_runtime_ctl.py",
                "create-launch",
                "--repo",
                str(repo),
                "--original-goal",
                "Nightly debt",
                "--mode",
                "loop",
                "--goal",
                "Nightly debt",
                "--scope",
                "scripts/",
                "--metric-name",
                "issues",
                "--direction",
                "lower",
                "--verify",
                "python3 -c pass",
                "--nightly-technical-debt",
                "--goal-id",
                "goal-routing-kit",
            )
            manifest = json.loads(Path(str(created["launch_path"])).read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_metadata"]["schedule"]["activation"], "paused")
            self.assertFalse(manifest["run_metadata"]["schedule"]["production_approved"])
            started = self.run_script_completed(
                "autoresearch_runtime_ctl.py",
                "start",
                "--repo",
                str(repo),
                "--codex-bin",
                str(fake_codex),
            )
            self.assertNotEqual(started.returncode, 0)
            self.assertIn("paused until explicit production approval", started.stderr)


if __name__ == "__main__":
    unittest.main()
