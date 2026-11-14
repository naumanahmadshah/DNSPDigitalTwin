# Gold: business-ready marts for the app, dashboards and Genie. Every code is mapped to a label here (ref_label), not in the UI.
from collections import defaultdict

from pyspark import pipelines as dp
from pyspark.sql import functions as F

CAT = spark.conf.get("atlas.catalog")
SCH = spark.conf.get("atlas.schema")
AS_OF = spark.conf.get("atlas.as_of")
METRIC_SRID = 7856  # GDA2020 / MGA zone 56, metres (the footprint lies between 150 and 156 degrees east)

LABELS = [
    ("cause", "VEG", "Vegetation contact"), ("cause", "WND", "Wind-blown debris"), ("cause", "LTN", "Lightning"),
    ("cause", "EQF", "Equipment failure"), ("cause", "ANM", "Animal contact"), ("cause", "VEH", "Vehicle impact"),
    ("cause", "TPD", "Third-party damage"), ("cause", "UNK", "Cause unknown"),
    ("asset_class", "pole", "Pole"), ("asset_class", "overhead_span", "Overhead span"), ("asset_class", "cable_section", "Underground cable"),
    ("asset_class", "distribution_substation", "Distribution substation"), ("asset_class", "zone_substation", "Zone substation"),
    ("asset_class", "transmission_substation", "Transmission substation"), ("asset_class", "transmission_transformer", "Transmission transformer"),
    ("asset_class", "transmission_line", "132 kV line"),
    ("asset_class", "cb", "Feeder circuit breaker"), ("asset_class", "recloser", "Recloser"), ("asset_class", "gas_switch", "Gas switch"),
    ("asset_class", "fuse", "Drop-out fuse"), ("asset_class", "swer_isolator", "SWER isolator"), ("asset_class", "nop", "Normally open point"),
    ("device_type", "CB", "Feeder circuit breaker"), ("device_type", "RECLOSER", "Recloser"), ("device_type", "FUSE", "Drop-out fuse"),
    ("device_type", "SWER_ISOLATOR", "SWER isolator"), ("device_type", "GAS_SWITCH", "Gas switch"),
    ("area", "MET", "Metro West"), ("area", "RNG", "Ranges"), ("area", "CST", "Coast"),
    ("defect", "CRACKED_CROSSARM", "Cracked cross-arm"), ("defect", "INSULATOR_DAMAGE", "Damaged insulator"),
    ("defect", "POLE_ROT", "Pole rot at ground line"), ("defect", "LEANING_POLE", "Leaning pole"),
    ("defect", "CONDUCTOR_DAMAGE", "Conductor strand damage"), ("defect", "VEGETATION_ENCROACH", "Vegetation inside clearance"),
    ("defect", "TIE_WIRE_LOOSE", "Loose tie wire"), ("defect", "FUSE_HOLDER", "Damaged fuse holder"),
    ("order_type", "PM01", "Corrective maintenance"), ("order_type", "PM02", "Priority defect repair"), ("order_type", "PM03", "Fault repair"), ("order_type", "PM04", "Routine inspection"), ("order_status", "TECO", "Complete"), ("order_status", "REL", "Released, work under way"),
    ("job_type", "LV_MAINT", "LV maintenance"), ("job_type", "HV_MAINT", "HV maintenance"), ("job_type", "CABLE_REPAIR", "Cable repair"),
    ("job_type", "SUBSTATION_MAINT", "Substation maintenance"), ("job_type", "PROTECTION_TEST", "Protection test"),
    ("job_type", "CUSTOMER_CONNECTION", "Customer connection"), ("job_type", "FAULT_RESPONSE", "Fault response"),
    ("job_type", "VEGETATION", "Vegetation"), ("job_type", "ASSET_INSPECTION", "Asset inspection"), ("job_type", "CIVIL", "Civil works"),
    ("job_type", "METERING", "Metering"), ("job_type", "SERVICE_CALL", "Service call"),
    ("skill", "LV_HV", "Overhead lines (LV and HV)"), ("skill", "CABLE_JOINT", "Cable jointing"), ("skill", "SUBSTATION", "Substations"),
    ("skill", "PROTECTION", "Protection and control"), ("skill", "VEG", "Vegetation management"), ("skill", "INSPECTION", "Asset inspection and drone"),
    ("skill", "CIVIL", "Civil works"), ("skill", "METERING", "Metering"),
    ("ticket", "HV_SWITCHING", "HV switching"), ("ticket", "WORK_HEIGHTS", "Working at heights"), ("ticket", "EWP", "Elevated work platform"),
    ("ticket", "POLE_RESCUE", "Pole-top rescue"), ("ticket", "CABLE_JOINT", "Cable jointing"), ("ticket", "CONFINED_SPACE", "Confined space"),
    ("ticket", "ARC_FLASH", "Arc flash"), ("ticket", "PROTECTION_RELAY", "Protection relay"), ("ticket", "CHAINSAW", "Chainsaw"),
    ("ticket", "TRAFFIC_MGMT", "Traffic management"), ("ticket", "CASA_REPL", "Drone pilot licence (CASA RePL)"), ("ticket", "FIRST_AID", "First aid"),
    ("ticket", "CPR", "CPR"), ("ticket", "METERING", "Metering"),
    ("funding", "CUSTOMER", "Customer funded"), ("funding", "CAPEX", "Network capital"), ("funding", "OPEX", "Network maintenance"),
    ("funding", "RETAILER", "Retailer service order"), ("funding", "EMERGENCY", "Emergency response"),
    ("event_type", "INCIDENT", "Incident"), ("event_type", "NEAR_MISS", "Near miss"), ("event_type", "HAZARD", "Hazard"),
    ("leave_type", "ANNUAL", "Annual leave"), ("leave_type", "PERSONAL", "Personal leave"), ("leave_type", "RDO", "Rostered day off"),
    ("source", "defect", "Inspection defect"), ("source", "vegetation", "Vegetation program"), ("source", "patrol", "Bushfire patrol"),
    ("source", "inspection", "Asset inspection"), ("source", "maintenance", "Planned maintenance"), ("source", "protection", "Protection testing"),
    ("source", "cable", "Cable program"), ("source", "civil", "Civil works"), ("source", "metering", "Metering service order"),
    ("source", "service", "Customer service call"), ("source", "connection", "Customer connection"), ("source", "callout", "Customer callout"),
    ("source", "outage", "Live outage"), ("source", "storm", "Storm damage"), ("source", "situation", "Situation response"),
    ("source", "manual", "Raised by a dispatcher"),
    ("vehicle_type", "EWP", "Elevated work platform truck"), ("vehicle_type", "LINE_TRUCK", "Line truck"),
    ("vehicle_type", "JOINTING_VAN", "Cable jointing van"), ("vehicle_type", "UTE", "Utility vehicle"),
    ("vehicle_type", "VEG_EWP", "Vegetation EWP and chipper"), ("vehicle_type", "DRONE_UTE", "Utility vehicle with drone kit"),
    ("vehicle_type", "CIVIL_PLANT", "Excavator and tipper"), ("vehicle_type", "BORER", "Pole borer"), ("vehicle_type", "VAN", "Service van"),
    ("vehicle_type", "MOBILE_GENERATOR", "Mobile generator"),
    ("critical_site", "hospital", "Hospital"), ("critical_site", "data_centre", "Data centre"),
    ("critical_site", "telecom", "Telecommunications site"), ("critical_site", "aged_care", "Aged care home"),
    ("critical_site", "water_pumping", "Water pumping station"), ("critical_site", "school", "School"),
    ("veg", "A", "Inside clearance (urgent)"), ("veg", "B", "Inside 2 m"), ("veg", "C", "Inside 3 m"), ("veg", "CLEAR", "Clear"),
    ("status", "red", "Needs action"), ("status", "amber", "Watch"), ("status", "green", "Normal"),
    ("situation_type", "storm", "Storm"), ("situation_type", "fire", "Fire"), ("situation_type", "trip", "Feeder trip"),
    ("situation_type", "heat", "Hot transformer"), ("situation_type", "min_load", "Minimum load"), ("situation_type", "vegetation", "Vegetation"),
    ("situation_type", "drone_defect", "Drone defect"), ("situation_type", "fatigue", "Crew fatigue"), ("situation_type", "records", "Records disagree"),
    ("audience", "life_support", "Life support customers"), ("audience", "customer", "Customers"),
    ("outbox_status", "queued", "Queued (can still be undone)"), ("outbox_status", "sent", "Sent"),
    # feature labels (notices, connections, planning, field reports, asset photos, vegetation)
    ("veg_class", "current_intrusion", "Current intrusion"),
    ("veg_class", "predicted_grow_in", "Predicted grow-in in 12 months"),
    ("veg_class", "no_action", "No action required"),
    ("veg_tier", "high_risk", "High risk"),
    ("veg_tier", "cut_required", "Cut required"),
    ("veg_tier", "planned", "Planned"),
    ("veg_tier", "no_action", "No action required"),
    ("veg_basis", "bushfire_prone", "Bushfire-prone land, 3 m"),
    ("veg_basis", "general", "General overhead span, 2 m"),
    ("veg_task_status", "issued", "Issued to contractor"),
    ("veg_task_status", "in_progress", "In progress"),
    ("veg_task_status", "awaiting_audit", "Cut complete, awaiting audit"),
    ("veg_task_status", "closed", "Closed, accepted"),
    ("veg_audit_result", "pending", "Awaiting decision"),
    ("veg_audit_result", "compliant", "Compliant, accepted and closed"),
    ("veg_audit_result", "non_compliant", "Non-compliant, reissued to contractor"),
    ("veg_exception", "access_blocked", "Access blocked"),
    ("veg_exception", "partial_cut", "Partial cut only"),
    ("veg_exception", "tree_too_large", "Tree too large for the crew"),
    ("veg_exception", "hazard", "Hazard found on site"),
    ("veg_exception", "landowner", "Landowner objection"),
    ("veg_exception", "equipment", "Equipment fault"),
    ("veg_exception", "other", "Other"),
    ("veg_photo_kind", "before", "Before the cut"),
    ("veg_photo_kind", "after", "After the cut"),
    ("veg_photo_kind", "exception", "Exception"),
    ("constraint_type", "zone_capacity", "Zone substation capacity"),
    ("constraint_type", "zone_security", "Zone supply security (single fed)"),
    ("constraint_type", "feeder_capacity", "Feeder capacity"),
    ("constraint_type", "feeder_voltage", "Voltage (rooftop solar export)"),
    ("constraint_type", "feeder_asset_risk", "Overhead asset risk"),
    ("constraint_type", "transformer_asset_risk", "Transformer condition"),
    ("constraint_type", "feeder_reliability", "Feeder reliability"),
    ("constraint_type", "transmission_capacity", "Transmission substation capacity"),
    ("constraint_level", "zone", "Zone substation"),
    ("constraint_level", "feeder", "Feeder"),
    ("constraint_level", "transformer", "Transformer"),
    ("constraint_level", "transmission", "Transmission substation"),
    ("option_type", "maintain", "Maintain"),
    ("option_type", "replace", "Replace"),
    ("option_type", "augment", "Augment"),
    ("option_type", "reconfigure", "Reconfigure"),
    ("option_type", "flexibility", "Flexibility (storage, DER, demand response)"),
    ("option_type", "defer", "Defer and monitor"),
    ("isolation_basis", "switch", "Switch in the connectivity model"),
    ("isolation_basis", "protective", "Protective device in the connectivity model"),
    ("isolation_basis", "nearest", "Nearest switch on the feeder (asset not in the connectivity model)"),
    ("isolation_basis", "feeder", "Feeder breaker (no switch found)"),
    ("source", "plan", "Integrated works plan"),
    ("field_outcome", "fixed", "Fixed"),
    ("field_outcome", "temporary_repair", "Temporary repair"),
    ("field_outcome", "follow_up", "Needs follow up"),
    ("field_access", "clear", "Clear"),
    ("field_access", "restricted", "Restricted"),
    ("field_access", "blocked", "Blocked"),
    ("field_fault_kind", "pole", "Pole"),
    ("field_fault_kind", "span", "Span"),
    ("field_fault_kind", "none", "Not found"),
    ("field_hazard", "live_wire", "Live wire down"),
    ("field_hazard", "tree", "Tree on the line"),
    ("field_hazard", "flood", "Flood water"),
    ("field_hazard", "fire", "Fire or smoke"),
    ("field_hazard", "traffic", "Traffic"),
    ("field_hazard", "unstable_pole", "Unstable pole"),
    ("field_material", "cross_arm", "Cross arm"),
    ("field_material", "conductor", "Conductor (m)"),
    ("field_material", "insulator", "Insulator"),
    ("field_material", "fuse_link", "Fuse link"),
    ("field_material", "pole", "Pole"),
    ("field_material", "lv_cable", "LV cable (m)"),
    ("field_material", "tie_wire", "Tie wire"),
    ("ops_channel", "sms", "SMS"),
    ("ops_channel", "email", "Email"),
    ("ops_channel", "app", "App or web"),
    ("ops_channel", "contact_centre", "Contact centre"),
    ("ops_channel", "mail", "Mail"),
    ("ops_crew_status", "travelling", "Travelling"),
    ("ops_crew_status", "on_site", "On site"),
    ("ops_crew_status", "available", "Available"),
    ("ops_job_status", "planned", "Planned"),
    ("ops_job_status", "driving", "Driving"),
    ("ops_job_status", "in_progress", "In progress"),
    ("ops_job_status", "done", "Done"),
    ("ops_job_status", "parked", "On hold"),
    ("ops_job_status", "reassigning", "Re-rostering"),
    ("ops_job_status", "reassigned", "Re-rostered"),
    ("ops_meter_event", "POWER_OUTAGE", "Power off alarm"),
    ("ops_meter_event", "POWER_RESTORE", "Power restored alarm"),
    ("ops_switch_quality", "good", "Good"),
    ("ops_switch_quality", "questionable", "Questionable"),
    ("ops_switch_quality", "comm_lost", "Communications lost"),
    ("ops_switch_quality", "substituted", "Substituted"),
    ("ops_drone_defect", "vegetation_contact", "Vegetation contact"),
    ("ops_drone_defect", "bird_nest", "Bird nest"),
    ("ops_drone_defect", "pole_damage", "Pole damage"),
    ("ops_drone_defect", "damaged_insulator", "Damaged insulator"),
    ("ops_drone_defect", "cracked_crossarm", "Cracked cross arm"),
    ("asset_photo_status", "uploaded", "Uploaded, AI check running"),
    ("asset_photo_status", "ai_checked", "AI checked, awaiting review"),
    ("asset_photo_status", "fixed", "Accepted as fixed"),
    ("asset_photo_status", "updated", "Accepted as updated photo"),
    ("asset_photo_status", "rejected", "Rejected"),
    ("asset_photo_source", "field", "Field crew"),
    ("asset_review_decision", "fixed", "Accept as fixed"),
    ("asset_review_decision", "updated", "Accept as updated photo"),
    ("asset_review_decision", "rejected", "Reject and delete"),
    ("asset_ai_outcome", "fixed", "Looks fixed"),
    ("asset_ai_outcome", "updated", "Usable current photo"),
    ("asset_ai_outcome", "rejected", "Does not match or unusable"),
    ("asset_condition", "good", "Good"),
    ("asset_condition", "fair", "Fair"),
    ("asset_condition", "poor", "Poor"),
    ("asset_condition", "unclear", "Unclear"),
    ("notice_type", "planned_work", "Planned work"),
    ("notice_type", "network_fault", "Network fault"),
    ("notice_type", "project_work", "Custom notice"),
    ("notice_type", "severe_weather", "Weather hazard"),
    ("notice_trigger", "planned_job", "Planned work"),
    ("notice_trigger", "fault", "Network fault"),
    ("notice_trigger", "project", "Custom notice"),
    ("notice_trigger", "weather", "Weather hazard"),
    ("notice_status", "draft", "Draft"),
    ("notice_status", "in_review", "In review"),
    ("notice_status", "sent", "Sent"),
    ("notice_status", "scheduled", "Scheduled"),
    ("notice_trigger", "plan", "Released works plan"),
    ("exclusion_reason", "no_chosen_channel", "Not reachable on the channels chosen"),
    ("bundle_check", "school_zone.ok", "Clear"), ("bundle_check", "school_zone.action", "Restricted hours"),
    ("bundle_check", "road_occupancy.ok", "Clear"), ("bundle_check", "road_occupancy.action", "Permit needed"),
    ("bundle_check", "crew_hours.ok", "Clear"), ("bundle_check", "crew_hours.action", "Over capacity"),
    ("bundle_check", "outage_duration.ok", "Clear"), ("bundle_check", "outage_duration.action", "Over 4 hours"),
    ("bundle_check", "switching_crew.ok", "Standard crew"), ("bundle_check", "switching_crew.action", "Specialist crew"),
    ("plan_item_status", "upcoming", "Upcoming"), ("plan_item_status", "rostered", "On the roster"),
    ("plan_item_status", "in_field", "In the field"), ("plan_item_status", "complete", "Complete"),
    ("plan_cost", "labour", "Labour"), ("plan_cost", "materials", "Materials"), ("plan_cost", "plant", "Plant and equipment"),
    ("plan_cost", "traffic", "Traffic control"), ("plan_cost", "switching", "Switching and outage management"),
    ("plan_cost", "customer", "Customer notices"), ("plan_cost", "contingency", "Contingency"),
    ("channel", "sms", "SMS"),
    ("channel", "email", "Email"),
    ("channel", "app", "App or web"),
    ("channel", "mail", "Mail"),
    ("channel", "contact_centre", "Contact centre call"),
    ("exclusion_reason", "outside_isolation", "Not behind the isolation point"),
    ("exclusion_reason", "duplicate_24h", "Messaged about these feeders in the last 24 hours"),
    ("notice_rule", "notice_period", "Four business days' notice before a planned interruption"),
    ("notice_rule", "isolation_scope", "Only customers the isolation affects"),
    ("notice_rule", "duplicate_24h", "No second message about the same feeders within 24 hours"),
    ("notice_rule", "safety_messages", "Fault and severe weather messages are never held back"),
    ("notice_rule", "life_support_first", "Life support customers first, with a contact centre call"),
    ("notice_rule", "quiet_hours", "No SMS or contact centre calls from 8 pm to 8 am"),
    ("notice_rule", "channel_preference", "Preferred channel on record, else another chosen digital channel, else mail"),
    ("connection_stage", "apply", "Apply"),
    ("connection_stage", "assess", "Assess"),
    ("connection_stage", "offer", "Offer"),
    ("connection_stage", "design", "Design"),
    ("connection_stage", "build", "Build"),
    ("connection_stage", "commission", "Commission"),
    ("connection_stage", "connected", "Connected"),
    ("connection_type", "residential", "New home connection"),
    ("connection_type", "solar_battery", "Solar and battery"),
    ("connection_type", "ev_charging", "EV charger"),
    ("connection_type", "commercial", "Commercial connection"),
    ("connection_type", "subdivision", "Subdivision"),
    ("connection_payment", "not_due", "Not due yet"),
    ("connection_payment", "quoted", "Quoted"),
    ("connection_payment", "invoiced", "Invoiced"),
    ("connection_payment", "paid", "Paid"),
    ("connection_payment", "no_charge", "No charge"),
    ("connection_blocker", "transformer", "Transformer upgrade needed before connection"),
    ("connection_blocker", "council", "Council approval pending"),
    ("connection_blocker", "quote_unaccepted", "Quote not yet accepted"),
    ("connection_blocker", "asp_missing", "No accredited service provider appointed"),
    ("connection_blocker", "easement", "Easement or land owner consent needed"),
    ("connection_blocker", "customer_design", "Waiting for the customer to confirm the design"),
    ("connection_blocker", "invoice_unpaid", "Invoice not yet paid"),
    ("connection_blocker", "outage_needed", "Planned outage needed for the tie-in"),
    ("connection_blocker", "materials", "Materials on back order"),
    ("connection_blocker", "meter_not_booked", "Meter installation not booked"),
    ("connection_blocker", "docs_incomplete", "Installer documents incomplete"),
    ("applicant_kind", "individual", "Person"),
    ("applicant_kind", "company", "Company"),
    ("life_support_status", "confirmed", "Registered"),
    ("life_support_status", "pending", "Registration pending"),
    ("outage_stage", "PROTECTION_TRIP", "Protection tripped"), ("outage_stage", "FUSE_OPERATED", "Fuse operated"),
    ("outage_stage", "FLISR_RESTORE", "Automatic restoration (FLISR)"), ("outage_stage", "FLISR_RESTORE_UPSTREAM", "Automatic restoration upstream (FLISR)"),
    ("outage_stage", "BACKFEED_VIA_TIE", "Back-fed through a tie"), ("outage_stage", "PATROL_STARTED", "Crew patrolling"),
    ("outage_stage", "FAULT_LOCATED", "Fault located"), ("outage_stage", "MADE_SAFE", "Made safe"),
    ("outage_stage", "CUSTOMERS_CONTACTED", "Customers contacted"), ("outage_stage", "GENERATOR_CONNECTED", "Mobile generator connected"),
    ("outage_stage", "PLANT_BOOKED", "Plant, materials and crew booked"), ("outage_stage", "REPAIR_COMPLETE", "Repair complete, supply restored"),
    ("fault_kind", "span", "Overhead span"), ("fault_kind", "cable", "Underground cable"), ("fault_kind", "pole", "Pole"),
    ("fault_kind", "transformer", "Distribution transformer"),
    ("finance_line", "network_fixed", "Network charges, daily fixed"),
    ("finance_line", "network_usage", "Network charges, usage"),
    ("finance_line", "metering", "Metering services"),
    ("finance_line", "reliability_incentive", "Reliability incentive (STPIS)"),
    ("finance_line", "preventive_maintenance", "Inspection and preventive maintenance"),
    ("finance_line", "corrective_maintenance", "Corrective maintenance (work orders)"),
    ("finance_line", "emergency_response", "Emergency response to outages"),
    ("finance_line", "vegetation", "Vegetation management"),
    ("finance_line", "network_operations", "Network operations"),
    ("finance_line", "customer_metering_ops", "Customer service and metering"),
    ("finance_line", "corporate_overhead", "Corporate overheads"),
    ("finance_line", "depreciation", "Depreciation"),
    ("finance_line", "finance_cost", "Finance costs"),
    ("finance_group", "revenue", "Revenue"),
    ("finance_group", "opex", "Operating costs"),
    ("finance_group", "depreciation", "Depreciation"),
    ("finance_group", "finance_cost", "Finance costs"),
]


