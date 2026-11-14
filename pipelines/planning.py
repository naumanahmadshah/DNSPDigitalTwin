# Planning gold: the inputs to the AI-aided Plan and Invest screen (forecast need, constraints, intervention options with costs and carbon,
# network-aware work bundles, crew capacity). Every number is rule-based and reproducible from the gold and silver beneath it; the
# app's Opus steps draft narrative and rankings from these marts and never change them. Planning assumptions are a table, so a planner
# can see every rate the numbers use.
#
# SQL lives in the SQL dict so the same text runs here (as materialized views) and in the local prototype runner, which composes the
# marts as CTEs against the warehouse. Table names are unqualified, as in gold.py (the pipeline's catalog and schema).
from pyspark import pipelines as dp
from pyspark.sql import functions as F

# ------------------------------------------------------------------ planning assumptions (key, value, unit, note)
ASSUMPTIONS = [
    ("plan_first_fy", 2027, "year", "First financial year in the plan (FY27 runs 1 July 2026 to 30 June 2027)."),
    ("plan_years", 10, "years", "Length of the demand projection and of the ten year cost."),
    ("power_factor", 0.95, "ratio", "Converts a transformer or feeder rating in MVA to MW for the N-1 comparison."),
    ("coincident_peak_share", 0.85, "ratio", "Share of the sum of transformer peaks that is coincident at zone level (transformer peaks do not all fall together)."),
    ("growth_floor_pa", 0.02, "per year", "Lowest annual peak growth applied to a zone (planning floor for the growth corridor; the last 12 month trend is flat or falling in most metro zones)."),
    ("growth_cap_pa", 0.06, "per year", "Highest annual peak growth applied to a zone (caps the 12 month trend in the fastest growing rural zones)."),
    ("ev_kw_per_charger", 7.0, "kW", "Average charging demand of one home EV charger at the evening peak."),
    ("ev_coincidence", 0.3, "ratio", "Share of EV chargers charging at the same time as the zone peak."),
    ("ev_growth_pa", 0.25, "per year", "Growth in EV charging demand each year from today's base."),
    ("hosting_reverse_flow_limit", 0.5, "index", "Midday reverse-flow index above which a distribution substation is over its rooftop solar hosting limit."),
    ("voltage_share_threshold", 0.15, "ratio", "Share of a feeder's distribution substations over the hosting limit that makes a voltage constraint."),
    ("span_risk_threshold", 0.6, "probability", "Span fault probability above which a span counts as high risk."),
    ("span_share_threshold", 0.3, "ratio", "Share of a feeder's spans that are high risk that makes an asset risk constraint."),
    ("feeder_util_threshold", 0.75, "ratio", "Feeder peak current over its rating in the last 30 days that makes a capacity constraint."),
    ("zone_util_threshold", 0.8, "ratio", "Zone share of its N-1 firm capacity in the first year that makes a capacity constraint (a projected breach also does)."),
    ("sustained_outage_threshold", 8, "events", "Sustained interruptions on a feeder in 12 months that make a reliability constraint."),
    ("crew_day_hours", 10, "hours", "Hours a crew can plan on the tools each day (the enterprise agreement cap is 12)."),
    ("availability_share", 0.9, "ratio", "Share of a crew's rostered time available after leave, training and travel."),
    ("window_days", 7, "days", "Days in the planning window (the planned work orders in gold cover seven days)."),
    ("programme_share_of_spare", 0.5, "ratio", "Share of spare crew hours ring-fenced for programme work; the rest is headroom for faults."),
    ("switching_crews_per_region", 2, "crews", "Switching crews per region that can isolate and restore one work bundle each day."),
    ("tie_transfer_share", 0.5, "ratio", "Share of a feeder's recent peak current that a normally open point can take over in a transfer."),
    ("contingency", 0.15, "ratio", "Contingency added to capital costs."),
    ("opex_years", 10, "years", "Years of operating cost in the ten year cost."),
    ("monitoring_cost_pa", 25000, "AUD a year", "Cost of monitoring a constraint each year while it is deferred."),
    ("arterial_share_threshold", 0.4, "ratio", "Share of a feeder's customer addresses on arterial roads (Road, Parade) that makes traffic management likely for road works."),
    ("kv_distribution", 11, "kV", "Distribution voltage used to turn feeder current into MVA."),
    ("ts_new_transformer_mva", 90, "MVA", "Rating of a new transmission substation transformer in the transmission option (sized to the 60 to 120 MVA transformers in the network)."),
    ("planned_notice_business_days", 4, "business days", "Notice a customer gets before a planned interruption (the notices engine applies this rule)."),
    ("crew_size", 2, "workers", "Field workers in one crew (labour cost of a crew hour is this times the field worker rate)."),
    ("works_contingency", 0.1, "ratio", "Contingency on the direct cost of programme works (labour, materials, plant, traffic control and switching)."),
]

# ------------------------------------------------------------------ unit costs (item, label, unit, cost AUD, life years, basis)
UNIT_COST = [
    ("zs_transformer_33mva", "33 MVA zone substation transformer, installed", "each", 8500000, 45, "Planning estimate installed, incl. bays and protection"),
    ("second_feed_zone", "Second subtransmission feed to a zone substation", "each", 14000000, 50, "Planning estimate for a new line and bays"),
    ("transmission_transformer", "Transmission substation transformer (90 MVA class), installed", "each", 21000000, 45, "Planning estimate installed incl. 132 kV bays and protection"),
    ("battery_2h_mw", "Battery storage, two hours, per MW", "MW", 1150000, 12, "Planning estimate installed"),
    ("dr_contract_mw_yr", "Demand response contract, per MW a year", "MW year", 165000, 1, "Planning estimate for an aggregator contract"),
    ("reconductor_km", "Overhead reconductor, per km", "km", 380000, 50, "Planning estimate, aluminium conductor on existing poles"),
    ("pole_replace", "Pole replacement, incl. crossarm and spans", "pole", 8450, 50, "Wood pole replacement at 11 kV: a NSW distributor's historical unit cost, EMCa review for the AER (August 2025), FY2026 dollars"),
    ("defect_repair", "Defect repair, average", "defect", 2400, 1, "Average crew cost of a priority defect repair"),
    ("ds_transformer_upgrade", "Distribution transformer upgrade, 100 kVA", "each", 42000, 40, "Planning estimate"),
    ("voltage_regulator", "Voltage regulator on a feeder", "each", 260000, 25, "Planning estimate installed"),
    ("volt_var_settings", "Volt-var settings and export limits for DER on a feeder", "feeder", 80000, 10, "Planning estimate for settings and CSIP-AUS integration"),
    ("tie_switch_auto", "Automated normally open point (switch and control)", "each", 140000, 25, "Planning estimate installed"),
    ("switching_study", "Load transfer study and switching plan", "each", 45000, 1, "Planning estimate per transfer"),
    ("flisr_device", "Recloser or sectionaliser with FLISR control", "each", 140000, 20, "Planning estimate installed"),
    ("flisr_control", "FLISR control and communications for a feeder", "feeder", 120000, 15, "Planning estimate"),
    ("veg_cut_span", "Vegetation cut per span, one cycle", "span", 650, 1, "Average cut cost per span on a cycle"),
    ("oil_monitoring_install", "Oil treatment and continuous gas monitoring", "each", 150000, 10, "Planning estimate per transformer"),
    ("dga_monitoring_pa", "Gas-in-oil monitoring a year", "each year", 18000, 1, "Planning estimate per transformer"),
    ("covered_conductor_km", "Covered conductor on existing poles, per km", "km", 320000, 45, "Planning estimate for a retrofit on the existing poles"),
    ("bushfire_patrol_span", "Pre-summer bushfire patrol and treatment, per span a year", "span year", 95, 1, "Planning estimate: patrol, clearance check and minor treatment"),
    ("animal_guard_site", "Animal guards at a distribution substation or pole top", "site", 380, 15, "Planning estimate, guards and fitting"),
    ("surge_arrester_set", "Surge arrester set, three phase, installed", "set", 2800, 25, "Planning estimate"),
    ("cable_replace_km", "Replace an underground cable section with a larger cable, per km", "km", 1100000, 50, "Planning estimate incl. trenching and reinstatement"),
    # the cost of a work bundle: labour, materials by work type, plant, traffic control, switching and the customer notices
    ("field_worker_hour", "Field worker, business hours", "worker hour", 196.41, 1, "AER 2024-29 final decision, outdoor field worker R4 quoted-service rate (2024-25 dollars)"),
    ("mat_lv_maint", "Materials for an LV maintenance item", "item", 420, 1, "Planning estimate: fuses, connectors, crossarm hardware"),
    ("mat_hv_maint", "Materials for an HV maintenance item", "item", 1850, 1, "Planning estimate: insulators, ties, switch parts"),
    ("mat_cable_repair", "Materials for a cable repair", "item", 4600, 1, "Planning estimate: joint kit, cable length, pit and reinstatement"),
    ("mat_substation_maint", "Materials for distribution substation maintenance", "item", 2300, 1, "Planning estimate: oil, gaskets, LV board parts"),
    ("mat_protection_test", "Consumables for a protection test", "item", 260, 1, "Planning estimate"),
    ("mat_civil", "Materials for civil works", "item", 3400, 1, "Planning estimate: conduit, pits, concrete and reinstatement"),
    ("mat_customer_connection", "Materials for a customer connection", "item", 1150, 1, "Planning estimate: service cable, fuse and meter panel parts"),
    ("ewp_day", "Elevated work platform truck, dry hire", "day", 650, 1, "Sydney truck-mounted EWP hire, A$350 to A$700 a day dry hire (2025 market rates)"),
    ("excavation_day", "Vacuum excavation truck with operator", "day", 1650, 1, "Planning estimate for cable and civil works"),
    ("traffic_control_day", "Traffic control: two controllers, a ute and a traffic guidance scheme, 10 hour day", "day", 1770, 1, "NSW 2025 published rates: two controllers A$128 an hour, ute A$24 an hour, guidance scheme A$250"),
    ("switching_outage", "Switching and outage management for one planned outage", "outage", 850, 1, "Three hours of a switching operator at the AER technical specialist rate, plus outage planning"),
    ("notice_per_customer", "Customer notice, blended across SMS, email and letters", "customer", 0.65, 1, "Planning estimate: letters at about A$1.60, SMS and email a few cents"),
    ("life_support_call", "Contact centre call to a life support customer", "call", 14, 1, "Planning estimate: about ten minutes of an agent's time"),
]

# ------------------------------------------------------------------ carbon factors (factor, label, value, unit, basis)
CARBON = [
    ("grid_intensity", "Grid electricity emissions factor (NSW)", 0.66, "kg CO2e per kWh", "Planning assumption for NSW scope 2; refresh from the annual factors"),
    ("transformer_embodied", "Embodied carbon, zone substation transformer", 2.6, "t CO2e per MVA", "Planning assumption"),
    ("conductor_embodied", "Embodied carbon, overhead conductor", 9.0, "t CO2e per km", "Planning assumption"),
    ("pole_embodied", "Embodied carbon, replacement pole", 0.18, "t CO2e per pole", "Planning assumption"),
    ("battery_embodied", "Embodied carbon, lithium iron phosphate battery", 0.11, "t CO2e per kWh", "Planning assumption"),
    ("battery_cycles_pa", "Battery cycles a year", 300, "cycles", "Planning assumption"),
    ("battery_round_trip_loss", "Battery round trip losses", 0.15, "share of throughput", "Planning assumption"),
    ("veg_cut_per_span", "Emissions from one vegetation cut per span", 0.004, "t CO2e per span", "Crew travel and chipping, planning assumption"),
    ("defect_repair_per_defect", "Emissions from one defect repair", 0.002, "t CO2e per defect", "Crew travel, planning assumption"),
    ("switching_op", "Emissions from one load transfer", 0.5, "t CO2e per transfer", "Crew travel and switching, planning assumption"),
]

