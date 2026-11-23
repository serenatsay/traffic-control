"""Behavioral tests using throwaway Git repositories."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "traffic-control" / "scripts" / "traffic_control.py"


class TrafficControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "sample"
        self.repo.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "feature.txt").write_text("base\n", encoding="utf-8")
        self.git(self.repo, "add", "feature.txt")
        self.git(self.repo, "commit", "-m", "Initial commit")

    def git(self, folder: Path, *args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=False)
        if success and result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def cli(self, folder: Path, *args: str, success: bool = True) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(folder), "--json", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"invalid helper output: {result.stdout}\n{result.stderr}")
        if success and result.returncode:
            self.fail(f"helper failed: {payload}")
        if not success:
            self.assertNotEqual(result.returncode, 0)
        return payload

    def commit(self, folder: Path, filename: str, content: str, message: str) -> None:
        (folder / filename).write_text(content, encoding="utf-8")
        self.git(folder, "add", filename)
        self.git(folder, "commit", "-m", message)

    def test_start_reuses_feature_across_conversations(self) -> None:
        first = self.cli(self.repo, "start", "Grammar quiz", "--agent", "codex")
        self.assertEqual(first["action"], "created")
        self.assertEqual(first["branch"], "traffic/grammar-quiz")
        self.assertTrue(Path(first["folder"]).is_dir())
        second = self.cli(self.repo, "start", "Grammar quiz", "--agent", "claude")
        self.assertEqual(second["action"], "reused")
        self.assertEqual(second["folder"], first["folder"])
        self.assertEqual(second["branch"], first["branch"])
        self.assertEqual(second["agents"], ["codex", "claude"])
        self.assertEqual(len(self.cli(self.repo, "status")["tasks"]), 1)

    def test_parallel_attempt_gets_its_own_branch_and_folder(self) -> None:
        first = self.cli(self.repo, "start", "Grammar quiz")
        second = self.cli(self.repo, "start", "Grammar quiz", "--parallel")
        self.assertEqual(second["id"], "grammar-quiz-alt-2")
        self.assertEqual(second["chat_title"], "Grammar quiz (alternative 2)")
        self.assertNotEqual(second["folder"], first["folder"])
        self.assertNotEqual(second["branch"], first["branch"])

    def test_start_preserves_uncommitted_changes_in_original_checkout(self) -> None:
        (self.repo / "feature.txt").write_text("my unfinished edit\n", encoding="utf-8")
        (self.repo / "private-notes.txt").write_text("keep this\n", encoding="utf-8")
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "my unfinished edit\n")
        self.assertEqual((self.repo / "private-notes.txt").read_text(encoding="utf-8"), "keep this\n")
        self.assertEqual((Path(task["folder"]) / "feature.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((Path(task["folder"]) / "private-notes.txt").exists())

    def test_adopts_existing_linked_worktree_instead_of_creating_another(self) -> None:
        linked = self.base / "claude-worktree"
        self.git(self.repo, "worktree", "add", "--detach", str(linked), "main")
        adopted = self.cli(linked, "start", "Logo animation", "--agent", "claude")
        self.assertEqual(adopted["action"], "adopted")
        self.assertEqual(adopted["folder"], str(linked.resolve()))
        self.assertEqual(adopted["branch"], "traffic/logo-animation")
        self.assertEqual(len(self.git(self.repo, "worktree", "list", "--porcelain").stdout.split("worktree ")) - 1, 2)

    def test_invalid_base_does_not_change_detached_worktree_or_record_task(self) -> None:
        linked = self.base / "claude-worktree"
        self.git(self.repo, "worktree", "add", "--detach", str(linked), "main")
        before = self.git(linked, "rev-parse", "HEAD").stdout.strip()
        blocked = self.cli(linked, "start", "Logo animation", "--base", "missing-branch", success=False)
        self.assertIn("does not exist", blocked["error"])
        self.assertEqual(self.git(linked, "branch", "--show-current").stdout.strip(), "")
        self.assertEqual(self.git(linked, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertNotEqual(self.git(self.repo, "show-ref", "--verify", "refs/heads/traffic/logo-animation", success=False).returncode, 0)
        self.assertEqual(self.cli(self.repo, "status")["tasks"], [])

    def test_handoff_note_is_available_when_resuming(self) -> None:
        self.cli(self.repo, "start", "Grammar quiz")
        self.cli(self.repo, "handoff", "Grammar quiz", "--note", "Quiz screen is done; verify the answers.")
        resumed = self.cli(self.repo, "resume", "Grammar quiz")
        self.assertEqual(resumed["handoff"], "Quiz screen is done; verify the answers.")

    def test_reuse_and_resume_refuse_a_task_folder_on_another_branch(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        folder = Path(task["folder"])
        self.git(folder, "switch", "-c", "other-work")
        reused = self.cli(self.repo, "start", "Grammar quiz", success=False)
        resumed = self.cli(self.repo, "resume", "Grammar quiz", success=False)
        self.assertIn("recorded branch", reused["error"])
        self.assertIn("recorded branch", resumed["error"])
        self.assertEqual(self.git(folder, "branch", "--show-current").stdout.strip(), "other-work")

    def test_reuse_and_resume_refuse_a_folder_replaced_by_another_repo(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        folder = Path(task["folder"])
        self.git(self.repo, "worktree", "remove", str(folder))
        folder.mkdir()
        self.git(folder, "init", "-b", "main")
        reused = self.cli(self.repo, "start", "Grammar quiz", success=False)
        resumed = self.cli(self.repo, "resume", "Grammar quiz", success=False)
        self.assertIn("different Git project", reused["error"])
        self.assertIn("different Git project", resumed["error"])

    def test_merge_requires_clean_folders_then_merges_locally(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        task_folder = Path(task["folder"])
        (task_folder / "unfinished.txt").write_text("not committed", encoding="utf-8")
        blocked = self.cli(self.repo, "merge", "Grammar quiz", "--apply", success=False)
        self.assertIn("unsaved", blocked["error"])
        (task_folder / "unfinished.txt").unlink()
        self.commit(task_folder, "quiz.txt", "quiz ready\n", "Add quiz")
        preview = self.cli(self.repo, "merge", "Grammar quiz")
        self.assertEqual(preview["action"], "ready_to_merge")
        self.assertFalse((self.repo / "quiz.txt").exists())
        merged = self.cli(self.repo, "merge", "Grammar quiz", "--apply")
        self.assertEqual(merged["action"], "merged")
        self.assertEqual((self.repo / "quiz.txt").read_text(encoding="utf-8"), "quiz ready\n")
        self.assertTrue(task_folder.is_dir())
        self.assertEqual(self.cli(self.repo, "status")["tasks"][0]["state"], "merged")

    def test_merge_leaves_dirty_destination_untouched(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.commit(Path(task["folder"]), "quiz.txt", "quiz ready\n", "Add quiz")
        (self.repo / "my-notes.txt").write_text("keep me", encoding="utf-8")
        blocked = self.cli(self.repo, "merge", "Grammar quiz", "--apply", success=False)
        self.assertIn("unsaved", blocked["error"])
        self.assertEqual((self.repo / "my-notes.txt").read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.repo / "quiz.txt").exists())

    def test_merge_detects_conflict_without_touching_destination(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.commit(Path(task["folder"]), "feature.txt", "feature version\n", "Feature edit")
        self.commit(self.repo, "feature.txt", "main version\n", "Main edit")
        before = self.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        blocked = self.cli(self.repo, "merge", "Grammar quiz", success=False)
        self.assertIn("conflict", blocked["error"])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "main version\n")

    def test_merge_refuses_when_local_main_is_behind_remote(self) -> None:
        origin = self.base / "origin.git"
        self.git(self.base, "init", "--bare", str(origin))
        self.git(self.repo, "remote", "add", "origin", str(origin))
        self.git(self.repo, "push", "-u", "origin", "main")
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.assertNotEqual(
            self.git(Path(task["folder"]), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", success=False).returncode,
            0,
        )
        self.commit(Path(task["folder"]), "quiz.txt", "quiz ready\n", "Add quiz")

        other = self.base / "other"
        self.git(self.base, "clone", str(origin), str(other))
        self.git(other, "checkout", "main")
        self.git(other, "config", "user.name", "Other User")
        self.git(other, "config", "user.email", "other@example.com")
        self.commit(other, "new.txt", "remote edit\n", "Advance main")
        self.git(other, "push", "origin", "main")
        # The read-only preview uses already-fetched state. Apply fetches again.
        preview = self.cli(self.repo, "merge", "Grammar quiz")
        self.assertEqual(preview["action"], "ready_to_merge")
        blocked_apply = self.cli(self.repo, "merge", "Grammar quiz", "--apply", success=False)
        self.assertIn("behind", blocked_apply["error"])
        self.assertFalse((self.repo / "quiz.txt").exists())
        blocked = self.cli(self.repo, "merge", "Grammar quiz", success=False)
        self.assertIn("behind", blocked["error"])
        self.assertFalse((self.repo / "quiz.txt").exists())

    def test_externally_merged_branch_is_reconciled_without_new_commit(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.commit(Path(task["folder"]), "quiz.txt", "quiz ready\n", "Add quiz")
        self.git(self.repo, "merge", "--no-ff", "--no-edit", task["branch"])
        before = self.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        preview = self.cli(self.repo, "merge", "Grammar quiz")
        self.assertEqual(preview["action"], "already_merged_in_git")
        reconciled = self.cli(self.repo, "merge", "Grammar quiz", "--apply")
        self.assertEqual(reconciled["action"], "reconciled")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(self.cli(self.repo, "status")["tasks"][0]["state"], "merged")

    def test_squash_merged_changes_are_reconciled_without_empty_merge_commit(self) -> None:
        task = self.cli(self.repo, "start", "Grammar quiz")
        self.commit(Path(task["folder"]), "quiz.txt", "quiz ready\n", "Add quiz")
        self.git(self.repo, "merge", "--squash", task["branch"])
        self.git(self.repo, "commit", "-m", "Squash grammar quiz")
        before = self.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertFalse(self.git(self.repo, "merge-base", "--is-ancestor", task["branch"], "main", success=False).returncode == 0)
        reconciled = self.cli(self.repo, "merge", "Grammar quiz", "--apply")
        self.assertEqual(reconciled["action"], "reconciled")
        self.assertEqual(reconciled["reason"], "changes_already_present")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(self.cli(self.repo, "status")["tasks"][0]["state"], "merged")


if __name__ == "__main__":
    unittest.main()
