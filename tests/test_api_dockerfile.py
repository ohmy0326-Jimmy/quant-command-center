"""確認 API 映像檔有複製 API 會用到的所有本地模組（含函式內延遲匯入）。

漏掉時 /v1/health 仍正常，但 /v1/portfolio 與 /v1/ai-advice 會因 ModuleNotFoundError 回 502。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_MODULES = ["api/main.py", "api/schemas.py", "api/auth.py", "api/advice_cache.py"]


def _local_root_modules() -> set[str]:
    return {p.stem for p in ROOT.glob("*.py")}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def required_root_modules() -> set[str]:
    local = _local_root_modules()
    seen: set[str] = set()
    queue = [ROOT / rel for rel in ENTRY_MODULES]
    while queue:
        path = queue.pop()
        for name in _imported_names(path):
            if name in local and name not in seen:
                seen.add(name)
                queue.append(ROOT / f"{name}.py")
    return seen


def dockerfile_copied_root_modules() -> set[str]:
    text = (ROOT / "api" / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    copied: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("COPY "):
            copied.update(Path(tok).stem for tok in re.split(r"\s+", line)[1:-1] if tok.endswith(".py"))
    return copied


class ApiDockerfileTests(unittest.TestCase):
    def test_all_local_modules_reachable_from_api_are_copied(self):
        required = required_root_modules()
        self.assertIn("portfolio_risk", required)
        missing = sorted(required - dockerfile_copied_root_modules())
        self.assertEqual(missing, [], f"api/Dockerfile 漏複製：{missing}")


if __name__ == "__main__":
    unittest.main()
