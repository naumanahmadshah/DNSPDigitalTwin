"""Write the simulated network in the native shapes of the systems a distributor runs.

- Network connectivity model: CIM RDF/XML (IEC 61970/61968 classes), one file per zone substation.
- GIS: a GeoPackage (OGC, SQLite) with one feature table per class, GDA2020 (EPSG:7844).
- Asset register: the SAP PM equipment master (EQUI) in an ODP-style change-file layout (ODQ_CHANGEMODE, ODQ_ENTITYCNTR).
- Market standing data: MSATS-style CATS_NMI_DATA; distributor life support register; CRM contact preferences.
- DER register extract and public EV charging sites. The workforce extracts are written by workforce.py.
Stdlib only. Paths may be local or a Unity Catalog volume (/Volumes/...).
"""
import csv
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from xml.sax.saxutils import escape

from network import rng_for

CIM_NS = "http://iec.ch/TC57/CIM100#"


def _mkdir(path):
    os.makedirs(path, exist_ok=True)


def _write_csv(path, header, rows):
    _mkdir(os.path.dirname(path))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------------------------------------- CIM
def write_cim(net, root):
    """CIM RDF/XML per zone substation. Customers are connected as the records say (recorded_ds_id)."""
    out = os.path.join(root, "ncm")
    _mkdir(out)
    feeders_by_zs = {}
    for f in net.feeders:
        feeders_by_zs.setdefault(f["zs_id"], []).append(f)
    nodes_by_feeder, spans_by_feeder, dev_by_feeder, ds_by_feeder = {}, {}, {}, {}
    for n in net.nodes:
        nodes_by_feeder.setdefault(n["feeder_id"], []).append(n)
    for s in net.spans:
        spans_by_feeder.setdefault(s["feeder_id"], []).append(s)
    for d in net.devices:
        dev_by_feeder.setdefault(d["feeder_id"], []).append(d)
    for d in net.dss:
        ds_by_feeder.setdefault(d["feeder_id"], []).append(d)
    nmis_by_ds = {}
    for m in net.nmis:
        nmis_by_ds.setdefault(m["recorded_ds_id"], []).append(m)
    files = []
    for z in net.zones:
        path = os.path.join(out, f"cim_{z['code']}.xml")
        with open(path, "w") as f:
            w = f.write
            w('<?xml version="1.0" encoding="UTF-8"?>\n')
            w(f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:cim="{CIM_NS}" '
              f'xmlns:md="http://iec.ch/TC57/61970-552/ModelDescription/1#">\n')
            w(f'<md:FullModel rdf:about="urn:uuid:ncm-{z["code"]}"><md:Model.created>{net.cfg["as_of"]}T00:00:00Z'
              f'</md:Model.created><md:Model.description>Network connectivity model export, {escape(z["name"])}'
              f'</md:Model.description></md:FullModel>\n')
            w(_obj("Substation", z["zs_id"], z["name"], extra=f'<cim:Substation.Region rdf:resource="#_{z["area"]}"/>'))
            for t in (1, 2):
                tid = f"{z['zs_id']}-TX{t}"
                w(_obj("PowerTransformer", tid, f"{z['name']} transformer {t}",
                       extra=f'<cim:Equipment.EquipmentContainer rdf:resource="#_{z["zs_id"]}"/>'))
            for fd in feeders_by_zs[z["zs_id"]]:
                fid = fd["feeder_id"]
                w(_obj("Feeder", fid, fd["name"], extra=f'<cim:Feeder.NormalEnergizingSubstation rdf:resource="#_{z["zs_id"]}"/>'))
                for n in nodes_by_feeder[fid]:
                    w(f'<cim:ConnectivityNode rdf:ID="_{n["node_id"]}"><cim:IdentifiedObject.mRID>{n["node_id"]}'
                      f'</cim:IdentifiedObject.mRID><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#_{fid}"/>'
                      f'</cim:ConnectivityNode>\n')
                # A switch splits its span: from-node -> switch -> intermediate node -> span -> to-node.
                switched = {}
                for d in dev_by_feeder.get(fid, []):
                    if d["span_id"]:
                        switched.setdefault(d["span_id"], []).append(d)
                for s in spans_by_feeder[fid]:
                    a = s["from_node"]
                    for d in switched.get(s["span_id"], []):
                        mid = f"{s['from_node']}-{d['device_id']}"
                        w(f'<cim:ConnectivityNode rdf:ID="_{mid}"><cim:IdentifiedObject.mRID>{mid}</cim:IdentifiedObject.mRID>'
                          f'<cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#_{fid}"/></cim:ConnectivityNode>\n')
                        cls = {"CB": "Breaker", "RECLOSER": "Recloser", "GAS_SWITCH": "LoadBreakSwitch", "FUSE": "Fuse",
                               "SWER_ISOLATOR": "Disconnector", "NOP": "LoadBreakSwitch"}[d["type"]]
                        no = "true" if d["normally_open"] else "false"
                        w(_obj(cls, d["device_id"], d["device_id"],
                               extra=f'<cim:Switch.normalOpen>{no}</cim:Switch.normalOpen><cim:Switch.open>{no}</cim:Switch.open>'
                                     f'<cim:Equipment.EquipmentContainer rdf:resource="#_{fid}"/>'))
                        w(_terms(d["device_id"], a, mid, "ABC"))
                        a = mid
                    w(_obj("ACLineSegment", s["span_id"], s["span_id"],
                           extra=f'<cim:Conductor.length>{s["length_m"]}</cim:Conductor.length>'
                                 f'<cim:Equipment.EquipmentContainer rdf:resource="#_{fid}"/>'))
                    w(_terms(s["span_id"], a, s["to_node"], s["phases"]))
                for d in ds_by_feeder.get(fid, []):
                    lv = f"{d['node_id']}-LV"
                    w(f'<cim:ConnectivityNode rdf:ID="_{lv}"><cim:IdentifiedObject.mRID>{lv}</cim:IdentifiedObject.mRID>'
                      f'<cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#_{fid}"/></cim:ConnectivityNode>\n')
                    w(_obj("PowerTransformer", d["ds_id"], d["ds_id"],
                           extra=f'<cim:Equipment.EquipmentContainer rdf:resource="#_{fid}"/>'))
                    w(_terms(d["ds_id"], d["node_id"], lv, "ABC"))
                    for m in nmis_by_ds.get(d["ds_id"], []):
                        ec = f"EC-{m['nmi']}"
                        w(_obj("EnergyConsumer", ec, m["nmi"], extra=f'<cim:Equipment.EquipmentContainer rdf:resource="#_{fid}"/>'))
                        w(f'<cim:Terminal rdf:ID="_{ec}_T1"><cim:ACDCTerminal.sequenceNumber>1</cim:ACDCTerminal.sequenceNumber>'
                          f'<cim:Terminal.ConductingEquipment rdf:resource="#_{ec}"/><cim:Terminal.ConnectivityNode rdf:resource="#_{lv}"/>'
                          f'<cim:Terminal.phases rdf:resource="{CIM_NS}PhaseCode.{"ABC" if m["phases"] == 3 else "A"}"/></cim:Terminal>\n')
                        w(f'<cim:UsagePoint rdf:ID="_UP-{m["nmi"]}"><cim:IdentifiedObject.mRID>UP-{m["nmi"]}</cim:IdentifiedObject.mRID>'
                          f'<cim:IdentifiedObject.name>{m["nmi"]}</cim:IdentifiedObject.name>'
                          f'<cim:UsagePoint.Equipments rdf:resource="#_{ec}"/></cim:UsagePoint>\n')
            w("</rdf:RDF>\n")
        files.append(path)
    for site in net.tss:   # one file per transmission substation: the site and its transformers (their lines are in the GIS)
        path = os.path.join(out, f"cim_{site['ts_id']}.xml")
        with open(path, "w") as f:
            w = f.write
            w('<?xml version="1.0" encoding="UTF-8"?>\n')
            w(f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:cim="{CIM_NS}" '
              f'xmlns:md="http://iec.ch/TC57/61970-552/ModelDescription/1#">\n')
            w(f'<md:FullModel rdf:about="urn:uuid:ncm-{site["ts_id"]}"><md:Model.created>{net.cfg["as_of"]}T00:00:00Z'
              f'</md:Model.created><md:Model.description>Network connectivity model export, {escape(site["name"])}'
              f'</md:Model.description></md:FullModel>\n')
            w(_obj("Substation", site["ts_id"], site["name"], extra=f'<cim:Substation.Region rdf:resource="#_{site["area"]}"/>'))
            for x in net.ts_transformers:
                if x["ts_id"] == site["ts_id"]:
                    w(_obj("PowerTransformer", x["asset_id"], x["name"],
                           extra=f'<cim:Equipment.EquipmentContainer rdf:resource="#_{site["ts_id"]}"/>'))
            w("</rdf:RDF>\n")
        files.append(path)
    return files


