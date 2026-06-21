"""
Gere les clients CODIAL devenus inactifs (FICHE_SUPPRIME=1) :
  1. Cree l'entite [INACTIFS] a la racine GLPI si elle n'existe pas
  2. Detecte les entites dans entity_map dont le code n'est plus actif dans CODIAL
  3. Deplace ces entites sous [INACTIFS] dans GLPI
  4. Desactive leurs utilisateurs (is_active=0)

Si un client redevient actif (FICHE_SUPPRIME repasse a 0) :
  - L'entite est replacee sous son parent d'origine (ou racine par defaut)
  - Ses utilisateurs sont reappliques (is_active=1)

Usage:
    python sync/sync_inactive.py             # desactiver les inactifs + reactivation
    python sync/sync_inactive.py --dry-run   # simulation
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB       = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")
INACTIFS_NAME    = "[INACTIFS]"
ROOT_ENTITY_ID   = 0


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
        CREATE TABLE IF NOT EXISTS protected_entities (
            glpi_id    INTEGER PRIMARY KEY,
            glpi_nom   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS inactive_entities (
            codial_code     TEXT PRIMARY KEY,
            glpi_id         INTEGER NOT NULL,
            original_parent INTEGER NOT NULL DEFAULT 0,
            inactivated_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    return conn


def get_config(conn, key):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_config(conn, key, value):
    conn.execute("""
        INSERT INTO config (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    conn.commit()


# ---------------------------------------------------------------------------
# Entite [INACTIFS]
# ---------------------------------------------------------------------------

def get_or_create_inactifs_entity(glpi, db, dry_run):
    cached = get_config(db, "inactifs_entity_id")
    if cached:
        return int(cached)

    # Chercher dans GLPI
    all_entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})
    for e in all_entities:
        if (e.get("name") or "").strip() == INACTIFS_NAME and e.get("entities_id") == ROOT_ENTITY_ID:
            eid = e["id"]
            set_config(db, "inactifs_entity_id", eid)
            print(f"  Entite {INACTIFS_NAME} existante : GLPI id={eid}")
            return eid

    # Creer
    print(f"  Creation de l'entite {INACTIFS_NAME}...")
    if dry_run:
        print(f"  [DRY-RUN] Entite {INACTIFS_NAME} serait creee a la racine.")
        return -1
    resp = glpi.create("Entity", {"name": INACTIFS_NAME, "entities_id": ROOT_ENTITY_ID})
    eid = resp[0]["id"] if isinstance(resp, list) else resp["id"]
    set_config(db, "inactifs_entity_id", eid)
    print(f"  Entite {INACTIFS_NAME} creee : GLPI id={eid}")
    return eid


# ---------------------------------------------------------------------------
# Helpers utilisateurs
# ---------------------------------------------------------------------------

def set_users_active(glpi, glpi_entity_id, is_active, dry_run):
    """Active ou desactive tous les users rattaches a une entite GLPI."""
    try:
        users = glpi._request("GET", "/User", params={"range": "0-9999"})
        targets = [u["id"] for u in users if isinstance(u, dict) and u.get("entities_id") == glpi_entity_id]
    except Exception as e:
        print(f"    [WARN] Impossible de lister les users de l'entite {glpi_entity_id} : {e}")
        return 0

    if not targets:
        return 0

    label = "reactivation" if is_active else "desactivation"
    if dry_run:
        print(f"    [DRY-RUN] {len(targets)} user(s) : {label}")
        return len(targets)

    done = 0
    for uid in targets:
        try:
            glpi.update("User", uid, {"is_active": is_active})
            done += 1
        except Exception:
            pass
    return done


# ---------------------------------------------------------------------------
# Sync inactifs / reactiver
# ---------------------------------------------------------------------------

