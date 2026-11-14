# Silver: conformed, joined and spatially enabled. The twin's keys are resolved here across SAP, GIS, CIM and the historian.
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque

from pyspark import pipelines as dp
from pyspark.errors import AnalysisException
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

CAT = spark.conf.get("atlas.catalog")
SCH = spark.conf.get("atlas.schema")
AS_OF = spark.conf.get("atlas.as_of")

# The change feed table names are the generator's (records.py), shared with the live layer of bronze.
sys.path.insert(0, os.path.join(spark.conf.get("atlas.src"), "generator"))
from records import change_feed_table  # noqa: E402


def _ref(c):
    return F.regexp_replace(F.col(c), "^#_", "")


def _geom(hexcol):
    return F.expr(f"st_geomfromwkb(unhex({hexcol}), 4326)")


# ------------------------------------------------------------------ connectivity
@dp.materialized_view(comment="Connectivity edges from the CIM model: each conducting equipment with the nodes at its terminals.")
def slv_ncm_edge():
    t = spark.read.table("brz_ncm_terminal").select(_ref("equipment_ref").alias("equipment_id"), "sequence_number",
                                                     _ref("node_ref").alias("node_id"),
                                                     F.regexp_extract("phases_ref", r"PhaseCode\.(\w+)", 1).alias("phases"))
    e = spark.read.table("brz_ncm_equipment").select(F.col("mrid").alias("equipment_id"), "cim_class",
                                                      _ref("container_ref").alias("container_id"),
                                                      (F.col("normal_open") == "true").alias("normal_open"), "length_m")
    return (t.groupBy("equipment_id").agg(F.max(F.when(F.col("sequence_number") == 1, F.col("node_id"))).alias("node_1"),
                                         F.max(F.when(F.col("sequence_number") == 2, F.col("node_id"))).alias("node_2"),
                                         F.first("phases").alias("phases"))
            .join(e, "equipment_id", "left"))


@dp.materialized_view(comment="Energised-path trace of the network as recorded (normal switch states): for every equipment, the "
                              "feeder that supplies it, its depth from the feeder breaker, the nearest upstream protective device "
                              "and the nearest upstream sectionalising switch. Normally open points stop the trace.")
def slv_feeder_membership():
    edges = spark.read.table("slv_ncm_edge").where("cim_class IS NOT NULL").select(
        "equipment_id", "cim_class", "node_1", "node_2", "normal_open", "container_id").collect()
    by_node = defaultdict(list)
    eq = {}
    for r in edges:
        eq[r.equipment_id] = r
        for n in (r.node_1, r.node_2):
            if n:
                by_node[n].append(r.equipment_id)
    protective = {"Breaker", "Recloser", "Fuse", "Disconnector"}
    sectional = {"Recloser", "LoadBreakSwitch"}
    out = []
    for r in edges:
        if r.cim_class != "Breaker":
            continue
        feeder = r.container_id
        seen_eq = {r.equipment_id}
        q = deque([(r.node_2, r.equipment_id, 1, r.equipment_id, None)])
        out.append((r.equipment_id, r.cim_class, feeder, 0, None, r.equipment_id, None))
        seen_nodes = {r.node_1}
        while q:
            node, parent, depth, prot, sect = q.popleft()
            if node in seen_nodes:
                continue
            seen_nodes.add(node)
            for e_id in by_node[node]:
                if e_id in seen_eq:
                    continue
                e = eq[e_id]
                seen_eq.add(e_id)
                out.append((e_id, e.cim_class, feeder, depth, parent, prot, sect))
                if e.normal_open:
                    continue
                other = e.node_2 if e.node_1 == node else e.node_1
                if other:
                    q.append((other, e_id, depth + 1, e_id if e.cim_class in protective else prot,
                              e_id if e.cim_class in sectional else sect))
    schema = ("equipment_id string, cim_class string, feeder_id string, depth int, parent_equipment string, "
              "protective_device string, sectionalising_device string")
    return spark.createDataFrame(out, schema)


@dp.materialized_view(comment="Which distribution substation and feeder supply each NMI according to the connectivity model "
                              "(the record, which can disagree with reality; see the data-quality checks).")
def slv_nmi_supply():
    up = spark.read.table("brz_ncm_usage_point").select("nmi", _ref("equipment_ref").alias("ec_id"))
    edge = spark.read.table("slv_ncm_edge")
    ec = edge.where("cim_class = 'EnergyConsumer'").select(F.col("equipment_id").alias("ec_id"), F.col("node_1").alias("lv_node"))
    tx = edge.where("cim_class = 'PowerTransformer' AND equipment_id LIKE 'DS-%'").select(
        F.col("equipment_id").alias("ds_id"), F.col("node_2").alias("lv_node"))
    m = spark.read.table("slv_feeder_membership").select(F.col("equipment_id").alias("ds_id"), "feeder_id", "protective_device",
                                                        "sectionalising_device")
    return up.join(ec, "ec_id").join(tx, "lv_node").join(m, "ds_id", "left").drop("lv_node")


# ------------------------------------------------------------------ SAP PM (current state via AUTO CDC)
def _sap_cdc(table, keys):
    target = f"slv_sap_{table}"
    dp.create_streaming_table(target, comment=f"Current state of SAP {table.upper()} after applying the delta extracts (AUTO CDC).")
    dp.create_auto_cdc_flow(target=target, source=f"brz_sap_{table}", keys=keys,
                            sequence_by=F.struct("_source_file", F.col("ODQ_ENTITYCNTR").cast("long")),
                            apply_as_deletes=F.expr("ODQ_CHANGEMODE = 'D'"),
                            except_column_list=["ODQ_CHANGEMODE", "ODQ_ENTITYCNTR"])


_sap_cdc("equi", ["EQUNR"])
_sap_cdc("qmel", ["QMNUM"])
_sap_cdc("qmih", ["QMNUM"])
_sap_cdc("aufk", ["AUFNR"])


# ------------------------------------------------------------------ unified asset
@dp.materialized_view(comment="The unified asset: one row per physical asset with its keys in every system (twin id, SAP equipment "
                              "number, GIS feature id, CIM mRID, historian element) and a GEOMETRY column (WGS84).")
