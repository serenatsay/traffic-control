#!/usr/bin/env python3
"""Small, local Git helper for the Traffic Control agent skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import urlopen


class TrafficError(Exception):
    pass


def git(path: Path, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrafficError(f"Git could not finish: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise TrafficError(detail)
    return result


def git_ok(path: Path, *args: str) -> bool:
    return git(path, *args, check=False).returncode == 0


def repo_root(path: Path) -> Path:
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise TrafficError("Open this from a folder inside a Git project.")
    return Path(result.stdout.strip()).resolve()


def common_git_dir(path: Path) -> Path:
    result = git(path, "rev-parse", "--git-common-dir").stdout.strip()
    return (path / result).resolve()


def worktrees(path: Path) -> list[dict[str, str]]:
    raw = git(path, "worktree", "list", "--porcelain").stdout
    entries: list[dict[str, str]] = []
    for block in raw.strip().split("\n\n"):
        entry: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if entry.get("worktree"):
            entry["worktree"] = str(Path(entry["worktree"]).resolve())
            entries.append(entry)
    return entries


@contextmanager
def locked_registry(common: Path) -> Iterator[dict[str, Any]]:
    lock_path = common / "traffic-control.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            registry_path = common / "traffic-control.json"
            if registry_path.exists():
                try:
                    data = json.loads(registry_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise TrafficError("Traffic Control's task list cannot be read; leave it untouched and inspect it.") from exc
                if data.get("version") != 1 or not isinstance(data.get("tasks"), dict):
                    raise TrafficError("Traffic Control's task list has an unsupported format.")
            else:
                data = {"version": 1, "tasks": {}}
            yield data
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def save_registry(common: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=common, prefix="traffic-control-", delete=False) as out:
        temp_path = Path(out.name)
        json.dump(data, out, indent=2, ensure_ascii=False, sort_keys=True)
        out.write("\n")
    os.replace(temp_path, common / "traffic-control.json")


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if not slug:
        raise TrafficError("Give this task a short name using letters or numbers.")
    return slug[:50].rstrip("-")


def choose_base(path: Path, requested: str | None) -> tuple[str, str]:
    if requested:
        if not git_ok(path, "rev-parse", "--verify", f"{requested}^{{commit}}"):
            raise TrafficError(f"Starting point '{requested}' does not exist.")
        base = requested
    else:
        remote_head = git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        choices = [remote_head.stdout.strip(), "origin/main", "origin/master", "main", "master"]
        base = next((name for name in choices if name and git_ok(path, "rev-parse", "--verify", f"{name}^{{commit}}")), "")
        if not base:
            raise TrafficError("I could not find the project's main branch. Specify --base BRANCH.")
    target = base.removeprefix("origin/") if base.startswith("origin/") else base
    if not git_ok(path, "show-ref", "--verify", f"refs/heads/{target}"):
        raise TrafficError(f"The local branch '{target}' is missing. Create or select a local main branch first.")
    return base, target


def get_task(data: dict[str, Any], reference: str) -> tuple[str, dict[str, Any]]:
    tasks = data["tasks"]
    if reference in tasks:
        return reference, tasks[reference]
    matches = [(key, task) for key, task in tasks.items() if task["title"].casefold() == reference.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(key for key, _ in matches)
        raise TrafficError(f"More than one task has that name. Use one of these IDs: {names}.")
    raise TrafficError(f"I could not find a task called '{reference}'. Run status to see known tasks.")


def task_view(task_id: str, task: dict[str, Any], path: Path) -> dict[str, Any]:
    folder = Path(task["folder"])
    if folder.exists():
        changed = git(folder, "status", "--porcelain", check=False)
        changes = len(changed.stdout.splitlines()) if changed.returncode == 0 else None
    else:
        changes = None
    return {
        "id": task_id,
        "title": task["title"],
        "chat_title": task.get("chat_title", task["title"]),
        "agent": task.get("agent", "unknown"),
        "agents": task.get("agents", [task.get("agent", "unknown")]),
        "folder": task["folder"],
        "branch": task["branch"],
        "target": task["target"],
        "state": task.get("state", "open"),
        "changed_files": changes,
        "handoff": task.get("handoff", ""),
    }


def verify_task_folder(root: Path, task: dict[str, Any]) -> Path:
    folder = Path(task["folder"])
    if not folder.is_dir():
        raise TrafficError("The existing task folder is missing. Inspect it before continuing.")
    try:
        folder_common = common_git_dir(folder)
        current_branch = git(folder, "branch", "--show-current").stdout.strip()
    except TrafficError as exc:
        raise TrafficError("The task folder is no longer a usable worktree for this project. I left it untouched.") from exc
    if folder_common != common_git_dir(root):
        raise TrafficError("The task folder now belongs to a different Git project. I left it untouched.")
    if current_branch != task["branch"]:
        raise TrafficError(
            f"The task folder is on '{current_branch or 'no branch'}', not its recorded branch '{task['branch']}'. "
            "Inspect it before continuing."
        )
    return folder


def start(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    title = args.title.strip()
    if not title:
        raise TrafficError("Give this task a short name.")
    desired_id = slugify(title)
    with locked_registry(common) as data:
        tasks = data["tasks"]
        matches = [(key, task) for key, task in tasks.items() if task["title"].casefold() == title.casefold()]
        if matches and not args.parallel:
            open_matches = [(key, task) for key, task in matches if task.get("state") != "merged"]
            if len(open_matches) == 1:
                key, task = open_matches[0]
                verify_task_folder(root, task)
                if args.agent and args.agent != task.get("agent"):
                    agents = task.setdefault("agents", [task.get("agent", "unspecified")])
                    if args.agent not in agents:
                        agents.append(args.agent)
                    task["agent"] = args.agent
                    save_registry(common, data)
                return {"action": "reused", **task_view(key, task, root)}
            if len(open_matches) > 1:
                raise TrafficError("This feature has multiple active attempts. Use resume with its task ID.")
            raise TrafficError("That task was already merged. Give the new work a distinct name.")
        if args.parallel and not matches:
            raise TrafficError("There is no existing task to make a parallel attempt of. Start it normally first.")

        if args.parallel:
            number = 2
            while f"{desired_id}-alt-{number}" in tasks:
                number += 1
            task_id = f"{desired_id}-alt-{number}"
            chat_title = f"{title} (alternative {number})"
        else:
            task_id = desired_id
            chat_title = title
        if task_id in tasks:
            raise TrafficError("This task name is already in use. Choose a more specific name.")

        all_trees = worktrees(root)
        main_root = Path(all_trees[0]["worktree"])
        in_linked_tree = root != main_root
        if in_linked_tree and not args.parallel:
            claimed = next((key for key, task in tasks.items() if Path(task["folder"]).resolve() == root), None)
            if claimed:
                raise TrafficError(f"This folder already belongs to '{tasks[claimed]['title']}'. Resume that task or start a parallel attempt.")
            _, target = choose_base(root, args.base)
            branch = git(root, "branch", "--show-current").stdout.strip()
            if not branch:
                branch = f"traffic/{task_id}"
                if git_ok(root, "show-ref", "--verify", f"refs/heads/{branch}"):
                    raise TrafficError(f"Branch '{branch}' already exists. Choose a different task name.")
                git(root, "switch", "-c", branch)
            if branch == target:
                raise TrafficError("This folder is already on the project's main branch. Start a separate worktree for the feature.")
            folder = root
            action = "adopted"
        else:
            if not args.no_fetch and git_ok(root, "remote", "get-url", "origin"):
                fetch = git(root, "fetch", "origin", check=False)
                if fetch.returncode:
                    raise TrafficError("Could not update the project from origin. Check the network, or retry with --no-fetch.")
            base, target = choose_base(root, args.base)
            branch = f"traffic/{task_id}"
            if git_ok(root, "show-ref", "--verify", f"refs/heads/{branch}"):
                raise TrafficError(f"Branch '{branch}' already exists. Inspect it before starting again.")
            folder = main_root.parent / f"{main_root.name}-traffic-control" / task_id
            if folder.exists():
                raise TrafficError(f"The folder '{folder}' already exists. I left it untouched.")
            folder.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "add", "--no-track", "-b", branch, str(folder), base)
            action = "created"

        task = {
            "title": title,
            "chat_title": chat_title,
            "agent": args.agent or "unspecified",
            "agents": [args.agent or "unspecified"],
            "folder": str(folder),
            "branch": branch,
            "target": target,
            "state": "open",
            "handoff": "",
        }
        tasks[task_id] = task
        save_registry(common, data)
        return {"action": action, **task_view(task_id, task, root)}


def resume(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    with locked_registry(common) as data:
        key, task = get_task(data, args.task)
        if task.get("state") == "merged":
            raise TrafficError("This task has already been merged.")
        verify_task_folder(root, task)
        return {"action": "resumed", **task_view(key, task, root)}


def status(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    with locked_registry(common) as data:
        views = [task_view(key, task, root) for key, task in data["tasks"].items()]
    return {"action": "status", "tasks": views}


def handoff(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    note = args.note.strip()
    if not note or len(note) > 4000:
        raise TrafficError("Write a short handoff note of at most 4,000 characters.")
    with locked_registry(common) as data:
        key, task = get_task(data, args.task)
        if task.get("state") == "merged":
            raise TrafficError("This task has already been merged.")
        task["handoff"] = note
        save_registry(common, data)
        return {"action": "handoff_saved", **task_view(key, task, root)}


def merge_preflight(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    folder = verify_task_folder(root, task)
    branch = task["branch"]
    if git(folder, "status", "--porcelain").stdout.strip():
        raise TrafficError("The task has unsaved file changes. Commit or review them before merging.")

    target = task["target"]
    trees = worktrees(root)
    target_folder = next((Path(tree["worktree"]) for tree in trees if tree.get("branch") == f"refs/heads/{target}"), None)
    if target_folder is None:
        raise TrafficError(f"The project's '{target}' branch is not open in a worktree. Open it before merging.")
    if target_folder == folder:
        raise TrafficError("The task and destination point to the same folder. I did not merge anything.")
    if git(target_folder, "status", "--porcelain").stdout.strip():
        raise TrafficError(f"The '{target}' folder has unsaved changes. I did not touch them.")
    remote_target = f"origin/{target}"
    if git_ok(root, "show-ref", "--verify", f"refs/remotes/{remote_target}"):
        if not git_ok(root, "merge-base", "--is-ancestor", remote_target, target):
            raise TrafficError(f"Local '{target}' is behind or differs from '{remote_target}'. Update it before merging.")
    if git_ok(root, "merge-base", "--is-ancestor", branch, target):
        return {
            "already_merged": True,
            "target_folder": str(target_folder),
            "target": target,
            "branch": branch,
            "reason": "commit_in_history",
        }
    preview = git(root, "merge-tree", "--write-tree", target, branch, check=False)
    if preview.returncode == 1:
        raise TrafficError("These changes conflict with the destination branch. Resolve the overlap in the task folder first.")
    if preview.returncode:
        raise TrafficError("Git could not check this merge safely. A recent Git version with merge-tree --write-tree is needed.")
    merged_tree = preview.stdout.splitlines()[0].strip() if preview.stdout else ""
    target_tree = git(root, "rev-parse", f"{target}^{{tree}}").stdout.strip()
    if merged_tree == target_tree:
        return {
            "already_merged": True,
            "target_folder": str(target_folder),
            "target": target,
            "branch": branch,
            "reason": "changes_already_present",
        }
    return {"already_merged": False, "target_folder": str(target_folder), "target": target, "branch": branch}


def merge(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    with locked_registry(common) as data:
        key, task = get_task(data, args.task)
        if task.get("state") == "merged":
            return {"action": "already_merged", **task_view(key, task, root)}
        if args.apply and git_ok(root, "remote", "get-url", "origin"):
            fetched = git(root, "fetch", "origin", check=False)
            if fetched.returncode:
                raise TrafficError("Could not check the latest remote changes. I did not merge anything.")
        plan = merge_preflight(root, task)
        if plan["already_merged"]:
            if args.apply:
                task["state"] = "merged"
                task["merged_commit"] = git(root, "rev-parse", task["target"]).stdout.strip()
                save_registry(common, data)
                return {"action": "reconciled", **task_view(key, task, root), **plan}
            return {"action": "already_merged_in_git", **task_view(key, task, root), **plan}
        if not args.apply:
            return {"action": "ready_to_merge", **task_view(key, task, root), **plan}
        destination = Path(plan["target_folder"])
        before = git(destination, "rev-parse", "HEAD").stdout.strip()
        result = git(destination, "merge", "--no-ff", "--no-edit", task["branch"], check=False)
        if result.returncode:
            if git_ok(destination, "rev-parse", "--verify", "MERGE_HEAD"):
                aborted = git(destination, "merge", "--abort", check=False)
                if aborted.returncode:
                    raise TrafficError("Git could not finish or undo the merge. Inspect the destination before doing anything else.")
            raise TrafficError("Git could not complete the merge; the destination was restored to its previous state. Inspect the overlap before retrying.")
        after = git(destination, "rev-parse", "HEAD").stdout.strip()
        task["state"] = "merged"
        task["merged_commit"] = after
        save_registry(common, data)
        return {"action": "merged", **task_view(key, task, root), "previous_commit": before, "merged_commit": after}


@contextmanager
def preview_lock(common: Path) -> Iterator[None]:
    """Serialize port allocation and preview state changes across conversations."""
    with (common / "traffic-control-preview.lock").open("a+b") as lock_file:
        if os.name == "nt":
            raise TrafficError("Managed previews currently need macOS or Linux.")
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def process_identity(pid: int) -> str | None:
    """A PID and its OS start time identify the server group we created."""
    if pid < 1:
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="], capture_output=True, text=True, check=False)
    except OSError:
        return None
    parts = result.stdout.strip().split()
    if result.returncode or len(parts) < 6 or parts[-1].startswith("Z"):
        return None
    return " ".join(parts[:-1])


def pid_running(pid: int) -> bool:
    """Treat a live PID with an unknown identity as unsafe to replace or stop."""
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False)
    except OSError:
        return True
    if status.returncode:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    return not status.stdout.strip().startswith("Z")


def saved_pid(data: dict[str, Any]) -> int:
    try:
        return int(data.get("pid", 0))
    except (TypeError, ValueError):
        return 0


def preview_paths(common: Path, key: str) -> tuple[Path, Path]:
    folder = common / "traffic-control-previews"
    folder.mkdir(exist_ok=True)
    return folder / f"{key}.json", folder / f"{key}.log"


def read_preview(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported preview state")
        return data
    except (OSError, ValueError) as exc:
        raise TrafficError("The saved preview state cannot be read. Inspect it before starting another preview.") from exc


def save_preview(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix="preview-", delete=False) as out:
        temp_path = Path(out.name)
        json.dump(data, out, indent=2, sort_keys=True)
        out.write("\n")
    os.replace(temp_path, path)


def preview_alive(data: dict[str, Any], task: dict[str, Any]) -> bool:
    try:
        pid = int(data["pid"])
        return (
            data.get("folder") == task["folder"]
            and data.get("branch") == task["branch"]
            and process_identity(pid) == data.get("identity")
            and os.getpgid(pid) == pid
        )
    except (KeyError, ValueError, ProcessLookupError, PermissionError):
        return False


def url_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
            return 200 <= response.status < 500
    except (OSError, URLError):
        return False


def free_port() -> int:
    # Use familiar Vite ports so URLs are easy to recognize. The lock prevents
    # two Traffic Control calls from choosing the same port at once.
    for port in range(5173, 5273):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise TrafficError("No free preview port was found. Stop an old preview and try again.")


def prepare_vite(folder: Path) -> None:
    manifest = folder / "package.json"
    try:
        package = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrafficError("I could not find a readable package.json in this task folder.") from exc
    scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
    dev = scripts.get("dev", "") if isinstance(scripts, dict) else ""
    if not isinstance(dev, str) or not re.fullmatch(r"\s*vite(?:\s+[^;&|]*)?\s*", dev):
        raise TrafficError("Automatic preview currently supports Vite projects with an npm dev script. Use your app's built-in preview for this project.")
    if (folder / "pnpm-lock.yaml").exists() or (folder / "yarn.lock").exists() or (folder / "bun.lockb").exists() or (folder / "bun.lock").exists():
        raise TrafficError("This project uses a package manager other than npm. Use your app's built-in preview for now.")
    if not shutil.which("npm"):
        raise TrafficError("npm is not installed or is not available in this conversation's environment.")
    if not (folder / "node_modules" / ".bin" / "vite").exists():
        install = ["npm", "ci"] if (folder / "package-lock.json").exists() else ["npm", "install", "--no-package-lock"]
        try:
            completed = subprocess.run(install, cwd=folder, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TrafficError(f"Could not install this worktree's dependencies: {exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise TrafficError(f"Could not install this worktree's dependencies: {detail[-1] if detail else 'npm failed'}")


def stop_preview(data: dict[str, Any], task: dict[str, Any]) -> bool:
    if not preview_alive(data, task):
        return False
    pid = int(data["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise TrafficError("I could not stop this preview process. Inspect it before retrying.") from exc
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not pid_running(pid):
            return True
        if not preview_alive(data, task):
            if not pid_running(pid):
                return True
            raise TrafficError("The saved preview process changed while stopping. I kept its record for inspection.")
        time.sleep(0.1)
    if preview_alive(data, task):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise TrafficError("I could not finish stopping this preview process. Inspect it before retrying.") from exc
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.1)
    raise TrafficError("The preview process is still running. I kept its record for inspection.")


def terminate_spawned(process: subprocess.Popen[bytes]) -> None:
    """Clean up a process we just created even if its identity was unreadable."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise TrafficError("I could not stop the new preview process. Inspect running processes before retrying.") from exc
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise TrafficError("The new preview process did not stop. Inspect running processes before retrying.")


