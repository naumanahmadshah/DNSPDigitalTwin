"""Create or update the Genie space over the twin's Gold layer and the live and app silver tables, and let the app's service principal use it.

Idempotent by title: the space whose title is exactly --title is updated, otherwise a new one is created, so each deployment has its own
space. Usage: build_genie.py --catalog <c> --schema <s> --warehouse <id> --title "<space title>" --app <app name>
"""
import argparse
import hashlib
import json

from databricks.sdk import WorkspaceClient

TABLES = ["gld_asset", "gld_feeder_state", "gld_zone_transformer_state", "gld_ds_state", "gld_outage", "gld_reliability_daily", "gld_reliability_year",
          "gld_vegetation_risk", "gld_fire_exposure", "gld_open_defect", "gld_work_order", "gld_crew", "gld_certification", "gld_field_job", "gld_customer_impact",
          "gld_dq_meter_mismatch", "gld_drone_detection", "gld_zone_load_daily", "ml_storm_projection", "ml_span_risk",
          "ml_zone_load_forecast", "gld_nmi", "gld_fire_incident", "gld_fire_danger", "ref_label", "gld_zone_supply", "gld_feeder_tie",
          "gld_suburb_supply", "gld_bulk_supply", "gld_subtransmission", "gld_transmission_substation", "gld_transmission_transformer_state",
          "gld_rm_region", "gld_crew_leave", "gld_crew_training", "gld_safety_event",
          "gld_eba_rule", "gld_situation_decision", "gld_customer_notice", "gld_field_dispatch", "slv_meter_event", "slv_call", "slv_app_notice", "slv_app_field_report", "slv_app_asset_photo", "slv_app_veg_bay_task", "gld_connection_application", "gld_veg_bay", "slv_app_audit_log", "slv_app_live_outage"]