def _obj(cls, mrid, name, extra=""):
    return (f'<cim:{cls} rdf:ID="_{mrid}"><cim:IdentifiedObject.mRID>{mrid}</cim:IdentifiedObject.mRID>'
            f'<cim:IdentifiedObject.name>{escape(str(name))}</cim:IdentifiedObject.name>{extra}</cim:{cls}>\n')


def _terms(eq, n1, n2, phases):
    ph = {"ABC": "ABC", "AB": "AB", "A": "A"}[phases]
    out = []
    for i, n in ((1, n1), (2, n2)):
        out.append(f'<cim:Terminal rdf:ID="_{eq}_T{i}"><cim:ACDCTerminal.sequenceNumber>{i}</cim:ACDCTerminal.sequenceNumber>'
                   f'<cim:Terminal.ConductingEquipment rdf:resource="#_{eq}"/><cim:Terminal.ConnectivityNode rdf:resource="#_{n}"/>'
                   f'<cim:Terminal.phases rdf:resource="{CIM_NS}PhaseCode.{ph}"/></cim:Terminal>\n')
    return "".join(out)


# ---------------------------------------------------------------------------------------------- GeoPackage
GDA2020_WKT = ('GEOGCRS["GDA2020",DATUM["Geocentric Datum of Australia 2020",ELLIPSOID["GRS 1980",6378137,298.257222101]],'
               'CS[ellipsoidal,2],AXIS["latitude",north],AXIS["longitude",east],UNIT["degree",0.0174532925199433],ID["EPSG",7844]]')


