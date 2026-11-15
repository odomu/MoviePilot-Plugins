import ast
from pathlib import Path
import types
import unittest


class TestTVSearchCacheScope(unittest.TestCase):
    def cache_key(self, media_id, season):
        source = Path(__file__).resolve().parents[1] / "plugins.v2/cloudsubscribe/handlers/sync/television.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        assignment = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "series_search_key" for target in node.targets)
        )
        namespace = {"mediainfo": types.SimpleNamespace(type="tv"), "series_id": media_id, "season": season}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(source), "exec"), namespace)
        return namespace["series_search_key"]

    def test_different_seasons_have_separate_candidates(self):
        self.assertNotEqual(self.cache_key(12345, 1), self.cache_key(12345, 2))

    def test_same_season_can_reuse_candidates(self):
        self.assertEqual(self.cache_key(12345, 2), self.cache_key(12345, 2))

    def test_different_series_remain_separate(self):
        self.assertNotEqual(self.cache_key(12345, 1), self.cache_key(54321, 1))


if __name__ == "__main__":
    unittest.main()