# ------------------------------------------------------------------ SQL
# Parameter, carbon and unit cost CTEs are generated from the lists above (the pipeline and the prototype build identical text).
PRM = "prm AS (SELECT " + ", ".join(f"max(CASE WHEN key = '{k}' THEN value END) AS {k}" for k, *_ in ASSUMPTIONS) + " FROM gld_plan_assumption)"
CF = "cf AS (SELECT " + ", ".join(f"max(CASE WHEN factor = '{k}' THEN value END) AS {k}" for k, *_ in CARBON) + " FROM gld_plan_carbon_factor)"
UC = "uc AS (SELECT " + ", ".join(f"max(CASE WHEN item = '{k}' THEN cost_aud END) AS {k}" for k, *_ in UNIT_COST) + " FROM gld_plan_unit_cost)"

SQL = {}

# Zone demand: one row per zone substation and financial year over the plan. The base is the coincident summer peak (12 month history)
# or the fortnight forecast, whichever is higher; growth is the 12 month trend within the floor and cap; EV charging adds on top.
SQL["gld_plan_zone_demand"] = """
WITH daily AS (
  SELECT zs_id, day, sum(peak_mw) AS zone_peak_mw FROM gld_zone_load_daily GROUP BY zs_id, day
),
trend AS (
  SELECT zs_id, max(zone_peak_mw) AS sum_peak_mw, regr_slope(zone_peak_mw, unix_date(day)) * 365.0 / avg(zone_peak_mw) AS slope_pa
  FROM daily GROUP BY zs_id
),
fc AS (SELECT zs_id, max(peak_mw_forecast) AS fortnight_peak_mw FROM ml_zone_load_forecast GROUP BY zs_id),
tx AS (SELECT zs_id, sum(rating_mva) AS tx_mva, max(rating_mva) AS largest_tx_mva FROM gld_zone_transformer_state GROUP BY zs_id),
ev AS (SELECT zs_id, sum(coalesce(ev_chargers, 0)) AS ev_chargers, sum(coalesce(solar_kw, 0)) / 1000.0 AS solar_mw FROM gld_ds_state GROUP BY zs_id),
fs AS (SELECT zs_id, sum(customers) AS customers, sum(life_support) AS life_support FROM gld_feeder_state GROUP BY zs_id),
""" + PRM + """,
base AS (
  SELECT z.zs_id, z.zs_name, z.area, coalesce(z.single_fed, false) AS single_fed, z.circuits_firm_mva,
         coalesce(tx.tx_mva, 0) AS tx_mva, coalesce(tx.largest_tx_mva, 0) AS largest_tx_mva,
         coalesce(t.sum_peak_mw, 0) * prm.coincident_peak_share AS hist_peak_mw,
         coalesce(f.fortnight_peak_mw, 0) AS fortnight_peak_mw,
         greatest(prm.growth_floor_pa, least(prm.growth_cap_pa, coalesce(t.slope_pa, 0))) AS growth_pa,
         coalesce(e.ev_chargers, 0) * prm.ev_kw_per_charger * prm.ev_coincidence / 1000.0 AS ev_mw_now,
         coalesce(e.solar_mw, 0) AS solar_mw,
         coalesce(fs.customers, 0) AS customers, coalesce(fs.life_support, 0) AS life_support
  FROM gld_zone_supply z
  CROSS JOIN prm
  LEFT JOIN trend t ON t.zs_id = z.zs_id
  LEFT JOIN fc f ON f.zs_id = z.zs_id
  LEFT JOIN tx ON tx.zs_id = z.zs_id
  LEFT JOIN ev e ON e.zs_id = z.zs_id
  LEFT JOIN fs ON fs.zs_id = z.zs_id
),
yrs AS (SELECT explode(sequence(0, CAST(plan_years AS INT) - 1)) AS years_out FROM prm),
proj AS (
  SELECT b.*, y.years_out, prm.plan_first_fy, prm.power_factor, prm.ev_growth_pa,
         greatest(b.hist_peak_mw, b.fortnight_peak_mw) AS base_peak_mw,
         CASE WHEN b.single_fed THEN 0.0 ELSE least(b.tx_mva - b.largest_tx_mva, coalesce(nullif(b.circuits_firm_mva, 0), b.tx_mva)) END AS firm_mva
  FROM base b CROSS JOIN yrs y CROSS JOIN prm
)
SELECT zs_id, zs_name, area, single_fed,
       CAST(plan_first_fy + years_out AS INT) AS fiscal_year,
       CAST(years_out AS INT) AS years_out,
       round(fortnight_peak_mw, 2) AS fortnight_peak_mw,
       round(hist_peak_mw, 2) AS hist_peak_mw,
       round(growth_pa, 4) AS growth_pa,
       round(ev_mw_now, 2) AS ev_mw_now,
       round(solar_mw, 2) AS solar_mw,
       round(tx_mva, 1) AS tx_mva,
       round(firm_mva, 1) AS firm_mva,
       round(base_peak_mw * pow(1 + growth_pa, years_out) + ev_mw_now * (pow(1 + ev_growth_pa, years_out) - 1), 2) AS peak_mw,
       CASE WHEN firm_mva > 0 THEN round(100 * (base_peak_mw * pow(1 + growth_pa, years_out) + ev_mw_now * (pow(1 + ev_growth_pa, years_out) - 1)) / (firm_mva * power_factor), 1) END AS utilisation_pct,
       round(firm_mva * power_factor - (base_peak_mw * pow(1 + growth_pa, years_out) + ev_mw_now * (pow(1 + ev_growth_pa, years_out) - 1)), 2) AS headroom_mw,
       (firm_mva = 0 OR firm_mva * power_factor < base_peak_mw * pow(1 + growth_pa, years_out) + ev_mw_now * (pow(1 + ev_growth_pa, years_out) - 1)) AS over_firm,
       customers, life_support
FROM proj
"""