@dp.materialized_view(comment="Display labels for every code used in the twin (causes, asset classes, areas, defects, field work, statuses).")
def ref_label():
    return spark.createDataFrame(LABELS, "domain string, code string, label string")


@dp.materialized_view(comment="Downstream impact of every piece of equipment: customers, life support customers, critical sites and "
                              "distribution substations it supplies. Walks the recorded energised path upwards from each NMI to the feeder "
                              "breaker, then the supply above it: the zone substation, the transmission substation and its transformers and "
                              "132 kV lines, and the bulk supply point. A zone or site fed by two circuits counts once.")
def gld_customer_impact():
    m = spark.read.table("slv_feeder_membership").select("equipment_id", "parent_equipment", "cim_class", "feeder_id").collect()
    parent = {r.equipment_id: r.parent_equipment for r in m}
    cls = {r.equipment_id: (r.cim_class, r.feeder_id) for r in m}
    n = spark.read.table("slv_nmi").select("nmi", "ds_id", "life_support", "critical_site", "feeder_id").collect()
    per_ds = defaultdict(lambda: [0, 0, 0])
    for r in n:
        if r.ds_id:
            v = per_ds[r.ds_id]
            v[0] += 1
            v[1] += 1 if r.life_support else 0
            v[2] += 1 if r.critical_site else 0
    tot = defaultdict(lambda: [0, 0, 0, 0])
    for ds, (c, ls, cr) in per_ds.items():
        e, hops = ds, 0
        while e and hops < 400:
            t = tot[e]
            t[0] += c
            t[1] += ls
            t[2] += cr
            t[3] += 1
            e = parent.get(e)
            hops += 1
    rows = [(e, cls.get(e, (None, None))[0], cls.get(e, (None, None))[1], v[0], v[1], v[2], v[3]) for e, v in tot.items()]
    # the supply above the feeders: per zone substation, the customers and distribution substations of its feeders
    zone = defaultdict(lambda: [0, 0, 0, set()])
    for r in n:
        if not r.feeder_id:
            continue
        z = zone[f"ZS-{r.feeder_id[:3]}"]
        z[0] += 1
        z[1] += 1 if r.life_support else 0
        z[2] += 1 if r.critical_site else 0
        z[3].add(r.ds_id)
    zone_ts = defaultdict(set)   # transmission substation -> zone substations it feeds on any circuit
    for r in spark.read.table("slv_subtransmission").select("ts_id", "zs_id").distinct().collect():
        zone_ts[r.ts_id].add(r.zs_id)
    site = {r.ts_id: (r.bsp_id, r.transformers) for r in spark.read.table("slv_transmission_substation").select("ts_id", "bsp_id", "transformers").collect()}
    lines = {r.line_id: r.ts_id for r in spark.read.table("slv_transmission_line").select("line_id", "ts_id").collect()}

    def sums(zones):
        t = [0, 0, 0, set()]
        for z in zones:
            if z in zone:
                t[0] += zone[z][0]
                t[1] += zone[z][1]
                t[2] += zone[z][2]
                t[3] |= zone[z][3]
        return [t[0], t[1], t[2], len(t[3])]

    def add(eid, zones):
        rows.append((eid, None, None, *sums(zones)))
    for z in zone:
        add(z, [z])
    bsp_zones = defaultdict(set)
    for ts, zones in zone_ts.items():
        add(ts, zones)
        for i in range(1, site[ts][1] + 1):   # each transformer carries the site (they run in parallel)
            add(f"{ts}-TX{i}", zones)
        bsp_zones[site[ts][0]] |= zones
    for line, ts in lines.items():
        add(line, zone_ts.get(ts, set()))
    for bsp, zones in bsp_zones.items():
        add(bsp, zones)
    return spark.createDataFrame(rows, "equipment_id string, cim_class string, feeder_id string, customers_downstream long, "
                                       "life_support_downstream long, critical_sites_downstream long, distribution_substations_downstream long")


