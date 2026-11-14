# Vegetation management marts (feature 6, November 2026): the bays (one overhead span between two poles), LiDAR survey coverage per
# campaign, current intrusion against predicted grow-in, risk per feeder, and a deterministic seasonal plan baseline. The app reads
# these through Lakebase (serving/tables_vegetation.py); the work itself (packages, completions, audits) is written by the app.
from pyspark import pipelines as dp
from pyspark.sql import functions as F
from pyspark.sql.window import Window

AS_OF = spark.conf.get("atlas.as_of")

# Demo clearance standard: a span must keep this many metres clear of vegetation. Bushfire-prone land takes the wider clearance.
# Replace with the distributor's vegetation management standard when it is confirmed.
REQUIRED_GENERAL_M = 2.0
REQUIRED_BUSHFIRE_M = 3.0
# Baseline crew capacity used by the seasonal plan: bays one contractor programme can cut in a month across the network.
PLAN_BAYS_PER_MONTH = 1500

# Display labels for every code this file produces. They also live in ref_label (the lead merges these rows into gold ref_label); the
# marts fall back to these so a label never shows as a raw code.
VEG_LABELS = [
    ("veg_class", "current_intrusion", "Current intrusion"), ("veg_class", "predicted_grow_in", "Predicted grow-in in 12 months"),
    ("veg_class", "no_action", "No action required"),
    ("veg_tier", "high_risk", "High risk"), ("veg_tier", "cut_required", "Cut required"), ("veg_tier", "planned", "Planned"),
    ("veg_tier", "no_action", "No action required"),
    ("veg_basis", "bushfire_prone", "Bushfire-prone land, 3 m"), ("veg_basis", "general", "General overhead span, 2 m"),
    ("veg_task_status", "issued", "Issued to contractor"), ("veg_task_status", "in_progress", "In progress"),
    ("veg_task_status", "awaiting_audit", "Cut complete, awaiting audit"), ("veg_task_status", "closed", "Closed, accepted"),
    ("veg_audit_result", "pending", "Awaiting decision"), ("veg_audit_result", "compliant", "Compliant, accepted and closed"),
    ("veg_audit_result", "non_compliant", "Non-compliant, reissued to contractor"),
    ("veg_exception", "access_blocked", "Access blocked"), ("veg_exception", "partial_cut", "Partial cut only"),
    ("veg_exception", "tree_too_large", "Tree too large for the crew"), ("veg_exception", "hazard", "Hazard found on site"),
    ("veg_exception", "landowner", "Landowner objection"), ("veg_exception", "equipment", "Equipment fault"),
    ("veg_exception", "other", "Other"),
    ("veg_photo_kind", "before", "Before the cut"), ("veg_photo_kind", "after", "After the cut"), ("veg_photo_kind", "exception", "Exception"),
]


def _labels(domain):
    """Labels for one domain: the ref_label rows, with this file's list as the fallback for any code not yet merged into ref_label."""
    local = spark.createDataFrame([(d, c, l) for d, c, l in VEG_LABELS if d == domain], "domain string, code string, label string")
    shared = spark.read.table("ref_label").where(F.col("domain") == domain).select("domain", "code", "label")
    return shared.unionByName(local).dropDuplicates(["code"]).select("code", "label")


def _with_label(df, domain, code_col, out_col):
    """Join the label for a code (left join: a code without a label stays null rather than dropping the bay)."""
    return df.join(_labels(domain).withColumnRenamed("code", code_col).withColumnRenamed("label", out_col), code_col, "left")


def _cm(df, comments):
    """Select the columns in order, each with the comment the Unity Catalog column and the Lakebase copy carry."""
    return df.select(*[F.col(c).alias(c, metadata={"comment": t}) for c, t in comments])


