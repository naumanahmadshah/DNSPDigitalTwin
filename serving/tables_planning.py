"""What the lakehouse serves for Plan and invest: the planning marts synced into the app's Lakebase project (schema serving, same table
name), with their primary keys. The planning assumptions, carbon factors and unit costs are small reference marts shown on the screen.

SERVING holds the marts that always exist. TRANSMISSION_SERVING holds the two transmission-level marts: they are built only when the
pipeline conf atlas.transmission_planning is true (after gld_transmission_substation is synced), so merge them into SERVING at the same time.
Used by ops/lakebase.py exactly like serving/tables.py SERVING."""

SERVING = {
    "gld_plan_assumption": ["key"],
    "gld_plan_zone_demand": ["zs_id", "fiscal_year"],
    "gld_plan_constraint": ["constraint_id"],
    "gld_plan_intervention": ["intervention_id"],
    "gld_plan_bundle": ["bundle_id"],
    "gld_plan_region_capacity": ["region_id"],
    # who a planned outage at an isolation point takes off supply (the customer notices read it)
    "gld_isolation_ds": ["device_id", "ds_id"],
}

TRANSMISSION_SERVING = {
    "gld_plan_constraint_transmission": ["constraint_id"],
    "gld_plan_intervention_transmission": ["intervention_id"],
}

# Indexes on the synced planning marts (plain b-tree; the screen filters on these columns)
INDEXES = [
    "CREATE INDEX IF NOT EXISTS gld_plan_intervention_constraint ON gld_plan_intervention (constraint_id)",
    "CREATE INDEX IF NOT EXISTS gld_plan_constraint_feeder ON gld_plan_constraint (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_plan_bundle_feeder ON gld_plan_bundle (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_plan_zone_demand_zone ON gld_plan_zone_demand (zs_id, years_out)",
    "CREATE INDEX IF NOT EXISTS gld_isolation_ds_device ON gld_isolation_ds (device_id)",
]