@dp.materialized_view(comment="The distribution substations behind every breaker, recloser, switch and fuse: opening the device takes them "
                              "(and their customers) off supply. Walks the recorded energised path upwards from each distribution substation, "
                              "as gld_customer_impact does. A planned outage's notice reaches the customers of these substations.")
def gld_isolation_ds():
    m = spark.read.table("slv_feeder_membership").select("equipment_id", "parent_equipment", "cim_class", "feeder_id").collect()
    parent = {r.equipment_id: r.parent_equipment for r in m}
    cls = {r.equipment_id: r.cim_class for r in m}
    devices = {"Breaker", "Recloser", "LoadBreakSwitch", "Disconnector", "Fuse"}
    rows = []
    for ds in {r.equipment_id for r in m if r.cim_class == "PowerTransformer"}:
        e, hops = parent.get(ds), 1
        while e and hops < 400:
            if cls.get(e) in devices:
                rows.append((e, ds, hops))
            e = parent.get(e)
            hops += 1
    df = spark.createDataFrame(rows, "device_id string, ds_id string, hops int")
    notes = {"device_id": "Breaker, recloser, switch or fuse (asset id): the isolation point.",
             "ds_id": "A distribution substation behind the device (asset id); its customers lose supply when the device is opened.",
             "hops": "Steps along the energised path from the distribution substation up to the device (1 is the nearest)."}
    return df.select([F.col(c).alias(c, metadata={"comment": t}) for c, t in notes.items()])


@dp.materialized_view(comment="Every asset with its keys in each system, location, age, open defects and downstream customers. The "
                              "search index of the Asset Explorer.")
def gld_asset():
    return spark.sql(f"""
      WITH d AS (SELECT asset_id, count(*) FILTER (WHERE is_open) open_defects, min(priority) FILTER (WHERE is_open) worst_open_priority
                 FROM slv_defect GROUP BY asset_id)
      SELECT a.asset_id, a.asset_class, coalesce(l.label, a.asset_class) asset_class_label, a.name, coalesce(a.feeder_id, a.ncm_feeder) feeder_id,
             a.zs_id, substring(a.zs_id, 4, 3) zone_code, a.lat, a.lon, a.geom_wkt, a.install_year,
             year(date'{AS_OF}') - a.install_year AS age_years, a.material, a.rating, a.rating_unit, a.manufacturer,
             a.sap_equnr, a.sap_object_type, a.gis_fid, a.asset_id cim_mrid, a.protective_device, a.sectionalising_device,
             coalesce(d.open_defects, 0) open_defects, d.worst_open_priority,
             coalesce(i.customers_downstream, 0) customers_downstream, coalesce(i.life_support_downstream, 0) life_support_downstream,
             coalesce(i.critical_sites_downstream, 0) critical_sites_downstream
      FROM slv_asset a
      LEFT JOIN ref_label l ON l.domain = 'asset_class' AND l.code = a.asset_class
      LEFT JOIN d ON d.asset_id = a.asset_id
      LEFT JOIN gld_customer_impact i ON i.equipment_id = a.asset_id""")


@dp.materialized_view(comment="Outages with labels, durations, area and zone substation. Momentary interruptions (under 1 minute, "
                              "auto-reclose) are kept but excluded from SAIDI and SAIFI.")
def gld_outage():
    return spark.sql("""
      SELECT o.*, coalesce(c.label, o.cause_code) cause_label, coalesce(d.label, o.device_type) device_label,
             substring(o.feeder_id, 1, 3) zone_code, z.area, coalesce(a.label, z.area) area_label,
             CASE WHEN o.momentary THEN 0 ELSE o.customers_interrupted END sustained_customers,
             size(filter(o.stages, s -> s.action = 'FLISR_RESTORE_UPSTREAM')) > 0 AS flisr_restored,
             aggregate(filter(o.stages, s -> s.action = 'FLISR_RESTORE_UPSTREAM'), 0L, (acc, s) -> acc + s.customers_restored) customers_restored_by_flisr
      FROM slv_outage o
      LEFT JOIN ref_label c ON c.domain = 'cause' AND c.code = o.cause_code
      LEFT JOIN ref_label d ON d.domain = 'device_type' AND d.code = o.device_type
      LEFT JOIN (SELECT DISTINCT zs_id, material area FROM slv_asset WHERE asset_class = 'zone_substation') z ON z.zs_id = o.zs_id
      LEFT JOIN ref_label a ON a.domain = 'area' AND a.code = z.area""")


@dp.materialized_view(comment="Outages in progress at the as-of moment (the ADMS active incidents): the app's starting operational state. Each "
                              "row has its device, fault, cause, customers still off and on life support, the hours off and the hours to the "
                              "estimated restoration measured from the as-of moment (the app anchors them to now), the repair job, the work so far "
                              "and the patrol crew's report.")
def gld_open_outage():
    return _commented(spark.sql("""
      WITH crit AS (SELECT protective_device device_id, count(critical_site) critical_sites FROM slv_nmi GROUP BY ALL)
      SELECT o.outage_id, o.feeder_id, f.feeder_name, o.zs_id, o.device_id, o.device_type, coalesce(d.label, o.device_type) device_label,
             o.span_id, o.fault_asset_id, o.fault_kind, coalesce(k.label, o.fault_kind) fault_kind_label, o.lat, o.lon, o.cause_code,
             coalesce(c.label, o.cause_code) cause_label, o.snapshot_time, o.start_time, o.etr,
             round((unix_timestamp(o.snapshot_time) - unix_timestamp(o.start_time)) / 3600.0, 3) hours_off,
             round((unix_timestamp(o.etr) - unix_timestamp(o.snapshot_time)) / 3600.0, 3) etr_hours,
             o.customers_interrupted, o.customers_off, o.life_support_affected, o.life_support_off, coalesce(cr.critical_sites, 0) critical_sites,
             o.repair_job_type, o.repair_minutes, o.access_status, to_json(o.hazards) hazards, to_json(o.materials) materials,
             to_json(transform(o.stages, s -> named_struct('offset_min', round((unix_timestamp(to_timestamp(s.ts)) - unix_timestamp(o.start_time)) / 60.0, 1),
                     'action', s.action, 'device', s.device, 'customers_restored', s.customers_restored, 'note', s.note))) stages,
             o.crew_id, o.crew_note
      FROM slv_open_outage o
      LEFT JOIN gld_feeder_state f ON f.feeder_id = o.feeder_id
      LEFT JOIN ref_label c ON c.domain = 'cause' AND c.code = o.cause_code
      LEFT JOIN ref_label d ON d.domain = 'device_type' AND d.code = o.device_type
      LEFT JOIN ref_label k ON k.domain = 'fault_kind' AND k.code = o.fault_kind
      LEFT JOIN crit cr ON cr.device_id = o.device_id"""), {
        "outage_id": "ADMS outage id.", "feeder_id": "Feeder the outage is on.", "feeder_name": "Feeder name.",
        "zs_id": "Zone substation the feeder comes from.", "device_id": "The protective device that operated (the fuse on the spur).",
        "device_type": "Device type code (label in device_label).", "device_label": "Device type, for display.",
        "span_id": "Overhead span or cable section the fault is on.", "fault_asset_id": "The asset at fault (span, cable section, pole or "
        "distribution substation).", "fault_kind": "What failed, code (label in fault_kind_label).", "fault_kind_label": "What failed, for display.",
        "lat": "Fault latitude (WGS84).", "lon": "Fault longitude (WGS84).", "cause_code": "Cause code (label in cause_label).",
        "cause_label": "Cause, for display.", "snapshot_time": "When the active incidents extract was taken (the as-of moment).",
        "start_time": "When supply was lost, in the as-of frame.", "etr": "Estimated restoration time, in the as-of frame.",
        "hours_off": "Hours the customers had been off supply at the snapshot (the app keeps this age, anchored to now).",
        "etr_hours": "Hours from the snapshot to the estimated restoration.", "customers_interrupted": "Customers who lost supply.",
        "customers_off": "Customers still off supply (a mobile generator may be carrying some).",
        "life_support_affected": "Life support customers who lost supply.", "life_support_off": "Life support customers still off supply.",
        "critical_sites": "Critical sites (hospitals, aged care and the like) behind the device.",
        "repair_job_type": "Job type of the repair the field still has to do.", "repair_minutes": "Expected repair time on site, minutes.",
        "access_status": "Site access the patrol crew reported (clear, restricted, blocked).",
        "hazards": "Hazards the patrol crew reported (JSON array of codes).", "materials": "Materials used so far (JSON array of code and quantity).",
        "stages": "The work so far as JSON: minutes after the start, action code, device, customers restored and the note for each step.",
        "crew_id": "The line crew that patrolled and made it safe (null when no crew has reported yet).",
        "crew_note": "What the patrol crew reported and what the repair needs."})


@dp.materialized_view(comment="Daily reliability as the AER defines it: customer interruptions (sustained, over 3 minutes), customer "
                              "minutes off supply, daily SAIDI and SAIFI, and the major event day threshold by the 2.5-beta method "
                              "(log-normal on daily unplanned SAIDI over the history).")
def gld_reliability_daily():
    return spark.sql("""
      WITH n AS (SELECT count(*) customers FROM slv_nmi),
      days AS (SELECT explode(sequence((SELECT min(outage_date) FROM slv_outage), (SELECT max(outage_date) FROM slv_outage))) d),
      o AS (SELECT outage_date d, sum(customers_interrupted) ci, sum(customer_minutes) cmos, count(*) outages
            FROM slv_outage WHERE NOT momentary AND NOT planned AND duration_min > 3 GROUP BY outage_date),
      daily AS (SELECT days.d AS day, coalesce(o.ci, 0) ci, coalesce(o.cmos, 0) cmos, coalesce(o.outages, 0) outages,
                       coalesce(o.cmos, 0) / n.customers saidi, coalesce(o.ci, 0) / n.customers saifi FROM days LEFT JOIN o ON o.d = days.d CROSS JOIN n),
      beta AS (SELECT avg(ln(saidi)) alpha, stddev(ln(saidi)) b FROM daily WHERE saidi > 0)
      SELECT daily.*, exp(beta.alpha + 2.5 * beta.b) med_threshold_min, saidi > exp(beta.alpha + 2.5 * beta.b) is_major_event_day,
             CASE WHEN month(day) >= 7 THEN year(day) + 1 ELSE year(day) END regulatory_year
      FROM daily CROSS JOIN beta""")


@dp.materialized_view(comment="Reliability by regulatory year (July to June): SAIDI and SAIFI with and without major event days, "
                              "against the year's targets.")
def gld_reliability_year():
    return spark.sql("""
      SELECT regulatory_year, count(*) days_in_data, sum(saidi) saidi_all, sum(CASE WHEN NOT is_major_event_day THEN saidi END) saidi_ex_med,
             sum(saifi) saifi_all, sum(CASE WHEN NOT is_major_event_day THEN saifi END) saifi_ex_med,
             sum(CAST(is_major_event_day AS INT)) major_event_days, 92.0 saidi_target_ex_med, 0.92 saifi_target_ex_med,
             92.0 * count(*) / 365.0 saidi_target_to_date, 0.92 * count(*) / 365.0 saifi_target_to_date
      FROM gld_reliability_daily GROUP BY regulatory_year""")


@dp.materialized_view(comment="Zone substation loading history: daily peak MW per zone substation transformer and per zone, from the "
                              "historian archive (input to the load forecast).")
def gld_zone_load_daily():
    return spark.sql("""
      SELECT element transformer_id, substring(element, 1, 6) zs_id, to_date(timestamp) day, max(value) peak_mw, avg(value) avg_mw
      FROM `{c}`.`{s}`.share_historian_archive WHERE attribute = 'MW' AND element LIKE 'ZS-%'
      GROUP BY ALL""".format(c=spark.conf.get("atlas.catalog"), s=spark.conf.get("atlas.schema")))


@dp.materialized_view(comment="Zone substation transformer condition: latest and 30-day peak loading against nameplate and cyclic "
                              "rating, top-oil temperature, dissolved hydrogen trend (DGA) and a status colour.")
