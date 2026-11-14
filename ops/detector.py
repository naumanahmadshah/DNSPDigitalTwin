"""Situation detector: reads Gold, the ML scores, the live feeds and Lakebase, and raises situations (idempotent upserts).

Trip situations are raised by the emitter the moment a protection trip happens; everything else is detected here.
"""
import datetime as dt
import json

FIRE_BAND_KM = 5


def _qty(n, one, many=None):
    """A count with its noun, singular for one: '1 span', '2 spans', '1,268 customers'."""
    n = round(n)
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _upsert(pg, sid, typ, title, severity, headline, feeders, customers, ls, crit, payload):
    pg.q("""INSERT INTO atlas.situation (situation_id, type, title, severity, status, source, feeders, customers, life_support, critical_sites,
              headline, payload)
            VALUES (%s,%s,%s,%s,'detected','detector',%s,%s,%s,%s,%s,%s)
            ON CONFLICT (situation_id) DO UPDATE SET severity = EXCLUDED.severity, headline = EXCLUDED.headline, feeders = EXCLUDED.feeders,
              customers = EXCLUDED.customers, life_support = EXCLUDED.life_support, critical_sites = EXCLUDED.critical_sites,
              payload = EXCLUDED.payload, updated_at = now()
            WHERE atlas.situation.status NOT IN ('closed')
            RETURNING (xmax = 0) AS inserted""",
         (sid, typ, title, severity, feeders, customers, ls, crit, headline, json.dumps(payload, default=str)))
    pg.q("""INSERT INTO atlas.situation_event (situation_id, actor, action, detail)
            SELECT %s, 'detector', 'detected', %s WHERE NOT EXISTS (SELECT 1 FROM atlas.situation_event WHERE situation_id = %s)""",
         (sid, json.dumps(dict(trigger=headline)), sid))


def fires(spark, pg, fq):
    rows = spark.sql(f"""
      SELECT fire_id, any_value(source) source, any_value(title) title, any_value(alert_level) alert_level, any_value(status) status,
             any_value(incident_type) incident_type, min(distance_m) nearest_m, collect_set(feeder_id) feeders,
             count(DISTINCT span_id) FILTER (WHERE distance_m <= 3000) spans_3km, max(customers_downstream) FILTER (WHERE distance_m <= 3000) cust_3km
      FROM {fq('gld_fire_exposure')} GROUP BY fire_id HAVING min(distance_m) <= {FIRE_BAND_KM * 1000}""").collect()
    # Real incidents first; hotspots only when no incident is nearby.
    for r in rows:
        if r.source == "Satellite hotspot" and (r.nearest_m or 99999) > 1500:
            continue
        sev = ("critical" if (r.alert_level or "").startswith("Emergency") or (r.nearest_m < 1000 and r.source != "Satellite hotspot")
               else "high" if r.nearest_m < 3000 else "medium")
        name = (r.title or "Fire").split(",")[0]
        _upsert(pg, f"SIT-FIRE-{r.fire_id[-24:]}", "fire", f"{'Fire' if r.source != 'Satellite hotspot' else 'Hotspot'} near the network: {name}",
                sev, f"{r.source} ({r.alert_level or 'unrated'}, {r.status or 'status unknown'}) is {r.nearest_m / 1000:.1f} km from the nearest span. "
                     f"{_qty(r.spans_3km, 'span')} {'lies' if r.spans_3km == 1 else 'lie'} within 3 km, on {_qty(len(r.feeders), 'feeder')}.",
                sorted(r.feeders)[:12], int(r.cust_3km or 0), 0, 0,
                dict(fire_id=r.fire_id, source=r.source, nearest_m=r.nearest_m, alert_level=r.alert_level, incident_type=r.incident_type))


def storm(spark, pg, fq):
    r = spark.sql(f"""SELECT max(gust_kmh) gust, max_by(day, gust_kmh) day, sum(faults_p50) faults, sum(customers_p50) cust,
                        max(model_version) mv, max(scored_at) scored FROM {fq('ml_storm_projection')}""").first()
    if r.gust and r.gust >= 70:
        _upsert(pg, f"SIT-STORM-{r.day}", "storm", f"Strong winds forecast {r.day}", "high" if r.gust < 85 else "critical",
                f"Forecast gusts reach {r.gust:.0f} km/h on {r.day}, with about {_qty(r.faults, 'fault')} and {_qty(r.cust, 'customer')} off expected "
                f"across the network.", [], int(r.cust or 0), 0, 0, dict(peak_gust=r.gust, day=str(r.day), model_version=r.mv))


def hot_transformer(spark, pg, fq):
    for r in spark.sql(f"SELECT * FROM {fq('gld_zone_transformer_state')} WHERE status = 'red'").collect():
        live = pg.q("SELECT value -> %s FROM app.live_state WHERE key = 'zone_transformers'", (r.transformer_id,), fetch=True)
        top = (live[0][0] or {}).get("top_oil") if live and live[0][0] else r.top_oil_now
        _upsert(pg, f"SIT-HEAT-{r.transformer_id}", "heat", f"{r.name}: dissolved gas levels are rising", "high",
                f"Hydrogen has risen {r.h2_rise_90d:.0f} ppm in 90 days (now {r.h2_now:.0f} ppm). Top oil is {top:.0f} degrees, and the 30-day peak "
                f"is {r.peak_utilisation_30d * 100:.0f} percent of nameplate.", [], 0, 0, 0,
                dict(transformer_id=r.transformer_id, zs_id=r.zs_id, h2_now=r.h2_now, h2_rise=r.h2_rise_90d, top_oil=top))


