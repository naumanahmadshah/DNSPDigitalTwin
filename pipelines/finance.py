# Network financials for the executive view (November 2026): monthly revenue, operating costs, depreciation and finance costs over the
# twelve months to the as-of day, actual against budget, so the Command Centre can show revenue, costs and profit side by side.
# The distributor is fictional and the rates are ILLUSTRATIVE (a real distributor's come from its AER determination, its approved
# network tariffs and its own ledger). Where the twin holds the activity behind a line, the actual comes from it: the reliability
# incentive from daily SAIDI against target, corrective maintenance from the completed work orders, emergency response from the
# unplanned outages. The other lines are the modelled rate times the customer base. The app reads gld_finance_month through Lakebase.
from pyspark import pipelines as dp
from pyspark.sql import functions as F

AS_OF = spark.conf.get("atlas.as_of")  # noqa: F821 - spark is the pipeline session

# Network tariffs per tariff and customer type: daily fixed charge (AUD a day), usage charge (AUD a kWh), annual consumption (kWh).
# The large customer demand tariff carries its demand charge inside the daily charge.
TARIFF = [
    ("EA010", "residential", 0.62, 0.095, 5400),
    ("EA050", "small_business", 0.85, 0.115, 22000),
    ("EA050", "large_business", 2.40, 0.105, 160000),
    ("EA302", "large_business", 320.0, 0.045, 2600000),
]
USAGE_SEASON = {1: 1.12, 2: 1.08, 3: 0.98, 4: 0.90, 5: 0.95, 6: 1.08, 7: 1.12, 8: 1.05, 9: 0.92, 10: 0.90, 11: 0.93, 12: 1.05}
CUSTOMER_GROWTH = 0.016          # a year: earlier months had proportionally fewer customers
METERING_PER_DAY = 0.11          # AUD a customer a day (metering services)
STPIS_PER_SAIDI_MIN = 110000.0   # AUD a SAIDI minute a year, better (positive) or worse (negative) than target
SAIDI_TARGET = 92.0              # minutes a regulatory year, major event days excluded (as gld_reliability_year)
OPEX_PER_CUSTOMER = [            # AUD a customer a year
    ("preventive_maintenance", 92.0, "Inspection and preventive maintenance: illustrative AUD 92 a customer a year."),
    ("network_operations", 58.0, "Control room, switching and network operations: illustrative AUD 58 a customer a year."),
    ("customer_metering_ops", 41.0, "Customer service, contact centre and metering operations: illustrative AUD 41 a customer a year."),
    ("corporate_overhead", 78.0, "Corporate, IT and insurance overheads: illustrative AUD 78 a customer a year."),
]
VEG_BAYS_PER_MONTH, VEG_COST_PER_BAY = 1500, 560.0
EMERGENCY_PER_OUTAGE, EMERGENCY_BUDGET_MONTH = 9500.0, 450000.0
CORRECTIVE_BUDGET_MONTH = 520000.0
RAB_PER_CUSTOMER, DEPRECIATION_RATE, GEARING, COST_OF_DEBT = 7200.0, 0.038, 0.60, 0.049

LINES = [  # line, group, sort order (labels come from ref_label, with this list's label as the fallback)
    ("network_fixed", "revenue", 10, "Network charges, daily fixed"), ("network_usage", "revenue", 20, "Network charges, usage"),
    ("metering", "revenue", 30, "Metering services"), ("reliability_incentive", "revenue", 40, "Reliability incentive (STPIS)"),
    ("preventive_maintenance", "opex", 110, "Inspection and preventive maintenance"),
    ("corrective_maintenance", "opex", 120, "Corrective maintenance (work orders)"),
    ("emergency_response", "opex", 130, "Emergency response to outages"), ("vegetation", "opex", 140, "Vegetation management"),
    ("network_operations", "opex", 150, "Network operations"), ("customer_metering_ops", "opex", 160, "Customer service and metering"),
    ("corporate_overhead", "opex", 170, "Corporate overheads"),
    ("depreciation", "depreciation", 210, "Depreciation"), ("finance_cost", "finance_cost", 310, "Finance costs"),
]
GROUPS = [("revenue", "Revenue"), ("opex", "Operating costs"), ("depreciation", "Depreciation"), ("finance_cost", "Finance costs")]
FINANCE_LABELS = [("finance_line", l, lab) for l, _, _, lab in LINES] + [("finance_group", g, lab) for g, lab in GROUPS]