def gld_zone_transformer_state():
    c, s = spark.conf.get("atlas.catalog"), spark.conf.get("atlas.schema")
    return spark.sql(f"""
      WITH h AS (SELECT element, attribute, timestamp, value FROM `{c}`.`{s}`.share_historian_archive
                 WHERE element LIKE 'ZS-%-TX%' AND timestamp >= date'{AS_OF}' - INTERVAL 90 DAYS),
      last AS (SELECT element, attribute, max_by(value, timestamp) v FROM h GROUP BY ALL),
      p30 AS (SELECT element, max(value) FILTER (WHERE attribute = 'MW' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS) peak_mw_30d,
                     max(value) FILTER (WHERE attribute = 'TOP_OIL_TEMP' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS) peak_top_oil_30d,
                     avg(value) FILTER (WHERE attribute = 'H2_PPM' AND timestamp >= date'{AS_OF}' - INTERVAL 7 DAYS) h2_now,
                     avg(value) FILTER (WHERE attribute = 'H2_PPM' AND timestamp < date'{AS_OF}' - INTERVAL 83 DAYS) h2_90d_ago
              FROM h GROUP BY element)
      SELECT a.transformer_id, substring(a.transformer_id, 1, 6) zs_id, a.name, a.rating_mva, a.manufacturer, a.year_built, e.EQUNR sap_equnr,
             l.mw_now, l.top_oil_now, p.peak_mw_30d, p.peak_top_oil_30d, round(p.h2_now) h2_now, round(p.h2_90d_ago) h2_90d_ago,
             round(p.h2_now - p.h2_90d_ago) h2_rise_90d, round(p.peak_mw_30d / a.rating_mva, 2) peak_utilisation_30d,
             CASE WHEN p.h2_now - p.h2_90d_ago > 150 OR p.peak_mw_30d / a.rating_mva > 1.0 THEN 'red'
                  WHEN p.h2_now - p.h2_90d_ago > 50 OR p.peak_mw_30d / a.rating_mva > 0.85 THEN 'amber' ELSE 'green' END status
      FROM gld_ref_zs_transformer a
      LEFT JOIN (SELECT element, max(CASE WHEN attribute = 'MW' THEN v END) mw_now, max(CASE WHEN attribute = 'TOP_OIL_TEMP' THEN v END) top_oil_now
                 FROM last GROUP BY element) l ON l.element = a.transformer_id
      LEFT JOIN p30 p ON p.element = a.transformer_id
      LEFT JOIN slv_sap_equi e ON e.INVNR = a.transformer_id""")


@dp.materialized_view(comment="Zone substation transformer nameplate data (from the asset register extract).")
def gld_ref_zs_transformer():
    return spark.sql("""
      SELECT INVNR transformer_id, EQKTX name, CASE WHEN INVNR RLIKE 'ZS-(LPH|KNV|GVL)' THEN 15.0 ELSE 33.0 END rating_mva, HERST manufacturer,
             CAST(BAUJJ AS INT) year_built
      FROM slv_sap_equi WHERE EQART = 'ZTX'""")


@dp.materialized_view(comment="Feeder state: customers, life support, length, recent load, its rating (what limits the trunk: conductor "
                              "or breaker), emergency rating and spare capacity, 12-month reliability, open defects, vegetation exposure, "
                              "bushfire share and a status colour.")
def gld_feeder_state():
    c, s = spark.conf.get("atlas.catalog"), spark.conf.get("atlas.schema")
    return spark.sql(f"""
      WITH cust AS (SELECT feeder_id, count(*) customers, sum(CAST(life_support AS INT)) life_support, count(critical_site) critical_sites,
                           sum(solar_kw) solar_kw FROM slv_nmi GROUP BY feeder_id),
      len AS (SELECT feeder_id, sum(CASE WHEN asset_class = 'overhead_span' THEN rating END) / 1000 overhead_km,
                     sum(CASE WHEN asset_class = 'cable_section' THEN rating END) / 1000 underground_km,
                     count(*) FILTER (WHERE asset_class = 'pole') poles, count(*) FILTER (WHERE asset_class = 'distribution_substation') dist_substations,
                     sum(open_defects) open_defects FROM gld_asset GROUP BY feeder_id),
      rel AS (SELECT feeder_id, count(*) FILTER (WHERE NOT momentary) sustained_outages_12m, count(*) FILTER (WHERE momentary) momentary_12m,
                     sum(customer_minutes) cmos_12m FROM slv_outage WHERE start_time >= date'{AS_OF}' - INTERVAL 365 DAYS GROUP BY feeder_id),
      veg AS (SELECT feeder_id, count(*) FILTER (WHERE encroachment_category = 'A') veg_cat_a, count(*) FILTER (WHERE encroachment_category = 'B') veg_cat_b,
                     avg(CAST(bushfire_prone AS INT)) bushfire_share FROM slv_lidar_span WHERE campaign_year = year(date'{AS_OF}') GROUP BY feeder_id),
      load AS (SELECT element feeder_id, max_by(value, timestamp) amps_now, max(value) amps_peak_30d FROM `{c}`.`{s}`.share_historian_archive
               WHERE attribute = 'AMPS' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS GROUP BY element),
      cap AS (SELECT feeder_id, min(rating_amps) FILTER (WHERE circuit_role = 'trunk') trunk_amps,
                     min_by(material, rating_amps) FILTER (WHERE circuit_role = 'trunk') trunk_conductor,
                     min(rating_amps) FILTER (WHERE asset_class = 'cb') cb_amps
              FROM slv_asset WHERE asset_class IN ('overhead_span', 'cable_section', 'cb') GROUP BY feeder_id)
      SELECT f.feeder_id, concat(regexp_replace(z.name, ' zone substation', ''), ' feeder ', CAST(CAST(substring(f.feeder_id, 6, 2) AS INT) AS STRING)) feeder_name,
             concat('ZS-', substring(f.feeder_id, 1, 3)) zs_id, z.name zs_name, z.material area, coalesce(al.label, z.material) area_label,
             cust.customers, cust.life_support, cust.critical_sites, round(cust.solar_kw / 1000, 2) solar_mw,
             round(len.overhead_km, 1) overhead_km, round(len.underground_km, 1) underground_km, len.poles, len.dist_substations, len.open_defects,
             coalesce(rel.sustained_outages_12m, 0) sustained_outages_12m, coalesce(rel.momentary_12m, 0) momentary_12m,
             round(coalesce(rel.cmos_12m, 0)) cmos_12m, coalesce(veg.veg_cat_a, 0) veg_cat_a, coalesce(veg.veg_cat_b, 0) veg_cat_b,
             round(coalesce(veg.bushfire_share, 0), 2) bushfire_share, round(load.amps_now) amps_now, round(load.amps_peak_30d) amps_peak_30d,
             least(cap.trunk_amps, coalesce(cap.cb_amps, 630)) rating_amps, round(least(cap.trunk_amps, coalesce(cap.cb_amps, 630)) * 1.1) emergency_amps,
             CASE WHEN cap.trunk_amps <= coalesce(cap.cb_amps, 630) THEN concat('Trunk conductor (', cap.trunk_conductor, ')') ELSE 'Feeder circuit breaker' END limited_by,
             round(least(cap.trunk_amps, coalesce(cap.cb_amps, 630)) - load.amps_peak_30d) spare_amps,
             round(load.amps_peak_30d / least(cap.trunk_amps, coalesce(cap.cb_amps, 630)), 2) peak_utilisation_30d,
             CASE WHEN coalesce(veg.veg_cat_a, 0) >= 23 OR coalesce(rel.sustained_outages_12m, 0) >= 32 THEN 'red'
                  WHEN coalesce(veg.veg_cat_a, 0) >= 14 OR coalesce(rel.sustained_outages_12m, 0) >= 10 OR len.open_defects >= 14 THEN 'amber'
                  ELSE 'green' END status
      FROM (SELECT DISTINCT feeder_id FROM slv_feeder_membership) f
      JOIN (SELECT asset_id zs_id, name, material FROM slv_asset WHERE asset_class = 'zone_substation') z ON z.zs_id = concat('ZS-', substring(f.feeder_id, 1, 3))
      LEFT JOIN ref_label al ON al.domain = 'area' AND al.code = z.material
      LEFT JOIN cust USING (feeder_id) LEFT JOIN len USING (feeder_id) LEFT JOIN rel USING (feeder_id) LEFT JOIN veg USING (feeder_id)
      LEFT JOIN cap USING (feeder_id)
      LEFT JOIN load USING (feeder_id)""")


@dp.materialized_view(comment="Distribution substation state: rating, customers, rooftop solar and EV load, estimated peak "
                              "utilisation and midday reverse-flow risk.")
def gld_ds_state():
    return spark.sql("""
      WITH c AS (SELECT ds_id, count(*) customers, sum(solar_kw) solar_kw, sum(has_ev_charger) ev_chargers, sum(CAST(life_support AS INT)) life_support
                 FROM slv_nmi GROUP BY ds_id)
      SELECT a.asset_id ds_id, a.feeder_id, a.zs_id, a.lat, a.lon, a.material ds_type, a.rating kva, c.customers, c.life_support,
             round(c.solar_kw, 1) solar_kw, c.ev_chargers,
             round((c.customers * 2.1 + c.ev_chargers * 3.5) / a.rating, 2) est_peak_utilisation,
             round((c.solar_kw * 0.7 - c.customers * 0.4) / a.rating, 2) est_midday_reverse_flow,
             CASE WHEN (c.customers * 2.1 + c.ev_chargers * 3.5) / a.rating > 1.1 OR (c.solar_kw * 0.7 - c.customers * 0.4) / a.rating > 0.8 THEN 'red'
                  WHEN (c.customers * 2.1 + c.ev_chargers * 3.5) / a.rating > 0.9 OR (c.solar_kw * 0.7 - c.customers * 0.4) / a.rating > 0.5 THEN 'amber'
                  ELSE 'green' END status
      FROM gld_asset a JOIN c ON c.ds_id = a.asset_id WHERE a.asset_class = 'distribution_substation'""")


@dp.materialized_view(comment="Vegetation risk per overhead span from the latest LiDAR campaign: clearance now, predicted clearance in "
                              "12 months from the measured grow-in, encroachment category, bushfire-prone land, hazard trees, and the "
                              "customers behind the span.")
def gld_vegetation_risk():
    return spark.sql(f"""
      SELECT v.span_id, v.feeder_id, concat('ZS-', substring(v.feeder_id, 1, 3)) zs_id, v.capture_date, v.clearance_m,
             round(v.clearance_m - v.growth_m_per_year, 2) clearance_12m_m, v.encroachment_category, coalesce(l.label, v.encroachment_category) category_label,
             v.dominant_species, v.growth_m_per_year, v.hazard_tree, v.bushfire_prone, v.pole_lean_deg, a.lat, a.lon, a.geom_wkt,
             coalesce(i.customers_downstream, 0) customers_downstream,
             CASE WHEN v.clearance_m < 1.0 OR (v.clearance_m - v.growth_m_per_year < 0.5 AND v.bushfire_prone) THEN 'red'
                  WHEN v.clearance_m - v.growth_m_per_year < 1.5 THEN 'amber' ELSE 'green' END status,
             round((3.0 - least(v.clearance_m - v.growth_m_per_year, 3.0)) * (CASE WHEN v.bushfire_prone THEN 2 ELSE 1 END) *
                   (CASE WHEN v.hazard_tree THEN 1.5 ELSE 1 END) * ln(2 + coalesce(i.customers_downstream, 0)), 2) risk_score
      FROM slv_lidar_span v
      LEFT JOIN gld_asset a ON a.asset_id = v.span_id
      LEFT JOIN gld_customer_impact i ON i.equipment_id = v.span_id
      LEFT JOIN ref_label l ON l.domain = 'veg' AND l.code = v.encroachment_category
      WHERE v.campaign_year = year(date'{AS_OF}')""")


@dp.materialized_view(comment="Spans near live fires: for each real NSW RFS incident and DEA hotspot, every overhead span within 10 km "
                              "with its distance (computed with Databricks spatial SQL in GDA2020 MGA zone 56), distance band and the "
                              "customers behind it.")
def gld_fire_exposure():
    return spark.sql(f"""
      WITH fires AS (
        SELECT incident_id fire_id, 'RFS incident' source, title, alert_level, status, incident_type, size_ha,
               st_transform(coalesce(perimeter, geom), {METRIC_SRID}) g FROM slv_fire_incident WHERE geom IS NOT NULL
        UNION ALL
        SELECT concat('HS-', hotspot_id), 'Satellite hotspot', concat(satellite, ' hotspot'), 'Hotspot', NULL, 'Thermal anomaly', NULL,
               st_transform(geom, {METRIC_SRID}) FROM slv_hotspot),
      spans AS (SELECT asset_id span_id, feeder_id, zs_id, st_transform(st_geomfromwkt(geom_wkt, 4326), {METRIC_SRID}) g, customers_downstream
                FROM gld_asset WHERE asset_class = 'overhead_span')
      SELECT f.fire_id, f.source, f.title, f.alert_level, f.status, f.incident_type, f.size_ha, s.span_id, s.feeder_id, s.zs_id,
             round(st_distance(f.g, s.g)) distance_m,
             CASE WHEN st_distance(f.g, s.g) <= 1000 THEN '0-1 km' WHEN st_distance(f.g, s.g) <= 3000 THEN '1-3 km'
                  WHEN st_distance(f.g, s.g) <= 5000 THEN '3-5 km' ELSE '5-10 km' END band, s.customers_downstream
      FROM fires f JOIN spans s ON st_dwithin(f.g, s.g, 10000)""")


