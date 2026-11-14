# Customers: new connection applications (the case management export, generator/connections.py) from the landing volume to gold.
# Bronze keeps each file as landed; silver keeps one row per application and one per stage it has been through; gold adds labels,
# ageing, the stuck flag and the summaries the Customers screen reads. Existing customers need no table of their own: the app reads
# the synced gld_nmi, gld_outage and the app's own outage and notice records.
from pyspark import pipelines as dp
from pyspark.sql import functions as F
from pyspark.sql.window import Window

CAT = spark.conf.get("atlas.catalog")
SCH = spark.conf.get("atlas.schema")
AS_OF = spark.conf.get("atlas.as_of")
LANDING = spark.conf.get("atlas.landing")

STAGES = "array('apply', 'assess', 'offer', 'design', 'build', 'commission', 'connected')"
# Days a case can sit in a stage before it counts as stuck (a blocker makes a case stuck at any age).
STAGE_LIMITS = "VALUES ('apply', 14), ('assess', 30), ('offer', 21), ('design', 35), ('build', 60), ('commission', 21) AS t(stage, limit_days)"


def _meta(df):
    return df.withColumn("_source_file", F.col("_metadata.file_path")).withColumn("_ingested_at", F.current_timestamp())


def _autoload(glob):
    """Auto Loader over the connections folder, one file pattern at a time, every column as text (types are set in silver)."""
    r = (spark.readStream.format("cloudFiles").option("cloudFiles.format", "csv").option("cloudFiles.schemaEvolutionMode", "none")
         .option("cloudFiles.inferColumnTypes", "false").option("header", "true").option("pathGlobFilter", glob))
    return _meta(r.load(f"{LANDING}/connections/"))


def _clean(c):
    """Blank text from the export is null."""
    return F.nullif(F.trim(F.col(c)), F.lit(""))


def _commented(df, comments):
    """Column comments on a mart: the Lakebase copy and Genie show them."""
    return df.select([F.col(c).alias(c, metadata={"comment": comments[c]}) if c in comments else F.col(c) for c in df.columns])


# ------------------------------------------------------------------ bronze: the export as landed
@dp.table(comment="Connection applications as landed from the case management export: one row per application (its current state), every "
                  "column as text, with the file it came from and when it was ingested.")
def brz_connection_case():
    return _autoload("connection_cases_*.csv")


@dp.table(comment="Connection stage events as landed: one row per stage each application has been through, with the day it was entered and left.")
def brz_connection_stage_event():
    return _autoload("connection_stage_events_*.csv")


# ------------------------------------------------------------------ silver: typed, one row per application and per stage
@dp.materialized_view(comment="Connection applications, typed and one row per application (the latest export wins if a case is sent again).")
def slv_connection_case():
    latest = Window.partitionBy("case_id").orderBy(F.col("_ingested_at").desc(), F.col("_source_file").desc())
    return (spark.read.table("brz_connection_case").withColumn("_rn", F.row_number().over(latest)).where("_rn = 1")
            .select(_clean("case_id").alias("case_id"), _clean("applicant_name").alias("applicant_name"),
                    _clean("applicant_kind").alias("applicant_kind"), _clean("connection_type").alias("connection_type"),
                    _clean("address").alias("address"), _clean("suburb").alias("suburb"), _clean("postcode").alias("postcode"),
                    _clean("state").alias("state"), F.col("latitude").cast("double").alias("latitude"),
                    F.col("longitude").cast("double").alias("longitude"), _clean("nmi").alias("nmi"), _clean("zs_id").alias("zs_id"),
                    _clean("feeder_id").alias("feeder_id"), _clean("ds_id").alias("ds_id"),
                    F.col("load_kva").cast("double").alias("load_kva"), F.col("export_kw").cast("double").alias("export_kw"),
                    F.col("battery_kwh").cast("double").alias("battery_kwh"), F.col("lots").cast("int").alias("lots"),
                    F.col("phases").cast("int").alias("phases"), F.to_date(_clean("applied_date")).alias("applied_date"),
                    F.to_date(_clean("target_date")).alias("target_date"), _clean("current_stage").alias("current_stage"),
                    F.to_date(_clean("stage_entered_date")).alias("stage_entered_date"),
                    _clean("payment_status").alias("payment_status"), _clean("asp_name").alias("asp_name"),
                    _clean("blocker_code").alias("blocker_code"), F.col("_source_file"), F.col("_ingested_at")))


@dp.materialized_view(comment="Stage events, typed: one row per stage a connection application has been through, with the day it was entered "
                              "and the day it was left (blank while it is the current stage).")
