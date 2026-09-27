"""Regression: aiohttp must not serve stale precompressed panel sidecars."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("frontend_assets", Path(__file__).parents[1] / "custom_components/is_it_dead/frontend_assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)

class FrontendAssetsTest(unittest.TestCase):
    def test_obsolete_sidecars_removed_without_touching_source_or_other_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "is_it_dead_panel.js"
            source.write_bytes(b"current panel")
            for suffix in (".gz", ".br"):
                (root / (source.name + suffix)).write_bytes(b"old panel")
            unrelated = root / "other.js.gz"
            unrelated.write_bytes(b"keep")
            assets.remove_legacy_compressed_panel(folder)
            assets.remove_legacy_compressed_panel(folder)  # integration reload is idempotent
            self.assertEqual(source.read_bytes(), b"current panel")
            self.assertEqual(unrelated.read_bytes(), b"keep")
            self.assertFalse((root / (source.name + ".gz")).exists())
            self.assertFalse((root / (source.name + ".br")).exists())

if __name__ == "__main__":
    unittest.main()
