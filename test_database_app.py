import os
import unittest
from unittest.mock import Mock, patch

from offline_lineage import analyze_files
from fastapi.testclient import TestClient
from launcher import DesktopLauncher, has_sql_server_odbc_drivers

from server import CatalogRequest, ConnectionRequest, _catalog_result, _history_lineage, _snowflake_connection, app


class OfflineLineageTests(unittest.TestCase):
    def test_snowflake_create_view_maps_source_to_target_columns(self):
        result = analyze_files([{
            "name": "ANALYTICS_DB.CORE.V_CUSTOMER_SALES",
            "content": """CREATE OR REPLACE VIEW ANALYTICS_DB.CORE.V_CUSTOMER_SALES AS
                SELECT c.CUSTOMER_ID, c.CUSTOMER_NAME, SUM(o.TOTAL_AMOUNT) AS LIFETIME_VALUE
                FROM ANALYTICS_DB.CORE.CUSTOMERS c
                JOIN ANALYTICS_DB.CORE.CUSTOMER_ORDERS o ON c.CUSTOMER_ID = o.CUSTOMER_ID
                GROUP BY c.CUSTOMER_ID, c.CUSTOMER_NAME""",
        }], "snowflake")
        self.assertEqual(result["files"][0]["status"], "Parsed")
        self.assertEqual(result["summary"]["mapped_columns"], 3)
        self.assertIn(("ANALYTICS_DB.CORE.CUSTOMERS", "CUSTOMER_ID", "ANALYTICS_DB.CORE.V_CUSTOMER_SALES", "CUSTOMER_ID"), {
            (row["source_table"], row["source_column"], row["target_table"], row["target_column"])
            for row in result["mappings"]
        })
        self.assertIn(("ANALYTICS_DB.CORE.CUSTOMER_ORDERS", "TOTAL_AMOUNT", "ANALYTICS_DB.CORE.V_CUSTOMER_SALES", "LIFETIME_VALUE"), {
            (row["source_table"], row["source_column"], row["target_table"], row["target_column"])
            for row in result["mappings"]
        })

    def test_snowflake_insert_maps_columns_and_calculation_inputs(self):
        result = analyze_files([{
            "name": "sales.sql",
            "content": """INSERT INTO ANALYTICS.SALES (CUSTOMER_ID, TOTAL)
                SELECT c.ID, SUM(o.QUANTITY * p.PRICE)
                FROM RAW.CUSTOMERS c JOIN RAW.ORDERS o ON c.ID = o.CUSTOMER_ID
                JOIN RAW.PRODUCTS p ON o.PRODUCT_ID = p.ID
                GROUP BY c.ID""",
        }], "snowflake")

        self.assertEqual(result["files"][0]["status"], "Parsed")
        self.assertEqual(result["summary"]["mapped_columns"], 3)
        self.assertEqual(result["dialect"], "snowflake")
        self.assertEqual(
            {(row["source_table"], row["source_column"]) for row in result["mappings"] if row["target_column"] == "TOTAL"},
            {("RAW.ORDERS", "QUANTITY"), ("RAW.PRODUCTS", "PRICE")},
        )

    def test_sql_server_insert_maps_target_columns_to_real_sources(self):
        result = analyze_files(
            [{
                "name": "employee_load.sql",
                "content": """
                    INSERT INTO dbo.EmployeeTarget (EmployeeId, DisplayName)
                    SELECT e.EmployeeId,
                           UPPER(e.FirstName) + ' ' + e.LastName AS DisplayName
                    FROM dbo.EmployeeSource e;
                """,
            }],
            "sqlserver",
        )

        self.assertEqual(result["summary"]["mapped_columns"], 3)
        self.assertEqual(result["files"][0]["status"], "Parsed")
        self.assertIn("dbo.EmployeeSource", result["tables"])
        self.assertIn("dbo.EmployeeTarget", result["tables"])
        self.assertEqual(len(result["relationships"]), 1)
        full_name = [row for row in result["mappings"] if row["target_column"] == "DisplayName"]
        self.assertEqual({row["source_column"] for row in full_name}, {"FirstName", "LastName"})

    def test_file_dependencies_link_writer_to_reader_through_shared_table(self):
        result = analyze_files(
            [
                {
                    "name": "load_stage.sql",
                    "content": "INSERT INTO dbo.Stage (id) SELECT source.id FROM dbo.Source source",
                },
                {
                    "name": "load_mart.sql",
                    "content": "INSERT INTO dbo.Mart (id) SELECT stage.id FROM dbo.Stage stage",
                },
                {
                    "name": "load_other.sql",
                    "content": "INSERT INTO dbo.OtherTarget (id) SELECT other.id FROM dbo.OtherSource other",
                },
                {
                    "name": "load_renamed.sql",
                    "content": "INSERT INTO dbo.Renamed (stage_identifier) SELECT stage.OtherId FROM dbo.Stage stage",
                },
            ],
            "sqlserver",
        )

        self.assertEqual(result["file_dependencies"], [
            {
                "source_file": "load_stage.sql",
                "target_file": "load_mart.sql",
                "via_table": "dbo.Stage",
                "via_columns": [{"writer_column": "id", "reader_column": "id"}],
            },
            {
                "source_file": "load_stage.sql",
                "target_file": "load_renamed.sql",
                "via_table": "dbo.Stage",
                "via_columns": [],
            },
        ])
        self.assertEqual(result["summary"]["file_dependency_count"], 2)

    def test_sql_server_stored_procedure_maps_insert_select(self):
        def stored_procedure(name, body):
            return {
                "name": name,
                "content": f"""
                    USE [ExampleDB]
                    GO
                    SET ANSI_NULLS ON
                    GO
                    SET QUOTED_IDENTIFIER ON
                    GO
                    ALTER PROCEDURE [dbo].[{name}] (@RuleId INT)
                    AS
                    BEGIN
                    {body}
                    END
                    SELECT CheckNotifyId = CAST(0 AS BIT)
                """.replace("\n", "\r\n"),
            }

        files = [
            stored_procedure(
                "usp_Load_MIAMI",
                """DELETE FROM [MIAMI_RawReconciliation]
                    WHERE [AsOfDate] = (SELECT TOP(1) [AsOfDate] FROM [UNI_MIAMI_RawReconciliation])


                    INSERT INTO [chameleon_IHC].[dbo].[MIAMI_RawReconciliation] ([ID])
                    (SELECT [ID] FROM [chameleon_IHC].[dbo].[UNI_MIAMI_RawReconciliation]
                     WHERE [StatusId] IS NULL)""",
            ),
            stored_procedure(
                "usp_Load_MRG_MIAMI",
                """SET NOCOUNT ON;
                    TRUNCATE TABLE [chameleon_IHC].[dbo].[MRG_MIAMI_RawReconciliation]
                    INSERT INTO [chameleon_IHC].[dbo].[MRG_MIAMI_RawReconciliation] ([ID])
                    (SELECT [ID] FROM [chameleon_IHC].[dbo].[STG_MIAMI_RawReconciliation]
                     WHERE [StatusId] IS NULL)""",
            ),
            stored_procedure(
                "usp_Load_UNI_MIAMI",
                """SET NOCOUNT ON;
                    TRUNCATE TABLE [chameleon_IHC].[dbo].[UNI_MIAMI_RawReconciliation]
                    INSERT INTO [chameleon_IHC].[dbo].[UNI_MIAMI_RawReconciliation] ([ID])
                    (SELECT [ID] FROM [chameleon_IHC].[dbo].[MRG_MIAMI_RawReconciliation]
                     WHERE [StatusId] IS NULL)""",
            ),
        ]
        result = analyze_files(files, "sqlserver")

        self.assertEqual([file["status"] for file in result["files"]], ["Parsed"] * 3)
        self.assertEqual(result["summary"]["mapped_columns"], 3)
        self.assertEqual(
            {(row["source_table"], row["target_table"]) for row in result["mappings"]},
            {
                ("chameleon_IHC.dbo.UNI_MIAMI_RawReconciliation", "chameleon_IHC.dbo.MIAMI_RawReconciliation"),
                ("chameleon_IHC.dbo.STG_MIAMI_RawReconciliation", "chameleon_IHC.dbo.MRG_MIAMI_RawReconciliation"),
                ("chameleon_IHC.dbo.MRG_MIAMI_RawReconciliation", "chameleon_IHC.dbo.UNI_MIAMI_RawReconciliation"),
            },
        )

    def test_sql_server_procedure_without_outer_begin_maps_insert_queries(self):
        content = """ALTER PROCEDURE [dbo].[usp_Load] (@RuleID INTEGER)
                    AS
                    DECLARE @AsOfDate DATE
                    IF @AsOfDate IS NULL
                    BEGIN
                        INSERT INTO dbo.Schedule ([ID])
                        SELECT [ID] FROM dbo.Source
                        SELECT @Server = ServerName FROM dbo.Config
                    END
                    ;WITH FinalRows AS (
                        SELECT [ID] FROM dbo.Source
                    )
                    INSERT INTO dbo.Target ([ID])
                    SELECT [ID] FROM FinalRows
                    SELECT CheckNotifyId = CAST(0 AS BIT)
                """.replace("\n", "\r\n")
        result = analyze_files([{"name": "usp_load.sql", "content": content}], "sqlserver")

        self.assertEqual(result["files"][0]["status"], "Parsed")
        self.assertEqual(result["summary"]["mapped_columns"], 2)
        self.assertIn(
            ("FinalRows", "dbo.Target"),
            {(row["source_table"], row["target_table"]) for row in result["mappings"]},
        )

    def test_sql_server_update_maps_assigned_source_columns(self):
        fields = ["TypeAExposureAmount", "TypeBExposureAmount", "TypeCExposureAmount"]
        files = [{
            "name": f"usp_load_{field}.sql",
            "content": f"""CREATE PROCEDURE [dbo].[usp_Load_{field}]
                    @SourceId varchar(15), @AdjIndicator bit = 0
                    AS
                    SET NOCOUNT ON;
                    UPDATE Uni_OneObligor
                    SET {field} = (SELECT SUM(Cust.{field})
                        FROM Uni_Customer AS Cust
                        WHERE Cust.OneObligorNumber = Uni_OneObligor.OneObligorNumber
                        AND Cust.AsOfDate = Uni_OneObligor.AsOfDate
                        AND Cust.AdjustmentIndicator = Uni_OneObligor.AdjustmentIndicator
                        AND Cust.SourceId = @SourceId)
                    WHERE Uni_OneObligor.SourceId = @SourceId
                    AND Uni_OneObligor.StatusCode IS NULL
                    AND Uni_OneObligor.AdjustmentIndicator = @AdjIndicator
                """.replace("\n", "\r\n"),
        } for field in fields]

        result = analyze_files(files, "sqlserver")

        self.assertEqual([file["status"] for file in result["files"]], ["Parsed"] * 3)
        self.assertEqual(
            {(row["source_table"], row["source_column"], row["target_table"], row["target_column"])
             for row in result["mappings"]},
            {("Uni_Customer", field, "Uni_OneObligor", field) for field in fields},
        )

    def test_oracle_create_view_maps_source_columns(self):
        result = analyze_files(
            [{
                "name": "employee_view.sql",
                "content": "CREATE VIEW HR.EMPLOYEE_VIEW AS SELECT e.ID, e.NAME FROM HR.EMPLOYEE e",
            }],
            "oracle",
        )
        self.assertEqual(result["summary"]["mapped_columns"], 2)
        self.assertEqual(result["mappings"][0]["target_table"], "HR.EMPLOYEE_VIEW")

    def test_tokenization_error_is_reported_per_file(self):
        result = analyze_files(
            [{"name": "malformed.sql", "content": "SELECT 'unterminated"}],
            "sqlserver",
        )

        self.assertEqual(result["files"][0]["status"], "Could not parse")
        self.assertEqual(result["files"][0]["mapped_columns"], 0)