QUESTIONS = [
    "Where are we most exposed this week, and do we have the right crews in the right places to deal with it?",
    "If tomorrow's storm hits the Ranges hard, which life support customers would be off longest, and which crews could reach them?",
    "Which customers are we most likely to let down in the next 72 hours, and why?",
    "What should worry me most before the bushfire season, across the network, the vegetation and the workforce?",
    "Are our crews where the risk is? Compare each region's storm, fire and vegetation exposure with the qualified crews it has.",
    "Which feeders have the highest vegetation risk in the next 12 months, and how many spans drive it?",
    "Which 10 feeders contributed most to customer minutes off supply in the last 12 months, by cause?",
    "How many customers did FLISR restore automatically in the last year, and how long did the rest wait on average?",
    "Which zone substation transformers have rising dissolved hydrogen or loading above 85 percent of nameplate?",
    "Which poles in bushfire-prone land have an open cross-arm or insulator defect older than 60 days?",
    "Which distribution transformers have the highest midday reverse flow from rooftop solar?",
    "Which life support customers sit on feeders with spans inside vegetation clearance?",
    "Which open defects share an isolation point and could be bundled into one planned outage?",
    "Which crews have tickets expired or expiring in the next 45 days?",
    "Which regions carry the most customer minutes at risk in the planned field work for the next 3 days, and in which job types?",
    "Are we on track against this year's SAIDI target, excluding major event days?",
    "Which feeders have spans within 3 km of a live fire, and how many customers are behind them?",
    "Which feeders does the storm model expect the most faults on in the next 72 hours, and why?",
    "Which situations were decided, who decided them, and were the recommendations accepted, adjusted or rejected?",
    "How many customer notices went out for each situation, to life support customers and to everyone else, and how many were delivered?",
]
GUIDANCE = [
    "This space describes a fictional electricity distributor over real NSW geography. Network data is simulated in the native "
    "shapes of real systems; fire incidents, fire danger ratings, hotspots and the weather forecast are real public feeds.",
    "Reliability follows the AER definitions: sustained interruptions are over 3 minutes; momentary interruptions (momentary = true) are excluded from "
    "SAIDI and SAIFI. SAIDI is customer minutes off supply divided by the customer base; use gld_reliability_daily and gld_reliability_year. Major event "
    "days (is_major_event_day) are excluded when the question says 'excluding major events'. The regulatory year runs July to June.",
    "Vegetation: gld_vegetation_risk holds the latest LiDAR campaign per overhead span; clearance_12m_m is the predicted clearance in 12 months; status "
    "red means cut now. Fire exposure: gld_fire_exposure has every span within 10 km of a live RFS incident or satellite hotspot with distance_m and band.",
    "Customers behind an asset come from gld_customer_impact or the customers_downstream column of gld_asset. Life support customers are gld_nmi.life_support.",
    "The network hierarchy runs from bulk supply points (gld_bulk_supply, 132 kV handover) to transmission substations (gld_transmission_substation, "
    "132 kV down to 33 or 66 kV, with their transformers in gld_transmission_transformer_state), then to "
    "zone substations (gld_zone_transformer_state), feeders (gld_feeder_state), distribution substations (gld_ds_state) and customers (gld_nmi). "
    "gld_subtransmission lists the circuits from each transmission substation (ts_id) to a zone substation (zs_id), with the bulk supply point behind it "
    "(bsp_id). firm_mva is the N-1 capacity; peak_utilisation_30d is the 30-day peak against it; single_transmission_zones lists the zones that lose "
    "supply if that site is lost. Backup supply: gld_zone_supply says how each zone substation is fed (ts_ids, transmission_substations, supply_circuits, "
    "bulk_supply_points, single_fed means one circuit and no alternative, single_transmission means one transmission substation and no alternative site). "
    "gld_feeder_tie lists each feeder's normally open "
    "ties (other_zone true means the tie reaches another zone substation). gld_suburb_supply has customers per suburb by feeder with life support, "
    "critical sites and critical_no_generator. Addresses are in gld_nmi (address, suburb, postcode, elevation_m).",
    "Field workforce: a crew is one deployable team and vehicle named by its leader. gld_crew has persona (trade), region_name (regions are the "
    "depots Penrith, Blacktown, Narellan, Ranges and Coast), employment_type (Internal or Contractor, contractors cost more), hourly_cost_aud, "
    "skills_label, tickets_expired, tickets_expiring_45d and open_safety_events (an open event keeps the crew off the roster). gld_certification "
    "has every crew ticket with ticket_label, expiry_date, days_to_expiry and status (expired, expiring within 45 days, current). gld_field_job "
    "is the planned field work by job_date: job_type_label, priority (1 low to 5 urgent), duration_min, region_name, required_skill_label, "
    "required_ticket_label, funding_label, customers and est_customer_minutes (customer minutes at risk if the job slips). gld_rm_region maps each depot "
    "region to its area and zone substations; gld_crew_leave and gld_crew_training say who is away on a day; gld_safety_event holds the safety "
    "events by crew (status Open keeps a crew off the roster); gld_eba_rule has the enterprise agreement limits per skill (12 hours a day).",
    "Broad questions ('where are we exposed', 'what should worry me', 'are crews where the risk is') want the domains joined, not one table: "
    "network risk (ml_storm_projection, gld_fire_exposure, gld_vegetation_risk, gld_open_defect, single-fed zones in gld_zone_supply and feeders "
    "without ties in gld_feeder_tie), the customers behind it (life support and critical sites from gld_nmi and gld_suburb_supply), and the field "
    "workforce that would respond (gld_crew by region and trade, tickets in gld_certification, leave, training and open safety events, and the planned "
    "work in gld_field_job). Rank by customers and life support exposed, say what drives each item, and say whether qualified crews are available "
    "near it. Answer the question asked, even when it is vague: state the interpretation in one line and go on.",
    "Defects sit on poles; the isolation point of a pole is the protective_device of its span (span id is 'S' followed by the pole number without the 'P').",
    "Storm projections: ml_storm_projection has expected faults (faults_p50), customers (customers_p50) and crew-hours (crew_hours_p50, p90) per feeder "
    "and day for the live forecast; drivers is JSON with the top reasons. Always present labels (cause_label, asset_class_label, area_label) rather than codes.",
    "Decisions and notices: gld_situation_decision has each situation with every recommendation made on it and the decision taken (decision_status "
    "is proposed, accepted, adjusted or rejected; decided_by and decided_at say who and when). gld_customer_notice has the customer notices per "
    "situation and recommendation, by audience and channel, with message counts by status (drafts, queued, sent, delivered). gld_field_dispatch has "
    "the raised field jobs with their field status, whether they are parked, the crew assigned and the crew on site. The slv_app_* tables hold the app's "
    "records as they stand now.",
    "Live streams: slv_meter_event holds the smart meter power outage and restoration alarms per NMI; slv_call holds the "
    "contact centre and IVR outage calls, one row per call with its NMI and reason.",
    "App records over time: slv_app_audit_log is the audit trail of changes to situations, jobs and crews, with the detail as JSON; slv_app_live_outage holds the outages in "
    "progress or restored, with customers off, life support customers off, the estimated time of restoration and the crew sent.",
    "Notices, field work and assets: slv_app_notice holds each customer notice (planned work, network fault, project work, severe weather) with "
    "its business rules, counts, drafts and who reviewed and sent it; slv_app_field_report holds what crews reported back from the field (patrol, "
    "fault located, segments cleared, switching, customers restored, outcome, notes); slv_app_asset_photo holds field photos tagged to assets with "
    "the AI findings and the reviewer decision (fixed, updated photo or rejected); gld_connection_application holds new connection applications "
    "with their journey stage (Apply to Commission, Connected), payment status and blockers; gld_veg_bay holds every vegetation bay with its "
    "clearance, predicted grow-in and risk tier, and slv_app_veg_bay_task the bays issued to contractors with their status.",
    "Answer in plain sentences with the key numbers; round money like $1.2M; do not mention table or column names in the prose.",
]
EXAMPLES = [
    ("Which 10 feeders contributed most to customer minutes off supply in the last 12 months?",
     "SELECT feeder_id, round(sum(customer_minutes)) AS customer_minutes, count(*) AS sustained_outages FROM {c}.{s}.gld_outage "
     "WHERE NOT momentary AND start_time >= date_sub(current_date(), 365) GROUP BY feeder_id ORDER BY customer_minutes DESC LIMIT 10"),
    ("Which feeders have the highest vegetation risk in the next 12 months?",
     "SELECT feeder_id, count(*) FILTER (WHERE status = 'red') AS spans_to_cut_now, count(*) FILTER (WHERE status = 'amber') AS spans_breaching_within_12_months, "
     "round(sum(risk_score)) AS risk FROM {c}.{s}.gld_vegetation_risk GROUP BY feeder_id ORDER BY risk DESC LIMIT 10"),
    ("Are we on track against this year's SAIDI target, excluding major event days?",
     "SELECT regulatory_year, round(saidi_ex_med, 1) AS saidi_ex_med, round(saidi_target_to_date, 1) AS target_to_date, major_event_days "
     "FROM {c}.{s}.gld_reliability_year ORDER BY regulatory_year DESC LIMIT 1"),
]


