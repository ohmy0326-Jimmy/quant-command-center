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
        self.assertIn("credit_transmission", required)
        self.assertIn("page1_lights", required)
        missing = sorted(required - dockerfile_copied_root_modules())
        self.assertEqual(missing, [], f"api/Dockerfile 漏複製：{missing}")

    def test_credit_fetch_without_streamlit_uses_public_csv(self):
        """API 映像沒有 streamlit。沒有 FRED_API_KEY 時要改走公開 CSV，不能變成 ModuleNotFoundError。"""
        import builtins
        import os
        from unittest.mock import patch

        import page1_lights

        real_import = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "streamlit" or str(name).startswith("streamlit."):
                raise ModuleNotFoundError("No module named 'streamlit'")
            return real_import(name, globals, locals, fromlist, level)

        class Response:
            status_code = 200
            text = "observation_date,DFII10\n2026-11-12,1.80\n"

            def json(self):
                raise AssertionError("沒有金鑰不該打官方 API")

        seen = []

        def fake_get(url, timeout=None, params=None, headers=None):
            seen.append(url)
            return Response()

        previous = os.environ.pop("FRED_API_KEY", None)
        try:
            with patch("builtins.__import__", guarded):
                self.assertEqual(page1_lights.fred_api_key(), "")
                points = page1_lights.fetch_fred_observations("DFII10", get=fake_get, api_key=page1_lights.fred_api_key())
        finally:
            if previous is not None:
                os.environ["FRED_API_KEY"] = previous
        self.assertEqual(points[-1], (page1_lights.date(2026, 11, 12), 1.8))
        self.assertTrue(any("fredgraph.csv" in url for url in seen))
        self.assertFalse(any("api.stlouisfed.org" in url for url in seen))


if __name__ == "__main__":
    unittest.main()
