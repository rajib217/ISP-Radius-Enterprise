"""Database backup and restore, plus the Google Drive copy.

Used three ways:
  * imported by the panel pages (mod_backup.py) for listing, Google sign-in and file checks
  * run by the panel as a detached worker:  python3 backuplib.py backup | restore NAME | fetch FILE_ID
  * run by the systemd timer every 10 minutes:  python3 backuplib.py scheduled

A backup is one gzip-compressed SQL dump of the whole panel + RADIUS database (subscribers, packages, payments,
settings, RADIUS users and accounting). Nothing outside the database is included.
"""
import glob
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
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
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,118}\.sql\.gz$")
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
    for p in glob.glob(os.path.join(DIR, "*.sql.gz")):
        name = os.path.basename(p)
        if not valid_name(name):
            continue
        st = os.stat(p)
        out.append({"name": name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime), "kind": kind_of(name)})
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
    """Raise BackupError unless the file looks like a complete dump. deep reads the whole file."""
    import zlib
    try:
        with gzip.open(path, "rb") as f:
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


def load_dump(path):
    """Replace the database contents with the dump."""
    with _Creds() as cnf:
        cmd = [_tool("mariadb", "mysql"), "--defaults-extra-file=" + cnf, "--max-allowed-packet=512M",
               "--default-character-set=utf8mb4", DBNAME()]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with gzip.open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if proc.wait() != 0:
            raise BackupError(err[-300:] or "The database refused the file.")


def prune_local(keep):
    rows = [r for r in list_backups() if r["kind"] in ("auto", "manual")]
    for r in rows[max(keep, 1):]:
        try:
            os.remove(os.path.join(DIR, r["name"]))
        except OSError:
            pass
    pre = [r for r in list_backups() if r["kind"] == "pre-restore"]
    for r in pre[3:]:
        try:
            os.remove(os.path.join(DIR, r["name"]))
        except OSError:
            pass


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
        check_dump(dest, deep=True)
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
        write_status(state="running", kind="backup", started=int(time.time()), msg="Dumping the database…")
        conn = cur = None
        try:
            name = make_dump("auto" if label == "auto" else "")
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
        safety = None
        try:
            check_dump(path, deep=True)
            write_status(msg="Saving the current data first…")
            safety = make_dump("pre-restore")
            write_status(msg="Restoring %s… the panel may show errors for a short while." % name)
            try:
                load_dump(path)
            except BackupError as e:
                write_status(msg="The restore failed (%s). Putting the previous data back…" % e)
                try:
                    load_dump(os.path.join(DIR, safety))
                    raise BackupError("The restore failed and the previous data was put back. Reason: %s" % e)
                except BackupError as inner:
                    if "previous data was put back" in str(inner):
                        raise
                    raise BackupError("The restore failed and putting the old data back also failed. The old data is in "
                                      "%s. Reason: %s" % (safety, e))
            write_status(state="ok", msg="Restored from %s. Sign in again if the panel asks. The data before the restore "
                                         "is kept as %s." % (name, safety), finished=int(time.time()))
        except BackupError as e:
            write_status(state="error", msg=str(e), finished=int(time.time()))


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


def job_scheduled():
    import core
    conn, cur, s = _settings()
    try:
        core.apply_timezone(s.get("timezone"))
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if s.get("backup_on") != "1" or now.hour < int(s.get("backup_hour") or 4) or s.get("backup_last_day") == today:
            return
        if busy():
            return
        import db
        db.save_setting(cur, "backup_last_day", today)
        conn.commit()
    finally:
        conn.close()
    job_backup("auto")


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
