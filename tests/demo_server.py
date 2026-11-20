"""Run the real panel on your own computer with sample data, to click through it yourself.

    python tests/demo_server.py            # http://127.0.0.1:8080
    python tests/demo_server.py --reset    # start again from fresh sample data

It uses the test MariaDB in WSL (~/mariadb-ci, port 3307; see tests/test_pages.py). On first start, or with --reset,
the database is rebuilt from sql/panel_schema.sql and filled with the same sample data the page test uses.
Afterwards your changes are kept between starts.

Sign in with (password for all: Ci-Pages-123):
    admin   administrator          karim      reseller (10 and 20 Mbps allocated)
    nadia   support staff          karim_sub  sub-reseller (nothing allocated)
Customer portal (/portal): username e.g. mirpur.rahim, password pw-mirpur.rahim

Nothing leaves the computer: ping, radclient, sudo and outgoing HTTP (bKash, SMS, Telegram) are switched off, so
those buttons show an error instead of acting. Never point this at a real database.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WSL_REPO = "/mnt/" + ROOT[0].lower() + ROOT[2:].replace("\\", "/")
os.environ.update({"DB_HOST": "127.0.0.1", "DB_PORT": "3307", "DB_USER": "radius_panel", "DB_PASS": "ci-test",
                   "DB_NAME": "radius", "PANEL_SECRET_KEY": os.environ.get("PANEL_SECRET_KEY", "demo-only")})
os.environ.setdefault("PANEL_DATA_DIR", os.path.join(ROOT, ".demo-data"))
PORT = int(os.environ.get("DEMO_PORT", "8080"))


def start_db(reset):
    """Start the WSL MariaDB; rebuild it when asked or when it has no data yet. Returns True when rebuilt."""
    script = "~/mariadb-ci/db_up.sh" if reset else "~/mariadb-ci/db_start.sh"
    r = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", '%s "%s"' % (script, WSL_REPO)],
                       capture_output=True, text=True, timeout=180)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        sys.exit("Could not start the test MariaDB in WSL (see above).")
    return reset or "schema loaded" in r.stdout


def main():
    start_db("--reset" in sys.argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import test_pages                      # sets up paths, imports the panel, knows the sample data
    import db
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM subscribers")
        empty = cur.fetchone()["c"] == 0
    if empty:                              # a fresh database: give it the sample data once
        print("== filling in sample data")
        test_pages.seed(conn)
    conn.close()

    def blocked(*a, **k):
        raise OSError("switched off in the demo (no processes or network)")
    subprocess.run = subprocess.Popen = subprocess.check_output = subprocess.call = blocked
    import urllib.request
    urllib.request.urlopen = blocked

    import app
    print("\n  Panel:  http://127.0.0.1:%d/   (admin / karim / nadia / karim_sub, password Ci-Pages-123)" % PORT)
    print("  Portal: http://127.0.0.1:%d/portal   (mirpur.rahim / pw-mirpur.rahim)\n" % PORT)
    app.app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