COMMENTS = [
    ("month", "First day of the month."),
    ("regulatory_year", "Regulatory (financial) year the month falls in, July to June, named by the year it ends."),
    ("line", "Profit and loss line code (label in line_label)."), ("line_label", "Profit and loss line, for display."),
    ("line_group", "Group code: revenue, opex, depreciation or finance_cost (label in line_group_label)."),
    ("line_group_label", "Group, for display."), ("sort_order", "Display order of the line in a profit and loss statement."),
    ("actual_aud", "Actual for the month, AUD excluding GST (revenue positive; costs positive; a reliability penalty is negative revenue)."),
    ("budget_aud", "Budget for the month, AUD excluding GST."),
    ("is_twin_driven", "True when the actual comes from activity in the twin (reliability, work orders, outages) rather than a rate."),
    ("basis", "How the line is calculated, in words (rates are illustrative)."),
]


def _labels(domain):
    local = spark.createDataFrame([r for r in FINANCE_LABELS if r[0] == domain], "domain string, code string, label string")  # noqa: F821
    shared = spark.read.table("ref_label").where(F.col("domain") == domain).select("domain", "code", "label")  # noqa: F821
    return shared.unionByName(local).dropDuplicates(["code"]).select("code", "label")


@dp.materialized_view(comment="Network financials by month for the twelve months to the as-of day: revenue, operating costs, depreciation "
                              "and finance costs per profit and loss line, actual against budget. Illustrative rates for a fictional "
                              "distributor; the reliability incentive, corrective maintenance and emergency response actuals come from "
                              "the twin's reliability, work orders and outages. Profit = revenue less all costs.")
def gld_finance_month():
    df = spark.sql(finance_sql())  # noqa: F821 - spark is the pipeline session
    meta = spark.createDataFrame([(l, g, s) for l, g, s, _ in LINES], "line string, line_group string, sort_order int")  # noqa: F821
    df = (df.join(meta, "line")
            .join(_labels("finance_line").withColumnRenamed("code", "line").withColumnRenamed("label", "line_label"), "line", "left")
            .join(_labels("finance_group").withColumnRenamed("code", "line_group").withColumnRenamed("label", "line_group_label"), "line_group", "left"))
    return df.select(*[F.col(c).alias(c, metadata={"comment": t}) for c, t in COMMENTS])


