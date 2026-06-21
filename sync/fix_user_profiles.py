"""
Rattache les utilisateurs CODIAL déjà créés à leur entité GLPI
via un enregistrement Profile_User (profil Self-Service id=1).

Sans ce rattachement, les users n'apparaissent pas dans l'onglet
Utilisateurs des entités et ne peuvent pas se connecter au portail.

Usage:
    python sync/fix_user_profiles.py              # réel
    python sync/fix_user_profiles.py --dry-run    # simulation
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from glpi_client import GlpiClient

MAPPING_DB  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")
PROFILE_ID  = 1   # Self-Service
BATCH_SIZE  = 100


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== Mode DRY-RUN — aucune ecriture ===\n")

    db = sqlite3.connect(MAPPING_DB)
    user_rows = db.execute("SELECT codial_id, glpi_id FROM user_map").fetchall()
    db.close()

    if not user_rows:
        print("user_map vide — lancez d'abord sync_users.py.")
        return

    print(f"{len(user_rows)} utilisateurs dans user_map.\n")

    with GlpiClient() as glpi:
        # Charger tous les users GLPI pour recuperer leur entities_id
        print("Chargement des utilisateurs GLPI...")
        all_users = glpi._request("GET", "/User", params={"range": "0-9999"})
        glpi_entity = {u["id"]: u.get("entities_id", 0) for u in all_users if isinstance(u, dict)}
        print(f"  {len(glpi_entity)} users charges.\n")

        # Charger les Profile_User existants pour eviter les doublons
        print("Chargement des Profile_User existants...")
        try:
            existing_pu = glpi._request("GET", "/Profile_User", params={"range": "0-9999"})
            already = {(r["users_id"], r["entities_id"]) for r in existing_pu if isinstance(r, dict)}
        except Exception:
            already = set()
        print(f"  {len(already)} rattachements Profile_User existants.\n")

        to_create = []
        skipped   = 0

        for codial_id, glpi_id in user_rows:
            entity_id = glpi_entity.get(glpi_id)
            if entity_id is None:
                skipped += 1
                continue
            if (glpi_id, entity_id) in already:
                skipped += 1
                continue
            to_create.append({
                "users_id":    glpi_id,
                "entities_id": entity_id,
                "profiles_id": PROFILE_ID,
                "is_recursive": 0,
                "is_dynamic":   0,
            })

        print(f"  A creer  : {len(to_create)}")
        print(f"  Deja OK  : {skipped}\n")

        if not to_create or dry_run:
            if dry_run and to_create:
                print(f"[DRY-RUN] {len(to_create)} Profile_User seraient crees.")
            return

        # Creation par lots
        created = 0
        errors  = 0
        for i in range(0, len(to_create), BATCH_SIZE):
            batch = to_create[i:i + BATCH_SIZE]
            try:
                glpi._request("POST", "/Profile_User", body={"input": batch})
                created += len(batch)
                print(f"  Lot {i // BATCH_SIZE + 1} : {created}/{len(to_create)} crees")
            except Exception as e:
                errors += len(batch)
                print(f"  [ERR] Lot {i // BATCH_SIZE + 1} : {e}")

    print(f"\n--- Resultat ---")
    print(f"  Profile_User crees : {created}")
    print(f"  Erreurs            : {errors}")
    print(f"  Deja existants     : {skipped}")


if __name__ == "__main__":
    main()