class _FakeCursor:
    def __init__(self, responses, descriptions=None):
        self.responses = iter(responses)
        self.descriptions = iter(descriptions or [])
        self.description = None
        self.queries = []
        self.parameters = []

    def execute(self, query, *_args):
        self.queries.append(query)
        self.parameters.append(_args[0] if _args else None)
        self.description = next(self.descriptions, None)
        return self

    def fetchall(self):
        return next(self.responses)

    def fetchone(self):
        return next(self.responses)


class CatalogTests(unittest.TestCase):
    def test_history_parser_reports_unsupported_or_incomplete_sql(self):
        cases = [
            ("INSERT INTO T SELECT * FROM S", "DB", "CORE", "Wildcard"),
            ("INSERT INTO T (ID) SELECT ID FROM S", None, None, "context"),
            ("INSERT INTO T (ID) WITH source AS (SELECT ID FROM S) SELECT ID FROM source", "DB", "CORE", "CTEs"),
            ("INSERT INTO T (ID) SELECT ID FROM (SELECT ID FROM S)", "DB", "CORE", "nested"),
            ("INSERT INTO T VALUES (1); INSERT INTO T VALUES (2)", "DB", "CORE", "Multi-statement"),
            ("CREATE TABLE T (ID NUMBER)", "DB", "CORE", "No supported"),
            ("INSERT INTO T SELECT 'unterminated", "DB", "CORE", "parsed safely"),
            (" " * 2_000_001, "DB", "CORE", "2 MB"),
        ]
        for sql, database, schema, warning in cases:
            with self.subTest(warning=warning):
                result = _history_lineage([("query-1", sql, database, schema, "INSERT")], ["DB.CORE.T", "DB.CORE.S"], 50)
                self.assertEqual(result["mappings"], [])
                self.assertIn(warning, result["warnings"][0])
                self.assertEqual(result["files"][0]["status"], "Skipped")

    def test_history_parser_preserves_quoted_names_and_filters_unselected_sources(self):
        sql = 'INSERT INTO "DB"."CORE"."Target" (ID) SELECT s.ID FROM "Source" s'
        rows = [("query-1", sql, "DB", "CORE", "INSERT")]
        result = _history_lineage(rows, ["DB.CORE.Target", "DB.CORE.Source"], 50)
        self.assertEqual(result["relationships"][0]["source"], "DB.CORE.Source")
        self.assertEqual(result["mappings"][0]["target_table"], "DB.CORE.Target")
        self.assertEqual(_history_lineage(rows, ["DB.CORE.Target"], 50)["mappings"], [])

    def test_history_parser_handles_empty_history_and_analysis_limits(self):
        result = _history_lineage([], ["DB.CORE.T"], 50)
        self.assertEqual(result["summary"]["relationship_count"], 0)
        rows = [(str(index), "INSERT INTO T (ID) SELECT ID FROM S", "DB", "CORE", "INSERT") for index in range(51)]
        result = _history_lineage(rows, ["DB.CORE.T", "DB.CORE.S"], 50)
        self.assertEqual(len(result["files"]), 50)
        self.assertIn("limit", result["warnings"][0])
        self.assertEqual(len(result["relationships"]), 1)

    def test_snowflake_connector_receives_scoped_connection_context(self):
        request = ConnectionRequest(
            engine="snowflake", account="org-account", warehouse="ANALYTICS_WH",
            username="reader", password="secret", database="ANALYTICS", role="READ_ONLY",
            schema="PUBLIC",
        )
        with patch("snowflake.connector.connect", return_value="fake connection") as connect:
            connection = _snowflake_connection(request)

        self.assertEqual(connection, "fake connection")
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["account"], "org-account")
        self.assertEqual(kwargs["warehouse"], "ANALYTICS_WH")
        self.assertEqual(kwargs["database"], "ANALYTICS")
        self.assertEqual(kwargs["schema"], "PUBLIC")
        self.assertEqual(kwargs["role"], "READ_ONLY")
        self.assertEqual(kwargs["password"], "secret")

    def test_snowflake_catalog_groups_columns_and_labels_declared_keys(self):
        imported_key_columns = [
            (name, None) for name in (
                "created_on", "fk_database_name", "fk_schema_name", "fk_name",
                "fk_table_name", "fk_column_name", "pk_database_name", "pk_schema_name",
                "pk_name", "pk_table_name", "pk_column_name", "key_sequence",
            )
        ]
        cursor = _FakeCursor([
            [("PUBLIC", "CUSTOMERS", "ID", "NUMBER(38,0)", "NO", "BASE TABLE"),
             ("PUBLIC", "ORDERS", "CUSTOMER_ID", "NUMBER(38,0)", "YES", "BASE TABLE"),
             ("PUBLIC", "V_ORDERS", "ID", "NUMBER(38,0)", "YES", "VIEW")],
            [("now", "ANALYTICS", "PUBLIC", "FK_ORDERS_CUSTOMERS", "ORDERS", "CUSTOMER_ID",
              "RAW", "PUBLIC", "CUSTOMERS_PK", "CUSTOMERS", "ID", 1)],
        ], descriptions=[None, imported_key_columns])
        result = _catalog_result(CatalogRequest(
            engine="snowflake", account="org-account", warehouse="ANALYTICS_WH",
            host=None, port=None, username="reader", password="secret", database="ANALYTICS",
        ), cursor)

        self.assertEqual(result["database"], "ANALYTICS")
        self.assertEqual(result["schemas"], ["PUBLIC"])
        self.assertEqual(result["tables"][0]["columns"][0], {
            "name": "ID", "type": "NUMBER(38,0)", "nullable": False,
        })
        self.assertEqual(next(table for table in result["tables"] if table["name"] == "V_ORDERS")["object_type"], "VIEW")
        self.assertEqual(result["relationships"][0]["constraint"], "FK_ORDERS_CUSTOMERS")
        self.assertEqual(result["relationships"][0]["source_database"], "RAW")
        self.assertEqual(result["relationships"][0]["target_database"], "ANALYTICS")
        self.assertIn("may not be enforced", result["relationship_kind"].lower())
        self.assertTrue(any("SHOW IMPORTED KEYS IN DATABASE" in query for query in cursor.queries))

    def test_snowflake_key_metadata_failure_is_not_reported_as_zero_keys(self):
        class KeyMetadataUnavailableCursor(_FakeCursor):
            def execute(self, query, *_args):
                if "SHOW IMPORTED KEYS" in query:
                    self.queries.append(query)
                    raise RuntimeError("not authorized")
                return super().execute(query, *_args)

        cursor = KeyMetadataUnavailableCursor([[("PUBLIC", "CUSTOMERS", "ID", "NUMBER", "NO")]])
        result = _catalog_result(CatalogRequest(
            engine="snowflake", account="org-account", warehouse="ANALYTICS_WH",
            username="reader", password="secret", database="ANALYTICS",
        ), cursor)
        self.assertEqual(result["relationships"], [])
        self.assertIn("could not be read", result["relationship_kind"].lower())

    def test_sql_server_catalog_groups_columns_and_foreign_keys(self):
        cursor = _FakeCursor([
            [("dbo", "Parent", "Id", "int", False), ("sales", "Child", "ParentId", "int", True)],
            [("sales", "Child", "ParentId", "dbo", "Parent", "Id", "FK_Child_Parent")],
        ])
        result = _catalog_result(CatalogRequest(
            engine="sqlserver", host="localhost", port=1433, username="reader",
            password="secret", database="ExampleDB",
        ), cursor)
        self.assertEqual(result["database"], "ExampleDB")
        self.assertEqual(result["schemas"], ["dbo", "sales"])
        self.assertEqual(len(result["tables"]), 2)
        self.assertEqual(result["relationships"][0]["source"], "dbo.Parent")
        self.assertEqual(result["relationships"][0]["target"], "sales.Child")

    def test_oracle_catalog_relationships_are_labeled_as_foreign_keys(self):
        cursor = _FakeCursor([
            [("HR", "EMPLOYEES", "ID", "NUMBER", "N")],
            [("HR", "EMPLOYEES", "ID", "HR", "DEPARTMENTS", "ID", "FK_EMP_DEPT")],
        ])
        result = _catalog_result(CatalogRequest(
            engine="oracle", host="localhost", port=1521, username="reader",
            password="secret", service_name="ORCLPDB1",
        ), cursor)
        self.assertEqual(result["database"], "ORCLPDB1")
        self.assertIn("Foreign-key structure", result["relationship_kind"])
        self.assertEqual(result["relationships"][0]["constraint"], "FK_EMP_DEPT")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="http://127.0.0.1:8765")

    def test_health_is_local_service_ready(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_query_history_lineage_qualifies_context_and_filters_selected_tables(self):
        cursor = _FakeCursor([[
            ("load-1", "INSERT INTO ANALYTICS_DB.CORE.ORDERS (ID) SELECT s.ID FROM ORDERS_STG s",
             "ANALYTICS_STG_DB", "CORE", "INSERT"),
            ("unrelated", "INSERT INTO OTHER.CORE.T (ID) SELECT ID FROM OTHER.CORE.S",
             "OTHER", "CORE", "INSERT"),
            ("merge-1", "MERGE INTO T USING S ON T.ID=S.ID WHEN MATCHED THEN UPDATE SET T.ID=S.ID",
             "ANALYTICS_DB", "CORE", "MERGE"),
        ]])
        with patch("server._connection") as connect:
            connect.return_value.cursor.return_value = cursor
            cursor.close = lambda: None
            response = self.client.post("/api/live/snowflake-history-lineage", json={
                "username": "reader", "password": "test-password", "account": "org-account",
                "warehouse": "WH", "databases": ["ANALYTICS_DB", "ANALYTICS_STG_DB"],
                "tables": ["ANALYTICS_DB.CORE.ORDERS", "ANALYTICS_STG_DB.CORE.ORDERS_STG"],
            })
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["relationships"], [{
            "source": "ANALYTICS_STG_DB.CORE.ORDERS_STG", "target": "ANALYTICS_DB.CORE.ORDERS",
            "kind": "sql_mapping",
        }])
        self.assertEqual(result["mappings"][0]["file"], "load-1")
        self.assertEqual(result["summary"]["mapped_columns"], 1)
        self.assertTrue(any("MERGE" in warning for warning in result["warnings"]))
        self.assertTrue(any("QUERY_HISTORY" in query for query in cursor.queries))
        self.assertIn("LOWER(execution_status) = 'success'", cursor.queries[0])
        self.assertEqual(cursor.parameters[0], (10079,))
        connect.return_value.close.assert_called_once()

    def test_query_history_lineage_rejects_out_of_scope_table_before_connecting(self):
        with patch("server._connection") as connect:
            response = self.client.post("/api/live/snowflake-history-lineage", json={
                "username": "reader", "password": "test-password", "account": "org-account",
                "warehouse": "WH", "databases": ["ANALYTICS_DB"], "tables": ["OTHER.CORE.T"],
            })
        self.assertEqual(response.status_code, 422)
        connect.assert_not_called()

    def test_query_history_permissions_failure_closes_resources_and_is_safe(self):
        with patch("server._connection") as connect:
            cursor = connect.return_value.cursor.return_value
            cursor.execute.side_effect = RuntimeError("sensitive driver detail")
            response = self.client.post("/api/live/snowflake-history-lineage", json={
                "username": "reader", "password": "test-password", "account": "org-account",
                "warehouse": "WH", "databases": ["DB"], "tables": ["DB.CORE.T"],
            })
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sensitive driver detail", response.text)
        cursor.close.assert_called_once()
        connect.return_value.close.assert_called_once()

    def test_cross_origin_requests_are_rejected(self):
        response = self.client.get("/api/health", headers={"origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)

    def test_offline_endpoint_returns_parsed_mappings(self):
        response = self.client.post("/api/offline/analyze", json={
            "dialect": "sqlserver",
            "files": [{"name": "load.sql", "content": "INSERT INTO dbo.t (id) SELECT s.id FROM dbo.s s"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["mapped_columns"], 1)

    def test_snowflake_is_available_for_offline_analysis(self):
        response = self.client.post("/api/offline/analyze", json={
            "dialect": "snowflake",
            "files": [{"name": "load.sql", "content": "INSERT INTO T (ID) SELECT S.ID FROM S"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dialect"], "snowflake")

    def test_snowflake_live_connection_lists_databases_with_fake_connection(self):
        class FakeSnowflakeConnection:
            def cursor(self):
                return self

            def execute(self, _query):
                return self

            def fetchone(self):
                return ("ANALYTICS", "ANALYTICS_WH", "ANALYST")

            def fetchall(self):
                return [("2026-10-08", "ANALYTICS"), ("2026-10-08", "RAW")]

            def close(self):
                pass

        with patch("server._snowflake_connection", return_value=FakeSnowflakeConnection()) as connect_snowflake:
            response = self.client.post("/api/connect", json={
                "engine": "snowflake", "account": "org-account", "warehouse": "ANALYTICS_WH",
                "username": "reader", "password": "test-password",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["engine"], "snowflake")
        self.assertEqual(response.json()["databases"], ["ANALYTICS", "RAW"])
        self.assertIsNone(connect_snowflake.call_args.args[1])

    def test_snowflake_live_view_definitions_generate_column_mappings(self):
        ddl = "CREATE OR REPLACE VIEW ANALYTICS.CORE.V_ORDERS AS SELECT O.ID AS ORDER_ID FROM ANALYTICS.CORE.ORDERS O"

        class FakeViewCursor:
            def __init__(self):
                self.queries = []
            def execute(self, query, params=None):
                self.queries.append((query, params))
                return self
            def fetchone(self):
                return (ddl,)
            def close(self):
                pass

        class FakeViewConnection:
            def __init__(self):
                self.cursor_instance = FakeViewCursor()
            def cursor(self):
                return self.cursor_instance
            def close(self):
                pass

        connection = FakeViewConnection()
        with patch("snowflake.connector.connect", return_value=connection) as connect_snowflake:
            response = self.client.post("/api/live/snowflake-view-mapping", json={
                "account": "org-account", "warehouse": "ANALYTICS_WH", "username": "reader",
                "password": "test-password", "database": "ANALYTICS",
                "views": ["CORE.V_ORDERS"],
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["mapped_columns"], 1)
        self.assertEqual(response.json()["mappings"][0]["source_column"], "ID")
        self.assertEqual(response.json()["mappings"][0]["target_table"], "ANALYTICS.CORE.V_ORDERS")
        self.assertEqual(connection.cursor_instance.queries[0][1], ("ANALYTICS.CORE.V_ORDERS",))
        self.assertEqual(connect_snowflake.call_args.kwargs["database"], "ANALYTICS")

    def test_snowflake_live_view_definition_failure_is_explicit(self):
        class FakeViewCursor:
            def execute(self, *_args):
                raise RuntimeError("permission denied")
            def close(self):
                pass

        class FakeViewConnection:
            def cursor(self):
                return FakeViewCursor()
            def close(self):
                pass

        with patch("server._snowflake_connection", return_value=FakeViewConnection()):
            response = self.client.post("/api/live/snowflake-view-mapping", json={
                "account": "org-account", "warehouse": "ANALYTICS_WH", "username": "reader",
                "password": "test-password", "database": "ANALYTICS",
                "views": ["ANALYTICS.CORE.V_ORDERS"],
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["files"][0]["status"], "Definition unavailable")
        self.assertIn("GET_DDL privileges", response.json()["files"][0]["message"])

    def test_snowflake_controls_are_available_for_offline_and_live_modes(self):
        page = os.path.join(os.path.dirname(__file__), "index.html")
        with open(page, encoding="utf-8") as source:
            html = source.read()
        self.assertIn('<option value="snowflake">Snowflake</option>', html)
        self.assertIn('id="engine-snowflake" value="snowflake"', html)
        self.assertIn('id="snowflake-account"', html)
        self.assertIn('id="snowflake-warehouse"', html)
        self.assertIn('class="field network-field"', html)
        self.assertIn('"declared key relationships"', html)
        self.assertIn('declared Snowflake key metadata; these keys may not be enforced', html)
        self.assertIn("function buildSnowflakeCandidateMappings(catalog, sourceId, targetId)", html)
        self.assertIn('id="database-select" multiple', html)
        self.assertIn('id="schema-select" multiple', html)
        self.assertIn("function mergeCatalogs(catalogs)", html)
        self.assertIn('id="generate-history-lineage"', html)
        self.assertIn('id="history-hours"', html)
        self.assertIn('"/api/live/snowflake-history-lineage"', html)
        self.assertIn('id="candidate-source-table"', html)
        self.assertIn('id="candidate-target-table"', html)
        self.assertIn('id="generate-candidate-mapping"', html)
        self.assertIn("Exact column-name match; candidate only", html)
        self.assertIn("not transformation lineage", html)
        self.assertNotIn('id="generate-live-mapping"', html)
        self.assertNotIn('id="snowflake-view-select"', html)
        self.assertIn('id="snowflake-candidate-panel"', html)
        with open(os.path.join(os.path.dirname(__file__), "requirements.txt"), encoding="utf-8") as source:
            requirements = source.read()
        self.assertIn('snowflake-connector-python', requirements)
        with open(os.path.join(os.path.dirname(__file__), "build_windows.ps1"), encoding="utf-8") as source:
            build_script = source.read()
        self.assertIn("--collect-submodules snowflake.connector", build_script)
        self.assertIn("Candidate source-to-target mappings", html)

    def test_page_does_not_reference_external_font_hosts(self):
        page = (os.path.join(os.path.dirname(__file__), "index.html"))
        with open(page, encoding="utf-8") as source:
            html = source.read()
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)

    def test_unreachable_local_api_explains_supported_page_url(self):
        page = os.path.join(os.path.dirname(__file__), "index.html")
        with open(page, encoding="utf-8") as source:
            html = source.read()
        self.assertIn("http://127.0.0.1:8765/", html)
        self.assertIn("Do not open index.html directly as a file", html)
        self.assertIn("Cannot reach the local DBConnect service", html)

    def test_old_server_snowflake_validation_error_has_restart_guidance(self):
        page = os.path.join(os.path.dirname(__file__), "index.html")
        with open(page, encoding="utf-8") as source:
            html = source.read()
        self.assertIn("function formatApiError(detail, status, payload)", html)
        self.assertIn("does not yet support Snowflake", html)
        self.assertIn("Restart the local DBConnect service", html)
        self.assertIn("formatApiError(data.detail, response.status, payload)", html)


class LauncherTests(unittest.TestCase):
    def test_installer_build_waits_and_publishes_only_a_new_artifact(self):
        with open(os.path.join(os.path.dirname(__file__), "build_windows.ps1"), encoding="utf-8") as source:
            script = source.read()
        self.assertIn("INNO_SETUP_COMPILER", script)
        self.assertIn("& $iscc", script)
        self.assertNotIn("iexpress", script.casefold())
        self.assertIn("[guid]::NewGuid()", script)
        self.assertIn("Move-Item -Path $stagedInstallerPath -Destination $installerPath -Force", script)

    def test_inno_installer_is_per_user_and_includes_launcher_and_uninstaller(self):
        with open(os.path.join(os.path.dirname(__file__), "installer", "DatabaseOfflineLineage.iss"), encoding="utf-8") as source:
            script = source.read()
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\DatabaseOfflineLineage", script)
        self.assertIn('Source: "{#AppSourceDir}\\DatabaseOfflineLineage.exe"', script)
        self.assertIn("Filename: \"{uninstallexe}\"", script)
        self.assertIn("Flags: nowait postinstall skipifsilent", script)
        self.assertIn("Uninstall\\DatabaseOfflineLineage", script)

    def test_windowed_launcher_starts_without_standard_streams(self):
        launcher = DesktopLauncher.__new__(DesktopLauncher)
        launcher.root = Mock()
        with patch.object(DesktopLauncher, "_port_is_open", return_value=False), \
                patch("launcher.uvicorn.Server") as server_factory, \
                patch("launcher.threading.Thread") as thread_factory, \
                patch("sys.stdout", None), patch("sys.stderr", None):
            launcher._start_service()
        config = server_factory.call_args.args[0]
        self.assertIsNone(config.log_config)
        self.assertFalse(config.access_log)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8765)
        thread_factory.return_value.start.assert_called_once()
        launcher.root.after.assert_called_once_with(100, launcher._wait_until_ready)

    def test_sql_server_odbc_detection_requires_a_supported_driver(self):
        self.assertTrue(has_sql_server_odbc_drivers(["ODBC Driver 18 for SQL Server"]))
        self.assertTrue(has_sql_server_odbc_drivers(["ODBC Driver 17 for SQL Server"]))
        self.assertFalse(has_sql_server_odbc_drivers(["PostgreSQL Unicode(x64)"]))


if __name__ == "__main__":
    unittest.main()