def _gpkg_geom(wkb, srs_id=7844):
    # GeoPackage binary header: magic 'GP', version 0, flags (little endian, no envelope), srs_id.
    return b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", srs_id) + wkb


def _wkb_point(lon, lat):
    return struct.pack("<BIdd", 1, 1, lon, lat)


def _wkb_line(coords):
    return struct.pack("<BII", 1, 2, len(coords)) + b"".join(struct.pack("<dd", lon, lat) for lat, lon in coords)


def write_gpkg(net, root):
    """GeoPackage with the GIS view of the network (built locally, then copied, since SQLite needs random writes)."""
    out = os.path.join(root, "gis")
    _mkdir(out)
    tmp = os.path.join(tempfile.mkdtemp(), "network.gpkg")
    con = sqlite3.connect(tmp)
    c = con.cursor()
    c.execute("PRAGMA application_id = 1196444487")  # 'GPKG'
    c.execute("PRAGMA user_version = 10400")
    c.executescript("""
    CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
      organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
    CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '',
      last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
    CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
      srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
    """)
    c.execute("INSERT INTO gpkg_spatial_ref_sys VALUES ('Undefined cartesian SRS',-1,'NONE',-1,'undefined',NULL)")
    c.execute("INSERT INTO gpkg_spatial_ref_sys VALUES ('Undefined geographic SRS',0,'NONE',0,'undefined',NULL)")
    c.execute("INSERT INTO gpkg_spatial_ref_sys VALUES ('GDA2020',7844,'EPSG',7844,?,'Geocentric Datum of Australia 2020')", (GDA2020_WKT,))

    def table(name, gtype, cols, rows, desc):
        coldefs = ", ".join(f"{n} {t}" for n, t in cols)
        c.execute(f"CREATE TABLE {name} (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom {gtype}, {coldefs})")
        c.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, description, srs_id) VALUES (?,?,?,?,7844)",
                  (name, "features", name, desc))
        c.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, 7844, 0, 0)", (name, gtype))
        ph = ", ".join("?" * (len(cols) + 1))
        c.executemany(f"INSERT INTO {name} (geom, {', '.join(n for n, _ in cols)}) VALUES ({ph})", rows)

    poles = []
    for n in net.nodes:
        if n["pole_id"]:
            poles.append((_gpkg_geom(_wkb_point(n["lon"], n["lat"])), n["pole_id"], n["material"], n["height_m"],
                          n["install_year"], n["feeder_id"], n["ds_id"]))
    table("electric_pole", "POINT", [("pole_number", "TEXT"), ("material", "TEXT"), ("height_m", "REAL"),
                                     ("install_year", "INTEGER"), ("feeder", "TEXT"), ("substation_ref", "TEXT")],
          poles, "Poles (support structures)")
    spans = [(_gpkg_geom(_wkb_line(s["coords"])), s["span_id"], "OH" if s["overhead"] else "UG", s["conductor"], s["phases"],
              s["length_m"], s["install_year"], s["feeder_id"], s["role"].upper(), 1 if s["bushfire_prone"] else 0, s.get("rating_amps"))
             for s in net.spans]
    table("electric_conductor", "LINESTRING", [("conductor_id", "TEXT"), ("construction", "TEXT"), ("conductor_type", "TEXT"),
                                               ("phasing", "TEXT"), ("length_m", "REAL"), ("install_year", "INTEGER"),
                                               ("feeder", "TEXT"), ("circuit_role", "TEXT"), ("bushfire_prone_land", "INTEGER"),
                                               ("rating_amps", "INTEGER")],
          spans, "HV conductors (overhead spans and underground cable sections)")
    dss = [(_gpkg_geom(_wkb_point(d["lon"], d["lat"])), d["ds_id"], d["kind"].upper(), d["kva"], d["feeder_id"], d["pole_id"],
            d["year"]) for d in net.dss]
    table("electric_dist_substation", "POINT", [("substation_id", "TEXT"), ("substation_type", "TEXT"), ("rating_kva", "INTEGER"),
                                                ("feeder", "TEXT"), ("pole_number", "TEXT"), ("install_year", "INTEGER")],
          dss, "Distribution substations")
    devs = [(_gpkg_geom(_wkb_point(d["lon"], d["lat"])), d["device_id"], d["type"], d["make"], d["feeder_id"],
             1 if d["normally_open"] else 0, d.get("rating_amps")) for d in net.devices]
    table("electric_switch", "POINT", [("device_id", "TEXT"), ("device_type", "TEXT"), ("make", "TEXT"), ("feeder", "TEXT"),
                                       ("normally_open", "INTEGER"), ("rating_amps", "INTEGER")], devs, "Switching and protection devices")
    zs = [(_gpkg_geom(_wkb_point(z["lon"], z["lat"])), z["zs_id"], z["name"], z["kv_primary"], z["kv_secondary"], z["area"])
          for z in net.zones]
    table("electric_zone_substation", "POINT", [("substation_id", "TEXT"), ("name", "TEXT"), ("primary_kv", "INTEGER"),
                                                ("secondary_kv", "INTEGER"), ("region", "TEXT")], zs, "Zone substations")
    bsp = [(_gpkg_geom(_wkb_point(b["lon"], b["lat"])), b["bsp_id"], b["name"], b["area"], b["kv"], b["capacity_mva"], b["transformers"], b["firm_mva"])
           for b in net.bsps]
    table("electric_bulk_supply_point", "POINT", [("substation_id", "TEXT"), ("name", "TEXT"), ("region", "TEXT"), ("kv", "INTEGER"),
                                                  ("capacity_mva", "INTEGER"), ("transformers", "INTEGER"), ("firm_mva", "INTEGER")], bsp,
          "Bulk supply points (transmission connection points)")
    tss = [(_gpkg_geom(_wkb_point(t["lon"], t["lat"])), t["ts_id"], t["name"], t["area"], t["kv_primary"], t["kv_secondary"], t["n_transformers"],
            t["transformer_mva"], t["firm_mva"], t["bsp_id"], t["commissioned"]) for t in net.tss]
    table("electric_transmission_substation", "POINT", [("substation_id", "TEXT"), ("name", "TEXT"), ("region", "TEXT"), ("primary_kv", "INTEGER"),
                                                        ("secondary_kv", "INTEGER"), ("transformers", "INTEGER"),
                                                        ("transformer_rating_mva", "INTEGER"), ("firm_mva", "INTEGER"),
                                                        ("bulk_supply_point", "TEXT"), ("commissioned", "INTEGER")], tss,
          "Transmission substations (132 kV down to 33 or 66 kV) between the bulk supply points and the zone substations")
    txl = [(_gpkg_geom(_wkb_line(l["coords"])), l["line_id"], l["from_id"], l["to_id"], l["circuit"], l["kv"], l["route_km"],
            "OH" if l["overhead"] else "UG", l["install_year"], l["rating_mva"]) for l in net.tx_lines]
    table("electric_transmission_line", "LINESTRING", [("line_id", "TEXT"), ("from_substation", "TEXT"), ("to_substation", "TEXT"),
                                                       ("circuit", "INTEGER"), ("kv", "INTEGER"), ("route_km", "REAL"), ("construction", "TEXT"),
                                                       ("install_year", "INTEGER"), ("rating_mva", "INTEGER")], txl,
          "132 kV transmission circuits from the bulk supply points to the transmission substations")
    stx = [(_gpkg_geom(_wkb_line(l["coords"])), l["line_id"], l["from_id"], l["to_id"], l["circuit"], l["kv"], l["route_km"],
            "OH" if l["overhead"] else "UG", l["install_year"], l["rating_mva"]) for l in net.subtx_lines]
    table("electric_subtransmission_line", "LINESTRING", [("line_id", "TEXT"), ("from_substation", "TEXT"), ("to_substation", "TEXT"),
                                                          ("circuit", "INTEGER"), ("kv", "INTEGER"), ("route_km", "REAL"),
                                                          ("construction", "TEXT"), ("install_year", "INTEGER"), ("rating_mva", "INTEGER")], stx,
          "Subtransmission circuits from transmission substations to zone substations")
    # The GIS also holds service points; it shows the recorded (not necessarily true) transformer.
    sp = [(_gpkg_geom(_wkb_point(m["lon"], m["lat"])), m["nmi"], m["recorded_ds_id"], m["customer_type"], m["elevation_m"], m["street_id"])
          for m in net.nmis]
    table("electric_service_point", "POINT", [("nmi", "TEXT"), ("substation_ref", "TEXT"), ("service_class", "TEXT"),
                                              ("ground_elevation_m", "REAL"), ("street_ref", "TEXT")],
          sp, "Customer service points (ground elevation in metres above sea level)")
    con.commit()
    con.close()
    dest = os.path.join(out, "network.gpkg")
    shutil.copyfile(tmp, dest)
    return dest


