"""Settings > Backup & Restore: database backups, restore from a file, and the Google Drive copy."""
import json
import os
import re
import time
from datetime import datetime

from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash

import backuplib as bl
import core
import db
from core import admin_required, audit, cfg, commit, get_db, q, route

PAGE = "settings_backup"


def _save(values):
    with get_db().cursor() as cur:
        for k, v in values.items():
            db.save_setting(cur, k, v)


def _back():
    return redirect(url_for(PAGE))


def _running():
    try:
        return bl.busy()
    except OSError:
        return False


def _device(s):
    try:
        d = json.loads(s.get("gdrive_device") or "{}")
    except ValueError:
        return None
    return d if d.get("device_code") and d.get("expires", 0) > time.time() else None


def _drive_ready(s):
    return bool(s.get("gdrive_client_id") and s.get("gdrive_client_secret") and s.get("gdrive_refresh_token"))


@route("/settings/backup", methods=["GET"])
@admin_required
def settings_backup():
    s = cfg()
    st = bl.read_status()
    running = _running()
    if st.get("state") == "running" and not running:     # the worker died without finishing
        st = dict(st, state="error", msg="The last job stopped unexpectedly. Check the server log.")
    drive_files, drive_err = None, ""
    if request.args.get("drive") == "1" and _drive_ready(s):
        try:
            drive_files = bl.drive_files(dict(s))
        except bl.BackupError as e:
            drive_err = str(e)
    return render_template("settings_backup.html", s=s, tab="backup", files=bl.list_backups(), st=st, running=running,
                           free=bl.disk_free(), device=_device(s), drive_ready=_drive_ready(s),
                           drive_files=drive_files, drive_err=drive_err, folder=bl.DIR, hours=range(24),
                           last={"msg": s.get("backup_last_msg"), "ok": s.get("backup_last_ok") == "1",
                                 "time": s.get("backup_last_time")})


@route("/settings/backup/status")
@admin_required
def backup_status():
    st = bl.read_status()
    return jsonify(running=_running(), state=st.get("state", ""), msg=st.get("msg", ""))


@route("/settings/backup/save", methods=["POST"])
@admin_required
def backup_save():
    f = request.form
    try:
        hour = int(f.get("backup_hour", "4"))
        keep = int(f.get("backup_keep", "7"))
        dkeep = int(f.get("gdrive_keep", "14"))
        assert 0 <= hour <= 23 and 1 <= keep <= 90 and 1 <= dkeep <= 365
    except (ValueError, AssertionError):
        flash("Check the numbers: hour 0-23, local copies 1-90, Google Drive copies 1-365. Nothing was saved.", "error")
        return _back()
    cid = f.get("gdrive_client_id", "").strip()[:200]
    secret = f.get("gdrive_client_secret", "").strip()[:200]
    s = cfg()
    new = {"backup_on": "1" if f.get("backup_on") else "0", "backup_hour": hour, "backup_keep": keep,
           "gdrive_on": "1" if f.get("gdrive_on") else "0", "gdrive_keep": dkeep, "gdrive_client_id": cid}
    if secret:                       # the secret is never shown again; leaving it empty keeps the saved one
        new["gdrive_client_secret"] = secret
    if cid != s.get("gdrive_client_id") and s.get("gdrive_refresh_token"):
        new.update(gdrive_refresh_token="", gdrive_folder_id="", gdrive_device="")
        flash("The Client ID changed, so Google Drive was disconnected. Connect it again.", "warn")
    if new["gdrive_on"] == "1" and not (s.get("gdrive_refresh_token") and cid == s.get("gdrive_client_id")):
        flash("Google Drive is switched on but not connected yet. Press Connect Google Drive.", "warn")
    _save(new)
    audit("backup_settings", "backup")
    commit()
    flash("Backup settings saved.", "success")
    return _back()


@route("/settings/backup/run", methods=["POST"])
@admin_required
def backup_run():
    if _running():
        flash("A backup or restore is already running.", "warn")
        return _back()
    audit("backup_started", "manual")
    commit()
    bl.start_worker("backup")
    time.sleep(0.4)
    flash("Backup started. This page updates when it finishes.", "success")
    return _back()


@route("/settings/backup/download/<name>")
@admin_required
def backup_download(name):
    if not bl.valid_name(name) or not os.path.isfile(bl.path_of(name)):
        abort(404)
    audit("backup_downloaded", name)
    commit()
    return send_file(bl.path_of(name), as_attachment=True, download_name=name, mimetype="application/gzip")


@route("/settings/backup/delete/<name>", methods=["POST"])
@admin_required
def backup_delete(name):
    if not bl.valid_name(name) or not os.path.isfile(bl.path_of(name)):
        abort(404)
    if _running():
        flash("Wait for the running backup or restore to finish.", "warn")
        return _back()
    os.remove(bl.path_of(name))
    audit("backup_deleted", name)
    commit()
    flash("%s deleted from this server." % name, "success")
    return _back()


