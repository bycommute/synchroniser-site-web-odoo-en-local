import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from odoo_local_sync.odoo_content_validator import validate_file  # noqa: E402


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestOdooContentValidator(unittest.TestCase):
    def test_language_suffix_must_match_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp) / "401-ricovero.it_IT.html",
                """<!--
ODOO-SYNC-METADATA
id: 401
lang: fr_FR
name: Test
-->
<section data-snippet="s_text_block"><p>Bonjour</p></section>
""",
            )
            issues = validate_file(path, category="products")
            self.assertIn("lang.mismatch", {issue.code for issue in issues})

    def test_script_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp) / "page.xml",
                """<!--
ODOO-SYNC-METADATA
lang: fr_FR
name: Test
-->
<section data-snippet="s_text_block"><script>alert(1)</script></section>
""",
            )
            issues = validate_file(path, category="pages")
            script_issues = [issue for issue in issues if issue.code == "html.script"]
            self.assertEqual(len(script_issues), 1)
            self.assertEqual(script_issues[0].severity, "warning")

    def test_qweb_page_without_layout_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp) / "page.xml",
                """<!--
ODOO-SYNC-METADATA
lang: fr_FR
name: Test
-->
<t t-name="website.test"><section data-snippet="s_text_block"><p>Bonjour</p></section></t>
""",
            )
            issues = validate_file(path, category="pages")
            self.assertIn("page.layout", {issue.code for issue in issues})

    def test_translation_structure_change_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write(
                directory / "10-page.xml",
                """<!--
ODOO-SYNC-METADATA
id: 10
lang: fr_FR
name: Test
-->
<section data-snippet="s_text_block" data-name="A"><p>Bonjour</p></section>
""",
            )
            translated = write(
                directory / "10-page.en_US.xml",
                """<!--
ODOO-SYNC-METADATA
id: 10
lang: en_US
name: Test
-->
<section data-snippet="s_text_block" data-name="B"><div>Hello</div></section>
""",
            )
            issues = validate_file(translated, category="pages")
            codes = {issue.code for issue in issues}
            self.assertIn("translation.structure", codes)


if __name__ == "__main__":
    unittest.main()
