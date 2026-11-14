# Bronze: every source as landed, in its native shape, with the file it came from and when it was ingested.
import os
import sqlite3
import struct
import sys

from pyspark import pipelines as dp
from pyspark.errors import AnalysisException
from pyspark.sql import functions as F
from pyspark.sql import types as T

CAT = spark.conf.get("atlas.catalog")
SCH = spark.conf.get("atlas.schema")
LANDING = spark.conf.get("atlas.landing")
FEEDS = spark.conf.get("atlas.feeds")

# The live streams' schemas and the change feed table names are the generator's (records.py), so the live engine and this layer agree.
sys.path.insert(0, os.path.join(spark.conf.get("atlas.src"), "generator"))
from records import LAND, change_feed_table  # noqa: E402


def _meta(df):
    return df.withColumn("_source_file", F.col("_metadata.file_path")).withColumn("_ingested_at", F.current_timestamp())


def _autoload(path, fmt, **opts):
    r = (spark.readStream.format("cloudFiles").option("cloudFiles.format", fmt)
         .option("cloudFiles.schemaEvolutionMode", "none").option("cloudFiles.inferColumnTypes", "false"))
    for k, v in opts.items():
        r = r.option(k, v)
    return _meta(r.load(path))


# ------------------------------------------------------------------ network connectivity model (CIM RDF/XML)
CIM_CLASSES = ["ACLineSegment", "Breaker", "Recloser", "LoadBreakSwitch", "Fuse", "Disconnector", "PowerTransformer",
               "EnergyConsumer", "Feeder", "Substation"]


def _cim(cls):
    df = (spark.read.format("xml").option("rowTag", f"cim:{cls}").option("attributePrefix", "")
          .option("valueTag", "value").load(f"{LANDING}/ncm/*.xml"))
    return df


@dp.materialized_view(comment="CIM conducting equipment and containers from the network connectivity model export (one row per object, "
                              "class in cim_class). Native IEC 61970 RDF/XML.")
def brz_ncm_equipment():
    out = None
    for cls in CIM_CLASSES:
        df = _cim(cls)
        cols = {c.split(".")[-1]: c for c in df.columns}
        sel = [F.lit(cls).alias("cim_class"), F.col("`rdf:ID`").alias("rdf_id"),
               F.col("`cim:IdentifiedObject.mRID`").cast("string").alias("mrid"),
               F.col("`cim:IdentifiedObject.name`").cast("string").alias("name")]
        cont = cols.get("EquipmentContainer") or cols.get("NormalEnergizingSubstation") or cols.get("Region")
        sel.append((F.col(f"`{cont}`.`rdf:resource`") if cont else F.lit(None)).cast("string").alias("container_ref"))
        sel.append((F.col("`cim:Switch.normalOpen`") if "normalOpen" in cols else F.lit(None)).cast("string").alias("normal_open"))
        sel.append((F.col("`cim:Conductor.length`") if "length" in cols else F.lit(None)).cast("double").alias("length_m"))
        d = df.select(*sel).withColumn("_source_file", F.col("_metadata.file_path"))
        out = d if out is None else out.unionByName(d)
    return out.withColumn("_ingested_at", F.current_timestamp())


@dp.materialized_view(comment="CIM Terminals: which conducting equipment connects to which ConnectivityNode, with phases.")
def brz_ncm_terminal():
    df = _cim("Terminal")
    return df.select(F.col("`rdf:ID`").alias("rdf_id"),
                     F.col("`cim:ACDCTerminal.sequenceNumber`").cast("int").alias("sequence_number"),
                     F.col("`cim:Terminal.ConductingEquipment`.`rdf:resource`").alias("equipment_ref"),
                     F.col("`cim:Terminal.ConnectivityNode`.`rdf:resource`").alias("node_ref"),
                     F.col("`cim:Terminal.phases`.`rdf:resource`").alias("phases_ref"),
                     F.col("_metadata.file_path").alias("_source_file"), F.current_timestamp().alias("_ingested_at"))


@dp.materialized_view(comment="CIM UsagePoints (one per NMI) and the EnergyConsumer they meter.")
def brz_ncm_usage_point():
    df = _cim("UsagePoint")
    return df.select(F.col("`cim:IdentifiedObject.name`").cast("string").alias("nmi"),
                     F.col("`cim:UsagePoint.Equipments`.`rdf:resource`").alias("equipment_ref"),
                     F.col("_metadata.file_path").alias("_source_file"), F.current_timestamp().alias("_ingested_at"))