@route("/settings/backup/upload", methods=["POST"])
@admin_required
def backup_upload():
    up = request.files.get("file")
    if not up or not up.filename:
        flash("Choose a .sql.gz backup file.", "error")
        return _back()
    if _running():
        flash("Wait for the running backup or restore to finish.", "warn")
        return _back()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(up.filename))[-90:]
    if not base.endswith(".sql.gz"):
        flash("The file must end with .sql.gz (a backup made by this panel).", "error")
        return _back()
    bl.ensure_dir()
    name = "uploaded-%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), base.lstrip(".-"))
    dest = bl.path_of(name)
    part = dest + ".part"
    try:
        with open(part, "wb") as out:
            while True:
                chunk = up.stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        bl.check_dump(part)
        os.replace(part, dest)
    except (bl.BackupError, OSError) as e:
        if os.path.exists(part):
            os.remove(part)
        flash(str(e) if isinstance(e, bl.BackupError) else "The file could not be saved (is the disk full?).", "error")
        return _back()
    audit("backup_uploaded", name)
    commit()
    flash("Uploaded as %s. Press Restore on it when you are ready." % name, "success")
    return _back()


@route("/settings/backup/restore/<name>", methods=["POST"])
@admin_required
def backup_restore(name):
    if not bl.valid_name(name) or not os.path.isfile(bl.path_of(name)):
        abort(404)
    if _running():
        flash("A backup or restore is already running.", "warn")
        return _back()
    row = q("SELECT password_hash FROM panel_admins WHERE id = %s", (g.me["id"],), one=True)
    if request.form.get("confirm", "").strip() != "RESTORE" or not row or \
            not check_password_hash(row["password_hash"], request.form.get("password", "")):
        flash("Restore not started: type RESTORE and enter your own password exactly.", "error")
        return _back()
    audit("backup_restore", name)
    commit()
    bl.start_worker("restore", name)
    time.sleep(0.4)
    flash("Restore started. The panel may show errors for a short while; reload in a minute.", "warn")
    return _back()


# ----------------------------------------------------------------- Google Drive
@route("/settings/backup/drive/connect", methods=["POST"])
@admin_required
def drive_connect():
    s = cfg()
    cid, secret = s.get("gdrive_client_id"), s.get("gdrive_client_secret")
    if not (cid and secret):
        flash("Save the Client ID and Client Secret first.", "error")
        return _back()
    try:
        dev = bl.drive_start(cid)
    except bl.BackupError as e:
        flash(str(e), "error")
        return _back()
    _save({"gdrive_device": json.dumps(dev)})
    commit()
    return _back()


@route("/settings/backup/drive/check", methods=["POST"])
@admin_required
def drive_check():
    s = cfg()
    dev = _device(s)
    if not dev:
        flash("The code expired. Press Connect Google Drive again.", "error")
        return _back()
    try:
        token = bl.drive_poll(s["gdrive_client_id"], s["gdrive_client_secret"], dev["device_code"])
    except bl.BackupError as e:
        _save({"gdrive_device": ""})
        commit()
        flash(str(e), "error")
        return _back()
    if not token:
        flash("Google has not received your approval yet. Finish the steps on the Google page, then press this again.", "warn")
        return _back()
    _save({"gdrive_refresh_token": token, "gdrive_device": "", "gdrive_on": "1"})
    audit("gdrive_connected", "backup")
    commit()
    flash("Google Drive is connected. Backups are copied to the folder \"%s\"." % bl.FOLDER_NAME, "success")
    return _back()


@route("/settings/backup/drive/test", methods=["POST"])
@admin_required
def drive_test():
    s = dict(cfg())
    if not _drive_ready(s):
        flash("Google Drive is not connected.", "error")
        return _back()
    try:
        tok = bl._access_token(s)
        with get_db().cursor() as cur:
            bl.drive_folder(s, tok, cur)
        commit()
        flash("Google Drive works. Backups go to the folder \"%s\" in My Drive." % bl.FOLDER_NAME, "success")
    except bl.BackupError as e:
        flash(str(e), "error")
    return _back()


@route("/settings/backup/drive/disconnect", methods=["POST"])
@admin_required
def drive_disconnect():
    _save({"gdrive_refresh_token": "", "gdrive_folder_id": "", "gdrive_device": "", "gdrive_on": "0"})
    audit("gdrive_disconnected", "backup")
    commit()
    flash("Google Drive disconnected. Files already on Drive were not deleted.", "success")
    return _back()


@route("/settings/backup/drive/fetch", methods=["POST"])
@admin_required
def drive_fetch():
    fid = request.form.get("file_id", "")
    if not re.match(r"^[A-Za-z0-9_-]{10,100}$", fid) or not _drive_ready(cfg()):
        abort(400)
    if _running():
        flash("A backup or restore is already running.", "warn")
        return _back()
    bl.start_worker("fetch", fid)
    time.sleep(0.4)
    flash("Downloading from Google Drive. It will appear in the list below when done.", "success")
    return _back()