# ---------------------------------------------------------------------------------------------- SAP PM
def write_sap(net, root, stamp):
    """Initial-load change file for EQUI (ODQ_CHANGEMODE C = create): the equipment master of zone transformers, switching devices,
    distribution substations and poles. The functional locations and equipment installations are not written: nothing reads them."""
    out = os.path.join(root, "sap")
    equi = []

    def eq(equnr, eqart, herst, year, invnr, desc):
        equi.append(["C", 0, equnr, "M", eqart, herst, year, f"{year}0701", invnr, desc[:40]])

    for t in net.zs_transformers:
        eq(t["equnr"], "ZTX", t["manufacturer"], t["year"], t["asset_id"], t["name"])
    for d in net.devices:
        eq(d["equnr"], {"CB": "CBR", "RECLOSER": "RCL", "GAS_SWITCH": "GSW", "FUSE": "FUS", "SWER_ISOLATOR": "ISO",
                        "NOP": "GSW"}[d["type"]], d["make"], d["year"], d["device_id"], f"{d['type'].replace('_', ' ').title()} {d['device_id']}")
    for d in net.dss:
        eq(d["equnr"], "DTX", "Wilson" if d["kva"] < 500 else "Schneider", d["year"], d["ds_id"], f"Distribution substation {d['ds_id']}")
    for n in net.nodes:
        if n["pole_id"]:
            eq(f"{int(n['pole_id'][1:]) + 30000000:018d}", "POL", n["material"].title(), n["install_year"], n["pole_id"], f"Pole {n['pole_id']}")
    for t in net.ts_transformers:   # appended last, so every other row keeps its place
        eq(t["equnr"], "TTX", t["manufacturer"], t["year"], t["asset_id"], t["name"])
    h = ["ODQ_CHANGEMODE", "ODQ_ENTITYCNTR"]
    _write_csv(os.path.join(out, "EQUI", f"EQUI_{stamp}_000001.csv"),
               h + ["EQUNR", "EQTYP", "EQART", "HERST", "BAUJJ", "INBDT", "INVNR", "EQKTX"], equi)
    return len(equi)