# Constraint register: the zones and feeders, transformers and feeders that breach or approach a limit. Severity is a rule-based
# 0 to 100 index for ranking, not a probability.
SQL["gld_plan_constraint"] = """
WITH zd AS (SELECT * FROM gld_plan_zone_demand WHERE years_out = 0),
zb AS (SELECT zs_id, min(CASE WHEN over_firm THEN fiscal_year END) AS breach_fy FROM gld_plan_zone_demand GROUP BY zs_id),
ds AS (SELECT feeder_id, count(*) AS ds_count,
              sum(CASE WHEN est_midday_reverse_flow > hosting_reverse_flow_limit THEN 1 ELSE 0 END) AS ds_over
       FROM gld_ds_state CROSS JOIN (SELECT max(CASE WHEN key = 'hosting_reverse_flow_limit' THEN value END) AS hosting_reverse_flow_limit
                                     FROM gld_plan_assumption) h
       GROUP BY feeder_id),
sp AS (SELECT feeder_id, count(*) AS spans, sum(CASE WHEN fault_probability >= span_risk_threshold THEN 1 ELSE 0 END) AS spans_high
       FROM ml_span_risk CROSS JOIN (SELECT max(CASE WHEN key = 'span_risk_threshold' THEN value END) AS span_risk_threshold
                                     FROM gld_plan_assumption) h
       GROUP BY feeder_id),
""" + PRM + """,
zone_rows AS (
  SELECT concat('C-SEC-', z.zs_id) AS constraint_id, 'zone_security' AS constraint_type, 'zone' AS constraint_level,
         z.zs_id, z.zs_name, z.area, CAST(NULL AS STRING) AS feeder_id, CAST(NULL AS STRING) AS feeder_name, z.zs_id AS asset_ref,
         'Peak load with no firm supply (MW)' AS metric_label, z.fortnight_peak_mw * 0 + z.peak_mw AS metric_value, 'MW' AS metric_unit,
         0.0 AS threshold, 0 AS years_to_limit, 100 AS severity, z.customers, z.life_support,
         coalesce(cs.critical_sites, 0) AS critical_sites, CAST(NULL AS DOUBLE) AS bushfire_share,
         concat(z.zs_name, ' has no firm supply: one subtransmission feed serves a peak of ', CAST(round(z.peak_mw, 1) AS STRING),
                ' MW to ', CAST(z.customers AS STRING), ' customers.') AS summary
  FROM zd z
  LEFT JOIN (SELECT zs_id, sum(critical_sites) AS critical_sites FROM gld_feeder_state GROUP BY zs_id) cs ON cs.zs_id = z.zs_id
  WHERE z.single_fed
),
zone_cap AS (
  SELECT concat('C-ZCAP-', z.zs_id) AS constraint_id, 'zone_capacity' AS constraint_type, 'zone' AS constraint_level,
         z.zs_id, z.zs_name, z.area, CAST(NULL AS STRING) AS feeder_id, CAST(NULL AS STRING) AS feeder_name, z.zs_id AS asset_ref,
         'Share of N-1 firm capacity used in the first year (%)' AS metric_label, z.utilisation_pct AS metric_value, '%' AS metric_unit,
         100.0 AS threshold,
         CASE WHEN b.breach_fy IS NULL THEN NULL ELSE CAST(b.breach_fy - z.fiscal_year AS INT) END AS years_to_limit,
         least(100, CAST(round(z.utilisation_pct) AS INT)) AS severity, z.customers, z.life_support,
         coalesce(cs.critical_sites, 0) AS critical_sites, CAST(NULL AS DOUBLE) AS bushfire_share,
         CASE WHEN b.breach_fy IS NULL
              THEN concat(z.zs_name, ' uses ', CAST(round(z.utilisation_pct) AS STRING), '% of its N-1 firm capacity in FY',
                          lpad(CAST(mod(z.fiscal_year, 100) AS STRING), 2, '0'), ' and stays inside it over the plan.')
              WHEN b.breach_fy <= z.fiscal_year
              THEN concat(z.zs_name, ' uses ', CAST(round(z.utilisation_pct) AS STRING), '% of its N-1 firm capacity and already exceeds it.')
              ELSE concat(z.zs_name, ' uses ', CAST(round(z.utilisation_pct) AS STRING), '% of its N-1 firm capacity and is projected to exceed it in FY',
                          lpad(CAST(mod(b.breach_fy, 100) AS STRING), 2, '0'), '.') END AS summary
  FROM zd z
  JOIN zb b ON b.zs_id = z.zs_id
  LEFT JOIN (SELECT zs_id, sum(critical_sites) AS critical_sites FROM gld_feeder_state GROUP BY zs_id) cs ON cs.zs_id = z.zs_id
  CROSS JOIN prm
  WHERE NOT z.single_fed AND (z.utilisation_pct >= prm.zone_util_threshold * 100 OR b.breach_fy IS NOT NULL)
),
feeder_cap AS (
  SELECT concat('C-FCAP-', fs.feeder_id) AS constraint_id, 'feeder_capacity' AS constraint_type, 'feeder' AS constraint_level,
         fs.zs_id, fs.zs_name, fs.area, fs.feeder_id, fs.feeder_name, fs.feeder_id AS asset_ref,
         'Peak current over the last 30 days (% of rating)' AS metric_label, round(100 * fs.peak_utilisation_30d, 1) AS metric_value, '%' AS metric_unit,
         100.0 AS threshold,
         CASE WHEN fs.peak_utilisation_30d >= 1 THEN 0
              ELSE CAST(ceil(ln(1 / fs.peak_utilisation_30d) / ln(1 + coalesce(z.growth_pa, prm.growth_floor_pa))) AS INT) END AS years_to_limit,
         least(100, CAST(round(100 * fs.peak_utilisation_30d) AS INT) + CASE WHEN fs.life_support > 0 THEN 5 ELSE 0 END) AS severity,
         fs.customers, fs.life_support, fs.critical_sites, fs.bushfire_share,
         concat(fs.feeder_name, ' carries ', CAST(round(100 * fs.peak_utilisation_30d) AS STRING), '% of its rating at peak on ', fs.zs_name,
                ', and ', CAST(fs.customers AS STRING), ' customers depend on it.') AS summary
  FROM gld_feeder_state fs
  CROSS JOIN prm
  LEFT JOIN zd z ON z.zs_id = fs.zs_id
  WHERE fs.peak_utilisation_30d >= prm.feeder_util_threshold
),
feeder_volt AS (
  SELECT concat('C-VOLT-', fs.feeder_id) AS constraint_id, 'feeder_voltage' AS constraint_type, 'feeder' AS constraint_level,
         fs.zs_id, fs.zs_name, fs.area, fs.feeder_id, fs.feeder_name, fs.feeder_id AS asset_ref,
         'Distribution substations over the solar hosting limit at midday (%)' AS metric_label,
         round(100 * ds.ds_over / ds.ds_count, 1) AS metric_value, '%' AS metric_unit,
         100 * prm.voltage_share_threshold AS threshold, 0 AS years_to_limit,
         least(100, CAST(round(100 * (ds.ds_over / ds.ds_count) / prm.voltage_share_threshold * 50) AS INT)) AS severity,
         fs.customers, fs.life_support, fs.critical_sites, fs.bushfire_share,
         concat(CAST(round(100 * ds.ds_over / ds.ds_count) AS STRING), '% of the distribution substations on ', fs.feeder_name,
                ' export above the hosting limit at midday, so voltage rises on sunny days.') AS summary
  FROM gld_feeder_state fs
  JOIN ds ON ds.feeder_id = fs.feeder_id
  CROSS JOIN prm
  WHERE ds.ds_over / ds.ds_count >= prm.voltage_share_threshold
),
feeder_risk AS (
  SELECT concat('C-RISK-', fs.feeder_id) AS constraint_id, 'feeder_asset_risk' AS constraint_type, 'feeder' AS constraint_level,
         fs.zs_id, fs.zs_name, fs.area, fs.feeder_id, fs.feeder_name, fs.feeder_id AS asset_ref,
         'Overhead spans with a high fault probability (%)' AS metric_label,
         round(100 * sp.spans_high / sp.spans, 1) AS metric_value, '%' AS metric_unit,
         100 * prm.span_share_threshold AS threshold, 0 AS years_to_limit,
         least(100, CAST(round(100 * (sp.spans_high / sp.spans) / prm.span_share_threshold * 50) AS INT)) AS severity,
         fs.customers, fs.life_support, fs.critical_sites, fs.bushfire_share,
         concat(CAST(round(100 * sp.spans_high / sp.spans) AS STRING), '% of the overhead spans on ', fs.feeder_name,
                ' have a high fault probability, and ', CAST(fs.open_defects AS STRING), ' defects are open on the feeder.') AS summary
  FROM gld_feeder_state fs
  JOIN sp ON sp.feeder_id = fs.feeder_id
  CROSS JOIN prm
  WHERE sp.spans_high / sp.spans >= prm.span_share_threshold
),
tx_risk AS (
  SELECT concat('C-TXRISK-', t.transformer_id) AS constraint_id, 'transformer_asset_risk' AS constraint_type, 'transformer' AS constraint_level,
         t.zs_id, z.zs_name, z.area, CAST(NULL AS STRING) AS feeder_id, CAST(NULL AS STRING) AS feeder_name, t.transformer_id AS asset_ref,
         'Hydrogen in the oil: rise over 90 days (ppm)' AS metric_label, round(t.h2_rise_90d, 0) AS metric_value, 'ppm' AS metric_unit,
         100.0 AS threshold, 0 AS years_to_limit,
         CASE WHEN t.status = 'red' THEN 90 ELSE 60 END AS severity,
         coalesce(fs.customers, 0) AS customers, coalesce(fs.life_support, 0) AS life_support, coalesce(fs.critical_sites, 0) AS critical_sites,
         CAST(NULL AS DOUBLE) AS bushfire_share,
         concat(z.zs_name, ' transformer ', t.transformer_id, ' rose ', CAST(round(t.h2_rise_90d, 0) AS STRING),
                ' ppm of hydrogen in its oil in 90 days, a sign of a developing fault.') AS summary
  FROM gld_zone_transformer_state t
  JOIN gld_zone_supply z ON z.zs_id = t.zs_id
  LEFT JOIN (SELECT zs_id, sum(customers) AS customers, sum(life_support) AS life_support, sum(critical_sites) AS critical_sites
             FROM gld_feeder_state GROUP BY zs_id) fs ON fs.zs_id = t.zs_id
  CROSS JOIN prm
  WHERE t.status = 'red' OR t.h2_rise_90d >= 100
),
feeder_rel AS (
  SELECT concat('C-REL-', fs.feeder_id) AS constraint_id, 'feeder_reliability' AS constraint_type, 'feeder' AS constraint_level,
         fs.zs_id, fs.zs_name, fs.area, fs.feeder_id, fs.feeder_name, fs.feeder_id AS asset_ref,
         'Sustained interruptions in the last 12 months (count)' AS metric_label, CAST(fs.sustained_outages_12m AS DOUBLE) AS metric_value,
         'interruptions' AS metric_unit, CAST(2 * prm.sustained_outage_threshold AS DOUBLE) AS threshold, 0 AS years_to_limit,
         least(100, CAST(round(100 * fs.sustained_outages_12m / (2 * prm.sustained_outage_threshold)) AS INT)) AS severity,
         fs.customers, fs.life_support, fs.critical_sites, fs.bushfire_share,
         concat(fs.feeder_name, ' had ', CAST(fs.sustained_outages_12m AS STRING), ' sustained interruptions in 12 months, and ',
                CAST(fs.veg_cat_a + fs.veg_cat_b AS STRING), ' spans need vegetation work.') AS summary
  FROM gld_feeder_state fs
  CROSS JOIN prm
  WHERE fs.sustained_outages_12m >= prm.sustained_outage_threshold
),
all_rows AS (
  SELECT * FROM zone_rows UNION ALL SELECT * FROM zone_cap UNION ALL SELECT * FROM feeder_cap UNION ALL SELECT * FROM feeder_volt
  UNION ALL SELECT * FROM feeder_risk UNION ALL SELECT * FROM tx_risk UNION ALL SELECT * FROM feeder_rel
)
SELECT a.constraint_id, a.constraint_type, lbl.label AS constraint_type_label, a.constraint_level, a.zs_id, a.zs_name, a.area,
       a.feeder_id, a.feeder_name, a.asset_ref, a.metric_label, CAST(a.metric_value AS DOUBLE) AS metric_value, a.metric_unit,
       CAST(a.threshold AS DOUBLE) AS threshold, a.years_to_limit, CAST(a.severity AS INT) AS severity, CAST(a.customers AS INT) AS customers,
       CAST(a.life_support AS INT) AS life_support, CAST(a.critical_sites AS INT) AS critical_sites, a.bushfire_share, a.summary
FROM all_rows a
LEFT JOIN ref_label lbl ON lbl.domain = 'constraint_type' AND lbl.code = a.constraint_type
"""

