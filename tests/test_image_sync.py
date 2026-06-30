import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import odoo_local_sync.image_sync as mod


class TestImageSyncHelpers(unittest.TestCase):
    def test_extension_prefers_existing_image_suffix(self) -> None:
        self.assertEqual(mod._extension("photo.jpeg", "image/png"), ".jpg")
        self.assertEqual(mod._extension("photo.webp", "image/png"), ".webp")

    def test_extension_uses_mimetype_when_name_has_no_suffix(self) -> None:
        self.assertEqual(mod._extension("photo", "image/png"), ".png")
        self.assertEqual(mod._extension("photo", "image/webp"), ".webp")

    def test_sidecar_for_image_path(self) -> None:
        path = Path("/tmp/123-image.webp")
        self.assertEqual(mod._sidecar_for(path), Path("/tmp/123-image.webp.odoo.json"))

    def test_hash_is_stable(self) -> None:
        self.assertEqual(mod._compute_bytes_hash(b"abc"), mod._compute_bytes_hash(b"abc"))
        self.assertNotEqual(mod._compute_bytes_hash(b"abc"), mod._compute_bytes_hash(b"abcd"))


if __name__ == "__main__":
    unittest.main()
