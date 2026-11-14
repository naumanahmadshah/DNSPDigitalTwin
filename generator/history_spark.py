"""Spark history generator (serverless job task).

Writes:
- <catalog>.<schema>.share_historian_archive : one year of 15-minute historian values for zone transformers, transmission
  transformers and feeder heads, in the shape a historian publishes over Delta Sharing (tag, timestamp, value, quality flags, units).
- landing/nem12/*.csv     : 60 days of NEM12 interval files for the hero slice (one file per metering provider per day).
- landing/meter_voltage/  : 2 days of 5-minute meter voltage (a metering coordinator voltage service), hero slice.
Usage: history_spark.py --src <dir> --catalog <c> --schema <s> --root /Volumes/<c>/<s>/landing
"""
import argparse
import datetime as dt
import json
import math
import os
import random
import sys


def build_historian(spark, truth, weather, catalog, schema, as_of, days):
    from pyspark.sql import functions as F

    end = dt.datetime.fromisoformat(as_of)
    start = end - dt.timedelta(days=days)
    # Tag dimension with per-tag load parameters.
    rows = []
    cust = truth["customers_by_feeder"]
    feeder_zone = {f["feeder_id"]: f["zs_id"][3:] for f in truth["feeders"]}
    zone_load = {}
    for f in truth["feeders"]:
        mw = cust.get(f["feeder_id"], 0) * 0.0021 + 0.4  # about 2.1 kW per customer at peak plus commercial base
        zone_load[f["zs_id"]] = zone_load.get(f["zs_id"], 0) + mw
        k = f["kv"]
        rows += [(f"{f['feeder_id']}.MW.PV", f["feeder_id"], "MW", "MW", feeder_zone[f["feeder_id"]], mw, 0.0, f["rating_amps"], k),
                 (f"{f['feeder_id']}.AMPS.PV", f["feeder_id"], "AMPS", "A", feeder_zone[f["feeder_id"]], mw, 0.0, f["rating_amps"], k),
                 (f"{f['feeder_id']}.KV.PV", f["feeder_id"], "KV", "kV", feeder_zone[f["feeder_id"]], mw, 0.0, f["rating_amps"], k)]
    for t in truth["zs_transformers"]:
        mw = zone_load[t["zs_id"]] / 2
        z = t["zs_id"][3:]
        stress = 1.25 if t["asset_id"] == "ZS-KNV-TX1" else 1.0  # the transformer that runs hot (Situation 4)
        for attr, unit in (("MW", "MW"), ("TOP_OIL_TEMP", "degC"), ("WINDING_TEMP", "degC"), ("TAP_POS", ""), ("H2_PPM", "ppm")):
            rows.append((f"{t['asset_id'].replace('ZS-', '')}.{attr}.PV", t["asset_id"], attr, unit, z, mw * stress, t["rating_mva"],
                         0, 0))
    # Transmission transformers (TS-<site>-TX<n>): each site carries the load of the zones it feeds (a zone fed from two sites splits its
    # load by circuit), shared across its transformers. Weather is by zone, so a site takes the weather of the first zone it feeds.
    ts_load, ts_zone = {}, {}
    for z in truth["zones"]:
        for ts in set(z["supply_ts"]):
            ts_load[ts] = ts_load.get(ts, 0) + zone_load[z["zs_id"]] * z["supply_ts"].count(ts) / len(z["supply_ts"])
            ts_zone.setdefault(ts, z["code"])
    n_tx = {}
    for t in truth.get("ts_transformers", []):
        n_tx[t["ts_id"]] = n_tx.get(t["ts_id"], 0) + 1
    for t in truth.get("ts_transformers", []):
        mw = ts_load[t["ts_id"]] / n_tx[t["ts_id"]]
        for attr, unit in (("MW", "MW"), ("TOP_OIL_TEMP", "degC"), ("WINDING_TEMP", "degC"), ("TAP_POS", "")):
            rows.append((f"{t['asset_id']}.{attr}.PV", t["asset_id"], attr, unit, ts_zone[t["ts_id"]], mw, t["rating_mva"], 0, 132))
    tags = spark.createDataFrame(rows, "tag string, element string, attribute string, units string, zone string, peak_mw double, "
                                       "rating_mva double, rating_amps int, kv int")
    w = spark.createDataFrame([(z, d, v["tmax"], v["tmin"]) for z, ds in weather.items() for d, v in ds.items()],
                              "zone string, day string, tmax double, tmin double")
    ts = spark.sql(f"SELECT explode(sequence(timestamp'{start:%Y-%m-%d %H:%M:%S}', timestamp'{end:%Y-%m-%d %H:%M:%S}', "
                   f"interval 15 minutes)) AS ts")
    df = (tags.crossJoin(ts)
          .withColumn("day", F.date_format("ts", "yyyy-MM-dd"))
          .join(w, ["zone", "day"], "left")
          .withColumn("h", F.hour("ts") + F.minute("ts") / 60.0)
          .withColumn("noise", (F.abs(F.xxhash64("tag", "ts")) % 10000) / 10000.0 - 0.5)
          .withColumn("tmax", F.coalesce("tmax", F.lit(24.0))).withColumn("tmin", F.coalesce("tmin", F.lit(13.0)))
          # Ambient: diurnal between tmin (05:00) and tmax (15:00).
          .withColumn("amb", F.col("tmin") + (F.col("tmax") - F.col("tmin")) * (0.5 - 0.5 * F.cos((F.col("h") - 3) / 24 * 2 * math.pi)))
          # Residential load shape: morning and evening peaks, a midday solar dip, more load on hot and cold days.
          .withColumn("shape", 0.45 + 0.18 * F.exp(-F.pow((F.col("h") - 7.5) / 1.6, 2)) + 0.42 * F.exp(-F.pow((F.col("h") - 18.5) / 2.2, 2))
                      - 0.12 * F.exp(-F.pow((F.col("h") - 12.5) / 2.0, 2)))
          .withColumn("weather_mult", 1 + F.greatest(F.lit(0.0), F.col("tmax") - 28) * 0.035 + F.greatest(F.lit(0.0), 12 - F.col("tmin")) * 0.02)
          .withColumn("mw", F.col("peak_mw") * F.col("shape") * F.col("weather_mult") * (1 + 0.06 * F.col("noise"))))
    load_frac = F.col("mw") / F.when(F.col("rating_mva") > 0, F.col("rating_mva")).otherwise(F.lit(1.0))
    # DGA hydrogen on the stressed transformer climbs over the last 90 days.
    days_left = F.datediff(F.lit(end.date()), F.col("ts").cast("date"))
    value = (F.when(F.col("attribute") == "MW", F.col("mw"))
             .when(F.col("attribute") == "AMPS", F.col("mw") * 1000 / (F.sqrt(F.lit(3.0)) * F.col("kv")) * 1.03)
             .when(F.col("attribute") == "KV", F.col("kv") * (1.02 - 0.025 * F.col("shape") + 0.004 * F.col("noise")))
             .when(F.col("attribute") == "TOP_OIL_TEMP", F.col("amb") + 8 + 48 * F.pow(load_frac, 2) + 1.2 * F.col("noise"))
             .when(F.col("attribute") == "WINDING_TEMP", F.col("amb") + 12 + 62 * F.pow(load_frac, 2) + 1.5 * F.col("noise"))
             .when(F.col("attribute") == "TAP_POS", F.round(9 + 4 * (F.col("shape") - 0.6)))
             .when(F.col("attribute") == "H2_PPM", F.when(F.col("element") == "ZS-KNV-TX1",
                                                       F.greatest(F.lit(60.0), 420 - 3.6 * days_left) + 8 * F.col("noise"))
                   .otherwise(40 + 15 * F.col("noise"))))
    out = (df.withColumn("value", F.round(value, 3))
           .withColumn("good", F.lit(True)).withColumn("questionable", (F.col("noise") > 0.4985))
           .withColumn("substituted", F.lit(False))
           .select("tag", F.col("ts").alias("timestamp"), "value", "good", "questionable", "substituted",
                   F.col("units").alias("units_abbreviation"), "element", "attribute"))
    tbl = f"`{catalog}`.`{schema}`.share_historian_archive"
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(tbl)
    spark.sql(f"COMMENT ON TABLE {tbl} IS 'Historian archive published over Delta Sharing (AVEVA CONNECT style): one year of "
              f"15-minute interpolated values for zone transformers and feeder heads. Simulated source.'")
    return spark.table(tbl).count()