# Intervention options: every constraint gets the options that apply to its type, each with a cost (capital and ten year operating),
# relief in MW, customers protected, asset risk and reliability effect, carbon over the ten years and a lead time. The planner's agent
# scores them; the numbers here are the facts behind the scores.
SQL["gld_plan_intervention"] = """
WITH zd AS (SELECT * FROM gld_plan_zone_demand WHERE years_out = 0),
tie AS (SELECT feeder_id, max(transfer_amps) AS tie_transfer_amps FROM gld_feeder_tie GROUP BY feeder_id),
-- each feeder's leading causes of sustained unplanned outages (the options answer the causes the feeder actually has)
oc AS (SELECT feeder_id, cause_code, count(*) AS n,
              row_number() OVER (PARTITION BY feeder_id ORDER BY count(*) DESC, cause_code) AS rn
       FROM gld_outage WHERE NOT planned AND NOT momentary AND feeder_id IS NOT NULL GROUP BY feeder_id, cause_code),
cz AS (SELECT feeder_id,
              max(CASE WHEN cause_code = 'VEG' AND rn <= 2 THEN 1 ELSE 0 END) AS veg_lead,
              max(CASE WHEN cause_code = 'ANM' AND rn <= 3 THEN 1 ELSE 0 END) AS animal_lead,
              max(CASE WHEN cause_code = 'LTN' AND rn <= 3 THEN 1 ELSE 0 END) AS lightning_lead,
              max(CASE WHEN cause_code = 'WND' AND rn <= 3 THEN 1 ELSE 0 END) AS wind_lead,
              sum(CASE WHEN cause_code = 'ANM' THEN n ELSE 0 END) AS animal_outages,
              sum(CASE WHEN cause_code = 'LTN' THEN n ELSE 0 END) AS lightning_outages,
              sum(CASE WHEN cause_code = 'WND' THEN n ELSE 0 END) AS wind_outages,
              sum(CASE WHEN cause_code = 'VEG' THEN n ELSE 0 END) AS veg_outages
       FROM oc GROUP BY feeder_id),
rc AS (SELECT feeder_id, count(*) AS reclosers FROM gld_asset WHERE asset_class = 'recloser' GROUP BY feeder_id),
ds AS (SELECT feeder_id, count(*) AS ds_count,
              sum(CASE WHEN est_midday_reverse_flow > hosting_reverse_flow_limit THEN 1 ELSE 0 END) AS ds_over
       FROM gld_ds_state CROSS JOIN (SELECT max(CASE WHEN key = 'hosting_reverse_flow_limit' THEN value END) AS hosting_reverse_flow_limit
                                     FROM gld_plan_assumption) h GROUP BY feeder_id),
sp AS (SELECT feeder_id, count(*) AS spans, sum(CASE WHEN fault_probability >= span_risk_threshold THEN 1 ELSE 0 END) AS spans_high
       FROM ml_span_risk CROSS JOIN (SELECT max(CASE WHEN key = 'span_risk_threshold' THEN value END) AS span_risk_threshold
                                     FROM gld_plan_assumption) h GROUP BY feeder_id),
""" + PRM + ",\n" + CF + ",\n" + UC + """,
-- what the feeder ties of a zone's feeders could take over from the zone in a transfer (MW)
zt AS (SELECT fs.zs_id, count(*) AS tie_feeders,
              sum(least(t.tie_transfer_amps, fs.amps_peak_30d * prm.tie_transfer_share) * sqrt(3.0) * prm.kv_distribution / 1000.0) AS tie_mw
       FROM gld_feeder_state fs JOIN tie t ON t.feeder_id = fs.feeder_id CROSS JOIN prm GROUP BY fs.zs_id),
facts AS (
  SELECT c.constraint_id, c.constraint_type, c.zs_id, c.feeder_id, c.asset_ref, c.customers, c.life_support, c.bushfire_share, c.customers AS constraint_customers,
         coalesce(z.peak_mw, 0) AS zone_peak_mw, coalesce(z.firm_mva, 0) AS zone_firm_mva,
         coalesce(fs.overhead_km, 0) AS overhead_km, coalesce(fs.underground_km, 0) AS underground_km,
         coalesce(fs.rating_amps, 0) AS rating_amps, coalesce(fs.amps_peak_30d, 0) AS amps_peak_30d,
         coalesce(fs.open_defects, 0) AS open_defects, coalesce(fs.veg_cat_a, 0) + coalesce(fs.veg_cat_b, 0) AS veg_spans,
         coalesce(fs.customers, 0) AS feeder_customers, coalesce(fs.dist_substations, 0) AS dist_substations,
         coalesce(fs.bushfire_share, 0) AS feeder_bushfire_share,
         coalesce(ds.ds_count, 0) AS ds_count, coalesce(ds.ds_over, 0) AS ds_over,
         coalesce(sp.spans, 0) AS spans, coalesce(sp.spans_high, 0) AS spans_high,
         coalesce(tie.tie_transfer_amps, 0) AS tie_transfer_amps,
         coalesce(tx.rating_mva, 0) AS tx_rating_mva,
         coalesce(cz.veg_lead, 0) AS veg_lead, coalesce(cz.animal_lead, 0) AS animal_lead, coalesce(cz.lightning_lead, 0) AS lightning_lead,
         coalesce(cz.wind_lead, 0) AS wind_lead, coalesce(cz.animal_outages, 0) AS animal_outages,
         coalesce(cz.lightning_outages, 0) AS lightning_outages, coalesce(cz.wind_outages, 0) AS wind_outages,
         coalesce(cz.veg_outages, 0) AS veg_outages, coalesce(rc.reclosers, 0) AS reclosers,
         coalesce(zt.tie_feeders, 0) AS zone_tie_feeders, coalesce(zt.tie_mw, 0) AS zone_tie_mw
  FROM gld_plan_constraint c
  CROSS JOIN prm
  LEFT JOIN zd z ON z.zs_id = c.zs_id AND c.constraint_level = 'zone'
  LEFT JOIN gld_feeder_state fs ON fs.feeder_id = c.feeder_id
  LEFT JOIN ds ON ds.feeder_id = c.feeder_id
  LEFT JOIN sp ON sp.feeder_id = c.feeder_id
  LEFT JOIN tie ON tie.feeder_id = c.feeder_id
  LEFT JOIN cz ON cz.feeder_id = c.feeder_id
  LEFT JOIN rc ON rc.feeder_id = c.feeder_id
  LEFT JOIN zt ON zt.zs_id = c.zs_id AND c.constraint_level = 'zone'
  LEFT JOIN gld_zone_transformer_state tx ON tx.transformer_id = c.asset_ref AND c.constraint_level = 'transformer'
),
opts AS (
  -- zone substations: a new transformer (or a second feed when single fed), storage with demand response, or defer
  SELECT concat(f.constraint_id, '-AUG') AS intervention_id, f.constraint_id, f.constraint_type, 'augment' AS option_type,
         CASE WHEN f.constraint_type = 'zone_security' THEN 'Build a second supply feed and a 33 MVA transformer'
              ELSE 'Add a 33 MVA zone substation transformer' END AS option_label,
         CASE WHEN f.constraint_type = 'zone_security' THEN 'A second subtransmission feed and a new transformer, so the zone keeps supply when a circuit or transformer is lost.'
              ELSE 'A third 33 MVA transformer bay restores N-1 headroom for the planning period.' END AS description,
         f.zs_id, f.feeder_id, f.asset_ref, 1.0 AS quantity, 'transformer' AS quantity_unit,
         uc.zs_transformer_33mva + CASE WHEN f.constraint_type = 'zone_security' THEN uc.second_feed_zone ELSE 0 END AS capex_aud,
         0.0 AS opex_10yr_aud,
         CASE WHEN f.constraint_type = 'zone_security' THEN f.zone_peak_mw ELSE round(33 * prm.power_factor, 1) END AS relief_mw,
         f.constraint_customers AS customers_protected,
         0.0 AS asset_risk_reduction_pct,
         CASE WHEN f.constraint_type = 'zone_security' THEN 50.0 ELSE 15.0 END AS reliability_gain_pct,
         round(33 * cf.transformer_embodied, 1) AS carbon_tco2e_10yr,
         CASE WHEN f.constraint_type = 'zone_security' THEN 48.0 ELSE 36.0 END AS lead_time_months,
         45.0 AS life_years, '' AS climate_note
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type IN ('zone_capacity', 'zone_security')
  UNION ALL
  SELECT concat(f.constraint_id, '-FLX'), f.constraint_id, f.constraint_type, 'flexibility',
         'Battery storage and demand response at the zone',
         'Two hour battery sized to a fifth of the zone peak, capped at 10 MW, with a demand response contract for the evening peak.',
         f.zs_id, f.feeder_id, f.asset_ref, least(10.0, round(0.2 * f.zone_peak_mw, 1)), 'MW of storage',
         least(10.0, round(0.2 * f.zone_peak_mw, 1)) * uc.battery_2h_mw,
         least(10.0, round(0.2 * f.zone_peak_mw, 1)) * uc.dr_contract_mw_yr * prm.opex_years,
         least(10.0, round(0.2 * f.zone_peak_mw, 1)), f.constraint_customers, 0.0, 5.0,
         round(least(10.0, round(0.2 * f.zone_peak_mw, 1)) * 2000 * (cf.battery_embodied + cf.battery_cycles_pa * cf.battery_round_trip_loss * cf.grid_intensity * prm.opex_years / 1000.0), 1),
         12.0, 12.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type IN ('zone_capacity', 'zone_security')
  UNION ALL
  SELECT concat(f.constraint_id, '-ZTR'), f.constraint_id, f.constraint_type, 'reconfigure', 'Transfer load to neighbouring zones on the feeder ties',
         concat('Move load at peak to the neighbouring zones through the normally open points on ', CAST(f.zone_tie_feeders AS STRING),
                ' of the zone''s feeders, after a load transfer study, with automated switches at the ties.'),
         f.zs_id, f.feeder_id, f.asset_ref, f.zone_tie_feeders, 'normally open points',
         f.zone_tie_feeders * (uc.switching_study + uc.tie_switch_auto), 0.0, round(f.zone_tie_mw, 1), round(f.constraint_customers * 0.3),
         0.0, 10.0, f.zone_tie_feeders * cf.switching_op, 6.0, 25.0, ''
  FROM facts f CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'zone_capacity' AND f.zone_tie_mw > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-DEF'), f.constraint_id, f.constraint_type, 'defer', 'Defer and monitor the zone',
         'Keep the zone under watch each year and accept the projected shortfall, with load transfers and emergency generation as the fallback.',
         f.zs_id, f.feeder_id, f.asset_ref, 0.0, 'no works', 0.0, prm.monitoring_cost_pa * prm.opex_years, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, ''
  FROM facts f CROSS JOIN prm
  WHERE f.constraint_type IN ('zone_capacity', 'zone_security')
  UNION ALL
  -- feeders: reconductor the overhead trunk, transfer load through a normally open point, a feeder battery, or defer
  SELECT concat(f.constraint_id, '-RCD'), f.constraint_id, f.constraint_type, 'augment', 'Reconductor the overhead trunk section',
         'Replace the conductor on about 30 per cent of the feeder overhead length with a larger conductor on the same poles.',
         f.zs_id, f.feeder_id, f.asset_ref, round(f.overhead_km * 0.3, 1), 'km of conductor',
         round(f.overhead_km * 0.3, 1) * uc.reconductor_km, 0.0,
         round(f.rating_amps * 0.25 * sqrt(3.0) * prm.kv_distribution / 1000.0, 1), f.feeder_customers, 25.0, 20.0,
         round(round(f.overhead_km * 0.3, 1) * cf.conductor_embodied, 1), 12.0, 50.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_capacity' AND f.overhead_km > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-TIE'), f.constraint_id, f.constraint_type, 'reconfigure', 'Transfer load through a normally open point',
         'Close the normally open point to a neighbouring feeder with spare capacity and open the constrained section, after a load transfer study.',
         f.zs_id, f.feeder_id, f.asset_ref, 1.0, 'normally open point',
         uc.switching_study + uc.tie_switch_auto, 0.0,
         round(least(f.tie_transfer_amps, f.amps_peak_30d * prm.tie_transfer_share) * sqrt(3.0) * prm.kv_distribution / 1000.0, 1),
         round(f.feeder_customers * 0.5), 0.0, 10.0, cf.switching_op, 3.0, 25.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_capacity' AND f.tie_transfer_amps > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-FFX'), f.constraint_id, f.constraint_type, 'flexibility', 'Community battery and demand response on the feeder',
         'A one MW, two hour battery at the feeder substation with a demand response contract for the evening peak.',
         f.zs_id, f.feeder_id, f.asset_ref, 1.0, 'MW of storage', uc.battery_2h_mw, 0.0, 1.0, f.feeder_customers, 0.0, 5.0,
         round(2000 * (cf.battery_embodied + cf.battery_cycles_pa * cf.battery_round_trip_loss * cf.grid_intensity * prm.opex_years / 1000.0), 1),
         9.0, 12.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_capacity'
  UNION ALL
  SELECT concat(f.constraint_id, '-CBL'), f.constraint_id, f.constraint_type, 'replace', 'Replace the limiting cable section with a larger cable',
         'Replace about 15 per cent of the underground length, the section that limits the rating, with a larger cable.',
         f.zs_id, f.feeder_id, f.asset_ref, round(f.underground_km * 0.15, 1), 'km of cable',
         round(f.underground_km * 0.15, 1) * uc.cable_replace_km, 0.0,
         round(f.rating_amps * 0.3 * sqrt(3.0) * prm.kv_distribution / 1000.0, 1), f.feeder_customers, 30.0, 15.0,
         round(round(f.underground_km * 0.15, 1) * cf.conductor_embodied * 1.5, 1), 15.0, 50.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_capacity' AND f.underground_km > f.overhead_km
  UNION ALL
  -- voltage: volt-var settings and export limits, regulators and larger transformers, or defer
  SELECT concat(f.constraint_id, '-VVC'), f.constraint_id, f.constraint_type, 'flexibility', 'Volt-var settings and export limits for rooftop solar',
         'Volt-var control and flexible export limits on the solar inverters (CSIP-AUS), with no new plant.',
         f.zs_id, f.feeder_id, f.asset_ref, 1.0, 'feeder settings', uc.volt_var_settings, 0.0, 0.0,
         round(f.feeder_customers * f.ds_over / greatest(f.ds_count, 1)), 0.0, 0.0, 0.0, 6.0, 10.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_voltage'
  UNION ALL
  SELECT concat(f.constraint_id, '-VREG'), f.constraint_id, f.constraint_type, 'augment', 'Voltage regulators and larger distribution transformers',
         'Voltage regulators on the feeder and larger transformers at the substations over the hosting limit.',
         f.zs_id, f.feeder_id, f.asset_ref, f.ds_over, 'substations upgraded',
         ceil(f.ds_over / 5.0) * uc.voltage_regulator + f.ds_over * uc.ds_transformer_upgrade, 0.0, 0.0,
         round(f.feeder_customers * f.ds_over / greatest(f.ds_count, 1)), 0.0, 0.0, 0.0, 9.0, 30.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_voltage'
  UNION ALL
  -- overhead asset risk and reliability: replacing the worst spans and poles is open to every such feeder; the rest answer the feeder's
  -- own condition (open defects, bushfire exposure) and its leading outage causes (vegetation, animals, lightning, wind-blown debris)
  SELECT concat(f.constraint_id, '-REP'), f.constraint_id, f.constraint_type, 'replace', 'Replace the highest risk spans and poles',
         concat('Replace the ', CAST(f.spans_high AS STRING), ' poles and spans with the highest fault probability on the feeder.'),
         f.zs_id, f.feeder_id, f.asset_ref, f.spans_high, 'poles',
         f.spans_high * uc.pole_replace, 0.0, 0.0, f.feeder_customers,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 70.0 ELSE 30.0 END,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 25.0 ELSE 20.0 END,
         round(f.spans_high * cf.pole_embodied, 1), 6.0, 50.0, ''
  FROM facts f CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type IN ('feeder_asset_risk', 'feeder_reliability') AND f.spans_high > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-DFX'), f.constraint_id, f.constraint_type, 'maintain', concat('Repair the ', CAST(f.open_defects AS STRING), ' open defects on the feeder'),
         'Repair the open priority defects on the feeder in one programme of work, so they stop adding to the fault risk.',
         f.zs_id, f.feeder_id, f.asset_ref, f.open_defects, 'defects',
         f.open_defects * uc.defect_repair, 0.0, 0.0, f.feeder_customers,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 35.0 ELSE 10.0 END, CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 5.0 ELSE 12.0 END,
         round(f.open_defects * cf.defect_repair_per_defect, 2), 3.0, 1.0, ''
  FROM facts f CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type IN ('feeder_asset_risk', 'feeder_reliability') AND f.open_defects > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-BFP'), f.constraint_id, f.constraint_type, 'maintain', 'Patrol and treat the bushfire prone spans before each fire season',
         concat(CAST(round(100 * f.feeder_bushfire_share) AS STRING), '% of the feeder is in bushfire prone land: a patrol before each summer finds and treats the spans that could start a fire.'),
         f.zs_id, f.feeder_id, f.asset_ref, round(f.spans * f.feeder_bushfire_share), 'spans a year',
         0.0, round(f.spans * f.feeder_bushfire_share) * uc.bushfire_patrol_span * prm.opex_years, 0.0, f.feeder_customers, 20.0, 8.0,
         round(round(f.spans * f.feeder_bushfire_share) * cf.veg_cut_per_span * prm.opex_years / 4, 2), 2.0, 1.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_asset_risk' AND f.feeder_bushfire_share >= 0.5
  UNION ALL
  SELECT concat(f.constraint_id, '-VEG'), f.constraint_id, f.constraint_type, 'maintain', 'Vegetation cut cycle on the feeder',
         concat('Vegetation is a leading cause of this feeder''s outages (', CAST(f.veg_outages AS STRING), ' in two years): cut the ',
                CAST(f.veg_spans AS STRING), ' spans that need work on a repeating cycle, starting inside clearance.'),
         f.zs_id, f.feeder_id, f.asset_ref, f.veg_spans, 'spans',
         0.0, f.veg_spans * uc.veg_cut_span * prm.opex_years, 0.0, f.feeder_customers, 10.0, 30.0,
         round(f.veg_spans * cf.veg_cut_per_span * prm.opex_years, 2), 2.0, 1.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_reliability' AND f.veg_lead = 1 AND f.veg_spans > 0
  UNION ALL
  SELECT concat(f.constraint_id, '-FLI'), f.constraint_id, f.constraint_type, 'reconfigure', 'Automate the feeder with reclosers and sectionalisers',
         concat('The feeder has ', CAST(f.reclosers AS STRING), ' recloser', CASE WHEN f.reclosers = 1 THEN '' ELSE 's' END,
                ': three more reclosers or sectionalisers with FLISR control restore most customers by automation, so fewer wait for a crew.'),
         f.zs_id, f.feeder_id, f.asset_ref, 3.0, 'devices',
         3 * uc.flisr_device + uc.flisr_control, 0.0, 0.0, f.feeder_customers, 0.0, 40.0, 0.5, 9.0, 20.0, ''
  FROM facts f CROSS JOIN uc
  WHERE f.constraint_type = 'feeder_reliability' AND f.reclosers <= 2
  UNION ALL
  SELECT concat(f.constraint_id, '-ANG'), f.constraint_id, f.constraint_type, 'maintain', 'Fit animal guards at the substations and pole tops',
         concat('Animals caused ', CAST(f.animal_outages AS STRING), ' of this feeder''s outages in two years: guards at its ',
                CAST(f.dist_substations AS STRING), ' distribution substations keep possums and birds off the live parts.'),
         f.zs_id, f.feeder_id, f.asset_ref, f.dist_substations, 'sites',
         f.dist_substations * uc.animal_guard_site, 0.0, 0.0, f.feeder_customers,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 10.0 ELSE 5.0 END, 15.0,
         round(f.dist_substations * 0.01, 2), 3.0, 15.0, ''
  FROM facts f CROSS JOIN uc
  WHERE f.constraint_type IN ('feeder_asset_risk', 'feeder_reliability') AND f.animal_lead = 1
  UNION ALL
  SELECT concat(f.constraint_id, '-LTA'), f.constraint_id, f.constraint_type, 'augment', 'Surge arresters on the lightning exposed section',
         concat('Lightning caused ', CAST(f.lightning_outages AS STRING), ' of this feeder''s outages in two years: arrester sets about every kilometre of the exposed overhead line protect its transformers and insulators.'),
         f.zs_id, f.feeder_id, f.asset_ref, round(f.overhead_km * 0.5), 'arrester sets',
         round(f.overhead_km * 0.5) * uc.surge_arrester_set, 0.0, 0.0, f.feeder_customers,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 15.0 ELSE 8.0 END, 12.0,
         round(f.overhead_km * 0.5 * 0.05, 2), 4.0, 25.0, ''
  FROM facts f CROSS JOIN uc
  WHERE f.constraint_type IN ('feeder_asset_risk', 'feeder_reliability') AND f.lightning_lead = 1
  UNION ALL
  SELECT concat(f.constraint_id, '-CCV'), f.constraint_id, f.constraint_type, 'replace', 'Covered conductor on the exposed spans',
         concat('Wind-blown debris caused ', CAST(f.wind_outages AS STRING), ' of this feeder''s outages in two years: covered conductor on about a fifth of the overhead length stops clashes and debris faults, and lowers ignition risk.'),
         f.zs_id, f.feeder_id, f.asset_ref, round(f.overhead_km * 0.2, 1), 'km of conductor',
         round(f.overhead_km * 0.2, 1) * uc.covered_conductor_km, 0.0, 0.0, f.feeder_customers,
         CASE WHEN f.constraint_type = 'feeder_asset_risk' THEN 45.0 ELSE 25.0 END, 25.0,
         round(round(f.overhead_km * 0.2, 1) * cf.conductor_embodied, 1), 12.0, 45.0, ''
  FROM facts f CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type IN ('feeder_asset_risk', 'feeder_reliability') AND f.wind_lead = 1 AND f.overhead_km > 0
  UNION ALL
  -- transformers: replace on a rating basis, or oil treatment and gas monitoring, or defer
  SELECT concat(f.constraint_id, '-TXR'), f.constraint_id, f.constraint_type, 'replace', 'Replace the transformer',
         'Replace the transformer like for like, sized to its nameplate rating.',
         f.zs_id, f.feeder_id, f.asset_ref, f.tx_rating_mva, 'MVA',
         f.tx_rating_mva / 33.0 * uc.zs_transformer_33mva, 0.0, 0.0, f.customers, 90.0, 30.0,
         round(f.tx_rating_mva * cf.transformer_embodied, 1), 18.0, 45.0, ''
  FROM facts f CROSS JOIN cf CROSS JOIN uc
  WHERE f.constraint_type = 'transformer_asset_risk'
  UNION ALL
  SELECT concat(f.constraint_id, '-TXM'), f.constraint_id, f.constraint_type, 'maintain', 'Oil treatment and continuous gas monitoring',
         'Treat the oil and fit continuous hydrogen monitoring, with a planned replacement if the rise continues.',
         f.zs_id, f.feeder_id, f.asset_ref, 1.0, 'transformer',
         uc.oil_monitoring_install, uc.dga_monitoring_pa * prm.opex_years, 0.0, f.customers, 40.0, 10.0, 0.0, 2.0, 10.0, ''
  FROM facts f CROSS JOIN prm CROSS JOIN uc
  WHERE f.constraint_type = 'transformer_asset_risk'
  UNION ALL
  -- every constraint can be deferred and monitored
  SELECT concat(f.constraint_id, '-DEF'), f.constraint_id, f.constraint_type, 'defer', 'Defer and monitor',
         'Keep the constraint under watch each year and revisit it at the next plan review.',
         f.zs_id, f.feeder_id, f.asset_ref, 0.0, 'no works', 0.0, prm.monitoring_cost_pa * prm.opex_years, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, ''
  FROM facts f CROSS JOIN prm
  WHERE f.constraint_type IN ('feeder_capacity', 'feeder_voltage', 'feeder_asset_risk', 'transformer_asset_risk', 'feeder_reliability')
)
SELECT o.intervention_id, o.constraint_id, o.constraint_type, lc.label AS constraint_type_label, o.option_type,
       lo.label AS option_type_label, o.option_label, o.description, o.zs_id, o.feeder_id, o.asset_ref,
       CAST(o.quantity AS DOUBLE) AS quantity, o.quantity_unit,
       CAST(round(o.capex_aud, 0) AS DOUBLE) AS capex_aud, CAST(round(o.opex_10yr_aud, 0) AS DOUBLE) AS opex_10yr_aud,
       CAST(round(o.capex_aud * (1 + prm.contingency) + o.opex_10yr_aud, 0) AS DOUBLE) AS cost_10yr_aud,
       CAST(o.relief_mw AS DOUBLE) AS relief_mw, CAST(o.customers_protected AS INT) AS customers_protected,
       CAST(o.asset_risk_reduction_pct AS DOUBLE) AS asset_risk_reduction_pct, CAST(o.reliability_gain_pct AS DOUBLE) AS reliability_gain_pct,
       CAST(o.carbon_tco2e_10yr AS DOUBLE) AS carbon_tco2e_10yr, CAST(o.lead_time_months AS DOUBLE) AS lead_time_months,
       CAST(prm.plan_first_fy + ceil(o.lead_time_months / 12.0) AS INT) AS earliest_fy,
       CAST(o.life_years AS DOUBLE) AS life_years,
       concat(CASE WHEN f.bushfire_share >= 0.5 AND o.option_type IN ('replace', 'maintain', 'reconfigure')
                   THEN 'Bushfire exposed feeder (' || CAST(round(100 * f.bushfire_share) AS STRING) || '% of the length): this option lowers ignition risk in the fire season. '
                   ELSE '' END,
              CASE WHEN o.option_type = 'defer' THEN 'Deferral keeps the risk and the customer exposure on the network. ' ELSE '' END) AS climate_note,
       CASE WHEN o.option_type = 'defer' THEN 'Leaves the customers on this asset exposed to the projected shortfall.' ELSE concat('Protects ', CAST(CAST(coalesce(o.customers_protected, 0) AS BIGINT) AS STRING), ' customers.') END AS community_note
FROM opts o
CROSS JOIN prm
LEFT JOIN facts f ON f.constraint_id = o.constraint_id
LEFT JOIN ref_label lc ON lc.domain = 'constraint_type' AND lc.code = o.constraint_type
LEFT JOIN ref_label lo ON lo.domain = 'option_type' AND lo.code = o.option_type
"""