def min_load(spark, pg, fq):
    """Minimum load: a sunny, mild midday ahead (strong solar radiation, no heat to drive air conditioning) is when rooftop solar
    pushes LV voltage up on high-solar transformers."""
    r = spark.sql(f"""SELECT to_date(forecast_time) d, round(avg(radiation_wm2)) rad, round(avg(temp_c), 1) t FROM {fq('slv_weather_forecast')}
                      WHERE hour(forecast_time) BETWEEN 11 AND 14 AND forecast_time >= current_timestamp()
                      GROUP BY 1 ORDER BY rad DESC LIMIT 1""").first()
    if r and r.rad and r.rad >= 750 and (r.t or 30) < 27:
        _upsert(pg, "SIT-MINLOAD-TODAY", "min_load", "Minimum system load at midday", "medium",
                f"Sunny, mild midday forecast on {r.d:%d %b} ({r.rad:,.0f} W/m² of solar, {r.t} degrees). "
                "Rooftop solar will push LV voltage up on high-solar transformers.", [], 0, 0, 0,
                dict(day=str(r.d), radiation_wm2=r.rad, temp_c=r.t))


def vegetation(spark, pg, fq, as_of):
    m = dt.date.fromisoformat(as_of).month
    if m in (10, 11, 12, 1, 2, 3):
        r = spark.sql(f"""SELECT count(*) FILTER (WHERE status = 'red') red, count(*) FILTER (WHERE status = 'amber') amber,
                            count(DISTINCT feeder_id) FILTER (WHERE status = 'red') feeders FROM {fq('gld_vegetation_risk')}""").first()
        _upsert(pg, f"SIT-VEG-{as_of[:7]}", "vegetation", "Pre-summer vegetation readiness", "medium",
                f"{_qty(r.red, 'span')} {'needs' if r.red == 1 else 'need'} cutting before summer. A further {_qty(r.amber, 'span')} "
                f"will breach within 12 months, across {_qty(r.feeders, 'feeder')}.",
                [], 0, 0, 0, dict(red=r.red, amber=r.amber))


def drone(spark, pg, fq):
    for r in spark.sql(f"SELECT * FROM {fq('gld_drone_detection')} WHERE score >= 0.85 AND defect_class IN ('cracked_crossarm', "
                       f"'damaged_insulator', 'pole_damage') ORDER BY score DESC LIMIT 3").collect():
        _upsert(pg, f"SIT-DRONE-{r.detection_id[-30:]}", "drone_defect", f"Drone spotted {r.defect_class.replace('_', ' ')} on {r.pole_id}", "medium",
                f"Patrol image {r.file_name} ({r.captured_at:%d %b}) has a detection score of {r.score:.2f}, {r.distance_m:.0f} m from pole {r.pole_id}.",
                [r.feeder_id], 0, 0, 0, dict(detection_id=r.detection_id, pole_id=r.pole_id, file_name=r.file_name, defect=r.defect_class))


def records(spark, pg, fq):
    r = spark.sql(f"SELECT count(*) n, count(DISTINCT recorded_ds) ds FROM {fq('gld_dq_meter_mismatch')}").first()
    if r.n:
        # medium: a records error that needs a correction (it misdirects outage notices), raised like any other alert
        _upsert(pg, "SIT-RECORDS", "records", "Meters recorded against the wrong transformer", "medium",
                f"The voltage of {_qty(r.n, 'meter')} follows a different transformer from the one in the records ({_qty(r.ds, 'transformer')} affected). Outage "
                f"notices for these customers would go to the wrong people.", [], int(r.n), 0, 0, dict(meters=r.n))


def fatigue(pg):
    """Crews whose planned day runs past the 12 hour cap (authorised overtime, or a callout on top of a full day), from the day
    resource management last planned for each crew."""
    rows = pg.q("""SELECT d.crew_id, round((d.planned_min / 60)::numeric, 1) FROM app.crew_day d WHERE d.planned_min > 720
                   AND NOT EXISTS (SELECT 1 FROM rm.stood_down s WHERE s.crew_id = d.crew_id)""", fetch=True) or []
    if rows:
        _upsert(pg, f"SIT-FATIGUE-{dt.date.today()}", "fatigue", "Crews past the 12-hour cap", "high",
                f"{_qty(len(rows), 'crew')} {'is' if len(rows) == 1 else 'are'} planned past the enterprise agreement's 12-hour cap. "
                "Stand down the affected crews and re-roster their stops.", [], 0, 0, 0,
                dict(crews=[dict(crew_id=c, hours=float(h)) for c, h in rows]))


def run(spark, pg, fq, cfg):
    for f in (lambda: fires(spark, pg, fq), lambda: storm(spark, pg, fq),
              lambda: hot_transformer(spark, pg, fq), lambda: min_load(spark, pg, fq), lambda: vegetation(spark, pg, fq, cfg["as_of"]),
              lambda: drone(spark, pg, fq), lambda: records(spark, pg, fq), lambda: fatigue(pg)):
        try:
            f()
        except Exception as e:  # noqa: BLE001 - one failing check must not stop the others
            print("detector check failed", repr(e)[:300], flush=True)