@dp.materialized_view(comment="Forecast exposure per feeder and 6-hour window for the next 72 hours: the strongest gust and heaviest rain "
                              "forecast over the feeder's spans, joined through H3 cells (resolution 5).")
def gld_feeder_weather_exposure():
    return spark.sql("""
      WITH cells AS (SELECT feeder_id, h3_longlatash3(lon, lat, 5) h3, count(*) spans FROM gld_asset
                     WHERE asset_class = 'overhead_span' AND lon IS NOT NULL GROUP BY ALL),
      fc AS (SELECT h3_r5 h3, date_trunc('HOUR', forecast_time) t, max(gust_kmh) gust, max(rain_mm) rain, max(temp_c) temp FROM slv_weather_forecast GROUP BY ALL),
      ring AS (SELECT c.feeder_id, c.spans, explode(h3_kring(c.h3, 1)) h3 FROM cells c)
      SELECT r.feeder_id, timestampadd(HOUR, 6 * floor(hour(fc.t) / 6), date_trunc('DAY', fc.t)) window_start,
             max(fc.gust) max_gust_kmh, max(fc.rain) max_rain_mm_h, sum(fc.rain) / count(DISTINCT r.h3) rain_mm_6h, max(fc.temp) max_temp_c
      FROM ring r JOIN fc ON fc.h3 = r.h3 GROUP BY ALL""")


@dp.materialized_view(comment="Records that disagree: meters whose voltage follows a different transformer than the one recorded "
                              "(correlation of voltage residuals, after removing the network-wide daily profile, against transformers within 800 m, found with a spatial join), and meters recorded more "
                              "than 400 m from their transformer.")
def gld_dq_meter_mismatch():
    return spark.sql("""
      WITH v0 AS (SELECT n.nmi, n.ds_id recorded_ds, m.interval_end, m.v_avg FROM slv_meter_voltage m JOIN slv_nmi n USING (nmi)),
      common AS (SELECT interval_end, avg(v_avg) v_common FROM v0 GROUP BY interval_end),
      v AS (SELECT v0.nmi, v0.recorded_ds, v0.interval_end, v0.v_avg - c.v_common v_avg FROM v0 JOIN common c USING (interval_end)),
      prof AS (SELECT recorded_ds ds_id, interval_end, avg(v_avg) v_ds FROM v GROUP BY ALL),
      nm AS (SELECT DISTINCT v.nmi, v.recorded_ds, n.lat, n.lon FROM v JOIN slv_nmi n USING (nmi)),
      cand AS (SELECT nm.nmi, nm.recorded_ds, d.ds_id cand_ds, st_distancesphere(st_point(nm.lon, nm.lat), st_point(d.lon, d.lat)) dist_m
               FROM nm JOIN gld_ds_state d ON st_dwithin(st_transform(st_point(nm.lon, nm.lat, 4326), 7856), st_transform(st_point(d.lon, d.lat, 4326), 7856), 800)
               UNION
               SELECT nm.nmi, nm.recorded_ds, d.ds_id, st_distancesphere(st_point(nm.lon, nm.lat), st_point(d.lon, d.lat))
               FROM nm JOIN gld_ds_state d ON d.ds_id = nm.recorded_ds),
      corr AS (SELECT c.nmi, c.recorded_ds, c.cand_ds, c.dist_m, corr(v.v_avg, p.v_ds) r FROM cand c
               JOIN v ON v.nmi = c.nmi JOIN prof p ON p.ds_id = c.cand_ds AND p.interval_end = v.interval_end GROUP BY ALL),
      best AS (SELECT nmi, recorded_ds, max_by(cand_ds, r) best_ds, max(r) best_r, max(CASE WHEN cand_ds = recorded_ds THEN r END) recorded_r,
                      max(CASE WHEN cand_ds = recorded_ds THEN dist_m END) recorded_dist_m FROM corr GROUP BY nmi, recorded_ds)
      SELECT nmi, recorded_ds, best_ds suggested_ds, round(best_r, 3) best_correlation, round(recorded_r, 3) recorded_correlation,
             round(recorded_dist_m) recorded_distance_m,
             CASE WHEN best_ds <> recorded_ds AND best_r - coalesce(recorded_r, 0) > 0.15 THEN 'Voltage follows another transformer'
                  WHEN recorded_dist_m > 1200 THEN 'Far from recorded transformer' END issue
      FROM best WHERE (best_ds <> recorded_ds AND best_r - coalesce(recorded_r, 0) > 0.15) OR recorded_dist_m > 1200""")


@dp.materialized_view(comment="Drone detections matched to the nearest pole within 40 m (spatial join), with the pole's open defects.")
def gld_drone_detection():
    return spark.sql("""
      WITH p AS (SELECT asset_id pole_id, feeder_id, st_transform(st_point(lon, lat, 4326), 7856) g, open_defects FROM gld_asset WHERE asset_class = 'pole'),
      d AS (SELECT *, st_transform(geom, 7856) g FROM slv_drone_detection)
      SELECT d.detection_id, d.file_name, d.captured_at, d.defect_class, d.score, d.lat, d.lon,
             min_by(p.pole_id, st_distance(d.g, p.g)) pole_id, min_by(p.feeder_id, st_distance(d.g, p.g)) feeder_id,
             round(min(st_distance(d.g, p.g)), 1) distance_m
      FROM d JOIN p ON st_dwithin(d.g, p.g, 40) GROUP BY ALL""")


@dp.materialized_view(comment="Open defects with the asset, location, feeder, priority, age and customers downstream.")
def gld_open_defect():
    return spark.sql("""
      SELECT d.notification_id, d.asset_id, a.asset_class_label, d.defect_code, coalesce(l.label, d.defect_code) defect_label, d.description,
             d.created_date, d.priority, d.age_days, a.feeder_id, a.zs_id, a.lat, a.lon, a.customers_downstream, a.material, a.age_years
      FROM slv_defect d JOIN gld_asset a USING (asset_id) LEFT JOIN ref_label l ON l.domain = 'defect' AND l.code = d.defect_code
      WHERE d.is_open""")


@dp.materialized_view(comment="Work orders with labels and the asset's location.")
def gld_work_order():
    return spark.sql("""
      SELECT w.*, coalesce(l.label, w.order_type) order_type_label, a.feeder_id, a.zs_id, a.lat, a.lon
      FROM slv_work_order w LEFT JOIN gld_asset a USING (asset_id) LEFT JOIN ref_label l ON l.domain = 'order_type' AND l.code = w.order_type""")


# ------------------------------------------------------------------ field resource management
def _lbl(domain):
    return spark.read.table("ref_label").where(F.col("domain") == domain).select(F.col("code"), F.col("label"))


def _cm(col, text):
    return F.col(col).alias(col.split(".")[-1], metadata={"comment": text})


@dp.materialized_view(comment="Field regions: each is a depot, with its area, the zone substations it serves, and its feeders, customers and crews.")
def gld_rm_region():
    d = spark.read.table("slv_depot")
    f = (spark.read.table("gld_feeder_state").select("feeder_id", "zs_id", "customers")
         .join(d.select("depot_id", F.explode("zone_substations").alias("zs_id")), "zs_id")
         .groupBy("depot_id").agg(F.count("*").alias("feeders"), F.sum("customers").alias("customers")))
    c = spark.read.table("slv_crew").groupBy(F.col("region_id").alias("depot_id")).agg(
        F.count("*").alias("crews"), F.sum(F.when(F.col("employment_type") == "Contractor", 1).otherwise(0)).alias("contractors"))
    a = _lbl("area").withColumnRenamed("code", "area").withColumnRenamed("label", "area_name")
    return (d.join(f, "depot_id", "left").join(c, "depot_id", "left").join(a, "area", "left")
            .select(F.col("depot_id").alias("region_id", metadata={"comment": "Region id (the depot code)."}),
                    F.col("region_name").alias("name", metadata={"comment": "Region name."}), _cm("area", "Area code."),
                    _cm("area_name", "Area name."), _cm("depot_id", "Depot."), _cm("depot_name", "Depot name."), _cm("lat", "Depot latitude."),
                    _cm("lon", "Depot longitude."), _cm("zone_substations", "Zone substations the region serves."),
                    _cm("feeders", "Feeders in the region."), _cm("customers", "Customers in the region."), _cm("crews", "Crews based in the region."),
                    _cm("contractors", "Contractor crews based in the region.")))


@dp.materialized_view(comment="Crews with region name, skills, vehicle, ticket compliance (expired and expiring within 45 days) and open "
                              "safety events.")
def gld_crew():
    c = spark.read.table("slv_crew")
    sk = spark.read.table("slv_crew_skill").join(_lbl("skill").withColumnRenamed("code", "skill_code"), "skill_code", "left").groupBy("crew_id").agg(
        F.array_sort(F.collect_list("skill_code")).alias("skills"), F.concat_ws(", ", F.array_sort(F.collect_list("label"))).alias("skills_label"))
    t = spark.read.table("slv_crew_ticket").groupBy("crew_id").agg(
        F.sum(F.when(F.col("status") == "expired", 1).otherwise(0)).alias("tickets_expired"),
        F.sum(F.when(F.col("status") == "expiring", 1).otherwise(0)).alias("tickets_expiring_45d"))
    e = spark.read.table("slv_safety_event").where("status = 'Open' AND crew_id IS NOT NULL").groupBy("crew_id").agg(F.count("*").alias("open_safety_events"))
    v = spark.read.table("slv_vehicle").where("crew_id IS NOT NULL").join(
        _lbl("vehicle_type").withColumnRenamed("code", "vehicle_type").withColumnRenamed("label", "vehicle_type_label"), "vehicle_type", "left"
    ).select("crew_id", "vehicle_id", "vehicle_type", "vehicle_type_label")
    r = spark.read.table("slv_depot").select(F.col("depot_id").alias("region_id"), "region_name")
    out = c.join(r, "region_id", "left").join(sk, "crew_id", "left").join(t, "crew_id", "left").join(e, "crew_id", "left").join(v, "crew_id", "left")
    return out.select(
        "crew_id", "crew_name", "persona", "primary_skill", "region_id", _cm("region_name", "Home region name."), "depot_id", "area",
        "employment_type", "hourly_cost_aud", "home_lat", "home_lon", "crew_size", "business_unit", "patch_feeder", "active",
        _cm("skills", "Skill codes held."), _cm("skills_label", "Skills held, in words."), _cm("vehicle_id", "Vehicle allocated."),
        _cm("vehicle_type", "Vehicle type code."), _cm("vehicle_type_label", "Vehicle type."),
        F.coalesce("tickets_expired", F.lit(0)).alias("tickets_expired", metadata={"comment": "Tickets already expired."}),
        F.coalesce("tickets_expiring_45d", F.lit(0)).alias("tickets_expiring_45d", metadata={"comment": "Tickets expiring within 45 days."}),
        F.coalesce("open_safety_events", F.lit(0)).alias("open_safety_events",
                                                          metadata={"comment": "Open safety events attributed to the crew (keeps it off the roster)."}))


@dp.materialized_view(comment="Every crew ticket and licence with its label, issue and expiry dates, days to expiry and status (expired, "
                              "expiring within 45 days, current), joined to the crew.")
def gld_certification():
    t = spark.read.table("slv_crew_ticket").join(_lbl("ticket").withColumnRenamed("code", "ticket_code").withColumnRenamed("label", "ticket_label"),
                                                 "ticket_code", "left")
    c = spark.read.table("gld_crew").select("crew_id", "crew_name", "persona", "region_id", "region_name", "employment_type")
    return t.join(c, "crew_id").select(
        "crew_id", "crew_name", "persona", "region_id", "region_name", "employment_type", "ticket_code",
        _cm("ticket_label", "Ticket or licence."), "holder_name", "issued_date", "expiry_date", "days_to_expiry", "is_expired", "status")


@dp.materialized_view(comment="Crew leave per day with its label, joined to the crew and region.")
def gld_crew_leave():
    return (spark.read.table("slv_crew_leave")
            .join(_lbl("leave_type").withColumnRenamed("code", "leave_type").withColumnRenamed("label", "leave_type_label"), "leave_type", "left")
            .join(spark.read.table("slv_crew").select("crew_id", "crew_name", "region_id"), "crew_id")
            .select("crew_id", "crew_name", "region_id", "leave_date", "leave_type", _cm("leave_type_label", "Leave type."), "status"))


@dp.materialized_view(comment="Crew training bookings per day, joined to the crew and region.")
def gld_crew_training():
    return (spark.read.table("slv_crew_training").join(spark.read.table("slv_crew").select("crew_id", "crew_name", "region_id"), "crew_id")
            .select("crew_id", "crew_name", "region_id", "training_date", "course_name", "duration_hours"))


