import unittest
from types import SimpleNamespace
from uuid import uuid4

from backend.app.services.sql_executor import validate_readonly_sql, execute_readonly_sql
from backend.app.mcp.tools import (
    execute_readonly_sql_tool,
    get_structured_query_results_tool,
    perform_catalog_update_tool
)


class SQLExecutorMCPTest(unittest.TestCase):
    def test_validate_readonly_sql_accepts_valid_select(self) -> None:
        query = "SELECT * FROM catalog_items WHERE ingredient_name ILIKE '%citric acid%'"
        validated = validate_readonly_sql(query)
        self.assertTrue(validated.startswith("SELECT"))
        self.assertIn("LIMIT 100", validated)

    def test_validate_readonly_sql_accepts_with_clause(self) -> None:
        query = "WITH recent_items AS (SELECT * FROM catalog_items) SELECT * FROM recent_items"
        validated = validate_readonly_sql(query)
        self.assertTrue(validated.startswith("WITH"))

    def test_validate_readonly_sql_rejects_insert(self) -> None:
        query = "INSERT INTO catalog_items (ingredient_name) VALUES ('Test')"
        with self.assertRaises(ValueError):
            validate_readonly_sql(query)

    def test_validate_readonly_sql_rejects_update(self) -> None:
        query = "UPDATE catalog_items SET price_per_unit = 10"
        with self.assertRaises(ValueError):
            validate_readonly_sql(query)

    def test_validate_readonly_sql_rejects_delete(self) -> None:
        query = "DELETE FROM suppliers WHERE id = '123'"
        with self.assertRaises(ValueError):
            validate_readonly_sql(query)

    def test_validate_readonly_sql_rejects_drop_table(self) -> None:
        query = "DROP TABLE suppliers"
        with self.assertRaises(ValueError):
            validate_readonly_sql(query)

    def test_validate_readonly_sql_rejects_stacked_query_comment_hacks(self) -> None:
        query = "SELECT * FROM catalog_items; DROP TABLE suppliers"
        with self.assertRaises(ValueError):
            validate_readonly_sql(query)

    def test_mcp_get_structured_query_results_tool(self) -> None:
        tenant_id = uuid4()
        fake_rows = [{"ingredient_name": "Citric Acid", "price_per_unit": 12.5}]
        
        class FakeResult:
            returns_rows = True
            def keys(self):
                return ["ingredient_name", "price_per_unit"]
            def fetchall(self):
                return [("Citric Acid", 12.5)]

        class FakeDB:
            def execute(self, statement, params=None):
                return FakeResult()

        db = FakeDB()
        res = get_structured_query_results_tool(
            db,
            "SELECT ingredient_name, price_per_unit FROM catalog_items WHERE tenant_id = :tenant_id",
            tenant_id=tenant_id,
        )
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["rows"][0]["ingredient_name"], "Citric Acid")
        self.assertEqual(res["rows"][0]["price_per_unit"], 12.5)

    def test_tenant_query_rejects_parameter_without_tenant_predicate(self) -> None:
        class FakeDB:
            def execute(self, statement, params=None):
                raise AssertionError("Unscoped SQL must not run")

        self.assertEqual(
            execute_readonly_sql(FakeDB(), "SELECT :tenant_id AS tenant", tenant_id=uuid4()),
            [],
        )


if __name__ == "__main__":
    unittest.main()
