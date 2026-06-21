import hfsql_bridge as hf

TABLE_CIBLE = "ARTICLE"
COLONNE_TRI = "ARTICLE"
NB_LIGNES = 10


def scanner_base_codial():
    print("Connexion au serveur HFSQL (192.168.0.4:4900, base CAPINFO)...")
    try:
        hf.test_connection()
    except RuntimeError as e:
        print(f"\n[ERREUR] Impossible de se connecter : {e}")
        return
    print("Connexion HFSQL reussie.\n")

    # --- Liste des tables ---
    print("=== LISTE DES TABLES DE LA BASE ===")
    tables = hf.list_tables()
    for t in tables:
        print(f"- {t}")
    print(f"\n{len(tables)} tables au total.\n" + "=" * 50 + "\n")

    # --- Extraction des derniers enregistrements ---
    print(f"=== {NB_LIGNES} PREMIERS ENREGISTREMENTS : {TABLE_CIBLE} ===")
    requete = f"SELECT TOP {NB_LIGNES} * FROM {TABLE_CIBLE} ORDER BY {COLONNE_TRI}"
    rows = hf.query(requete)
    if not rows:
        print("Aucun enregistrement retourne.")
        return
    colonnes = list(rows[0].keys())
    print(f"COLONNES : {colonnes}\n")
    print("DONNEES :")
    for row in rows:
        print(list(row.values()))


if __name__ == "__main__":
    scanner_base_codial()