# ------------------------------------------------------------------ bays
BAY_COMMENTS = [
    ("bay_id", "Bay id: the overhead span between two poles (the span asset id). Also the asset id used for bay photos."),
    ("bay_label", "Bay label for the crews, BAY- and the span number, e.g. BAY-213182."),
    ("feeder_id", "Feeder the bay sits on."), ("zs_id", "Zone substation the feeder is fed from."),
    ("capture_date", "Date of the latest LiDAR capture of the bay (the as-of campaign)."),
    ("lat", "Bay latitude (WGS84)."), ("lon", "Bay longitude (WGS84)."),
    ("geom_wkt", "Bay line as WKT text (WGS84), for the map."),
    ("bushfire_prone", "True when the bay is on bushfire-prone land (wider required clearance)."),
    ("hazard_tree", "True when a hazard tree was recorded against the bay."),
    ("dominant_species", "Dominant vegetation species under the span."),
    ("growth_m_per_year", "Measured vegetation growth toward the line, metres a year."),
    ("pole_lean_deg", "Pole lean from vertical, degrees (LiDAR survey)."),
    ("clearance_m", "Minimum clearance from vegetation to the conductor in the latest LiDAR survey, metres."),
    ("clearance_12m_m", "Predicted clearance in 12 months: clearance minus the measured growth, metres."),
    ("clearance_prior_m", "Minimum clearance in the previous LiDAR survey, metres (null for the first survey)."),
    ("clearance_change_m", "Change in clearance since the previous survey, metres (negative means the vegetation grew in)."),
    ("required_basis", "Which clearance standard applies: bushfire_prone or general."),
    ("required_clearance_m", "Required minimum clearance under the standard, metres."),
    ("clearance_gap_m", "Required clearance minus measured clearance, metres. Positive means the bay is inside the clearance today."),
    ("classification", "current_intrusion (inside the required clearance now), predicted_grow_in (clear now, inside it in 12 months) or no_action."),
    ("risk_tier", "high_risk (current intrusion and red in the risk model), cut_required (current intrusion), planned (predicted grow-in) or no_action."),
    ("gold_status", "Risk colour from the vegetation risk model: red, amber or green."),
    ("risk_score", "Vegetation risk score (higher is riskier), from the vegetation risk model."),
    ("customers_downstream", "Customers downstream of the bay."),
    ("classification_label", "Classification in words."), ("risk_tier_label", "Risk tier in words, as the crews see it."),
    ("required_basis_label", "Clearance standard in words."),
]


@dp.materialized_view(comment="Vegetation bays: one overhead span between two poles from the latest LiDAR campaign, with its clearance now, "
                              "predicted clearance in 12 months, the required clearance, the classification (current intrusion, predicted "
                              "grow-in or no action) and the risk tier crews work to.")
def gld_veg_bay():
    base = spark.sql("""
      WITH cur AS (
        SELECT v.*, CASE WHEN v.bushfire_prone THEN 'bushfire_prone' ELSE 'general' END AS required_basis,
               CASE WHEN v.bushfire_prone THEN 3.0 ELSE 2.0 END AS required_clearance_m
        FROM gld_vegetation_risk v),
      prior AS (SELECT span_id, clearance_m AS clearance_prior_m FROM slv_lidar_span
                WHERE campaign_year = year(date'%s') - 1),
      cls AS (
        SELECT c.*, CASE WHEN c.clearance_m < c.required_clearance_m THEN 'current_intrusion'
                         WHEN c.clearance_12m_m < c.required_clearance_m THEN 'predicted_grow_in'
                         ELSE 'no_action' END AS classification
        FROM cur c)
      SELECT c.span_id AS bay_id, concat('BAY-', regexp_replace(c.span_id, '^[A-Za-z]+', '')) AS bay_label,
             c.feeder_id, a.zs_id, c.capture_date, c.lat, c.lon, c.geom_wkt, c.bushfire_prone, c.hazard_tree,
             c.dominant_species, c.growth_m_per_year, c.pole_lean_deg, c.clearance_m, c.clearance_12m_m,
             p.clearance_prior_m, round(c.clearance_m - p.clearance_prior_m, 2) AS clearance_change_m,
             c.required_basis, c.required_clearance_m, round(c.required_clearance_m - c.clearance_m, 2) AS clearance_gap_m,
             c.classification,
             CASE WHEN c.classification = 'current_intrusion' AND c.status = 'red' THEN 'high_risk'
                  WHEN c.classification = 'current_intrusion' THEN 'cut_required'
                  WHEN c.classification = 'predicted_grow_in' THEN 'planned'
                  ELSE 'no_action' END AS risk_tier,
             c.status AS gold_status, c.risk_score, c.customers_downstream
      FROM cls c
      LEFT JOIN prior p ON p.span_id = c.span_id
      LEFT JOIN gld_asset a ON a.asset_id = c.span_id
    """ % AS_OF)
    df = _with_label(base, "veg_class", "classification", "classification_label")
    df = _with_label(df, "veg_tier", "risk_tier", "risk_tier_label")
    df = _with_label(df, "veg_basis", "required_basis", "required_basis_label")
    return _cm(df, BAY_COMMENTS)


