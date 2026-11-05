"""The script entry point must not drift from the project dependencies."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_script_dependencies_match_the_project():
    script = (ROOT / "skills/book-translate/bt.py").read_text(encoding="utf-8")
    block = re.search(r"# /// script\n(.*?)# ///", script, re.S).group(1)
    inline = tomllib.loads("".join(line[2:] + "\n" for line in block.splitlines()))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert sorted(inline["dependencies"]) == sorted(project["dependencies"])