@dp.materialized_view(comment="Safety events with their label and the crew and region involved; open events attributed to a crew keep it off "
                              "the roster unless a supervisor authorises it.")
def gld_safety_event():
    e = spark.read.table("slv_safety_event").join(
        _lbl("event_type").withColumnRenamed("code", "event_type").withColumnRenamed("label", "event_type_label"), "event_type", "left")
    c = spark.read.table("slv_crew").select("crew_id", "crew_name")
    return e.join(c, "crew_id", "left").select(
        "event_id", "event_date", "crew_id", _cm("crew_name", "Crew involved."), "depot_id", F.col("depot_id").alias("region_id", metadata={"comment": "Region (depot)."}),
        "event_type", _cm("event_type_label", "Event type."), "equipment_class", "severity", "status",
        (F.col("status") == "Open").alias("is_open", metadata={"comment": "Still open."}))


@dp.materialized_view(comment="Enterprise agreement working-time rules per skill, with the skill in words.")
def gld_eba_rule():
    return (spark.read.table("slv_eba_rule").join(_lbl("skill").withColumnRenamed("code", "role").withColumnRenamed("label", "role_label"), "role", "left")
            .select("role", _cm("role_label", "Skill the rule applies to."), "max_hours_per_day", "max_consecutive_days", "min_rest_hours",
                    "ot_multiplier", "max_travel_as_work_hours"))


@dp.materialized_view(comment="Planned field work with every code in words: job type, skill, ticket, funding and source, plus region and zone "
                              "substation. The work the field resource management rosters, from the work management system.")
def gld_field_job():
    j = spark.read.table("slv_field_job")
    for dom, col in (("job_type", "job_type"), ("skill", "required_skill"), ("ticket", "required_ticket"), ("funding", "funding"), ("source", "source")):
        j = j.join(_lbl(dom).withColumnRenamed("code", col).withColumnRenamed("label", f"{col}_label"), col, "left")
    r = spark.read.table("slv_depot").select(F.col("depot_id").alias("region_id"), "region_name", "area")
    z = spark.read.table("gld_feeder_state").select("feeder_id", "zs_id")
    return j.join(r, "region_id", "left").join(z, "feeder_id", "left").select(
        "job_id", "job_date", "job_type", _cm("job_type_label", "Job type."), "title", "priority", "duration_min", "window_start_min",
        "window_end_min", F.col("job_type").isin("CUSTOMER_CONNECTION", "FAULT_RESPONSE").alias(
            "is_fixed", metadata={"comment": "A booked appointment: the crew must start inside its window."}),
        "region_id", _cm("region_name", "Region name."), _cm("area", "Area code."), "depot_id", "feeder_id", _cm("zs_id", "Zone substation."),
        "asset_id", "nmi", "address", "suburb", "lat", "lon", "required_skill", _cm("required_skill_label", "Skill required."),
        "required_ticket", _cm("required_ticket_label", "Ticket required."), "funding", _cm("funding_label", "Who pays."), "customers",
        "est_customer_minutes", "source", _cm("source_label", "Where the work came from."), "notification_id")


@dp.materialized_view(comment="The network spine: red, amber and green counts at each stop from bulk supply to customers.")
def gld_spine_status():
    return spark.sql("""
      SELECT 1 ord, 'bulk_supply' stop, 'Bulk supply' stop_label, 0 red, 0 amber, count(*) green, count(*) total FROM slv_bulk_supply
      UNION ALL SELECT 2, 'transmission_substations', 'Transmission substations', count(*) FILTER (WHERE status = 'red'),
                       count(*) FILTER (WHERE status = 'amber'), count(*) FILTER (WHERE status = 'green'), count(*) FROM gld_transmission_substation
      UNION ALL SELECT 3, 'zone_substations', 'Zone substations', count(DISTINCT zs_id) FILTER (WHERE status = 'red'),
                       count(DISTINCT zs_id) FILTER (WHERE status = 'amber'),
                       (SELECT count(*) FROM slv_asset WHERE asset_class = 'zone_substation')
                         - count(DISTINCT zs_id) FILTER (WHERE status IN ('red', 'amber')),
                       (SELECT count(*) FROM slv_asset WHERE asset_class = 'zone_substation')
                FROM gld_zone_transformer_state
      UNION ALL SELECT 4, 'hv_feeders', 'HV feeders', count(*) FILTER (WHERE status = 'red'), count(*) FILTER (WHERE status = 'amber'),
                       count(*) FILTER (WHERE status = 'green'), count(*) FROM gld_feeder_state
      UNION ALL SELECT 5, 'distribution_lv', 'Distribution and LV', count(*) FILTER (WHERE status = 'red'), count(*) FILTER (WHERE status = 'amber'),
                       count(*) FILTER (WHERE status = 'green'), count(*) FROM gld_ds_state
      UNION ALL SELECT 6, 'customers_der', 'Customers and DER', 0, (SELECT count(*) FROM gld_dq_meter_mismatch),
                       (SELECT count(*) FROM slv_nmi) - (SELECT count(*) FROM gld_dq_meter_mismatch), (SELECT count(*) FROM slv_nmi)""")


# ------------------------------------------------------------------ who depends on what (the Impact analysis)
@dp.materialized_view(comment="How each zone substation is supplied: its subtransmission circuits, the transmission substations they come from and "
                              "the bulk supply points behind those. A zone on one circuit has no alternative supply (single-fed); a zone on one "
                              "transmission substation has no alternative site (single transmission); one on two sites survives the loss of either.")
def gld_zone_supply():
    return spark.sql("""
      SELECT z.asset_id zs_id, z.name zs_name, z.material area, count(l.line_id) supply_circuits,
             count(DISTINCT l.ts_id) transmission_substations, array_sort(collect_set(l.ts_id)) ts_ids,
             count(DISTINCT l.bsp_id) bulk_supply_points, array_sort(collect_set(l.bsp_id)) bsp_ids,
             array_sort(collect_list(l.line_id)) line_ids, count(l.line_id) < 2 single_fed, count(DISTINCT l.ts_id) < 2 single_transmission,
             round(sum(l.route_km), 1) circuit_km, sum(l.rating_mva) circuits_mva,
             CASE WHEN count(l.line_id) < 2 THEN 0 ELSE sum(l.rating_mva) - max(l.rating_mva) END circuits_firm_mva
      FROM (SELECT asset_id, name, material FROM slv_asset WHERE asset_class = 'zone_substation') z
      LEFT JOIN slv_subtransmission l ON l.zs_id = z.asset_id GROUP BY ALL""")


@dp.materialized_view(comment="Backup ties between feeders: each normally open point and the two feeders it can join, whether the other "
                              "feeder comes from a different zone substation, and how much it can carry: the other feeder's emergency rating less "
                              "its own peak, capped by the tie switch (630 A) and tie conductor (420 A), as amps and as a share of this feeder's peak.")
def gld_feeder_tie():
    return spark.sql("""
      WITH t AS (SELECT equipment_id nop, array_sort(collect_set(feeder_id)) fs FROM slv_feeder_membership
                 WHERE cim_class = 'LoadBreakSwitch' AND equipment_id RLIKE '-N[0-9]+$' GROUP BY equipment_id HAVING size(fs) = 2)
      , e AS (SELECT nop, f feeder_id, filter(fs, x -> x <> f)[0] tie_feeder FROM t LATERAL VIEW explode(fs) ex AS f)
      SELECT e.nop, e.feeder_id, e.tie_feeder, concat('ZS-', substring(e.feeder_id, 1, 3)) zs_id, concat('ZS-', substring(e.tie_feeder, 1, 3)) tie_zs_id,
             substring(e.feeder_id, 1, 3) <> substring(e.tie_feeder, 1, 3) other_zone,
             a.amps_peak_30d feeder_peak_amps, b.emergency_amps tie_emergency_amps, b.amps_peak_30d tie_peak_amps,
             greatest(0, least(b.emergency_amps - b.amps_peak_30d, 630, 420)) transfer_amps,
             round(least(1.0, greatest(0, least(b.emergency_amps - b.amps_peak_30d, 630, 420)) / greatest(a.amps_peak_30d, 1)), 2) transfer_share
      FROM e JOIN gld_feeder_state a ON a.feeder_id = e.feeder_id JOIN gld_feeder_state b ON b.feeder_id = e.tie_feeder""")


@dp.materialized_view(comment="Suburb dependency: customers in each suburb by the feeder and zone substation that supply them, with the "
                              "cohorts that matter in an outage (life support, critical sites, critical sites without a backup generator, "
                              "rooftop solar) and where the suburb sits.")
def gld_suburb_supply():
    return spark.sql("""
      SELECT n.suburb, n.postcode, f.area, f.area_label, n.zs_id, f.zs_name, n.feeder_id, count(*) customers,
             sum(CAST(n.life_support AS INT)) life_support, count(n.critical_site) critical_sites,
             count(CASE WHEN n.critical_site IS NOT NULL AND NOT coalesce(n.backup_generator, false) THEN 1 END) critical_no_generator,
             count(CASE WHEN n.solar_kw > 0 THEN 1 END) solar, round(avg(n.lat), 5) lat, round(avg(n.lon), 5) lon
      FROM slv_nmi n JOIN gld_feeder_state f USING (feeder_id) GROUP BY ALL""")


# ------------------------------------------------------------------ serving marts (synced into the app's Lakebase, schema serving)
def _commented(df, comments):
    """Column comments on a mart: the Lakebase copy and Genie show them."""
    return df.select([F.col(c).alias(c, metadata={"comment": comments[c]}) if c in comments else F.col(c) for c in df.columns])


@dp.materialized_view(comment="Every metering point (NMI) with its address, supply point (distribution substation, feeder, zone substation), "
                              "customer type, life support, critical site, DER, contact preferences and location. The app's customer index.")
def gld_nmi():
    return _commented(spark.read.table("slv_nmi").drop("geom"), {
        "nmi": "National metering identifier (10 digits).", "classification": "NMI classification code (market standing data).",
        "tariff": "Network tariff code.", "unit": "Flat or unit number, empty for a house.", "house_number": "Street number.",
        "street_name": "Street name, without its type.", "street_type": "Street type in words.", "suburb": "Suburb or locality.",
        "state": "State.", "postcode": "Postcode.", "address": "Full postal address on one line.",
        "ds_id": "Distribution substation supplying the premises.", "feeder_id": "Feeder supplying the premises.",
        "protective_device": "Protective device on the supply path.", "customer_type": "Customer type code: residential, small_business or large_business.",
        "customer_type_label": "Customer type, in words.", "elevation_m": "Ground elevation, metres above sea level.",
        "street_id": "Street segment the premises fronts in the GIS.", "life_support_status": "Life support registration status (empty when not registered).",
        "life_support_equipment": "Life support equipment registered.", "critical_site": "Critical site type (hospital, aged care...), when the NMI is one.",
        "site_name": "Critical site name.", "backup_generator": "The critical site has its own backup generator.",
        "generator_kva": "Backup generator rating, kVA.", "generator_fuel_hours": "Hours the backup generator can run on its fuel.",
        "solar_kw": "Rooftop solar capacity, kW.", "has_battery": "1 when a battery is registered.",
        "has_ev_charger": "1 when a home EV charger is registered.", "sms_opt_in": "Registered for outage notices by SMS.",
        "email_opt_in": "Registered for outage notices by email.", "facebook_opt_in": "Registered for outage notices by Facebook Messenger.",
        "preferred_channel": "Channel the customer prefers for notices, on record for every customer: Sms, Email, Facebook (the app) or Mail (a letter).", "life_support": "True when the premises is on the life support register.",
        "lon": "Service point longitude (WGS84).", "lat": "Service point latitude (WGS84).", "zs_id": "Zone substation supplying the premises."})


@dp.materialized_view(comment="Interval energy per NMI and register for the 3 days up to the as-of day. An interval delivered in two NEM12 files "
                              "is collapsed to one row.")
def gld_meter_interval_recent():
    return _commented(spark.sql(f"""
      SELECT nmi, interval_date, interval_end, register, max(kwh) kwh
      FROM slv_meter_interval WHERE interval_date >= date_sub(date'{AS_OF}', 2) AND interval_date <= date'{AS_OF}'
      GROUP BY ALL"""), {
        "nmi": "National metering identifier.", "interval_date": "Day of the interval.", "interval_end": "End of the 30-minute interval.",
        "register": "E1 (import) or B1 (export).", "kwh": "Energy in the interval, kWh."})


@dp.materialized_view(comment="Historian values averaged per hour for the 30 days up to the as-of day, per element and attribute (MW, AMPS, "
                              "TOP_OIL_TEMP, H2_PPM and the other tags).")
def gld_historian_hourly():
    return _commented(spark.sql(f"""
      SELECT element, attribute, date_trunc('HOUR', timestamp) hour, avg(value) value
      FROM `{CAT}`.`{SCH}`.share_historian_archive
      WHERE timestamp >= date_sub(date'{AS_OF}', 29) AND timestamp < date_add(date'{AS_OF}', 1)
      GROUP BY ALL"""), {
        "element": "Twin asset id the tag belongs to.", "attribute": "Historian attribute: MW, AMPS, TOP_OIL_TEMP, H2_PPM and others.",
        "hour": "Hour starting.", "value": "Average value in the hour, in the attribute's units."})


