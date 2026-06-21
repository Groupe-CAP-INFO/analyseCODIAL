"""
Synchronise les clients CODIAL actifs vers les entités GLPI.
Clé de synchro : CLIENT.CODE = Entity.registration_number
Parent : Entité racine (ID=0)

Usage:
    python sync/sync_entities.py             # sync réelle
    python sync/sync_entities.py --dry-run   # simulation sans écriture
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")
PARENT_ENTITY_ID = 0  # Entité racine GLPI

SQL_CLIENTS = "SELECT CODE, NOM, ADRESSE1, VILLE, TEL, EMAIL, SITE_WEB FROM CLIENT WHERE FICHE_SUPPRIME = 0 AND NOM <> '' ORDER BY CODE"


# ---------------------------------------------------------------------------
# Mapping DB
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
        CREATE TABLE IF NOT EXISTS protected_entities (
            glpi_id    INTEGER PRIMARY KEY,
            glpi_nom   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS parent_overrides (
            codial_code    TEXT PRIMARY KEY,
            glpi_parent_id INTEGER NOT NULL,
            note           TEXT,
            created_at     TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def load_mapping(conn):
    rows = conn.execute("SELECT codial_code, glpi_id FROM entity_map").fetchall()
    return {code: gid for code, gid in rows}


def load_protected(conn):
    """Retourne l'ensemble des glpi_id proteges (jamais touches par le sync)."""
    rows = conn.execute("SELECT glpi_id FROM protected_entities").fetchall()
    return {gid for (gid,) in rows}


def load_parent_overrides(conn):
    """Retourne {codial_code: glpi_parent_id} pour les clients avec parent specifique."""
    rows = conn.execute("SELECT codial_code, glpi_parent_id FROM parent_overrides").fetchall()
    return {code: pid for code, pid in rows}


def save_mapping(conn, code, glpi_id, nom):
    conn.execute("""
        INSERT INTO entity_map (codial_code, glpi_id, codial_nom, synced_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(codial_code) DO UPDATE SET
            glpi_id=excluded.glpi_id,
            codial_nom=excluded.codial_nom,
            synced_at=excluded.synced_at
    """, (code, glpi_id, nom))
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_fields(client, parent_id=None):
    """Construit le dict de champs pour CREATE ou UPDATE.

    parent_id=None -> pas de entities_id dans le payload (update en place)
    parent_id=int  -> inclut entities_id (creation ou override explicite)
    """
    fields = {
        "name": (client.get("NOM") or "").strip(),
        "registration_number": (client.get("CODE") or "").strip(),
    }
    if parent_id is not None:
        fields["entities_id"] = parent_id
    for src, dst in [("ADRESSE1", "address"), ("VILLE", "town"),
                     ("TEL", "phonenumber"), ("EMAIL", "email"),
                     ("SITE_WEB", "website")]:
        val = (client.get(src) or "").strip()
        if val:
            fields[dst] = val
    return fields


def fetch_glpi_entities(glpi):
    """Retourne {registration_number: entity_dict} pour toutes les entités GLPI."""
    try:
        entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})
    except Exception as e:
        print(f"  [WARN] GET /Entity échoué : {e}")
        return {}
    if not isinstance(entities, list):
        return {}
    result = {}
    for e in entities:
        reg = (e.get("registration_number") or "").strip()
        if reg:
            result[reg] = e
    return result


def extract_id(response):
    """Extrait l'id depuis la réponse GLPI create (dict ou list)."""
    if isinstance(response, list):
        return response[0]["id"]
    return response["id"]


# ---------------------------------------------------------------------------
# Sync principal
# ---------------------------------------------------------------------------

def sync_entities(dry_run=False):
    tag = " [DRY-RUN]" if dry_run else ""
    print(f"=== Sync CODIAL -> GLPI Entities{tag} ===\n")

    print("Chargement des clients CODIAL...")
    clients = hfsql_query(SQL_CLIENTS)
    print(f"  {len(clients)} clients actifs.\n")

    db = init_db()
    mapping         = load_mapping(db)
    protected_ids   = load_protected(db)
    parent_overrides = load_parent_overrides(db)

    if protected_ids:
        print(f"  {len(protected_ids)} entite(s) protegee(s) (jamais modifiees).")
    if parent_overrides:
        print(f"  {len(parent_overrides)} override(s) de parent defini(s).")
    print()

    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    with GlpiClient() as glpi:
        print("Chargement des entités GLPI existantes...")
        existing = fetch_glpi_entities(glpi)
        print(f"  {len(existing)} entités indexées par registration_number.\n")

        for i, client in enumerate(clients, 1):
            code = (client.get("CODE") or "").strip()
            nom  = (client.get("NOM") or "").strip()

            if not code or not nom:
                stats["skipped"] += 1
                continue

            if i % 100 == 0:
                print(f"  ... {i}/{len(clients)} traités")

            try:
                if code in existing:
                    glpi_id = existing[code]["id"]
                    if glpi_id in protected_ids:
                        # Entite conteneur protegee — on ne la touche jamais
                        stats["skipped"] += 1
                        continue
                    # Update en place : pas de entities_id pour conserver la position dans l'arbre
                    fields = build_fields(client, parent_id=None)
                    if not dry_run:
                        glpi.update("Entity", glpi_id, fields)
                        save_mapping(db, code, glpi_id, nom)
                    stats["updated"] += 1

                elif code in mapping:
                    glpi_id = mapping[code]
                    if glpi_id in protected_ids:
                        stats["skipped"] += 1
                        continue
                    fields = build_fields(client, parent_id=None)
                    if not dry_run:
                        glpi.update("Entity", glpi_id, fields)
                        save_mapping(db, code, glpi_id, nom)
                    stats["updated"] += 1

                else:
                    # Nouvelle entite — verifier si un parent specifique est defini
                    parent_id = parent_overrides.get(code, PARENT_ENTITY_ID)
                    fields = build_fields(client, parent_id=parent_id)
                    if not dry_run:
                        resp = glpi.create("Entity", fields)
                        glpi_id = extract_id(resp)
                        save_mapping(db, code, glpi_id, nom)
                    stats["created"] += 1

            except Exception as e:
                stats["errors"] += 1
                print(f"  [ERR] {code} — {nom} : {e}")

    db.close()

    print(f"\n--- Résumé ---")
    print(f"  Créés    : {stats['created']}")
    print(f"  Mis à j. : {stats['updated']}")
    print(f"  Ignorés  : {stats['skipped']}")
    print(f"  Erreurs  : {stats['errors']}")
    if dry_run:
        print("\n  Simulation terminée — aucune modification appliquée.")


if __name__ == "__main__":
    sync_entities(dry_run="--dry-run" in sys.argv)
