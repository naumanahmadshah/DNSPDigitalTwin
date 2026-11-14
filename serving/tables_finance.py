"""Network financial marts synced from the lakehouse into the app's Lakebase project (same format as serving/tables.py): revenue, costs
and profit lines per month for the executive view (pipelines/finance.py)."""

# table -> primary key columns (the source is <catalog>.<schema>.<table>; the Postgres copy is serving.<table>)
SERVING = {
    "gld_finance_month": ["month", "line"],   # one row per month and profit and loss line, actual against budget
}

INDEXES = []
