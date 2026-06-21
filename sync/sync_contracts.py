"""
Synchronise les contrats CODIAL (FA4_CONTRAT_CLIENT) vers GLPI.
Cle de synchro : IDFA4_CONTRAT_CLIENT -> contract_map dans mapping.db.
Filtre par defaut : COCHE_TERMINE=0 (contrats actifs uniquement).
Les clients non presents dans entity_map sont ignores.

Types de contrat CODIAL : crees dans GLPI a la volee si absents.

Usage:
    python sync/sync_contracts.py              # sync reelle (actifs uniquement)
    python sync/sync_contracts.py --dry-run    # simulation
    python sync/sync_contracts.py --stats      # volume uniquement
    python sync/sync_contracts.py --all        # inclure aussi les contrats termines
"""
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")

STATE_ACTIF    = 1
STATE_RESILIE  = 2
BATCH_SIZE     = 50

SQL_CONTRACTS = (
    "SELECT IDFA4_CONTRAT_CLIENT, CODE, NUMERO_CONTRAT, OBSERVATION, "
    "DATE_DEBUT, DATE_FIN, DUREE_MOIS, MONTANT_HT, IDFA4_CONTRAT_TYPE, "
    "COCHE_TERMINE, COMPTE_VENTE, PERIODICITE_RELEVE "
    "FROM FA4_CONTRAT_CLIENT "
    "ORDER BY CODE, DATE_DEBUT"
)

SQL_CONTRACTS_ACTIVE = (
    "SELECT IDFA4_CONTRAT_CLIENT, CODE, NUMERO_CONTRAT, OBSERVATION, "
    "DATE_DEBUT, DATE_FIN, DUREE_MOIS, MONTANT_HT, IDFA4_CONTRAT_TYPE, "
    "COCHE_TERMINE, COMPTE_VENTE, PERIODICITE_RELEVE "
    "FROM FA4_CONTRAT_CLIENT "
    "WHERE COCHE_TERMINE = 0 "
    "ORDER BY CODE, DATE_DEBUT"
)

SQL_CONTRACT_TYPES = (
    "SELECT IDFA4_CONTRAT_TYPE, DESCRIPTION "
    "FROM FA4_CONTRAT_TYPE ORDER BY IDFA4_CONTRAT_TYPE"
)