def _profile(h, solar_kw, ev, rnd):
    base = 0.18 + 0.25 * math.exp(-((h - 7.5) / 1.5) ** 2) + 0.55 * math.exp(-((h - 18.8) / 2.0) ** 2)
    load = base * rnd.uniform(0.7, 1.4)
    if ev and 19 <= h <= 23.5:
        load += 1.8 * rnd.random()
    gen = 0.0
    if solar_kw:
        gen = max(0.0, math.sin(math.pi * (h - 6) / 13.0)) * solar_kw * 0.45 * rnd.uniform(0.6, 1.0)
    return load, gen


def write_nem12(truth, root, as_of, days, seed):
    """One NEM12 file per metering provider per day (100 header, 200 per stream, 300 day record, 900 end)."""
    end = dt.date.fromisoformat(as_of)
    out = os.path.join(root, "nem12")
    os.makedirs(out, exist_ok=True)
    nmis = truth["hero_nmis"]
    mdp = ["MDPA", "MDPB", "MDPC"]
    files = 0
    for d in range(days, 0, -1):
        day = end - dt.timedelta(days=d)
        by_mdp = {m: [] for m in mdp}
        for n in nmis:
            by_mdp[mdp[int(n["nmi"]) % 3]].append(n)
        for m, rows in by_mdp.items():
            lines = [f"100,NEM12,{day:%Y%m%d}0430,{m},LNSP0001"]
            for n in rows:
                rnd = random.Random(f"{seed}|nem12|{n['nmi']}|{day}")
                ints = [_profile(i / 2 + 0.25, n["solar_kw"], n["ev"], rnd) for i in range(48)]
                # E1 import, B1 export; net per interval as metered by a bidirectional meter.
                imp = [max(0.0, l - g) / 2 for l, g in ints]
                exp = [max(0.0, g - l) / 2 for l, g in ints]
                cfg = "E1B1" if n["solar_kw"] else "E1"
                lines.append(f"200,{n['nmi']},{cfg},E1,E1,N1,M{n['nmi'][-6:]},KWH,30,{(day + dt.timedelta(days=30)):%Y%m%d}")
                q = "A" if rnd.random() > 0.01 else "V"
                lines.append(f"300,{day:%Y%m%d}," + ",".join(f"{v:.3f}" for v in imp) + f",{q},,,{day:%Y%m%d}040000,")
                if q == "V":
                    lines.append("400,1,30,A,,")
                    lines.append("400,31,48,S53,79,Substituted after communications failure")
                if n["solar_kw"]:
                    lines.append(f"200,{n['nmi']},{cfg},B1,B1,N2,M{n['nmi'][-6:]},KWH,30,{(day + dt.timedelta(days=30)):%Y%m%d}")
                    lines.append(f"300,{day:%Y%m%d}," + ",".join(f"{v:.3f}" for v in exp) + f",A,,,{day:%Y%m%d}040000,")
            lines.append("900")
            with open(os.path.join(out, f"NEM12#{day:%Y%m%d}0430#{m}#LNSP0001.csv"), "w") as f:
                f.write("\n".join(lines) + "\n")
            files += 1
    return files