def slv_connection_stage_event():
    return (spark.read.table("brz_connection_stage_event")
            .select(_clean("case_id").alias("case_id"), _clean("stage").alias("stage"), F.col("stage_seq").cast("int").alias("stage_seq"),
                    F.to_date(_clean("entered_date")).alias("entered_date"), F.to_date(_clean("exited_date")).alias("exited_date"))
            .dropDuplicates(["case_id", "stage_seq"]))


# ------------------------------------------------------------------ gold: what the Customers screen reads (synced into Lakebase)
@dp.materialized_view(comment="New connection applications, one row each: the stage the case is in and how long it has been there, whether it is "
                              "stuck and why, when it was applied for and when it is due, payment, the accredited service provider, the blocker "
                              "and where the premises is. Dates are the as-of day's; the app moves them to today.")
def gld_connection_application():
    return _commented(spark.sql(f"""
      WITH lim AS (SELECT * FROM {STAGE_LIMITS}),
      c AS (SELECT k.*, array_position({STAGES}, k.current_stage) AS stage_no,
                   datediff(date'{AS_OF}', k.stage_entered_date) AS days_in_stage,
                   datediff(date'{AS_OF}', k.applied_date) AS days_since_applied
            FROM slv_connection_case k),
      j AS (SELECT c.*, coalesce(lim.limit_days, 999) AS limit_days, coalesce(sl.label, c.current_stage) AS stage_label,
                   coalesce(tl.label, c.connection_type) AS connection_type_label, coalesce(pl.label, c.payment_status) AS payment_label,
                   coalesce(kl.label, c.applicant_kind) AS applicant_kind_label, bl.label AS blocker_label,
                   c.current_stage <> 'connected' AS is_open
            FROM c
            LEFT JOIN lim ON lim.stage = c.current_stage
            LEFT JOIN ref_label sl ON sl.domain = 'connection_stage' AND sl.code = c.current_stage
            LEFT JOIN ref_label tl ON tl.domain = 'connection_type' AND tl.code = c.connection_type
            LEFT JOIN ref_label pl ON pl.domain = 'connection_payment' AND pl.code = c.payment_status
            LEFT JOIN ref_label kl ON kl.domain = 'applicant_kind' AND kl.code = c.applicant_kind
            LEFT JOIN ref_label bl ON bl.domain = 'connection_blocker' AND bl.code = c.blocker_code)
      SELECT case_id AS application_id, applicant_name, applicant_kind, applicant_kind_label, address, suburb, postcode,
             latitude AS lat, longitude AS lon, nmi, zs_id, feeder_id, connection_type, connection_type_label,
             load_kva, export_kw, battery_kwh, lots, phases,
             current_stage AS stage, stage_label, stage_no, is_open, stage_entered_date, days_in_stage,
             applied_date, days_since_applied, target_date,
             CASE WHEN is_open THEN greatest(0, datediff(date'{AS_OF}', target_date)) ELSE 0 END AS days_past_target,
             payment_status, payment_label, asp_name AS asp, blocker_code AS blocker, blocker_label,
             is_open AND (days_in_stage > limit_days OR blocker_code IS NOT NULL) AS stuck,
             CASE WHEN is_open AND (days_in_stage > limit_days OR blocker_code IS NOT NULL)
                  THEN coalesce(blocker_label, concat('Over ', limit_days, ' days in ', lower(stage_label))) END AS stuck_reason
      FROM j"""), {
        "application_id": "Connection application id (the case number).", "applicant_name": "Applicant (a person, or a company).",
        "applicant_kind": "individual or company.", "applicant_kind_label": "Applicant kind, in words.",
        "address": "Premises address (a new premises is the lot's street address).", "suburb": "Suburb.", "postcode": "Postcode.",
        "lat": "Premises latitude (WGS84).", "lon": "Premises longitude (WGS84).",
        "nmi": "NMI of an existing premises (blank for a new premises not yet on the register).", "zs_id": "Zone substation supplying the premises.",
        "feeder_id": "Feeder supplying the premises.", "connection_type": "Connection type code: residential, solar_battery, ev_charging, commercial or subdivision.",
        "connection_type_label": "Connection type, in words.", "load_kva": "Load the premises draws after connection, kVA.",
        "export_kw": "Solar capacity exported to the network, kW (zero when none).", "battery_kwh": "Battery capacity, kWh (zero when none).",
        "lots": "Lots created (subdivisions only).", "phases": "Phases connected (1 or 3).",
        "stage": "Stage the case is in now: apply, assess, offer, design, build, commission or connected.",
        "stage_label": "Stage, in words.", "stage_no": "Position of the stage in the journey, 1 (apply) to 7 (connected).",
        "is_open": "True until the premises is connected.", "stage_entered_date": "Day the case entered its current stage.",
        "days_in_stage": "Days in the current stage (or since connection, once connected).", "applied_date": "Day the application was lodged.",
        "days_since_applied": "Days since the application was lodged.", "target_date": "Target connection day promised at application.",
        "days_past_target": "Days past the target connection day while the case is open (zero otherwise).",
        "payment_status": "Payment status code: not_due, quoted, invoiced, paid or no_charge.", "payment_label": "Payment status, in words.",
        "asp": "Accredited service provider doing the work (blank until one is appointed).",
        "blocker": "Blocker code holding the case in its stage (blank when none).", "blocker_label": "Blocker, in words.",
        "stuck": "True when the case is open and either has a blocker or has been in its stage longer than the stage allows.",
        "stuck_reason": "Why the case is stuck, in words (the blocker, or the time in stage)."})