# Work bundles: planned and open-defect work that needs a network outage, grouped by the isolation point (the switch or protective device
# that cuts the work off, from the connectivity model, or the nearest switch on the feeder when the asset is not in that model). Each
# bundle carries the constraints the planner must respect: school zone, road occupancy, crew hours, outage duration and switching crew.
SQL["gld_plan_bundle"] = """
WITH mem AS (
  SELECT equipment_id, max(feeder_id) AS mem_feeder, max(sectionalising_device) AS sect, max(protective_device) AS prot
  FROM slv_feeder_membership GROUP BY equipment_id
),
asset AS (SELECT asset_id, asset_class, feeder_id, lat, lon, customers_downstream, life_support_downstream, critical_sites_downstream FROM gld_asset),
planned AS (
  SELECT j.job_id, j.job_type, j.job_date, j.asset_id, coalesce(j.feeder_id, m.mem_feeder) AS feeder_id, j.region_id, j.duration_min,
         j.priority, j.required_skill, j.required_ticket, j.lat, j.lon, 'planned' AS source, j.notification_id
  FROM gld_field_job j LEFT JOIN mem m ON m.equipment_id = j.asset_id
  WHERE j.job_type IN ('LV_MAINT', 'HV_MAINT', 'CABLE_REPAIR', 'SUBSTATION_MAINT', 'PROTECTION_TEST', 'CIVIL', 'CUSTOMER_CONNECTION')
),
defects AS (
  SELECT concat('DEF-', d.notification_id) AS job_id,
         CASE WHEN a.asset_class = 'cable_section' THEN 'CABLE_REPAIR'
              WHEN a.asset_class IN ('cb', 'recloser', 'gas_switch', 'swer_isolator', 'nop') THEN 'HV_MAINT'
              WHEN a.asset_class = 'distribution_substation' THEN 'SUBSTATION_MAINT'
              ELSE 'LV_MAINT' END AS job_type,
         CAST(NULL AS DATE) AS job_date, d.asset_id, coalesce(d.feeder_id, a.feeder_id, m.mem_feeder) AS feeder_id, CAST(NULL AS STRING) AS region_id,
         CASE d.priority WHEN 1 THEN 180 WHEN 2 THEN 120 WHEN 3 THEN 90 ELSE 60 END AS duration_min,
         d.priority AS priority,
         CASE WHEN a.asset_class = 'cable_section' THEN 'CABLE_JOINT' ELSE 'LV_HV' END AS required_skill,
         CASE WHEN a.asset_class = 'cable_section' THEN 'CONFINED_SPACE' ELSE 'WORK_HEIGHTS' END AS required_ticket,
         d.lat, d.lon, 'defect' AS source, d.notification_id
  FROM gld_open_defect d
  LEFT JOIN asset a ON a.asset_id = d.asset_id
  LEFT JOIN mem m ON m.equipment_id = d.asset_id
  WHERE d.notification_id NOT IN (SELECT notification_id FROM gld_field_job WHERE notification_id IS NOT NULL)
    AND d.priority <= 2
),
pool AS (
  SELECT job_id, job_type, job_date, asset_id, feeder_id, region_id, duration_min, priority, required_skill, required_ticket, lat, lon, source
  FROM planned
  UNION ALL
  SELECT job_id, job_type, coalesce(job_date, date_add((SELECT max(job_date) FROM gld_field_job), 1)), asset_id, feeder_id, region_id,
         duration_min, priority, required_skill, required_ticket, lat, lon, source
  FROM defects
),
iso0 AS (
  SELECT p.job_id, coalesce(m.sect, m.prot) AS device, CASE WHEN m.sect IS NOT NULL THEN 'switch' WHEN m.prot IS NOT NULL THEN 'protective' END AS basis
  FROM pool p LEFT JOIN mem m ON m.equipment_id = p.asset_id
),
sw AS (SELECT asset_id AS switch_id, feeder_id AS sw_feeder, lat AS sw_lat, lon AS sw_lon FROM asset
       WHERE asset_class IN ('recloser', 'gas_switch', 'fuse', 'cb', 'swer_isolator', 'nop') AND lat IS NOT NULL),
near AS (
  SELECT p.job_id, s.switch_id,
         row_number() OVER (PARTITION BY p.job_id ORDER BY power(p.lat - s.sw_lat, 2) + power((p.lon - s.sw_lon) * cos(radians(p.lat)), 2)) AS rn
  FROM pool p JOIN sw s ON s.sw_feeder = p.feeder_id
  WHERE p.lat IS NOT NULL
),
iso AS (
  SELECT p.job_id, coalesce(i.device, n.switch_id, concat('FEEDER-', p.feeder_id)) AS iso_point,
         CASE WHEN i.basis IS NOT NULL THEN i.basis WHEN n.switch_id IS NOT NULL THEN 'nearest' ELSE 'feeder' END AS iso_basis
  FROM pool p
  LEFT JOIN iso0 i ON i.job_id = p.job_id
  LEFT JOIN (SELECT job_id, switch_id FROM near WHERE rn = 1) n ON n.job_id = p.job_id
),
jobs AS (
  SELECT p.*, i.iso_point, i.iso_basis, coalesce(p.feeder_id, 'UNKNOWN') AS feeder
  FROM pool p JOIN iso i ON i.job_id = p.job_id
  WHERE p.feeder_id IS NOT NULL
),
feeder_info AS (
  SELECT feeder_id, feeder_name, zs_id, customers, life_support, critical_sites, bushfire_share FROM gld_feeder_state
),
arts AS (
  SELECT feeder_id, sum(CASE WHEN street_type IN ('Road', 'Parade') THEN 1 ELSE 0 END) / count(*) AS arterial_share
  FROM gld_nmi WHERE feeder_id IS NOT NULL GROUP BY feeder_id
),
schools AS (SELECT feeder_id, count(*) AS school_count FROM gld_nmi WHERE critical_site = 'school' GROUP BY feeder_id),
ref AS (SELECT code, label FROM ref_label WHERE domain = 'job_type'),
""" + PRM + ",\n" + UC + """,
agg AS (
  SELECT j.feeder, j.iso_point, max(j.iso_basis) AS iso_basis,
         count(*) AS job_count, sum(CASE WHEN j.source = 'defect' THEN 1 ELSE 0 END) AS defect_jobs,
         array_join(array_sort(collect_set(coalesce(r.label, j.job_type))), ', ') AS job_type_labels,
         max_by(j.job_type, j.duration_min) AS primary_job_type,
         sum(j.duration_min) / 60.0 AS crew_hours,
         max(j.duration_min) AS longest_job_min,
         60 + max(j.duration_min) AS outage_minutes,
         sum(CASE WHEN j.required_skill IN ('PROTECTION', 'SUBSTATION') OR j.job_type IN ('PROTECTION_TEST', 'SUBSTATION_MAINT') THEN 1 ELSE 0 END) AS switching_jobs,
         bool_or(j.job_type IN ('CABLE_REPAIR', 'CIVIL', 'HV_MAINT')) AS road_works,
         max(j.priority) AS priority,
         min(j.job_date) AS earliest_date,
         avg(j.lat) AS lat, avg(j.lon) AS lon,
         concat_ws(',', slice(array_sort(collect_list(j.job_id)), 1, 40)) AS job_ids,
         -- the inputs to the cost: items by work type (materials), hours of overhead work (an elevated work platform) and of
         -- cable and civil work (an excavation truck), and hours of road works (traffic control)
         sum(CASE WHEN j.job_type = 'LV_MAINT' THEN 1 ELSE 0 END) AS n_lv, sum(CASE WHEN j.job_type = 'HV_MAINT' THEN 1 ELSE 0 END) AS n_hv,
         sum(CASE WHEN j.job_type = 'CABLE_REPAIR' THEN 1 ELSE 0 END) AS n_cable, sum(CASE WHEN j.job_type = 'SUBSTATION_MAINT' THEN 1 ELSE 0 END) AS n_sub,
         sum(CASE WHEN j.job_type = 'PROTECTION_TEST' THEN 1 ELSE 0 END) AS n_prot, sum(CASE WHEN j.job_type = 'CIVIL' THEN 1 ELSE 0 END) AS n_civil,
         sum(CASE WHEN j.job_type = 'CUSTOMER_CONNECTION' THEN 1 ELSE 0 END) AS n_conn,
         sum(CASE WHEN j.job_type IN ('LV_MAINT', 'HV_MAINT') THEN j.duration_min ELSE 0 END) / 60.0 AS overhead_hours,
         sum(CASE WHEN j.job_type IN ('CABLE_REPAIR', 'CIVIL') THEN j.duration_min ELSE 0 END) / 60.0 AS dig_hours,
         sum(CASE WHEN j.job_type IN ('CABLE_REPAIR', 'CIVIL', 'HV_MAINT') THEN j.duration_min ELSE 0 END) / 60.0 AS road_hours
  FROM jobs j LEFT JOIN ref r ON r.code = j.job_type
  GROUP BY j.feeder, j.iso_point
),
iso_asset AS (SELECT a.asset_id, a.customers_downstream, a.life_support_downstream, a.critical_sites_downstream, coalesce(l.label, a.asset_class) AS class_label
              FROM asset a LEFT JOIN ref_label l ON l.domain = 'asset_class' AND l.code = a.asset_class),
priced AS (
  SELECT a.*, fi.feeder_name, fi.zs_id, fi.customers AS feeder_customers, fi.life_support AS feeder_life_support,
         ia.customers_downstream, ia.life_support_downstream, ia.critical_sites_downstream, ia.class_label,
         (coalesce(ar.arterial_share, 0) >= prm.arterial_share_threshold AND a.road_works) AS traffic_needed,
         coalesce(ar.arterial_share, 0) AS arterial_share, coalesce(sc.school_count, 0) AS school_count,
         round(a.crew_hours * prm.crew_size * uc.field_worker_hour, 0) AS cost_labour,
         round(a.n_lv * uc.mat_lv_maint + a.n_hv * uc.mat_hv_maint + a.n_cable * uc.mat_cable_repair + a.n_sub * uc.mat_substation_maint
               + a.n_prot * uc.mat_protection_test + a.n_civil * uc.mat_civil + a.n_conn * uc.mat_customer_connection, 0) AS cost_materials,
         round(ceil(a.overhead_hours / prm.crew_day_hours) * uc.ewp_day + ceil(a.dig_hours / prm.crew_day_hours) * uc.excavation_day, 0) AS cost_plant,
         CASE WHEN coalesce(ar.arterial_share, 0) >= prm.arterial_share_threshold AND a.road_works
              THEN round(greatest(1, ceil(a.road_hours / prm.crew_day_hours)) * uc.traffic_control_day, 0) ELSE 0 END AS cost_traffic,
         uc.switching_outage AS cost_switching,
         round(coalesce(ia.customers_downstream, fi.customers) * uc.notice_per_customer
               + coalesce(ia.life_support_downstream, fi.life_support) * uc.life_support_call, 0) AS cost_customer,
         prm.works_contingency
  FROM agg a
  CROSS JOIN prm CROSS JOIN uc
  LEFT JOIN feeder_info fi ON fi.feeder_id = a.feeder
  LEFT JOIN iso_asset ia ON ia.asset_id = a.iso_point
  LEFT JOIN arts ar ON ar.feeder_id = a.feeder
  LEFT JOIN schools sc ON sc.feeder_id = a.feeder
)
SELECT concat('BND-', a.iso_point) AS bundle_id,
       a.feeder AS feeder_id, a.feeder_name, a.zs_id, z.zs_name, rf.region_id,
       a.iso_point AS isolation_point, a.iso_basis,
       CASE WHEN a.iso_point LIKE 'FEEDER-%' THEN 'Feeder circuit breaker (the whole feeder)' ELSE concat(a.class_label, ' ', a.iso_point) END AS isolation_label,
       a.job_count, a.defect_jobs, a.job_type_labels, a.primary_job_type, round(a.crew_hours, 1) AS crew_hours,
       a.longest_job_min, a.outage_minutes, a.switching_jobs,
       CAST(coalesce(a.customers_downstream, a.feeder_customers) AS INT) AS customers_interrupted,
       CAST(coalesce(a.life_support_downstream, a.feeder_life_support) AS INT) AS life_support_interrupted,
       CAST(coalesce(a.critical_sites_downstream, 0) AS INT) AS critical_sites_interrupted,
       round(a.outage_minutes * coalesce(a.customers_downstream, a.feeder_customers) / 60.0, 0) AS customer_hours,
       a.school_count > 0 AS school_on_feeder,
       round(100 * a.arterial_share, 0) AS arterial_share_pct,
       a.traffic_needed AS traffic_management_needed,
       a.priority, a.earliest_date,
       round(a.lat, 6) AS lat, round(a.lon, 6) AS lon,
       least(100, CAST(10 * a.priority + least(10, 3 * a.defect_jobs) + least(20, a.crew_hours / 2) + least(20, coalesce(a.customers_downstream, a.feeder_customers) / 200.0) AS INT)) AS value_score,
       concat_ws(',', (SELECT array_sort(collect_set(c.constraint_id)) FROM gld_plan_constraint c WHERE c.feeder_id = a.feeder)) AS constraint_refs,
       a.job_ids,
       CAST(a.cost_labour AS DOUBLE) AS cost_labour_aud, CAST(a.cost_materials AS DOUBLE) AS cost_materials_aud,
       CAST(a.cost_plant AS DOUBLE) AS cost_plant_aud, CAST(a.cost_traffic AS DOUBLE) AS cost_traffic_aud,
       CAST(a.cost_switching AS DOUBLE) AS cost_switching_aud, CAST(a.cost_customer AS DOUBLE) AS cost_customer_aud,
       CAST(round(a.works_contingency * (a.cost_labour + a.cost_materials + a.cost_plant + a.cost_traffic + a.cost_switching), 0) AS DOUBLE) AS cost_contingency_aud,
       CAST(round((1 + a.works_contingency) * (a.cost_labour + a.cost_materials + a.cost_plant + a.cost_traffic + a.cost_switching) + a.cost_customer, 0) AS DOUBLE) AS cost_total_aud
FROM priced a
LEFT JOIN (SELECT feeder_id, region_id FROM gld_rm_feeder) rf ON rf.feeder_id = a.feeder
LEFT JOIN (SELECT zs_id, zs_name, area FROM gld_zone_supply) z ON z.zs_id = a.zs_id
"""

