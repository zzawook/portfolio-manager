import shutil
from pathlib import Path

from rebalancer.cli import main

ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    return tmp_path / "config.yaml"


def test_check_and_plan_run(tmp_path, capsys):
    cfg = project_copy(tmp_path)
    assert main(["-c", str(cfg), "check"]) == 0
    assert main(["-c", str(cfg), "plan", "--today", "2027-01-05"]) == 0
    out = capsys.readouterr().out
    assert "드라이런" in out and "VWRA" in out


def test_mock_execute_uses_separate_state_file(tmp_path, monkeypatch):
    cfg = project_copy(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert main(["-c", str(cfg), "execute", "--today", "2027-01-05"]) == 0
    assert (tmp_path / "state.mock.json").exists()
    assert not (tmp_path / "state.json").exists()


def test_bad_config_returns_error_code(tmp_path, capsys):
    cfg = project_copy(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("target: 40", "target: 41"), encoding="utf-8")
    assert main(["-c", str(cfg), "check"]) == 1
    assert "100%" in capsys.readouterr().err
