from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
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
    enforce_nightly_activation,
    provider_cost_gate,
    ready_pr_authority,
    record_provider_cost,
    register_candidate,
    validate_candidate_isolation,
)
from autoresearch_core import AutoresearchError, parse_decimal
from autoresearch_resume_prompt import build_runtime_prompt


class NightlyControlPlaneTest(AutoresearchScriptsTestBase):
    def test_ordinary_recovery_prompt_allows_corrupt_or_incomplete_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            created = self.create_launch_manifest(repo, guard=None)
            launch_path = Path(str(created["launch_path"]))
            manifest = json.loads(launch_path.read_text(encoding="utf-8"))
            state_path = repo / "autoresearch-state.json"
            for contents in ('{"interrupted":', '{"version": 1}', '[]'):
                with self.subTest(contents=contents):
                    state_path.write_text(contents, encoding="utf-8")
                    prompt = build_runtime_prompt(
                        launch_manifest=manifest,
                        launch_context={"decision": "resumable", "resume_strategy": "tsv_fallback"},
                        launch_path=launch_path,
                        results_path=repo / "research-results.tsv",
                        state_path=state_path,
                    )
                    self.assertIn("resumable (tsv_fallback)", prompt)

    def test_saved_nightly_state_cannot_be_resumed_by_an_ordinary_manifest(self) -> None:
        fixture = SCRIPTS_DIR.parent / "tests/e2e-fixtures/exec_marker_reduction/autoresearch-state.json"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            created = self.create_launch_manifest(repo, guard=None)
            launch_path = Path(str(created["launch_path"]))
            manifest = json.loads(launch_path.read_text(encoding="utf-8"))
            state_path = repo / "autoresearch-state.json"
            metadata = build_nightly_run_metadata(title="Nightly debt", primary_repo=repo)
            metadata["schedule"].update(activation="approved", production_approved=True)
            for location in ("top_level", "config", "state"):
                with self.subTest(location=location):
                    state = json.loads(fixture.read_text(encoding="utf-8"))
                    section = state if location == "top_level" else state[location]
                    section["run_metadata"] = metadata
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    result = self.run_script_completed(
                        "autoresearch_runtime_ctl.py", "run", "--repo", str(repo)
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("activation is not implemented", result.stderr)
                    self.assertFalse((repo / "autoresearch-runtime.json").exists())
                    with self.assertRaisesRegex(AutoresearchError, "activation is not implemented"):
                        build_runtime_prompt(
                            launch_manifest=manifest,
                            launch_context={},
                            launch_path=launch_path,
                            results_path=repo / "research-results.tsv",
                            state_path=state_path,
                        )

    def test_approval_flags_cannot_activate_unimplemented_nightly_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata = build_nightly_run_metadata(title="Nightly debt", primary_repo=Path(tmp))
            metadata["schedule"].update(activation="approved", production_approved=True)
            with self.assertRaisesRegex(AutoresearchError, "activation is not implemented"):
                enforce_nightly_activation(metadata, {"production": True})

    def test_window_requires_aware_timestamps_and_converts_utc_across_dst(self) -> None:
        with self.assertRaisesRegex(AutoresearchError, "timezone-aware"):
            evaluate_nightly_launch_window(datetime(2026, 8, 20, 5, 30))
        for month, day, utc_cutoff_hour in [(8, 20, 12), (11, 3, 13)]:
            with self.subTest(month=month):
                before = evaluate_nightly_launch_window(
                    datetime(2026, month, day, utc_cutoff_hour, 29, tzinfo=timezone.utc)
                )
                cutoff = evaluate_nightly_launch_window(
                    datetime(2026, month, day, utc_cutoff_hour, 30, tzinfo=timezone.utc)
                )
                self.assertTrue(before["allowed"])
                self.assertFalse(cutoff["allowed"])
                self.assertTrue(cutoff["checkpoint_active_candidate"])

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

    def test_nightly_manifest_cannot_start_run_or_generate_an_execution_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake_codex = repo / "fake-codex"
            marker = repo / "child-was-launched"
            fake_codex.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
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
            launch_path = Path(str(created["launch_path"]))
            manifest = json.loads(launch_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_metadata"]["schedule"]["activation"], "paused")
            self.assertFalse(manifest["run_metadata"]["schedule"]["production_approved"])
            for approved in (False, True):
                for location in ("both", "top_level", "config"):
                    edited = deepcopy(manifest)
                    if approved:
                        for section in (edited, edited["config"]):
                            section["run_metadata"]["schedule"].update(
                                activation="approved", production_approved=True
                            )
                        edited["approvals"] = {"production": True}
                    if location == "top_level":
                        edited["config"].pop("run_metadata")
                    if location == "config":
                        edited.pop("run_metadata")
                    launch_path.write_text(json.dumps(edited), encoding="utf-8")
                    commands = [
                        ("autoresearch_runtime_ctl.py", action, "--codex-bin", str(fake_codex))
                        for action in ("start", "run")
                    ]
                    commands.append(("autoresearch_resume_prompt.py",))
                    for command in commands:
                        with self.subTest(approved=approved, location=location, command=command[0:2]):
                            result = self.run_script_completed(*command, "--repo", str(repo))
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("activation is not implemented", result.stderr)
                            self.assertFalse(marker.exists())
                            self.assertFalse((repo / "autoresearch-runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
