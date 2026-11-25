"""Database backup and restore, plus the Google Drive copy.

Used three ways:
  * imported by the panel pages (mod_backup.py) for listing, Google sign-in and file checks
  * run by the panel as a detached worker:  python3 backuplib.py backup | restore NAME | fetch FILE_ID
  * run by the systemd timer every 10 minutes:  python3 backuplib.py scheduled

A backup is one .tar file holding everything needed to bring the panel back:
  database.sql.gz      a gzip-compressed SQL dump of the whole panel + RADIUS database (customers, ledgers, payments,
                       packages, users, every setting, tickets, RADIUS users and accounting)
  files/photos/ ...    the files the panel keeps outside the database: customer photos, documents, ticket work photos
                       and the logo / branding images (FILE_DIRS)
  manifest.json        what is inside
Older backups are a bare .sql.gz (database only); they still restore. Not included on purpose: /etc/isp-panel/panel.env
(PANEL_SETTINGS_KEY reads the encrypted secret settings; keep a copy of it apart from the backups).
"""
import glob
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import fcntl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

DATA_DIR = os.environ.get("PANEL_DATA_DIR", "/var/lib/isp-panel")
DIR = os.path.join(DATA_DIR, "backups")
STATUS = os.path.join(DIR, "status.json")
LOCK = os.path.join(DIR, ".lock")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,118}\.(sql\.gz|tar)$")
FILE_DIRS = ("photos", "documents", "ticket_photos", "branding")     # folders in DATA_DIR that go into a backup
DB_MEMBER = "database.sql.gz"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD = 4 * 1024 ** 3
SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_NAME = "ISP Panel Backups"


class BackupError(Exception):
    pass


# ------------------------------------------------------------------ files
def ensure_dir():
    os.makedirs(DIR, mode=0o750, exist_ok=True)


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and ".." not in name


def path_of(name):
    if not valid_name(name):
        raise BackupError("That is not a backup file name.")
    return os.path.join(DIR, name)


def kind_of(name):
    for k in ("pre-restore", "uploaded", "drive"):
        if name.startswith(k + "-"):
            return k
    return "auto" if name.startswith("auto-") else "manual"


def list_backups():
    ensure_dir()
    out = []
    for p in glob.glob(os.path.join(DIR, "*.sql.gz")) + glob.glob(os.path.join(DIR, "*.tar")):
        name = os.path.basename(p)
        if not valid_name(name):
            continue
        st = os.stat(p)
        out.append({"name": name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime), "kind": kind_of(name),
                    "full": name.endswith(".tar")})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def disk_free():
    ensure_dir()
    return shutil.disk_usage(DIR).free


# ------------------------------------------------------------------ status + lock
def read_status():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_status(**kw):
    ensure_dir()
    data = read_status()
    data.update(kw)
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATUS)


def busy():
    """True while a backup or restore worker holds the lock."""
    ensure_dir()
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


class Lock:
    def __enter__(self):
        ensure_dir()
        self.fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o640)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise BackupError("Another backup or restore is already running.")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def start_worker(*args):
    """Start this file as a detached process so the web request returns at once."""
    ensure_dir()
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + [str(a) for a in args],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


