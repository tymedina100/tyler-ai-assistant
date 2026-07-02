import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gumroad_helpers


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class GumroadHelpersTests(unittest.TestCase):
    def test_not_configured_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            products, err = gumroad_helpers.list_products()
        self.assertIsNone(products)
        self.assertIn("isn't configured", err)

    def test_list_products_parses_sales(self):
        payload = {"success": True, "products": [
            {"id": "P1", "name": "Widget", "short_url": "https://g.co/l/w",
             "sales_count": 3, "sales_usd_cents": 5700, "published": True},
        ]}
        with patch.dict(os.environ, {"GUMROAD_ACCESS_TOKEN": "tok"}), \
                patch.object(gumroad_helpers.requests, "get", return_value=FakeResp(200, payload)):
            products, err = gumroad_helpers.list_products()
        self.assertIsNone(err)
        self.assertEqual(products[0]["sales_count"], 3)
        self.assertEqual(products[0]["sales_usd_cents"], 5700)
        self.assertEqual(products[0]["short_url"], "https://g.co/l/w")

    def test_list_products_reports_api_error(self):
        with patch.dict(os.environ, {"GUMROAD_ACCESS_TOKEN": "tok"}), \
                patch.object(gumroad_helpers.requests, "get", return_value=FakeResp(401, {"success": False})):
            products, err = gumroad_helpers.list_products()
        self.assertIsNone(products)
        self.assertIn("401", err)


if __name__ == "__main__":
    unittest.main()