def slv_asset():
    equi = spark.read.table("slv_sap_equi").select(F.col("INVNR").alias("asset_id"), F.col("EQUNR").alias("sap_equnr"),
                                                    F.col("EQART").alias("sap_object_type"), F.col("HERST").alias("manufacturer"),
                                                    F.col("BAUJJ").cast("int").alias("sap_year"))
    memb = spark.read.table("slv_feeder_membership").select(F.col("equipment_id").alias("asset_id"), F.col("feeder_id").alias("ncm_feeder"),
                                                           "protective_device", "sectionalising_device", "depth")
    pole = spark.read.table("brz_gis_electric_pole").select(
        F.col("pole_number").alias("asset_id"), F.lit("pole").alias("asset_class"), F.col("pole_number").alias("name"),
        F.col("feeder").alias("feeder_id"), F.col("fid").alias("gis_fid"), "geom_wkb_hex",
        F.col("install_year").cast("int").alias("install_year"), F.col("material"), F.col("height_m").cast("double").alias("rating"),
        F.lit("m").alias("rating_unit"), F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role"))
    ds = spark.read.table("brz_gis_electric_dist_substation").select(
        F.col("substation_id").alias("asset_id"), F.lit("distribution_substation").alias("asset_class"),
        F.concat(F.lit("Distribution substation "), F.col("substation_id")).alias("name"), F.col("feeder").alias("feeder_id"),
        F.col("fid").alias("gis_fid"), "geom_wkb_hex", F.col("install_year").cast("int").alias("install_year"),
        F.lower("substation_type").alias("material"), F.col("rating_kva").cast("double").alias("rating"), F.lit("kVA").alias("rating_unit"),
        F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role"))
    sw = spark.read.table("brz_gis_electric_switch").select(
        F.col("device_id").alias("asset_id"), F.lower("device_type").alias("asset_class"), F.col("device_id").alias("name"),
        F.col("feeder").alias("feeder_id"), F.col("fid").alias("gis_fid"), "geom_wkb_hex", F.lit(None).cast("int").alias("install_year"),
        F.col("make").alias("material"), F.lit(None).cast("double").alias("rating"), F.lit(None).cast("string").alias("rating_unit"),
        F.col("rating_amps").cast("int").alias("rating_amps", metadata={"comment": "Continuous rating, amps (conductors and switching devices)."}),
        F.lit(None).cast("string").alias("circuit_role"))
    zs = spark.read.table("brz_gis_electric_zone_substation").select(
        F.col("substation_id").alias("asset_id"), F.lit("zone_substation").alias("asset_class"), F.col("name"),
        F.lit(None).cast("string").alias("feeder_id"), F.col("fid").alias("gis_fid"), "geom_wkb_hex",
        F.lit(None).cast("int").alias("install_year"), F.col("region").alias("material"),
        F.col("secondary_kv").cast("double").alias("rating"), F.lit("kV").alias("rating_unit"), F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role"))
    span = spark.read.table("brz_gis_electric_conductor").select(
        F.col("conductor_id").alias("asset_id"), F.when(F.col("construction") == "OH", "overhead_span").otherwise("cable_section").alias("asset_class"),
        F.col("conductor_id").alias("name"), F.col("feeder").alias("feeder_id"), F.col("fid").alias("gis_fid"), "geom_wkb_hex",
        F.col("install_year").cast("int").alias("install_year"), F.col("conductor_type").alias("material"),
        F.col("length_m").cast("double").alias("rating"), F.lit("m").alias("rating_unit"),
        F.col("rating_amps").cast("int").alias("rating_amps"), F.lower("circuit_role").alias("circuit_role", metadata={"comment": "Trunk, spur, swer or tie."}))
    # transmission level: the site, its transformers (at the site point, rated in the GIS) and the 132 kV lines
    tsite = spark.read.table("brz_gis_electric_transmission_substation").select(
        F.col("substation_id").alias("asset_id"), F.lit("transmission_substation").alias("asset_class"), F.col("name"),
        F.lit(None).cast("string").alias("feeder_id"), F.col("fid").alias("gis_fid"), "geom_wkb_hex",
        F.col("commissioned").cast("int").alias("install_year"), F.col("region").alias("material"),
        (F.col("transformers").cast("int") * F.col("transformer_rating_mva").cast("int")).cast("double").alias("rating"),
        F.lit("MVA").alias("rating_unit"), F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role"))
    tx = (spark.read.table("brz_gis_electric_transmission_substation")
          .withColumn("n", F.col("transformers").cast("int"))
          .withColumn("x", F.explode(F.sequence(F.lit(1), F.col("n"))))
          .select(F.concat(F.col("substation_id"), F.lit("-TX"), F.col("x").cast("string")).alias("asset_id"),
                  F.lit("transmission_transformer").alias("asset_class"),
                  F.concat(F.regexp_replace("name", " substation$", ""), F.lit(" transformer "), F.col("x").cast("string")).alias("name"),
                  F.lit(None).cast("string").alias("feeder_id"), F.lit(None).cast("string").alias("gis_fid"), "geom_wkb_hex",
                  F.lit(None).cast("int").alias("install_year"), F.lit(None).cast("string").alias("material"),
                  F.col("transformer_rating_mva").cast("double").alias("rating"), F.lit("MVA").alias("rating_unit"),
                  F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role")))
    tline = spark.read.table("brz_gis_electric_transmission_line").select(
        F.col("line_id").alias("asset_id"), F.lit("transmission_line").alias("asset_class"),
        F.concat(F.lit("132 kV line "), F.col("line_id")).alias("name"), F.lit(None).cast("string").alias("feeder_id"),
        F.col("fid").alias("gis_fid"), "geom_wkb_hex", F.col("install_year").cast("int").alias("install_year"),
        F.lit(None).cast("string").alias("material"), F.col("rating_mva").cast("double").alias("rating"), F.lit("MVA").alias("rating_unit"),
        F.lit(None).cast("int").alias("rating_amps"), F.lit(None).cast("string").alias("circuit_role"))
    allg = (pole.unionByName(ds).unionByName(sw).unionByName(zs).unionByName(span)
            .unionByName(tsite).unionByName(tx).unionByName(tline))
    a = (allg.join(equi, "asset_id", "left").join(memb, "asset_id", "left")
         .withColumn("geom", _geom("geom_wkb_hex"))
         .withColumn("zs_id", F.when(F.col("asset_class") == "zone_substation", F.col("asset_id"))
                     .otherwise(F.concat(F.lit("ZS-"), F.substring(F.coalesce("feeder_id", "ncm_feeder"), 1, 3))))
         .withColumn("lon", F.expr("st_x(st_centroid(geom))")).withColumn("lat", F.expr("st_y(st_centroid(geom))"))
         .withColumn("geom_wkt", F.expr("st_astext(geom)"))
         .withColumn("install_year", F.coalesce("install_year", "sap_year"))
         .withColumn("pi_element", F.when(F.col("asset_class").isin("zone_substation"), F.col("asset_id")))
         .drop("geom_wkb_hex", "sap_year"))
    return a


# ------------------------------------------------------------------ supply into the zone substations
@dp.materialized_view(comment="Bulk supply points (transmission connection points) from the GIS: voltage, capacity and transformers.")
def slv_bulk_supply():
    return (spark.read.table("brz_gis_electric_bulk_supply_point")
            .select(F.col("substation_id").alias("bsp_id", metadata={"comment": "Bulk supply point id."}),
                    F.col("name").alias("name", metadata={"comment": "Bulk supply point name."}),
                    F.col("region").alias("area", metadata={"comment": "Region code (see ref_label, domain area)."}),
                    F.col("kv").cast("int").alias("kv", metadata={"comment": "Supply voltage, kV."}),
                    F.col("capacity_mva").cast("int").alias("capacity_mva", metadata={"comment": "Firm capacity, MVA."}),
                    F.col("transformers").cast("int").alias("transformers", metadata={"comment": "Transformers at the site (two means one can fail)."}),
                    F.col("firm_mva").cast("int").alias("firm_mva", metadata={"comment": "Capacity left with one transformer out of service, MVA."}),
                    _geom("geom_wkb_hex").alias("geom"))
            .withColumn("lon", F.expr("st_x(geom)")).withColumn("lat", F.expr("st_y(geom)")))


@dp.materialized_view(comment="Transmission substations from the GIS: the 132 kV site between a bulk supply point and the zone substations it feeds, "
                              "with its voltages, transformers, firm capacity and location.")
def slv_transmission_substation():
    return (spark.read.table("brz_gis_electric_transmission_substation")
            .select(F.col("substation_id").alias("ts_id", metadata={"comment": "Transmission substation id."}),
                    F.col("name").alias("name", metadata={"comment": "Transmission substation name."}),
                    F.col("region").alias("area", metadata={"comment": "Region code (see ref_label, domain area)."}),
                    F.col("primary_kv").cast("int").alias("primary_kv", metadata={"comment": "Supply voltage from the bulk supply point, kV (132)."}),
                    F.col("secondary_kv").cast("int").alias("secondary_kv", metadata={"comment": "Voltage out to the zone substations, kV (33 or 66)."}),
                    F.col("transformers").cast("int").alias("transformers", metadata={"comment": "Transformers at the site, in parallel."}),
                    F.col("transformer_rating_mva").cast("int").alias("transformer_mva", metadata={"comment": "Rating of each transformer, MVA."}),
                    F.col("firm_mva").cast("int").alias("firm_mva", metadata={"comment": "Capacity left with the largest transformer out of service, MVA."}),
                    F.col("bulk_supply_point").alias("bsp_id", metadata={"comment": "Bulk supply point that feeds the site over 132 kV."}),
                    F.col("commissioned").cast("int").alias("commissioned", metadata={"comment": "Year commissioned."}),
                    _geom("geom_wkb_hex").alias("geom"))
            .withColumn("installed_mva", F.col("transformers") * F.col("transformer_mva"))
            .withColumn("lon", F.expr("st_x(geom)")).withColumn("lat", F.expr("st_y(geom)")))


@dp.materialized_view(comment="132 kV transmission lines from the bulk supply points to the transmission substations (the GIS circuits).")
def slv_transmission_line():
    return (spark.read.table("brz_gis_electric_transmission_line")
            .select(F.col("line_id").alias("line_id", metadata={"comment": "Circuit id."}),
                    F.col("from_substation").alias("bsp_id", metadata={"comment": "The bulk supply point at the sending end."}),
                    F.col("to_substation").alias("ts_id", metadata={"comment": "The transmission substation it supplies."}),
                    F.col("circuit").cast("int").alias("circuit", metadata={"comment": "Circuit number on the pair."}),
                    F.col("kv").cast("int").alias("kv", metadata={"comment": "Voltage, kV (132)."}),
                    F.col("route_km").cast("double").alias("route_km", metadata={"comment": "Route length, km."}),
                    F.col("rating_mva").cast("int").alias("rating_mva", metadata={"comment": "Thermal rating of the circuit, MVA."}),
                    F.col("install_year").cast("int").alias("install_year", metadata={"comment": "Year built."}),
                    _geom("geom_wkb_hex").alias("geom")))


@dp.materialized_view(comment="Subtransmission circuits from the GIS: which transmission substation feeds which zone substation, on which circuit, "
                              "and the bulk supply point behind that transmission substation.")
def slv_subtransmission():
    ts = spark.read.table("slv_transmission_substation").select("ts_id", "bsp_id")
    return (spark.read.table("brz_gis_electric_subtransmission_line")
            .select(F.col("line_id").alias("line_id", metadata={"comment": "Circuit id."}),
                    F.col("from_substation").alias("ts_id", metadata={"comment": "The transmission substation at the sending end."}),
                    F.col("to_substation").alias("zs_id", metadata={"comment": "The zone substation it supplies."}),
                    F.col("circuit").cast("int").alias("circuit", metadata={"comment": "Circuit number into the zone substation."}),
                    F.col("kv").cast("int").alias("kv", metadata={"comment": "Voltage, kV (33 or 66)."}),
                    F.col("route_km").cast("double").alias("route_km", metadata={"comment": "Route length, km."}),
                    F.col("rating_mva").cast("int").alias("rating_mva", metadata={"comment": "Thermal rating of the circuit, MVA."}),
                    F.col("install_year").cast("int").alias("install_year", metadata={"comment": "Year built."}),
                    _geom("geom_wkb_hex").alias("geom"))
            .join(ts, "ts_id", "left"))


# ------------------------------------------------------------------ customers
# street types as the market data abbreviates them (Australian address standard), in words
STREET_TYPES = {"ST": "Street", "RD": "Road", "AVE": "Avenue", "CRES": "Crescent", "CL": "Close", "PL": "Place", "PDE": "Parade",
                "DR": "Drive", "WAY": "Way", "CT": "Court", "CCT": "Circuit", "GR": "Grove", "LANE": "Lane"}
CUSTOMER_TYPES = {"residential": "Residential", "small_business": "Small business", "large_business": "Large business"}


@dp.materialized_view(comment="NMI master: standing data with the premises address (unit, number, street, suburb, postcode), supply "
                              "point in the connectivity model, life support, critical sites, DER, and the GIS service point location "
                              "with its ground elevation.")
def slv_nmi():
    c = lambda col, name, text: F.col(col).alias(name, metadata={"comment": text})  # noqa: E731
    types = F.create_map(*[F.lit(x) for kv in STREET_TYPES.items() for x in kv])
    m = spark.read.table("brz_market_nmi")
    unit = F.nullif(F.trim(F.col("FLAT_OR_UNIT_NUMBER")), F.lit(""))
    nmi = (m.select(F.col("NMI").alias("nmi"), F.col("NMI_CLASSIFICATION_CODE").alias("classification"), F.col("NETWORK_TARIFF_CODE").alias("tariff"),
                    unit.alias("unit", metadata={"comment": "Flat or unit number, for customers in a block of units; empty for a house."}),
                    c("HOUSE_NUMBER", "house_number", "Street number of the premises."),
                    F.initcap("STREET_NAME").alias("street_name", metadata={"comment": "Street name, without its type."}),
                    F.coalesce(types[F.col("STREET_TYPE")], F.initcap("STREET_TYPE")).alias(
                        "street_type", metadata={"comment": "Street type in words (Street, Road, Crescent...), from the address standard abbreviation."}),
                    F.initcap("SUBURB_OR_PLACE_OR_LOCALITY").alias("suburb", metadata={"comment": "Suburb or locality of the premises."}),
                    c("STATE_OR_TERRITORY", "state", "State of the premises."),
                    c("POSTCODE", "postcode", "Postcode of the premises."))
           .withColumn("address", F.concat(F.when(F.col("unit").isNotNull(), F.concat("unit", F.lit("/"))).otherwise(F.lit("")),
                                           "house_number", F.lit(" "), "street_name", F.lit(" "), "street_type", F.lit(", "), "suburb",
                                           F.lit(" "), "state", F.lit(" "), "postcode").alias(
                           "address", metadata={"comment": "The full postal address on one line, e.g. 3/45 Wattle Street, Glenbrook NSW 2773."})))
    ls = spark.read.table("brz_life_support").select(F.col("premises_nmi").alias("nmi"), F.col("registration_status").alias("life_support_status"),
                                                     F.col("equipment_type").alias("life_support_equipment"))
    crit = spark.read.table("brz_critical_sites").select(
        "nmi", F.col("site_type").alias("critical_site"), F.col("site_name"),
        (F.col("backup_generator") == "Y").alias("backup_generator", metadata={"comment": "Critical site with its own backup generator."}),
        F.col("generator_kva").cast("int").alias("generator_kva", metadata={"comment": "Backup generator rating, kVA."}),
        F.col("fuel_hours").cast("int").alias("generator_fuel_hours", metadata={"comment": "Hours the backup generator can run on its fuel."}))
    der = (spark.read.table("brz_der_register").where("nmi IS NOT NULL AND nmi <> ''")
           .groupBy("nmi").agg(F.sum(F.when(F.col("device_type") == "solar_pv", F.col("nameplate_kw").cast("double"))).alias("solar_kw"),
                               F.max((F.col("device_type") == "battery").cast("int")).alias("has_battery"),
                               F.max((F.col("device_type") == "home_ev_charger").cast("int")).alias("has_ev_charger")))
    sp = spark.read.table("brz_gis_electric_service_point").select(
        "nmi", F.col("service_class").alias("customer_type"), "geom_wkb_hex",
        F.coalesce(F.create_map(*[F.lit(x) for kv in CUSTOMER_TYPES.items() for x in kv])[F.col("service_class")], F.col("service_class")).alias(
            "customer_type_label", metadata={"comment": "Customer type in words: Residential, Small business or Large business."}),
        F.round(F.col("ground_elevation_m").cast("double"), 1).alias("elevation_m", metadata={
            "comment": "Ground elevation at the service point, metres above sea level (from the GIS)."}),
        F.col("street_ref").alias("street_id", metadata={"comment": "The street segment the premises fronts in the GIS."}))
    sup = spark.read.table("slv_nmi_supply").select("nmi", "ds_id", "feeder_id", "protective_device")
    cp = spark.read.table("brz_contact_preferences").select(
        "nmi", (F.col("sms_opt_in") == "Y").alias("sms_opt_in", metadata={"comment": "Registered for outage notices by SMS."}),
        (F.col("email_opt_in") == "Y").alias("email_opt_in", metadata={"comment": "Registered for outage notices by email."}),
        (F.col("facebook_messenger_opt_in") == "Y").alias("facebook_opt_in", metadata={"comment": "Registered for outage notices by Facebook Messenger."}),
        F.initcap(F.nullif("preferred_channel", F.lit(""))).alias("preferred_channel", metadata={"comment": "The channel the customer prefers on record: Sms, Email, Facebook (the app) or Mail (a letter, when no digital channel is registered)."}))
    return (nmi.join(sup, "nmi", "left").join(sp, "nmi", "left").join(ls, "nmi", "left").join(crit, "nmi", "left").join(der, "nmi", "left").join(cp, "nmi", "left")
            .withColumn("life_support", F.col("life_support_status").isNotNull())
            .withColumn("geom", _geom("geom_wkb_hex")).withColumn("lon", F.expr("st_x(geom)")).withColumn("lat", F.expr("st_y(geom)"))
            .withColumn("zs_id", F.concat(F.lit("ZS-"), F.substring("feeder_id", 1, 3)))
            .drop("geom_wkb_hex").fillna({"has_battery": 0, "has_ev_charger": 0, "solar_kw": 0.0, "sms_opt_in": False, "email_opt_in": False, "facebook_opt_in": False}))


# ------------------------------------------------------------------ outages and events
STAGE = T.ArrayType(T.StructType([T.StructField("ts", T.StringType()), T.StructField("action", T.StringType()),
                                  T.StructField("device", T.StringType()), T.StructField("customers_restored", T.LongType())]))


@dp.materialized_view(comment="ADMS outages typed, with restoration stages and duration; momentary interruptions flagged.")
def slv_outage():
    o = spark.read.table("brz_adms_outage")
    return (o.select("outage_id", "feeder_id", "device_id", "device_type", "span_id", F.to_timestamp("start_time").alias("start_time"),
                     F.to_timestamp("end_time").alias("end_time"), F.col("planned").cast("boolean").alias("planned"),
                     F.col("momentary").cast("boolean").alias("momentary"), "cause_code",
                     F.col("customers_interrupted").cast("long").alias("customers_interrupted"),
                     F.col("life_support_affected").cast("long").alias("life_support_affected"),
                     F.from_json("stages", STAGE).alias("stages"),
                     F.col("customer_minutes").cast("double").alias("customer_minutes"),
                     F.get_json_object("weather", "$.gust_kmh").cast("double").alias("gust_kmh"),
                     F.get_json_object("weather", "$.rain_mm").cast("double").alias("rain_mm"),
                     F.get_json_object("weather", "$.tmax_c").cast("double").alias("tmax_c"))
            .dropDuplicates(["outage_id"])
            .withColumn("duration_min", (F.unix_timestamp("end_time") - F.unix_timestamp("start_time")) / 60.0)
            .withColumn("outage_date", F.to_date("start_time"))
            .withColumn("zs_id", F.concat(F.lit("ZS-"), F.substring("feeder_id", 1, 3))))


OPEN_STAGE = T.ArrayType(T.StructType([T.StructField("ts", T.StringType()), T.StructField("action", T.StringType()),
                                       T.StructField("device", T.StringType()), T.StructField("customers_restored", T.LongType()),
                                       T.StructField("note", T.StringType())]))


@dp.materialized_view(comment="ADMS active incidents typed: each outage still in progress at the extract (the as-of moment), its device, fault, "
                              "customers still off, estimated restoration, the work so far as stages, and the patrol crew's note.")
def slv_open_outage():
    o = spark.read.table("brz_adms_open_outage")
    ts = lambda c: F.to_timestamp(c).alias(c)  # noqa: E731
    return (o.select("outage_id", "status", ts("snapshot_time"), "feeder_id", "device_id", "device_type", "span_id", "fault_asset_id", "fault_kind",
                     F.col("fault_lat").cast("double").alias("lat"), F.col("fault_lon").cast("double").alias("lon"), "cause_code",
                     ts("start_time"), ts("etr"),
                     *[F.col(c).cast("long").alias(c) for c in ("customers_interrupted", "customers_off", "life_support_affected", "life_support_off")],
                     "repair_job_type", F.col("repair_minutes").cast("int").alias("repair_minutes"), "access_status",
                     F.from_json("hazards", "array<string>").alias("hazards"),
                     F.from_json("materials", "array<struct<code:string,qty:int>>").alias("materials"),
                     F.from_json("stages", OPEN_STAGE).alias("stages"), F.nullif(F.col("crew_id"), F.lit("")).alias("crew_id"), "crew_note")
            .dropDuplicates(["outage_id"])
            .withColumn("zs_id", F.concat(F.lit("ZS-"), F.substring("feeder_id", 1, 3))))


# ------------------------------------------------------------------ smart meter intervals (NEM12 parser)
@dp.materialized_view(comment="Interval energy per NMI, register (E1 import, B1 export) and 30-minute interval, parsed from NEM12 "
                              "files: each 300 record takes its NMI and register from the preceding 200 record.")
def slv_meter_interval():
    f = spark.read.table("brz_nem12_file")
    lines = f.select("_source_file", F.posexplode(F.split("value", "\n")).alias("pos", "line")).where("line <> ''")
    lines = lines.withColumn("p", F.split("line", ",")).withColumn("rec", F.col("p")[0])
    w = Window.partitionBy("_source_file").orderBy("pos").rowsBetween(Window.unboundedPreceding, 0)
    lines = (lines.withColumn("nmi", F.last(F.when(F.col("rec") == "200", F.col("p")[1]), True).over(w))
             .withColumn("register", F.last(F.when(F.col("rec") == "200", F.col("p")[3]), True).over(w))
             .withColumn("interval_len", F.last(F.when(F.col("rec") == "200", F.col("p")[8]), True).over(w).cast("int")))
    d = lines.where("rec = '300'").withColumn("n", (1440 / F.col("interval_len")).cast("int"))
    d = d.select("_source_file", "nmi", "register", F.to_date(F.col("p")[1], "yyyyMMdd").alias("interval_date"),
                 F.element_at("p", F.col("n") + 3).alias("quality"),
                 F.posexplode(F.slice("p", 3, F.col("n"))).alias("i", "kwh_s"), "interval_len")
    return (d.select("nmi", "register", "interval_date", (F.col("i") + 1).alias("interval_no"), F.col("kwh_s").cast("double").alias("kwh"),
                     "quality", "_source_file")
            .withColumn("interval_end", F.expr("timestampadd(MINUTE, interval_no * 30, cast(interval_date as timestamp))")))


@dp.materialized_view(comment="Metering coordinator voltage service, typed (5-minute average volts per meter).")
def slv_meter_voltage():
    return (spark.read.table("brz_meter_voltage").select("nmi", F.to_timestamp("interval_end").alias("interval_end"),
                                                          F.col("v_avg").cast("double").alias("v_avg")))


# ------------------------------------------------------------------ vegetation and drone
@dp.materialized_view(comment="LiDAR clearance per span and campaign, with the change since the previous campaign (grow-in).")
def slv_lidar_span():
    w = Window.partitionBy("span_id").orderBy("campaign_year")
    return (spark.read.table("brz_lidar_span")
            .select("span_id", F.col("feeder").alias("feeder_id"), "campaign_year", F.to_date("capture_date").alias("capture_date"),
                    F.col("min_vegetation_clearance_m").cast("double").alias("clearance_m"), "encroachment_category",
                    "dominant_species", F.col("estimated_growth_m_per_year").cast("double").alias("growth_m_per_year"),
                    (F.col("hazard_tree_flag") == "1").alias("hazard_tree"), F.col("pole_lean_deg").cast("double").alias("pole_lean_deg"),
                    (F.col("bushfire_prone_land") == "1").alias("bushfire_prone"))
            .withColumn("clearance_change_m", F.col("clearance_m") - F.lag("clearance_m").over(w)))


@dp.materialized_view(comment="Drone detections joined to their image metadata, with the patrol they came from.")
def slv_drone_detection():
    d = spark.read.table("brz_drone_coco")
    imgs = d.select(F.explode("images").alias("i"), "_source_file").select("i.*", "_source_file")
    cats = d.select(F.explode("categories").alias("c")).select("c.*").dropDuplicates(["id"])
    ann = d.select(F.explode("annotations").alias("a"), "_source_file").select("a.*", "_source_file")
    return (ann.join(imgs.withColumnRenamed("id", "image_id"), ["image_id", "_source_file"])
            .join(cats.withColumnRenamed("id", "category_id").withColumnRenamed("name", "defect_class"), "category_id")
            .select(F.concat(F.lit("DET-"), F.col("file_name"), F.lit("-"), F.col("id").cast("string")).alias("detection_id"),
                    "file_name", F.to_timestamp("date_captured").alias("captured_at"), "defect_class", "score", "bbox",
                    F.col("gps_latitude").alias("lat"), F.col("gps_longitude").alias("lon"), "_source_file")
            .withColumn("geom", F.expr("st_point(lon, lat, 4326)")))


# ------------------------------------------------------------------ defects and work
@dp.materialized_view(comment="Open and closed defects (SAP notifications) on network equipment, with priority and age.")
def slv_defect():
    q = spark.read.table("slv_sap_qmel")
    h = spark.read.table("slv_sap_qmih")
    return (q.join(h, "QMNUM").select(F.col("QMNUM").alias("notification_id"), F.col("INVNR").alias("asset_id"),
                                      F.col("FECOD").alias("defect_code"), F.col("QMTXT").alias("description"),
                                      F.to_date("QMDAT", "yyyyMMdd").alias("created_date"), F.col("PRIOK").cast("int").alias("priority"),
                                      (F.col("STAT") == "OSNO").alias("is_open"))
            .withColumn("age_days", F.datediff(F.lit(AS_OF), "created_date")))


@dp.materialized_view(comment="Work orders (SAP orders) with type, linked notification, dates, status and cost.")
def slv_work_order():
    a = spark.read.table("slv_sap_aufk")
    return a.select(F.col("AUFNR").alias("order_id"), F.col("AUART").alias("order_type"), F.col("KTEXT").alias("description"),
                    F.col("QMNUM").alias("notification_id"), F.col("INVNR").alias("asset_id"),
                    F.to_date("ERDAT", "yyyyMMdd").alias("created_date"), F.to_date("IDAT2", "yyyyMMdd").alias("completed_date"),
                    F.col("STAT").alias("status"), F.col("COST_AUD").cast("double").alias("cost_aud"))


# ------------------------------------------------------------------ workforce
def _c(col, name, text, cast=None):
    e = F.col(col).cast(cast) if cast else F.col(col)
    return e.alias(name, metadata={"comment": text})


@dp.materialized_view(comment="Crews: one deployable unit (a team and its vehicle) named by its leader, with persona, home region and depot, "
                              "employment, fully loaded hourly cost and the patch it works from.")
def slv_crew():
    return spark.read.table("brz_wf_crews").select(
        _c("crew_id", "crew_id", "Crew id: region, persona code and number, e.g. PEN-LN-07."),
        _c("crew_name", "crew_name", "Crew leader's name; the crew is known by it."),
        _c("persona", "persona", "Trade: Linesperson, Cable Jointer, Substation Technician, Protection Technician, Vegetation Crew, "
                                 "Asset Inspector / Drone, Civil Crew or Metering Technician."),
        _c("primary_skill", "primary_skill", "Main skill code (see ref_label, domain skill)."),
        _c("region", "region_id", "Home region, which is its depot (see gld_rm_region)."),
        _c("depot", "depot_id", "Depot the crew starts from."),
        _c("area", "area", "Area code (see ref_label, domain area)."),
        _c("employment_type", "employment_type", "Internal (the distributor's own staff) or Contractor (labour hire, at a premium)."),
        _c("hourly_cost_aud", "hourly_cost_aud", "Fully loaded cost of the crew and its vehicle, AUD per hour.", "double"),
        _c("home_lat", "home_lat", "Latitude of the patch the crew starts its day from.", "double"),
        _c("home_lon", "home_lon", "Longitude of the patch the crew starts its day from.", "double"),
        _c("crew_size", "crew_size", "People in the crew.", "int"),
        _c("business_unit", "business_unit", "Business unit the crew reports to."),
        _c("patch_feeder", "patch_feeder", "Feeder whose area the crew is based in."),
        (F.col("active") == "Y").alias("active", metadata={"comment": "Whether the crew is on the roster."}))


@dp.materialized_view(comment="Crew skills (primary and secondary), one row per crew and skill.")
def slv_crew_skill():
    return spark.read.table("brz_wf_crew_skills").select(
        _c("crew_id", "crew_id", "Crew id."), _c("skill_code", "skill_code", "Skill code (see ref_label, domain skill)."),
        (F.col("is_primary") == "Y").alias("is_primary", metadata={"comment": "The crew's main skill."}))


@dp.materialized_view(comment="Crew tickets and licences with issue and expiry dates, days to expiry and status (expired, expiring within "
                              "45 days, current) as of the data's day.")
def slv_crew_ticket():
    t = spark.read.table("brz_wf_crew_tickets")
    d = F.datediff(F.to_date("expiry_date"), F.lit(AS_OF))
    return t.select(
        _c("crew_id", "crew_id", "Crew id."), _c("ticket_code", "ticket_code", "Ticket or licence code (see ref_label, domain ticket)."),
        _c("holder_name", "holder_name", "Who holds it (the crew leader)."),
        F.to_date("issued_date").alias("issued_date", metadata={"comment": "Issue date."}),
        F.to_date("expiry_date").alias("expiry_date", metadata={"comment": "Expiry date."}),
        d.alias("days_to_expiry", metadata={"comment": "Days until expiry (negative once expired)."}),
        (d < 0).alias("is_expired", metadata={"comment": "Expired: the crew cannot be rostered to work that needs it."}),
        F.when(d < 0, "expired").when(d <= 45, "expiring").otherwise("current").alias(
            "status", metadata={"comment": "expired, expiring (within 45 days) or current."}))


@dp.materialized_view(comment="Crew leave per day (annual, personal, rostered day off) with approval status.")
def slv_crew_leave():
    return spark.read.table("brz_wf_crew_leave").select(
        _c("crew_id", "crew_id", "Crew id."), F.to_date("leave_date").alias("leave_date", metadata={"comment": "Day of leave."}),
        _c("leave_type", "leave_type", "Leave type code (see ref_label, domain leave_type)."),
        _c("status", "status", "Approved or Pending."))


@dp.materialized_view(comment="Crew training bookings per day.")
def slv_crew_training():
    return spark.read.table("brz_wf_crew_training").select(
        _c("crew_id", "crew_id", "Crew id."), F.to_date("training_date").alias("training_date", metadata={"comment": "Day of training."}),
        _c("course_name", "course_name", "Course."), _c("duration_hours", "duration_hours", "Hours booked.", "int"))


@dp.materialized_view(comment="Safety events (incidents, near misses, hazards) from the safety management system. An open event attributed to "
                              "a crew keeps that crew off the roster unless a supervisor authorises it.")
def slv_safety_event():
    return spark.read.table("brz_wf_safety_events").select(
        _c("event_id", "event_id", "Event id."), F.to_date("event_date").alias("event_date", metadata={"comment": "Day reported."}),
        F.nullif(F.col("crew_id"), F.lit("")).alias("crew_id", metadata={"comment": "Crew involved, if attributed."}),
        _c("depot", "depot_id", "Depot."), _c("event_type", "event_type", "Event type code (see ref_label, domain event_type)."),
        _c("equipment_class", "equipment_class", "Equipment involved."), _c("severity", "severity", "Severity 1 (minor) to 4 (serious).", "int"),
        _c("status", "status", "Open or Closed."))


@dp.materialized_view(comment="Enterprise agreement working-time rules per skill.")
def slv_eba_rule():
    return spark.read.table("brz_wf_eba_rules").select(
        _c("role", "role", "Skill code the rule applies to."),
        _c("max_hours_per_day", "max_hours_per_day", "Daily cap on hours, including travel.", "double"),
        _c("max_consecutive_days", "max_consecutive_days", "Most days in a row.", "int"),
        _c("min_rest_hours", "min_rest_hours", "Minimum rest between shifts, hours.", "double"),
        _c("ot_multiplier", "ot_multiplier", "Overtime pay multiplier.", "double"),
        _c("max_travel_as_work_hours", "max_travel_as_work_hours", "Travel counted as work, hours per day.", "double"))


@dp.materialized_view(comment="Depots: the region each one runs and the zone substations it serves.")
def slv_depot():
    return spark.read.table("brz_wf_depots").select(
        _c("depot", "depot_id", "Depot code; also the region id."), _c("name", "depot_name", "Depot name."),
        _c("region_name", "region_name", "Region name."), _c("area", "area", "Area code (see ref_label, domain area)."),
        _c("latitude", "lat", "Latitude.", "double"), _c("longitude", "lon", "Longitude.", "double"),
        F.split("zone_substations", ";").alias("zone_substations", metadata={"comment": "Zone substations whose feeders the region serves."}))


@dp.materialized_view(comment="Planned field work orders from the work management system, conformed: type, priority, duration, time window, "
                              "location on the network, skill and ticket required, funding, customers affected and schedule-of-rates price.")
def slv_field_job():
    w = spark.read.table("brz_wms_work_orders")
    mins = lambda col: (F.split(col, ":")[0].cast("int") * 60 + F.split(col, ":")[1].cast("int"))  # noqa: E731
    blank = lambda col, name, text: F.nullif(F.col(col), F.lit("")).alias(name, metadata={"comment": text})  # noqa: E731
    return w.select(
        _c("order_id", "job_id", "Work order number."),
        F.to_date("planned_date").alias("job_date", metadata={"comment": "Day the work is planned for."}),
        _c("work_type", "job_type", "Job type code (see ref_label, domain job_type)."), _c("description", "title", "What the job is."),
        _c("priority", "priority", "Priority 1 (low) to 5 (urgent).", "int"), _c("est_duration_min", "duration_min", "On-site minutes.", "int"),
        mins("window_start").alias("window_start_min", metadata={"comment": "Earliest start, minutes after midnight."}),
        mins("window_end").alias("window_end_min", metadata={"comment": "Latest finish, minutes after midnight."}),
        _c("depot", "region_id", "Region (depot) the job is in."), _c("depot", "depot_id", "Depot that plans it."),
        blank("feeder_id", "feeder_id", "Feeder the job is on."), blank("asset_id", "asset_id", "Pole, span, cable, substation or device."),
        blank("nmi", "nmi", "Customer premises, for customer jobs."), blank("address", "address", "Street address, for customer jobs."),
        blank("suburb", "suburb", "Suburb, for customer jobs."),
        _c("latitude", "lat", "Latitude.", "double"), _c("longitude", "lon", "Longitude.", "double"),
        _c("required_skill", "required_skill", "Skill code the crew must hold (see ref_label, domain skill)."),
        _c("required_ticket", "required_ticket", "Ticket the crew must hold, in date (see ref_label, domain ticket)."),
        _c("funding_code", "funding", "Who pays: funding code (see ref_label, domain funding)."),
        _c("customers_affected", "customers", "Customers downstream of the asset (1 for a customer premises).", "int"),
        _c("customer_minutes_at_risk", "est_customer_minutes", "Expected customer minutes off supply if the job slips.", "int"),
        _c("schedule_rate_aud_per_hr", "bill_rate_per_hr", "Schedule-of-rates price, AUD per hour on site and travelling.", "double"),
        _c("source", "source", "Where the work came from (see ref_label, domain source)."),
        blank("notification_id", "notification_id", "SAP notification the job rectifies, for defects."))


@dp.materialized_view(comment="Fleet: vehicles and plant by crew and depot.")
def slv_vehicle():
    return spark.read.table("brz_wf_vehicles").select(
        _c("vehicle_id", "vehicle_id", "Vehicle id."), _c("rego", "rego", "Registration."),
        _c("vehicle_type", "vehicle_type", "Vehicle type code (see ref_label, domain vehicle_type)."),
        F.nullif(F.col("crew_id"), F.lit("")).alias("crew_id", metadata={"comment": "Crew it is allocated to (none for pool plant)."}),
        _c("depot", "depot", "Depot it is kept at."))


# ------------------------------------------------------------------ real public feeds (latest capture, parsed)
def _f(x):
    return None if x is None else float(x)


def _latest(tbl):
    w = Window.orderBy(F.col("_source_file").desc())
    return spark.read.table(tbl).withColumn("rn", F.row_number().over(w)).where("rn = 1").drop("rn")


@dp.materialized_view(comment="Live NSW RFS incidents from the latest capture: alert level, status, type, size, geometry (point or "
                              "fire perimeter). Real public data, CC BY 4.0, State of NSW (NSW RFS).")
def slv_fire_incident():
    raw = _latest("brz_feed_rfs_incidents").collect()
    rows = []
    for r in raw:
        cap = re.search(r"(\d{8}T\d{6}Z)", r["_source_file"]).group(1)
        for f in json.loads(r["value"]).get("features", []):
            p = f.get("properties", {})
            desc = p.get("description", "")
            fields = dict(re.findall(r"([A-Z ]+): (.*?)(?: <br />|$)", desc))
            g = f.get("geometry") or {}
            polys = [x for x in g.get("geometries", [g]) if x.get("type") in ("Polygon", "MultiPolygon")]
            pts = [x for x in g.get("geometries", [g]) if x.get("type") == "Point"]
            rows.append((p.get("guid"), p.get("title"), p.get("category"), fields.get("STATUS"), fields.get("TYPE"),
                         fields.get("SIZE"), fields.get("COUNCIL AREA"), fields.get("RESPONSIBLE AGENCY"), fields.get("UPDATED"),
                         json.dumps(pts[0]) if pts else None, json.dumps(polys[0]) if polys else None, desc, cap))
    schema = ("incident_id string, title string, alert_level string, status string, incident_type string, size string, "
              "council_area string, agency string, updated string, point_geojson string, perimeter_geojson string, "
              "description string, captured_at string")
    df = spark.createDataFrame(rows, schema)
    return (df.withColumn("geom", F.expr("st_geomfromgeojson(point_geojson)"))
            .withColumn("perimeter", F.expr("CASE WHEN perimeter_geojson IS NOT NULL THEN st_geomfromgeojson(perimeter_geojson) END"))
            .withColumn("lon", F.expr("st_x(geom)")).withColumn("lat", F.expr("st_y(geom)"))
            .withColumn("size_ha", F.regexp_extract("size", r"([\d.]+)", 1).cast("double")))


@dp.materialized_view(comment="Today's and tomorrow's fire danger rating and total fire ban per NSW fire weather district (latest "
                              "capture). Real public data from the NSW RFS (ratings from the Bureau of Meteorology).")
def slv_fire_danger():
    raw = _latest("brz_feed_rfs_fire_danger").collect()
    rows = []
    for r in raw:
        cap = re.search(r"(\d{8}T\d{6}Z)", r["_source_file"]).group(1)
        root = ET.fromstring(r["value"])
        for d in root.findall("District"):
            g = lambda k: (d.findtext(k) or "").strip()  # noqa: E731
            rows.append((g("Name"), int(g("RegionNumber") or 0), g("Councils"), g("DangerLevelToday"), g("DangerLevelTomorrow"),
                         g("FireBanToday") == "Yes", g("FireBanTomorrow") == "Yes", cap))
    return spark.createDataFrame(rows, "district string, region_number int, councils string, rating_today string, "
                                       "rating_tomorrow string, fire_ban_today boolean, fire_ban_tomorrow boolean, captured_at string")


@dp.materialized_view(comment="Satellite thermal hotspots over the footprint in the last three days (latest capture). Real public "
                              "data, DEA Hotspots, Geoscience Australia, CC BY 4.0. Not for safety-of-life decisions.")
def slv_hotspot():
    raw = _latest("brz_feed_dea_hotspots").collect()
    rows = []
    for r in raw:
        for f in json.loads(r["value"]).get("features", []):
            p = f["properties"]
            rows.append((str(p.get("id")), p.get("satellite"), p.get("sensor"), p.get("datetime"), float(p.get("latitude")),
                         float(p.get("longitude")), _f(p.get("temp_kelvin")), _f(p.get("power")), _f(p.get("confidence")),
                         _f(p.get("hours_since_hotspot"))))
    schema = ("hotspot_id string, satellite string, sensor string, observed_at string, lat double, lon double, temp_kelvin double, "
              "power_mw double, confidence double, hours_since double")
    return spark.createDataFrame(rows, schema).withColumn("geom", F.expr("st_point(lon, lat, 4326)"))


@dp.materialized_view(comment="Hourly forecast per grid point over the footprint from the latest Open-Meteo capture (real, CC BY 4.0), "
                              "with the H3 cell of each point.")
def slv_weather_forecast():
    raw = _latest("brz_feed_forecast").collect()
    rows = []
    for r in raw:
        cap = re.search(r"(\d{8}T\d{6}Z)", r["_source_file"]).group(1)
        pts = json.loads(r["value"])
        pts = pts if isinstance(pts, list) else [pts]
        for p in pts:
            h = p["hourly"]
            for i, t in enumerate(h["time"]):
                rows.append((round(p["latitude"], 3), round(p["longitude"], 3), t, _f(h["wind_gusts_10m"][i]), _f(h["wind_speed_10m"][i]),
                             _f(h["wind_direction_10m"][i]), _f(h["precipitation"][i]), _f(h["temperature_2m"][i]),
                             _f(h["relative_humidity_2m"][i]), _f(h["cloud_cover"][i]), _f(h["shortwave_radiation"][i]), cap))
    schema = ("lat double, lon double, forecast_time string, gust_kmh double, wind_kmh double, wind_dir_deg double, rain_mm double, "
              "temp_c double, rh_pct double, cloud_pct double, radiation_wm2 double, captured_at string")
    return (spark.createDataFrame(rows, schema).withColumn("forecast_time", F.to_timestamp("forecast_time"))
            .withColumn("h3_r7", F.expr("h3_longlatash3(lon, lat, 7)")).withColumn("h3_r5", F.expr("h3_longlatash3(lon, lat, 5)")))


# ------------------------------------------------------------------ live streams (typed)
@dp.materialized_view(comment="Live smart meter power outage and restoration alarms.")
def slv_meter_event():
    return spark.read.table("brz_meter_event")


@dp.materialized_view(comment="Live contact centre and IVR outage calls, one row per call (a call landed twice is kept once).")
def slv_call():
    return spark.read.table("brz_call").dropDuplicates(["call_id"])


# ------------------------------------------------------------------ the app's records, current state (Lakebase change feed)
# Postgres table (schema, name) -> primary key, and its columns as the change feed lands them: TIMESTAMPTZ as TIMESTAMP, JSONB and TEXT[] as
# STRING. The fed tables are the atlas and rm schemas only (the app's app schema is not fed). Each table's history is
# <atlas.schema>_lb_<postgres schema>.lb_<name>_history (change_feed_table); it appears once the table has a row.
APP = {
    ("atlas", "situation"): (["situation_id"], """
        situation_id STRING COMMENT 'Situation id, e.g. SIT-...', type STRING COMMENT 'storm, fire, trip, heat, min_load, vegetation, drone_defect, fatigue or records',
        title STRING, severity STRING COMMENT 'critical, high, medium or info', status STRING COMMENT 'detected, plan_ready, decided, executing or closed',
        detected_at TIMESTAMP, updated_at TIMESTAMP, source STRING COMMENT 'What raised it: the detector (the live engine and its rules).',
        feeders STRING COMMENT 'Feeders in the area, as the change feed lands the array',
        customers INT, life_support INT, critical_sites INT, headline STRING COMMENT 'One line for the card',
        payload STRING COMMENT 'Situation detail as JSON', owner STRING"""),
    ("atlas", "recommendation"): (["rec_id"], """
        rec_id STRING, situation_id STRING, kind STRING COMMENT 'Kind of action, e.g. notify, dispatch, switching, patrol.', title STRING,
        detail STRING, payload STRING COMMENT 'Recommendation payload as JSON', status STRING COMMENT 'proposed, accepted, adjusted or rejected.',
        created_at TIMESTAMP COMMENT 'When it was proposed.', decided_at TIMESTAMP COMMENT 'When it was decided.',
        decided_by STRING COMMENT 'Who decided it.', reason STRING COMMENT 'Reason given for the decision.'"""),
    ("atlas", "notification_outbox"): (["msg_id"], """
        msg_id STRING, situation_id STRING, rec_id STRING COMMENT 'The notify recommendation these messages belong to.', nmi STRING COMMENT 'Customer NMI.',
        audience STRING COMMENT 'life_support or customer.', channel STRING COMMENT 'Channel of the message (sms, email, facebook).', body STRING,
        status STRING COMMENT 'draft, queued, sent or delivered (delivery is simulated).', created_at TIMESTAMP, sent_at TIMESTAMP"""),
    ("atlas", "live_outage"): (["outage_id"], """
        outage_id STRING, feeder_id STRING, device_id STRING, start_time TIMESTAMP, status STRING COMMENT 'active or restored.',
        customers_off INT, customers_initial INT, life_support_off INT, cause_code STRING, etr TIMESTAMP COMMENT 'Estimated time of restoration.',
        stages STRING COMMENT 'FLISR and repair stages as JSON', situation_id STRING, crew_id STRING, lat DOUBLE, lon DOUBLE,
        restored_at TIMESTAMP"""),
    ("atlas", "audit_log"): (["id"], """
        id BIGINT COMMENT 'Entry id.', ts TIMESTAMP, actor STRING, action STRING, entity STRING COMMENT 'What it changed, e.g. a job or a situation.',
        detail STRING COMMENT 'Detail as JSON'"""),
    ("rm", "raised_job"): (["job_id"], """
        job_id STRING, job_date DATE, job_type STRING COMMENT 'Job type code (ref_label, domain job_type).', title STRING, priority INT COMMENT '1 (low) to 5 (urgent).',
        duration_min INT COMMENT 'On-site minutes.', window_start_min INT COMMENT 'Earliest start, minutes after midnight.',
        window_end_min INT COMMENT 'Latest finish, minutes after midnight.', region_id STRING, depot_id STRING, lat DOUBLE, lon DOUBLE,
        required_skill STRING, required_ticket STRING, funding STRING COMMENT 'Who pays (ref_label, domain funding).', feeder_id STRING,
        asset_id STRING, nmi STRING, address STRING, suburb STRING, customers INT, est_customer_minutes INT,
        source STRING COMMENT 'Where the work came from (ref_label, domain source).', situation_id STRING, is_emergency BOOLEAN,
        created_at TIMESTAMP"""),
    ("rm", "parked"): (["region_id", "job_id"], """
        region_id STRING, job_id STRING, payload STRING COMMENT 'Stop, reason and time parked, as JSON'"""),
    ("rm", "assignment_override"): (["region_id", "job_id"], """
        region_id STRING, job_id STRING, payload STRING COMMENT 'Crew that owns the job (crew_id, crew_name), travel, cost and breaches, as JSON'"""),
    ("rm", "field_status"):(["region_id", "job_id"], """
        region_id STRING, job_id STRING, status STRING COMMENT 'planned, driving, in_progress, done or parked.'"""),
    ("rm", "crew_live"): (["crew_id"], """
        crew_id STRING, lat DOUBLE, lon DOUBLE, status STRING COMMENT 'travelling, on_site or available.', job_id STRING,
        since TIMESTAMP COMMENT 'When the crew reached this status.', updated_at TIMESTAMP"""),
    ("atlas", "notice"): (["notice_id"], """
        notice_id STRING, notice_type STRING COMMENT 'planned_work, network_fault, project_work or severe_weather (ref_label notice_type).',
        trigger_kind STRING, trigger_ref STRING COMMENT 'The planned job, outage or forecast behind the notice.', title STRING, description STRING,
        area_code STRING, feeders STRING COMMENT 'Feeders in scope, as the change feed lands the array', suburbs STRING,
        scope STRING COMMENT 'Who is in scope, as JSON', window_start TIMESTAMP, window_end TIMESTAMP COMMENT 'The interruption or event window.',
        status STRING COMMENT 'draft, in_review or sent (ref_label notice_status).', rules STRING COMMENT 'Business rules applied, as JSON',
        counts STRING COMMENT 'Customers reached and excluded, as JSON', ai_recommendation STRING COMMENT 'The model channel recommendation, as JSON',
        ai_reason STRING, ai_drafts STRING COMMENT 'Model drafts per audience and channel, as JSON', drafts STRING COMMENT 'Drafts after review, as JSON',
        edits STRING COMMENT 'Reviewer edits, as JSON', model STRING, send_at TIMESTAMP, created_by STRING, created_at TIMESTAMP,
        reviewed_by STRING, reviewed_at TIMESTAMP, sent_by STRING COMMENT 'The person who pressed send.', sent_at TIMESTAMP"""),
    ("rm", "field_report"): (["report_id"], """
        report_id STRING, job_id STRING, situation_id STRING, outage_id STRING, region_id STRING, crew_id STRING, asset_id STRING,
        reported_at TIMESTAMP, reported_by STRING, arrived_at TIMESTAMP, response_note STRING COMMENT 'How the crew responded.',
        patrol_complete BOOLEAN, fault_located BOOLEAN, fault_kind STRING, fault_ref STRING COMMENT 'Pole or span where the fault was found.',
        segments_total INT, segments_cleared INT, switching_executed BOOLEAN, customers_restored INT, access_status STRING,
        hazards STRING COMMENT 'Hazards found, as the change feed lands the array', materials STRING COMMENT 'Materials used, as JSON',
        notes STRING, outcome STRING COMMENT 'fixed, temporary repair or needs follow up.', photo_ids STRING"""),
    ("atlas", "asset_photo"): (["photo_id"], """
        photo_id STRING, asset_id STRING, asset_name STRING, tag_kind STRING, source STRING COMMENT 'field (the crew app).', ref_id STRING,
        note STRING, mime STRING, width_px INT, height_px INT, bytes_len INT, status STRING COMMENT 'Review status (ref_label asset_photo_status).',
        ai_status STRING, ai_result STRING COMMENT 'What the vision model found, as JSON', ai_error STRING, ai_at TIMESTAMP, submitted_by STRING,
        submitted_at TIMESTAMP, reviewed_by STRING, reviewed_at TIMESTAMP, review_decision STRING COMMENT 'fixed, updated or rejected.',
        review_notes STRING, image_deleted BOOLEAN COMMENT 'The photo bytes were deleted on rejection.'"""),
    ("rm", "veg_bay_task"): (["task_id"], """
        task_id STRING, package_id STRING, bay_id STRING, bay_label STRING, feeder_id STRING, zs_id STRING, crew_id STRING COMMENT 'Contractor crew.',
        status STRING COMMENT 'issued, in_progress, awaiting_audit or closed (ref_label veg_task_status).', risk_tier_at_issue STRING,
        required_clearance_m DOUBLE, clearance_before_m DOUBLE, attempt INT, due_date DATE, issued_by STRING, issued_at TIMESTAMP,
        started_at TIMESTAMP, completed_at TIMESTAMP, closed_at TIMESTAMP, updated_at TIMESTAMP"""),
}


def _app_current(schema, table, keys, cols):
    """The latest version of each record: the last insert or update (or a delete, which drops the key), in change order."""
    target = T.StructType.fromDDL(" ".join(cols.split()))
    try:
        hist = spark.read.table(change_feed_table(CAT, SCH, schema, table))
    except AnalysisException:
        return spark.createDataFrame([], target)
    w = Window.partitionBy(*keys).orderBy(F.col("_sort_by").desc())
    last = (hist.where(F.col("_pg_change_type") != "update_preimage")
            .withColumn("_rn", F.row_number().over(w)).where("_rn = 1 AND _pg_change_type <> 'delete'"))
    return last.select(*[F.col(f.name).cast(f.dataType).alias(f.name, metadata=f.metadata) for f in target.fields])


for (_schema, _table), (_keys, _cols) in APP.items():
    def _mk_app(schema=_schema, table=_table, keys=_keys, cols=_cols):
        @dp.materialized_view(name=f"slv_app_{table}", comment=f"Current state of {schema}.{table} in the app's Lakebase project (one row per "
                                                               f"{'/'.join(keys)}, deleted records dropped).")
        def _f():
            return _app_current(schema, table, keys, cols)
    _mk_app()
