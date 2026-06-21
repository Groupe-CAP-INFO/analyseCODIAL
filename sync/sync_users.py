"""
Synchronise les contacts CODIAL (FA4_TIERS_CONTACT) vers les utilisateurs GLPI.
Clé de synchro : ID_CONTACT (CODIAL) → user_map dans mapping.db.
Filtre : TYPE_TIERS = 1 (clients), clients présents dans entity_map.

Usage:
    python sync/sync_users.py             # sync réelle
    python sync/sync_users.py --dry-run   # simulation sans écriture
    python sync/sync_users.py --stats     # compte uniquement, sans sync
"""
import os
import re
import sqlite3
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hfsql_bridge import query as hfsql_query
from glpi_client import GlpiClient

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")

SQL_CONTACTS = (
    "SELECT ID_CONTACT, CODE, TYPE_TIERS, NOM_CONTACT, PRENOM, CIVILITE_CONTACT, "
    "EMAIL_CONTACT, TEL_CONTACT, PORT_CONTACT, FONCTION_CONTACT, INT_PRINCIPALE, COMMENTAIRE "
    "FROM FA4_TIERS_CONTACT "
    "WHERE TYPE_TIERS = 1 AND NOM_CONTACT <> '' AND NOM_CONTACT <> '-Inconnu-' "
    "ORDER BY CODE, INT_PRINCIPALE DESC, ID_CONTACT"
)

_NOISE_NAMES = {"-inconnu-", "inconnu", ""}

