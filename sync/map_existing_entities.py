"""
Mappe les entites GLPI existantes (creees manuellement) vers leurs clients CODIAL.

Usage:
    py -3.14 sync/map_existing_entities.py --export
        -> genere entity_map_review.csv avec les meilleures correspondances

    py -3.14 sync/map_existing_entities.py --apply
        -> lit entity_map_review.csv, enregistre les lignes valide=1 dans
           mapping.db et met a jour registration_number dans GLPI.
           Les lignes protege=1 sont enregistrees dans protected_entities
           (jamais touchees par le sync).

Colonnes CSV :
    glpi_id       : ID de l'entite GLPI
    glpi_nom      : Nom de l'entite GLPI
    note          : INTERNE (entite CAP INFO) | CONTENEUR (groupe sans equivalent CODIAL)
    code1/nom1    : 1er candidat CODIAL (lecture seule, ne pas modifier)
    code2/nom2    : 2eme candidat
    code3/nom3    : 3eme candidat
    code_retenu   : code CODIAL a utiliser (modifier si code1 n'est pas le bon)
    valide        : 1 = confirmer le mapping, 0 = ignorer
    protege       : 1 = entite conteneur sans equivalent CODIAL (jamais touchee par le sync)
                    Incompatible avec valide=1.
"""
import csv
import difflib
import os
import sqlite3
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

CSV_PATH   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entity_map_review.csv")
MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")

SQL_CLIENTS = "SELECT CODE, NOM FROM CLIENT WHERE FICHE_SUPPRIME = 0 AND NOM <> '' ORDER BY CODE"

# Entite racine GLPI — jamais mappee, jamais dans le CSV
EXCLUDED_IDS = {0}

# Entites internes CAP INFO — forcees valide=0, note=INTERNE
INTERNAL_IDS = {2, 3, 5, 6, 58, 63}

_NOISE = [
    "sarl", "sas", "sasu", "sa", "sci", "eurl", "snc", "scop", "asso",
    "association", "groupe", "group", "ets", "etablissements",
    "garage", "societe", "soc",
]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def normalize(name):
    s = _strip_accents(name.lower())
    s = "".join(c if c.isalnum() else " " for c in s)
    words = [w for w in s.split() if w not in _NOISE]
    return " ".join(words)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def best_matches(glpi_norm, codial_index, n=3):
    scores = []
    for code, (nom, norm) in codial_index.items():
        ratio = difflib.SequenceMatcher(None, glpi_norm, norm).ratio()
        scores.append((ratio, code, nom))
    scores.sort(reverse=True)
    return scores[:n]


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
            glpi_id   INTEGER PRIMARY KEY,
            glpi_nom  TEXT,
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


def save_mapping(conn, codial_code, glpi_id, nom):
    conn.execute("""
        INSERT INTO entity_map (codial_code, glpi_id, codial_nom, synced_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(codial_code) DO UPDATE SET
            glpi_id=excluded.glpi_id,
            codial_nom=excluded.codial_nom,
            synced_at=excluded.synced_at
    """, (codial_code, glpi_id, nom))
    conn.commit()


def save_protected(conn, glpi_id, glpi_nom):
    conn.execute("""
        INSERT INTO protected_entities (glpi_id, glpi_nom)
        VALUES (?, ?)
        ON CONFLICT(glpi_id) DO UPDATE SET glpi_nom=excluded.glpi_nom
    """, (glpi_id, glpi_nom))
    conn.commit()


# ---------------------------------------------------------------------------
# Etape 1 : export CSV
# ---------------------------------------------------------------------------