# ---------------------------------------------------------------------------------------------- Market and customers
def write_market(net, root, stamp):
    out = os.path.join(root, "market")
    nmi_rows, ls_rows = [], []
    retailers = ["RET-ALPHA", "RET-BRAVO", "RET-COAST", "RET-DELTA", "RET-ECHO"]
    for m in net.nmis:
        r = rng_for(net.seed, "msats", m["nmi"])
        dlf = round(r.uniform(1.03, 1.07), 4)
        r.randint(1, 240), r.choice("abcdefg"), r.choice("abcde")   # the draws an older made-up address used; kept so the rest is unchanged
        # the address as MSATS holds it: unit, house number, street name, street type (address standard abbreviation), locality, state, postcode
        nmi_rows.append([m["nmi"], m["checksum"], "LARGE" if m["customer_type"] == "large_business" else "SMALL",
                         "A", f"N{m['feeder_id'][:3]}", dlf, "LNSP0001", m["tariff"],
                         m["unit"], m["house_number"], m["street_name"].upper(), m["street_type_code"], m["suburb"].upper(), "NSW", m["postcode"],
                         stamp[:8]])
        # the FRMP and RP draws of the participant relations file, no longer written; kept so the life support draws are unchanged
        r.choice(retailers), r.choice(["MC-ONE", "MC-TWO", "MC-THREE"])
        if m["life_support"]:
            reg = f"20{r.randint(15, 26)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"
            ls_rows.append([m["nmi"], f"CUS{m['nmi'][-7:]}", r.choice(["confirmed", "confirmed", "confirmed", "pending"]),
                            reg, r.choice(["retailer", "customer"]), r.choice(["oxygen concentrator", "home dialysis",
                                                                               "ventilator", "feeding pump", "CPAP"]),
                            "", "", "", "", r.choice(retailers)])
    _write_csv(os.path.join(out, f"CATS_NMI_DATA_{stamp}.csv"),
               ["NMI", "CHECKSUM", "NMI_CLASSIFICATION_CODE", "NMI_STATUS_CODE", "TNI_CODE", "DLF_CODE_VALUE", "LNSP",
                "NETWORK_TARIFF_CODE", "FLAT_OR_UNIT_NUMBER", "HOUSE_NUMBER", "STREET_NAME", "STREET_TYPE", "SUBURB_OR_PLACE_OR_LOCALITY",
                "STATE_OR_TERRITORY", "POSTCODE", "FROM_DATE"], nmi_rows)
    _write_csv(os.path.join(out, f"LIFE_SUPPORT_REGISTER_{stamp}.csv"),
               ["premises_nmi", "customer_id", "registration_status", "notification_date", "notification_source",
                "equipment_type", "medical_form_issued_date", "reminder_1_date", "reminder_2_date",
                "deregistration_date", "retailer_id"], ls_rows)
    crit = [[c["nmi"], c["kind"], c["name"], c["ds_id"], "Y" if c["backup_generator"] else "N", c["generator_kva"] or "", c["fuel_hours"] or ""]
            for c in net.critical_sites]
    _write_csv(os.path.join(out, f"CRITICAL_SITES_{stamp}.csv"),
               ["nmi", "site_type", "site_name", "substation_ref", "backup_generator", "generator_kva", "fuel_hours"], crit)
    der = [[d["der_id"], d.get("nmi") or "", d.get("ds_id") or "", d["kind"], d["kw"], d.get("kwh") or "",
            d["export_limit_kva"], d["installed"]] for d in net.der]
    _write_csv(os.path.join(root, "der", f"DER_REGISTER_{stamp}.csv"),
               ["der_id", "nmi", "substation_ref", "device_type", "nameplate_kw", "storage_kwh", "export_limitation_kva",
                "install_year"], der)
    hubs = [[h["hub_id"], h["ds_id"], h["lat"], h["lon"], h["chargers"], h["kw_each"]] for h in net.ev_hubs]
    _write_csv(os.path.join(root, "der", f"PUBLIC_CHARGING_SITES_{stamp}.csv"),
               ["site_id", "substation_ref", "latitude", "longitude", "evse_count", "evse_kw"], hubs)
    return len(nmi_rows)


