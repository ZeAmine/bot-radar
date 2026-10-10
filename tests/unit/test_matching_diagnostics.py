"""Contrats hors ligne : étiquettes indépendantes et rapports sans contenu d'annonce."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


def tool():
    assert importlib.util.find_spec("scripts.matching_diagnostics") is not None
    return importlib.import_module("scripts.matching_diagnostics")


def pair():
    return {
        "alert": {
            "name": "secret_synthetic",
            "category_slug": "bd_comics",
            "include_keywords": ["blacksad"],
            "exclude_keywords": ["secret_synthetic"],
        },
        "listing": {
            "title": "blacksad secret_synthetic",
            "description": "private_synthetic",
            "category_slug": "bd_comics",
            "item_price_cents": 1000,
            "url": "https://synthetic.invalid/secret_synthetic",
        },
    }


def test_report_is_redacted_and_matcher_called_once(monkeypatch):
    module = tool()
    original = module.evaluate_listing_against_alert
    calls = []

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module, "evaluate_listing_against_alert", counted)
    report = module.explain_pair(module.Pair.model_validate(pair()))
    rendered = json.dumps(report)
    assert report["mode"] == "rejeu"
    assert report["reason_code"] == "user_exclusion"
    assert len(calls) == 1
    for sensitive in ["secret_synthetic", "private_synthetic", "https://", "blacksad"]:
        assert sensitive not in rendered
    assert "reason" not in report


def test_labels_do_not_follow_matcher_output_and_sources_are_separate():
    module = tool()
    accepted = pair()
    accepted["alert"]["exclude_keywords"] = []
    cases = [
        {
            "pair": accepted,
            "origin": "synthetic",
            "annotation": {"expected": "rejeter", "source": "business_test"},
        },
        {
            "pair": pair(),
            "origin": "synthetic",
            "annotation": {"expected": "accepter", "source": "human_validated", "reviewed": True},
        },
        {
            "pair": accepted,
            "origin": "synthetic",
            "annotation": {"expected": "indetermine", "source": "business_test"},
        },
        {"pair": accepted, "origin": "synthetic"},
    ]
    report = module.evaluate_corpus(module.Corpus.model_validate({"version": 1, "cases": cases}))
    assert report["population"] == 4
    assert report["unlabeled"] == 1
    assert report["groups"]["business_test"]["indeterminate"] == 1
    assert report["groups"]["business_test"]["matrix"]["rejeter"]["accepter"] == 1
    assert report["groups"]["human_validated"]["matrix"]["accepter"]["rejeter"] == 1
    assert [d["case_index"] for d in report["disagreements"]] == [0, 1]
    assert report["groups"]["human_validated"]["sample_precision"]["value"] is None
    assert report["groups"]["human_validated"]["sample_precision"]["denominator"] == 0
    assert report["groups"]["human_validated"]["sample_recall"]["denominator"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"annotation": {"expected": "accepter", "source": "none"}},
        {"annotation": {"expected": "accepter", "source": "human_validated"}},
        {"annotation": {"expected": "maybe", "source": "business_test"}},
        {"origin": "scraped"},
        {"unknown": "secret_synthetic"},
    ],
)
def test_invalid_corpus_fails_closed(change):
    module = tool()
    with pytest.raises(ValidationError):
        module.Corpus.model_validate(
            {
                "version": 1,
                "cases": [
                    {
                        "pair": pair(),
                        "origin": "synthetic",
                        **change,
                    }
                ],
            }
        )


def test_empty_corpus_has_explicit_undefined_measures():
    module = tool()
    r = module.evaluate_corpus(module.Corpus.model_validate({"version": 1, "cases": []}))
    assert r["population"] == 0
    assert r["groups"]["human_validated"]["sample_precision"] == {
        "value": None,
        "numerator": 0,
        "denominator": 0,
    }


def test_cli_does_not_load_env_or_echo_validation_errors(tmp_path):
    tool()
    script = Path(__file__).resolve().parents[2] / "scripts/matching_diagnostics.py"
    fd = os.open(tmp_path / ".env", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"SECRET=secret_synthetic\n")
    finally:
        os.close(fd)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"unexpected": "secret_synthetic"}))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(script), "explain", str(invalid)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "secret_synthetic" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["error"] == "invalid_input"


def test_cli_runs_without_network_settings_and_reports_replay(tmp_path):
    tool()
    script = Path(__file__).resolve().parents[2] / "scripts/matching_diagnostics.py"
    case = tmp_path / "pair.json"
    case.write_text(json.dumps(pair()))
    (tmp_path / "sitecustomize.py").write_text(
        "import builtins, socket\n"
        "original = builtins.__import__\n"
        "def checked(name, *args, **kwargs):\n"
        "    if name in ('app.core.config', 'app.db.session', 'dotenv'):\n"
        "        raise RuntimeError('settings_forbidden')\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = checked\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise RuntimeError('network_forbidden')\n"
        "socket.socket.connect = forbidden\n"
        "socket.getaddrinfo = forbidden\n"
    )
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT")}
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(script), "explain", str(case)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "rejeu"


def test_input_and_output_are_bounded(tmp_path, capsys):
    module = tool()
    with pytest.raises(ValidationError):
        module.Corpus.model_validate(
            {"version": 1, "cases": [{"pair": pair(), "origin": "synthetic"} for _ in range(201)]}
        )
    invalid = tmp_path / "large.json"
    invalid.write_bytes(b" " * (1_048_576 + 1))
    assert module.main(["explain", str(invalid)]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_input"}


def test_versioned_synthetic_examples_have_no_human_labels():
    module = tool()
    path = Path(__file__).resolve().parents[2] / "docs/evaluations/matching-synthetic-v1.json"
    corpus = module.Corpus.model_validate_json(path.read_text())
    assert all(c.origin == "synthetic" for c in corpus.cases)
    assert all(not c.annotation.reviewed for c in corpus.cases)
    r = module.evaluate_corpus(corpus)
    assert r["population"] == 10
    assert r["groups"]["human_validated"]["population"] == 0
    assert r["groups"]["business_test"]["binary_labeled"] == 7
    assert r["groups"]["business_test"]["indeterminate"] == 1
    assert r["unlabeled"] == 2
