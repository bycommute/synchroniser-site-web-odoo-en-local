import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from odoo_local_sync.sync_manager import SyncManager  # noqa: E402
from odoo_local_sync.odoo_content_validator import ValidationIssue  # noqa: E402


def safety_manager() -> SyncManager:
    manager = SyncManager.__new__(SyncManager)
    manager.default_lang = "fr_FR"
    manager.source_lang = "fr_FR"
    manager.active_langs = ["fr_FR", "en_US", "de_DE", "it_IT", "es_ES", "nl_BE"]
    manager._field_info_cache = {}
    manager.allow_description_sale_writes = False
    manager.allow_product_structural_writes = False
    manager.allow_metadata_false_clears = False
    manager.enforce_file_lang_match = True
    return manager


class TestSyncManagerSafety(unittest.TestCase):
    def test_description_sale_is_blocked_by_default(self) -> None:
        manager = safety_manager()
        reason = manager._push_field_skip_reason("product.template", "description_sale", "False")
        self.assertIn("description_sale", reason)

    def test_product_structural_fields_are_blocked_by_default(self) -> None:
        manager = safety_manager()
        for field in ("default_code", "list_price", "categ_id", "website_published"):
            with self.subTest(field=field):
                reason = manager._push_field_skip_reason("product.template", field, "123")
                self.assertIn("blocked", reason)

    def test_metadata_false_does_not_clear_text_fields_by_default(self) -> None:
        manager = safety_manager()
        reason = manager._push_field_skip_reason("website.page", "website_meta_description", "False")
        self.assertIn("False clears", reason)

    def test_metadata_false_can_clear_boolean_fields(self) -> None:
        manager = safety_manager()
        reason = manager._push_field_skip_reason("website.page", "website_published", "False")
        self.assertIsNone(reason)

    def test_file_language_must_match_metadata_language(self) -> None:
        manager = safety_manager()
        mismatch = manager._push_file_lang_mismatch_reason(
            Path("products/401-ricovero-per-biciclette-jungle.it_IT.html"),
            "fr_FR",
        )
        self.assertIn("Language mismatch", mismatch)

    def test_source_file_without_suffix_is_source_language(self) -> None:
        manager = safety_manager()
        reason = manager._push_file_lang_mismatch_reason(
            Path("products/401-abri-velos-jungle.html"),
            "fr_FR",
        )
        self.assertIsNone(reason)

    def test_raw_page_content_gets_odoo_website_layout(self) -> None:
        wrapped = SyncManager._ensure_qweb_template(
            '<section class="s_text_block"><div class="container">Bonjour</div></section>',
            "website.syncengine_test",
        )
        self.assertIn('t-name="website.syncengine_test"', wrapped)
        self.assertIn('t-call="website.layout"', wrapped)
        self.assertIn('id="wrap"', wrapped)
        self.assertIn('class="oe_structure"', wrapped)
        self.assertIn("Bonjour", wrapped)

    def test_existing_qweb_template_is_not_wrapped_twice(self) -> None:
        existing = '<t t-name="website.existing"><t t-call="website.layout"><div id="wrap">OK</div></t></t>'
        self.assertEqual(SyncManager._ensure_qweb_template(existing, "website.other"), existing)

    def test_existing_layout_without_t_name_only_gets_template_name(self) -> None:
        existing_layout = '<t t-call="website.layout"><div id="wrap" class="oe_structure">OK</div></t>'
        wrapped = SyncManager._ensure_qweb_template(existing_layout, "website.syncengine_layout")
        self.assertIn('t-name="website.syncengine_layout"', wrapped)
        self.assertEqual(wrapped.count('t-call="website.layout"'), 1)

    def test_website_page_arch_db_is_treated_as_rendered_translation_field(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"translate": True, "type": "text"}
        self.assertTrue(manager._is_html_translation_field("website.page", "arch_db"))

    def test_non_translated_text_field_is_not_rendered_translation_field(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"translate": False, "type": "text"}
        self.assertFalse(manager._is_html_translation_field("website.page", "url"))

    def test_translated_meta_description_is_not_rendered_translation_field(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"translate": True, "type": "text"}
        self.assertFalse(manager._is_html_translation_field("website.page", "website_meta_description"))

    def test_existing_page_raw_content_is_normalized_before_push(self) -> None:
        manager = safety_manager()
        manager._page_key_for_push = lambda record_id, metadata, clean_content: "website.normalized_page"
        rendered = manager._render_content_for_push(
            "pages",
            123,
            {"id": "123"},
            '<section><div class="container">Bonjour</div></section>',
        )
        self.assertIn('t-name="website.normalized_page"', rendered)
        self.assertIn('t-call="website.layout"', rendered)
        self.assertIn("Bonjour", rendered)

    def test_create_product_from_translation_is_refused(self) -> None:
        manager = safety_manager()
        with self.assertRaisesRegex(ValueError, "langue source"):
            manager._create_remote_product_from_file(
                Path("products/_drafts/product.en_US.html"),
                {"lang": "en_US", "name": "Draft"},
                "<p>Draft</p>",
                "en_US",
                dry_run=True,
            )

    def test_create_blog_from_translation_is_refused(self) -> None:
        manager = safety_manager()
        with self.assertRaisesRegex(ValueError, "langue source"):
            manager._create_remote_blog_post_from_file(
                Path("blog-posts/_drafts/post.en_US.html"),
                {"lang": "en_US", "name": "Draft"},
                "<p>Draft</p>",
                "en_US",
                dry_run=True,
            )

    def test_parse_bool_publication_values(self) -> None:
        self.assertTrue(SyncManager._parse_bool("published"))
        self.assertTrue(SyncManager._parse_bool("True"))
        self.assertFalse(SyncManager._parse_bool("False"))
        self.assertFalse(SyncManager._parse_bool("", default=False))

    def test_visibility_values_allow_publish_when_field_exists(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"type": "boolean"} if field == "website_published" else {}
        self.assertEqual(
            manager._visibility_update_values("product.template", publish=True),
            {"website_published": True},
        )

    def test_visibility_values_refuse_index_when_field_missing(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"type": "boolean"} if field == "website_published" else {}
        with self.assertRaisesRegex(ValueError, "website_indexed"):
            manager._visibility_update_values("product.template", index=True)

    def test_visibility_values_allow_page_index_when_field_exists(self) -> None:
        manager = safety_manager()
        manager._get_field_info = lambda model, field: {"type": "boolean"} if field in {"website_published", "website_indexed"} else {}
        self.assertEqual(
            manager._visibility_update_values("website.page", publish=False, index=True),
            {"website_published": False, "website_indexed": True},
        )

    def test_validation_errors_block_but_warnings_do_not_by_default(self) -> None:
        error = ValidationIssue("x.html", "error", "metadata.missing", "missing")
        warning = ValidationIssue("x.html", "warning", "html.script", "script")
        blocking, warnings = SyncManager._split_validation_issues([error, warning], strict=False)
        self.assertEqual(blocking, [error])
        self.assertEqual(warnings, [warning])

    def test_strict_validation_warnings_block(self) -> None:
        warning = ValidationIssue("x.html", "warning", "html.script", "script")
        blocking, warnings = SyncManager._split_validation_issues([warning], strict=True)
        self.assertEqual(blocking, [warning])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