# ------------------------------------------------------------------ risk per feeder
FEEDER_COMMENTS = [
    ("feeder_id", "Feeder."), ("zs_id", "Zone substation the feeder is fed from."),
    ("bays", "Overhead spans (bays) surveyed on the feeder in the latest campaign."),
    ("current_intrusions", "Bays inside the required clearance now."), ("high_risk", "Bays in the high risk tier."),
    ("cut_required", "Bays with a current intrusion that are not high risk."), ("planned", "Bays with a predicted grow-in in 12 months."),
    ("bushfire_bays", "Bays on bushfire-prone land."), ("hazard_trees", "Bays with a hazard tree recorded."),
    ("customers_at_intrusion", "Customers downstream of the bays with a current intrusion."),
    ("max_risk_score", "Highest bay risk score on the feeder."),
    ("feeder_status", "Feeder colour: red when it has a high risk bay, amber when it has a cut required bay, otherwise green."),
]


@dp.materialized_view(comment="Vegetation risk per feeder from the bays: intrusions now, predicted grow-in, high risk bays, bushfire-prone "
                              "bays, hazard trees and the customers behind the intrusions.")
def gld_veg_feeder():
    df = (spark.read.table("gld_veg_bay").groupBy("feeder_id", "zs_id").agg(
        F.count("*").alias("bays"),
        F.sum(F.when(F.col("classification") == "current_intrusion", 1).otherwise(0)).alias("current_intrusions"),
        F.sum(F.when(F.col("risk_tier") == "high_risk", 1).otherwise(0)).alias("high_risk"),
        F.sum(F.when(F.col("risk_tier") == "cut_required", 1).otherwise(0)).alias("cut_required"),
        F.sum(F.when(F.col("risk_tier") == "planned", 1).otherwise(0)).alias("planned"),
        F.sum(F.when(F.col("bushfire_prone"), 1).otherwise(0)).alias("bushfire_bays"),
        F.sum(F.when(F.col("hazard_tree"), 1).otherwise(0)).alias("hazard_trees"),
        F.sum(F.when(F.col("classification") == "current_intrusion", F.col("customers_downstream")).otherwise(0)).alias("customers_at_intrusion"),
        F.max("risk_score").alias("max_risk_score")))
    df = df.withColumn("feeder_status", F.when(F.col("high_risk") > 0, "red").when(F.col("cut_required") > 0, "amber").otherwise("green"))
    return _cm(df, FEEDER_COMMENTS)


# ------------------------------------------------------------------ LiDAR survey coverage per campaign and zone
COVERAGE_COMMENTS = [
    ("campaign_year", "LiDAR campaign year (one annual campaign per year)."),
    ("zs_id", "Zone substation."), ("overhead_spans", "Overhead spans on the zone's feeders (asset register)."),
    ("surveyed_spans", "Overhead spans with a result in the campaign."),
    ("not_surveyed_spans", "Overhead spans with no result in the campaign (tie spans are not surveyed)."),
    ("coverage_pct", "Share of the zone's overhead spans surveyed in the campaign, percent."),
    ("capture_start", "First capture date of the campaign on the zone."), ("capture_end", "Last capture date of the campaign on the zone."),
    ("avg_clearance_m", "Average minimum clearance of the surveyed spans, metres."),
    ("intrusions", "Surveyed spans inside the required clearance in the campaign."),
    ("spans_grown_in", "Spans whose clearance fell since the previous campaign (vegetation grew in)."),
    ("spans_cut_back", "Spans whose clearance rose by at least 1 metre since the previous campaign (cut back)."),
    ("avg_change_m", "Average change in clearance since the previous campaign, metres (null for the first campaign)."),
]


@dp.materialized_view(comment="LiDAR survey coverage per campaign and zone substation: spans surveyed against the overhead spans in the asset "
                              "register, intrusions, grow-in and cut-back since the previous campaign.")
