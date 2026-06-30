import unittest
from unittest.mock import patch

import odoo_local_sync.odoo_client as client


class TestOdooClientSafety(unittest.TestCase):
    def test_invalid_url_scheme_is_missing_config(self) -> None:
        with patch.dict(
            client.ODOO_CONFIG,
            {
                "url": "file:///tmp/socket",
                "db": "demo",
                "username": "demo@example.com",
                "api_key": "secret",
            },
            clear=False,
        ):
            self.assertIn("url", client.missing_config_keys())

    def test_https_url_is_accepted_when_other_fields_exist(self) -> None:
        with patch.dict(
            client.ODOO_CONFIG,
            {
                "url": "https://example.odoo.com",
                "db": "demo",
                "username": "demo@example.com",
                "api_key": "secret",
            },
            clear=False,
        ):
            self.assertEqual(client.missing_config_keys(), [])


if __name__ == "__main__":
    unittest.main()
