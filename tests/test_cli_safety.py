import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import odoo_local_sync.cli as cli  # noqa: E402


class TestCliSafety(unittest.TestCase):
    def test_git_targets_for_specific_files(self) -> None:
        targets = cli._git_targets_for_push(["products/demo.html"], None)
        self.assertEqual(targets, [str(cli.PROJECT_ROOT / "products/demo.html")])

    def test_git_targets_for_category(self) -> None:
        targets = cli._git_targets_for_push(None, "products")
        self.assertEqual(targets, [str(cli.SYNC_DIRS["products"])])

    def test_git_targets_for_all_categories(self) -> None:
        targets = cli._git_targets_for_push(None, None)
        self.assertIn(str(cli.SYNC_DIRS["products"]), targets)
        self.assertIn(str(cli.SYNC_DIRS["pages"]), targets)
        self.assertNotIn(str(cli.PROJECT_ROOT), targets)

    def test_expand_files_all_langs_groups_by_odoo_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "371-abri-velos-bosquet.html"
            en = root / "371-bosquet-bike-shelter.en_US.html"
            it = root / "371-ricovero-per-biciclette-bosquet.it_IT.html"
            other = root / "401-abri-velos-jungle.html"
            for path in (source, en, it, other):
                path.write_text("<p>demo</p>", encoding="utf-8")

            expanded = cli._expand_files_all_langs([str(source)])

            self.assertEqual(expanded, [str(source), str(en), str(it)])

    def test_expand_files_all_langs_from_translated_file_includes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "371-abri-velos-bosquet.html"
            en = root / "371-bosquet-bike-shelter.en_US.html"
            de = root / "371-fahrradunterstand-bosquet.de_DE.html"
            for path in (source, en, de):
                path.write_text("<p>demo</p>", encoding="utf-8")

            expanded = cli._expand_files_all_langs([str(en)])

            self.assertEqual(expanded, [str(source), str(de), str(en)])

    def test_expand_files_all_langs_keeps_non_id_files_single(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nouvelle-page.html"
            path.write_text("<p>demo</p>", encoding="utf-8")

            expanded = cli._expand_files_all_langs([str(path)])

            self.assertEqual(expanded, [str(path)])


if __name__ == "__main__":
    unittest.main()