# ---------------------------------------------------------------------------------------------- Twin reference (for the generator itself)
def write_ground_truth(net, root):
    """The generator's own record of true connectivity, used only by the history and live emitters (never by pipelines)."""
    out = os.path.join(root, "_generator")
    _mkdir(out)
    solar = {d["nmi"]: d["kw"] for d in net.der if d["kind"] == "solar_pv"}
    evs = {d["nmi"] for d in net.der if d["kind"] == "home_ev_charger"}
    cust_by_feeder = {}
    for m in net.nmis:
        cust_by_feeder[m["feeder_id"]] = cust_by_feeder.get(m["feeder_id"], 0) + 1
    with open(os.path.join(out, "network_truth.json"), "w") as f:
        json.dump(dict(feeders=net.feeders, devices=net.devices, dss=[{k: d[k] for k in ("ds_id", "feeder_id", "kind", "kva", "lat", "lon",
                                                                                          "lv_monitor", "node_id")} for d in net.dss],
                       zones=net.zones, zs_transformers=net.zs_transformers, transmission_substations=net.tss,
                       ts_transformers=net.ts_transformers,
                       hero_nmis=[dict(nmi=m["nmi"], ds_id=m["ds_id"], recorded_ds_id=m["recorded_ds_id"], feeder_id=m["feeder_id"],
                                       customer_type=m["customer_type"], solar_kw=solar.get(m["nmi"], 0.0),
                                       ev=m["nmi"] in evs) for m in net.nmis if m["hero"]],
                       customers_by_feeder=cust_by_feeder), f)


