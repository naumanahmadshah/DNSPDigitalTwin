"""Vegetation management marts synced from the lakehouse into the app's Lakebase project (same format as serving/tables.py): merged into
SERVING and INDEXES by the lead. The app reads these by bare name (the synced gold schema leads the search path). The app's own
vegetation records live in schema rm (serving/lakebase_vegetation.sql), not here."""

# table -> primary key columns (the source is <catalog>.<schema>.<table>; the Postgres copy is serving.<table>)
SERVING = {
    "gld_veg_bay": ["bay_id"],                                   # one row per overhead span (bay) from the latest LiDAR campaign
    "gld_veg_feeder": ["feeder_id"],                             # risk per feeder rolled up from the bays
    "gld_veg_survey_coverage": ["campaign_year", "zs_id"],       # LiDAR survey coverage per campaign and zone substation
    "gld_veg_plan_baseline": ["bay_id"],                         # the deterministic seasonal plan: month and sequence per bay
}

# Lookup indexes over the synced vegetation marts (plain btree: the app filters by feeder, zone and risk tier)
INDEXES = [
    "CREATE INDEX IF NOT EXISTS gld_veg_bay_tier ON gld_veg_bay (risk_tier, zs_id)",
    "CREATE INDEX IF NOT EXISTS gld_veg_bay_feeder ON gld_veg_bay (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_veg_plan_baseline_tier ON gld_veg_plan_baseline (risk_tier, plan_seq)",
    "CREATE INDEX IF NOT EXISTS gld_veg_plan_baseline_feeder ON gld_veg_plan_baseline (feeder_id)",
]
