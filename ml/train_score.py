"""Train, register (MLflow, Unity Catalog) and score the twin's models.

Models:
- storm_impact: feeder-day sustained faults (Poisson), customers interrupted and crew-hours (quantile P10/P50/P90), trained on two
  years of history conditioned on real weather; scored on the live 72-hour forecast.
- span_fault_risk: probability that an overhead span faults in a year (age, construction, vegetation, bushfire land, exposure).
Scores land in ml_* tables with model versions and per-prediction drivers. The load forecast uses ai_forecast (AI Functions).
Usage: train_score.py --src <dir> --catalog <c> --schema <s> [--mode train|score|all]
"""
import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

STORM_FEATURES = ["gust_kmh", "rain_mm", "tmax_c", "overhead_km", "bushfire_share", "veg_cat_a", "veg_cat_b", "open_defects",
                  "customers", "poles"]
SPAN_FEATURES = ["age_years", "length_m", "clearance_m", "growth_m_per_year", "hazard_tree", "bushfire_prone", "customers_downstream",
                 "rural"]
FEATURE_LABELS = {"gust_kmh": "forecast gusts", "rain_mm": "forecast rain", "tmax_c": "heat", "overhead_km": "overhead length",
                  "bushfire_share": "bushfire-prone spans", "veg_cat_a": "spans inside vegetation clearance",
                  "veg_cat_b": "spans with vegetation inside 2 m", "open_defects": "open defects", "customers": "customers on the feeder",
                  "poles": "pole count"}


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.builder.getOrCreate()


def feeder_features(spark, fq):
    df = spark.sql(f"SELECT feeder_id, overhead_km, bushfire_share, veg_cat_a, veg_cat_b, open_defects, customers, poles "
                   f"FROM {fq('gld_feeder_state')}").toPandas().fillna(0)
    for c in df.columns[1:]:
        df[c] = df[c].astype("float64")
    return df


def weather_history(src):
    w = json.load(open(os.path.join(src, "config", "weather_history.json")))
    rows = []
    for z in w["zones"]:
        for i, t in enumerate(z["time"]):
            rows.append((z["zs"], t, z["wind_gusts_10m_max"][i] or 30.0, z["precipitation_sum"][i] or 0.0, z["temperature_2m_max"][i] or 22.0))
    return pd.DataFrame(rows, columns=["zone", "day", "gust_kmh", "rain_mm", "tmax_c"])


def storm_training(spark, fq, src):
    feats = feeder_features(spark, fq)
    feats["zone"] = feats["feeder_id"].str[:3]
    w = weather_history(src)
    base = feats.merge(w, on="zone")
    o = spark.sql(f"""SELECT feeder_id, CAST(outage_date AS STRING) day, count(*) faults, sum(customers_interrupted) cust_int,
                       sum(duration_min) / 60.0 * 1.6 crew_hours
                     FROM {fq('gld_outage')} WHERE NOT momentary GROUP BY ALL""").toPandas()
    df = base.merge(o, on=["feeder_id", "day"], how="left").fillna({"faults": 0, "cust_int": 0, "crew_hours": 0})
    return df


