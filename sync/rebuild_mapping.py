"""
Reconstruit mapping.db depuis l'état actuel de GLPI.

- entity_map  : toutes les entités GLPI qui ont un registration_number
- protected_entities : entités connues comme conteneurs (sans équivalent CODIAL)

Usage:
    python sync/rebuild_mapping.py
    python sync/rebuild_mapping.py --dry-run   # affiche sans écrire
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")

# IDs d'entités GLPI jamais touchées par le sync (conteneurs / internes)
# D'après la hiérarchie connue — à compléter si nécessaire
KNOWN_PROTECTED = [
    (1,  "Groupe BELLEC"),
    (2,  "Groupe CAP INFO"),
    (3,  "Siège - Artigues"),
    (5,  "Datacenter OVH"),
    (6,  "Serveurs"),
    (16, "AGS NISSAN"),
    (29, "GARAGE LAMERAIN"),
    (31, "Postes"),
    (32, "ALAIN MARTIN"),
    (35, "ETS J BOURDEN"),
    (40, "TALDI"),
    (47, "TRISCOS AUTOMOBILES"),
    (54, "KORERO"),
    (58, "PersoJFG"),
    (63, "Commerciaux"),
    (64, "Groupe Univerp"),
]


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
        CREATE TABLE IF NOT EXISTS user_map (
            codial_id   TEXT PRIMARY KEY,
            glpi_id     INTEGER NOT NULL,
            login       TEXT,
            synced_at   TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== Mode DRY-RUN — aucune écriture ===\n")

    db = init_db()

    # -----------------------------------------------------------------------
    # 1. Charger les noms CODIAL (pour enrichir entity_map.codial_nom)
    # -----------------------------------------------------------------------
    print("Chargement CODIAL...")
    clients = hfsql_query(
        "SELECT CODE, NOM FROM CLIENT WHERE FICHE_SUPPRIME = 0 AND NOM <> '' ORDER BY CODE"
    )
    codial_noms = {r["CODE"].strip(): r["NOM"].strip() for r in clients if r.get("CODE")}
    print(f"  {len(codial_noms)} clients actifs.\n")

    # -----------------------------------------------------------------------
    # 2. Récupérer toutes les entités GLPI avec registration_number
    # -----------------------------------------------------------------------
    print("Chargement des entités GLPI...")
    with GlpiClient() as glpi:
        all_entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})

    if not isinstance(all_entities, list):
        print("Erreur : réponse inattendue de GET /Entity")
        sys.exit(1)

    mapped   = [(e["id"], (e.get("registration_number") or "").strip(), (e.get("name") or "").strip())
                for e in all_entities
                if (e.get("registration_number") or "").strip()]
    unmapped = [e for e in all_entities if not (e.get("registration_number") or "").strip()]

    print(f"  {len(all_entities)} entités GLPI au total.")
    print(f"  {len(mapped)} avec registration_number -> entity_map")
    print(f"  {len(unmapped)} sans registration_number (non mappées ou conteneurs)\n")

    # -----------------------------------------------------------------------
    # 3. Insérer entity_map
    # -----------------------------------------------------------------------
    if not dry_run:
        for glpi_id, code, glpi_nom in mapped:
            nom = codial_noms.get(code, glpi_nom)
            db.execute("""
                INSERT INTO entity_map (codial_code, glpi_id, codial_nom, synced_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(codial_code) DO UPDATE SET
                    glpi_id=excluded.glpi_id,
                    codial_nom=excluded.codial_nom,
                    synced_at=excluded.synced_at
            """, (code, glpi_id, nom))
        db.commit()
        print(f"  {len(mapped)} lignes insérées dans entity_map.")
    else:
        print(f"  [DRY-RUN] {len(mapped)} lignes à insérer dans entity_map.")
        for glpi_id, code, glpi_nom in mapped[:10]:
            print(f"    GLPI {glpi_id:>4} | {code:<12} | {glpi_nom}")
        if len(mapped) > 10:
            print(f"    ... ({len(mapped) - 10} de plus)")

    # -----------------------------------------------------------------------
    # 4. Insérer protected_entities (conteneurs connus)
    # -----------------------------------------------------------------------
    print()
    if not dry_run:
        for glpi_id, glpi_nom in KNOWN_PROTECTED:
            db.execute("""
                INSERT INTO protected_entities (glpi_id, glpi_nom)
                VALUES (?, ?)
                ON CONFLICT(glpi_id) DO UPDATE SET glpi_nom=excluded.glpi_nom
            """, (glpi_id, glpi_nom))
        db.commit()
        print(f"  {len(KNOWN_PROTECTED)} entités protégées insérées.")
    else:
        print(f"  [DRY-RUN] {len(KNOWN_PROTECTED)} entités protégées à insérer.")

    db.close()
    print(f"\nmapping.db reconstruit : {MAPPING_DB}")
    print("Étapes suivantes :")
    print("  1. Si nécessaire : python sync/manage_parents.py --list  (vérifier parent_overrides)")
    print("  2. python sync/sync_users.py --stats")
    print("  3. python sync/fix_duplicates.py")


if __name__ == "__main__":
    main()