@dp.materialized_view(comment="Historian values averaged per day for the 120 days up to the as-of day, per element and attribute.")
def gld_historian_daily():
    return _commented(spark.sql(f"""
      SELECT element, attribute, to_date(timestamp) day, avg(value) value
      FROM `{CAT}`.`{SCH}`.share_historian_archive
      WHERE timestamp >= date_sub(date'{AS_OF}', 119) AND timestamp < date_add(date'{AS_OF}', 1)
      GROUP BY ALL"""), {
        "element": "Twin asset id the tag belongs to.", "attribute": "Historian attribute: MW, AMPS, TOP_OIL_TEMP, H2_PPM and others.",
        "day": "Day.", "value": "Average value in the day, in the attribute's units."})


@dp.materialized_view(comment="Weather forecast per H3 cell (resolution 5) and hour, with the cell boundary as GeoJSON. Grid points that share a "
                              "cell are combined: the strongest gust, rain and temperature, the average radiation.")
def gld_weather_cell():
    return _commented(spark.sql("""
      WITH c AS (SELECT h3_r5, forecast_time, max(gust_kmh) gust_kmh, max(rain_mm) rain_mm, max(temp_c) temp_c, avg(radiation_wm2) radiation_wm2
                 FROM slv_weather_forecast GROUP BY h3_r5, forecast_time)
      SELECT h3_h3tostring(h3_r5) h3, forecast_time, h3_boundaryasgeojson(h3_r5) geo, gust_kmh, rain_mm, temp_c, radiation_wm2 FROM c"""), {
        "h3": "H3 cell id (resolution 5, hexadecimal string).", "forecast_time": "Forecast hour.", "geo": "Cell boundary, GeoJSON polygon.",
        "gust_kmh": "Strongest forecast gust in the cell, km/h.", "rain_mm": "Heaviest forecast rain in the cell, mm.",
        "temp_c": "Highest forecast temperature in the cell, degrees Celsius.", "radiation_wm2": "Average solar radiation, W/m2."})


@dp.materialized_view(comment="RFS major incidents with their geometry as GeoJSON (the perimeter stays as perimeter_geojson).")
def gld_fire_incident():
    return _commented(spark.read.table("slv_fire_incident").drop("geom", "perimeter"), {
        "incident_id": "NSW RFS incident id.", "title": "Incident title.", "alert_level": "Alert level as published by NSW RFS.",
        "status": "Status as published by NSW RFS.", "incident_type": "Type of incident.", "size": "Size as published (text).",
        "council_area": "Council area.", "agency": "Responding agency.", "updated": "When the feed last updated the incident.",
        "point_geojson": "Incident point, GeoJSON.", "perimeter_geojson": "Perimeter, GeoJSON (empty when only a point is published).",
        "description": "Description.", "captured_at": "When the feed was captured.", "lon": "Longitude (WGS84).", "lat": "Latitude (WGS84).",
        "size_ha": "Size, hectares."})


@dp.materialized_view(comment="Satellite thermal hotspots over the footprint in the last three days.")
def gld_hotspot():
    return _commented(spark.read.table("slv_hotspot").drop("geom"), {
        "hotspot_id": "Hotspot id.", "satellite": "Satellite that detected it.", "sensor": "Sensor on the satellite.",
        "observed_at": "When it was observed.", "lat": "Latitude (WGS84).", "lon": "Longitude (WGS84).",
        "temp_kelvin": "Brightness temperature, kelvin.", "power_mw": "Fire radiative power, MW.",
        "confidence": "Detection confidence, percent.", "hours_since": "Hours since it was observed, at capture."})


@dp.materialized_view(comment="NSW fire weather districts: today's and tomorrow's fire danger rating and total fire ban (the latest capture).")
def gld_fire_danger():
    return _commented(spark.read.table("slv_fire_danger"), {
        "district": "Fire weather district.", "region_number": "NSW RFS region number.", "councils": "Councils in the district.",
        "rating_today": "Fire danger rating today.", "rating_tomorrow": "Fire danger rating tomorrow.",
        "fire_ban_today": "A total fire ban applies today.", "fire_ban_tomorrow": "A total fire ban applies tomorrow.",
        "captured_at": "When the feed was captured."})


@dp.materialized_view(comment="Bulk supply points (transmission connection points) with voltage, capacity, firm capacity, the transmission "
                              "substations they feed and location.")
def gld_bulk_supply():
    return _commented(spark.sql("""
      SELECT b.bsp_id, b.name, b.area, b.kv, b.capacity_mva, b.transformers, b.firm_mva, b.lon, b.lat,
             count(t.ts_id) transmission_substations
      FROM slv_bulk_supply b LEFT JOIN slv_transmission_substation t ON t.bsp_id = b.bsp_id GROUP BY ALL"""), {
        "firm_mva": "Capacity left with one transformer out of service, MVA.", "lon": "Longitude (WGS84).", "lat": "Latitude (WGS84).",
        "transmission_substations": "Transmission substations this bulk supply point feeds over 132 kV."})


@dp.materialized_view(comment="Subtransmission circuits from each transmission substation to a zone substation, with the bulk supply point behind "
                              "the site and the route as GeoJSON.")
def gld_subtransmission():
    return _commented(spark.read.table("slv_subtransmission").withColumn("geo", F.expr("st_asgeojson(geom)")).drop("geom"), {
        "line_id": "Circuit id.", "bsp_id": "Bulk supply point behind the transmission substation.",
        "ts_id": "Transmission substation at the sending end.", "zs_id": "Zone substation it supplies.", "circuit": "Circuit number into the zone.",
        "kv": "Voltage, kV (33 or 66).", "route_km": "Route length, km.", "rating_mva": "Thermal rating of the circuit, MVA.",
        "install_year": "Year built.", "geo": "Route, GeoJSON."})


@dp.materialized_view(comment="Transmission substations: the 132 kV sites between the bulk supply points and the zone substations. Each row has its "
                              "transformers, installed and firm (N-1) capacity, the 30-day peak load against firm capacity, the zones it feeds, "
                              "the zones that depend on it alone, its customers and a status colour (the app's Transmission stop).")
def gld_transmission_substation():
    c, s = spark.conf.get("atlas.catalog"), spark.conf.get("atlas.schema")
    return _commented(spark.sql(f"""
      WITH per_ts AS (SELECT substring(element, 1, 6) ts_id, timestamp, sum(value) mw FROM `{c}`.`{s}`.share_historian_archive
                      WHERE attribute = 'MW' AND element LIKE 'TS-%-TX%' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS GROUP BY ALL),
      pk AS (SELECT ts_id, max(mw) peak_mw_30d, max_by(mw, timestamp) mw_latest FROM per_ts GROUP BY ts_id),
      sub AS (SELECT ts_id, array_sort(collect_set(zs_id)) zones, count(*) supply_circuits FROM slv_subtransmission GROUP BY ts_id),
      dep AS (SELECT ts_ids[0] ts_id, array_sort(collect_set(zs_id)) single_transmission_zones FROM gld_zone_supply
              WHERE size(ts_ids) = 1 GROUP BY ALL),
      ln AS (SELECT ts_id, count(*) lines_132 FROM slv_transmission_line GROUP BY ts_id)
      SELECT t.ts_id, t.name, t.area, coalesce(l.label, t.area) area_label, t.primary_kv kv, t.secondary_kv, t.transformers,
             t.transformer_mva, t.installed_mva, t.firm_mva, t.bsp_id, b.name bulk_supply_name, t.lat, t.lon, t.commissioned,
             coalesce(sub.zones, array()) zones, coalesce(size(sub.zones), 0) zone_count, coalesce(sub.supply_circuits, 0) supply_circuits,
             coalesce(ln.lines_132, 0) lines_132, coalesce(dep.single_transmission_zones, array()) single_transmission_zones,
             coalesce(i.customers_downstream, 0) customers, coalesce(i.life_support_downstream, 0) life_support,
             coalesce(i.critical_sites_downstream, 0) critical_sites,
             round(pk.peak_mw_30d, 1) peak_mw_30d, round(pk.mw_latest, 1) mw_latest, round(pk.peak_mw_30d / t.firm_mva, 2) peak_utilisation_30d,
             CASE WHEN pk.peak_mw_30d / t.firm_mva > 1.0 THEN 'red' WHEN pk.peak_mw_30d / t.firm_mva > 0.85 THEN 'amber' ELSE 'green' END status
      FROM slv_transmission_substation t
      LEFT JOIN ref_label l ON l.domain = 'area' AND l.code = t.area
      LEFT JOIN slv_bulk_supply b ON b.bsp_id = t.bsp_id
      LEFT JOIN sub ON sub.ts_id = t.ts_id
      LEFT JOIN dep ON dep.ts_id = t.ts_id
      LEFT JOIN ln ON ln.ts_id = t.ts_id
      LEFT JOIN pk ON pk.ts_id = t.ts_id
      LEFT JOIN gld_customer_impact i ON i.equipment_id = t.ts_id"""), {
        "ts_id": "Transmission substation id.", "name": "Transmission substation name.", "area": "Area code (MET, RNG, CST).",
        "area_label": "Area, in words.", "kv": "Supply voltage from the bulk supply point, kV (132).",
        "secondary_kv": "Voltage out to the zone substations, kV (33 or 66).", "transformers": "Transformers at the site, in parallel.",
        "transformer_mva": "Rating of each transformer, MVA.", "installed_mva": "Total installed rating, MVA.",
        "firm_mva": "N-1 firm capacity (installed less the largest transformer), MVA.", "bsp_id": "Bulk supply point feeding the site.",
        "bulk_supply_name": "Bulk supply point name.", "lat": "Latitude (WGS84).", "lon": "Longitude (WGS84).",
        "commissioned": "Year commissioned.", "zones": "Zone substations it feeds (ids).", "zone_count": "Zone substations it feeds.",
        "supply_circuits": "Subtransmission circuits out of the site.", "lines_132": "132 kV circuits from the bulk supply point.",
        "single_transmission_zones": "Zone substations with no other transmission substation (they lose supply if this site is lost).",
        "customers": "Customers in the zones it feeds (a zone fed from two sites counts for each).", "life_support": "Life support customers in those zones.",
        "critical_sites": "Critical sites in those zones.", "peak_mw_30d": "Highest combined load of its transformers in the last 30 days, MW.",
        "mw_latest": "Combined load of its transformers at the latest historian reading, MW.",
        "peak_utilisation_30d": "Peak 30-day load against the N-1 firm capacity (1.0 is full).",
        "status": "Status colour: red over 1.0 of firm capacity, amber over 0.85, otherwise green."})


@dp.materialized_view(comment="Transmission transformer condition: latest and 30-day peak loading against nameplate, top-oil temperature and a "
                              "status colour, per transmission transformer.")
def gld_transmission_transformer_state():
    c, s = spark.conf.get("atlas.catalog"), spark.conf.get("atlas.schema")
    return _commented(spark.sql(f"""
      WITH h AS (SELECT element, attribute, timestamp, value FROM `{c}`.`{s}`.share_historian_archive
                 WHERE element LIKE 'TS-%-TX%' AND timestamp >= date'{AS_OF}' - INTERVAL 90 DAYS),
      last AS (SELECT element, attribute, max_by(value, timestamp) v FROM h GROUP BY ALL),
      p30 AS (SELECT element, max(value) FILTER (WHERE attribute = 'MW' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS) peak_mw_30d,
                     max(value) FILTER (WHERE attribute = 'TOP_OIL_TEMP' AND timestamp >= date'{AS_OF}' - INTERVAL 30 DAYS) peak_top_oil_30d
              FROM h GROUP BY element),
      nw AS (SELECT element, max(CASE WHEN attribute = 'MW' THEN v END) mw_now, max(CASE WHEN attribute = 'TOP_OIL_TEMP' THEN v END) top_oil_now
             FROM last GROUP BY element)
      SELECT x.INVNR transformer_id, t.ts_id, x.EQKTX name, t.transformer_mva rating_mva, x.HERST manufacturer, CAST(x.BAUJJ AS INT) year_built,
             x.EQUNR sap_equnr, nw.mw_now, nw.top_oil_now, p.peak_mw_30d, p.peak_top_oil_30d,
             round(p.peak_mw_30d / t.transformer_mva, 2) peak_utilisation_30d,
             CASE WHEN p.peak_mw_30d / t.transformer_mva > 1.0 THEN 'red' WHEN p.peak_mw_30d / t.transformer_mva > 0.85 THEN 'amber' ELSE 'green' END status
      FROM slv_sap_equi x
      JOIN slv_transmission_substation t ON t.ts_id = substring(x.INVNR, 1, 6)
      LEFT JOIN nw ON nw.element = x.INVNR
      LEFT JOIN p30 p ON p.element = x.INVNR
      WHERE x.EQART = 'TTX'"""), {
        "transformer_id": "Transmission transformer asset id (the site id and -TX number).", "ts_id": "Transmission substation it sits in.",
        "name": "Transformer name.", "rating_mva": "Nameplate rating, MVA.", "manufacturer": "Manufacturer.", "year_built": "Year built.",
        "sap_equnr": "Equipment number in the asset register.", "mw_now": "Load at the latest historian reading, MW.",
        "top_oil_now": "Top-oil temperature at the latest reading, deg C.", "peak_mw_30d": "Highest load in the last 30 days, MW.",
        "peak_top_oil_30d": "Highest top-oil temperature in the last 30 days, deg C.",
        "peak_utilisation_30d": "Peak 30-day load against nameplate (1.0 is full).",
        "status": "Status colour: red over 1.0 of nameplate, amber over 0.85, otherwise green."})


