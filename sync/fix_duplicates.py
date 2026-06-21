"""
Résout les doublons restants après sync_entities.
Pour chaque code CODIAL en erreur, cherche l'entité GLPI correspondante,
demande confirmation, puis insère dans mapping.db et set registration_number.

Usage:
    python sync/fix_duplicates.py
    python sync/fix_duplicates.py --dry-run
"""
import difflib
import os
import sqlite3
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")

PENDING_CODES = [
    "CAGFPME", "CBRIPH", "CCHAMBR", "CJDELAG", "CMONTER",
    "CROUX01", "CTAUZIN", "GRO04", "MIN01", "SAS01",
]


def normalize(name):
    s = unicodedata.normalize("NFD", name.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = "".join(c if c.isalnum() else " " for c in s)
    return " ".join(s.split())


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


def find_candidates(glpi_entities, target_nom, n=5):
    target_norm = normalize(target_nom)
    scores = []
    for e in glpi_entities:
        name = (e.get("name") or "").strip()
        norm = normalize(name)
        ratio = difflib.SequenceMatcher(None, target_norm, norm).ratio()
        scores.append((ratio, e))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores[:n]


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== Mode DRY-RUN — aucune écriture ===\n")

    print("Chargement CODIAL...")
    clients = hfsql_query(
        "SELECT CODE, NOM FROM CLIENT WHERE FICHE_SUPPRIME = 0 AND NOM <> '' ORDER BY CODE"
    )
    codial_noms = {r["CODE"].strip(): r["NOM"].strip() for r in clients if r.get("CODE")}

    db = init_db()
    already = {row[0] for row in db.execute("SELECT codial_code FROM entity_map").fetchall()}
    pending = [c for c in PENDING_CODES if c not in already]

    if not pending:
        print("Tous les doublons sont déjà résolus dans mapping.db.")
        db.close()
        return

    print(f"{len(pending)} doublon(s) à résoudre.\n")

    with GlpiClient() as glpi:
        all_entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})
        unlinked = [e for e in all_entities if not (e.get("registration_number") or "").strip()]
        print(f"  {len(unlinked)} entités GLPI sans registration_number.\n")

        stats = {"ok": 0, "skip": 0}

        for code in pending:
            nom = codial_noms.get(code, "???")
            print(f"{'─' * 60}")
            print(f"Code CODIAL : {code}  |  Nom CODIAL : {nom}")
            print()

            candidates = find_candidates(unlinked, nom)
            for i, (score, e) in enumerate(candidates, 1):
                parent_id = e.get("entities_id", "?")
                print(f"  [{i}] id={e['id']:>4}  score={score:.2f}  nom={e.get('name')}  (parent={parent_id})")
            print(f"  [0] Ignorer ce code")
            print()

            choice = input("  Votre choix : ").strip()
            if choice == "0" or not choice:
                print("  -> Ignoré.\n")
                stats["skip"] += 1
                continue

            try:
                idx = int(choice) - 1
                _, chosen = candidates[idx]
            except (ValueError, IndexError):
                print("  Choix invalide, ignoré.\n")
                stats["skip"] += 1
                continue

            glpi_id = chosen["id"]
            glpi_nom = (chosen.get("name") or "").strip()
            print(f"  -> Mapping : {code} ({nom}) → GLPI {glpi_id} ({glpi_nom})")

            if not dry_run:
                glpi.update("Entity", glpi_id, {"registration_number": code})
                save_mapping(db, code, glpi_id, nom)
                print("  -> Enregistré.\n")
            else:
                print("  -> [DRY-RUN] Non appliqué.\n")
            stats["ok"] += 1

    db.close()
    print(f"\n--- Résumé ---")
    print(f"  Résolus : {stats['ok']}")
    print(f"  Ignorés : {stats['skip']}")


if __name__ == "__main__":
    main()
