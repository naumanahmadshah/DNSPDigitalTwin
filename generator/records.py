"""Canonical schemas of the live streams. The Lakebase setup creates schema live from them (ops/lakebase.py), the live engine writes them
(ops/live.py), and the pipeline's bronze layer reads their change history. Also the one place that names the lakehouse tables holding
the Lakebase change feeds, so the pipeline and the serving setup cannot disagree about them."""

LAND = {
    "scada": ("ref STRING COMMENT 'IEC 61850 style reference, e.g. KNV-F02-R01/XCBR1.Pos.stVal', "
              "tag STRING COMMENT 'Historian tag for analog values, e.g. KNV-F02.AMPS.PV', element STRING COMMENT 'Twin asset id', "
              "attribute STRING, feeder_id STRING, ts TIMESTAMP, value DOUBLE, "
              "quality STRING COMMENT 'good, questionable, comm_lost, substituted', source STRING",
              "Live SCADA and ADMS point values pushed through Zerobus (analogs every 4 s, status on change)."),
    "meter_event": ("nmi STRING, meter_serial STRING, event_code STRING COMMENT 'POWER_OUTAGE or POWER_RESTORE', ts TIMESTAMP, "
                    "metering_coordinator STRING", "Smart meter power outage and restoration alarms from metering coordinators."),
    "call": ("call_id STRING, ts TIMESTAMP, nmi STRING, reason STRING, channel STRING", "Contact centre and IVR outage calls."),
}


def change_feed_table(catalog, schema, plumbing, table):
    """The UC table that holds the Lakebase change feed of one Postgres table. The fed schemas of the app's Lakebase project (atlas, rm and
    live) land in UC schemas named <schema>_lb_<plumbing>, where schema is this deployment's atlas.schema, as lb_<table>_history."""
    return f"{catalog}.{schema}_lb_{plumbing}.lb_{table}_history"
