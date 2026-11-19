"""Command line helper.

    python3 manage.py create-admin <username> <password> [admin|support]
    python3 manage.py reset-password <username> <new-password>

Needs the DB_* environment variables (see /etc/isp-panel/panel.env).
"""
import sys

from werkzeug.security import generate_password_hash

import db


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    conn = db.connect()
    cur = conn.cursor()
    try:
        if cmd == "create-admin" and len(argv) >= 4:
            role = argv[4] if len(argv) > 4 else "admin"
            if role not in ("admin", "support"):
                print("role must be admin or support")
                return 2
            cur.execute("SELECT id FROM panel_admins WHERE username = %s", (argv[2],))
            if cur.fetchone():
                print("exists")
                return 0
            cur.execute(
                "INSERT INTO panel_admins (username, full_name, password_hash, role) "
                "VALUES (%s, %s, %s, %s)",
                (argv[2], "Administrator" if role == "admin" else argv[2],
                 generate_password_hash(argv[3]), role),
            )
            conn.commit()
            print("created")
            return 0
        if cmd == "reset-password" and len(argv) >= 4:
            cur.execute("UPDATE panel_admins SET password_hash = %s, is_active = 1 WHERE username = %s",
                        (generate_password_hash(argv[3]), argv[2]))
            conn.commit()
            print("updated" if cur.rowcount else "no such user")
            return 0 if cur.rowcount else 1
        print(__doc__)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
