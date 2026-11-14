"""What the lakehouse serves to the app: every gold table synced into the app's Lakebase project (schema serving, same table name),
its source table in the twin's schema and its primary key, plus the PostGIS indexes laid over them after each sync.

Used by ops/lakebase.py (create the synced tables, run the shared sync pipeline, index and grant). The app reads these tables only."""

# table -> primary key columns (the source is <catalog>.<schema>.<table>; the Postgres copy is serving.<table>)
SERVING = {
    # the network, its state and its history
    "gld_asset": ["asset_id"],
    "gld_feeder_state": ["feeder_id"],
    "gld_zone_supply": ["zs_id"],
    "gld_zone_transformer_state": ["transformer_id"],
    "gld_ds_state": ["ds_id"],
    "gld_feeder_tie": ["nop", "feeder_id"],
    "gld_suburb_supply": ["suburb", "postcode", "feeder_id"],
    "gld_outage": ["outage_id"],
    "gld_open_outage": ["outage_id"],
    "gld_reliability_year": ["regulatory_year"],
    "gld_reliability_daily": ["day"],
    "gld_spine_status": ["stop"],
    "gld_open_defect": ["notification_id"],
    "gld_work_order": ["order_id"],
    "gld_vegetation_risk": ["span_id"],
    "gld_drone_detection": ["detection_id"],
    "gld_dq_meter_mismatch": ["nmi"],
    "ref_label": ["domain", "code"],
    # field resource management reference data
    "gld_rm_region": ["region_id"],
    "gld_crew": ["crew_id"],
    "gld_certification": ["crew_id", "ticket_code"],
    "gld_crew_leave": ["crew_id", "leave_date"],
    "gld_crew_training": ["crew_id", "training_date"],
    "gld_safety_event": ["event_id"],
    "gld_eba_rule": ["role"],
    "gld_field_job": ["job_id"],
    "gld_rm_feeder": ["feeder_id"],
    "gld_crew_skill": ["crew_id", "skill_code"],
    # the network's shapes, metering and history (new marts, so the app never reads silver, bronze or the historian)
    "gld_nmi": ["nmi"],
    "gld_meter_interval_recent": ["nmi", "register", "interval_end"],
    "gld_historian_hourly": ["element", "attribute", "hour"],
    "gld_historian_daily": ["element", "attribute", "day"],
    "gld_feeder_geo": ["feeder_id"],
    "gld_subtransmission": ["line_id"],
    "gld_bulk_supply": ["bsp_id"],
    "gld_transmission_substation": ["ts_id"],
    "gld_transmission_transformer_state": ["transformer_id"],
    "gld_transmission_line": ["line_id"],
    "gld_weather_cell": ["h3", "forecast_time"],
    "gld_fire_incident": ["incident_id"],
    "gld_hotspot": ["hotspot_id"],
    "gld_fire_danger": ["district"],
    "gld_public_charging": ["site_id"],
    # model scores (gold-tier outputs of the ML job)
    "ml_span_risk": ["span_id"],
    "ml_storm_projection": ["feeder_id", "day"],
    "ml_storm_projection_6h": ["window_start", "feeder_id"],
    "ml_zone_load_forecast": ["zs_id", "day"],
}

# PostGIS expression indexes; queries must use exactly these expressions (geography, metres) to use them
INDEXES = [
    "CREATE INDEX IF NOT EXISTS gld_asset_shape ON gld_asset USING gist (ST_GeogFromText(geom_wkt)) WHERE geom_wkt IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS gld_asset_point ON gld_asset USING gist ((ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography)) WHERE lat IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS gld_asset_feeder ON gld_asset (feeder_id, asset_class)",
    "CREATE INDEX IF NOT EXISTS gld_ds_state_point ON gld_ds_state USING gist ((ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography))",
    "CREATE INDEX IF NOT EXISTS gld_ds_state_feeder ON gld_ds_state (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_outage_feeder ON gld_outage (feeder_id, start_time)",
    "CREATE INDEX IF NOT EXISTS gld_open_defect_asset ON gld_open_defect (asset_id)",
    "CREATE INDEX IF NOT EXISTS gld_work_order_asset ON gld_work_order (asset_id)",
    "CREATE INDEX IF NOT EXISTS gld_vegetation_risk_feeder ON gld_vegetation_risk (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_field_job_day ON gld_field_job (job_date, region_id)",
    "CREATE INDEX IF NOT EXISTS gld_nmi_point ON gld_nmi USING gist ((ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography)) WHERE lat IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS gld_nmi_feeder ON gld_nmi (feeder_id)",
    "CREATE INDEX IF NOT EXISTS gld_nmi_zs ON gld_nmi (zs_id)",
    "CREATE INDEX IF NOT EXISTS gld_nmi_ds ON gld_nmi (ds_id)",
    "CREATE INDEX IF NOT EXISTS gld_subtransmission_ts ON gld_subtransmission (ts_id)",
    "CREATE INDEX IF NOT EXISTS gld_subtransmission_zs ON gld_subtransmission (zs_id)",
    "CREATE INDEX IF NOT EXISTS gld_transmission_line_ts ON gld_transmission_line (ts_id)",
    "CREATE INDEX IF NOT EXISTS gld_transmission_transformer_state_ts ON gld_transmission_transformer_state (ts_id)",
]


def _merge_features():
    """Each feature lists its synced marts in its own module (serving/tables_<feature>.py, same SERVING/INDEXES format)."""
    import importlib
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(here)):
        if f.startswith("tables_") and f.endswith(".py"):
            m = importlib.import_module(f[:-3])
            SERVING.update(m.SERVING)
            INDEXES.extend(getattr(m, "INDEXES", []))


_merge_features()
