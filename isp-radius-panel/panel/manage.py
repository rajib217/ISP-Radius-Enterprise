"""Command line helper.

    python3 manage.py create-admin <username> <password> [admin|support]
    python3 manage.py reset-password <username> <new-password>
    python3 manage.py seed-install          (used by install.sh; reads SEED_* environment variables)

Needs the DB_* environment variables (see /etc/isp-panel/panel.env).
"""
import os
import sys

from werkzeug.security import generate_password_hash

import db


def seed_install(conn, cur, env):
    """Write the install wizard's answers: company branding, the super admin and the first router.
    Secrets come from the environment (never the command line, which other users can see). Empty values are skipped,
    so a re-run without a new password keeps the old one. Prints what changed, one word per item."""
    def get(key):
        return (env.get("SEED_" + key) or "").strip()

    done = []
    settings = {"isp_name": get("ISP_NAME"), "support_phone": get("ISP_PHONE"), "support_email": get("ADMIN_EMAIL"),
                "company_address": get("ISP_ADDRESS"), "bkash_callback_base": get("CALLBACK_BASE")}
    if get("BKASH_ON") == "1":
        settings.update({"bkash_on": "1", "bkash_mode": "sandbox" if get("BKASH_MODE") == "sandbox" else "live",
                         "bkash_key": get("BKASH_KEY"), "bkash_secret": get("BKASH_SECRET"),
                         "bkash_user": get("BKASH_USER"), "bkash_pass": get("BKASH_PASS")})
        cur.execute("DELETE FROM settings WHERE skey = 'bkash_token_cache'")
    for key, value in settings.items():
        if value:
            db.save_setting(cur, key, value)
    done.append("settings")

    user, password = get("ADMIN_USER") or "admin", get("ADMIN_PASS")
    profile = (get("ADMIN_NAME") or "Administrator", get("ADMIN_EMAIL"), get("ISP_PHONE"),
               get("ISP_NAME"), get("ISP_ADDRESS"), get("ISP_PHONE"))
    cur.execute("SELECT id FROM panel_admins WHERE username = %s", (user,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE panel_admins SET full_name = %s, email = %s, phone = %s, inv_name = %s, inv_address = %s, "
                    "inv_phone = %s, role = 'admin', is_active = 1 WHERE id = %s", profile + (row["id"],))
        if password:
            cur.execute("UPDATE panel_admins SET password_hash = %s WHERE id = %s", (generate_password_hash(password), row["id"]))
            done.append("admin-password-changed")
        done.append("admin-updated")
    else:
        if not password:
            raise SystemExit("seed-install: a password is needed to create the super admin")
        cur.execute("INSERT INTO panel_admins (username, password_hash, role, full_name, email, phone, inv_name, inv_address, "
                    "inv_phone) VALUES (%s, %s, 'admin', %s, %s, %s, %s, %s, %s)",
                    (user, generate_password_hash(password)) + profile)
        done.append("admin-created")

    ip, name, secret = get("NAS_IP"), get("NAS_NAME") or "MikroTik", get("NAS_SECRET")
    if ip and secret:
        cur.execute("SELECT id, secret, shortname FROM nas WHERE nasname = %s", (ip,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO nas (nasname, shortname, type, secret, description, coa_enabled) "
                        "VALUES (%s, %s, 'mikrotik', %s, 'Added by the installer', 1)", (ip, name[:32], secret))
            done.append("router-added")
        elif row["secret"] != secret or row["shortname"] != name[:32]:
            cur.execute("UPDATE nas SET shortname = %s, secret = %s WHERE id = %s", (name[:32], secret, row["id"]))
            done.append("router-updated")
    conn.commit()
    return " ".join(done)


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
        if cmd == "seed-install":
            print(seed_install(conn, cur, os.environ))
            return 0
        print(__doc__)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
