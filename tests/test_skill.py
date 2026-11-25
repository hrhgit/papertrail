import json
import re
import shlex
import subprocess
from pathlib import Path

from papertrail.cli import main, skill_text
from papertrail.graph import build
from papertrail.sources import ClaudeCodeSource

FIXTURE = Path(__file__).parent / "fixtures"
GREETING = (FIXTURE / "project/greeting.py").resolve()


def test_skill_installs_per_agent(tmp_path, monkeypatch, capsys):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert main(["skill", "--project", str(project)]) == 0
    local = project / ".claude/skills/papertrail/SKILL.md"
    assert local.is_file() and not (home / ".claude").exists()
    text = local.read_text()
    assert text.startswith("---\nname: papertrail\ndescription: ")
    assert "{cmd}" not in text and "{store}" not in text and "--store" not in text

    capsys.readouterr()
    assert main(["skill", "--agent", "all", "--global", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [r["agent"] for r in rows] == ["claude-code", "codex"]
    assert [r["path"] for r in rows] == [
        str(home / ".claude/skills/papertrail/SKILL.md"),
        str(home / ".codex/skills/papertrail/SKILL.md"),
    ]
    assert all(Path(r["path"]).read_text() == text for r in rows)

    # Codex has no project folder for skills, so it always goes to home.
    assert main(["skill", "--agent", "codex", "--project", str(project)]) == 0
    assert not (project / ".codex").exists()


def test_skill_print_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert main(["skill", "--print", "--store", "some where/g.rl"]) == 0
    out = capsys.readouterr().out
    assert f"--store {shlex.quote(str(tmp_path.resolve() / 'some where/g.rl'))}" in out
    assert list(tmp_path.iterdir()) == []
    assert main(["skill", "--project", str(tmp_path / "absent")]) == 1


def test_skill_commands_run(tmp_path):
    """Every query the skill teaches works as written against the demo graph."""
    store = tmp_path / "demo.rl"
    build(store, list(ClaudeCodeSource().sessions(FIXTURE / "logs")))
    text = skill_text(store)
    blocks = re.findall(r"```sh\n(.+?)\n```", text, re.S)
    assert len(blocks) == 5
    values = {"FILE:LINE": f"{GREETING}:2", "FILE": str(GREETING), "START,END": "1,3", "ID": "birch"}
    ran = 0
    for line in blocks:
        if " ingest " in line:
            continue
        for name, value in values.items():
            line = re.sub(rf"(?<= ){re.escape(name)}(?= )", shlex.quote(value), line)
        done = subprocess.run(line, shell=True, capture_output=True, text=True, cwd=tmp_path)
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        assert result["rows"] and "truncated" in result
        ran += 1
    assert ran == 4
