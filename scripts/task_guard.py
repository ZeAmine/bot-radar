#!/usr/bin/env python
"""Hooks Cursor : permissions ciblées sur la tâche autonome active."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from task_verdict import (
    AGENT_BLOCKED_SUBCOMMANDS,
    GuardDecision,
    default_autonomy_home,
    load_active_task,
    matches_any,
    resolve_under_root,
    shell_command_allowed,
    task_guard_increment_tool_call,
)

READ_FORBIDDEN_RE = re.compile(r"(^|/)\.env(\.|$)|/\.ssh/")


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


def parse_hook_payload(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return data


def extract_shell_command(payload: dict[str, Any]) -> str:
    for key in ("command", "shellCommand", "cmd"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def extract_tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name", "tool"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def extract_file_path(payload: dict[str, Any]) -> str:
    for key in ("path", "file_path", "filePath", "target"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    inner = payload.get("input") or payload.get("arguments") or {}
    if isinstance(inner, dict):
        for key in ("path", "file_path", "filePath", "target"):
            value = inner.get(key)
            if isinstance(value, str):
                return value
    return ""


def task_verdict_subcommand(command: str) -> str | None:
    match = re.search(r"task_verdict\.py\s+(\w+)", command)
    if match:
        return match.group(1)
    match = re.search(r"task_verdict\s+(\w+)", command)
    if match:
        return match.group(1)
    return None


def decide_shell_for_active_task(command: str, autonomy_home: Path) -> GuardDecision:
    active = load_active_task(autonomy_home)
    if active is None:
        return GuardDecision(True)
    _task_id, state = active
    if state.status == "stopped":
        allowed = re.search(r"task_verdict\.py\s+(status|report|note)\b", command)
        if not allowed:
            return GuardDecision(
                False,
                "tâche arrêtée : seuls status, report et note sont autorisés",
            )
    sub = task_verdict_subcommand(command)
    if sub in AGENT_BLOCKED_SUBCOMMANDS:
        return GuardDecision(False, f"sous-commande réservée à l'humain : {sub}")
    if not shell_command_allowed(command, Path(state.trust_root)):
        return GuardDecision(False, "commande shell non autorisée pour la tâche active")
    task_guard_increment_tool_call(autonomy_home, state.task_id)
    return GuardDecision(True)


def decide_write_for_active_task(path_str: str, autonomy_home: Path) -> GuardDecision:
    active = load_active_task(autonomy_home)
    if active is None:
        return GuardDecision(True)
    task_id, state = active
    if state.status == "stopped":
        return GuardDecision(False, "tâche arrêtée : écriture refusée")
    worktree = Path(state.worktree_path)
    relative = path_str
    if Path(path_str).is_absolute():
        try:
            relative = str(Path(path_str).resolve().relative_to(worktree.resolve()))
        except ValueError:
            return GuardDecision(False, "écriture hors worktree de la tâche")
    else:
        resolved = resolve_under_root(worktree, relative)
        if resolved is None:
            return GuardDecision(False, "chemin relatif ou lien symbolique hors worktree")
        relative = str(resolved.relative_to(worktree.resolve()))

    from task_verdict import load_task

    task = load_task(Path(state.task_path))
    if matches_any(relative, task.all_forbidden()):
        return GuardDecision(False, f"écriture interdite : {relative}")
    if not matches_any(relative, task.allowed_write):
        return GuardDecision(False, f"écriture hors périmètre : {relative}")
    task_guard_increment_tool_call(autonomy_home, task_id)
    return GuardDecision(True)


def decide_read_for_active_task(path_str: str) -> GuardDecision:
    if READ_FORBIDDEN_RE.search(path_str.replace("\\", "/")):
        return GuardDecision(False, "lecture de secrets interdite")
    return GuardDecision(True)


def decide_subagent_for_active_task(autonomy_home: Path) -> GuardDecision:
    active = load_active_task(autonomy_home)
    if active is None:
        return GuardDecision(True)
    _task_id, state = active
    from task_verdict import load_task

    task = load_task(Path(state.task_path))
    if task.allow_subagents:
        return GuardDecision(True)
    return GuardDecision(False, "sous-agent interdit pour cette tâche")


def handle_hook(event: str, payload: dict[str, Any], autonomy_home: Path) -> dict[str, str]:
    if event in {"beforeShellExecution", "before_shell_execution"}:
        command = extract_shell_command(payload)
        if not command:
            return hook_allow()
        decision = decide_shell_for_active_task(command, autonomy_home)
        return hook_allow() if decision.allowed else hook_deny(decision.reason)

    if event in {"preToolUse", "pre_tool_use"}:
        tool = extract_tool_name(payload)
        if tool in {"Task", "task", "subagent", "Subagent"}:
            decision = decide_subagent_for_active_task(autonomy_home)
            return hook_allow() if decision.allowed else hook_deny(decision.reason)
        if tool in {"Write", "write", "StrReplace", "str_replace", "ApplyPatch", "apply_patch"}:
            path_str = extract_file_path(payload)
            if not path_str:
                return hook_allow()
            decision = decide_write_for_active_task(path_str, autonomy_home)
            return hook_allow() if decision.allowed else hook_deny(decision.reason)
        if tool in {"Read", "read"}:
            path_str = extract_file_path(payload)
            if not path_str:
                return hook_allow()
            decision = decide_read_for_active_task(path_str)
            return hook_allow() if decision.allowed else hook_deny(decision.reason)
        if tool in {"Shell", "shell"}:
            command = extract_shell_command(payload)
            if not command:
                return hook_allow()
            decision = decide_shell_for_active_task(command, autonomy_home)
            return hook_allow() if decision.allowed else hook_deny(decision.reason)
        return hook_allow()

    if event in {"subagentStart", "subagent_start"}:
        decision = decide_subagent_for_active_task(autonomy_home)
        return hook_allow() if decision.allowed else hook_deny(decision.reason)

    return hook_allow()


def main() -> int:
    raw = sys.stdin.read()
    payload = parse_hook_payload(raw)
    event = str(payload.get("event") or payload.get("hook_event") or payload.get("type") or "")
    if not event:
        event = str(payload.get("hook") or "beforeShellExecution")
    autonomy_home = default_autonomy_home()
    result = handle_hook(event, payload, autonomy_home)
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
