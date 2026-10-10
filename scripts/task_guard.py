#!/usr/bin/env python
"""Hooks Cursor : permissions ciblées sur la tâche autonome active."""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from task_verdict import (
    GuardDecision,
    ShellMode,
    TaskState,
    blocked_read_reason,
    default_autonomy_home,
    default_tasks_root,
    load_active_task,
    load_task,
    locked_state,
    matches_any,
    path_is_under,
    preexisting_test_path,
    reserve_tool_call,
    resolve_under_root,
    shell_command_allowed,
    validate_task_context,
)

KNOWN_EVENTS = frozenset({"beforeShellExecution", "preToolUse", "subagentStart"})
KNOWN_TOOLS = frozenset({"Shell", "Read", "Write", "Delete", "Grep", "Task"})


def hook_allow(message: str = "") -> dict[str, str]:
    payload: dict[str, str] = {"permission": "allow"}
    if message:
        payload["agent_message"] = message
    return payload


def hook_deny(reason: str) -> dict[str, str]:
    return {
        "permission": "deny",
        "user_message": reason,
        "agent_message": reason,
    }


def _workspace_targets_task(roots: list[Path], tasks_root: Path) -> bool:
    try:
        tasks_resolved = tasks_root.resolve()
    except OSError:
        return False
    return any(path_is_under(tasks_resolved, root) or root == tasks_resolved for root in roots)


def task_for_payload(
    payload: dict[str, Any],
    autonomy_home: Path,
) -> tuple[str, TaskState] | str | None:
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not all(isinstance(item, str) and item for item in roots):
        return None
    resolved: list[Path] = []
    for item in roots:
        try:
            resolved.append(Path(item).resolve())
        except OSError:
            return None
    try:
        active = load_active_task(autonomy_home)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        if _workspace_targets_task(resolved, default_tasks_root()):
            return "état de tâche illisible"
        return None
    if active is None:
        return None
    task_id, state = active
    try:
        worktree = Path(state.worktree_path).resolve()
    except OSError:
        return "worktree illisible"
    linked = any(
        root == worktree or path_is_under(worktree, root) or path_is_under(root, worktree)
        for root in resolved
    )
    if linked:
        return task_id, state
    return None


def _cwd_denied(payload: dict[str, Any], worktree: Path) -> str | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return "cwd manquant"
    if not path_is_under(worktree, Path(cwd)):
        return "répertoire de travail hors du worktree de la tâche"
    return None


def _relative_target(worktree: Path, path_str: str) -> str | None:
    if Path(path_str).is_absolute():
        candidate = Path(path_str)
        if not path_is_under(worktree, candidate):
            return None
        return str(candidate.resolve().relative_to(worktree.resolve()))
    resolved = resolve_under_root(worktree, path_str)
    if resolved is None:
        return None
    return str(resolved.relative_to(worktree.resolve()))


def _write_decision(path_str: str, state: TaskState) -> GuardDecision:
    worktree = Path(state.worktree_path)
    relative = _relative_target(worktree, path_str)
    if relative is None:
        return GuardDecision(False, "écriture hors worktree de la tâche")
    if ".git" in PurePosixPath(relative).parts:
        return GuardDecision(False, f"métadonnées git interdites : {relative}")
    try:
        if preexisting_test_path(worktree, state.base_sha, relative):
            return GuardDecision(False, f"revue humaine : test existant modifié : {relative}")
    except RuntimeError as exc:
        return GuardDecision(False, str(exc))
    task = load_task(Path(state.task_path))
    if matches_any(relative, task.all_forbidden()):
        return GuardDecision(False, f"écriture interdite : {relative}")
    if not matches_any(relative, task.allowed_write):
        return GuardDecision(False, f"écriture hors périmètre : {relative}")
    return GuardDecision(True)


def _read_decision(path_str: str, state: TaskState) -> GuardDecision:
    reason = blocked_read_reason(Path(state.worktree_path), path_str)
    if reason:
        return GuardDecision(False, reason)
    return GuardDecision(True)


def _shell_mode(status: str) -> ShellMode:
    if status in {"stopped", "failed"}:
        return "stopped"
    if status == "ready_for_review":
        return "review"
    return "active"


def _command_decision(command: str, state: TaskState) -> GuardDecision:
    proof: list[str] = []
    if state.status == "active":
        try:
            proof = load_task(Path(state.task_path)).proof
        except (OSError, ValueError):
            return GuardDecision(False, "fiche illisible")
    allowed = shell_command_allowed(
        command,
        Path(state.trust_root),
        task_id=state.task_id,
        worktree=Path(state.worktree_path),
        mode=_shell_mode(state.status),
        proof=proof,
    )
    if allowed:
        return GuardDecision(True)
    if state.status == "stopped":
        return GuardDecision(False, "tâche arrêtée : seuls status, report et note sont autorisés")
    if state.status == "ready_for_review":
        return GuardDecision(False, "revue en cours : modification refusée sans invalidation")
    return GuardDecision(False, "commande shell non autorisée pour la tâche active")


def _tool_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return tool_input


def _working_directory_denied(tool_input: dict[str, Any], worktree: Path) -> str | None:
    raw = tool_input.get("working_directory")
    if raw is None:
        return None
    if not isinstance(raw, str) or not path_is_under(worktree, Path(raw)):
        return "working_directory hors du worktree de la tâche"
    return None


