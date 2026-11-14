"""The customers gold marts the app reads, synced into the app's Lakebase project (same format as serving/tables.py). The Being connected
tab reads these four; the Existing customers tab reads the synced gld_nmi and gld_outage already in serving/tables.py, and the app's
own atlas.live_outage and atlas.notification_outbox records. Merged into serving/tables.py by the lead."""

# table -> primary key columns (the source is <catalog>.<schema>.<table>; the Postgres copy is serving.<table>)
SERVING = {
    "gld_connection_application": ["application_id"],
    "gld_connection_stage_event": ["application_id", "stage_seq"],
    "gld_connection_stage": ["stage"],
}

# PostGIS-free indexes the screen filters and orders on
INDEXES = [
    "CREATE INDEX IF NOT EXISTS gld_connection_application_stage ON gld_connection_application (is_open, stage, stuck)",
    "CREATE INDEX IF NOT EXISTS gld_connection_application_zs ON gld_connection_application (zs_id, feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_connection_stage_event_app ON gld_connection_stage_event (application_id, stage_no)",
]