# Region crew capacity: the hours the crews can work in the planning window, what the business as usual work already takes, and the
# hours ring-fenced for programme work.
SQL["gld_plan_region_capacity"] = """
WITH prm AS (SELECT max(CASE WHEN key = 'crew_day_hours' THEN value END) AS crew_day_hours,
                    max(CASE WHEN key = 'availability_share' THEN value END) AS availability_share,
                    max(CASE WHEN key = 'window_days' THEN value END) AS window_days,
                    max(CASE WHEN key = 'programme_share_of_spare' THEN value END) AS programme_share_of_spare,
                    max(CASE WHEN key = 'switching_crews_per_region' THEN value END) AS switching_crews_per_region
             FROM gld_plan_assumption),
crews AS (SELECT region_id, count(*) AS crews, sum(CASE WHEN employment_type = 'Contractor' THEN 1 ELSE 0 END) AS contractor_crews
          FROM gld_crew WHERE active GROUP BY region_id),
bau AS (SELECT region_id, sum(duration_min) / 60.0 AS bau_hours FROM gld_field_job GROUP BY region_id)
SELECT r.region_id, r.name AS region_name, coalesce(c.crews, 0) AS crews, coalesce(c.contractor_crews, 0) AS contractor_crews,
       round(coalesce(c.crews, 0) * prm.window_days * prm.crew_day_hours * prm.availability_share, 1) AS available_hours,
       round(coalesce(b.bau_hours, 0), 1) AS bau_hours,
       round(greatest(0, coalesce(c.crews, 0) * prm.window_days * prm.crew_day_hours * prm.availability_share - coalesce(b.bau_hours, 0)), 1) AS spare_hours,
       round(prm.programme_share_of_spare * greatest(0, coalesce(c.crews, 0) * prm.window_days * prm.crew_day_hours * prm.availability_share - coalesce(b.bau_hours, 0)), 1) AS programme_hours,
       CAST(prm.switching_crews_per_region AS INT) AS switching_crews,
       CAST(prm.window_days AS INT) AS window_days
FROM gld_rm_region r
CROSS JOIN prm
LEFT JOIN crews c ON c.region_id = r.region_id
LEFT JOIN bau b ON b.region_id = r.region_id
"""