def write_contacts(net, root, stamp):
    """Customer contact preferences from the distributor's outage notification register (CRM export): which channels each customer
    has opted into for planned and unplanned outage notices, and the channel each prefers. Every customer has a preference on record:
    one of the digital channels they opted into (SMS, EMAIL or FACEBOOK, the app), or MAIL (a letter) when they have none. Opt-in flags
    only; no phone numbers or addresses leave the CRM. Life support customers are always registered for SMS (the distributor must be
    able to reach them)."""
    out = os.path.join(root, "crm")
    _mkdir(out)
    rows = []
    for m in net.nmis:
        r = rng_for(net.seed, "contact", m["nmi"])
        res = m["customer_type"] == "residential"
        sms = m["life_support"] or r.random() < (0.72 if res else 0.55)
        email = r.random() < (0.58 if res else 0.85)
        fb = r.random() < (0.14 if res else 0.05)
        pref = next((c for c, on in (("SMS", sms), ("EMAIL", email), ("FACEBOOK", fb)) if on), "MAIL")   # no digital channel: a letter
        if sms and email and r.random() < (0.35 if res else 0.6):
            pref = "EMAIL"
        rows.append([m["nmi"], "Y" if sms else "N", "Y" if email else "N", "Y" if fb else "N", pref,
                     f"20{r.randint(18, 26)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"])
    path = os.path.join(out, f"CUSTOMER_CONTACT_PREFERENCES_{stamp}.csv")
    _write_csv(path, ["nmi", "sms_opt_in", "email_opt_in", "facebook_messenger_opt_in", "preferred_channel", "last_updated"], rows)
    return len(rows)


def write_all(net, root, stamp):
    from workforce import write_workforce   # workforce.py builds on this module's writers
    res = {}
    res["cim_files"] = len(write_cim(net, root))
    res["gpkg"] = write_gpkg(net, root)
    res["sap_equipment"] = write_sap(net, root, stamp)
    res["nmis"] = write_market(net, root, stamp)
    res["contact_preferences"] = write_contacts(net, root, stamp)
    wf = write_workforce(net, root, stamp)
    res["crews"] = len(wf["crews"])
    write_ground_truth(net, root)
    return res