_CLEAN_PREFIX = re.compile(
    r"^(m\.?|mr\.?|mme\.?|dr\.?|mlle\.?|m/mme\.?)\s+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def slugify(s):
    s = _strip_accents(s.lower())
    s = re.sub(r"[^a-z0-9]+", ".", s)
    return s.strip(".")


def clean_name(s):
    s = (s or "").strip()
    s = _CLEAN_PREFIX.sub("", s)
    return s.strip()


def is_valid_email(s):
    return bool(s and "@" in s and "." in s.split("@")[-1] and len(s) >= 5)


def make_login(contact):
    email = (contact.get("EMAIL_CONTACT") or "").strip()
    if is_valid_email(email):
        return email.lower()
    prenom = slugify((contact.get("PRENOM") or "").strip())
    nom = slugify(clean_name(contact.get("NOM_CONTACT") or ""))
    contact_id = (contact.get("ID_CONTACT") or "").strip()
    parts = [p for p in [prenom, nom] if p]
    base = ".".join(parts) if parts else "contact"
    return f"{base}.{contact_id}"


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
        CREATE TABLE IF NOT EXISTS user_map (
            codial_id   TEXT PRIMARY KEY,
            glpi_id     INTEGER NOT NULL,
            login       TEXT,
            synced_at   TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def load_entity_map(conn):
    rows = conn.execute("SELECT codial_code, glpi_id FROM entity_map").fetchall()
    return {code: gid for code, gid in rows}


def load_user_map(conn):
    rows = conn.execute("SELECT codial_id, glpi_id FROM user_map").fetchall()
    return {cid: gid for cid, gid in rows}


def save_user_map(conn, codial_id, glpi_id, login):
    conn.execute("""
        INSERT INTO user_map (codial_id, glpi_id, login, synced_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(codial_id) DO UPDATE SET
            glpi_id=excluded.glpi_id,
            login=excluded.login,
            synced_at=excluded.synced_at
    """, (str(codial_id), glpi_id, login))
    conn.commit()


# ---------------------------------------------------------------------------
# Construction du payload GLPI
# ---------------------------------------------------------------------------

def build_user_fields(contact, entity_id, login):
    nom = clean_name(contact.get("NOM_CONTACT") or "")
    prenom = (contact.get("PRENOM") or "").strip()
    fonction = (contact.get("FONCTION_CONTACT") or "").strip()
    commentaire = (contact.get("COMMENTAIRE") or "").strip()
    tel = (contact.get("TEL_CONTACT") or "").strip()
    mobile = (contact.get("PORT_CONTACT") or "").strip()

    fields = {
        "name": login,
        "realname": nom,
        "firstname": prenom,
        "entities_id": entity_id,
        "is_active": 1,
    }
    if tel:
        fields["phone"] = tel
    if mobile:
        fields["mobile"] = mobile
    comment_parts = [p for p in [fonction, commentaire] if p]
    if comment_parts:
        fields["comment"] = " | ".join(comment_parts)
    return fields


def set_user_email(glpi, glpi_user_id, email):
    """Crée ou met à jour l'email principal de l'utilisateur via UserEmail."""
    try:
        glpi.create("UserEmail", {
            "users_id": glpi_user_id,
            "email": email,
            "is_default": 1,
            "is_dynamic": 0,
        })
    except Exception:
        pass  # peut échouer si l'email existe déjà — non bloquant


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_users(dry_run=False, stats_only=False):
    tag = " [DRY-RUN]" if dry_run else (" [STATS]" if stats_only else "")
    print(f"=== Sync CODIAL Contacts -> GLPI Users{tag} ===\n")

    print("Chargement des contacts CODIAL...")
    contacts = hfsql_query(SQL_CONTACTS)
    print(f"  {len(contacts)} contacts bruts (TYPE_TIERS=1, nom non vide).\n")

    db = init_db()
    entity_map = load_entity_map(db)
    user_map   = load_user_map(db)

    print(f"  {len(entity_map)} entités mappées dans entity_map.")
    print(f"  {len(user_map)} contacts déjà dans user_map.\n")

    # Filtrer : seuls les clients dans entity_map
    linked = [c for c in contacts if (c.get("CODE") or "").strip() in entity_map]
    skipped_no_entity = len(contacts) - len(linked)
    print(f"  {len(linked)} contacts rattachés à une entité GLPI connue.")
    print(f"  {skipped_no_entity} ignorés (client non dans entity_map).\n")

    if stats_only:
        already  = sum(1 for c in linked if (c.get("ID_CONTACT") or "").strip() in user_map)
        to_create = len(linked) - already
        print(f"  A créer  : {to_create}")
        print(f"  A mettre à jour : {already}")
        db.close()
        return

    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    with GlpiClient() as glpi:
        # Charger les logins existants pour détecter les collisions
        existing_logins_resp = glpi._request("GET", "/User", params={"range": "0-9999"})
        existing_logins = {}
        if isinstance(existing_logins_resp, list):
            for u in existing_logins_resp:
                login = (u.get("name") or "").strip()
                if login:
                    existing_logins[login] = u["id"]

        used_logins = set(existing_logins.keys())

        for i, contact in enumerate(linked, 1):
            codial_id  = (contact.get("ID_CONTACT") or "").strip()
            code       = (contact.get("CODE") or "").strip()
            nom        = clean_name(contact.get("NOM_CONTACT") or "")
            email      = (contact.get("EMAIL_CONTACT") or "").strip()
            entity_id  = entity_map[code]

            if not codial_id or not nom:
                stats["skipped"] += 1
                continue

            if i % 100 == 0:
                print(f"  ... {i}/{len(linked)} traités")

            login = make_login(contact)

            try:
                if codial_id in user_map:
                    # Mise à jour
                    glpi_id = user_map[codial_id]
                    fields = build_user_fields(contact, entity_id, login)
                    if not dry_run:
                        glpi.update("User", glpi_id, fields)
                        if is_valid_email(email):
                            set_user_email(glpi, glpi_id, email)
                        save_user_map(db, codial_id, glpi_id, login)
                    stats["updated"] += 1

                else:
                    # Création — éviter les collisions de login
                    final_login = login
                    if final_login in used_logins:
                        final_login = f"{login}.{codial_id}"
                    if final_login in used_logins:
                        stats["skipped"] += 1
                        print(f"  [SKIP] Login collision : {final_login} (ID_CONTACT={codial_id})")
                        continue

                    fields = build_user_fields(contact, entity_id, final_login)
                    if not dry_run:
                        resp = glpi.create("User", fields)
                        glpi_id = resp[0]["id"] if isinstance(resp, list) else resp["id"]
                        if is_valid_email(email):
                            set_user_email(glpi, glpi_id, email)
                        save_user_map(db, codial_id, glpi_id, final_login)
                        used_logins.add(final_login)
                    stats["created"] += 1

            except Exception as e:
                stats["errors"] += 1
                print(f"  [ERR] ID={codial_id} {nom} ({code}) : {e}")

    db.close()

    print(f"\n--- Résumé ---")
    print(f"  Créés    : {stats['created']}")
    print(f"  Mis à j. : {stats['updated']}")
    print(f"  Ignorés  : {stats['skipped']}")
    print(f"  Erreurs  : {stats['errors']}")
    if dry_run:
        print("\n  Simulation terminée — aucune modification appliquée.")


if __name__ == "__main__":
    dry_run    = "--dry-run" in sys.argv
    stats_only = "--stats"   in sys.argv
    sync_users(dry_run=dry_run, stats_only=stats_only)