# ------------------------------------------------------------------ database tools
def _tool(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise BackupError("%s is not installed (apt install mariadb-client)." % names[0])


class _Creds:
    """A temporary 0600 option file so the database password never appears on a command line."""

    def __enter__(self):
        ensure_dir()
        fd, self.path = tempfile.mkstemp(prefix=".my-", suffix=".cnf", dir=DIR)
        with os.fdopen(fd, "w") as f:
            f.write("[client]\nuser=%s\npassword=\"%s\"\nhost=%s\nport=%s\n" % (
                os.environ.get("DB_USER", "radius_panel"),
                os.environ.get("DB_PASS", "").replace("\\", "\\\\").replace('"', '\\"'),
                os.environ.get("DB_HOST", "127.0.0.1"), os.environ.get("DB_PORT", "3306")))
        os.chmod(self.path, 0o600)
        return self.path

    def __exit__(self, *a):
        try:
            os.remove(self.path)
        except OSError:
            pass


DBNAME = lambda: os.environ.get("DB_NAME", "radius")  # noqa: E731


def make_dump(label=""):
    """Dump the database into a new .sql.gz and return its name."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "%s-%s-%s.sql.gz" % (label or "manual", DBNAME(), stamp)
    final, part = os.path.join(DIR, name), os.path.join(DIR, name + ".part")
    tail = b""
    try:
        with _Creds() as cnf:
            cmd = [_tool("mariadb-dump", "mysqldump"), "--defaults-extra-file=" + cnf, "--single-transaction",
                   "--quick", "--routines", "--triggers", "--default-character-set=utf8mb4", DBNAME()]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with gzip.open(part, "wb", compresslevel=6) as out:
                while True:
                    chunk = proc.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    tail = (tail + chunk)[-400:]
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            if proc.wait() != 0:
                raise BackupError("The database dump failed: %s" % (err[-300:] or "unknown error"))
        if b"Dump completed" not in tail:
            raise BackupError("The dump ended early and was discarded.")
        os.chmod(part, 0o640)
        os.replace(part, final)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return name


def check_dump(path, deep=False):
    """Raise BackupError unless the file (a path or an open binary file) looks like a complete dump. deep reads it all."""
    import zlib
    try:
        with gzip.open(path, "rb") if isinstance(path, str) else gzip.GzipFile(fileobj=path, mode="rb") as f:
            head = f.read(65536)
            if not re.search(rb"(MariaDB|MySQL) dump", head[:4096]):
                raise BackupError("This is not a database dump made by this panel.")
            if deep:
                tail = head[-400:]
                while True:
                    chunk = f.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    tail = (tail + chunk)[-400:]
                if b"Dump completed" not in tail:
                    raise BackupError("The file is incomplete (the dump has no end marker). Upload it again.")
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
        raise BackupError("The file is not a valid .sql.gz archive or is damaged.")


# "DEFINER=`root`@`localhost`" on triggers / views / routines: the dump names who created them (root, when install.sh
# or update.sh loaded the schema). The panel's own database user may not create objects in someone else's name (that
# needs SUPER), so the restore would stop there. Dropped, the panel user owns them; they work the same.
DEFINER_RE = re.compile(rb"DEFINER\s*=\s*(`[^`]*`|'[^']*'|[^\s@]+)@(`[^`]*`|'[^']*'|[^\s*]+)\s*")


def load_dump(path):
    """Replace the database contents with the dump."""
    with _Creds() as cnf:
        cmd = [_tool("mariadb", "mysql"), "--defaults-extra-file=" + cnf, "--max-allowed-packet=512M",
               "--default-character-set=utf8mb4", DBNAME()]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with gzip.open(path, "rb") as f:
                for line in f:                  # line by line, so a DEFINER clause is never split between two reads
                    if b"DEFINER" in line and not line.startswith(b"INSERT "):     # never touch the data rows
                        line = DEFINER_RE.sub(b"", line)
                    proc.stdin.write(line)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if proc.wait() != 0:
            raise BackupError(err[-300:] or "The database refused the file.")


def prune_local(keep):
    """Keep the newest `keep` automatic / manual backups and the newest 3 made before a restore. Uploaded and
    Drive-downloaded files stay 30 days (the newest 3 always stay), so they cannot fill the disk."""
    rows = [r for r in list_backups() if r["kind"] in ("auto", "manual")]
    gone = rows[max(keep, 1):]
    gone += [r for r in list_backups() if r["kind"] == "pre-restore"][3:]
    other = [r for r in list_backups() if r["kind"] in ("uploaded", "drive")]
    gone += [r for r in other[3:] if (datetime.now() - r["mtime"]).days > 30]
    for r in gone:
        try:
            os.remove(os.path.join(DIR, r["name"]))
        except OSError:
            pass


def clean_leftovers():
    """Remove what a killed job can leave behind: the temporary database option file (it holds the password),
    half-written parts and unpacked folders older than an hour."""
    ensure_dir()
    now = time.time()
    for pat in (".my-*.cnf", "*.part", ".restore-*", ".unpack-*"):
        for p in glob.glob(os.path.join(DIR, pat)) + glob.glob(os.path.join(DATA_DIR, pat)):
            try:
                if now - os.path.getmtime(p) > 3600:
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            except OSError:
                pass


# ------------------------------------------------------------------ the full backup (.tar: database + files)
def _add_file(tar, path, arcname):
    """One plain file into the archive (no owner names, no links: only the bytes, its size, time and mode)."""
    st = os.stat(path)
    ti = tarfile.TarInfo(arcname)
    ti.size, ti.mtime, ti.mode = st.st_size, int(st.st_mtime), 0o640
    with open(path, "rb") as f:
        tar.addfile(ti, f)


def make_backup(label=""):
    """Dump the database, then pack it with the file folders (FILE_DIRS) into one .tar. Returns its name."""
    db_name = make_dump(label)
    db_path = os.path.join(DIR, db_name)
    name = db_name[:-len(".sql.gz")] + ".tar"
    final, part = os.path.join(DIR, name), os.path.join(DIR, name + ".part")
    counts = {}
    try:
        with tarfile.open(part, "w", format=tarfile.PAX_FORMAT) as tar:
            _add_file(tar, db_path, DB_MEMBER)
            for folder in FILE_DIRS:
                src = os.path.join(DATA_DIR, folder)
                d = tarfile.TarInfo("files/" + folder)
                d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o750, int(time.time())
                tar.addfile(d)                    # always there: a restore then knows to replace this folder
                n = 0
                for root, dirs, names in os.walk(src):
                    dirs[:] = [x for x in dirs if not x.startswith(".")]
                    for fn in names:
                        p = os.path.join(root, fn)
                        if fn.startswith(".") or os.path.islink(p) or not os.path.isfile(p):
                            continue
                        rel = os.path.relpath(p, src).replace(os.sep, "/")
                        _add_file(tar, p, "files/%s/%s" % (folder, rel))
                        n += 1
                counts[folder] = n
            info = json.dumps({"format": 1, "created": datetime.now().isoformat(timespec="seconds"), "database": DBNAME(),
                               "files": counts}).encode()
            m = tarfile.TarInfo("manifest.json")
            m.size, m.mtime, m.mode = len(info), int(time.time()), 0o640
            import io
            tar.addfile(m, io.BytesIO(info))
        os.chmod(part, 0o640)
        os.replace(part, final)
    finally:
        for p in (part, db_path):
            if os.path.exists(p):
                os.remove(p)
    return name


def _members(tar):
    """The archive's members, refused unless every one is a plain file or folder inside the known layout."""
    out = []
    for m in tar.getmembers():
        n = m.name
        parts = n.split("/")
        if n.startswith("/") or "\\" in n or any(p in ("", ".", "..") for p in parts) or not (m.isfile() or m.isdir()):
            raise BackupError("The backup file holds something unexpected (%s) and was not used." % n[:80])
        if not (n in ("manifest.json", DB_MEMBER, "files") or (parts[0] == "files" and parts[1] in FILE_DIRS)):
            raise BackupError("The backup file holds something unexpected (%s) and was not used." % n[:80])
        out.append(m)
    return out


def check_backup(path, deep=False, as_name=None):
    """Raise BackupError unless `path` is a usable backup (.sql.gz, or a .tar with the database inside).
    as_name: the name the file will have (an upload is checked while it is still a .part file)."""
    if not (as_name or path).endswith(".tar"):
        return check_dump(path, deep)
    try:
        with tarfile.open(path, "r:") as tar:
            ms = {m.name: m for m in _members(tar)}
            if DB_MEMBER not in ms or not ms[DB_MEMBER].isfile():
                raise BackupError("This archive has no database in it, so it is not a panel backup.")
            check_dump(tar.extractfile(ms[DB_MEMBER]), deep)
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("The file is not a valid backup archive or is damaged.")


def backup_contents(path):
    """{'files': {folder: count}} from a .tar backup's manifest; {} for a database-only backup."""
    if not path.endswith(".tar"):
        return {}
    try:
        with tarfile.open(path, "r:") as tar:
            return json.loads(tar.extractfile("manifest.json").read().decode() or "{}")
    except (tarfile.TarError, OSError, KeyError, ValueError, AttributeError):
        return {}


def _db_file(path):
    """A path to the database dump of a backup: the file itself, or the dump unpacked from a .tar (temporary)."""
    if not path.endswith(".tar"):
        return path, False
    fd, tmp = tempfile.mkstemp(prefix=".restore-db-", suffix=".sql.gz", dir=DIR)
    with os.fdopen(fd, "wb") as out, tarfile.open(path, "r:") as tar:
        _members(tar)
        shutil.copyfileobj(tar.extractfile(DB_MEMBER), out, 1024 * 1024)
    return tmp, True


def restore_files(path):
    """Put the file folders of a .tar backup back: each folder in the archive replaces the one on disk. Returns
    {folder: count}. A database-only backup changes no files."""
    if not path.endswith(".tar"):
        return {}
    done = {}
    with tarfile.open(path, "r:") as tar:
        ms = _members(tar)
        for folder in FILE_DIRS:
            prefix = "files/%s" % folder
            if not any(m.name == prefix for m in ms):
                continue
            tmp = tempfile.mkdtemp(prefix=".unpack-%s-" % folder, dir=DATA_DIR)
            n = 0
            for m in ms:
                if not m.isfile() or not m.name.startswith(prefix + "/"):
                    continue
                dest = os.path.join(tmp, *m.name[len(prefix) + 1:].split("/"))
                if not os.path.abspath(dest).startswith(os.path.abspath(tmp) + os.sep):
                    raise BackupError("The backup file holds an unsafe path and was not used.")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with tar.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                n += 1
            live = os.path.join(DATA_DIR, folder)
            old = None
            if os.path.exists(live):
                old = os.path.join(DATA_DIR, ".restore-old-%s-%d" % (folder, int(time.time())))
                os.replace(live, old)
            os.replace(tmp, live)
            os.chmod(live, 0o750)
            if old:
                shutil.rmtree(old, ignore_errors=True)
            done[folder] = n
    return done


def _sql_dir():
    for d in (os.path.join(APP_DIR, "sql"), os.path.join(APP_DIR, "..", "sql")):
        if os.path.isfile(os.path.join(d, "panel_schema.sql")):
            return d
    return None


def apply_schema():
    """After a restore: bring the tables up to this version of the panel (panel_schema.sql, then the migrations not
    recorded yet), so an older backup does not miss tables or columns newer pages need. Returns a warning or ''."""
    d = _sql_dir()
    if not d:
        return "The schema files were not found next to the panel; run update.sh to bring the tables up to date."
    try:
        _run_sql_file(os.path.join(d, "panel_schema.sql"))
        import db
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INT UNSIGNED NOT NULL PRIMARY KEY, "
                            "name VARCHAR(128) NOT NULL DEFAULT '', applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) "
                            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
                cur.execute("SELECT version FROM schema_migrations")
                done = {int(r["version"]) for r in cur.fetchall()}
            conn.commit()
            for f in sorted(glob.glob(os.path.join(d, "migrations", "[0-9]*.sql"))):
                base = os.path.basename(f)
                ver = int(base.split("_", 1)[0])
                if ver in done:
                    continue
                _run_sql_file(f)
                with conn.cursor() as cur:
                    cur.execute("INSERT IGNORE INTO schema_migrations (version, name) VALUES (%s, %s)", (ver, base[:-4][:128]))
                conn.commit()
        finally:
            conn.close()
    except BackupError as e:
        return "The data was restored, but bringing the tables up to date failed (%s). Run update.sh." % e
    except Exception as e:                         # the database connection itself
        return "The data was restored, but bringing the tables up to date failed (%s). Run update.sh." % e
    return ""


def _run_sql_file(path):
    with _Creds() as cnf, open(path, "rb") as f:
        proc = subprocess.run([_tool("mariadb", "mysql"), "--defaults-extra-file=" + cnf, "--default-character-set=utf8mb4",
                               DBNAME()], stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise BackupError("%s: %s" % (os.path.basename(path), proc.stderr.decode("utf-8", "replace").strip()[-300:]))


def after_restore():
    """Sign everyone in again (the user ids in the restored data may belong to other people now) and say whether
    saved secrets can be read with this server's key. Returns a warning or ''."""
    import db
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            db.save_setting(cur, "session_epoch", "%d" % int(time.time() * 1000))
            cur.execute("SELECT skey, svalue FROM settings WHERE svalue LIKE 'enc:v1:%%'")
            bad = [r["skey"] for r in cur.fetchall() if db._try_decrypt(r["svalue"]) is None]
        conn.commit()
    finally:
        conn.close()
    if bad:
        return ("%d saved secret setting(s) cannot be read with this server's key (the backup is from another server, or "
                "PANEL_SETTINGS_KEY changed): %s. Copy that server's PANEL_SETTINGS_KEY into panel.env, or type them again."
                % (len(bad), ", ".join(bad[:8])))
    return ""


# ------------------------------------------------------------------ Google Drive (OAuth for devices)
def _http(url, data=None, headers=None, method=None, timeout=60, raw=False):
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return (r, body) if raw else json.loads(body.decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read().decode())
        except (ValueError, OSError):
            info = {}
        err = info.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or ""
        desc = info.get("error_description") or ""
        raise BackupError("Google answered HTTP %s%s." % (e.code, (": " + (desc or msg)) if (desc or msg) else ""))
    except (urllib.error.URLError, OSError):
        raise BackupError("Could not reach Google. Check this server's internet connection.")


def drive_start(client_id):
    d = _http("https://oauth2.googleapis.com/device/code", {"client_id": client_id, "scope": SCOPE}, method="POST")
    if "device_code" not in d:
        raise BackupError("Google did not return a sign-in code.")
    return {"device_code": d["device_code"], "user_code": d["user_code"],
            "url": d.get("verification_url") or d.get("verification_uri") or "https://www.google.com/device",
            "expires": int(time.time()) + int(d.get("expires_in", 1800)), "interval": int(d.get("interval", 5))}


def drive_poll(client_id, secret, device_code):
    """Return the refresh token once the person has approved, None while still waiting."""
    try:
        d = _http("https://oauth2.googleapis.com/token",
                  {"client_id": client_id, "client_secret": secret, "device_code": device_code,
                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, method="POST")
    except BackupError as e:
        if "authorization_pending" in str(e) or "slow_down" in str(e):
            return None
        if "access_denied" in str(e):
            raise BackupError("Access was declined on the Google page.")
        if "expired" in str(e):
            raise BackupError("The code expired. Start again.")
        raise
    if not d.get("refresh_token"):
        raise BackupError("Google did not give a refresh token. Remove the app at myaccount.google.com/permissions and connect again.")
    return d["refresh_token"]


def _access_token(s):
    try:
        d = _http("https://oauth2.googleapis.com/token",
                  {"client_id": s["gdrive_client_id"], "client_secret": s["gdrive_client_secret"],
                   "refresh_token": s["gdrive_refresh_token"], "grant_type": "refresh_token"}, method="POST")
    except BackupError as e:
        if "invalid_grant" in str(e) or "Token has been expired" in str(e):
            raise BackupError("Google no longer accepts the saved sign-in. If the Google Cloud app is still in "
                              "Testing mode, move it to In production; then connect Google Drive again.")
        raise
    return d["access_token"]


def _auth(tok, extra=None):
    h = {"Authorization": "Bearer " + tok}
    h.update(extra or {})
    return h


def drive_folder(s, tok, cur=None):
    """The folder id (created on first use). Saved in settings."""
    fid = s.get("gdrive_folder_id")
    if fid:
        try:
            info = _http("https://www.googleapis.com/drive/v3/files/%s?fields=id,trashed" % urllib.parse.quote(fid),
                         headers=_auth(tok))
            if not info.get("trashed"):
                return fid
        except BackupError:
            pass
    q = "name = '%s' and mimeType = 'application/vnd.google-apps.folder' and trashed = false" % FOLDER_NAME
    found = _http("https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}),
                  headers=_auth(tok)).get("files", [])
    if found:
        fid = found[0]["id"]
    else:
        fid = _http("https://www.googleapis.com/drive/v3/files?fields=id",
                    json.dumps({"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}).encode(),
                    _auth(tok, {"Content-Type": "application/json"}), method="POST")["id"]
    if cur is not None:
        import db
        db.save_setting(cur, "gdrive_folder_id", fid)
    s["gdrive_folder_id"] = fid
    return fid


def drive_upload(path, s, cur=None):
    tok = _access_token(s)
    fid = drive_folder(s, tok, cur)
    size = os.path.getsize(path)
    meta = json.dumps({"name": os.path.basename(path), "parents": [fid]}).encode()
    r, _ = _http("https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id",
                 meta, _auth(tok, {"Content-Type": "application/json; charset=UTF-8",
                                   "X-Upload-Content-Type": "application/gzip", "X-Upload-Content-Length": str(size)}),
                 method="POST", raw=True)
    session = r.headers.get("Location")
    if not session:
        raise BackupError("Google did not start the upload.")
    with open(path, "rb") as f:
        req = urllib.request.Request(session, data=f, method="PUT",
                                     headers={"Content-Length": str(size), "Content-Type": "application/gzip"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise BackupError("Google refused the upload (HTTP %s)." % e.code)
        except (urllib.error.URLError, OSError):
            raise BackupError("The upload to Google Drive was interrupted.")
    return tok, fid


def drive_files(s, tok=None):
    tok = tok or _access_token(s)
    fid = drive_folder(s, tok)
    out, page = [], ""
    while True:
        qs = {"q": "'%s' in parents and trashed = false" % fid, "pageSize": 200,
              "fields": "nextPageToken,files(id,name,size,createdTime)", "orderBy": "createdTime desc"}
        if page:
            qs["pageToken"] = page
        d = _http("https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(qs), headers=_auth(tok))
        out += d.get("files", [])
        page = d.get("nextPageToken")
        if not page or len(out) > 1000:
            return out


def drive_prune(s, keep):
    files = [f for f in drive_files(s) if valid_name(f.get("name", ""))]
    tok = _access_token(s)
    for f in files[max(keep, 1):]:
        try:
            _http("https://www.googleapis.com/drive/v3/files/%s" % urllib.parse.quote(f["id"]), headers=_auth(tok),
                  method="DELETE")
        except BackupError:
            pass


def drive_fetch(s, file_id):
    tok = _access_token(s)
    info = _http("https://www.googleapis.com/drive/v3/files/%s?fields=name" % urllib.parse.quote(file_id), headers=_auth(tok))
    name = info.get("name", "")
    if not valid_name(name):
        raise BackupError("That Drive file is not a panel backup.")
    dest = os.path.join(DIR, name if not os.path.exists(os.path.join(DIR, name)) else "drive-" + name)
    req = urllib.request.Request("https://www.googleapis.com/drive/v3/files/%s?alt=media" % urllib.parse.quote(file_id),
                                 headers=_auth(tok))
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(dest + ".part", "wb") as out:
            shutil.copyfileobj(r, out, 1024 * 1024)
    except (urllib.error.URLError, OSError):
        if os.path.exists(dest + ".part"):
            os.remove(dest + ".part")
        raise BackupError("The download from Google Drive failed.")
    os.replace(dest + ".part", dest)
    try:
        check_backup(dest, deep=True)
    except BackupError:
        os.remove(dest)
        raise
    return os.path.basename(dest)


# ------------------------------------------------------------------ jobs
def _settings():
    import db
    conn = db.connect()
    cur = conn.cursor()
    return conn, cur, db.load_settings(cur)


def _note(cur, conn, msg, ok):
    import db
    db.save_setting(cur, "backup_last_msg", msg)
    db.save_setting(cur, "backup_last_ok", "1" if ok else "0")
    db.save_setting(cur, "backup_last_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.commit()


def job_backup(label="manual"):
    with Lock():
        write_status(state="running", kind="backup", started=int(time.time()), msg="Dumping the database and packing the files…")
        clean_leftovers()
        conn = cur = None
        try:
            name = make_backup("auto" if label == "auto" else "")
            conn, cur, s = _settings()
            prune_local(int(s.get("backup_keep") or 7))
            msg = "Backup %s saved." % name
            if s.get("gdrive_on") == "1" and s.get("gdrive_refresh_token"):
                write_status(msg="Copying to Google Drive…")
                try:
                    drive_upload(os.path.join(DIR, name), s, cur)
                    drive_prune(s, int(s.get("gdrive_keep") or 14))
                    msg += " Copied to Google Drive."
                except BackupError as e:
                    msg += " The Google Drive copy failed: %s" % e
                    write_status(state="error", msg=msg, finished=int(time.time()))
                    _note(cur, conn, msg, False)
                    return
            write_status(state="ok", msg=msg, finished=int(time.time()), file=name)
            _note(cur, conn, msg, True)
        except BackupError as e:
            write_status(state="error", msg=str(e), finished=int(time.time()))
            if conn:
                _note(cur, conn, str(e), False)
        finally:
            if conn:
                conn.close()


def job_restore(name):
    with Lock():
        path = path_of(name)
        write_status(state="running", kind="restore", started=int(time.time()), msg="Checking the file…", file=name)
        clean_leftovers()
        safety, temps = None, []
        try:
            check_backup(path, deep=True)
            write_status(msg="Saving the current data and files first…")
            safety = make_backup("pre-restore")
            write_status(msg="Restoring %s… the panel may show errors for a short while." % name)
            db_path, tmp = _db_file(path)
            if tmp:
                temps.append(db_path)
            try:
                load_dump(db_path)
            except BackupError as e:
                write_status(msg="The restore failed (%s). Putting the previous data back…" % e)
                try:
                    back, tmp2 = _db_file(os.path.join(DIR, safety))
                    if tmp2:
                        temps.append(back)
                    load_dump(back)
                    raise BackupError("The restore failed and the previous data was put back. Reason: %s" % e)
                except BackupError as inner:
                    if "previous data was put back" in str(inner):
                        raise
                    raise BackupError("The restore failed and putting the old data back also failed. The old data is in "
                                      "%s. Reason: %s" % (safety, e))
            notes = []
            try:
                files = restore_files(path)
                if files:
                    notes.append("Files put back: %s." % ", ".join("%s %d" % (k.replace("_", " "), v) for k, v in files.items()))
            except (BackupError, OSError, tarfile.TarError) as e:
                notes.append("The database was restored but the files were not (%s); they are in %s." % (e, safety))
            write_status(msg="Bringing the tables up to date…")
            notes += [w for w in (apply_schema(), after_restore()) if w]
            bad = any("failed" in n or "cannot be read" in n or "were not" in n for n in notes)
            write_status(state="error" if bad else "ok",
                         msg="Restored from %s. %s Everyone signs in again. The data before the restore is kept as %s."
                             % (name, " ".join(notes), safety), finished=int(time.time()))
        except BackupError as e:
            write_status(state="error", msg=str(e), finished=int(time.time()))
        finally:
            for p in temps:
                try:
                    os.remove(p)
                except OSError:
                    pass


def job_fetch(file_id):
    with Lock():
        write_status(state="running", kind="fetch", started=int(time.time()), msg="Downloading from Google Drive…")
        conn = None
        try:
            conn, cur, s = _settings()
            name = drive_fetch(s, file_id)
            write_status(state="ok", msg="Downloaded %s from Google Drive. You can restore it now." % name,
                         finished=int(time.time()), file=name)
        except BackupError as e:
            write_status(state="error", msg=str(e), finished=int(time.time()))
        finally:
            if conn:
                conn.close()


RETRY_MIN = 30          # a failed daily backup is tried again after this many minutes...
RETRY_MAX = 3           # ...up to this many tries a day


def job_scheduled():
    """The daily backup, from the timer every 10 minutes. A failed one is tried again (30 minutes apart, 3 a day)."""
    import core
    import db
    conn, cur, s = _settings()
    try:
        core.apply_timezone(s.get("timezone"))
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if s.get("backup_on") != "1" or now.hour < int(s.get("backup_hour") or 4) or s.get("backup_last_day") == today:
            return
        day, _sep, tries = (s.get("backup_tries") or "").partition(":")
        tries = int(tries) if day == today and tries.isdigit() else 0
        if tries >= RETRY_MAX or (tries and time.time() - float(s.get("backup_try_at") or 0) < RETRY_MIN * 60):
            return
        if busy():
            return
        db.save_setting(cur, "backup_tries", "%s:%d" % (today, tries + 1))
        db.save_setting(cur, "backup_try_at", "%d" % int(time.time()))
        conn.commit()
    finally:
        conn.close()
    job_backup("auto")
    conn, cur, s = _settings()
    try:
        if s.get("backup_last_ok") == "1":           # done for today only when it worked
            db.save_setting(cur, "backup_last_day", today)
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "backup":
            job_backup("manual")
        elif cmd == "scheduled":
            job_scheduled()
        elif cmd == "restore" and len(sys.argv) > 2:
            job_restore(sys.argv[2])
        elif cmd == "fetch" and len(sys.argv) > 2:
            job_fetch(sys.argv[2])
        else:
            print("usage: backuplib.py backup | scheduled | restore NAME | fetch DRIVE_FILE_ID")
            sys.exit(2)
    except BackupError as exc:
        print("backup: %s" % exc, file=sys.stderr)
        sys.exit(1)