# Transmission substations (132 kV or 66 kV to 33 kV, two to four transformers) sit between the bulk supply points and the zone substations.
# They exist in the lakehouse only once the transmission mart is synced, so these two marts are defined only when the pipeline conf
# atlas.transmission_planning is true (see the guard at the end of this file). The zone demand of the zones each site supplies is summed
# with the same coincidence share, against the site's firm (N-1) capacity. ASSUMPTION to confirm with the mart owner: gld_transmission_substation
# has ts_id, name, kv, firm_mva (N-1 firm), status and zones (an array of zone substation ids).
SQL["gld_plan_constraint_transmission"] = """
WITH ts AS (SELECT ts_id, name, kv, firm_mva, status, explode(zones) AS zs_id FROM gld_transmission_substation),
tp AS (
  SELECT ts.ts_id, ts.name, ts.kv, ts.firm_mva, ts.status, zd.fiscal_year, zd.years_out,
         sum(zd.peak_mw) AS zones_peak_mw, sum(zd.customers) AS customers, sum(zd.life_support) AS life_support
  FROM ts JOIN gld_plan_zone_demand zd ON zd.zs_id = ts.zs_id
  GROUP BY ts.ts_id, ts.name, ts.kv, ts.firm_mva, ts.status, zd.fiscal_year, zd.years_out
),
""" + PRM + """,
tpk AS (SELECT tp.*, tp.zones_peak_mw * prm.coincident_peak_share AS peak_mw, prm.power_factor, prm.zone_util_threshold FROM tp CROSS JOIN prm),
tb AS (SELECT ts_id, min(CASE WHEN peak_mw > firm_mva * power_factor THEN fiscal_year END) AS breach_fy FROM tpk GROUP BY ts_id),
y0 AS (SELECT * FROM tpk WHERE years_out = 0)
SELECT concat('C-', y0.ts_id) AS constraint_id, 'transmission_capacity' AS constraint_type, 'transmission' AS constraint_level,
       CAST(NULL AS STRING) AS zs_id, y0.name AS zs_name, CAST(NULL AS STRING) AS area, CAST(NULL AS STRING) AS feeder_id,
       CAST(NULL AS STRING) AS feeder_name, y0.ts_id AS asset_ref,
       'Share of N-1 firm capacity used in the first year (%)' AS metric_label,
       round(100 * y0.peak_mw / (y0.firm_mva * y0.power_factor), 1) AS metric_value, '%' AS metric_unit, 100.0 AS threshold,
       CASE WHEN tb.breach_fy IS NULL THEN NULL ELSE CAST(tb.breach_fy - 2027 AS INT) END AS years_to_limit,
       least(100, CAST(round(100 * y0.peak_mw / (y0.firm_mva * y0.power_factor)) AS INT)) AS severity,
       CAST(y0.customers AS INT) AS customers, CAST(y0.life_support AS INT) AS life_support, CAST(0 AS INT) AS critical_sites,
       CAST(NULL AS DOUBLE) AS bushfire_share,
       concat(y0.name, ' (', CAST(y0.kv AS STRING), ' kV) uses ', CAST(round(100 * y0.peak_mw / (y0.firm_mva * y0.power_factor)) AS STRING),
              '% of its N-1 firm capacity for the zones it supplies', CASE WHEN tb.breach_fy IS NULL THEN '.'
              ELSE concat(' and is projected to exceed it in FY', lpad(CAST(mod(tb.breach_fy, 100) AS STRING), 2, '0'), '.') END) AS summary
FROM y0
JOIN tb ON tb.ts_id = y0.ts_id
WHERE 100 * y0.peak_mw / (y0.firm_mva * y0.power_factor) >= 100 * y0.zone_util_threshold OR tb.breach_fy IS NOT NULL
"""

# Transmission options: a third transformer (N-1 headroom), or defer and monitor.
SQL["gld_plan_intervention_transmission"] = """
WITH cons AS (SELECT * FROM gld_plan_constraint_transmission),
""" + PRM + ",\n" + CF + ",\n" + UC + """,
opts AS (
  SELECT concat(c.constraint_id, '-AUG') AS intervention_id, c.constraint_id, c.constraint_type, 'augment' AS option_type,
         'Add a third transformer at the transmission substation' AS option_label,
         'A new transformer on the transmission substation restores N-1 headroom for the zones it supplies.' AS description,
         c.asset_ref AS asset_ref, 1.0 AS quantity, 'transformer' AS quantity_unit,
         uc.transmission_transformer AS capex_aud, 0.0 AS opex_10yr_aud,
         round(prm.ts_new_transformer_mva * prm.power_factor, 1) AS relief_mw,
         c.customers AS customers_protected, 0.0 AS asset_risk_reduction_pct, 30.0 AS reliability_gain_pct,
         round(prm.ts_new_transformer_mva * cf.transformer_embodied, 1) AS carbon_tco2e_10yr,
         24.0 AS lead_time_months, 45.0 AS life_years, '' AS climate_note
  FROM cons c CROSS JOIN prm CROSS JOIN cf CROSS JOIN uc
  UNION ALL
  SELECT concat(c.constraint_id, '-DEF'), c.constraint_id, c.constraint_type, 'defer', 'Defer and monitor the transmission substation',
         'Keep the site under watch each year and accept the projected shortfall, with zone load transfers as the fallback.',
         c.asset_ref, 0.0, 'no works', 0.0, prm.monitoring_cost_pa * prm.opex_years, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, ''
  FROM cons c CROSS JOIN prm
)
SELECT o.intervention_id, o.constraint_id, o.constraint_type, lc.label AS constraint_type_label, o.option_type,
       lo.label AS option_type_label, o.option_label, o.description, CAST(NULL AS STRING) AS zs_id, CAST(NULL AS STRING) AS feeder_id,
       o.asset_ref, CAST(o.quantity AS DOUBLE) AS quantity, o.quantity_unit,
       CAST(round(o.capex_aud, 0) AS DOUBLE) AS capex_aud, CAST(round(o.opex_10yr_aud, 0) AS DOUBLE) AS opex_10yr_aud,
       CAST(round(o.capex_aud * (1 + prm.contingency) + o.opex_10yr_aud, 0) AS DOUBLE) AS cost_10yr_aud,
       CAST(o.relief_mw AS DOUBLE) AS relief_mw, CAST(o.customers_protected AS INT) AS customers_protected,
       CAST(o.asset_risk_reduction_pct AS DOUBLE) AS asset_risk_reduction_pct, CAST(o.reliability_gain_pct AS DOUBLE) AS reliability_gain_pct,
       CAST(o.carbon_tco2e_10yr AS DOUBLE) AS carbon_tco2e_10yr, CAST(o.lead_time_months AS DOUBLE) AS lead_time_months,
       CAST(prm.plan_first_fy + ceil(o.lead_time_months / 12.0) AS INT) AS earliest_fy,
       CAST(o.life_years AS DOUBLE) AS life_years, '' AS climate_note,
       CASE WHEN o.option_type = 'defer' THEN 'Leaves the customers on this asset exposed to the projected shortfall.' ELSE concat('Protects ', CAST(CAST(coalesce(o.customers_protected, 0) AS BIGINT) AS STRING), ' customers and ', CAST(CAST(coalesce(c.life_support, 0) AS BIGINT) AS STRING), ' life support customers.') END AS community_note
FROM opts o
JOIN cons c ON c.constraint_id = o.constraint_id
CROSS JOIN prm
LEFT JOIN ref_label lc ON lc.domain = 'constraint_type' AND lc.code = o.constraint_type
LEFT JOIN ref_label lo ON lo.domain = 'option_type' AND lo.code = o.option_type
"""

