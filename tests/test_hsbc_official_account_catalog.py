"""Contract checks for the reviewed HSBC UK/HK/US account catalog."""

import json
import unittest
from collections import Counter
from pathlib import Path

from scripts.sync_hsbc_official_account_catalog import (
    DOCUMENTS,
    PRODUCTS,
    RELATIONS,
    ROUTES,
    RULES,
    validate_catalog,
)


class HsbcOfficialAccountCatalogTests(unittest.TestCase):
    def test_inventory_and_sources_are_complete(self):
        validate_catalog()
        self.assertEqual({"UK": 16, "US": 6, "HK": 9},
                         dict(Counter(row[3] for row in PRODUCTS)))
        self.assertTrue(all(row[10].startswith("https://") for row in PRODUCTS))

    def test_every_product_has_route_rule_and_document(self):
        ids = {row[0] for row in PRODUCTS}
        self.assertEqual(ids, {row[1] for row in ROUTES})
        self.assertFalse(ids - {row[1] for row in RULES})
        self.assertFalse(ids - {row[1] for row in DOCUMENTS})

    def test_product_relationship_graph_references_catalog_products(self):
        ids = {row[0] for row in PRODUCTS}
        self.assertEqual(11, len(RELATIONS))
        self.assertTrue(all(row[1] in ids and row[2] in ids for row in RELATIONS))
        self.assertTrue(any(row[1] == "acct_us_global_money" for row in RELATIONS))

    def test_every_hk_product_has_an_archived_source_document(self):
        manifest_path = (Path(__file__).resolve().parents[1] / "agent1" / "migrations" /
                         "sources" / "account_source_documents.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archived = {entry["account_key"] for entry in manifest["documents"]
                    if entry["market"] == "CN-HK"}
        expected = {f"hsbc_{row[1]}" for row in PRODUCTS if row[3] == "HK"}
        self.assertEqual(expected, archived)


if __name__ == "__main__":
    unittest.main()