# ---------------------------------------------------------------------------
# Base de donnees
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(MAPPING_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entity_map (
            codial_code  TEXT PRIMARY KEY,
            glpi_id      INTEGER NOT NULL,
            codial_nom   TEXT,
            synced_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS contract_map (
            codial_id   TEXT PRIMARY KEY,
            glpi_id     INTEGER NOT NULL,
            num         TEXT,
            synced_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS contract_type_map (
            codial_type_id  TEXT PRIMARY KEY,
            glpi_type_id    INTEGER NOT NULL,
            label           TEXT
        );
    """)
    conn.commit()
    return conn


def load_entity_map(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT codial_code, glpi_id FROM entity_map")}


def load_contract_map(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT codial_id, glpi_id FROM contract_map")}


def load_contract_type_map(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT codial_type_id, glpi_type_id FROM contract_type_map")}


def save_contract(conn, codial_id, glpi_id, num):
    conn.execute("""
        INSERT INTO contract_map (codial_id, glpi_id, num, synced_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(codial_id) DO UPDATE SET
            glpi_id=excluded.glpi_id, num=excluded.num, synced_at=excluded.synced_at
    """, (str(codial_id), glpi_id, num))
    conn.commit()


def save_contract_type(conn, codial_type_id, glpi_type_id, label):
    conn.execute("""
        INSERT INTO contract_type_map (codial_type_id, glpi_type_id, label)
        VALUES (?, ?, ?)
        ON CONFLICT(codial_type_id) DO UPDATE SET
            glpi_type_id=excluded.glpi_type_id, label=excluded.label
    """, (str(codial_type_id), glpi_type_id, label))
    conn.commit()


# ---------------------------------------------------------------------------
# Conversion date CODIAL (MM/DD/YYYY HH:MM:SS) -> GLPI (YYYY-MM-DD)
# ---------------------------------------------------------------------------

def parse_date(s):
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), "%m/%d/%Y %H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Gestion des types de contrat
# ---------------------------------------------------------------------------

def sync_contract_types(glpi, db, dry_run):
    """Cree dans GLPI les types CODIAL absents. Retourne le mapping {codial_id: glpi_id}."""
    type_map = load_contract_type_map(db)

    print("Chargement des types de contrat CODIAL...")
    codial_types = hfsql_query(SQL_CONTRACT_TYPES)
    print(f"  {len(codial_types)} types CODIAL.")

    print("Chargement des types de contrat GLPI...")
    glpi_types = glpi._request("GET", "/ContractType", params={"range": "0-999"})
    glpi_by_name = {(t.get("name") or "").strip().lower(): t["id"] for t in glpi_types}
    print(f"  {len(glpi_types)} types GLPI existants.\n")

    for ct in codial_types:
        cid   = str(ct.get("IDFA4_CONTRAT_TYPE") or "").strip()
        label = (ct.get("DESCRIPTION") or "").strip()
        if not cid or not label:
            continue
        if cid in type_map:
            continue  # deja mappe

        key = label.lower()
        if key in glpi_by_name:
            gid = glpi_by_name[key]
        else:
            if dry_run:
                print(f"  [DRY-RUN] Type '{label}' serait cree dans GLPI.")
                type_map[cid] = 0
                continue
            resp = glpi.create("ContractType", {"name": label})
            gid  = resp[0]["id"] if isinstance(resp, list) else resp["id"]
            glpi_by_name[key] = gid
            print(f"  [NEW] ContractType cree : {label} (GLPI id={gid})")

        save_contract_type(db, cid, gid, label)
        type_map[cid] = gid

    return type_map


# ---------------------------------------------------------------------------
# Construction du payload contrat
# ---------------------------------------------------------------------------

def build_contract_fields(c, entity_id, glpi_type_id):
    num        = (c.get("NUMERO_CONTRAT") or "").strip()
    obs        = (c.get("OBSERVATION") or "").strip()
    debut      = parse_date(c.get("DATE_DEBUT"))
    duree      = int(c.get("DUREE_MOIS") or 0)
    montant    = (c.get("MONTANT_HT") or "0").strip()
    compte     = (c.get("COMPTE_VENTE") or "").strip()
    termine    = str(c.get("COCHE_TERMINE") or "0").strip()
    periodicite = int(c.get("PERIODICITE_RELEVE") or 0)

    # Calculer la duree depuis les dates si DUREE_MOIS = 0
    if duree == 0:
        debut_dt = parse_date(c.get("DATE_DEBUT"))
        fin_dt   = parse_date(c.get("DATE_FIN"))
        if debut_dt and fin_dt:
            try:
                d1 = datetime.strptime(debut_dt, "%Y-%m-%d")
                d2 = datetime.strptime(fin_dt,   "%Y-%m-%d")
                duree = max(0, (d2.year - d1.year) * 12 + d2.month - d1.month)
            except ValueError:
                pass

    name = obs or num or "Contrat"
    fields = {
        "name":               name,
        "num":                num,
        "entities_id":        entity_id,
        "is_recursive":       0,
        "states_id":          STATE_RESILIE if termine == "1" else STATE_ACTIF,
        "contracttypes_id":   glpi_type_id or 0,
        "duration":           duree,
        "periodicity":        periodicite * 30 if periodicite else 0,
        "billing":            periodicite * 30 if periodicite else 0,
    }
    if debut:
        fields["begin_date"] = debut
    if compte:
        fields["accounting_number"] = compte
    if montant and montant != "0":
        try:
            m = float(montant)
            if m > 0:
                fields["comment"] = f"Montant HT : {m:.2f} EUR"
        except ValueError:
            pass
    return fields


# ---------------------------------------------------------------------------
# Sync principale
# ---------------------------------------------------------------------------

def sync_contracts(dry_run=False, stats_only=False, active_only=False):
    tag = " [DRY-RUN]" if dry_run else (" [STATS]" if stats_only else "")
    print(f"=== Sync CODIAL Contrats -> GLPI{tag} ===\n")

    sql = SQL_CONTRACTS if active_only else SQL_CONTRACTS_ACTIVE
    print("Chargement des contrats CODIAL...")
    contracts = hfsql_query(sql, timeout=180)
    print(f"  {len(contracts)} contrats charges.\n")

    db = init_db()
    entity_map   = load_entity_map(db)
    contract_map = load_contract_map(db)

    linked   = [c for c in contracts if (c.get("CODE") or "").strip() in entity_map]
    skipped  = len(contracts) - len(linked)
    print(f"  {len(linked)} contrats rattaches a une entite GLPI connue.")
    print(f"  {skipped} ignores (client non dans entity_map).\n")

    if stats_only:
        already = sum(1 for c in linked
                      if str(c.get("IDFA4_CONTRAT_CLIENT") or "").strip() in contract_map)
        print(f"  A creer          : {len(linked) - already}")
        print(f"  A mettre a jour  : {already}")
        db.close()
        return

    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    with GlpiClient() as glpi:
        type_map = sync_contract_types(glpi, db, dry_run)

        for i, c in enumerate(linked, 1):
            codial_id  = str(c.get("IDFA4_CONTRAT_CLIENT") or "").strip()
            code       = (c.get("CODE") or "").strip()
            num        = (c.get("NUMERO_CONTRAT") or "").strip()
            entity_id  = entity_map[code]
            type_id    = str(c.get("IDFA4_CONTRAT_TYPE") or "").strip()
            glpi_type  = type_map.get(type_id, 0)

            if not codial_id:
                stats["skipped"] += 1
                continue

            if i % 200 == 0:
                print(f"  ... {i}/{len(linked)} traites")

            try:
                fields = build_contract_fields(c, entity_id, glpi_type)

                if codial_id in contract_map:
                    glpi_id = contract_map[codial_id]
                    if not dry_run:
                        glpi.update("Contract", glpi_id, fields)
                        save_contract(db, codial_id, glpi_id, num)
                    stats["updated"] += 1
                else:
                    if not dry_run:
                        resp    = glpi.create("Contract", fields)
                        glpi_id = resp[0]["id"] if isinstance(resp, list) else resp["id"]
                        save_contract(db, codial_id, glpi_id, num)
                    stats["created"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    print(f"  [ERR] {codial_id} {num} : {e}")

    db.close()
    print(f"\n--- Resultat ---")
    print(f"  Crees    : {stats['created']}")
    print(f"  Maj      : {stats['updated']}")
    print(f"  Ignores  : {stats['skipped']}")
    print(f"  Erreurs  : {stats['errors']}")
    if dry_run:
        print("\n  Simulation terminee — aucune modification appliquee.")


if __name__ == "__main__":
    dry_run    = "--dry-run" in sys.argv
    stats_only = "--stats"   in sys.argv
    active_only = "--all"    in sys.argv
    sync_contracts(dry_run=dry_run, stats_only=stats_only, active_only=active_only)