def train(spark, fq, src, catalog, schema):
    import mlflow
    from mlflow.models import infer_signature
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, roc_auc_score

    mlflow.set_registry_uri("databricks-uc")
    user = spark.sql("SELECT current_user()").first()[0]
    mlflow.set_experiment(f"/Users/{user}/atlas-models-{catalog}-{schema}")
    versions = {}

    # ---------------------------------------------------------------- storm impact
    df = storm_training(spark, fq, src)
    df = df.sort_values("day")
    df[STORM_FEATURES] = df[STORM_FEATURES].astype("float64")
    cut = df["day"].quantile(0.8) if df["day"].dtype != object else sorted(df["day"].unique())[int(df["day"].nunique() * 0.8)]
    tr, te = df[df["day"] < cut], df[df["day"] >= cut]
    X, Xt = tr[STORM_FEATURES], te[STORM_FEATURES]
    with mlflow.start_run(run_name="storm_impact"):
        faults = HistGradientBoostingRegressor(loss="poisson", max_iter=300, learning_rate=0.06, max_leaf_nodes=24).fit(X, tr["faults"])
        # Severity per fault (customers and crew-hours), trained on fault days only; combined with expected faults at scoring.
        quant = {}
        fd = tr[tr["faults"] > 0]
        for target, col in (("customers", "cust_int"), ("crew_hours", "crew_hours")):
            for q in (0.1, 0.5, 0.9):
                quant[f"{target}_p{int(q * 100)}"] = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=200,
                                                                                    learning_rate=0.06).fit(fd[STORM_FEATURES], fd[col] / fd["faults"])
        mae = mean_absolute_error(te["faults"], faults.predict(Xt))
        # Storm days in the test window: how well the model ranks feeders on the windiest days.
        storm = te[te["gust_kmh"] > 70]
        mlflow.log_metrics({"faults_mae": mae, "test_rows": len(te), "storm_days_rows": len(storm),
                            "faults_mae_storm_days": mean_absolute_error(storm["faults"], faults.predict(storm[STORM_FEATURES])) if len(storm) else 0})
        mlflow.log_params({"features": ",".join(STORM_FEATURES), "train_rows": len(tr), "target_faults": "Poisson GBT",
                           "targets_quantile": "customers, crew_hours at P10/P50/P90"})
        model = StormImpact(faults, quant)
        sig = infer_signature(Xt.head(50), model.predict(None, Xt.head(50)))
        info = mlflow.pyfunc.log_model(name="model", python_model=model, signature=sig, input_example=Xt.head(3),
                                       registered_model_name=f"{catalog}.{schema}.storm_impact_model",
                                       pip_requirements=["scikit-learn", "pandas", "numpy"])
        versions["storm_impact_model"] = info.registered_model_version
    # ---------------------------------------------------------------- span fault risk
    sp = spark.sql(f"""
      SELECT a.asset_id span_id, a.age_years, a.rating length_m, v.clearance_m, v.growth_m_per_year, CAST(v.hazard_tree AS INT) hazard_tree,
             CAST(v.bushfire_prone AS INT) bushfire_prone, a.customers_downstream, CAST(a.feeder_id RLIKE '^(LPH|KNV|GVL)' AS INT) rural,
             CAST(EXISTS (SELECT 1 FROM {fq('gld_outage')} o WHERE o.span_id = a.asset_id AND NOT o.momentary) AS INT) faulted
      FROM {fq('gld_asset')} a JOIN {fq('gld_vegetation_risk')} v ON v.span_id = a.asset_id WHERE a.asset_class = 'overhead_span'""").toPandas().fillna(0)
    rng = np.random.default_rng(7)
    msk = rng.random(len(sp)) < 0.8
    with mlflow.start_run(run_name="span_fault_risk"):
        clf = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, class_weight="balanced").fit(sp[msk][SPAN_FEATURES], sp[msk]["faulted"])
        auc = roc_auc_score(sp[~msk]["faulted"], clf.predict_proba(sp[~msk][SPAN_FEATURES])[:, 1])
        mlflow.log_metrics({"auc": auc, "positives": int(sp["faulted"].sum())})
        sig = infer_signature(sp[SPAN_FEATURES].head(50), clf.predict_proba(sp[SPAN_FEATURES].head(50))[:, 1])
        info = mlflow.sklearn.log_model(clf, name="model", signature=sig, registered_model_name=f"{catalog}.{schema}.span_fault_risk_model",
                                        pip_requirements=["scikit-learn"])
        versions["span_fault_risk_model"] = info.registered_model_version
    for name, v in versions.items():
        from mlflow import MlflowClient
        MlflowClient().set_registered_model_alias(f"{catalog}.{schema}.{name}", "champion", v)
    return versions


import mlflow.pyfunc  # noqa: E402  (base class for the storm model wrapper)


class StormImpact(mlflow.pyfunc.PythonModel):
    """Feeder-day storm impact: faults (P50 from Poisson, P10/P90 from the Poisson interval) and quantile customers and crew-hours."""

    def __init__(self, faults, quant):
        self.faults = faults
        self.quant = quant

    def predict(self, context, model_input, params=None):
        from scipy.stats import poisson
        X = model_input[STORM_FEATURES]
        lam = np.clip(self.faults.predict(X), 1e-4, None)
        out = pd.DataFrame({"faults_p50": lam, "faults_p10": poisson.ppf(0.1, lam), "faults_p90": poisson.ppf(0.9, lam)})
        sev = {k: np.clip(m.predict(X), 0, None) for k, m in self.quant.items()}
        for t in ("customers", "crew_hours"):
            lo, mid, hi = np.sort(np.vstack([sev[f"{t}_p10"], sev[f"{t}_p50"], sev[f"{t}_p90"]]), axis=0)
            out[f"{t}_p10"] = out["faults_p10"] * lo
            out[f"{t}_p50"] = lam * mid
            out[f"{t}_p90"] = np.maximum(out["faults_p90"], np.ceil(lam)) * hi
        return out