# ------------------------------------------------------------------ GIS (GeoPackage)
def _gpkg_rows(table):
    path = f"{LANDING}/gis/network.gpkg"
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    cur = con.execute(f"SELECT * FROM {table}")
    cols = [c[0] for c in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        g = d.pop("geom")
        # GeoPackage binary: 'GP', version, flags, srs_id, optional envelope, then WKB.
        flags = g[3]
        env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 0b111]
        d["geom_wkb_hex"] = g[8 + env:].hex()
        d["srs_id"] = struct.unpack("<i", g[4:8])[0]
        rows.append(d)
    con.close()
    return rows


def _gpkg_view(table):
    rows = _gpkg_rows(table)
    schema = T.StructType([T.StructField(k, T.StringType()) for k in rows[0].keys()])
    df = spark.createDataFrame([{k: (None if v is None else str(v)) for k, v in r.items()} for r in rows], schema)
    return df.withColumn("_source_file", F.lit(f"{LANDING}/gis/network.gpkg#{table}")).withColumn("_ingested_at", F.current_timestamp())


for _t in ["electric_pole", "electric_conductor", "electric_dist_substation", "electric_switch", "electric_zone_substation",
           "electric_service_point", "electric_bulk_supply_point", "electric_transmission_substation", "electric_transmission_line",
           "electric_subtransmission_line"]:
    def _mk(t=_t):
        @dp.materialized_view(name=f"brz_gis_{t}", comment=f"GIS feature table {t} from the GeoPackage export (WKB geometry, GDA2020).")
        def _f():
            return _gpkg_view(t)
    _mk()


# ------------------------------------------------------------------ SAP PM (ODP change files)
for _t in ["EQUI", "QMEL", "QMIH", "AUFK"]:
    def _mk_sap(t=_t):
        @dp.table(name=f"brz_sap_{t.lower()}", comment=f"SAP PM table {t} delta extract as landed (ODQ_CHANGEMODE C/U/D).")
        def _f():
            return _autoload(f"{LANDING}/sap/{t}/", "csv", header="true")
    _mk_sap()


# ------------------------------------------------------------------ market standing data, life support, DER register
def _batch_csv(path):
    return _meta(spark.read.option("header", True).csv(path))


@dp.materialized_view(comment="MSATS-style NMI standing data (CATS_NMI_DATA).")
def brz_market_nmi():
    return _batch_csv(f"{LANDING}/market/CATS_NMI_DATA_*.csv")


@dp.materialized_view(comment="Customer contact preferences from the outage notification register (CRM): SMS, email and Facebook "
                              "Messenger opt-ins per NMI.")
def brz_contact_preferences():
    return _batch_csv(f"{LANDING}/crm/CUSTOMER_CONTACT_PREFERENCES_*.csv")


@dp.materialized_view(comment="Distributor life support register (National Energy Retail Rules Part 7 fields).")
def brz_life_support():
    return _batch_csv(f"{LANDING}/market/LIFE_SUPPORT_REGISTER_*.csv")


@dp.materialized_view(comment="Critical customer sites register (hospitals, aged care, schools, water and telecom).")
def brz_critical_sites():
    return _batch_csv(f"{LANDING}/market/CRITICAL_SITES_*.csv")


@dp.materialized_view(comment="DER register extract (solar, batteries, EV chargers, community batteries).")
def brz_der_register():
    return _batch_csv(f"{LANDING}/der/DER_REGISTER_*.csv")


@dp.materialized_view(comment="Public EV charging sites.")
def brz_public_charging():
    return _batch_csv(f"{LANDING}/der/PUBLIC_CHARGING_SITES_*.csv")


# ------------------------------------------------------------------ ADMS
@dp.table(comment="ADMS outage records with restoration stages, as exported (JSON lines).")
def brz_adms_outage():
    return _autoload(f"{LANDING}/adms/outages/", "json")


@dp.table(comment="ADMS active incidents: the outages still in progress when the extract was taken, with the work so far (JSON lines).")
def brz_adms_open_outage():
    return _autoload(f"{LANDING}/adms/open_outages/", "json")


# ------------------------------------------------------------------ smart meters
@dp.table(comment="NEM12 interval files as landed (one row per file; records 100/200/300/400/900 inside).")
def brz_nem12_file():
    return _autoload(f"{LANDING}/nem12/", "text", wholetext="true")


