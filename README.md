# Database & Offline Lineage

This is a separate local web app. It does not modify or depend on the browser
generator at `../STTM_Data_Lineage_Generator/app/browser/skillfile_generator.html`.

## Start

From this folder, install the Python dependencies and start the local service:

```powershell
py -3 -m pip install -r requirements.txt
py -3 server.py
```

Then open <http://127.0.0.1:8765>.

## Windows installer

Testers do not need Python installed. A Windows build machine can create a
per-user installer with `build_windows.ps1`; it bundles the app and Python
runtime, adds Start Menu and desktop shortcuts, starts the service
only on loopback, and opens the browser. The build requires Python 3.14 and
Inno Setup 6.3 or later on the build machine only. Run the script from PowerShell
in this folder. Regression tests run before packaging. The installer is
written to `dist/installer/DatabaseOfflineLineage-Setup.exe`.
The build waits for the Inno compiler and publishes a newly staged installer only after
successful completion, so an older output cannot masquerade as a successful build.
It checks PATH and standard Inno installation folders. For an approved compiler
installed elsewhere, set `INNO_SETUP_COMPILER` to the full path of `ISCC.exe`:

```powershell
$env:INNO_SETUP_COMPILER = 'C:\Approved Tools\Inno Setup 6\ISCC.exe'
.\build_windows.ps1
```

When software installation is restricted by company policy, ask IT to provide
the compiler; do not bypass that policy. A missing compiler stops the build
before packaging. Do not distribute an existing installer after a failed build.
The installer is per-user, requires no administrator rights for the app, provides
standard uninstall support, and offers a desktop shortcut. Upgrading removes the
legacy per-user uninstall registration and replaces its shortcut; other software
and database settings are untouched. Legacy install scripts remain for reference
but are no longer packaged or executed.

### Windowed startup error

If a deployed executable reports `NoneType ... isatty` followed by
`Unable to configure formatter 'default'`, it is using Uvicorn's console logging
configuration without console streams. The current launcher disables that
configuration with `log_config=None`; a regression test covers missing stdout
and stderr. Rebuild with `./build_windows.ps1` and distribute the newly generated
installer. Close the installed app before reinstalling. Editing local Python
source does not update an executable already installed on another machine.

Microsoft ODBC Driver 17 or 18 is a separate prerequisite for Live SQL Server
mode. Oracle uses `python-oracledb` thin mode and does not require Oracle
Instant Client. Snowflake uses `snowflake-connector-python`; live access needs
an account identifier, network access, a warehouse, credentials, and a role
with metadata visibility. When SQL Server's ODBC driver is unavailable,
Snowflake and offline modes remain available.

## Modes

- **Offline files** parses SQL Server, Oracle, or Snowflake `.sql`/`.txt` scripts locally,
    producing table-level structure and column mappings from INSERT, UPDATE,
    CREATE VIEW, and CTAS SELECT statements. It can also show SQL-file
    dependencies when one analyzed file writes a table another reads. These
    links include column-name matches (case-insensitive) when available; renamed
    columns are not inferred. Shared-table evidence does not establish execution
    order or runtime behavior. It does not connect to a database.
- **Live connection** checks credentials and retrieves accessible database,
  schema, table, column, and declared foreign-key metadata. It does not query
  table rows or execute DDL/DML. Live relationships are explicitly labeled as
    key structure, not transformation lineage; use source SQL/ETL files to
    inspect transformations. Snowflake catalog access reads visible tables/views
    and columns from each selected database's `INFORMATION_SCHEMA`. Its standard
    table primary/foreign-key constraints are generally informational and may
    not be enforced; key metadata is shown only when visible to the selected
    role. A usable warehouse is required.
    The live Snowflake catalog also offers
    an explicit candidate-mapping view for a chosen source/target table pair. It
    lists exact column-name matches and declared key-column pairs as structural
    candidates only; it cannot infer expressions, transformations, or actual data
    movement. Verify every candidate against source SQL/ETL before treating it as
    a mapping.
    The dedicated view-selection mapping workflow is no longer offered in the UI.
    Use Offline files with source SQL for transformation mappings.

### Multiple databases and schemas

After connecting to Snowflake or SQL Server, select multiple accessible databases
in the Databases list (Ctrl-click on Windows). The app loads each catalog and
combines tables using database-qualified names. Choose multiple database-qualified
schemas in the Schemas list to filter the table selection, or choose All schemas.
Generate lineage uses the checked tables currently visible under these filters.
Unchecked table choices persist when schema or search filters change. Changing
databases reloads the catalog and resets table choices; any catalog-read failure
prevents generation from a partial or stale catalog.

Snowflake cross-database declared keys retain both endpoint database names; both
tables must be selected to display the relationship. SQL Server foreign keys
remain database-local. Oracle exposes the connected service only, with multiple
visible schemas; this does not connect to multiple Oracle services. No engine
infers transformations or undisclosed relationships from multiple selections.

### Snowflake query-history lineage

For checked tables, **Analyze query-history lineage** reads successful SQL from
`INFORMATION_SCHEMA.QUERY_HISTORY` and derives source-to-target evidence separately
from declared keys. Choose a 1-168 hour window (default: seven days). The read is
bounded to the most recent 1,000 visible queries, with up to 50 loading statements
analyzed. The seven-day window uses a one-minute retention-boundary margin to
avoid Snowflake rejecting timestamps at its exact cutoff. This function is
account/session-history scoped, not restricted to the
database used to invoke it; returned mappings and edges are filtered to selected
tables. Older relevant queries may be omitted by these limits.

Unqualified names use each query's recorded database/schema context. Supported
INSERT SELECT, UPDATE, and CTAS statements use the existing SQL parser. MERGE,
wildcard projections, CTEs, nested queries, scripts, and procedural/dynamic SQL
are not expanded in this workflow; query IDs and analysis limitations are shown
in the results. The app never re-executes historical SQL or reads table rows.
Query history visibility depends on the active role (own queries may be all that
is visible). No edge does not prove that no pipeline exists, and SQL-derived
evidence does not verify row movement. History SQL and credentials are not saved.

## Security and permissions

The service binds only to loopback (`127.0.0.1:8765`) and rejects cross-origin
requests. Credentials are held in page memory and sent to the local service
only for a connection/catalog request; they are not stored in browser storage,
files, or logs. Disconnect clears the form. The service returns generic
connection errors rather than driver messages that could contain sensitive
details.

Use a dedicated read-only account with only the metadata visibility required
for the chosen engine. SQL Server needs access to the selected database and
visibility of its `sys.tables`, `sys.schemas`, `sys.columns`, `sys.types`, and
foreign-key catalog views. Oracle reads `ALL_TABLES`, `ALL_TAB_COLUMNS`, and
foreign-key constraint views visible to the account. No account needs table-row
SELECT privileges for the catalog view.

Offline file contents are posted only to this local service for parsing and
are not saved. Do not use a shared/network-exposed host for the service.

## Tests

```powershell
py -3 -m unittest .\test_database_app.py
```

Live connectivity still needs a reachable SQL Server, Oracle, or Snowflake instance; the
included tests mock catalog rows and do not use real credentials or servers.