def cmd_export():
    print("Chargement des clients CODIAL...")
    clients = hfsql_query(SQL_CLIENTS, timeout=120)
    codial_index = {
        row["CODE"].strip(): (row["NOM"].strip(), normalize(row["NOM"].strip()))
        for row in clients
        if row.get("CODE") and row.get("NOM")
    }
    print(f"  {len(codial_index)} clients charges.")

    with GlpiClient() as glpi:
        print("Chargement des entites GLPI...")
        entities = glpi._request("GET", "/Entity", params={"range": "0-9999"})

    if not isinstance(entities, list):
        print("Erreur : reponse inattendue de GET /Entity")
        sys.exit(1)

    # IDs qui ont au moins un enfant dans la hierarchie GLPI
    parent_ids = {e.get("entities_id") for e in entities if e.get("entities_id") is not None}

    # Entites sans registration_number (pas encore mappees), hors exclusions
    to_map = [
        e for e in entities
        if not (e.get("registration_number") or "").strip()
        and e["id"] not in EXCLUDED_IDS
    ]
    print(f"  {len(entities)} entites GLPI, dont {len(to_map)} a traiter (hors exclusions).\n")

    rows = []
    for entity in to_map:
        glpi_id   = entity["id"]
        glpi_nom  = (entity.get("name") or "").strip()
        glpi_norm = normalize(glpi_nom)
        has_children = glpi_id in parent_ids
        is_internal  = glpi_id in INTERNAL_IDS

        matches = best_matches(glpi_norm, codial_index)
        score1, code1, nom1 = matches[0] if len(matches) > 0 else (0, "", "")
        score2, code2, nom2 = matches[1] if len(matches) > 1 else (0, "", "")
        score3, code3, nom3 = matches[2] if len(matches) > 2 else (0, "", "")

        if is_internal:
            note, valide, protege = "INTERNE", 0, 0
        elif has_children and score1 < 0.85:
            # Conteneur sans equivalent CODIAL clair → suggerer protection
            note, valide, protege = "CONTENEUR", 0, 1
        else:
            note    = ""
            valide  = 1 if score1 >= 0.85 else 0
            protege = 0

        rows.append({
            "glpi_id":     glpi_id,
            "glpi_nom":    glpi_nom,
            "note":        note,
            "code1":       code1,
            "nom1":        nom1,
            "score1":      f"{score1:.2f}",
            "code2":       code2,
            "nom2":        nom2,
            "score2":      f"{score2:.2f}",
            "code3":       code3,
            "nom3":        nom3,
            "score3":      f"{score3:.2f}",
            "code_retenu": code1,
            "valide":      valide,
            "protege":     protege,
        })

        if is_internal:
            print(f"  [INTERNE]    [{glpi_id:>4}] {glpi_nom}")
        elif protege:
            print(f"  [CONTENEUR]  [{glpi_id:>4}] {glpi_nom:<35} (meilleur candidat: {code1} {nom1} {score1:.2f})")
        else:
            flag = "[AUTO]" if valide else "[?]   "
            print(f"  {flag} [{glpi_id:>4}] {glpi_nom:<35} -> {code1} {nom1} ({score1:.2f})")

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys(), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    auto      = sum(1 for r in rows if r["valide"] == 1)
    conteneur = sum(1 for r in rows if r["protege"] == 1)
    manual    = len(rows) - auto - conteneur - sum(1 for r in rows if r["note"] == "INTERNE")
    print(f"\nCSV genere : {CSV_PATH}")
    print(f"  {auto} correspondances auto-validees (score >= 0.85)")
    print(f"  {conteneur} conteneurs suggeres (protege=1 — verifier)")
    print(f"  {manual} a verifier manuellement (valide=0)")
    print("\nActions :")
    print("  1. Ouvrez entity_map_review.csv dans Excel")
    print("  2. Lignes protege=1 : verifiez que c'est bien un conteneur sans equivalent CODIAL")
    print("     Si vous avez finalement un code CODIAL pour cette entite :")
    print("     -> mettez protege=0, code_retenu=<code>, valide=1")
    print("  3. Lignes valide=0 : mettre valide=1 pour confirmer, laisser 0 pour ignorer")
    print("  4. Relancez avec --apply")


# ---------------------------------------------------------------------------
# Etape 2 : apply
# ---------------------------------------------------------------------------

def cmd_apply():
    if not os.path.exists(CSV_PATH):
        print(f"Fichier introuvable : {CSV_PATH}")
        print("Lancez d'abord --export.")
        sys.exit(1)

    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    to_apply   = [r for r in rows if r.get("valide", "0").strip() == "1" and r.get("code_retenu", "").strip()]
    to_protect = [r for r in rows if r.get("protege", "0").strip() == "1"]

    # Verifier incoherences valide=1 ET protege=1
    conflicts = [r for r in rows if r.get("valide", "0").strip() == "1" and r.get("protege", "0").strip() == "1"]
    if conflicts:
        print("ATTENTION : les lignes suivantes ont valide=1 ET protege=1 (incompatible) :")
        for r in conflicts:
            print(f"  [{r['glpi_id']}] {r['glpi_nom']}")
        print("Corrigez le CSV avant de relancer --apply.")
        sys.exit(1)

    print(f"{len(to_apply)} mapping(s) a appliquer (valide=1).")
    print(f"{len(to_protect)} entite(s) a proteger (protege=1).\n")

    db = init_db()
    stats = {"ok": 0, "err": 0, "protected": 0}

    # --- Mappings CODIAL ---
    if to_apply:
        with GlpiClient() as glpi:
            clients_raw = hfsql_query("SELECT CODE, NOM FROM CLIENT WHERE FICHE_SUPPRIME = 0", timeout=120)
            codial_noms = {r["CODE"].strip(): r["NOM"].strip() for r in clients_raw if r.get("CODE")}

            for row in to_apply:
                glpi_id  = int(row["glpi_id"])
                glpi_nom = row["glpi_nom"]
                code     = row["code_retenu"].strip()
                nom      = codial_noms.get(code, code)

                try:
                    glpi.update("Entity", glpi_id, {"registration_number": code})
                    save_mapping(db, code, glpi_id, nom)
                    stats["ok"] += 1
                    print(f"  [OK]     GLPI {glpi_id} ({glpi_nom}) -> CODIAL {code} ({nom})")
                except Exception as e:
                    stats["err"] += 1
                    print(f"  [ERR]    GLPI {glpi_id} ({glpi_nom}) : {e}")

    # --- Entites protegees ---
    for row in to_protect:
        glpi_id  = int(row["glpi_id"])
        glpi_nom = row["glpi_nom"]
        save_protected(db, glpi_id, glpi_nom)
        stats["protected"] += 1
        print(f"  [PROTEGE] GLPI {glpi_id} ({glpi_nom}) — jamais touche par le sync")

    db.close()
    print(f"\nResultat : {stats['ok']} mappes, {stats['protected']} proteges, {stats['err']} erreurs.")
    if stats["ok"]:
        print("Les entites mappees seront mises a jour (et non recrees) lors du prochain sync_entities.")
    if stats["protected"]:
        print("Pour placer de futurs clients CODIAL sous un conteneur :")
        print("  py -3.14 sync/manage_parents.py --add <CODE_CODIAL> <GLPI_PARENT_ID>")


# ---------------------------------------------------------------------------
# Point d'entree
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--apply" in sys.argv:
        cmd_apply()
    elif "--export" in sys.argv:
        cmd_export()
    else:
        print(__doc__)
        sys.exit(1)