def score(spark, fq, catalog, schema):
    import mlflow
    mlflow.set_registry_uri("databricks-uc")
    storm = mlflow.pyfunc.load_model(f"models:/{catalog}.{schema}.storm_impact_model@champion")
    span = mlflow.sklearn.load_model(f"models:/{catalog}.{schema}.span_fault_risk_model@champion")
    from mlflow import MlflowClient
    cl = MlflowClient()
    ver = {n: cl.get_model_version_by_alias(f"{catalog}.{schema}.{n}", "champion").version
           for n in ("storm_impact_model", "span_fault_risk_model")}
    now = dt.datetime.utcnow()
    # ---------------------------------------------------------------- storm projection on the live forecast
    feats = feeder_features(spark, fq)
    wx = spark.sql(f"""SELECT feeder_id, CAST(to_date(window_start) AS STRING) day, max(max_gust_kmh) gust_kmh, sum(rain_mm_6h) rain_mm,
                         max(max_temp_c) tmax_c FROM {fq('gld_feeder_weather_exposure')} GROUP BY ALL""").toPandas()
    wx6 = spark.sql(f"SELECT feeder_id, window_start, max_gust_kmh, rain_mm_6h FROM {fq('gld_feeder_weather_exposure')}").toPandas()
    df = wx.merge(feats, on="feeder_id")
    df[STORM_FEATURES] = df[STORM_FEATURES].astype("float64")
    pred = storm.predict(df[STORM_FEATURES])
    res = pd.concat([df[["feeder_id", "day", "gust_kmh", "rain_mm", "tmax_c"]].reset_index(drop=True), pred.reset_index(drop=True)], axis=1)
    # Drivers: how much each feature moves the expected faults against a calm baseline.
    base = df[STORM_FEATURES].copy()
    calm = {"gust_kmh": 35.0, "rain_mm": 0.0, "tmax_c": 24.0}
    drivers = []
    lam0 = storm.predict(base)["faults_p50"].values
    contrib = {}
    for f in ("gust_kmh", "rain_mm", "tmax_c", "veg_cat_a", "open_defects", "bushfire_share"):
        alt = base.copy()
        alt[f] = calm.get(f, float(base[f].median()) if f not in calm else calm[f])
        contrib[f] = lam0 - storm.predict(alt)["faults_p50"].values
    for i in range(len(df)):
        items = sorted(((f, contrib[f][i]) for f in contrib), key=lambda x: -x[1])
        drivers.append(json.dumps([{"feature": f, "label": FEATURE_LABELS.get(f, f), "effect": round(float(v), 3)} for f, v in items[:3] if v > 0.001]))
    res["drivers"] = drivers
    res["model_version"] = f"storm_impact_model v{ver['storm_impact_model']}"
    res["scored_at"] = now
    sdf = spark.createDataFrame(res)
    sdf.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(fq("ml_storm_projection"))
    # 6-hour split of each day's expected faults, proportional to wind and rain severity in the window.
    wx6["day"] = pd.to_datetime(wx6["window_start"]).dt.strftime("%Y-%m-%d")
    wx6["w"] = np.exp((wx6["max_gust_kmh"].fillna(30) - 55) / 11) + wx6["rain_mm_6h"].fillna(0) / 10 + 0.05
    wx6["share"] = wx6["w"] / wx6.groupby(["feeder_id", "day"])["w"].transform("sum")
    w6 = wx6.merge(res[["feeder_id", "day", "faults_p50", "customers_p50", "crew_hours_p50", "crew_hours_p90"]], on=["feeder_id", "day"])
    for c in ("faults_p50", "customers_p50", "crew_hours_p50", "crew_hours_p90"):
        w6[c] = w6[c] * w6["share"]
    spark.createDataFrame(w6[["feeder_id", "window_start", "max_gust_kmh", "rain_mm_6h", "faults_p50", "customers_p50", "crew_hours_p50",
                              "crew_hours_p90"]]).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(fq("ml_storm_projection_6h"))
    for t, c in (("ml_storm_projection", "Storm impact projection per feeder and day for the live forecast: expected faults, customers "
                                         "interrupted and crew-hours (P10, P50, P90), with the top drivers and the model version."),
                 ("ml_storm_projection_6h", "Storm impact projection split into 6-hour windows by forecast severity.")):
        spark.sql(f"COMMENT ON TABLE {fq(t)} IS '{c}'")
    # ---------------------------------------------------------------- span fault risk
    sp = spark.sql(f"""
      SELECT a.asset_id span_id, a.feeder_id, a.age_years, a.rating length_m, v.clearance_m, v.growth_m_per_year, CAST(v.hazard_tree AS INT) hazard_tree,
             CAST(v.bushfire_prone AS INT) bushfire_prone, a.customers_downstream, CAST(a.feeder_id RLIKE '^(LPH|KNV|GVL)' AS INT) rural
      FROM {fq('gld_asset')} a JOIN {fq('gld_vegetation_risk')} v ON v.span_id = a.asset_id WHERE a.asset_class = 'overhead_span'""").toPandas().fillna(0)
    sp["fault_probability"] = span.predict_proba(sp[SPAN_FEATURES])[:, 1]
    sp["model_version"] = f"span_fault_risk_model v{ver['span_fault_risk_model']}"
    spark.createDataFrame(sp[["span_id", "feeder_id", "fault_probability", "model_version"]]).write.mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(fq("ml_span_risk"))
    spark.sql(f"COMMENT ON TABLE {fq('ml_span_risk')} IS 'Probability that each overhead span faults within a year (span fault risk model).'")
    # ---------------------------------------------------------------- load forecast with ai_forecast (AI Functions)
    try:
        spark.sql(f"""
          CREATE OR REPLACE TABLE {fq('ml_zone_load_forecast')} COMMENT 'Daily peak load forecast per zone substation for the next 14 days (ai_forecast).' AS
          SELECT * FROM ai_forecast(
            TABLE(SELECT zs_id, day, sum(peak_mw) peak_mw FROM {fq('gld_zone_load_daily')} WHERE day >= date_sub(current_date(), 400) GROUP BY ALL),
            horizon => date_add((SELECT max(day) FROM {fq('gld_zone_load_daily')}), 14), time_col => 'day', value_col => 'peak_mw',
            group_col => 'zs_id', prediction_interval_width => 0.8)""")
        fc = "ok"
    except Exception as e:  # noqa: BLE001 - ai_forecast is a preview that may be disabled; fall back to a seasonal baseline
        fc = f"fallback (ai_forecast unavailable: {str(e)[:120]})"
        spark.sql(f"""
          CREATE OR REPLACE TABLE {fq('ml_zone_load_forecast')} COMMENT 'Daily peak load forecast per zone substation for the next 14 days. Seasonal baseline (same weekday, last 8 weeks, with trend) because ai_forecast is disabled in this workspace.' AS
          WITH h AS (SELECT zs_id, day, sum(peak_mw) peak_mw FROM {fq('gld_zone_load_daily')} GROUP BY ALL),
          last AS (SELECT max(day) d FROM h),
          f AS (SELECT zs_id, date_add(l.d, k) day FROM (SELECT DISTINCT zs_id FROM h) CROSS JOIN last l LATERAL VIEW explode(sequence(1, 14)) t AS k)
          SELECT f.zs_id, f.day, round(avg(h.peak_mw), 2) peak_mw_forecast, round(percentile(h.peak_mw, 0.1), 2) peak_mw_lower,
                 round(percentile(h.peak_mw, 0.9), 2) peak_mw_upper
          FROM f JOIN h ON h.zs_id = f.zs_id AND dayofweek(h.day) = dayofweek(f.day) AND h.day >= date_sub(f.day, 63) GROUP BY ALL""")
    return dict(versions=ver, storm_rows=len(res), span_rows=len(sp), load_forecast=fc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--mode", default="all")
    a, _ = ap.parse_known_args()
    spark = _spark()
    fq = lambda t: f"`{a.catalog}`.`{a.schema}`.{t}"  # noqa: E731
    if a.mode in ("train", "all"):
        print("trained", train(spark, fq, a.src, a.catalog, a.schema), flush=True)
    if a.mode in ("score", "all"):
        print("scored", score(spark, fq, a.catalog, a.schema), flush=True)


if __name__ == "__main__":
    main()