def finance_sql(prefix=""):
    """The monthly lines as one Spark SQL query over gold and silver (prefix qualifies the tables when run outside the pipeline)."""
    season_mean = sum(USAGE_SEASON.values()) / 12
    season = ", ".join(f"({m}, {f / season_mean})" for m, f in USAGE_SEASON.items())
    tariff = ", ".join(f"('{t}', '{c}', {d}, {u}, {k})" for t, c, d, u, k in TARIFF)
    opex = " UNION ALL ".join(f"SELECT c.month, '{k}', {v} * c.customers / 12 * c.growth, {v} * c.customers / 12 * c.growth, false, '{b}' FROM cal c"
                              for k, v, b in OPEX_PER_CUSTOMER)
    rab = f"{RAB_PER_CUSTOMER} * c.customers * c.growth"
    return f"""
      WITH months AS (SELECT explode(sequence(add_months(trunc(date'{AS_OF}', 'MM'), -11), trunc(date'{AS_OF}', 'MM'), interval 1 month)) month),
      base AS (SELECT count(*) customers FROM {prefix}gld_nmi),
      cal AS (SELECT m.month, day(last_day(m.month)) days, s.season, b.customers,
                     1 - {CUSTOMER_GROWTH} * months_between(trunc(date'{AS_OF}', 'MM'), m.month) / 12 growth
              FROM months m JOIN (VALUES {season}) s(m, season) ON s.m = month(m.month) CROSS JOIN base b),
      mix AS (SELECT n.tariff, n.customer_type, count(*) n FROM {prefix}gld_nmi n GROUP BY 1, 2),
      net AS (SELECT c.month, sum(x.n * t.fixed_day) * c.days * c.growth fixed, sum(x.n * t.kwh_year * t.usage_kwh) / 12 * c.season * c.growth usage
              FROM cal c CROSS JOIN mix x JOIN (VALUES {tariff}) t(tariff, customer_type, fixed_day, usage_kwh, kwh_year) ON t.tariff = x.tariff AND t.customer_type = x.customer_type
              GROUP BY c.month, c.days, c.season, c.growth),
      rel AS (SELECT trunc(day, 'MM') month, sum(CASE WHEN NOT is_major_event_day THEN saidi ELSE 0 END) saidi, count(*) days
              FROM {prefix}gld_reliability_daily GROUP BY 1),
      wo AS (SELECT trunc(completed_date, 'MM') month, sum(cost_aud) cost FROM {prefix}gld_work_order
             WHERE completed_date <= date'{AS_OF}' AND order_type <> 'PM04' GROUP BY 1),
      outg AS (SELECT trunc(outage_date, 'MM') month, count(*) n FROM {prefix}slv_outage WHERE NOT momentary AND NOT planned GROUP BY 1),
      line AS (
        SELECT c.month, 'network_fixed' line, n.fixed amt, n.fixed bud, false twin,
               'Customers on each network tariff times its daily fixed charge times the days in the month (illustrative tariffs).' basis
        FROM cal c JOIN net n USING (month)
        UNION ALL SELECT c.month, 'network_usage', n.usage, n.usage, false,
               'Annual consumption per customer type times the usage charge, shaped by the season (illustrative tariffs).' FROM cal c JOIN net n USING (month)
        UNION ALL SELECT month, 'metering', {METERING_PER_DAY} * customers * days * growth, {METERING_PER_DAY} * customers * days * growth, false,
               'Metering services: AUD {METERING_PER_DAY} a customer a day (illustrative).' FROM cal
        UNION ALL SELECT c.month, 'reliability_incentive', coalesce(({SAIDI_TARGET} * r.days / 365 - r.saidi) * {STPIS_PER_SAIDI_MIN}, 0), 0, true,
               'SAIDI target to date less actual SAIDI (major event days excluded) times AUD {int(STPIS_PER_SAIDI_MIN):,} a minute: a reward when better than target, a penalty when worse.'
        FROM cal c LEFT JOIN rel r USING (month)
        UNION ALL SELECT c.month, 'corrective_maintenance', coalesce(w.cost, 0), {CORRECTIVE_BUDGET_MONTH}, true,
               'Cost of the repair work orders completed in the month, from the asset register (routine inspections are in preventive maintenance).' FROM cal c LEFT JOIN wo w USING (month)
        UNION ALL SELECT c.month, 'emergency_response', coalesce(o.n, 0) * {EMERGENCY_PER_OUTAGE}, {EMERGENCY_BUDGET_MONTH}, true,
               'Unplanned sustained outages in the month times AUD {int(EMERGENCY_PER_OUTAGE):,} each (crews, overtime, materials).' FROM cal c LEFT JOIN outg o USING (month)
        UNION ALL SELECT month, 'vegetation', {VEG_BAYS_PER_MONTH * VEG_COST_PER_BAY}, {VEG_BAYS_PER_MONTH * VEG_COST_PER_BAY}, false,
               '{VEG_BAYS_PER_MONTH:,} bays cut a month at AUD {int(VEG_COST_PER_BAY)} a bay (illustrative).' FROM cal
        UNION ALL {opex}
        UNION ALL SELECT c.month, 'depreciation', {rab} * {DEPRECIATION_RATE} / 12, {rab} * {DEPRECIATION_RATE} / 12, false,
               'Regulated asset base (illustrative AUD {int(RAB_PER_CUSTOMER):,} a customer) times {DEPRECIATION_RATE * 100:.1f}% a year.' FROM cal c
        UNION ALL SELECT c.month, 'finance_cost', {rab} * {GEARING} * {COST_OF_DEBT} / 12, {rab} * {GEARING} * {COST_OF_DEBT} / 12, false,
               'Debt at {int(GEARING * 100)}% of the regulated asset base times a {COST_OF_DEBT * 100:.1f}% cost of debt.' FROM cal c)
      SELECT month, CASE WHEN month(month) >= 7 THEN year(month) + 1 ELSE year(month) END regulatory_year, line,
             round(amt, 2) actual_aud, round(bud, 2) budget_aud, twin is_twin_driven, basis FROM line"""