def hid(x):
    return hashlib.md5(x.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--app", required=True)
    a, _ = ap.parse_known_args()
    w = WorkspaceClient()
    space = {
        "version": 2,
        "data_sources": {"tables": sorted([{"identifier": f"{a.catalog}.{a.schema}.{t}"} for t in TABLES], key=lambda x: x["identifier"])},
        "config": {"sample_questions": sorted([{"id": hid(q), "question": [q]} for q in QUESTIONS], key=lambda x: x["id"])},
        "instructions": {
            "text_instructions": [{"id": hid("guidance"), "content": ["\n\n".join(GUIDANCE)]}],
            "example_question_sqls": sorted([{"id": hid(q), "question": [q], "sql": [sql.format(c=a.catalog, s=a.schema)]} for q, sql in EXAMPLES],
                                            key=lambda x: x["id"]),
        },
    }
    body = dict(title=a.title, description="Natural-language questions over the electricity distribution twin (Gold layer and live streams).",
                warehouse_id=a.warehouse, serialized_space=json.dumps(space))
    existing = [s for s in (w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces") or []) if s.get("title") == a.title]
    if existing:
        sid = existing[0]["space_id"]
        w.api_client.do("PATCH", f"/api/2.0/genie/spaces/{sid}", body=body)
    else:
        sid = w.api_client.do("POST", "/api/2.0/genie/spaces", body=body)["space_id"]
    try:
        sp = w.apps.get(a.app).service_principal_client_id
        w.api_client.do("PATCH", f"/api/2.0/permissions/genie/{sid}", body={"access_control_list": [{"service_principal_name": sp, "permission_level": "CAN_RUN"}]})
        print("granted CAN_RUN to", sp)
    except Exception as e:  # noqa: BLE001 - the app may not exist yet on a first deploy
        print("grant skipped:", repr(e)[:200])
    print("genie space", sid)


if __name__ == "__main__":
    main()