@dp.table(comment="Metering coordinator voltage service: 5-minute average voltage per meter.")
def brz_meter_voltage():
    return _autoload(f"{LANDING}/meter_voltage/", "csv", header="true", pathGlobFilter="*.csv")


# ------------------------------------------------------------------ LiDAR, drone
@dp.materialized_view(comment="LiDAR campaign results per span (all campaigns), campaign year from the folder name.")
def brz_lidar_span():
    return (_batch_csv(f"{LANDING}/lidar/campaign_*/span_clearance.csv")
            .withColumn("campaign_year", F.regexp_extract("_source_file", r"campaign_(\d{4})", 1).cast("int")))


@dp.materialized_view(comment="Drone patrol detections (COCO JSON) exploded to one row per image with its detections.")
def brz_drone_coco():
    df = spark.read.option("multiLine", True).json(f"{LANDING}/drone/*/detections_coco.json")
    return _meta(df)


# ------------------------------------------------------------------ workforce and planned work
WORKFORCE = {"crews": "Rostering system crew roster: one row per deployable crew (a team and its vehicle), named by its leader.",
             "crew_skills": "Crew skills from the rostering system (primary and secondary).",
             "crew_tickets": "Tickets and licences per crew from the learning management system, with issue and expiry dates.",
             "crew_leave": "Approved and pending leave from the HR system, per crew and day.",
             "crew_training": "Training bookings from the learning management system, per crew and day.",
             "safety_events": "Safety incidents, near misses and hazards from the safety management system.",
             "eba_rules": "Enterprise agreement working-time rules per skill (daily cap, rest, overtime).",
             "vehicles": "Fleet register: vehicles and plant allocated to crews and depots.",
             "depots": "Depots, the region each one runs and the zone substations it serves."}
for _t, _c in WORKFORCE.items():
    def _mk_wf(t=_t, c=_c):
        @dp.materialized_view(name=f"brz_wf_{t}", comment=c)
        def _f():
            return _batch_csv(f"{LANDING}/workforce/{t}_*.csv")
    _mk_wf()


@dp.materialized_view(comment="Work management system: planned field work orders for the coming days, one row per order, as exported.")
def brz_wms_work_orders():
    return _batch_csv(f"{LANDING}/wms/work_orders_*.csv")


# ------------------------------------------------------------------ real public feeds (raw payloads)
@dp.table(comment="NSW RFS major incidents feed (GeoJSON), one row per capture. Licence CC BY 4.0, State of NSW (NSW RFS).")
def brz_feed_rfs_incidents():
    return _autoload(f"{FEEDS}/rfs_incidents/", "text", wholetext="true")


@dp.table(comment="NSW RFS fire danger ratings and total fire bans (XML), one row per capture.")
def brz_feed_rfs_fire_danger():
    return _autoload(f"{FEEDS}/rfs_fire_danger/", "text", wholetext="true")


@dp.table(comment="DEA Hotspots (Geoscience Australia) WFS captures over the footprint, CC BY 4.0.")
def brz_feed_dea_hotspots():
    return _autoload(f"{FEEDS}/dea_hotspots/", "text", wholetext="true")




@dp.table(comment="Open-Meteo forecast captures on a grid over the footprint (CC BY 4.0).")
def brz_feed_forecast():
    return _autoload(f"{FEEDS}/openmeteo_forecast/", "text", wholetext="true")


# ------------------------------------------------------------------ live streams (the app's Lakebase live.* tables, inserts only)
def _live(t, cols):
    """Inserts of one live stream from the Lakebase change feed. The history table appears once the stream has rows."""
    schema = T.StructType.fromDDL(cols)
    try:
        hist = spark.read.table(change_feed_table(CAT, SCH, "live", t))
    except AnalysisException:
        return spark.createDataFrame([], schema)
    return (hist.where(F.col("_pg_change_type") == "insert")
            .select(*[F.col(f.name).cast(f.dataType).alias(f.name, metadata=f.metadata) for f in schema.fields]))


for _t, (_cols, _c) in LAND.items():
    if _t == "scada":   # SCADA values are read live from Lakebase by the app; nothing in the lakehouse uses them
        continue
    def _mk_live(t=_t, c=_c, cols=_cols):
        @dp.materialized_view(name=f"brz_{t}", comment=f"{c} Live {t.replace('_', ' ')} records from the live.{t} Lakebase table (inserts).")
        def _f():
            return _live(t, cols)
    _mk_live()