@dp.materialized_view(comment="132 kV transmission lines from each bulk supply point to a transmission substation, with the route as GeoJSON.")
def gld_transmission_line():
    return _commented(spark.read.table("slv_transmission_line").withColumn("geo", F.expr("st_asgeojson(geom)")).drop("geom"), {
        "line_id": "Circuit id.", "bsp_id": "Bulk supply point at the sending end.", "ts_id": "Transmission substation it supplies.",
        "circuit": "Circuit number on the pair.", "kv": "Voltage, kV (132).", "route_km": "Route length, km.",
        "rating_mva": "Thermal rating of the circuit, MVA.", "install_year": "Year built.", "geo": "Route, GeoJSON."})


@dp.materialized_view(comment="Public EV charging sites and the distribution substation each one plugs into.")
def gld_public_charging():
    return _commented(spark.read.table("brz_public_charging").select(
        "site_id", "substation_ref", F.col("latitude").cast("double").alias("latitude"), F.col("longitude").cast("double").alias("longitude"),
        F.col("evse_count").cast("int").alias("evse_count"), F.col("evse_kw").cast("double").alias("evse_kw")), {
        "site_id": "Charging site id.", "substation_ref": "Distribution substation the site connects to (ds_id).",
        "latitude": "Latitude (WGS84).", "longitude": "Longitude (WGS84).",
        "evse_count": "Charging points (EVSE) at the site.", "evse_kw": "Power per charging point, kW."})


@dp.materialized_view(comment="Every feeder's overhead spans and cable sections as one shape: the union of the lines, simplified to about 25 m, as "
                              "GeoJSON. The network map draws each feeder from this.")
def gld_feeder_geo():
    return _commented(spark.sql("""
      SELECT feeder_id, st_asgeojson(st_simplify(st_union_agg(st_geomfromwkt(geom_wkt, 4326)), 0.00025)) geo
      FROM gld_asset WHERE asset_class IN ('overhead_span', 'cable_section') AND geom_wkt IS NOT NULL GROUP BY feeder_id"""), {
        "feeder_id": "Feeder.", "geo": "Overhead and underground route of the feeder, GeoJSON (simplified to about 25 m)."})


@dp.materialized_view(comment="Crew skills, one row per crew and skill code (primary and secondary together).")
def gld_crew_skill():
    return spark.read.table("slv_crew_skill").select("crew_id", "skill_code").distinct()


@dp.materialized_view(comment="Feeders for the field resource management map: the zone substation, the region that serves it, customers, life "
                              "support, a stress level from 0 to 1 (peak loading, outages, vegetation and bushfire exposure), and status.")
def gld_rm_feeder():
    return _commented(spark.sql("""
      WITH r AS (SELECT region_id, explode(zone_substations) zs_id FROM gld_rm_region),
      c AS (SELECT feeder_id, avg(lat) lat, avg(lon) lon FROM gld_asset WHERE asset_class IN ('pole', 'distribution_substation') GROUP BY feeder_id)
      SELECT s.feeder_id, s.feeder_name, r.region_id, s.zs_id, c.lat, c.lon, s.customers, s.life_support,
             round(least(1.0, 0.5 * least(1.0, coalesce(s.peak_utilisation_30d, 0)) + 0.2 * least(1.0, s.sustained_outages_12m / 8.0)
                              + 0.15 * least(1.0, s.veg_cat_a / 20.0) + 0.15 * coalesce(s.bushfire_share, 0)), 3) stress_level, s.status
      FROM gld_feeder_state s JOIN r ON r.zs_id = s.zs_id LEFT JOIN c ON c.feeder_id = s.feeder_id"""), {
        "feeder_id": "Feeder.", "feeder_name": "Feeder name.", "zs_id": "Zone substation the feeder comes from.",
        "lat": "Feeder location: the average of its poles and distribution substations (WGS84).",
        "lon": "Feeder location longitude (WGS84).", "customers": "Customers on the feeder.", "life_support": "Life support customers on the feeder.",
        "stress_level": "Stress from 0 (none) to 1: peak loading, outages, vegetation and bushfire exposure.", "status": "Status colour: red, amber or green."})


# ------------------------------------------------------------------ Genie over the app's records (the decisions, the notices, the dispatch)
@dp.materialized_view(comment="Each situation with every recommendation made on it and the decision taken: who decided, when, and the status "
                              "(proposed, accepted, adjusted or rejected). A situation with no recommendation appears once, with no recommendation.")
def gld_situation_decision():
    return _commented(spark.sql("""
      SELECT s.situation_id, s.type situation_type, coalesce(t.label, s.type) situation_type_label, s.title situation_title, s.severity,
             s.status situation_status, s.detected_at, s.customers, s.life_support, s.critical_sites, s.feeders,
             r.rec_id, r.kind recommendation_kind, r.title recommendation_title, r.status decision_status, r.created_at proposed_at,
             r.decided_at, r.decided_by, r.reason decision_reason
      FROM slv_app_situation s
      LEFT JOIN slv_app_recommendation r ON r.situation_id = s.situation_id
      LEFT JOIN ref_label t ON t.domain = 'situation_type' AND t.code = s.type"""), {
        "situation_id": "Situation id.", "situation_type": "Kind of situation (code).", "situation_type_label": "Kind of situation, in words.",
        "situation_title": "Situation title.", "severity": "critical, high, medium or info.", "situation_status": "detected, plan_ready, decided, executing or closed.",
        "detected_at": "When the situation was detected.",
        "customers": "Customers in the situation area.", "life_support": "Life support customers in the area.",
        "critical_sites": "Critical sites in the area.", "feeders": "Feeders in the area.",
        "rec_id": "Recommendation id (empty for a situation with none).", "recommendation_kind": "Kind of action recommended (code).",
        "recommendation_title": "What is recommended.", "decision_status": "proposed, accepted, adjusted or rejected.",
        "proposed_at": "When the recommendation was made.", "decided_at": "When the decision was taken.",
        "decided_by": "Who took the decision.", "decision_reason": "Reason given for the decision."})


@dp.materialized_view(comment="Customer notices per situation and recommendation: who they went to (audience and channel), how many customers, and "
                              "the message counts by status (draft, queued, sent, delivered).")
def gld_customer_notice():
    return _commented(spark.sql("""
      SELECT o.situation_id, s.title situation_title, coalesce(t.label, s.type) situation_type_label, s.severity,
             o.rec_id, r.title recommendation_title, r.status decision_status, r.decided_by, r.decided_at,
             o.audience, coalesce(a.label, o.audience) audience_label, o.channel,
             count(*) messages, count(DISTINCT o.nmi) customers,
             count(*) FILTER (WHERE o.status = 'draft') drafts, count(*) FILTER (WHERE o.status = 'queued') queued,
             count(*) FILTER (WHERE o.status = 'sent') sent, count(*) FILTER (WHERE o.status = 'delivered') delivered,
             min(o.created_at) first_created_at, max(o.sent_at) last_sent_at
      FROM slv_app_notification_outbox o
      LEFT JOIN slv_app_situation s ON s.situation_id = o.situation_id
      LEFT JOIN slv_app_recommendation r ON r.rec_id = o.rec_id
      LEFT JOIN ref_label t ON t.domain = 'situation_type' AND t.code = s.type
      LEFT JOIN ref_label a ON a.domain = 'audience' AND a.code = o.audience
      GROUP BY ALL"""), {
        "situation_id": "Situation the notice is for.", "situation_title": "Situation title.",
        "situation_type_label": "Kind of situation, in words.", "severity": "critical, high, medium or info.",
        "rec_id": "The notify recommendation the messages belong to.",
        "recommendation_title": "What was recommended.", "decision_status": "Status of the recommendation: proposed, accepted, adjusted or rejected.",
        "decided_by": "Who decided the recommendation.", "decided_at": "When it was decided.",
        "audience": "life_support or customer.", "audience_label": "Audience, in words.", "channel": "Channel of the messages.",
        "messages": "Messages in the notice.", "customers": "Distinct customers (NMIs) messaged.", "drafts": "Messages still in draft.",
        "queued": "Messages queued (can still be undone).", "sent": "Messages sent.", "delivered": "Messages delivered (simulated).",
        "first_created_at": "When the first message was drafted.", "last_sent_at": "When the last message was sent."})


@dp.materialized_view(comment="Field work raised by the app or the live engine, with its field status, whether it is parked, the crew assigned to it "
                              "and the crew on site. Planned work orders are in gld_field_job.")
def gld_field_dispatch():
    return _commented(spark.sql("""
      WITH c AS (SELECT job_id, max_by(crew_id, since) crew_on_job, max_by(status, since) crew_status, max(since) crew_since
                 FROM slv_app_crew_live WHERE job_id IS NOT NULL GROUP BY job_id)
      SELECT j.job_id, j.job_date, j.job_type, coalesce(jt.label, j.job_type) job_type_label, j.title, j.priority, j.is_emergency,
             j.source, coalesce(sl.label, j.source) source_label, j.situation_id, j.region_id, j.feeder_id, j.asset_id, j.suburb,
             j.customers, j.est_customer_minutes, j.duration_min, j.window_start_min, j.window_end_min,
             j.required_skill, j.required_ticket, j.funding, coalesce(fl.label, j.funding) funding_label,
             coalesce(fs.status, 'planned') field_status, (p.job_id IS NOT NULL) AS is_parked,
             get_json_object(o.payload, '$.crew_id') assigned_crew_id, get_json_object(o.payload, '$.crew_name') assigned_crew_name,
             CAST(get_json_object(o.payload, '$.travel_min') AS DOUBLE) travel_min,
             c.crew_on_job, g.crew_name crew_on_job_name, c.crew_status, c.crew_since
      FROM slv_app_raised_job j
      LEFT JOIN slv_app_field_status fs ON fs.region_id = j.region_id AND fs.job_id = j.job_id
      LEFT JOIN slv_app_parked p ON p.region_id = j.region_id AND p.job_id = j.job_id
      LEFT JOIN slv_app_assignment_override o ON o.region_id = j.region_id AND o.job_id = j.job_id
      LEFT JOIN c ON c.job_id = j.job_id
      LEFT JOIN gld_crew g ON g.crew_id = c.crew_on_job
      LEFT JOIN ref_label jt ON jt.domain = 'job_type' AND jt.code = j.job_type
      LEFT JOIN ref_label sl ON sl.domain = 'source' AND sl.code = j.source
      LEFT JOIN ref_label fl ON fl.domain = 'funding' AND fl.code = j.funding"""), {
        "job_id": "Job id.", "job_date": "Day the work is planned for.", "job_type": "Job type code.", "job_type_label": "Job type, in words.",
        "title": "What the job is.", "priority": "Priority, 1 (low) to 5 (urgent).", "is_emergency": "Emergency work.",
        "source": "Where the work came from (code).", "source_label": "Where the work came from, in words.",
        "situation_id": "Situation that raised the job, if any.", "region_id": "Region the job is in.", "feeder_id": "Feeder the job is on.",
        "asset_id": "Asset the job is at.", "suburb": "Suburb.", "customers": "Customers downstream of the asset.",
        "est_customer_minutes": "Customer minutes at risk if the job slips.", "duration_min": "On-site minutes.",
        "window_start_min": "Earliest start, minutes after midnight.", "window_end_min": "Latest finish, minutes after midnight.",
        "required_skill": "Skill the crew must hold.", "required_ticket": "Ticket the crew must hold, in date.",
        "funding": "Who pays (code).", "funding_label": "Who pays, in words.",
        "field_status": "planned, driving, in_progress, done or parked (planned until the field app reports).",
        "is_parked": "True when the job is on hold in the dispatcher's queue.",
        "assigned_crew_id": "Crew that owns the job after a manual assign or re-dispatch (empty when the roster solved it).",
        "assigned_crew_name": "Name of the assigned crew.", "travel_min": "Travel minutes from the crew's start to the job.",
        "crew_on_job": "Crew on the job now (live position).", "crew_on_job_name": "Name of the crew on the job.",
        "crew_status": "travelling, on_site or available: the crew's live status.", "crew_since": "When the crew reached that status."})