# ------------------------------------------------------------------ column comments (every table and column)
COMMENTS = {
    "gld_plan_zone_demand": {
        "zs_id": "Zone substation id.", "zs_name": "Zone substation name.", "area": "Area code (MET, RNG, CST).",
        "single_fed": "True when one subtransmission feed serves the zone (no firm supply on loss of the feed).",
        "fiscal_year": "Financial year (FY27 is July 2026 to June 2027), as a four digit year of the year end.",
        "years_out": "Years after the first plan year (0 is FY27).",
        "fortnight_peak_mw": "Highest forecast peak over the next 14 days from the zone load model (MW, shoulder season).",
        "hist_peak_mw": "Coincident peak of the last 12 months (sum of transformer peaks times the coincidence share), MW.",
        "growth_pa": "Annual peak growth applied, from the 12 month trend within the floor and cap (share a year).",
        "ev_mw_now": "Evening demand of today's home EV chargers after coincidence, MW.",
        "solar_mw": "Rooftop solar capacity on the zone's distribution substations, MW (not netted off the peak).",
        "tx_mva": "Installed transformer rating at the zone, MVA.",
        "firm_mva": "N-1 firm capacity: installed rating less the largest transformer, limited by the subtransmission circuits; zero when single fed, MVA.",
        "peak_mw": "Projected peak in this year (base grown at the growth rate plus extra EV charging), MW.",
        "utilisation_pct": "Projected peak over firm capacity at the power factor, percent (blank when there is no firm capacity).",
        "headroom_mw": "Firm capacity at the power factor less the projected peak, MW (negative is a shortfall).",
        "over_firm": "True when the projected peak exceeds firm capacity this year (or there is no firm capacity).",
        "customers": "Customers on the zone's feeders.", "life_support": "Life support customers on the zone's feeders.",
    },
    "gld_plan_constraint": {
        "constraint_id": "Constraint id: a type code and the asset it applies to.",
        "constraint_type": "Constraint type code (zone capacity, zone security, feeder capacity, feeder voltage, feeder asset risk, transformer asset risk, feeder reliability).",
        "constraint_type_label": "Constraint type in words.", "constraint_level": "Level the constraint sits at: zone, feeder or transformer.",
        "zs_id": "Zone substation the constraint sits in.", "zs_name": "Zone substation name.", "area": "Area code.",
        "feeder_id": "Feeder id (blank at zone and transformer level).", "feeder_name": "Feeder name.",
        "asset_ref": "The asset the constraint applies to: zone, feeder or transformer id.",
        "metric_label": "What the metric measures, in words.", "metric_value": "The measured value.", "metric_unit": "Unit of the metric.",
        "threshold": "The trigger or limit the metric is compared with, in the same unit.",
        "years_to_limit": "Years until the limit is projected to be reached (0 when already reached; blank when not projected).",
        "severity": "Rule-based 0 to 100 index for ranking the constraints (not a probability).",
        "customers": "Customers supplied by the asset.", "life_support": "Life support customers supplied by the asset.",
        "critical_sites": "Critical sites supplied by the asset.", "bushfire_share": "Share of the feeder's length in bushfire prone areas (0 to 1).",
        "summary": "One sentence on the constraint, in plain words.",
    },
    "gld_plan_intervention": {
        "intervention_id": "Intervention id: the constraint id and an option code.", "constraint_id": "Constraint the option addresses.",
        "constraint_type": "Constraint type code.", "constraint_type_label": "Constraint type in words.",
        "option_type": "Option type code (maintain, replace, augment, reconfigure, flexibility, defer).",
        "option_type_label": "Option type in words.", "option_label": "Option in words.", "description": "What the option does, in plain words.",
        "zs_id": "Zone substation.", "feeder_id": "Feeder (blank at zone level).", "asset_ref": "Asset the option works on.",
        "quantity": "Quantity of the works (see quantity unit).", "quantity_unit": "Unit of the quantity.",
        "capex_aud": "Capital cost, AUD, before contingency.", "opex_10yr_aud": "Operating cost over the cost horizon, AUD.",
        "cost_10yr_aud": "Ten year cost, AUD: capital with contingency plus operating cost.",
        "relief_mw": "Peak demand relief the option gives, MW (zero when it does not relieve capacity).",
        "customers_protected": "Customers whose supply the option protects.",
        "asset_risk_reduction_pct": "Reduction in asset fault risk, percent.", "reliability_gain_pct": "Reduction in customer interruptions, percent.",
        "carbon_tco2e_10yr": "Emissions over the cost horizon, t CO2e: embodied in the works plus operating losses; before avoided emissions.",
        "lead_time_months": "Months from approval to completion.", "earliest_fy": "Earliest financial year the option can be complete.",
        "life_years": "Asset life in years.", "climate_note": "Climate and fire season note, when it applies.",
        "community_note": "Who the option protects, in plain words.",
    },
    "gld_plan_bundle": {
        "bundle_id": "Bundle id: the feeder and the isolation point.", "feeder_id": "Feeder.", "feeder_name": "Feeder name.",
        "zs_id": "Zone substation.", "zs_name": "Zone substation name.", "region_id": "Field region (depot).",
        "isolation_point": "Switch or protective device that isolates the work (the work's outage boundary).",
        "iso_basis": "How the isolation point was found: switch or protective device in the connectivity model, nearest switch, or feeder breaker.",
        "job_count": "Work items in the bundle (planned jobs and open defects).", "defect_jobs": "Open defect repairs in the bundle.",
        "job_type_labels": "Work types in the bundle, in words.", "primary_job_type": "Work type of the longest item (drives the job raised on release).",
        "crew_hours": "Crew hours the bundle needs.", "longest_job_min": "Longest single item in minutes.",
        "outage_minutes": "Planned outage needed to isolate the work: switching allowance plus the longest item, minutes.",
        "switching_jobs": "Items that need a switching or protection crew.",
        "customers_interrupted": "Customers off supply during the outage (customers behind the isolation point).",
        "life_support_interrupted": "Life support customers off supply during the outage.",
        "critical_sites_interrupted": "Critical sites off supply during the outage.",
        "customer_hours": "Customer hours off supply for the outage (customers times outage hours).",
        "school_on_feeder": "True when a school is connected to the feeder (school hours restrict heavy plant).",
        "arterial_share_pct": "Share of the feeder's customer addresses on arterial roads (Road, Parade), percent.",
        "traffic_management_needed": "True when road works are likely on arterial roads, so traffic control is needed.",
        "priority": "Highest priority of the items (1 low to 5 urgent).", "earliest_date": "Earliest date the work can start (planned date, or the day after the planned week for defects).",
        "lat": "Latitude of the bundle centre.", "lon": "Longitude of the bundle centre.",
        "value_score": "Rule-based 0 to 100 work value index for ranking (priority, defects, crew hours, customers).",
        "constraint_refs": "Constraint ids on the same feeder.", "job_ids": "Up to 40 work item ids in the bundle, comma separated.",
        "isolation_label": "The isolation point in words: the kind of device and its id (or the feeder breaker when no switch was found).",
        "cost_labour_aud": "Estimated labour, AUD: crew hours times the crew size times the field worker rate (gld_plan_unit_cost field_worker_hour).",
        "cost_materials_aud": "Estimated materials, AUD: work items by type times the materials rate of each type.",
        "cost_plant_aud": "Estimated plant and equipment, AUD: elevated work platform days for overhead work and excavation truck days for cable and civil work.",
        "cost_traffic_aud": "Estimated traffic control, AUD: road works days times the traffic control day rate, when traffic management is needed.",
        "cost_switching_aud": "Switching and outage management for the bundle's one planned outage, AUD.",
        "cost_customer_aud": "Customer notices for the customers off supply and contact centre calls to the life support customers, AUD.",
        "cost_contingency_aud": "Contingency on the direct cost (labour, materials, plant, traffic control, switching), AUD.",
        "cost_total_aud": "Estimated total cost of the bundle, AUD (2026 dollars, excluding GST): the direct cost with contingency plus the customer notices.",
    },
    "gld_plan_region_capacity": {
        "region_id": "Field region (depot) id.", "region_name": "Region name.", "crews": "Active crews.", "contractor_crews": "Contractor crews.",
        "available_hours": "Crew hours available in the planning window after availability.", "bau_hours": "Crew hours the business as usual work already takes.",
        "spare_hours": "Available hours less business as usual hours (not less than zero).", "programme_hours": "Hours ring-fenced for programme work.",
        "switching_crews": "Switching crews available each day.", "window_days": "Days in the planning window.",
    },
    "gld_plan_assumption": {
        "key": "Assumption key used in the planning marts.", "value": "Value.", "unit": "Unit.", "note": "What the assumption is and why it is set here.",
    },
    "gld_plan_carbon_factor": {
        "factor": "Factor key.", "label": "Factor in words.", "value": "Value.", "unit": "Unit.", "basis": "Basis of the factor.",
    },
    "gld_plan_unit_cost": {
        "item": "Cost item key.", "label": "Item in words.", "unit": "Unit the cost applies to.", "cost_aud": "Cost per unit, AUD (2026 dollars).",
        "life_years": "Asset life in years.", "basis": "Basis of the estimate.",
    },
}

COMMENTS["gld_plan_constraint_transmission"] = COMMENTS["gld_plan_constraint"]
COMMENTS["gld_plan_intervention_transmission"] = COMMENTS["gld_plan_intervention"]
ANNOTATION_TABLES = {"gld_plan_assumption", "gld_plan_carbon_factor", "gld_plan_unit_cost"}


def _sql(name):
    return spark.sql(SQL[name])  # noqa: F821 - spark is the pipeline session


def _commented(df, name):
    """Table and column comments from COMMENTS: the Lakebase copy and Genie show them."""
    comments = COMMENTS[name]
    return df.select([F.col(c).alias(c, metadata={"comment": comments[c]}) if c in comments else F.col(c) for c in df.columns])


@dp.materialized_view(comment="Planning assumptions: the rates, thresholds and horizon the plan uses, each with its unit and note.")
def gld_plan_assumption():
    return _commented(spark.createDataFrame([(k, float(v), u, n) for k, v, u, n in ASSUMPTIONS], "key string, value double, unit string, note string"), "gld_plan_assumption")


@dp.materialized_view(comment="Carbon factors for the planning options: grid intensity, embodied carbon of works, battery losses (planning assumptions).")
def gld_plan_carbon_factor():
    return _commented(spark.createDataFrame([(f, l, float(v), u, b) for f, l, v, u, b in CARBON], "factor string, label string, value double, unit string, basis string"), "gld_plan_carbon_factor")


@dp.materialized_view(comment="Unit cost estimates for planning options (AUD, 2026 dollars, installed where stated).")
def gld_plan_unit_cost():
    return _commented(spark.createDataFrame([(i, l, u, float(c), int(y), b) for i, l, u, c, y, b in UNIT_COST], "item string, label string, unit string, cost_aud double, life_years int, basis string"),
                      "gld_plan_unit_cost")


@dp.materialized_view(comment="Zone demand projection: for each zone substation and financial year, the projected peak against N-1 firm capacity, "
                              "with headroom, EV and rooftop solar inputs. Ten years from the first plan year.")
def gld_plan_zone_demand():
    return _commented(_sql("gld_plan_zone_demand"), "gld_plan_zone_demand")


@dp.materialized_view(comment="Constraint register: zones, feeders and transformers that breach or approach a capacity, voltage, asset risk or "
                              "reliability limit, with the metric, the years to the limit and a rule-based severity index.")
def gld_plan_constraint():
    return _commented(_sql("gld_plan_constraint"), "gld_plan_constraint")


@dp.materialized_view(comment="Candidate interventions per constraint (maintain, replace, augment, reconfigure, flexibility, defer) with capital "
                              "and ten year cost, relief, customers protected, risk and reliability effects, carbon and lead time.")
def gld_plan_intervention():
    return _commented(_sql("gld_plan_intervention"), "gld_plan_intervention")


@dp.materialized_view(comment="Network-aware work bundles: planned and open-defect work that needs a network outage, grouped by feeder and "
                              "isolation point, with crew hours, outage duration, customers off, school, road occupancy and switching needs.")
def gld_plan_bundle():
    return _commented(_sql("gld_plan_bundle"), "gld_plan_bundle")


@dp.materialized_view(comment="Crew capacity by field region in the planning window: available, business as usual and programme hours, switching crews.")
def gld_plan_region_capacity():
    return _commented(_sql("gld_plan_region_capacity"), "gld_plan_region_capacity")


# Transmission marts exist only once gld_transmission_substation is synced into this pipeline. The owner sets the conf to true then.
if spark.conf.get("atlas.transmission_planning", "false") == "true":  # noqa: F821

    @dp.materialized_view(comment="Transmission substation constraints: sites whose zones' coincident peak breaches or approaches N-1 firm capacity.")
    def gld_plan_constraint_transmission():
        return _commented(_sql("gld_plan_constraint_transmission"), "gld_plan_constraint_transmission")

    @dp.materialized_view(comment="Candidate interventions for transmission substation constraints (a third transformer, or defer).")
    def gld_plan_intervention_transmission():
        return _commented(_sql("gld_plan_intervention_transmission"), "gld_plan_intervention_transmission")