def preview(args: argparse.Namespace, root: Path, common: Path) -> dict[str, Any]:
    with locked_registry(common) as registry:
        key, task = get_task(registry, args.task)
        task = dict(task)
    if task.get("state") == "merged" and not (args.stop or args.status):
        raise TrafficError("This task has already been merged. Stop its old preview or start a new task.")
    folder = Path(task["folder"])
    if not (args.stop or args.status):
        folder = verify_task_folder(root, task)
    with preview_lock(common):
        state_path, log_path = preview_paths(common, key)
        state = read_preview(state_path)
        running = state is not None and preview_alive(state, task)
        if state is not None and not running and pid_running(saved_pid(state)):
            raise TrafficError(f"The saved preview process no longer matches this task. I kept its record at {state_path}; inspect it before retrying.")
        if args.status:
            return {"action": "preview_running" if running else "preview_stopped", "title": task["title"], "folder": str(folder), "url": state["url"] if running else None}
        if args.stop:
            if running:
                stop_preview(state, task)
            state_path.unlink(missing_ok=True)
            return {"action": "preview_stopped", "title": task["title"], "folder": str(folder), "url": None}
        if running:
            return {"action": "preview_running", "title": task["title"], "folder": str(folder), "url": state["url"]}
        # A stale state is only replaced after preparation succeeds. Never signal
        # a process if its stored PID identity no longer matches.
        prepare_vite(folder)
        port = free_port()
        command = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"]
        try:
            with log_path.open("ab") as log:
                log.write(f"\nTraffic Control preview for {task['title']} on port {port}\n".encode())
                log.flush()
                process = subprocess.Popen(command, cwd=folder, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise TrafficError(f"Could not start the preview server: {exc}") from exc
        identity = process_identity(process.pid)
        if not identity:
            terminate_spawned(process)
            raise TrafficError("The preview server exited immediately. Check its dev script and try again.")
        state = {"version": 1, "pid": process.pid, "identity": identity, "folder": str(folder), "branch": task["branch"], "port": port, "url": f"http://127.0.0.1:{port}/"}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if url_ready(port):
                save_preview(state_path, state)
                return {"action": "preview_started", "title": task["title"], "folder": str(folder), "url": state["url"]}
            time.sleep(0.2)
        terminate_spawned(process)
        raise TrafficError(f"The preview server did not become ready. Its log is at {log_path}.")


def render(result: dict[str, Any]) -> str:
    if result["action"] == "status":
        if not result["tasks"]:
            return "Traffic Control has no recorded tasks in this project."
        return "\n\n".join(render({"action": "listed", **task}) for task in result["tasks"])
    if result["action"].startswith("preview_"):
        if result["action"] == "preview_stopped":
            return f"Preview for {result['title']} is stopped."
        return f"Preview for {result['title']}: {result['url']}\nWorktree: {result['folder']}"
    lines = [f"{result['title']} — {result.get('state', 'open')}"]
    if result["action"] in {"created", "adopted", "reused", "resumed", "listed"}:
        lines.append(f"Folder: {result['folder']}")
        lines.append(f"Branch: {result['branch']}")
        lines.append(f"Suggested chat name: {result['chat_title']}")
        if result["action"] == "created":
            lines.append("Open a new coding conversation in this folder. This conversation stays in its current folder.")
        elif result["action"] == "adopted":
            lines.append("This conversation is already in the task folder.")
        elif result["action"] == "reused":
            lines.append("This task already exists. Continue in its folder instead of creating another copy.")
    elif result["action"] == "handoff_saved":
        lines.append("Handoff note saved for the next conversation.")
    elif result["action"] == "ready_to_merge":
        lines.append(f"Ready to merge into {result['target']} after project checks pass. No files were changed.")
    elif result["action"] == "merged":
        lines.append(f"Merged locally into {result['target']}. The task folder remains available.")
    elif result["action"] == "reconciled":
        lines.append(f"These changes were already on {result['target']}; no new merge commit was made.")
    elif result["action"].startswith("already_merged"):
        lines.append("These changes are already on the destination branch.")
    if result.get("handoff"):
        lines.append(f"Handoff: {result['handoff']}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage one feature's worktree, handoff, and safe local merge.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Folder inside the Git project")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    start_parser = sub.add_parser("start", help="Create or reuse a feature workspace")
    start_parser.add_argument("title", help="Plain-English feature name")
    start_parser.add_argument("--agent", choices=["codex", "claude", "other"], help="Agent running this task")
    start_parser.add_argument("--base", help="Starting branch")
    start_parser.add_argument("--parallel", action="store_true", help="Create another attempt at an existing feature")
    start_parser.add_argument("--no-fetch", action="store_true", help="Use local branch information without fetching")

    resume_parser = sub.add_parser("resume", help="Find an existing feature workspace")
    resume_parser.add_argument("task", help="Feature name or task ID")

    sub.add_parser("status", help="List known feature workspaces")

    handoff_parser = sub.add_parser("handoff", help="Save a short note for the next conversation")
    handoff_parser.add_argument("task", help="Feature name or task ID")
    handoff_parser.add_argument("--note", required=True, help="What is done and what remains")

    merge_parser = sub.add_parser("merge", help="Check or apply a local merge")
    merge_parser.add_argument("task", help="Feature name or task ID")
    merge_parser.add_argument("--apply", action="store_true", help="Perform the merge after checks")

    preview_parser = sub.add_parser("preview", help="Start, check, or stop a task's local Vite dev server")
    preview_parser.add_argument("task", help="Feature name or task ID")
    preview_mode = preview_parser.add_mutually_exclusive_group()
    preview_mode.add_argument("--status", action="store_true", help="Check whether the preview is running")
    preview_mode.add_argument("--stop", action="store_true", help="Stop this task's preview")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        root = repo_root(args.repo.resolve())
        common = common_git_dir(root)
        handlers = {"start": start, "resume": resume, "status": status, "handoff": handoff, "merge": merge, "preview": preview}
        result = handlers[args.command](args, root, common)
    except TrafficError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Traffic Control: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
