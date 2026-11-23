# Traffic Control

Traffic Control keeps parallel Codex and Claude Code tasks in separate folders and helps you bring finished work together. You name a feature in plain English; the skill finds or creates its worktree and branch.

## Why I made this

I found myself opening multiple coding chats in Claude Code and Codex for the same project... sometimes with overlapping code (bad practice, I know). Once several were going, it was hard to remember where each set of changes lived and how to bring them back together. Traffic Control gives each task a name and its own place to work, then helps you merge it when ready.

## Use it

In Claude Code, say:

> `/traffic-control start "Add a grammar quiz"`

In Codex, say:

> `$traffic-control start "Add a grammar quiz"`


The skill tells you which folder to open for the coding conversation and suggests the same name for the chat. Ask it to **resume** a feature when opening another conversation about it. It reuses the same folder for sequential work. Ask it to **start a parallel attempt** only when two agents will edit at the same time or try different solutions.

When you are happy with a feature, say:

> `$traffic-control merge "Add a grammar quiz"`

It reviews the changes, runs the project's checks, and checks for merge problems before merging locally. It leaves the task folder in place. If your project uses GitHub pull requests, it follows that process instead.

Traffic Control does not require a coordinator chat or a dashboard. It does not automatically read the contents of other conversations. Use a handoff note when moving work from one conversation to another.

## Install

With a recent GitHub CLI that supports `gh skill install`:

```sh
gh skill install serenatsay/traffic-control traffic-control --agent codex --scope user
gh skill install serenatsay/traffic-control traffic-control --agent claude-code --scope user
```

For a manual install, download or clone this repository, then copy the entire `skills/traffic-control` folder to:

| Agent | Personal skills folder |
| --- | --- |
| Codex | `~/.agents/skills/traffic-control` |
| Claude Code | `~/.claude/skills/traffic-control` |

The skill needs Git 2.38 or newer and Python 3.10 or newer. Git 2.38 added the conflict preview used before a local merge. No Python packages are required.

## What happens behind the scenes

- `start` uses the project's current linked worktree when there is one. Otherwise it creates a branch such as `traffic/add-a-grammar-quiz` and a sibling folder such as `perrito-traffic-control/add-a-grammar-quiz`. It fetches `origin` when available and starts from the default branch.
- Repeating `start` with the same feature name returns its existing folder. An explicit parallel attempt gets another branch and folder.
- A small task list lives in the repository's Git metadata. It is shared by the repository's local worktrees and is not added to your source files.
- `merge` first checks that the feature and destination have no uncommitted changes, the destination is current with its last fetched remote state, and Git sees no text conflicts. The read-only preview does not contact the network. `merge --apply` fetches `origin` and repeats those checks immediately before changing the destination. The skill runs your project's relevant checks before it applies a local merge.
- If the work was already merged outside Traffic Control, including a squash merge with the same resulting files, `merge --apply` marks the task complete without making an empty merge commit.
- Traffic Control does not push, remove worktrees, rename other conversations, or overwrite uncommitted files.

The helper can also be used directly from a terminal:

```sh
python3 skills/traffic-control/scripts/traffic_control.py start "Add a grammar quiz"
python3 skills/traffic-control/scripts/traffic_control.py status
python3 skills/traffic-control/scripts/traffic_control.py resume "Add a grammar quiz"
python3 skills/traffic-control/scripts/traffic_control.py merge "Add a grammar quiz"
```

Run those commands from inside the project you want to work on. `merge` without `--apply` only checks readiness.

## Limits

Traffic Control manages **local Git projects**. If a task was started in a cloud workspace or another computer, its local worktree will not appear here. The skill cannot move an already open Codex or Claude conversation into a new folder; open a new conversation in the folder it returns. App-specific chat renaming is best effort.

## Development

Run the isolated Git tests with `python3 -m unittest discover -s tests -v` and validate the skill with Codex's `quick_validate.py`.

Licensed under MIT. Contributions and bug reports are welcome.