def write_meter_voltage(spark, truth, root, as_of):
    """Two days of 5-minute average voltage per hero meter. Voltage follows the TRUE transformer, which is what lets
    the data-quality situation find meters recorded against the wrong transformer."""
    from pyspark.sql import functions as F

    end = dt.datetime.fromisoformat(as_of)
    start = end - dt.timedelta(days=2)
    nm = spark.createDataFrame([(n["nmi"], n["ds_id"]) for n in truth["hero_nmis"]], "nmi string, true_ds string")
    ts = spark.sql(f"SELECT explode(sequence(timestamp'{start:%Y-%m-%d %H:%M:%S}', timestamp'{end:%Y-%m-%d %H:%M:%S}', "
                   f"interval 5 minutes)) AS interval_end")
    h = F.hour("interval_end") + F.minute("interval_end") / 60.0
    ds_sig = (F.abs(F.xxhash64("true_ds", F.floor(F.unix_timestamp("interval_end") / 900))) % 1000) / 1000.0 - 0.5
    own = (F.abs(F.xxhash64("nmi", "interval_end")) % 1000) / 1000.0 - 0.5
    v = 240 + 4.5 * F.exp(-F.pow((h - 12.5) / 2.2, 2)) - 5.5 * F.exp(-F.pow((h - 18.8) / 2.0, 2)) + 3.2 * ds_sig + 0.7 * own
    df = nm.crossJoin(ts).select("nmi", F.date_format("interval_end", "yyyy-MM-dd HH:mm:ss").alias("interval_end"),
                                 F.round(v, 1).alias("v_avg"))
    path = os.path.join(root, "meter_voltage")
    df.repartition(8).write.mode("overwrite").option("header", True).csv(path)
    return df.count()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--root", required=True)
    a, _ = ap.parse_known_args(argv)
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    cfg = json.load(open(os.path.join(a.src, "config", "atlas.json")))
    truth = json.load(open(os.path.join(a.root, "_generator", "network_truth.json")))
    sys.path.insert(0, os.path.join(a.src, "generator"))
    from history import _weather
    weather = _weather(os.path.join(a.src, "config"))
    n = build_historian(spark, truth, weather, a.catalog, a.schema, cfg["as_of"], cfg["scada_history_days"])
    print("historian rows", n, flush=True)
    print("nem12 files", write_nem12(truth, a.root, cfg["as_of"], cfg["nem12_days"], cfg["seed"]), flush=True)
    print("meter voltage rows", write_meter_voltage(spark, truth, a.root, cfg["as_of"]), flush=True)


if __name__ == "__main__":
    main()
