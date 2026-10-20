"""Preuves d'absence des points d'entrée de la maintenance retirée."""

import importlib
from pathlib import Path

import pytest

import app.bot.handlers as bot_handlers
from app.bot.bot_instance import BOT_COMMANDS, dp

ROOT = Path(__file__).resolve().parents[2]

REMOVED_MODULES = (
    "app.services.alert_fix",
    "app.services.alert_chain_state",
    "app.bot.handlers.alert_chain_approval",
)

REMOVED_PATHS = (
    "app/services/alert_fix",
    "app/services/alert_chain_state.py",
    "app/bot/handlers/alert_chain_approval.py",
    "scripts/check_alert_chain.py",
    "scripts/alert_fix.py",
    "scripts/alert_fix_rehearsal.py",
    "tests/alert_fix_fixtures.py",
    "tests/unit/test_alert_chain_approval.py",
    "tests/unit/test_alert_chain_state.py",
    "tests/unit/test_alert_fix_prepare.py",
    "tests/unit/test_check_alert_chain.py",
    "tests/unit/test_check_alert_chain_state_integration.py",
    "tests/unit/test_maintenance_foundation.py",
    "docs/operations/alert-chain-check.md",
    "docs/operations/alert-fix-executor.md",
)


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_modules_cannot_be_imported(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_removed_paths_are_absent():
    remaining = [path for path in REMOVED_PATHS if (ROOT / path).exists()]
    assert remaining == []


def test_router_has_no_maintenance_handlers():
    def routers(current):
        yield current
        for child in current.sub_routers:
            yield from routers(child)

    offenders = [
        handler.callback.__module__
        for current in routers(dp)
        for handler in current.callback_query.handlers
        if "alert_chain" in (handler.callback.__module__ or "")
    ]
    assert offenders == []
    assert "alert_chain_approval" not in bot_handlers.__all__
    assert not hasattr(bot_handlers, "alert_chain_approval")


def test_bot_commands_have_no_maintenance():
    assert all("maintenance" not in command.command for command in BOT_COMMANDS)


def test_compose_and_dockerfile_have_no_maintenance_state():
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "alert_chain_state" not in compose
    assert "/var/lib/botradar" not in dockerfile
