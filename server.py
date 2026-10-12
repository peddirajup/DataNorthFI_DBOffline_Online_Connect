"""Local-only API for offline SQL analysis and read-only database catalogs."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, SecretStr
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify_tables import qualify_tables

from offline_lineage import analyze_files


BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ALLOWED_HOSTS = {"127.0.0.1:8765", "localhost:8765"}
app = FastAPI(title="Offline and Database Lineage", docs_url=None, redoc_url=None)


class ConnectionRequest(BaseModel):
    engine: Literal["sqlserver", "oracle", "snowflake"]
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr
    database: str | None = Field(default=None, max_length=256)
    service_name: str | None = Field(default=None, max_length=256)
    account: str | None = Field(default=None, max_length=255)
    warehouse: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    driver: Literal["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"] = "ODBC Driver 18 for SQL Server"
    trust_server_certificate: bool = False


class CatalogRequest(ConnectionRequest):
    database: str | None = Field(default=None, max_length=256)


class SnowflakeHistoryRequest(ConnectionRequest):
    engine: Literal["snowflake"] = "snowflake"
    databases: list[str] = Field(min_length=1, max_length=20)
    tables: list[str] = Field(min_length=1, max_length=500)
    hours: int = Field(default=168, ge=1, le=168)
    query_limit: int = Field(default=50, ge=1, le=50)


class OfflineFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str


class OfflineAnalysisRequest(BaseModel):
    dialect: Literal["sqlserver", "oracle", "snowflake"]
    files: list[OfflineFile] = Field(min_length=1, max_length=50)


class SnowflakeViewMappingRequest(BaseModel):
    account: str = Field(min_length=1, max_length=255)
    warehouse: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr
    database: str = Field(min_length=1, max_length=256)
    role: str | None = Field(default=None, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    views: list[str] = Field(min_length=1, max_length=50)


def _clean_connection_text(value: str, label: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise HTTPException(status_code=422, detail=f"Enter a valid {label}.")
    return value


def _sqlserver_connection(request: ConnectionRequest, database: str):
    try:
        import pyodbc
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="SQL Server support is not installed. Install the requirements and Microsoft ODBC Driver 17 or 18.",
        ) from exc

    host = _clean_connection_text(request.host or "", "server host")
    username = _clean_connection_text(request.username, "username")
    password = request.password.get_secret_value()

    def odbc(value: str) -> str:
        return "{" + value.replace("}", "}}") + "}"

    server = f"{host},{request.port or 1433}"
    connection_string = (
        f"DRIVER={odbc(request.driver)};SERVER={odbc(server)};DATABASE={odbc(database)};"
        f"UID={odbc(username)};PWD={odbc(password)};Encrypt=yes;"
        f"TrustServerCertificate={'yes' if request.trust_server_certificate else 'no'};"
        "Connection Timeout=8;"
    )
    try:
        return pyodbc.connect(connection_string, timeout=8)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to SQL Server. Check the host, port, credentials, database access, network, and ODBC driver.",
        ) from exc


def _oracle_connection(request: ConnectionRequest):
    try:
        import oracledb
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="Oracle support is not installed. Install the requirements.") from exc

    host = _clean_connection_text(request.host or "", "host")
    username = _clean_connection_text(request.username, "username")
    service_name = _clean_connection_text(request.service_name or "", "Oracle service name")
    try:
        dsn = oracledb.makedsn(host, request.port or 1521, service_name=service_name)
        return oracledb.connect(
            user=username,
            password=request.password.get_secret_value(),
            dsn=dsn,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Oracle. Check the host, port, service name, credentials, network, and database permissions.",
        ) from exc


def _snowflake_connection(request: ConnectionRequest, database: str | None = None):
    try:
        import snowflake.connector
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="Snowflake support is not installed. Install the requirements and restart the local service.",
        ) from exc

    account = _clean_connection_text(request.account or "", "Snowflake account identifier")
    username = _clean_connection_text(request.username, "username")
    warehouse = _clean_connection_text(request.warehouse or "", "Snowflake warehouse")
    connection_options = {
        "account": account,
        "user": username,
        "password": request.password.get_secret_value(),
        "warehouse": warehouse,
        "login_timeout": 15,
        "network_timeout": 15,
        "application": "DataNorthLineage",
    }
    selected_database = database or request.database
    if selected_database:
        connection_options["database"] = _clean_connection_text(selected_database, "database")
    if request.schema_name:
        connection_options["schema"] = _clean_connection_text(request.schema_name, "schema")
    if request.role:
        connection_options["role"] = _clean_connection_text(request.role, "role")
    try:
        return snowflake.connector.connect(**connection_options)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Snowflake. Check the account identifier, credentials, warehouse, role, network, and database permissions.",
        ) from exc


def _connection(request: ConnectionRequest, database: str | None = None):
    if request.engine == "sqlserver":
        selected_database = _clean_connection_text(database or request.database or "master", "database")
        return _sqlserver_connection(request, selected_database)
    if request.engine == "oracle":
        return _oracle_connection(request)
    return _snowflake_connection(request, database or request.database)


@app.middleware("http")
async def restrict_to_local_browser(request: Request, call_next):
    host = request.headers.get("host", "").lower()
    if host not in ALLOWED_HOSTS:
        return PlainTextResponse("This service accepts local connections only.", status_code=403)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.netloc.lower() != host:
            return PlainTextResponse("Cross-origin requests are not accepted.", status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ready", "database_access": "read-only metadata only"}


@app.post("/api/offline/analyze")
def analyze_offline(request: OfflineAnalysisRequest):
    try:
        return analyze_files([item.model_dump() for item in request.files], request.dialect)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/live/snowflake-view-mapping")
def snowflake_view_mapping(request: SnowflakeViewMappingRequest):
    connection = _snowflake_connection(request, request.database)
    files: list[dict[str, str]] = []
    unavailable_views: set[str] = set()
    try:
        cursor = connection.cursor()
        try:
            for view_name in request.views:
                view_name = _clean_connection_text(view_name, "view name")
                parts = view_name.split(".")
                if len(parts) == 2 and all(parts):
                    view_name = f"{request.database.strip()}.{view_name}"
                    parts = view_name.split(".")
                if len(parts) != 3 or any(not part for part in parts):
                    raise HTTPException(status_code=422, detail="Choose a schema.view or database.schema.view name.")
                if parts[0].casefold() != request.database.strip().casefold():
                    raise HTTPException(status_code=422, detail="Selected views must belong to the connected database.")
                try:
                    cursor.execute("SELECT GET_DDL('VIEW', %s, TRUE)", (view_name,))
                    row = cursor.fetchone()
                    if not row or not row[0]:
                        raise ValueError("No view definition was returned.")
                    files.append({"name": view_name, "content": str(row[0])})
                except HTTPException:
                    raise
                except Exception:
                    unavailable_views.add(view_name)
                    files.append({"name": view_name, "content": ""})
        finally:
            cursor.close()
    finally:
        connection.close()

    try:
        result = analyze_files(files, "snowflake")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for file_result in result["files"]:
        if file_result["name"] in unavailable_views:
            file_result.update({
                "status": "Definition unavailable",
                "message": "The view definition could not be read. Check GET_DDL privileges and object visibility for the active Snowflake role.",
            })
    return result


@app.post("/api/connect")
def connect(request: ConnectionRequest):
    selected_database = request.database or ("master" if request.engine == "sqlserver" else None)
    connection = _connection(request, selected_database)
    try:
        cursor = connection.cursor()
        try:
            if request.engine == "sqlserver":
                cursor.execute("SELECT DB_NAME()")
                current_database = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT name FROM sys.databases WHERE state = 0 AND HAS_DBACCESS(name) = 1 ORDER BY name"
                )
                databases = [row[0] for row in cursor.fetchall()]
            elif request.engine == "oracle":
                cursor.execute("SELECT SYS_CONTEXT('USERENV', 'DB_NAME'), SYS_CONTEXT('USERENV', 'SERVICE_NAME') FROM DUAL")
                current_database, current_service = cursor.fetchone()
                databases = [current_service or request.service_name]
                current_warehouse = None
                current_role = None
            else:
                cursor.execute("SELECT CURRENT_DATABASE(), CURRENT_WAREHOUSE(), CURRENT_ROLE()")
                current_database, current_warehouse, current_role = cursor.fetchone()
                cursor.execute("SHOW DATABASES")
                databases = [str(row[1] if len(row) > 1 else row[0]) for row in cursor.fetchall()]
            response = {
                "engine": request.engine,
                "connected_database": current_database or request.database,
                "databases": databases,
            }
            if request.engine == "snowflake":
                response.update({"warehouse": current_warehouse, "role": current_role})
            return response
        finally:
            cursor.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Connected, but could not read database metadata.") from exc
    finally:
        connection.close()


def _history_lineage(rows, selected_tables: list[str], query_limit: int) -> dict:
    selected = set(selected_tables)
    mappings = []
    relationships = {}
    files = []
    warnings = []
    writes = [row for row in rows if row[4] in {"INSERT", "UPDATE", "MERGE", "CREATE_TABLE", "CREATE_TABLE_AS_SELECT"}]
    if len(rows) >= 1000 or len(writes) > query_limit:
        warnings.append("History results reached a limit; older loading statements may be omitted.")
    for query_id, text, database, schema, query_type in writes[:query_limit]:
        query_id = str(query_id)
        try:
            if query_type == "MERGE":
                raise ValueError("MERGE mappings are not supported by this parser.")
            if len(text or "") > 2_000_000:
                raise ValueError("SQL exceeds the 2 MB parsing limit.")
            statements = [statement for statement in sqlglot.parse(text, read="snowflake") if statement]
            if len(statements) != 1:
                raise ValueError("Multi-statement SQL is not expanded.")
            statement = statements[0]
            if statement.find(exp.With) or statement.find(exp.Subquery):
                raise ValueError("CTEs and nested queries require review and are not expanded in history mode.")
            if any(projection.is_star for query in statement.find_all(exp.Select) for projection in query.expressions):
                raise ValueError("Wildcard projections require column expansion and are not mapped in history mode.")
            for table in statement.find_all(exp.Table):
                if (not table.catalog and not database) or (not table.db and not schema):
                    raise ValueError("Recorded database/schema context is missing for an unqualified table.")
            qualify_tables(statement, db=exp.to_identifier(schema, quoted=True) if schema else None,
                           catalog=exp.to_identifier(database, quoted=True) if database else None)
            for identifier in statement.find_all(exp.Identifier):
                if not identifier.args.get("quoted"):
                    identifier.set("this", identifier.name.upper())
            parsed = analyze_files([{"name": query_id, "content": statement.sql(dialect="snowflake")}], "snowflake")
            if not parsed["mappings"]:
                raise ValueError("No supported column mappings found in this statement.")
            mappings.extend(mapping for mapping in parsed["mappings"] if mapping["target_table"] in selected
                            and (mapping["source_table"] in selected or mapping["kind"] == "static"))
            for edge in parsed["relationships"]:
                if edge["source"] in selected and edge["target"] in selected:
                    relationships[(edge["source"], edge["target"])] = edge
            files.extend(parsed["files"])
        except (ValueError, sqlglot.errors.SqlglotError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "SQL could not be parsed safely."
            warnings.append(f"{query_id}: {message}")
            files.append({"name": query_id, "status": "Skipped", "mapped_columns": 0, "message": message})
    tables = sorted({name for mapping in mappings for name in (mapping["source_table"], mapping["target_table"]) if name in selected})
    return {
        "dialect": "snowflake", "files": files, "tables": tables,
        "relationships": list(relationships.values()), "mappings": mappings, "file_dependencies": [],
        "warnings": warnings, "history_rows": len(rows),
        "summary": {"table_count": len(tables), "relationship_count": len(relationships), "mapped_columns": len(mappings)},
    }


@app.post("/api/live/snowflake-history-lineage")
def snowflake_history_lineage(request: SnowflakeHistoryRequest):
    databases = [_clean_connection_text(database, "database") for database in request.databases]
    for table in request.tables:
        parts = table.split(".")
        if len(parts) != 3 or not all(parts) or parts[0] not in databases:
            raise HTTPException(status_code=422, detail="Selected tables must be database.schema.table names within the selected databases.")
    quoted_database = '"' + databases[0].replace('"', '""') + '"'
    connection = _connection(request, databases[0])
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"""SELECT query_id, query_text, database_name, schema_name, query_type
                    FROM TABLE({quoted_database}.INFORMATION_SCHEMA.QUERY_HISTORY(
                        END_TIME_RANGE_START => DATEADD('minute', -%s, CURRENT_TIMESTAMP()),
                        RESULT_LIMIT => 1000))
                    WHERE LOWER(execution_status) = 'success'
                    ORDER BY start_time DESC""",
                (min(request.hours * 60, 10079),),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    except Exception as exc:
        error_code = getattr(exc, "errno", None)
        sqlstate = getattr(exc, "sqlstate", None)
        diagnostic = f" Snowflake error {error_code}" if isinstance(error_code, int) else ""
        if diagnostic and isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            diagnostic += f" (SQL state {sqlstate})"
        raise HTTPException(status_code=502, detail="Could not read Snowflake query history. Check the active role's history visibility and database permissions." + diagnostic) from exc
    finally:
        connection.close()
    result = _history_lineage(rows, request.tables, request.query_limit)
    result["hours"] = request.hours
    result["warnings"].insert(0, "Only successful SQL visible to this role in the requested window is analyzed. Missing history is not proof of no lineage; SQL parsing does not verify row movement.")
    return result


def _catalog_result(request: CatalogRequest, cursor) -> dict:
    key_databases: dict[tuple, dict[str, str]] = {}
    if request.engine == "sqlserver":
        database = request.database or "master"
        cursor.execute(
            """SELECT s.name, t.name, c.name, ty.name, c.is_nullable
               FROM sys.tables AS t
               JOIN sys.schemas AS s ON s.schema_id = t.schema_id
               JOIN sys.columns AS c ON c.object_id = t.object_id
               JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
               ORDER BY s.name, t.name, c.column_id"""
        )
        column_rows = cursor.fetchall()
        cursor.execute(
            """SELECT ps.name, pt.name, pc.name, rs.name, rt.name, rc.name, fk.name
               FROM sys.foreign_keys AS fk
               JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
               JOIN sys.tables AS pt ON pt.object_id = fkc.parent_object_id
               JOIN sys.schemas AS ps ON ps.schema_id = pt.schema_id
               JOIN sys.columns AS pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
               JOIN sys.tables AS rt ON rt.object_id = fkc.referenced_object_id
               JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
               JOIN sys.columns AS rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
               ORDER BY ps.name, pt.name, fk.name"""
        )
        foreign_keys = cursor.fetchall()
        database_name = database
    elif request.engine == "oracle":
        database_name = request.service_name or "Oracle service"
        cursor.execute(
            """SELECT c.owner, c.table_name, c.column_name, c.data_type, c.nullable
               FROM all_tab_columns c
               JOIN all_tables t ON t.owner = c.owner AND t.table_name = c.table_name
               ORDER BY c.owner, c.table_name, c.column_id"""
        )
        column_rows = cursor.fetchall()
        cursor.execute(
            """SELECT c.owner, c.table_name, cc.column_name,
                      p.owner, p.table_name, pc.column_name, c.constraint_name
               FROM all_constraints c
               JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
               JOIN all_constraints p ON p.owner = c.r_owner AND p.constraint_name = c.r_constraint_name
               JOIN all_cons_columns pc ON pc.owner = p.owner AND pc.constraint_name = p.constraint_name
                    AND pc.position = cc.position
               WHERE c.constraint_type = 'R'
               ORDER BY c.owner, c.table_name, c.constraint_name, cc.position"""
        )
        foreign_keys = cursor.fetchall()
        relationship_kind = "Declared foreign-key structure; this does not represent transformation lineage."
    else:
        database = _clean_connection_text(request.database or "", "database")
        quoted_database = '"' + database.replace('"', '""') + '"'
        information_schema = f"{quoted_database}.INFORMATION_SCHEMA"
        cursor.execute(
            f"""SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable, t.table_type
                FROM {information_schema}.COLUMNS AS c
                JOIN {information_schema}.TABLES AS t
                  ON t.table_catalog = c.table_catalog
                 AND t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                WHERE c.table_schema <> 'INFORMATION_SCHEMA'
                  AND t.table_type IN ('BASE TABLE', 'VIEW', 'EXTERNAL TABLE', 'MATERIALIZED VIEW', 'HYBRID TABLE')
                ORDER BY c.table_schema, c.table_name, c.ordinal_position"""
        )
        column_rows = cursor.fetchall()
        try:
            cursor.execute(f"SHOW IMPORTED KEYS IN DATABASE {quoted_database}")
            key_rows = cursor.fetchall()
            description = cursor.description or []
            column_names = [
                str(getattr(column, "name", column[0] if isinstance(column, (tuple, list)) else column)).casefold()
                for column in description
            ]
            foreign_keys = []
            for row in key_rows:
                values = dict(zip(column_names, row))
                fk_schema = values.get("fk_schema_name")
                fk_table = values.get("fk_table_name")
                fk_column = values.get("fk_column_name")
                pk_schema = values.get("pk_schema_name")
                pk_table = values.get("pk_table_name")
                pk_column = values.get("pk_column_name")
                constraint = values.get("fk_name")
                if all((fk_schema, fk_table, fk_column, pk_schema, pk_table, pk_column)):
                    key = (fk_schema, fk_table, fk_column, pk_schema, pk_table, pk_column, constraint,
                           values.get("pk_database_name") or database, values.get("fk_database_name") or database)
                    foreign_keys.append(key)
            relationship_kind = (
                "Declared Snowflake key metadata; standard-table PRIMARY KEY and FOREIGN KEY constraints "
                "are generally informational and may not be enforced. This is not transformation lineage."
            )
        except Exception:
            foreign_keys = []
            relationship_kind = (
                "Snowflake table and column metadata loaded, but imported-key metadata could not be read. "
                "Check role privileges and database scope; zero relationships is not verified. "
                "Standard-table key constraints may also be informational."
            )
        database_name = database

    tables: dict[str, dict] = {}
    schemas: set[str] = set()
    for column_row in column_rows:
        schema, table, column, data_type, nullable = column_row[:5]
        object_type = column_row[5] if len(column_row) > 5 else ("TABLE" if request.engine == "sqlserver" else "TABLE")
        schemas.add(schema)
        table_id = f"{schema}.{table}"
        entry = tables.setdefault(table_id, {"id": table_id, "schema": schema, "name": table, "object_type": object_type, "columns": []})
        entry["columns"].append({"name": column, "type": data_type, "nullable": bool(nullable in (True, "Y", "YES"))})

    relationships: dict[tuple, dict] = {}
    for foreign_key in foreign_keys:
        child_schema, child_table, child_column, parent_schema, parent_table, parent_column, constraint = foreign_key[:7]
        parent_id = f"{parent_schema}.{parent_table}"
        child_id = f"{child_schema}.{child_table}"
        key = (parent_id, child_id, parent_column, child_column, constraint)
        if len(foreign_key) > 7:
            key += foreign_key[7:]
            key_databases[key] = {"source_database": foreign_key[7], "target_database": foreign_key[8]}
        relationships[key] = {
            "source": parent_id, "target": child_id,
            "source_column": parent_column, "target_column": child_column,
            "constraint": constraint,
            **key_databases.get(key, {}),
        }

    return {
        "database": database_name,
        "schemas": sorted(schemas),
        "tables": sorted(tables.values(), key=lambda table: table["id"].casefold()),
        "relationships": list(relationships.values()),
        "relationship_kind": relationship_kind if request.engine == "snowflake" else "Foreign-key structure; this does not represent transformation lineage.",
    }


@app.post("/api/catalog")
def catalog(request: CatalogRequest):
    if request.engine in {"sqlserver", "snowflake"} and not request.database:
        raise HTTPException(status_code=422, detail="Choose a database before loading its catalog.")
    connection = _connection(request, request.database)
    try:
        cursor = connection.cursor()
        try:
            return _catalog_result(request, cursor)
        finally:
            cursor.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not read the catalog. Check metadata permissions and the selected database.",
        ) from exc
    finally:
        connection.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False, access_log=False)