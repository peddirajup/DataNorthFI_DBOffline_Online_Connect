"""Static source-to-target mapping for offline SQL Server, Oracle, and Snowflake scripts."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp


DIALECTS = {"sqlserver": "tsql", "oracle": "oracle", "snowflake": "snowflake"}
_SQLGLOT_PARSE_ERRORS = (sqlglot.errors.ParseError, sqlglot.errors.TokenError)
MAX_FILES = 50
MAX_FILE_CHARS = 2_000_000
_SQLSERVER_SCRIPT_DIRECTIVE = re.compile(
    r"(?im)^[ \t]*(?:GO(?:[ \t]+\d+)?|USE\b[^\r\n]*|"
    r"SET[ \t]+(?:ANSI_NULLS|QUOTED_IDENTIFIER)[ \t]+ON)[ \t]*(?:--[^\r\n]*)?\r?$"
)
_SQLSERVER_PROCEDURE_HEADER = re.compile(
    r"^[ \t]*(?:CREATE[ \t]+(?:OR[ \t]+ALTER[ \t]+)?|ALTER[ \t]+)"
    r"(?:PROC|PROCEDURE)\b.*?\bAS\b(?:[ \t\r\n]+(?P<outer_begin>BEGIN)\b)?",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_SQLSERVER_PROCEDURE_END = re.compile(r"(?im)^[ \t]*END[ \t]*;?[ \t]*(?:--[^\r\n]*)?\r?$")
_SQLSERVER_WRITE_START = re.compile(r"(?im)^[ \t]*(?:INSERT[ \t]+INTO\b|UPDATE\b)")
_SQLSERVER_INSERT_END = re.compile(
    r"(?im)^[ \t]*(?:SELECT[ \t]+(?:CheckNotifyId\b|"
    r"(?:TOP(?:[ \t]*\([^)]*\)|[ \t]+\d+)[ \t]+)?@\w+[ \t]*=)|RETURN\b|END\b|"
    r"DROP[ \t]+TABLE\b|TRUNCATE[ \t]+TABLE\b|UPDATE\b|DELETE\b)"
)


def _parse_sqlserver_procedure_writes(content: str) -> list[exp.Expression | None]:
    write_starts = list(_SQLSERVER_WRITE_START.finditer(content))
    statements: list[exp.Expression | None] = []
    for index, write in enumerate(write_starts):
        end = write_starts[index + 1].start() if index + 1 < len(write_starts) else len(content)
        boundary = _SQLSERVER_INSERT_END.search(content, write.end(), end)
        if boundary:
            end = boundary.start()
        try:
            statements.extend(sqlglot.parse(content[write.start():end].strip(), read="tsql"))
        except _SQLGLOT_PARSE_ERRORS:
            continue
    return statements


def _parse_sql(content: str, dialect: str) -> list[exp.Expression | None]:
    if dialect != "tsql":
        return sqlglot.parse(content, read=dialect)

    content = _SQLSERVER_SCRIPT_DIRECTIVE.sub("", content)
    procedure = _SQLSERVER_PROCEDURE_HEADER.search(content)
    if procedure:
        content = content[procedure.end():]
        if procedure.group("outer_begin"):
            procedure_ends = list(_SQLSERVER_PROCEDURE_END.finditer(content))
            if procedure_ends:
                procedure_end = procedure_ends[-1]
                content = content[:procedure_end.start()] + ";\n" + content[procedure_end.end():]
        content = re.sub(
            r"(?im)(?<!;)(?<=\S)((?:\r?\n[ \t]*)+)(?=(?:INSERT|UPDATE|DELETE|TRUNCATE|MERGE)\b)",
            r";\1",
            content,
        )
        if not procedure.group("outer_begin"):
            return _parse_sqlserver_procedure_writes(content)

    try:
        return sqlglot.parse(content, read=dialect)
    except _SQLGLOT_PARSE_ERRORS:
        if procedure:
            return _parse_sqlserver_procedure_writes(content)
        raise


def _table_name(table: exp.Table) -> str:
    parts = [part.name for part in (table.args.get("catalog"), table.args.get("db"), table.this)
             if isinstance(part, exp.Identifier) and part.name]
    return ".".join(parts) or table.name


def _write_target(statement: exp.Expression) -> tuple[str | None, list[str], exp.Expression | None]:
    if isinstance(statement, exp.Insert):
        target = statement.this
        target_columns: list[str] = []
        if isinstance(target, exp.Schema):
            target_columns = [column.name for column in target.expressions if isinstance(column, exp.Identifier)]
            target = target.this
        query = statement.expression
        if isinstance(query, exp.Subquery):
            query = query.this
        return (_table_name(target) if isinstance(target, exp.Table) else None,
                target_columns, query)

    if (isinstance(statement, exp.Create) and isinstance(statement.this, exp.Table)
            and statement.args.get("kind", "TABLE").upper() in {"TABLE", "VIEW"}):
        return _table_name(statement.this), [], statement.expression

    return None, [], None


def _source_table_names(query: exp.Expression) -> list[str]:
    result: list[str] = []
    for table in query.find_all(exp.Table):
        name = _table_name(table)
        if name and name not in result:
            result.append(name)
    return result


def _source_aliases(query: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in query.find_all(exp.Table):
        name = _table_name(table)
        aliases[table.name.casefold()] = name
        if table.alias:
            aliases[table.alias.casefold()] = name
    return aliases


def _projection_name(projection: exp.Expression, index: int) -> str:
    name = projection.alias_or_name
    return str(name) if name else f"column_{index + 1}"


def analyze_files(files: list[dict[str, str]], dialect: str) -> dict[str, Any]:
    """Parse uploaded SQL text and return mappings and a table relationship graph."""
    if dialect not in DIALECTS:
        raise ValueError("Choose SQL Server, Oracle, or Snowflake for offline analysis.")
    if not files:
        raise ValueError("Select at least one SQL file.")
    if len(files) > MAX_FILES:
        raise ValueError(f"Select no more than {MAX_FILES} files at once.")

    sql_dialect = DIALECTS[dialect]
    mappings: list[dict[str, Any]] = []
    relationships: dict[tuple[str, str], dict[str, str]] = {}
    tables: set[str] = set()
    file_results: list[dict[str, Any]] = []

    for file in files:
        name = str(file.get("name", "Untitled SQL file"))[:255]
        content = str(file.get("content", ""))
        if len(content) > MAX_FILE_CHARS:
            file_results.append({"name": name, "status": "Skipped", "mapped_columns": 0,
                                 "message": "File exceeds the 2 MB text limit."})
            continue
        try:
            statements = [statement for statement in _parse_sql(content, sql_dialect) if statement]
        except _SQLGLOT_PARSE_ERRORS:
            file_results.append({"name": name, "status": "Could not parse", "mapped_columns": 0,
                                 "message": f"Could not parse this file as {dialect.upper()} SQL."})
            continue

        file_mapping_count = 0
        file_warnings: list[str] = []
        for statement in statements:
            if isinstance(statement, exp.Update) and isinstance(statement.this, exp.Table):
                target = _table_name(statement.this)
                source_names = [
                    name for name in _source_table_names(statement)
                    if name.casefold() != target.casefold()
                ]
                source_aliases = _source_aliases(statement)
                tables.add(target)
                tables.update(source_names)

                for assignment in statement.expressions:
                    if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
                        continue
                    target_column = assignment.this.name
                    value = assignment.expression
                    query = value.this if isinstance(value, exp.Subquery) else value
                    projections = query.expressions if isinstance(query, exp.Select) else [query]
                    for projection in projections:
                        references: list[tuple[str, str]] = []
                        for column in projection.find_all(exp.Column):
                            qualifier = column.table
                            if qualifier:
                                source_table = source_aliases.get(qualifier.casefold(), f"{qualifier}.{column.name}")
                            elif len(source_names) == 1:
                                source_table = source_names[0]
                            else:
                                source_table = "Unresolved source"
                            reference = (source_table, column.name)
                            if reference not in references:
                                references.append(reference)

                        if not references:
                            references = [("Static or system value", "-")]
                        for source_table, source_column in references:
                            mappings.append({
                                "file": name,
                                "source_table": source_table,
                                "source_column": source_column,
                                "target_table": target,
                                "target_column": target_column,
                                "transformation": projection.sql(dialect=sql_dialect),
                                "kind": "static" if source_table == "Static or system value" else "sql",
                            })
                            file_mapping_count += 1
                            if source_table not in {"Static or system value", "Unresolved source"}:
                                relationships[(source_table, target)] = {
                                    "source": source_table, "target": target, "kind": "sql_mapping",
                                }
                continue

            target, insert_columns, query = _write_target(statement)
            if not target or not isinstance(query, exp.Select):
                continue

            source_names = _source_table_names(query)
            source_aliases = _source_aliases(query)
            tables.add(target)
            tables.update(source_names)
            projections = query.expressions
            for index, projection in enumerate(projections):
                target_column = insert_columns[index] if index < len(insert_columns) else _projection_name(projection, index)
                references: list[tuple[str, str]] = []
                for column in projection.find_all(exp.Column):
                    qualifier = column.table
                    if qualifier:
                        source_table = source_aliases.get(qualifier.casefold(), f"{qualifier}.{column.name}")
                    elif len(source_names) == 1:
                        source_table = source_names[0]
                    else:
                        source_table = "Unresolved source"
                    reference = (source_table, column.name)
                    if reference not in references:
                        references.append(reference)

                if not references:
                    references = [("Static or system value", "-")]
                for source_table, source_column in references:
                    mappings.append({
                        "file": name,
                        "source_table": source_table,
                        "source_column": source_column,
                        "target_table": target,
                        "target_column": target_column,
                        "transformation": projection.sql(dialect=sql_dialect),
                        "kind": "static" if source_table == "Static or system value" else "sql",
                    })
                    file_mapping_count += 1
                    if source_table not in {"Static or system value", "Unresolved source"}:
                        relationships[(source_table, target)] = {
                            "source": source_table, "target": target, "kind": "sql_mapping",
                        }

            if not source_names:
                file_warnings.append("Statement target was found, but no source table could be identified.")

        file_results.append({
            "name": name,
            "status": "Parsed" if file_mapping_count or statements else "No supported write found",
            "mapped_columns": file_mapping_count,
            "message": "; ".join(file_warnings),
        })

    file_inputs: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}
    file_outputs: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}
    for mapping in mappings:
        file_name = mapping["file"]
        target_table = mapping["target_table"]
        target_columns = file_outputs.setdefault(file_name, {}).setdefault(
            target_table.casefold(), (target_table, {})
        )[1]
        target_column = mapping["target_column"]
        if target_column != "-":
            target_columns[target_column.casefold()] = target_column
        source_table = mapping["source_table"]
        if source_table not in {"Static or system value", "Unresolved source"}:
            source_columns = file_inputs.setdefault(file_name, {}).setdefault(
                source_table.casefold(), (source_table, {})
            )[1]
            source_column = mapping["source_column"]
            if source_column != "-":
                source_columns[source_column.casefold()] = source_column

    file_dependencies: list[dict[str, Any]] = []
    for source_file, written_tables in file_outputs.items():
        for target_file, read_tables in file_inputs.items():
            if source_file.casefold() == target_file.casefold():
                continue
            for normalized_table, (written_table, written_columns) in written_tables.items():
                if normalized_table in read_tables:
                    _, read_columns = read_tables[normalized_table]
                    via_columns = [
                        {"writer_column": writer_column, "reader_column": read_columns[normalized_column]}
                        for normalized_column, writer_column in written_columns.items()
                        if normalized_column in read_columns
                    ]
                    file_dependencies.append({
                        "source_file": source_file,
                        "target_file": target_file,
                        "via_table": written_table,
                        "via_columns": via_columns,
                    })

    return {
        "dialect": dialect,
        "files": file_results,
        "tables": sorted(tables),
        "relationships": list(relationships.values()),
        "file_dependencies": file_dependencies,
        "mappings": mappings,
        "summary": {"table_count": len(tables), "relationship_count": len(relationships),
                    "mapped_columns": len(mappings),
                    "file_dependency_count": len(file_dependencies)},
    }