def sync_inactive(dry_run=False):
    tag = " [DRY-RUN]" if dry_run else ""
    print(f"=== Sync entites inactives{tag} ===\n")

    # 1. Codes actifs dans CODIAL
    print("Chargement CODIAL...")
    clients = hfsql_query(
        "SELECT CODE FROM CLIENT WHERE FICHE_SUPPRIME = 0 AND NOM <> '' ORDER BY CODE"
    )
    active_codes = {r["CODE"].strip() for r in clients if r.get("CODE")}
    print(f"  {len(active_codes)} clients actifs.\n")

    db = init_db()
    entity_map = {
        code: gid
        for code, gid in db.execute("SELECT codial_code, glpi_id FROM entity_map").fetchall()
    }
    protected_ids = {
        gid for (gid,) in db.execute("SELECT glpi_id FROM protected_entities").fetchall()
    }
    already_inactive = {
        code: (gid, parent)
        for code, gid, parent in db.execute(
            "SELECT codial_code, glpi_id, original_parent FROM inactive_entities"
        ).fetchall()
    }

    # Codes dans entity_map non actifs => a desactiver
    to_deactivate = {
        code: gid for code, gid in entity_map.items()
        if code not in active_codes
        and code not in already_inactive
        and gid not in protected_ids
    }

    # Codes dans inactive_entities redevenus actifs => a reactivation
    to_reactivate = {
        code: (gid, parent)
        for code, (gid, parent) in already_inactive.items()
        if code in active_codes
    }

    print(f"  {len(to_deactivate)} entite(s) a desactiver.")
    print(f"  {len(to_reactivate)} entite(s) a reactivation.\n")

    if not to_deactivate and not to_reactivate:
        print("Rien a faire.")
        db.close()
        return

    stats = {"deactivated": 0, "reactivated": 0, "users_off": 0, "users_on": 0, "errors": 0}

    with GlpiClient() as glpi:

        # --- Desactivation ---
        if to_deactivate:
            inactifs_id = get_or_create_inactifs_entity(glpi, db, dry_run)
            print()
            print(f"--- Desactivation ({len(to_deactivate)}) ---")

            # Recuperer le parent actuel de chaque entite concernee
            all_entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})
            entity_parents = {e["id"]: e.get("entities_id", 0) for e in all_entities}

            for code, glpi_id in to_deactivate.items():
                nom = next((r[0] for r in db.execute(
                    "SELECT codial_nom FROM entity_map WHERE codial_code=?", (code,)
                ).fetchall()), code)
                original_parent = entity_parents.get(glpi_id, ROOT_ENTITY_ID)
                print(f"  {code} | {nom} | GLPI {glpi_id} -> {INACTIFS_NAME}")

                try:
                    if not dry_run:
                        glpi.update("Entity", glpi_id, {"entities_id": inactifs_id})
                        db.execute("""
                            INSERT INTO inactive_entities (codial_code, glpi_id, original_parent)
                            VALUES (?, ?, ?)
                            ON CONFLICT(codial_code) DO UPDATE SET
                                glpi_id=excluded.glpi_id,
                                original_parent=excluded.original_parent,
                                inactivated_at=datetime('now')
                        """, (code, glpi_id, original_parent))
                        db.commit()

                    n = set_users_active(glpi, glpi_id, 0, dry_run)
                    stats["users_off"] += n
                    stats["deactivated"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"    [ERR] {e}")

        # --- Reactivation ---
        if to_reactivate:
            print()
            print(f"--- Reactivation ({len(to_reactivate)}) ---")

            parent_overrides = {
                code: pid
                for code, pid in db.execute(
                    "SELECT codial_code, glpi_parent_id FROM parent_overrides"
                ).fetchall()
            }

            for code, (glpi_id, original_parent) in to_reactivate.items():
                parent_id = parent_overrides.get(code, original_parent)
                nom = next((r[0] for r in db.execute(
                    "SELECT codial_nom FROM entity_map WHERE codial_code=?", (code,)
                ).fetchall()), code)
                print(f"  {code} | {nom} | GLPI {glpi_id} -> parent={parent_id}")

                try:
                    if not dry_run:
                        glpi.update("Entity", glpi_id, {"entities_id": parent_id})
                        db.execute(
                            "DELETE FROM inactive_entities WHERE codial_code=?", (code,)
                        )
                        db.commit()

                    n = set_users_active(glpi, glpi_id, 1, dry_run)
                    stats["users_on"] += n
                    stats["reactivated"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"    [ERR] {e}")

    db.close()

    print(f"\n--- Resultat ---")
    print(f"  Desactivees   : {stats['deactivated']}  (users off : {stats['users_off']})")
    print(f"  Reactivees    : {stats['reactivated']}  (users on  : {stats['users_on']})")
    print(f"  Erreurs       : {stats['errors']}")
    if dry_run:
        print("\n  Simulation terminee — aucune modification appliquee.")


if __name__ == "__main__":
    sync_inactive(dry_run="--dry-run" in sys.argv)