def gld_veg_survey_coverage():
    grid = spark.sql("""
      WITH years AS (SELECT DISTINCT campaign_year FROM slv_lidar_span),
           zones AS (SELECT zs_id, count(*) AS overhead_spans FROM gld_asset WHERE asset_class = 'overhead_span' GROUP BY zs_id)
      SELECT y.campaign_year, z.zs_id, z.overhead_spans FROM years y CROSS JOIN zones z""")
    s = spark.sql("""
      SELECT campaign_year, concat('ZS-', substring(feeder_id, 1, 3)) AS zs_id, count(*) AS surveyed_spans,
             min(capture_date) AS capture_start, max(capture_date) AS capture_end, avg(clearance_m) AS avg_clearance_m,
             sum(CASE WHEN clearance_m < (CASE WHEN bushfire_prone THEN 3.0 ELSE 2.0 END) THEN 1 ELSE 0 END) AS intrusions,
             sum(CASE WHEN clearance_change_m < 0 THEN 1 ELSE 0 END) AS spans_grown_in,
             sum(CASE WHEN clearance_change_m >= 1.0 THEN 1 ELSE 0 END) AS spans_cut_back,
             avg(clearance_change_m) AS avg_change_m
      FROM slv_lidar_span GROUP BY campaign_year, concat('ZS-', substring(feeder_id, 1, 3))""")
    df = (grid.join(s, ["campaign_year", "zs_id"], "left")
          .na.fill(0, ["surveyed_spans", "intrusions", "spans_grown_in", "spans_cut_back"])
          .withColumn("not_surveyed_spans", F.col("overhead_spans") - F.col("surveyed_spans"))
          .withColumn("coverage_pct", F.round(F.col("surveyed_spans") * 100.0 / F.col("overhead_spans"), 1)))
    return _cm(df, COVERAGE_COMMENTS)


# ------------------------------------------------------------------ seasonal plan baseline
PLAN_COMMENTS = [
    ("bay_id", "Bay id (span asset id)."), ("bay_label", "Bay label for the crews."), ("feeder_id", "Feeder."), ("zs_id", "Zone substation."),
    ("risk_tier", "high_risk, cut_required or planned."), ("risk_tier_label", "Risk tier in words."),
    ("plan_seq", "Position in the baseline programme: tier first, then risk score (highest first), then bay id."),
    ("plan_month", "First day of the month the bay is scheduled in the baseline."),
    ("plan_month_label", "Month the bay is scheduled in, in words, e.g. Nov 2026."),
    ("plan_wave", "Month number in the programme, counting from the first month after the as-of day."),
    ("capacity_per_month", "Bays the baseline assumes can be cut in a month (the planning assumption)."),
    ("risk_score", "Vegetation risk score of the bay."), ("customers_downstream", "Customers downstream of the bay."),
]


@dp.materialized_view(comment="Deterministic seasonal plan baseline: every bay that needs work, ordered by risk tier then risk score, and "
                              "scheduled into months at a fixed capacity per month. The planner edits the work from here.")
def gld_veg_plan_baseline():
    b = (spark.read.table("gld_veg_bay").where(F.col("risk_tier") != "no_action")
         .withColumn("tier_order", F.when(F.col("risk_tier") == "high_risk", 1).when(F.col("risk_tier") == "cut_required", 2).otherwise(3)))
    w = F.row_number().over(Window.orderBy("tier_order", F.col("risk_score").desc(), "bay_id"))
    df = b.withColumn("plan_seq", w.cast("int"))
    df = df.withColumn("plan_index", F.floor((F.col("plan_seq") - 1) / PLAN_BAYS_PER_MONTH).cast("int"))
    df = (df.withColumn("plan_wave", F.col("plan_index") + 1)
          .withColumn("plan_month", F.expr(f"add_months(trunc(date'{AS_OF}', 'MM'), plan_index + 1)"))
          .withColumn("plan_month_label", F.date_format("plan_month", "MMM yyyy"))
          .withColumn("capacity_per_month", F.lit(PLAN_BAYS_PER_MONTH)))
    return _cm(df.select("bay_id", "bay_label", "feeder_id", "zs_id", "risk_tier", "risk_tier_label", "plan_seq", "plan_month",
                         "plan_month_label", "plan_wave", "capacity_per_month", "risk_score", "customers_downstream"), PLAN_COMMENTS)