@dp.materialized_view(comment="Stage events of each connection application, with labels: when each stage was entered and left, and the days in it "
                              "(to the as-of day while it is the current stage).")
def gld_connection_stage_event():
    return _commented(spark.sql(f"""
      SELECT e.case_id AS application_id, e.stage, coalesce(l.label, e.stage) AS stage_label,
             array_position({STAGES}, e.stage) AS stage_no, e.stage_seq, e.entered_date, e.exited_date,
             datediff(coalesce(e.exited_date, date'{AS_OF}'), e.entered_date) AS days_in_stage
      FROM slv_connection_stage_event e LEFT JOIN ref_label l ON l.domain = 'connection_stage' AND l.code = e.stage"""), {
        "application_id": "Connection application id.", "stage": "Stage code.", "stage_label": "Stage, in words.",
        "stage_no": "Position of the stage in the journey, 1 (apply) to 7 (connected).", "stage_seq": "Order of this stage event for the application.",
        "entered_date": "Day the case entered the stage.", "exited_date": "Day the case left the stage (blank while current).",
        "days_in_stage": "Days the case spent in the stage (to the as-of day while current)."})


@dp.materialized_view(comment="Pipeline by stage: open applications in each stage, how many are stuck or blocked, the median time in stage, the ageing "
                              "buckets, and connections in the last 90 days (on the connected row).")
def gld_connection_stage():
    return _commented(spark.sql("""
      WITH s AS (SELECT * FROM VALUES ('apply', 1), ('assess', 2), ('offer', 3), ('design', 4), ('build', 5), ('commission', 6),
                                       ('connected', 7) AS t(stage, stage_no))
      SELECT s.stage, coalesce(l.label, s.stage) AS stage_label, s.stage_no,
             count(a.application_id) FILTER (WHERE a.is_open) AS open_applications,
             count(a.application_id) FILTER (WHERE a.stuck) AS stuck_applications,
             count(a.application_id) FILTER (WHERE a.is_open AND a.blocker IS NOT NULL) AS blocked_applications,
             percentile_approx(a.days_in_stage, 0.5) FILTER (WHERE a.is_open) AS median_days_in_stage,
             count(a.application_id) FILTER (WHERE a.is_open AND a.days_in_stage <= 30) AS aged_0_30_days,
             count(a.application_id) FILTER (WHERE a.is_open AND a.days_in_stage > 30 AND a.days_in_stage <= 60) AS aged_31_60_days,
             count(a.application_id) FILTER (WHERE a.is_open AND a.days_in_stage > 60 AND a.days_in_stage <= 90) AS aged_61_90_days,
             count(a.application_id) FILTER (WHERE a.is_open AND a.days_in_stage > 90) AS aged_over_90_days,
             count(a.application_id) FILTER (WHERE NOT a.is_open AND a.days_in_stage <= 90) AS connected_last_90_days
      FROM s
      LEFT JOIN gld_connection_application a ON a.stage = s.stage
      LEFT JOIN ref_label l ON l.domain = 'connection_stage' AND l.code = s.stage
      GROUP BY s.stage, s.stage_no, l.label"""), {
        "stage": "Stage code.", "stage_label": "Stage, in words.", "stage_no": "Position of the stage in the journey, 1 to 7.",
        "open_applications": "Open applications in the stage.", "stuck_applications": "Open applications in the stage that are stuck.",
        "blocked_applications": "Open applications in the stage held by a blocker.", "median_days_in_stage": "Median days open applications have been in the stage.",
        "aged_0_30_days": "Open applications in the stage 30 days or less.", "aged_31_60_days": "Open applications in the stage 31 to 60 days.",
        "aged_61_90_days": "Open applications in the stage 61 to 90 days.", "aged_over_90_days": "Open applications in the stage over 90 days.",
        "connected_last_90_days": "Applications connected in the last 90 days (connected row only)."})
