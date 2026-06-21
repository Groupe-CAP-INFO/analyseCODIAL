"""
Gere les overrides de parent pour le sync CODIAL -> GLPI.

Permet de placer un client CODIAL sous un groupe GLPI specifique
plutot que sous l'entite racine (comportement par defaut).

Usage:
    py -3.14 sync/manage_parents.py --list
        -> affiche les entites protegees et les overrides de parent

    py -3.14 sync/manage_parents.py --add CODE_CODIAL GLPI_PARENT_ID [note]
        -> definit que CODE_CODIAL sera cree sous GLPI_PARENT_ID

    py -3.14 sync/manage_parents.py --remove CODE_CODIAL
        -> supprime l'override (le client retournera sous la racine)

Exemple :
    py -3.14 sync/manage_parents.py --add CNEWCLI 1 "Nouveau client Bellec"
    -> le client CNEWCLI sera cree sous Groupe BELLEC (GLPI id=1)
"""
import os
import sqlite3
import sys

MAPPING_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping.db")


def get_db():
    if not os.path.exists(MAPPING_DB):
        print(f"mapping.db introuvable : {MAPPING_DB}")
        print("Lancez d'abord sync/map_existing_entities.py --apply")
        sys.exit(1)
    return sqlite3.connect(MAPPING_DB)


def cmd_list():
    conn = get_db()

    protected = conn.execute(
        "SELECT glpi_id, glpi_nom, created_at FROM protected_entities ORDER BY glpi_id"
    ).fetchall()
    overrides = conn.execute(
        "SELECT codial_code, glpi_parent_id, note, created_at FROM parent_overrides ORDER BY codial_code"
    ).fetchall()
    conn.close()

    print("=== Entites GLPI protegees (jamais modifiees par le sync) ===")
    if protected:
        for glpi_id, nom, ts in protected:
            print(f"  [{glpi_id:>4}] {nom}  (depuis {ts[:10]})")
        print(f"  Total : {len(protected)}")
    else:
        print("  Aucune entite protegee.")

    print()
    print("=== Overrides de parent (clients CODIAL -> groupe GLPI) ===")
    if overrides:
        for code, parent_id, note, ts in overrides:
            note_str = f"  ({note})" if note else ""
            print(f"  {code:<20} -> GLPI parent={parent_id}{note_str}  (depuis {ts[:10]})")
        print(f"  Total : {len(overrides)}")
    else:
        print("  Aucun override defini.")
        print()
        print("  Pour ajouter un override :")
        print("    py -3.14 sync/manage_parents.py --add CODE_CODIAL GLPI_PARENT_ID [note]")


def cmd_add(args):
    if len(args) < 2:
        print("Usage : --add CODE_CODIAL GLPI_PARENT_ID [note]")
        sys.exit(1)
    codial_code = args[0].strip().upper()
    try:
        glpi_parent_id = int(args[1])
    except ValueError:
        print(f"GLPI_PARENT_ID doit etre un entier, recu : {args[1]!r}")
        sys.exit(1)
    note = " ".join(args[2:]) if len(args) > 2 else ""

    conn = get_db()
    conn.execute("""
        INSERT INTO parent_overrides (codial_code, glpi_parent_id, note, created_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(codial_code) DO UPDATE SET
            glpi_parent_id=excluded.glpi_parent_id,
            note=excluded.note,
            created_at=excluded.created_at
    """, (codial_code, glpi_parent_id, note))
    conn.commit()
    conn.close()
    print(f"Override ajoute : {codial_code} -> GLPI parent={glpi_parent_id}" + (f" ({note})" if note else ""))
    print("Le prochain sync creera ce client sous l'entite GLPI specifiee.")


def cmd_remove(args):
    if not args:
        print("Usage : --remove CODE_CODIAL")
        sys.exit(1)
    codial_code = args[0].strip().upper()

    conn = get_db()
    cursor = conn.execute("DELETE FROM parent_overrides WHERE codial_code=?", (codial_code,))
    conn.commit()
    conn.close()

    if cursor.rowcount:
        print(f"Override supprime : {codial_code} (retournera sous l'entite racine).")
    else:
        print(f"Aucun override trouve pour : {codial_code}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if "--list" in args:
        cmd_list()
    elif "--add" in args:
        cmd_add(args[args.index("--add") + 1:])
    elif "--remove" in args:
        cmd_remove(args[args.index("--remove") + 1:])
    else:
        print(__doc__)
        sys.exit(1)