def _decide_tool(payload: dict[str, Any], state: TaskState) -> GuardDecision:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in KNOWN_TOOLS:
        return GuardDecision(False, "outil non documenté refusé")
    if tool_name.startswith("MCP:"):
        return GuardDecision(False, "outil MCP refusé")
    tool_input = _tool_input(payload)
    if tool_input is None:
        return GuardDecision(False, "tool_input manquant")
    worktree = Path(state.worktree_path)
    directory_error = _working_directory_denied(tool_input, worktree)
    if directory_error:
        return GuardDecision(False, directory_error)
    if tool_name == "Shell":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return GuardDecision(False, "commande manquante")
        return _command_decision(command, state)
    if tool_name in {"Write", "Delete"}:
        if state.status in {"stopped", "failed", "ready_for_review"}:
            return GuardDecision(False, "écriture refusée dans l'état actuel")
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return GuardDecision(False, "file_path manquant")
        return _write_decision(file_path, state)
    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return GuardDecision(False, "file_path manquant")
        return _read_decision(file_path, state)
    if tool_name == "Grep":
        raw_path = tool_input.get("path") or tool_input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return GuardDecision(False, "grep hors worktree")
        reason = blocked_read_reason(worktree, raw_path)
        if reason:
            return GuardDecision(False, reason)
        return GuardDecision(True)
    task = load_task(Path(state.task_path))
    if task.allow_subagents:
        return GuardDecision(True)
    return GuardDecision(False, "sous-agent interdit pour cette tâche")


def _workspace_covers(payload: dict[str, Any], worktree: Path) -> bool:
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not all(isinstance(item, str) and item for item in roots):
        return False
    resolved: list[Path] = []
    for item in roots:
        try:
            resolved.append(Path(item).resolve())
        except OSError:
            return False
    try:
        root = worktree.resolve()
    except OSError:
        return False
    return any(
        candidate == root or path_is_under(root, candidate) or path_is_under(candidate, root)
        for candidate in resolved
    )


def _consume(
    autonomy_home: Path,
    state: TaskState,
    payload: dict[str, Any],
    command: str | None,
) -> str | None:
    if state.status != "active":
        return None
    tool_use_id = payload.get("tool_use_id")
    if tool_use_id is not None and not isinstance(tool_use_id, str):
        return "tool_use_id invalide"
    return reserve_tool_call(
        autonomy_home,
        state.task_id,
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
        command=command,
    )


def _bind(autonomy_home: Path, task_id: str, conversation_id: str) -> str | None:
    with locked_state(autonomy_home, task_id) as state:
        if state.conversation_id is None:
            state.conversation_id = conversation_id
            return None
        if state.conversation_id != conversation_id:
            return "autre session refusée pour cette tâche"
    return None


def _linked_decision(
    payload: dict[str, Any],
    autonomy_home: Path,
    task_id: str,
    state: TaskState,
) -> dict[str, str]:
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in KNOWN_EVENTS:
        return hook_deny("événement de hook inconnu ou non documenté")
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return hook_deny("conversation_id manquant")
    if state.conversation_id != conversation_id:
        bind_error = _bind(autonomy_home, task_id, conversation_id)
        if bind_error:
            return hook_deny(bind_error)
        loaded = load_active_task(autonomy_home)
        if loaded is None:
            return hook_deny("tâche active introuvable")
        _task_id, state = loaded
    context_error = validate_task_context(autonomy_home, state)
    if context_error:
        return hook_deny(context_error)
    worktree = Path(state.worktree_path)
    cwd_error = _cwd_denied(payload, worktree)
    if cwd_error:
        return hook_deny(cwd_error)
    if event == "beforeShellExecution":
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return hook_deny("commande manquante")
        decision = _command_decision(command, state)
        if not decision.allowed:
            return hook_deny(decision.reason)
        consumed = _consume(autonomy_home, state, payload, command)
        return hook_allow() if consumed is None else hook_deny(consumed)
    if event == "subagentStart":
        parent = payload.get("parent_conversation_id")
        if parent != state.conversation_id:
            return hook_deny("sous-agent hors de la session liée")
        task = load_task(Path(state.task_path))
        if state.status != "active":
            return hook_deny("sous-agent refusé dans l'état actuel")
        if not task.allow_subagents:
            return hook_deny("sous-agent interdit pour cette tâche")
        return hook_allow()
    decision = _decide_tool(payload, state)
    if not decision.allowed:
        return hook_deny(decision.reason)
    command = None
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") == "Shell" and isinstance(tool_input, dict):
        raw_command = tool_input.get("command")
        command = raw_command if isinstance(raw_command, str) else None
    consumed = _consume(autonomy_home, state, payload, command)
    return hook_allow() if consumed is None else hook_deny(consumed)


def handle_hook(payload: dict[str, Any], autonomy_home: Path) -> dict[str, str]:
    conversation_id = payload.get("conversation_id")
    try:
        active = load_active_task(autonomy_home)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        active = None
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and all(isinstance(item, str) for item in roots):
            resolved = [Path(item) for item in roots]
            if _workspace_targets_task(resolved, default_tasks_root()):
                return hook_deny("état de tâche illisible")
    if (
        isinstance(conversation_id, str)
        and conversation_id
        and active is not None
        and active[1].conversation_id == conversation_id
    ):
        task_id, state = active
        if not _workspace_covers(payload, Path(state.worktree_path)):
            return hook_deny("racines de workspace absentes ou modifiées")
        return _linked_decision(payload, autonomy_home, task_id, state)
    matched = task_for_payload(payload, autonomy_home)
    if matched is None:
        return hook_allow()
    if isinstance(matched, str):
        return hook_deny(matched)
    task_id, state = matched
    return _linked_decision(payload, autonomy_home, task_id, state)


def main() -> int:
    raw = sys.stdin.read()
    try:
        parsed: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.stdout.write(json.dumps(hook_deny("JSON invalide")))
        return 0
    if not isinstance(parsed, dict):
        sys.stdout.write(json.dumps(hook_deny("payload invalide")))
        return 0
    result = handle_hook(parsed, default_autonomy_home())
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
