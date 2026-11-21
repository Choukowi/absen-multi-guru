#!/usr/bin/env python3
"""
Aplikasi Absen Siswa — model GERBANG GLOBAL (satu perangkat scan di gerbang).

Stack: Python 3 stdlib (http.server + sqlite3) + `qrcode`/`Pillow` untuk
gambar QR kartu + `openpyxl` untuk unduhan Excel tabel mingguan.

Model absen:
- Tidak ada lagi sesi per mata pelajaran / penugasan. Setiap siswa & guru
  punya kartu QR (siswa: NIS; guru: kode QR guru, mis. G001).
- Di gerbang, petugas scan QR lewat halaman /scan-gerbang (kamera + jsQR,
  atau ketik kode manual) -> POST /api/scan mencatat kehadiran HARI ITU:
  scan <= 07:00 = Hadir, 07:01-08:59 = Terlambat (jam dicatat persis).
- Yang sampai 09:00 belum scan otomatis menjadi Alpa (metode 'auto'),
  terisi saat tabel/dashboard dibuka. Sakit/Izin diisi manual.
- Scan pukul 09:00 ke atas DITUTUP: orang yang belum punya catatan asli
  hari itu ditolak ("Scan sudah ditutup") dan tetap tercatat Alpa.
- Tabel mingguan Senin-Sabtu per kelas (siswa) dan untuk guru bisa dilihat
  semua pengguna yang login, dan diunduh sebagai Excel (.xlsx).

Jalankan:  python3 app.py            (default http://127.0.0.1:8000)
           PORT=8080 python3 app.py
"""
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import urllib.parse
import zipfile
from datetime import datetime, timedelta, time as dtime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ABSEN_DB", os.path.join(BASE_DIR, "data", "absen.db"))
PORT = int(os.environ.get("PORT", "8000"))
TZ_JKT = ZoneInfo("Asia/Jakarta")

JAM_BATAS_HADIR = dtime(7, 0)    # scan <= 07:00 -> Hadir
JAM_BATAS_ALPA = dtime(9, 0)     # >= 09:00 belum scan -> Alpa otomatis
STATUS_LIST = ["Hadir", "Terlambat", "Sakit", "Izin", "Alpa"]
NAMA_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
NAMA_BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
              "Agustus", "September", "Oktober", "November", "Desember"]
NAMA_BULAN_SINGKAT = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul",
                      "Agu", "Sep", "Okt", "Nov", "Des"]


def waktu_sekarang():
    """Datetime 'sekarang' sadar-zona di Asia/Jakarta.

    Untuk pengujian, env var ABSEN_NOW_OVERRIDE (format ISO, mis.
    '2026-11-16T06:45:00') menggantikan jam asli. Env dibaca SETIAP
    pemanggilan, jadi server uji cukup di-restart dengan env baru.
    Ini hook khusus tes — jangan dipakai di produksi.
    """
    ov = os.environ.get("ABSEN_NOW_OVERRIDE", "").strip()
    if ov:
        try:
            dt = datetime.fromisoformat(ov)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_JKT)
            return dt.astimezone(TZ_JKT)
        except ValueError:
            pass
    return datetime.now(TZ_JKT)


def fmt_tanggal(d):
    """date -> '16 November 2026'."""
    return f"{d.day} {NAMA_BULAN[d.month - 1]} {d.year}"


def fmt_jam_titik(jam):
    """'06:45' -> '06.45' (gaya tampilan tabel & Excel)."""
    return jam.replace(":", ".") if jam else ""


def senin_dari(d):
    """date -> date hari Senin pada minggu yang sama."""
    return d - timedelta(days=d.weekday())


def minggu_sen_sab(d):
    """date -> list 6 date (Senin..Sabtu) pada minggu tanggal itu."""
    sen = senin_dari(d)
    return [sen + timedelta(days=i) for i in range(6)]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','guru')),
    nama TEXT NOT NULL,
    aktif INTEGER NOT NULL DEFAULT 1,
    mapel TEXT DEFAULT '',
    kode_qr TEXT
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT NOT NULL,
    kode TEXT UNIQUE NOT NULL,          -- NIS siswa (isi QR kartunya)
    class_id INTEGER NOT NULL REFERENCES classes(id)
);
CREATE TABLE IF NOT EXISTS kehadiran (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tanggal TEXT NOT NULL,              -- YYYY-MM-DD (Asia/Jakarta)
    ref_type TEXT NOT NULL CHECK (ref_type IN ('siswa','guru')),
    ref_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Hadir','Terlambat','Sakit','Izin','Alpa')),
    jam TEXT,                           -- HH:MM (Jakarta); NULL untuk non-scan
    metode TEXT NOT NULL DEFAULT 'scan' CHECK (metode IN ('scan','manual','auto')),
    dicatat_oleh INTEGER REFERENCES users(id),
    UNIQUE (tanggal, ref_type, ref_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Tabel lama (subjects/assignments/sessions/attendance) sengaja TIDAK dibuat
-- lagi di sini; pada file DB lama tabel-tabel itu boleh tetap ada, tidak dipakai.
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), 200_000)
    return dk.hex(), salt


def verify_password(password, password_hash, salt):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), 200_000)
    return secrets.compare_digest(dk.hex(), password_hash)


def kode_qr_guru_baru(conn, gid):
    """Kode QR guru: 'G' + id 3 digit (G001). Unik di antara guru."""
    kode = f"G{gid:03d}"
    n = 1
    while conn.execute("SELECT 1 FROM users WHERE kode_qr=? AND id<>?",
                       (kode, gid)).fetchone():
        n += 1
        kode = f"G{gid:03d}-{n}"
    return kode


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    # --- migrasi idempoten untuk DB lama ---
    kolom_users = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "mapel" not in kolom_users:
        conn.execute("ALTER TABLE users ADD COLUMN mapel TEXT DEFAULT ''")
    if "kode_qr" not in kolom_users:
        conn.execute("ALTER TABLE users ADD COLUMN kode_qr TEXT")
    conn.commit()
    # backfill kode_qr untuk guru lama yang belum punya
    for r in conn.execute(
            "SELECT id FROM users WHERE role='guru' AND (kode_qr IS NULL OR kode_qr='')").fetchall():
        conn.execute("UPDATE users SET kode_qr=? WHERE id=?",
                     (kode_qr_guru_baru(conn, r["id"]), r["id"]))
    conn.commit()
    row = conn.execute("SELECT COUNT(*) c FROM users").fetchone()
    if row["c"] == 0:
        # Akun admin AWAL khusus pengujian lokal. WAJIB diganti sebelum produksi.
        ph, salt = hash_password("admin123")
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, nama) VALUES (?,?,?,?,?)",
            ("admin", ph, salt, "admin", "Administrator (akun awal lokal)"))
        conn.commit()
        print("[init] Akun admin awal dibuat: username=admin (password ada di README). "
              "WAJIB ganti password sebelum dipakai beneran.")
    conn.close()


def pastikan_alpa(conn, tanggal):
    """Isi Alpa otomatis (metode 'auto') untuk satu tanggal bila sudah waktunya.

    Berlaku untuk tanggal Senin-Sabtu yang sudah lewat, atau hari ini bila
    jam Jakarta sudah >= 09:00. Tidak pernah untuk tanggal masa depan
    atau hari Minggu. Siswa & guru aktif yang belum punya catatan apa pun
    pada tanggal itu menjadi Alpa.
    """
    try:
        d = datetime.strptime(tanggal, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return
    if d.weekday() >= 6:  # Minggu tidak ada absen
        return
    now = waktu_sekarang()
    if d > now.date():
        return
    if d == now.date() and now.time() < JAM_BATAS_ALPA:
        return
    conn.execute(
        """INSERT OR IGNORE INTO kehadiran (tanggal, ref_type, ref_id, status, jam, metode, dicatat_oleh)
           SELECT ?, 'siswa', id, 'Alpa', NULL, 'auto', NULL FROM students""", (tanggal,))
    conn.execute(
        """INSERT OR IGNORE INTO kehadiran (tanggal, ref_type, ref_id, status, jam, metode, dicatat_oleh)
           SELECT ?, 'guru', id, 'Alpa', NULL, 'auto', NULL FROM users
           WHERE role='guru' AND aktif=1""", (tanggal,))
    conn.commit()


# ---------------------------------------------------------------------------
# Session login (server-side, cookie hanya menyimpan token acak)
# ---------------------------------------------------------------------------
LOGIN_SESSIONS = {}  # token -> user_id


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def js_str(v):
    return str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


# ---------------------------------------------------------------------------
# Ikon SVG inline (tanpa aset eksternal)
# ---------------------------------------------------------------------------
def _svg(inner):
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{inner}</svg>')


ICONS = {
    "logo": _svg('<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/>'
                 '<rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/>'
                 '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/>'
                 '<path d="M13.5 17.5l2 2 4-4.5"/>'),
    "grid": _svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
                 '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
                 '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
                 '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
    "scan": _svg('<path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3"/>'
                 '<path d="M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/>'
                 '<path d="M3 12h18"/>'),
    "users": _svg('<circle cx="9" cy="8" r="3.5"/>'
                  '<path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/>'
                  '<path d="M15.5 4.7a3.5 3.5 0 0 1 0 6.6M21 20c0-2.8-1.9-4.8-4.5-5.3"/>'),
    "badge": _svg('<rect x="4" y="3" width="16" height="18" rx="2"/>'
                  '<circle cx="12" cy="10" r="2.5"/>'
                  '<path d="M8 17c.6-1.8 2.2-2.7 4-2.7s3.4.9 4 2.7"/>'),
    "cap": _svg('<path d="M2 9l10-4 10 4-10 4z"/>'
                '<path d="M6 11.5V16c0 1.5 2.7 3 6 3s6-1.5 6-3v-4.5"/>'
                '<path d="M22 9v5"/>'),
    "building": _svg('<path d="M5 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16"/>'
                     '<path d="M15 8h3a2 2 0 0 1 2 2v11"/><path d="M3 21h18"/>'
                     '<path d="M8.5 7h.01M11.5 7h.01M8.5 11h.01M11.5 11h.01M8.5 15h.01M11.5 15h.01"/>'),
    "printer": _svg('<path d="M6 9V3h12v6"/>'
                    '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
                    '<rect x="6" y="14" width="12" height="8"/>'),
    "key": _svg('<circle cx="8" cy="15" r="4.5"/>'
                '<path d="M11.2 11.8L20 3M16.5 4.5l3 3M13.8 7.2l2.7 2.7"/>'),
    "logout": _svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
                   '<path d="M16 17l5-5-5-5M21 12H9"/>'),
    "menu": _svg('<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/>'
                 '<circle cx="19" cy="12" r="1.6"/>'),
    "check": _svg('<path d="M4 12.5l5 5L20 6.5"/>'),
    "clock": _svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    "x": _svg('<path d="M5 5l14 14M19 5L5 19"/>'),
    "table": _svg('<rect x="3" y="4" width="18" height="16" rx="2"/>'
                  '<path d="M3 10h18M9 10v10M15 10v10"/>'),
    "download": _svg('<path d="M12 3v12M6.5 10.5L12 16l5.5-5.5"/><path d="M4 20h16"/>'),
    "sliders": _svg('<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
                    '<path d="M1.5 14h5M9.5 8h5M17.5 16h5"/>'),
}


def icon(name):
    return ICONS.get(name, "")


# ---------------------------------------------------------------------------
# HTML layout — shell aplikasi modern (sidebar desktop + bottom-nav mobile)
# ---------------------------------------------------------------------------
def nav_items(user):
    items = [("dashboard", "/dashboard", "Dashboard", "grid"),
             ("scan", "/scan-gerbang", "Scan Gerbang", "scan"),
             ("tabel-siswa", "/tabel-siswa", "Tabel Siswa", "users"),
             ("tabel-guru", "/tabel-guru", "Tabel Guru", "badge")]
    if user["role"] == "admin":
        items += [("siswa", "/admin/siswa", "Siswa", "cap"),
                  ("guru", "/admin/guru", "Guru", "badge"),
                  ("kelas", "/admin/kelas", "Kelas", "building"),
                  ("tampilan", "/admin/tampilan", "Pengaturan Tampilan", "sliders"),
                  ("backup", "/admin/backup", "Backup & Pulihkan", "download")]
    items += [("cetak", "/cetak", "Cetak Kartu", "printer"),
              ("ganti-password", "/ganti-password", "Ganti Password", "key")]
    return items


def brand_mark(peng, sm=False):
    """Tanda brand aplikasi: logo sekolah terunggah bila sudah dipasang,
    kalau belum pakai ikon SVG bawaan. Nama sekolah di sebelahnya tidak berubah."""
    cls = "brand-mark sm" if sm else "brand-mark"
    if peng.get("logo"):
        return (f'<span class="{cls} has-logo">'
                f'<img src="/logo/{esc(peng["logo"])}" alt="Logo sekolah"></span>')
    return f'<span class="{cls}">{icon("logo")}</span>'


def layout(user, title, body, flash="", aktif=""):
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    peng = get_pengaturan()
    nama_app = peng["nama_sekolah"] or "Absen Siswa"
    bg_style = f' style="background-color:{peng["bg_color"]}"'
    wp_layer = ""
    if peng["wallpaper"]:
        wp_layer = (f'<div class="bg-wallpaper" '
                    f'style="background-image:url(\'/wallpaper/{esc(peng["wallpaper"])}\')"></div>')
    tahun = waktu_sekarang().year
    if not user:
        return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - {esc(nama_app)}</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="auth-body"{bg_style}>{wp_layer}<div class="auth-wrap">
<div class="auth-card">
<div class="auth-brand">{brand_mark(peng)}
<span class="brand-name">{esc(nama_app)}</span></div>
{flash_html}{body}</div>
<p class="auth-foot">&copy; {tahun} {esc(nama_app)} &bull; Absensi sekolah dengan QR Code biasa (bukan QRIS pembayaran).</p>
</div><script src="/static/app.js"></script></body></html>"""
    items = nav_items(user)
    sidenav = "".join(
        f'<a class="nav-item{" aktif" if k == aktif else ""}" href="{href}">'
        f'{icon(ic)}<span>{label}</span></a>' for k, href, label, ic in items)
    bawah = [("dashboard", "/dashboard", "Dashboard", "grid"),
             ("scan", "/scan-gerbang", "Scan", "scan"),
             ("tabel-siswa", "/tabel-siswa", "Siswa", "users"),
             ("tabel-guru", "/tabel-guru", "Guru", "badge"),
             ("menu", "/menu", "Menu", "menu")]
    bottomnav = "".join(
        f'<a class="bnav-item{" aktif" if k == aktif else ""}" href="{href}">'
        f'{icon(ic)}<span>{label}</span></a>' for k, href, label, ic in bawah)
    initial = esc((user["nama"] or "?").strip()[:1].upper() or "?")
    role_label = "Admin" if user["role"] == "admin" else "Guru"
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - {esc(nama_app)}</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body{bg_style}>{wp_layer}<div class="shell">
<aside class="sidebar">
<div class="brand">{brand_mark(peng)}
<span class="brand-name">{esc(nama_app)}</span></div>
<nav class="sidenav">{sidenav}</nav>
<div class="userchip"><span class="avatar">{initial}</span>
<span class="uinfo"><b>{esc(user["nama"])}</b><small>{role_label}</small></span>
<a class="logout-btn" href="/logout" title="Keluar">{icon('logout')}</a></div>
</aside>
<div class="content">
<header class="topbar">{brand_mark(peng, sm=True)}
<span class="topbar-title">{esc(nama_app)}</span><span class="avatar">{initial}</span></header>
<main>{flash_html}{body}</main>
<footer><small>&copy; {tahun} {esc(nama_app)} &bull; Absensi gerbang sekolah &bull; QR Code biasa (bukan QRIS pembayaran)</small></footer>
</div>
<nav class="bottomnav">{bottomnav}</nav>
</div><script src="/static/app.js"></script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AbsenGerbang/2.0"

    def log_message(self, fmt, *args):  # lebih senyap
        pass

    # -- util ---------------------------------------------------------------
    def send_html(self, content, status=200, extra_headers=None):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path, flash="", set_cookie=None):
        headers = []
        if set_cookie:
            headers.append(("Set-Cookie", set_cookie))
        if flash:
            path += ("&" if "?" in path else "?") + "msg=" + urllib.parse.quote(flash)
        headers.append(("Location", path))
        self.send_html("", status=303, extra_headers=headers)

    def current_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("absen_session")
        if not morsel:
            return None
        uid = LOGIN_SESSIONS.get(morsel.value)
        if not uid:
            return None
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE id=? AND aktif=1", (uid,)).fetchone()
        conn.close()
        return row

    def form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        flash = (qs.get("msg") or [""])[0]
        try:
            self.route_get(path, qs, flash)
        except Exception as e:  # jangan bocorkan stack ke user, tapi catat di log server
            import traceback; traceback.print_exc()
            self.send_html(layout(self.current_user(), "Error",
                                  f"<p>Terjadi kesalahan: {esc(e)}</p>"), status=500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            self.route_post(parsed.path)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_html(layout(self.current_user(), "Error",
                                  f"<p>Terjadi kesalahan: {esc(e)}</p>"), status=500)

    # -- GET routes ---------------------------------------------------------
    def route_get(self, path, qs, flash):
        if path == "/static/style.css" or path == "/static/app.js":
            fname = os.path.join(BASE_DIR, path.lstrip("/"))
            ctype = "text/css" if path.endswith(".css") else "application/javascript"
            with open(fname, "rb") as f:
                data = f.read()
            return self.send_bytes(data, ctype)
        if path.startswith("/qr/") and path.endswith(".png"):
            return self.qr_png(path[4:-4])
        if path.startswith("/wallpaper/"):
            return self.wallpaper_file(path[len("/wallpaper/"):])
        if path.startswith("/logo/"):
            return self.logo_file(path[len("/logo/"):])
        if path == "/":
            return self.redirect("/dashboard")
        if path == "/login":
            body = """<h2>Masuk</h2>
            <form method="post" action="/login">
              <label>Username <input name="username" required autocomplete="username"></label>
              <label>Password <input name="password" type="password" required autocomplete="current-password"></label>
              <button type="submit" class="btn btn-primary btn-block">Masuk</button></form>
            <p class="muted"><small>Akun uji lokal ada di README proyek. Jangan pakai akun awal di produksi.</small></p>"""
            return self.send_html(layout(None, "Masuk", body, flash))
        if path == "/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("absen_session")
            if morsel:
                LOGIN_SESSIONS.pop(morsel.value, None)
            return self.redirect("/login", "Sampai jumpa.",
                                 set_cookie="absen_session=; Max-Age=0; Path=/")

        user = self.current_user()
        if not user:
            return self.redirect("/login", "Silakan masuk dulu.")
        if path == "/dashboard":
            return self.dashboard(user, flash)
        if path == "/cari":
            return self.cari(user, qs, flash)
        if path == "/ganti-password":
            body = """<h2>Ganti Password</h2>
            <form method="post" action="/ganti-password" class="card form-card">
              <label>Password baru <input name="password" type="password" required minlength="8"></label>
              <button type="submit" class="btn btn-primary">Simpan</button></form>"""
            return self.send_html(layout(user, "Ganti Password", body, flash,
                                         aktif="ganti-password"))
        if path == "/scan-gerbang":
            return self.scan_gerbang(user, flash)
        if path == "/tabel-siswa":
            return self.tabel_siswa(user, qs, flash)
        if path == "/tabel-guru":
            return self.tabel_guru(user, qs, flash)
        if path == "/tabel-siswa/excel":
            return self.excel_siswa(user, qs)
        if path == "/tabel-guru/excel":
            return self.excel_guru(user, qs)
        if path == "/kehadiran/edit":
            return self.kehadiran_edit(user, qs, flash)
        if path == "/menu":
            return self.menu_page(user, flash)
        if path == "/cetak":
            return self.cetak_index(user, flash)
        if path == "/cetak/guru":
            return self.cetak_guru(user, flash)
        if path.startswith("/cetak/kelas/"):
            return self.cetak_kelas(user, int(path.split("/")[3]), flash)
        if user["role"] == "admin":
            if path == "/admin/guru": return self.admin_guru(user, flash)
            if path.startswith("/admin/guru/") and path.endswith("/edit"):
                return self.admin_guru_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/kelas": return self.admin_kelas(user, flash)
            if path.startswith("/admin/kelas/") and path.endswith("/edit"):
                return self.admin_kelas_edit(user, int(path.split("/")[3]), flash)
            if path.startswith("/admin/kelas/") and path.split("/")[3].isdigit():
                return self.admin_kelas_detail(user, int(path.split("/")[3]), flash)
            if path == "/admin/siswa": return self.admin_siswa(user, qs, flash)
            if path.startswith("/admin/siswa/") and path.endswith("/edit"):
                return self.admin_siswa_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/tampilan": return self.admin_tampilan(user, flash)
            if path == "/admin/backup": return self.admin_backup(user, flash)
            if path == "/admin/backup/unduh": return self.admin_backup_unduh()
        return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>",
                                     aktif=""), status=404)

    # -- POST routes --------------------------------------------------------
    def route_post(self, path):
        if path == "/login":
            return self.post_login()
        user = self.current_user()
        if not user:
            return self.redirect("/login", "Silakan masuk dulu.")
        if path == "/admin/tampilan/wallpaper":
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_tampilan_wallpaper()
        if path == "/admin/tampilan/logo":
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_tampilan_logo()
        if path == "/admin/backup/pulihkan":
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_backup_pulihkan()
        f = self.form()
        if path == "/ganti-password":
            ph, salt = hash_password(f["password"])
            conn = db()
            conn.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                         (ph, salt, user["id"]))
            conn.commit(); conn.close()
            return self.redirect("/dashboard", "Password berhasil diganti.")
        if path == "/api/scan":
            return self.post_scan(user, f)
        if path == "/kehadiran/edit":
            return self.post_kehadiran_edit(user, f)
        if user["role"] == "admin":
            if path == "/admin/guru": return self.post_admin_guru(f)
            if path.startswith("/admin/guru/") and path.endswith("/edit"):
                return self.post_admin_guru_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/guru/") and path.endswith("/hapus"):
                return self.post_admin_guru_hapus(int(path.split("/")[3]))
            if path.startswith("/admin/guru/") and path.endswith("/aktifkan"):
                return self.post_admin_guru_aktifkan(int(path.split("/")[3]))
            if path == "/admin/kelas": return self.post_admin_kelas(f)
            if path.startswith("/admin/kelas/") and path.endswith("/edit"):
                return self.post_admin_kelas_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/kelas/") and path.endswith("/hapus"):
                return self.post_admin_kelas_hapus(int(path.split("/")[3]))
            if path == "/admin/siswa": return self.post_admin_siswa(f)
            if path.startswith("/admin/siswa/") and path.endswith("/edit"):
                return self.post_admin_siswa_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/siswa/") and path.endswith("/hapus"):
                return self.post_admin_siswa_hapus(int(path.split("/")[3]))
            if path == "/admin/tampilan": return self.post_admin_tampilan(f)
            if path == "/admin/tampilan/wallpaper/hapus":
                return self.post_admin_tampilan_wallpaper_hapus()
            if path == "/admin/tampilan/logo/hapus":
                return self.post_admin_tampilan_logo_hapus()
        return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                              status=404)

    # -- auth ---------------------------------------------------------------
    def post_login(self):
        f = self.form()
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           (f.get("username", "").strip(),)).fetchone()
        conn.close()
        if row and not row["aktif"]:
            # Akun nonaktif tidak bisa masuk; beri pesan jelas bila password-nya benar.
            if verify_password(f.get("password", ""), row["password_hash"], row["salt"]):
                return self.redirect("/login", "Akun ini sudah dinonaktifkan.")
            return self.redirect("/login", "Username atau password salah.")
        if row and verify_password(f.get("password", ""), row["password_hash"], row["salt"]):
            token = secrets.token_urlsafe(32)
            LOGIN_SESSIONS[token] = row["id"]
            return self.redirect("/dashboard", f"Selamat datang, {row['nama']}.",
                                 set_cookie=f"absen_session={token}; HttpOnly; Path=/; SameSite=Lax")
        return self.redirect("/login", "Username atau password salah.")

    # -- dashboard ------------------------------------------------------------
    def dashboard(self, user, flash):
        now = waktu_sekarang()
        today = now.date()
        tgl = today.strftime("%Y-%m-%d")
        kemarin = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = db()
        pastikan_alpa(conn, tgl)
        pastikan_alpa(conn, kemarin)
        hitung = {}
        for tipe in ("siswa", "guru"):
            for st in STATUS_LIST:
                hitung[(tipe, st)] = conn.execute(
                    "SELECT COUNT(*) c FROM kehadiran WHERE tanggal=? AND ref_type=? AND status=?",
                    (tgl, tipe, st)).fetchone()["c"]
        total_siswa = conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
        # "Belum Scan" = total - yang SUDAH scan / dicatat manual hari ini.
        # Catatan Alpa OTOMATIS (metode='auto') TIDAK mengurangi: siswa/guru
        # itu tidak pernah scan, jadi tetap terhitung "belum scan" sepanjang
        # hari walau status resminya sudah Alpa. Alpa MANUAL tetap mengurangi
        # (sudah ada keterangan dari petugas), begitu juga Sakit/Izin manual.
        tercatat_siswa = conn.execute(
            "SELECT COUNT(*) c FROM kehadiran WHERE tanggal=? AND ref_type='siswa' "
            "AND NOT (metode='auto' AND status='Alpa')", (tgl,)).fetchone()["c"]
        belum = max(total_siswa - tercatat_siswa, 0)
        total_guru = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='guru' AND aktif=1").fetchone()["c"]
        tercatat_guru = conn.execute(
            "SELECT COUNT(*) c FROM kehadiran k JOIN users u ON u.id=k.ref_id "
            "WHERE k.tanggal=? AND k.ref_type='guru' AND u.role='guru' AND u.aktif=1 "
            "AND NOT (k.metode='auto' AND k.status='Alpa')", (tgl,)).fetchone()["c"]
        belum_guru = max(total_guru - tercatat_guru, 0)
        datang = conn.execute(
            """SELECT k.status, k.jam, s.nama AS nama, c.nama AS sub, 'siswa' AS tipe
               FROM kehadiran k JOIN students s ON s.id=k.ref_id
               JOIN classes c ON c.id=s.class_id
               WHERE k.tanggal=? AND k.ref_type='siswa' AND k.metode='scan'
               UNION ALL
               SELECT k.status, k.jam, u.nama, u.mapel, 'guru'
               FROM kehadiran k JOIN users u ON u.id=k.ref_id
               WHERE k.tanggal=? AND k.ref_type='guru' AND k.metode='scan'
               ORDER BY jam DESC LIMIT 10""", (tgl, tgl)).fetchall()
        # Titik berganti pukul 09:00 (satu penanda bersama): panel bawah
        # dashboard berganti dari daftar kedatangan terakhir menjadi
        # Laporan Hari Ini (siapa Terlambat dan siapa Belum Scan), dan
        # kartu statistik kembali ke nilai AWAL hari (lihat di bawah).
        # pastikan_alpa di atas sudah berjalan, jadi daftar Belum Scan
        # lengkap pada saat laporan dibuat.
        sudah_sembilan = now.time() >= JAM_BATAS_ALPA
        panel_laporan = self._panel_laporan(conn, tgl) if sudah_sembilan else None
        conn.close()
        # Nilai kartu dari catatan hari ini (dipakai sebelum 09:00).
        k_hadir_s = hitung[("siswa", "Hadir")] + hitung[("siswa", "Terlambat")]
        k_telat_s = hitung[("siswa", "Terlambat")]
        k_sakit_s = hitung[("siswa", "Sakit")] + hitung[("siswa", "Izin")]
        k_alpa_s = hitung[("siswa", "Alpa")]
        k_belum_s = belum
        k_belum_g = belum_guru
        k_hadir_g = hitung[("guru", "Hadir")] + hitung[("guru", "Terlambat")]
        if sudah_sembilan:
            # Keputusan user: mulai 09:00 kartu statistik di-reset
            # TAMPILANNYA ke nilai awal hari — Hadir/Terlambat/Sakit-Izin/
            # Alpa nol lagi, Belum Scan penuh lagi. Ini HANYA tampilan:
            # catatan kehadiran tidak diubah sama sekali, tabel mingguan
            # & Excel tetap menampilkan data asli, dan panel Laporan Hari
            # Ini di bawah kartu menampilkan daftar sebenarnya.
            k_hadir_s = k_telat_s = k_sakit_s = k_alpa_s = k_hadir_g = 0
            k_belum_s = total_siswa
            k_belum_g = total_guru

        def stat(ic, warna, angka, label):
            return (f'<div class="stat-card"><span class="stat-ic {warna}">{icon(ic)}</span>'
                    f'<span class="stat-txt"><b>{angka}</b><small>{esc(label)}</small></span></div>')
        cards = "".join([
            stat("check", "hijau", k_hadir_s, "Siswa Hadir"),
            stat("clock", "kuning", k_telat_s, "Siswa Terlambat"),
            stat("users", "biru", k_sakit_s, "Siswa Sakit/Izin"),
            stat("x", "merah", k_alpa_s, "Siswa Alpa"),
            stat("scan", "ungu", k_belum_s, "Siswa Belum Scan"),
            stat("scan", "indigo", k_belum_g, "Guru Belum Scan"),
            stat("badge", "indigo", k_hadir_g, "Guru Hadir"),
        ])
        items = "".join(
            f'<li><span class="pill p-{r["status"].lower()}">{esc(r["status"])}</span> '
            f'<b>{esc(r["nama"])}</b> <span class="muted">{esc(r["sub"] or "")}</span> '
            f'<span class="jam">{esc(r["jam"] or "")}</span></li>' for r in datang)
        items = items or '<li class="muted">Belum ada yang scan hari ini.</li>'
        if panel_laporan is None:
            panel_laporan = ('<section class="card"><h3>Kedatangan Terakhir Hari Ini</h3>\n'
                             f'<ul class="arrivals">{items}</ul></section>')
        warn = ""
        if user["role"] == "admin" and verify_password("admin123", user["password_hash"], user["salt"]):
            warn = ('<div class="flash warn">PERINGATAN: akun admin ini masih memakai '
                    'password awal lokal. Segera ganti lewat menu Ganti Password '
                    'sebelum dipakai beneran.</div>')
        body = f"""<h2>Dashboard</h2>
        <p class="muted">{esc(NAMA_HARI[today.weekday()])}, {esc(fmt_tanggal(today))} &bull;
        batas Hadir {JAM_BATAS_HADIR.strftime('%H:%M')}, Alpa otomatis {JAM_BATAS_ALPA.strftime('%H:%M')}</p>
        {warn}<div class="card search-card">
          <form method="get" action="/cari" class="search-form">
            <label class="search-label">Pencarian
              <input name="q" placeholder="Cari nama siswa, NIS, kelas, nama guru, atau mata pelajaran…"></label>
            <button type="submit" class="btn btn-primary">Cari</button>
          </form>
        </div>
        <div class="stat-grid">{cards}</div>
        <div class="quick-grid">
          <a class="quick-card" href="/scan-gerbang">{icon('scan')}<span>Scan Gerbang</span></a>
          <a class="quick-card" href="/tabel-siswa">{icon('users')}<span>Tabel Siswa</span></a>
          <a class="quick-card" href="/tabel-guru">{icon('badge')}<span>Tabel Guru</span></a>
        </div>
        {panel_laporan}"""
        return self.send_html(layout(user, "Dashboard", body, flash, aktif="dashboard"))

    def _panel_laporan(self, conn, tgl):
        """Panel 'Laporan Hari Ini' untuk dashboard mulai pukul 09:00:
        siapa yang Terlambat dan siapa yang Belum Scan hari ini, siswa
        dan guru terpisah. 'Belum Scan' = hanya punya catatan Alpa
        OTOMATIS (metode 'auto'), atau belum punya catatan sama sekali.
        Dihitung langsung dari catatan hari ini (bukan snapshot). Scan
        sesudah 09:00 sudah ditutup (keputusan user), jadi daftar ini
        final: yang Belum Scan tetap Belum Scan. Yang Sakit/Izin
        tidak muncul di daftar mana pun (sudah ada keterangan)."""
        sis_telat = conn.execute(
            """SELECT s.nama AS nama, c.nama AS sub, k.jam AS jam
               FROM kehadiran k JOIN students s ON s.id=k.ref_id
               JOIN classes c ON c.id=s.class_id
               WHERE k.tanggal=? AND k.ref_type='siswa' AND k.status='Terlambat'
               ORDER BY k.jam, s.nama""", (tgl,)).fetchall()
        sis_belum = conn.execute(
            """SELECT s.nama AS nama, c.nama AS sub
               FROM students s JOIN classes c ON c.id=s.class_id
               LEFT JOIN kehadiran k ON k.tanggal=? AND k.ref_type='siswa'
                    AND k.ref_id=s.id
               WHERE k.id IS NULL OR (k.metode='auto' AND k.status='Alpa')
               ORDER BY c.nama, s.nama""", (tgl,)).fetchall()
        gur_telat = conn.execute(
            """SELECT u.nama AS nama, u.mapel AS sub, k.jam AS jam
               FROM kehadiran k JOIN users u ON u.id=k.ref_id
               WHERE k.tanggal=? AND k.ref_type='guru' AND k.status='Terlambat'
                     AND u.role='guru' AND u.aktif=1
               ORDER BY k.jam, u.nama""", (tgl,)).fetchall()
        gur_belum = conn.execute(
            """SELECT u.nama AS nama, u.mapel AS sub
               FROM users u
               LEFT JOIN kehadiran k ON k.tanggal=? AND k.ref_type='guru'
                    AND k.ref_id=u.id
               WHERE u.role='guru' AND u.aktif=1
                     AND (k.id IS NULL OR (k.metode='auto' AND k.status='Alpa'))
               ORDER BY u.nama""", (tgl,)).fetchall()

        def bagian(judul, baris, pakai_jam):
            isi = "".join(
                f'<li><span class="no">{i}.</span> <b>{esc(r["nama"])}</b> '
                f'<span class="muted">{esc(r["sub"] or "")}</span>'
                + (f' <span class="jam">{esc(r["jam"] or "")}</span>' if pakai_jam else "")
                + "</li>"
                for i, r in enumerate(baris, 1))
            isi = isi or '<li class="muted">Tidak ada.</li>'
            return (f"<h4>{esc(judul)} ({len(baris)})</h4>"
                    f'<ul class="plain-list">{isi}</ul>')

        return ('<section class="card laporan"><h3>Laporan Hari Ini</h3>'
                + bagian("Siswa Terlambat", sis_telat, True)
                + bagian("Siswa Belum Scan", sis_belum, False)
                + bagian("Guru Terlambat", gur_telat, True)
                + bagian("Guru Belum Scan", gur_belum, False)
                + "</section>")

    # -- scan gerbang -----------------------------------------------------------
    def _kedatangan_hari_ini(self, conn, tgl):
        return conn.execute(
            """SELECT k.status, k.jam, s.nama AS nama, c.nama AS sub, 'Siswa' AS tipe
               FROM kehadiran k JOIN students s ON s.id=k.ref_id
               JOIN classes c ON c.id=s.class_id
               WHERE k.tanggal=? AND k.ref_type='siswa' AND k.metode='scan'
               UNION ALL
               SELECT k.status, k.jam, u.nama, u.mapel, 'Guru'
               FROM kehadiran k JOIN users u ON u.id=k.ref_id
               WHERE k.tanggal=? AND k.ref_type='guru' AND k.metode='scan'
               ORDER BY jam DESC""", (tgl, tgl)).fetchall()

    def _daftar_datang_html(self, conn, tgl):
        """(jumlah, HTML <li>) daftar kedatangan scan hari ini — dipakai halaman
        Scan Gerbang dan balasan JSON mode ajax (update daftar tanpa reload)."""
        datang = self._kedatangan_hari_ini(conn, tgl)
        items = "".join(
            f'<li><span class="pill p-{r["status"].lower()}">{esc(r["status"])}</span> '
            f'<b>{esc(r["nama"])}</b> <span class="muted">{esc(r["tipe"])} &bull; '
            f'{esc(r["sub"] or "")}</span> <span class="jam">{esc(r["jam"] or "")}</span></li>'
            for r in datang)
        items = items or '<li class="muted">Belum ada yang scan hari ini.</li>'
        return len(datang), items

    def scan_gerbang(self, user, flash, hasil=None):
        now = waktu_sekarang()
        tgl = now.strftime("%Y-%m-%d")
        conn = db()
        jumlah_datang, items = self._daftar_datang_html(conn, tgl)
        conn.close()
        hasil_html = ""
        if hasil:
            hasil_html = (f'<div class="hasil hasil-{hasil["jenis"]}">'
                          f'<div class="hasil-nama">{esc(hasil["judul"])}</div>'
                          f'<div class="hasil-meta">{esc(hasil["pesan"])}</div></div>')
        body = f"""<h2>Scan Gerbang</h2>
        <p class="muted">{esc(NAMA_HARI[now.weekday()])}, {esc(fmt_tanggal(now.date()))} &bull;
        pukul {esc(now.strftime('%H:%M'))} WIB &bull; scan &le; 07:00 = Hadir, lewat = Terlambat, scan ditutup 09:00</p>
        <div id="hasilScan">{hasil_html}</div>
        <section class="card scan-card">
          <div class="scan-frame"><video id="cam" autoplay playsinline muted></video></div>
          <div class="scan-btns"><button type="button" id="btnCam" class="btn btn-primary">Nyalakan Kamera</button>
          <button type="button" id="btnStop" class="btn btn-ghost" disabled>Matikan Kamera</button></div>
          <p id="scanStatus" class="muted"><small>Kamera memakai getUserMedia + jsQR.
          Sekali dinyalakan, kamera TETAP MENYALA dan tiap kartu QR yang terbaca
          langsung tercatat &mdash; matikan manual bila sudah selesai.
          Bila kamera tidak tersedia, ketik NIS / kode guru di bawah.</small></p>
          <form method="post" action="/api/scan" id="scanForm">
            <label>NIS siswa / kode QR guru (hasil scan / ketik manual)
              <input name="kode" id="kodeInput" required autocomplete="off"></label>
            <button type="submit" class="btn btn-primary">Catat dari Kode</button></form>
        </section>
        <section class="card"><h3>Sudah Scan Hari Ini (<span id="jumlahDatang">{jumlah_datang}</span>)</h3>
        <ul class="arrivals" id="daftarDatang">{items}</ul></section>
        <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"></script>"""
        return self.send_html(layout(user, "Scan Gerbang", body, flash, aktif="scan"))

    def post_scan(self, user, f):
        kode = (f.get("kode") or "").strip()
        hasil = self._proses_scan(user, kode)
        if (f.get("ajax") or "") == "1":
            # Mode ajax (kamera menyala terus): balas JSON berisi banner hasil
            # + daftar kedatangan terbaru; halaman TIDAK dimuat ulang sehingga
            # stream kamera tidak mati.
            tgl = waktu_sekarang().strftime("%Y-%m-%d")
            conn = db()
            jumlah, items = self._daftar_datang_html(conn, tgl)
            conn.close()
            data = json.dumps({"jenis": hasil["jenis"], "judul": hasil["judul"],
                               "pesan": hasil["pesan"], "jumlah": jumlah,
                               "items": items}).encode("utf-8")
            return self.send_bytes(data, "application/json")
        return self.scan_gerbang(user, "", hasil=hasil)

    def _proses_scan(self, user, kode):
        """Catat satu kode scan untuk HARI INI; kembalikan dict hasil
        {jenis, judul, pesan} untuk ditampilkan sebagai banner."""
        now = waktu_sekarang()
        tgl = now.strftime("%Y-%m-%d")
        jam = now.strftime("%H:%M")
        conn = db()
        siswa = conn.execute(
            """SELECT s.*, c.nama AS kelas FROM students s JOIN classes c ON c.id=s.class_id
               WHERE s.kode=?""", (kode,)).fetchone()
        guru_row = None
        if not siswa:
            guru_row = conn.execute(
                "SELECT * FROM users WHERE kode_qr=? AND role='guru' AND aktif=1",
                (kode,)).fetchone()
        if not siswa and not guru_row:
            conn.close()
            return {
                "jenis": "error", "judul": "Kode tidak dikenal.",
                "pesan": f"Kode '{kode}' tidak terdaftar sebagai NIS siswa maupun kode guru."}
        if siswa:
            ref_type, ref_id, nama = "siswa", siswa["id"], siswa["nama"]
            sub = siswa["kelas"]
        else:
            ref_type, ref_id, nama = "guru", guru_row["id"], guru_row["nama"]
            sub = guru_row["mapel"] or ""
        # Bila sudah lewat 09:00, Alpa otomatis mungkin sudah terisi untuk
        # hari ini; pastikan_alpa memastikan catatan itu ada sebelum dicek.
        pastikan_alpa(conn, tgl)
        ada = conn.execute(
            "SELECT * FROM kehadiran WHERE tanggal=? AND ref_type=? AND ref_id=?",
            (tgl, ref_type, ref_id)).fetchone()
        # Keputusan user: scan pukul 09:00 ke atas TIDAK berlaku lagi.
        # Orang yang belum punya catatan ASLI hari ini (belum ada catatan
        # sama sekali, atau hanya Alpa otomatis) DITOLAK: tidak dibuat /
        # diubah apa pun, Alpa otomatisnya tetap berdiri. Yang sudah
        # tercatat asli (scan atau manual) tetap jatuh ke cabang-cabang
        # "Sudah tercatat" di bawah seperti biasa.
        if now.time() >= JAM_BATAS_ALPA and (ada is None or ada["metode"] == "auto"):
            conn.close()
            return {
                "jenis": "error", "judul": "Scan sudah ditutup",
                "pesan": f"Scan sudah ditutup (lewat jam 09.00). "
                         f"{nama} tetap tercatat Alpa hari ini."}
        if ada:
            if ada["metode"] == "scan":
                conn.close()
                return {
                    "jenis": "info", "judul": "Sudah tercatat",
                    "pesan": f"{nama} sudah tercatat {ada['status']} pukul {ada['jam'] or '-'} hari ini."}
            if ada["status"] in ("Sakit", "Izin"):
                conn.close()
                return {
                    "jenis": "info", "judul": f"{nama} ditandai {ada['status']}",
                    "pesan": f"{nama} hari ini ditandai {ada['status']} (manual), scan tidak mengubahnya."}
            if ada["metode"] == "manual":
                conn.close()
                return {
                    "jenis": "info", "judul": "Sudah tercatat",
                    "pesan": f"{nama} sudah tercatat {ada['status']} (manual) hari ini."}
            # metode 'auto' (Alpa otomatis) sebelum 09:00 -> ganti dengan
            # catatan scan di bawah. (Sesudah 09:00 kasus ini sudah
            # ditolak oleh pemeriksaan "Scan sudah ditutup" di atas.)
            conn.execute("DELETE FROM kehadiran WHERE id=?", (ada["id"],))
        status = "Hadir" if now.time() <= JAM_BATAS_HADIR else "Terlambat"
        conn.execute(
            """INSERT INTO kehadiran (tanggal, ref_type, ref_id, status, jam, metode, dicatat_oleh)
               VALUES (?,?,?,?,?,'scan',?)""",
            (tgl, ref_type, ref_id, status, jam, user["id"]))
        conn.commit(); conn.close()
        return {
            "jenis": status.lower(), "judul": nama,
            "pesan": f"{sub} • {status} • {jam}"}


    # -- tabel mingguan ---------------------------------------------------------
    def _parse_minggu(self, qs):
        raw = (qs.get("minggu") or [""])[0]
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            d = waktu_sekarang().date()
        return minggu_sen_sab(d)

    def _peta_kehadiran(self, conn, ref_type, ref_ids, tgl_awal, tgl_akhir):
        if not ref_ids:
            return {}
        tanda = ",".join("?" * len(ref_ids))
        rows = conn.execute(
            f"""SELECT * FROM kehadiran WHERE ref_type=? AND tanggal BETWEEN ? AND ?
                AND ref_id IN ({tanda})""",
            (ref_type, tgl_awal, tgl_akhir, *ref_ids)).fetchall()
        return {(r["ref_id"], r["tanggal"]): r for r in rows}

    def _sel_html(self, rec, tanggal, tipe, ref_id, hari_ini):
        link = f'/kehadiran/edit?tanggal={tanggal}&tipe={tipe}&id={ref_id}'
        if rec:
            jam = f'<span class="jam">{esc(fmt_jam_titik(rec["jam"]))}</span>' if rec["jam"] else ""
            return (f'<td><a class="sel" href="{link}">'
                    f'<span class="pill p-{rec["status"].lower()}">{esc(rec["status"])}</span>{jam}</a></td>')
        if tanggal > hari_ini:
            return '<td><span class="kosong">&ndash;</span></td>'
        return f'<td><a class="sel kosong" href="{link}">&ndash;</a></td>'

    def _kepala_tabel(self, hari_list):
        th = "".join(f'<th>{NAMA_HARI[d.weekday()]}<br><small>{d.day} '
                     f'{NAMA_BULAN_SINGKAT[d.month - 1]}</small></th>' for d in hari_list)
        tot = "".join(f'<th class="tot">{s[0]}</th>' for s in STATUS_LIST)
        return th, tot

    def _total_bawah(self, per_hari):
        tds = ""
        for per_status in per_hari:
            bagian = [f"{s[0]} {per_status[s]}" for s in STATUS_LIST if per_status.get(s)]
            tds += f'<td class="tot-hari">{"<br>".join(bagian) or "&ndash;"}</td>'
        return tds

    def tabel_siswa(self, user, qs, flash):
        conn = db()
        classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        if not classes:
            conn.close()
            body = ('<h2>Tabel Siswa</h2><div class="card"><p>Belum ada kelas. '
                    + ('Admin perlu membuat kelas dulu dari menu Kelas.'
                       if user["role"] == "admin" else
                       'Minta admin membuat kelas dulu.') + '</p></div>')
            return self.send_html(layout(user, "Tabel Siswa", body, flash, aktif="tabel-siswa"))
        kid_raw = (qs.get("kelas") or [""])[0]
        try:
            kid = int(kid_raw)
        except (ValueError, TypeError):
            kid = classes[0]["id"]
        kelas = next((c for c in classes if c["id"] == kid), classes[0])
        hari_list = self._parse_minggu(qs)
        for d in hari_list:
            pastikan_alpa(conn, d.strftime("%Y-%m-%d"))
        students = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY nama",
                                (kelas["id"],)).fetchall()
        peta = self._peta_kehadiran(conn, "siswa", [s["id"] for s in students],
                                    hari_list[0].strftime("%Y-%m-%d"),
                                    hari_list[-1].strftime("%Y-%m-%d"))
        conn.close()
        hari_ini = waktu_sekarang().date().strftime("%Y-%m-%d")
        th_hari, th_tot = self._kepala_tabel(hari_list)
        opts = "".join(f'<option value="{c["id"]}"{" selected" if c["id"] == kelas["id"] else ""}>'
                       f'{esc(c["nama"])}</option>' for c in classes)
        senin = hari_list[0]
        prev_m = (senin - timedelta(days=7)).strftime("%Y-%m-%d")
        next_m = (senin + timedelta(days=7)).strftime("%Y-%m-%d")
        base = f"/tabel-siswa?kelas={kelas['id']}"
        rows_html = ""
        per_hari = [dict() for _ in hari_list]
        for i, s in enumerate(students):
            totals = {st: 0 for st in STATUS_LIST}
            tds = ""
            for j, d in enumerate(hari_list):
                tgl = d.strftime("%Y-%m-%d")
                rec = peta.get((s["id"], tgl))
                if rec:
                    totals[rec["status"]] += 1
                    per_hari[j][rec["status"]] = per_hari[j].get(rec["status"], 0) + 1
                tds += self._sel_html(rec, tgl, "siswa", s["id"], hari_ini)
            tot_tds = "".join(f'<td class="tot">{totals[st] or ""}</td>' for st in STATUS_LIST)
            rows_html += (f'<tr><td class="no">{i + 1}</td>'
                          f'<td class="nama">{esc(s["nama"])}<br>'
                          f'<small class="muted">NIS {esc(s["kode"])}</small></td>'
                          f'{tds}{tot_tds}</tr>')
        rows_html = rows_html or (
            f'<tr><td colspan="14"><div class="empty-state">{icon("users")}'
            f'<p>Belum ada siswa di kelas ini.</p>'
            f'<small>Siswa yang ditambahkan admin akan muncul di sini.</small></div></td></tr>')
        total_row = (f'<tr class="total-row"><td></td><td class="nama">Total per hari</td>'
                     f'{self._total_bawah(per_hari)}'
                     + "".join(f'<td class="tot">{sum(per_hari[j].get(st, 0) for j in range(6)) or ""}</td>'
                               for st in STATUS_LIST) + '</tr>')
        body = f"""<h2>Tabel Siswa</h2>
        <div class="card toolbar-card">
          <form method="get" action="/tabel-siswa" class="toolbar">
            <label>Kelas <select name="kelas">{opts}</select></label>
            <button type="submit" class="btn btn-primary">Tampilkan</button>
            <a class="btn btn-excel" href="/tabel-siswa/excel?kelas={kelas['id']}&minggu={senin.strftime('%Y-%m-%d')}">{icon('download')} Download Excel</a>
          </form>
          <div class="week-nav">
            <a class="btn btn-ghost" href="{base}&minggu={prev_m}">&larr; Minggu lalu</a>
            <span class="week-label">{esc(fmt_tanggal(hari_list[0]))} &ndash; {esc(fmt_tanggal(hari_list[-1]))}</span>
            <a class="btn btn-ghost" href="{base}&minggu={next_m}">Minggu depan &rarr;</a>
          </div>
        </div>
        <div class="card table-card"><div class="table-scroll">
        <table class="tabel"><thead><tr><th class="no">No</th><th class="nama">Nama</th>
        {th_hari}{th_tot}</tr></thead>
        <tbody>{rows_html}{total_row}</tbody></table></div>
        <p class="muted"><small>Ketuk sel untuk mengubah status manual (Sakit/Izin/dll).
        H=Hadir, T=Terlambat, S=Sakit, I=Izin, A=Alpa.</small></p></div>"""
        return self.send_html(layout(user, "Tabel Siswa", body, flash, aktif="tabel-siswa"))

    def tabel_guru(self, user, qs, flash):
        conn = db()
        hari_list = self._parse_minggu(qs)
        for d in hari_list:
            pastikan_alpa(conn, d.strftime("%Y-%m-%d"))
        gurus = conn.execute(
            "SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        peta = self._peta_kehadiran(conn, "guru", [g["id"] for g in gurus],
                                    hari_list[0].strftime("%Y-%m-%d"),
                                    hari_list[-1].strftime("%Y-%m-%d"))
        conn.close()
        hari_ini = waktu_sekarang().date().strftime("%Y-%m-%d")
        th_hari, th_tot = self._kepala_tabel(hari_list)
        senin = hari_list[0]
        prev_m = (senin - timedelta(days=7)).strftime("%Y-%m-%d")
        next_m = (senin + timedelta(days=7)).strftime("%Y-%m-%d")
        rows_html = ""
        per_hari = [dict() for _ in hari_list]
        for i, g in enumerate(gurus):
            totals = {st: 0 for st in STATUS_LIST}
            tds = ""
            for j, d in enumerate(hari_list):
                tgl = d.strftime("%Y-%m-%d")
                rec = peta.get((g["id"], tgl))
                if rec:
                    totals[rec["status"]] += 1
                    per_hari[j][rec["status"]] = per_hari[j].get(rec["status"], 0) + 1
                tds += self._sel_html(rec, tgl, "guru", g["id"], hari_ini)
            tot_tds = "".join(f'<td class="tot">{totals[st] or ""}</td>' for st in STATUS_LIST)
            rows_html += (f'<tr><td class="no">{i + 1}</td>'
                          f'<td class="nama">{esc(g["nama"])}<br>'
                          f'<small class="muted">{esc(g["mapel"] or "-")}</small></td>'
                          f'{tds}{tot_tds}</tr>')
        rows_html = rows_html or (
            f'<tr><td colspan="14"><div class="empty-state">{icon("badge")}'
            f'<p>Belum ada guru aktif.</p>'
            f'<small>Guru yang ditambahkan admin akan muncul di sini.</small></div></td></tr>')
        total_row = (f'<tr class="total-row"><td></td><td class="nama">Total per hari</td>'
                     f'{self._total_bawah(per_hari)}'
                     + "".join(f'<td class="tot">{sum(per_hari[j].get(st, 0) for j in range(6)) or ""}</td>'
                               for st in STATUS_LIST) + '</tr>')
        body = f"""<h2>Tabel Guru</h2>
        <div class="card toolbar-card">
          <div class="week-nav">
            <a class="btn btn-ghost" href="/tabel-guru?minggu={prev_m}">&larr; Minggu lalu</a>
            <span class="week-label">{esc(fmt_tanggal(hari_list[0]))} &ndash; {esc(fmt_tanggal(hari_list[-1]))}</span>
            <a class="btn btn-ghost" href="/tabel-guru?minggu={next_m}">Minggu depan &rarr;</a>
            <a class="btn btn-excel" href="/tabel-guru/excel?minggu={senin.strftime('%Y-%m-%d')}">{icon('download')} Download Excel</a>
          </div>
        </div>
        <div class="card table-card"><div class="table-scroll">
        <table class="tabel"><thead><tr><th class="no">No</th><th class="nama">Nama</th>
        {th_hari}{th_tot}</tr></thead>
        <tbody>{rows_html}{total_row}</tbody></table></div>
        <p class="muted"><small>Ketuk sel untuk mengubah status manual. Kolom kedua nama berisi mata pelajaran guru.</small></p></div>"""
        return self.send_html(layout(user, "Tabel Guru", body, flash, aktif="tabel-guru"))

    # -- edit kehadiran manual ---------------------------------------------------
    def kehadiran_edit(self, user, qs, flash):
        tanggal = (qs.get("tanggal") or [""])[0]
        tipe = (qs.get("tipe") or [""])[0]
        try:
            ref_id = int((qs.get("id") or ["0"])[0])
        except ValueError:
            ref_id = 0
        conn = db()
        orang, sub = None, ""
        if tipe == "siswa":
            orang = conn.execute(
                """SELECT s.*, c.nama AS sub FROM students s JOIN classes c ON c.id=s.class_id
                   WHERE s.id=?""", (ref_id,)).fetchone()
        elif tipe == "guru":
            r = conn.execute("SELECT * FROM users WHERE id=? AND role='guru'", (ref_id,)).fetchone()
            if r:
                orang, sub = r, (r["mapel"] or "")
        if not orang:
            conn.close()
            return self.redirect("/dashboard", "Data tidak ditemukan.")
        if tipe == "siswa":
            sub = orang["sub"]
        rec = conn.execute(
            "SELECT * FROM kehadiran WHERE tanggal=? AND ref_type=? AND ref_id=?",
            (tanggal, tipe, ref_id)).fetchone()
        conn.close()
        try:
            d = datetime.strptime(tanggal, "%Y-%m-%d").date()
            tgl_label = f"{NAMA_HARI[d.weekday()]}, {fmt_tanggal(d)}"
        except ValueError:
            tgl_label = tanggal
        status_opts = "".join(
            f'<option{" selected" if rec and rec["status"] == st else ""}>{st}</option>'
            for st in STATUS_LIST)
        jam_val = rec["jam"] if rec and rec["jam"] else ""
        body = f"""<h2>Ubah Kehadiran</h2>
        <form method="post" action="/kehadiran/edit" class="card form-card">
          <input type="hidden" name="tipe" value="{esc(tipe)}">
          <input type="hidden" name="id" value="{ref_id}">
          <p><b>{esc(orang['nama'])}</b> <span class="muted">{esc(sub)}</span></p>
          <label>Tanggal <input type="date" name="tanggal" value="{esc(tanggal)}" required></label>
          <p class="muted"><small>{esc(tgl_label)}</small></p>
          <label>Status <select name="status">{status_opts}</select></label>
          <label>Jam masuk (untuk Hadir/Terlambat; boleh kosong)
            <input type="time" name="jam" value="{esc(jam_val)}"></label>
          <button type="submit" class="btn btn-primary">Simpan</button></form>
        <p><a href="/tabel-{'siswa' if tipe == 'siswa' else 'guru'}">Kembali ke tabel</a></p>"""
        return self.send_html(layout(user, "Ubah Kehadiran", body, flash))

    def post_kehadiran_edit(self, user, f):
        tipe = f.get("tipe", "")
        tanggal = (f.get("tanggal") or "").strip()
        status = f.get("status", "")
        try:
            ref_id = int(f.get("id", "0"))
            datetime.strptime(tanggal, "%Y-%m-%d")
        except (ValueError, TypeError):
            return self.redirect("/dashboard", "Data tidak valid.")
        if tipe not in ("siswa", "guru") or status not in STATUS_LIST:
            return self.redirect("/dashboard", "Data tidak valid.")
        conn = db()
        if tipe == "siswa":
            orang = conn.execute("SELECT * FROM students WHERE id=?", (ref_id,)).fetchone()
        else:
            orang = conn.execute("SELECT * FROM users WHERE id=? AND role='guru'",
                                 (ref_id,)).fetchone()
        if not orang:
            conn.close()
            return self.redirect("/dashboard", "Data tidak ditemukan.")
        jam = (f.get("jam") or "").strip() or None
        if status not in ("Hadir", "Terlambat"):
            jam = None
        elif not jam and tanggal == waktu_sekarang().strftime("%Y-%m-%d"):
            jam = waktu_sekarang().strftime("%H:%M")
        conn.execute(
            """INSERT INTO kehadiran (tanggal, ref_type, ref_id, status, jam, metode, dicatat_oleh)
               VALUES (?,?,?,?,?,'manual',?)
               ON CONFLICT(tanggal, ref_type, ref_id) DO UPDATE SET
                 status=excluded.status, jam=excluded.jam, metode='manual',
                 dicatat_oleh=excluded.dicatat_oleh""",
            (tanggal, tipe, ref_id, status, jam, user["id"]))
        conn.commit(); conn.close()
        if tipe == "siswa":
            return self.redirect(
                f"/tabel-siswa?kelas={orang['class_id']}&minggu={tanggal}",
                f"{orang['nama']} ditandai {status} pada {tanggal}.")
        return self.redirect(f"/tabel-guru?minggu={tanggal}",
                             f"{orang['nama']} ditandai {status} pada {tanggal}.")

    # -- Excel (openpyxl) ---------------------------------------------------------
    def _buat_excel(self, judul, subjudul, label_kolom3, baris, hari_list, nama_sekolah=""):
        """baris: list dict {nama, kolom3, sel: [(status, jam)|None x6], total: {status: n}}"""
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = "Absensi"
        tipis = Side(style="thin", color="9AA3B2")
        border = Border(left=tipis, right=tipis, top=tipis, bottom=tipis)
        fill_status = {"Hadir": "C6EFCE", "Terlambat": "FFEB9C", "Sakit": "BDD7EE",
                       "Izin": "E4DFEC", "Alpa": "FFC7CE"}
        font_status = {"Hadir": "006100", "Terlambat": "9C6500", "Sakit": "1F4E79",
                       "Izin": "5B4A8A", "Alpa": "9C0006"}
        nkol = 3 + 6 + 5
        akhir = get_column_letter(nkol)
        periode = (f"Minggu: {NAMA_HARI[0]}, {fmt_tanggal(hari_list[0])} – "
                   f"{NAMA_HARI[5]}, {fmt_tanggal(hari_list[-1])}")
        off = 1 if nama_sekolah else 0  # baris nama sekolah di paling atas bila diisi
        if nama_sekolah:
            ws.merge_cells(f"A1:{akhir}1")
            c = ws["A1"]; c.value = nama_sekolah
            c.font = Font(bold=True, size=16, color="1F3864")
        ws.merge_cells(f"A{1 + off}:{akhir}{1 + off}")
        c = ws[f"A{1 + off}"]; c.value = judul
        c.font = Font(bold=True, size=15, color="1F3864")
        ws.merge_cells(f"A{2 + off}:{akhir}{2 + off}")
        c = ws[f"A{2 + off}"]; c.value = subjudul
        c.font = Font(bold=True, size=11, color="404040")
        ws.merge_cells(f"A{3 + off}:{akhir}{3 + off}")
        c = ws[f"A{3 + off}"]; c.value = periode
        c.font = Font(size=11, color="404040")
        header = ["No", "Nama", label_kolom3]
        header += [f"{NAMA_HARI[d.weekday()]}\n{d.day} {NAMA_BULAN_SINGKAT[d.month - 1]}"
                   for d in hari_list]
        header += ["H", "T", "S", "I", "A"]
        fill_head = PatternFill("solid", fgColor="203764")
        font_head = Font(bold=True, color="FFFFFF", size=11)
        for j, h in enumerate(header, start=1):
            c = ws.cell(row=4 + off, column=j, value=h)
            c.fill = fill_head; c.font = font_head
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = border
        ws.row_dimensions[4 + off].height = 30
        per_hari = [dict() for _ in hari_list]
        r = 5 + off
        for i, b in enumerate(baris):
            zebra = PatternFill("solid", fgColor="F2F5FA") if i % 2 == 1 else None
            vals = [i + 1, b["nama"], b["kolom3"]]
            for j in range(3):
                c = ws.cell(row=r, column=j + 1, value=vals[j])
                c.border = border
                if zebra: c.fill = zebra
                c.alignment = Alignment(horizontal="center" if j != 1 else "left",
                                        vertical="center")
            for j, sel in enumerate(b["sel"]):
                c = ws.cell(row=r, column=4 + j)
                c.border = border
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                if sel:
                    status, jam = sel
                    c.value = f"{status}\n{fmt_jam_titik(jam)}" if jam else status
                    c.fill = PatternFill("solid", fgColor=fill_status[status])
                    c.font = Font(color=font_status[status], size=10)
                    per_hari[j][status] = per_hari[j].get(status, 0) + 1
                elif zebra:
                    c.fill = zebra
            for j, st in enumerate(STATUS_LIST):
                c = ws.cell(row=r, column=10 + j, value=b["total"].get(st, 0) or None)
                c.border = border
                c.alignment = Alignment(horizontal="center", vertical="center")
                if zebra: c.fill = zebra
            r += 1
        # baris total bawah
        c = ws.cell(row=r, column=2, value="TOTAL")
        c.font = Font(bold=True); c.border = border
        ws.cell(row=r, column=1).border = border
        ws.cell(row=r, column=3).border = border
        for j in range(6):
            c = ws.cell(row=r, column=4 + j)
            bagian = [f"{st[0]}: {per_hari[j][st]}" for st in STATUS_LIST if per_hari[j].get(st)]
            c.value = "\n".join(bagian) if bagian else None
            c.font = Font(bold=True, size=9)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = border
        for j, st in enumerate(STATUS_LIST):
            c = ws.cell(row=r, column=10 + j,
                        value=sum(per_hari[d].get(st, 0) for d in range(6)) or None)
            c.font = Font(bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
        lebar = {1: 5, 2: 34, 3: 16}
        for j in range(4, 10):
            lebar[j] = 13
        for j in range(10, 15):
            lebar[j] = 5
        for j, w in lebar.items():
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = f"A{5 + off}"
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _nama_file_excel(self, awalan, label, hari_list):
        bersih = re.sub(r"[^\w.\-]+", "_", label).strip("_") or "data"
        a, b = hari_list[0], hari_list[-1]
        return (f"{awalan}_{bersih}_{a.day}-{b.day}"
                f"{NAMA_BULAN_SINGKAT[b.month - 1]}{b.year}.xlsx")

    def excel_siswa(self, user, qs):
        conn = db()
        classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        if not classes:
            conn.close()
            return self.redirect("/tabel-siswa", "Belum ada kelas.")
        try:
            kid = int((qs.get("kelas") or [""])[0])
        except (ValueError, TypeError):
            kid = classes[0]["id"]
        kelas = next((c for c in classes if c["id"] == kid), classes[0])
        hari_list = self._parse_minggu(qs)
        for d in hari_list:
            pastikan_alpa(conn, d.strftime("%Y-%m-%d"))
        students = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY nama",
                                (kelas["id"],)).fetchall()
        peta = self._peta_kehadiran(conn, "siswa", [s["id"] for s in students],
                                    hari_list[0].strftime("%Y-%m-%d"),
                                    hari_list[-1].strftime("%Y-%m-%d"))
        conn.close()
        baris = []
        for s in students:
            sel, total = [], {st: 0 for st in STATUS_LIST}
            for d in hari_list:
                rec = peta.get((s["id"], d.strftime("%Y-%m-%d")))
                if rec:
                    sel.append((rec["status"], rec["jam"]))
                    total[rec["status"]] += 1
                else:
                    sel.append(None)
            baris.append({"nama": s["nama"], "kolom3": s["kode"], "sel": sel, "total": total})
        data = self._buat_excel("ABSENSI SISWA", f"Kelas: {kelas['nama']}", "NIS",
                                baris, hari_list,
                                nama_sekolah=get_pengaturan()["nama_sekolah"])
        nama_file = self._nama_file_excel("Absensi", kelas["nama"], hari_list)
        return self.send_bytes(
            data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=nama_file)

    def excel_guru(self, user, qs):
        conn = db()
        hari_list = self._parse_minggu(qs)
        for d in hari_list:
            pastikan_alpa(conn, d.strftime("%Y-%m-%d"))
        gurus = conn.execute(
            "SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        peta = self._peta_kehadiran(conn, "guru", [g["id"] for g in gurus],
                                    hari_list[0].strftime("%Y-%m-%d"),
                                    hari_list[-1].strftime("%Y-%m-%d"))
        conn.close()
        baris = []
        for g in gurus:
            sel, total = [], {st: 0 for st in STATUS_LIST}
            for d in hari_list:
                rec = peta.get((g["id"], d.strftime("%Y-%m-%d")))
                if rec:
                    sel.append((rec["status"], rec["jam"]))
                    total[rec["status"]] += 1
                else:
                    sel.append(None)
            baris.append({"nama": g["nama"], "kolom3": g["mapel"] or "",
                          "sel": sel, "total": total})
        data = self._buat_excel("ABSENSI GURU", "Semua Guru", "Mapel", baris, hari_list,
                                nama_sekolah=get_pengaturan()["nama_sekolah"])
        nama_file = self._nama_file_excel("Absensi_Guru", "Semua_Guru", hari_list)
        return self.send_bytes(
            data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=nama_file)

    # -- menu (mobile) ---------------------------------------------------------
    def menu_page(self, user, flash):
        items = nav_items(user)
        cards = "".join(
            f'<a class="menu-card" href="{href}">{icon(ic)}<span>{label}</span></a>'
            for k, href, label, ic in items)
        cards += (f'<a class="menu-card danger" href="/logout">{icon("logout")}'
                  f'<span>Keluar</span></a>')
        role_label = "Admin" if user["role"] == "admin" else "Guru"
        body = f"""<h2>Menu</h2>
        <div class="card user-card"><span class="avatar lg">{esc((user['nama'] or '?')[:1].upper())}</span>
        <span><b>{esc(user['nama'])}</b><br><small class="muted">{role_label} &bull; @{esc(user['username'])}</small></span></div>
        <div class="menu-grid">{cards}</div>"""
        return self.send_html(layout(user, "Menu", body, flash, aktif="menu"))

    # -- cetak kartu QR ---------------------------------------------------------
    def cetak_index(self, user, flash):
        conn = db()
        classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        conn.close()
        links = "".join(
            f'<a class="menu-card" href="/cetak/kelas/{c["id"]}">{icon("users")}'
            f'<span>Kartu Siswa &mdash; {esc(c["nama"])}</span></a>' for c in classes)
        links = links or '<p class="muted">Belum ada kelas.</p>'
        body = f"""<h2>Cetak Kartu QR</h2>
        <h3>Kartu Siswa per Kelas</h3><div class="menu-grid">{links}</div>
        <h3>Kartu Guru</h3><div class="menu-grid">
        <a class="menu-card" href="/cetak/guru">{icon('badge')}<span>Kartu Semua Guru</span></a></div>
        <p class="muted"><small>Kartu berisi QR Code biasa (bukan QRIS pembayaran),
        di-scan di gerbang sekolah.</small></p>"""
        return self.send_html(layout(user, "Cetak Kartu", body, flash, aktif="cetak"))

    def cetak_kelas(self, user, class_id, flash):
        conn = db()
        kelas = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
        if not kelas:
            conn.close()
            return self.send_html(layout(user, "404", "<p>Kelas tidak ditemukan.</p>"), status=404)
        students = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY nama",
                                (class_id,)).fetchall()
        conn.close()
        cards = "".join(
            f"""<div class="kartu"><div class="knama">{esc(st['nama'])}</div>
                <div class="kkelas">{esc(kelas['nama'])}</div>
                <img src="/qr/{urllib.parse.quote(st['kode'])}.png" alt="QR {esc(st['kode'])}">
                <div class="kkode">NIS {esc(st['kode'])}</div></div>""" for st in students)
        body = f"""<h2 class="noprint">Cetak Kartu QR Siswa - {esc(kelas['nama'])}</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;Kartu berisi QR Code biasa dari NIS siswa (bukan QRIS). Gunting tiap kartu untuk dibagikan.</p>
        <div class="lembar">{cards or '<p>Belum ada siswa di kelas ini.</p>'}</div>"""
        return self.send_html(layout(user, "Cetak Kartu QR Siswa", body, flash, aktif="cetak"))

    def cetak_guru(self, user, flash):
        conn = db()
        gurus = conn.execute(
            "SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        conn.close()
        cards = "".join(
            f"""<div class="kartu"><div class="knama">{esc(g['nama'])}</div>
                <div class="kkelas">{esc(g['mapel'] or 'Guru')}</div>
                <img src="/qr/{urllib.parse.quote(g['kode_qr'] or '')}.png" alt="QR {esc(g['kode_qr'] or '')}">
                <div class="kkode">{esc(g['kode_qr'] or '')}</div></div>""" for g in gurus)
        body = f"""<h2 class="noprint">Cetak Kartu QR Guru</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;Kartu berisi QR Code biasa dari kode guru (bukan QRIS). Gunting tiap kartu untuk dibagikan.</p>
        <div class="lembar">{cards or '<p>Belum ada guru aktif.</p>'}</div>"""
        return self.send_html(layout(user, "Cetak Kartu QR Guru", body, flash, aktif="cetak"))

    def qr_png(self, raw_code):
        code = urllib.parse.unquote(raw_code)
        try:
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(code)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        except Exception as e:
            return self.send_html(f"Gagal membuat QR: {esc(e)}", status=500)
        return self.send_bytes(data, "image/png")


# ---------------------------------------------------------------------------
# Pengaturan tampilan (tabel settings key-value)
# ---------------------------------------------------------------------------
BG_PRESETS = ["#f3f5fb", "#ffffff", "#e6f4ea", "#fdeee3",
              "#ece7f7", "#e0f2fe", "#fef3c7", "#334155"]
BG_DEFAULT = "#f3f5fb"
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
WALLPAPER_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}
WALLPAPER_NAMA_RE = re.compile(r"^[0-9a-f]{24}\.(jpg|jpeg|png|webp)$")


def get_pengaturan():
    """Baca pengaturan tampilan; selalu kembalikan dict lengkap dgn default."""
    peng = {"bg_color": BG_DEFAULT, "wallpaper": "", "nama_sekolah": "", "logo": ""}
    try:
        conn = db()
        for r in conn.execute("SELECT key, value FROM settings").fetchall():
            if r["key"] in peng and r["value"]:
                peng[r["key"]] = r["value"]
        conn.close()
    except Exception:
        pass
    if not re.match(r"^#[0-9a-fA-F]{6}$", peng["bg_color"] or ""):
        peng["bg_color"] = BG_DEFAULT
    if peng["wallpaper"] and not WALLPAPER_NAMA_RE.match(peng["wallpaper"]):
        peng["wallpaper"] = ""
    if peng["logo"] and not WALLPAPER_NAMA_RE.match(peng["logo"]):
        peng["logo"] = ""
    return peng


def set_pengaturan(conn, key, value):
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))


def parse_multipart(body, content_type):
    """Parser multipart/form-data sederhana -> (fields, files).

    files: {nama_field: (filename, bytes)}. Cukup untuk form unggah satu
    berkas di aplikasi ini.
    """
    fields, files = {}, {}
    m = re.search(r"boundary=(.+)$", content_type or "")
    if not m:
        return fields, files
    boundary = m.group(1).strip().strip('"').encode()
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_txt = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]+)"', head_txt)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', head_txt)
        if file_m:
            files[name] = (file_m.group(1), data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


# ---------------------------------------------------------------------------
# Handler tambahan: admin CRUD + tampilan + wallpaper (ditempel ke Handler)
# ---------------------------------------------------------------------------
def _cari(self, user, qs, flash):
    """Pencarian global dari Dashboard: cocok sebagian (tidak peka huruf)
    pada nama siswa, NIS, nama kelas, nama guru, username guru, kode QR
    guru, dan mata pelajaran (mapel). Boleh dipakai admin & guru; tautan
    /admin/ hanya dirender untuk admin."""
    q = (qs.get("q") or [""])[0].strip()
    form = f"""<div class="card search-card">
      <form method="get" action="/cari" class="search-form">
        <label class="search-label">Pencarian
          <input name="q" value="{esc(q)}" placeholder="Cari nama siswa, NIS, kelas, nama guru, atau mata pelajaran…"></label>
        <button type="submit" class="btn btn-primary">Cari</button>
      </form></div>"""
    if not q:
        body = f"""<h2>Pencarian</h2>
        {form}
        <div class="card"><p class="muted">Ketik kata kunci untuk mencari.</p></div>"""
        return self.send_html(layout(user, "Pencarian", body, flash))
    # Escape wildcard LIKE (% dan _) plus escape char-nya sendiri, supaya
    # kata kunci dicocokkan apa adanya.
    pola = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conn = db()
    siswa_rows = conn.execute(
        """SELECT s.*, c.nama AS kelas FROM students s
           JOIN classes c ON c.id = s.class_id
           WHERE s.nama LIKE ? ESCAPE '\\' OR s.kode LIKE ? ESCAPE '\\'
              OR c.nama LIKE ? ESCAPE '\\'
           ORDER BY c.nama, s.nama LIMIT 50""",
        (pola, pola, pola)).fetchall()
    guru_rows = conn.execute(
        """SELECT * FROM users WHERE role='guru' AND aktif=1 AND
           (nama LIKE ? ESCAPE '\\' OR COALESCE(mapel,'') LIKE ? ESCAPE '\\'
            OR username LIKE ? ESCAPE '\\' OR COALESCE(kode_qr,'') LIKE ? ESCAPE '\\')
           ORDER BY nama LIMIT 50""",
        (pola, pola, pola, pola)).fetchall()
    conn.close()
    admin = user["role"] == "admin"
    ringkasan = (f'<p class="muted">Ditemukan {len(siswa_rows)} siswa dan '
                 f'{len(guru_rows)} guru untuk "{esc(q)}".</p>')
    bagian = ""
    if siswa_rows:
        items = ""
        for i, r in enumerate(siswa_rows):
            if admin:
                tautan = (f' &mdash; <a href="/admin/kelas/{r["class_id"]}">Lihat Kelas</a>'
                          f' &mdash; <a href="/admin/siswa/{r["id"]}/edit">Edit</a>')
            else:
                tautan = f' &mdash; <a href="/tabel-siswa?kelas={r["class_id"]}">Lihat Kelas</a>'
            items += (f'<li><span class="no">{i + 1}.</span> <b>{esc(r["nama"])}</b> '
                      f'&mdash; NIS: <code>{esc(r["kode"])}</code> '
                      f'&mdash; {esc(r["kelas"])}{tautan}</li>')
        bagian += f'<div class="card"><h3>Siswa</h3><ul class="plain-list">{items}</ul></div>'
    if guru_rows:
        items = ""
        for i, r in enumerate(guru_rows):
            tautan = (f' &mdash; <a href="/admin/guru/{r["id"]}/edit">Edit</a>'
                      if admin else "")
            items += (f'<li><span class="no">{i + 1}.</span> <b>{esc(r["nama"])}</b> '
                      f'&mdash; {esc(r["mapel"] or "—")} '
                      f'&mdash; username: <code>{esc(r["username"])}</code> '
                      f'&mdash; kode QR: <code>{esc(r["kode_qr"] or "-")}</code>{tautan}</li>')
        bagian += f'<div class="card"><h3>Guru</h3><ul class="plain-list">{items}</ul></div>'
    if not bagian:
        bagian = (f'<div class="card"><p class="muted">Tidak ada hasil untuk '
                  f'"{esc(q)}". Coba kata kunci lain.</p></div>')
    body = f"""<h2>Pencarian</h2>
    {form}
    {ringkasan}
    {bagian}"""
    return self.send_html(layout(user, "Pencarian", body, flash))


def _admin_guru(self, user, flash):
    conn = db()
    rows = conn.execute(
        """SELECT u.*,
                  (SELECT COUNT(*) FROM kehadiran k WHERE k.ref_type='guru' AND k.ref_id=u.id)
                + (SELECT COUNT(*) FROM kehadiran k WHERE k.dicatat_oleh=u.id) AS jml_riwayat
           FROM users u WHERE u.role='guru' ORDER BY u.nama""").fetchall()
    conn.close()
    items = ""
    for i, r in enumerate(rows):
        nama_js = js_str(r["nama"])
        dasar = (f'<li><span class="no">{i + 1}.</span> <b>{esc(r["nama"])}</b> '
                 f'&mdash; {esc(r["mapel"] or "-")} '
                 f'&mdash; username: <code>{esc(r["username"])}</code> '
                 f'&mdash; kode QR: <code>{esc(r["kode_qr"] or "-")}</code> '
                 f'&mdash; <a href="/admin/guru/{r["id"]}/edit">Edit</a> ')
        if not r["aktif"]:
            items += (dasar + '(nonaktif) '
                      f'<form method="post" action="/admin/guru/{r["id"]}/aktifkan" class="inline">'
                      f'<button type="submit" class="btn btn-ghost btn-sm">Aktifkan</button></form></li>')
        elif r["jml_riwayat"]:
            confirm_msg = (f"Nonaktifkan guru {nama_js}? Guru ini sudah punya riwayat kehadiran, "
                           f"jadi akunnya akan dinonaktifkan (bukan dihapus) dan riwayat "
                           f"kehadirannya tetap tersimpan.")
            items += (dasar +
                      f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                      f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                      f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
        else:
            confirm_msg = f"Hapus guru {nama_js}? Akun guru ini akan dihapus permanen."
            items += (dasar +
                      f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                      f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                      f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
    items = items or '<li class="muted">Belum ada guru.</li>'
    body = f"""<h2>Kelola Guru</h2>
    <div class="card"><ul class="plain-list">{items}</ul></div>
    <div class="card form-card"><h3>Tambah Guru</h3>
    <form method="post" action="/admin/guru">
      <label>Nama <input name="nama" required></label>
      <label>Mata Pelajaran <input name="mapel" required placeholder="mis. Matematika"></label>
      <label>Username <input name="username" required></label>
      <label>Password awal <input name="password" type="password" required minlength="8"></label>
      <button type="submit" class="btn btn-primary">Tambah Guru</button></form></div>"""
    return self.send_html(layout(user, "Guru", body, flash, aktif="guru"))


def _post_admin_guru(self, f):
    try:
        ph, salt = hash_password(f["password"])
        conn = db()
        cur = conn.execute(
            "INSERT INTO users (username,password_hash,salt,role,nama,mapel) VALUES (?,?,?,'guru',?,?)",
            (f["username"].strip(), ph, salt, f["nama"].strip(), f.get("mapel", "").strip()))
        gid = cur.lastrowid
        conn.execute("UPDATE users SET kode_qr=? WHERE id=?",
                     (kode_qr_guru_baru(conn, gid), gid))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru", "Guru ditambahkan.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect("/admin/guru", "Username sudah dipakai.")


def _get_guru(self, conn, gid):
    return conn.execute("SELECT * FROM users WHERE id=? AND role='guru'", (gid,)).fetchone()


def _admin_guru_edit(self, user, gid, flash):
    conn = db()
    g = self._get_guru(conn, gid)
    conn.close()
    if not g:
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    body = f"""<h2>Edit Guru</h2>
    <form method="post" action="/admin/guru/{g['id']}/edit" class="card form-card">
      <label>Nama <input name="nama" value="{esc(g['nama'])}" required></label>
      <label>Mata Pelajaran <input name="mapel" value="{esc(g['mapel'] or '')}" required></label>
      <label>Username <input name="username" value="{esc(g['username'])}" required></label>
      <label>Kode QR guru <input value="{esc(g['kode_qr'] or '')}" readonly disabled></label>
      <label>Password baru (kosongkan jika tidak diganti)
        <input name="password" type="password" minlength="8"></label>
      <button type="submit" class="btn btn-primary">Simpan Perubahan</button></form>
    <p><a href="/admin/guru">Kembali ke daftar guru</a></p>"""
    return self.send_html(layout(user, "Edit Guru", body, flash, aktif="guru"))


def _post_admin_guru_edit(self, gid, f):
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    try:
        pw = f.get("password", "")
        if pw:
            ph, salt = hash_password(pw)
            conn.execute(
                "UPDATE users SET nama=?, username=?, mapel=?, password_hash=?, salt=? WHERE id=?",
                (f["nama"].strip(), f["username"].strip(), f.get("mapel", "").strip(), ph, salt, gid))
        else:
            conn.execute(
                "UPDATE users SET nama=?, username=?, mapel=? WHERE id=?",
                (f["nama"].strip(), f["username"].strip(), f.get("mapel", "").strip(), gid))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru", "Guru diperbarui.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect(f"/admin/guru/{gid}/edit", "Username sudah dipakai.")


def _post_admin_guru_hapus(self, gid):
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    jml = conn.execute(
        """SELECT COUNT(*) c FROM kehadiran
           WHERE (ref_type='guru' AND ref_id=?) OR dicatat_oleh=?""", (gid, gid)).fetchone()["c"]
    if jml:
        # Sudah punya riwayat kehadiran: jangan hapus, cukup nonaktifkan.
        conn.execute("UPDATE users SET aktif=0 WHERE id=?", (gid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru",
                             "Guru dinonaktifkan karena sudah punya riwayat kehadiran. "
                             "Riwayat kehadirannya tetap tersimpan.")
    conn.execute("DELETE FROM users WHERE id=?", (gid,))
    conn.commit(); conn.close()
    return self.redirect("/admin/guru", "Guru dihapus.")


def _post_admin_guru_aktifkan(self, gid):
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    conn.execute("UPDATE users SET aktif=1 WHERE id=?", (gid,))
    conn.commit(); conn.close()
    return self.redirect("/admin/guru", "Guru diaktifkan kembali.")


# -- admin: kelas (pola daftar sederhana) ------------------------------------
def _simple_list(self, user, flash, table, title, action, label, aktif_key):
    conn = db()
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY nama").fetchall()
    conn.close()
    items = ""
    for r in rows:
        confirm_msg = f"Hapus {label.lower()} {js_str(r['nama'])}?"
        items += (
            f'<li><b>{esc(r["nama"])}</b> '
            f'&mdash; <a href="{action}/{r["id"]}/edit">Edit</a> '
            f'<form method="post" action="{action}/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
    items = items or '<li class="muted">Belum ada data.</li>'
    body = f"""<h2>{esc(title)}</h2>
    <div class="card"><ul class="plain-list">{items}</ul></div>
    <div class="card form-card"><h3>Tambah {esc(label)}</h3>
    <form method="post" action="{action}">
      <label>Nama baru <input name="nama" required></label>
      <button type="submit" class="btn btn-primary">Tambah</button></form></div>"""
    return self.send_html(layout(user, title, body, flash, aktif=aktif_key))


def _admin_kelas(self, user, flash):
    conn = db()
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM students s WHERE s.class_id=c.id) AS jml
           FROM classes c ORDER BY c.nama""").fetchall()
    conn.close()
    items = ""
    for r in rows:
        confirm_msg = f"Hapus kelas {js_str(r['nama'])}?"
        items += (
            f'<li><b><a href="/admin/kelas/{r["id"]}">{esc(r["nama"])}</a></b> '
            f'&mdash; {r["jml"]} siswa '
            f'&mdash; <a href="/admin/kelas/{r["id"]}/edit">Edit</a> '
            f'<form method="post" action="/admin/kelas/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
    items = items or '<li class="muted">Belum ada data.</li>'
    body = f"""<h2>Kelola Kelas</h2>
    <div class="card"><ul class="plain-list">{items}</ul></div>
    <div class="card form-card"><h3>Tambah Kelas</h3>
    <form method="post" action="/admin/kelas">
      <label>Nama baru <input name="nama" required></label>
      <button type="submit" class="btn btn-primary">Tambah</button></form></div>"""
    return self.send_html(layout(user, "Kelola Kelas", body, flash, aktif="kelas"))


def _post_simple(self, table, f, back):
    try:
        conn = db()
        conn.execute(f"INSERT INTO {table} (nama) VALUES (?)", (f["nama"].strip(),))
        conn.commit(); conn.close()
        return self.redirect(back, "Data ditambahkan.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect(back, "Nama sudah ada.")


def _post_admin_kelas(self, f):
    return self._post_simple("classes", f, "/admin/kelas")


def _simple_edit(self, user, table, action, label, item_id, flash, aktif_key):
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return self.redirect(action, "Data tidak ditemukan.")
    body = f"""<h2>Edit {esc(label)}</h2>
    <form method="post" action="{action}/{row['id']}/edit" class="card form-card">
      <label>Nama <input name="nama" value="{esc(row['nama'])}" required></label>
      <button type="submit" class="btn btn-primary">Simpan Perubahan</button></form>
    <p><a href="{action}">Kembali ke daftar {esc(label.lower())}</a></p>"""
    return self.send_html(layout(user, f"Edit {label}", body, flash, aktif=aktif_key))


def _admin_kelas_edit(self, user, kid, flash):
    return self._simple_edit(user, "classes", "/admin/kelas", "Kelas", kid, flash, "kelas")


def _post_simple_edit(self, table, action, label, item_id, f):
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return self.redirect(action, "Data tidak ditemukan.")
    try:
        conn.execute(f"UPDATE {table} SET nama=? WHERE id=?",
                     (f["nama"].strip(), item_id))
        conn.commit(); conn.close()
        return self.redirect(action, f"{label} diperbarui.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect(f"{action}/{item_id}/edit", "Nama sudah ada.")


def _post_admin_kelas_edit(self, kid, f):
    return self._post_simple_edit("classes", "/admin/kelas", "Kelas", kid, f)


def _post_admin_kelas_hapus(self, kid):
    conn = db()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    if not row:
        conn.close()
        return self.redirect("/admin/kelas", "Data tidak ditemukan.")
    # Hapus aman: kelas yang masih punya siswa tidak boleh dihapus, agar
    # data siswa & riwayat kehadirannya tidak rusak.
    jml = conn.execute("SELECT COUNT(*) c FROM students WHERE class_id=?",
                       (kid,)).fetchone()["c"]
    if jml:
        conn.close()
        return self.redirect(
            "/admin/kelas",
            "Kelas tidak bisa dihapus karena masih dipakai (ada siswa). "
            "Hapus atau pindahkan dulu siswa di kelas ini.")
    try:
        conn.execute("DELETE FROM classes WHERE id=?", (kid,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect(
            "/admin/kelas",
            "Kelas tidak bisa dihapus karena masih dipakai data lain.")
    conn.close()
    return self.redirect("/admin/kelas", "Kelas dihapus.")


def _admin_kelas_detail(self, user, kid, flash):
    conn = db()
    kelas = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    if not kelas:
        conn.close()
        return self.redirect("/admin/kelas", "Data tidak ditemukan.")
    rows = conn.execute(
        "SELECT * FROM students WHERE class_id=? ORDER BY nama", (kid,)).fetchall()
    conn.close()
    items = ""
    for r in rows:
        confirm_msg = (f"Hapus siswa {js_str(r['nama'])}? "
                       "Catatan kehadirannya juga akan terhapus.")
        items += (
            f'<li><b>{esc(r["nama"])}</b> &mdash; NIS: <code>{esc(r["kode"])}</code> '
            f'&mdash; <a href="/admin/siswa/{r["id"]}/edit">Edit</a> '
            f'<form method="post" action="/admin/siswa/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
    if items:
        daftar = f'<div class="card"><ol class="plain-list">{items}</ol></div>'
    else:
        daftar = ('<div class="card"><p class="muted">'
                  'Belum ada siswa di kelas ini.</p></div>')
    body = f"""<h2>Kelas {esc(kelas["nama"])} &mdash; {len(rows)} siswa</h2>
    {daftar}
    <div class="card form-card"><h3>Tambah Siswa</h3>
    <form method="post" action="/admin/siswa">
      <label>Nama <input name="nama" required></label>
      <label>NIS <input name="kode" required></label>
      <input type="hidden" name="class_id" value="{kelas['id']}">
      <button type="submit" class="btn btn-primary">Tambah Siswa</button></form></div>
    <p><a href="/admin/kelas">Kembali ke daftar kelas</a></p>"""
    return self.send_html(layout(user, f"Kelas {kelas['nama']}", body, flash,
                                 aktif="kelas"))


# -- admin: siswa --------------------------------------------------------------
def _admin_siswa(self, user, qs, flash):
    conn = db()
    classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
    class_opts = "".join(f'<option value="{c["id"]}">{esc(c["nama"])}</option>' for c in classes)
    rows = conn.execute(
        """SELECT s.*, c.nama kelas FROM students s JOIN classes c ON c.id=s.class_id
           ORDER BY c.nama, s.nama""").fetchall()
    conn.close()
    items = ""
    for i, r in enumerate(rows):
        confirm_msg = f"Hapus siswa {js_str(r['nama'])}? Catatan kehadirannya juga akan terhapus."
        items += (
            f'<li><span class="no">{i + 1}.</span> <b>{esc(r["nama"])}</b> '
            f'&mdash; NIS: <code>{esc(r["kode"])}</code> '
            f'&mdash; {esc(r["kelas"])} '
            f'&mdash; <a href="/admin/siswa/{r["id"]}/edit">Edit</a> '
            f'<form method="post" action="/admin/siswa/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn btn-danger btn-sm">Hapus</button></form></li>')
    items = items or '<li class="muted">Belum ada siswa.</li>'
    body = f"""<h2>Kelola Siswa</h2>
    <div class="card"><ul class="plain-list">{items}</ul></div>
    <div class="card form-card"><h3>Tambah Siswa</h3>
    <form method="post" action="/admin/siswa">
      <label>Nama <input name="nama" required></label>
      <label>NIS <input name="kode" required></label>
      <label>Kelas <select name="class_id">{class_opts}</select></label>
      <button type="submit" class="btn btn-primary">Tambah Siswa</button></form></div>"""
    return self.send_html(layout(user, "Siswa", body, flash, aktif="siswa"))


def _post_admin_siswa(self, f):
    try:
        conn = db()
        conn.execute("INSERT INTO students (nama,kode,class_id) VALUES (?,?,?)",
                     (f["nama"].strip(), f["kode"].strip(), int(f["class_id"])))
        conn.commit(); conn.close()
        return self.redirect("/admin/siswa", "Siswa ditambahkan.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect("/admin/siswa", "NIS sudah dipakai.")


def _admin_siswa_edit(self, user, sid, flash):
    conn = db()
    st = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not st:
        conn.close()
        return self.redirect("/admin/siswa", "Siswa tidak ditemukan.")
    classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
    conn.close()
    class_opts = "".join(
        f'<option value="{c["id"]}"{" selected" if c["id"] == st["class_id"] else ""}>'
        f'{esc(c["nama"])}</option>' for c in classes)
    body = f"""<h2>Edit Siswa</h2>
    <form method="post" action="/admin/siswa/{st['id']}/edit" class="card form-card">
      <label>Nama <input name="nama" value="{esc(st['nama'])}" required></label>
      <label>NIS <input name="kode" value="{esc(st['kode'])}" required></label>
      <label>Kelas <select name="class_id">{class_opts}</select></label>
      <button type="submit" class="btn btn-primary">Simpan Perubahan</button></form>
    <p><a href="/admin/siswa">Kembali ke daftar siswa</a></p>"""
    return self.send_html(layout(user, "Edit Siswa", body, flash, aktif="siswa"))


def _post_admin_siswa_edit(self, sid, f):
    conn = db()
    st = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not st:
        conn.close()
        return self.redirect("/admin/siswa", "Siswa tidak ditemukan.")
    try:
        conn.execute("UPDATE students SET nama=?, kode=?, class_id=? WHERE id=?",
                     (f["nama"].strip(), f["kode"].strip(), int(f["class_id"]), sid))
        conn.commit(); conn.close()
        return self.redirect("/admin/siswa", "Siswa diperbarui.")
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect(f"/admin/siswa/{sid}/edit", "NIS sudah dipakai.")


def _post_admin_siswa_hapus(self, sid):
    conn = db()
    st = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not st:
        conn.close()
        return self.redirect("/admin/siswa", "Siswa tidak ditemukan.")
    # Satu transaksi: hapus catatan kehadiran siswa dulu, baru siswanya.
    conn.execute("DELETE FROM kehadiran WHERE ref_type='siswa' AND ref_id=?", (sid,))
    conn.execute("DELETE FROM students WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return self.redirect("/admin/siswa", "Siswa dihapus.")


# -- admin: pengaturan tampilan --------------------------------------------------
def _admin_tampilan(self, user, flash):
    peng = get_pengaturan()
    swatches = ""
    for warna in BG_PRESETS:
        cek = " checked" if peng["bg_color"].lower() == warna.lower() else ""
        swatches += (f'<label class="swatch" title="{warna}">'
                     f'<input type="radio" name="bg_color" value="{warna}"{cek}>'
                     f'<span style="background:{warna}"></span></label>')
    cek_kustom = "" if peng["bg_color"].lower() in [w.lower() for w in BG_PRESETS] else " checked"
    wp_html = ""
    if peng["wallpaper"]:
        wp_html = f"""<div class="wp-preview"><img src="/wallpaper/{esc(peng['wallpaper'])}"
          alt="Wallpaper saat ini"></div>
        <form method="post" action="/admin/tampilan/wallpaper/hapus" class="inline"
          onsubmit="return confirm('Hapus wallpaper? Latar kembali hanya warna saja.')">
          <button type="submit" class="btn btn-danger">Hapus Wallpaper</button></form>"""
    logo_html = ""
    if peng["logo"]:
        logo_html = f"""<div class="logo-preview"><img src="/logo/{esc(peng['logo'])}"
          alt="Logo sekolah saat ini"></div>
        <form method="post" action="/admin/tampilan/logo/hapus" class="inline"
          onsubmit="return confirm('Hapus logo sekolah? Brand kembali ke ikon bawaan.')">
          <button type="submit" class="btn btn-danger">Hapus Logo</button></form>"""
    body = f"""<h2>Pengaturan Tampilan</h2>
    <div class="card form-card"><h3>Nama Sekolah &amp; Warna Latar</h3>
    <form method="post" action="/admin/tampilan">
      <label>Nama sekolah (tampil di menu &amp; judul Excel)
        <input name="nama_sekolah" value="{esc(peng['nama_sekolah'])}"
               placeholder="mis. SMA Negeri 1 Contoh"></label>
      <label>Warna latar aplikasi</label>
      <div class="swatches">{swatches}
        <label class="swatch kustom" title="Warna kustom">
          <input type="radio" name="bg_color" value="kustom"{cek_kustom}>
          <input type="color" name="bg_color_custom" value="{esc(peng['bg_color'])}">
        </label>
      </div>
      <p class="muted"><small>Pilih warna contoh, atau pilih radio terakhir lalu
      tentukan warna kustom dengan pemilih warna.</small></p>
      <button type="submit" class="btn btn-primary">Simpan Pengaturan</button></form></div>
    <div class="card form-card"><h3>Wallpaper Sekolah</h3>
    <p class="muted">Gambar menjadi latar seluruh aplikasi (termasuk halaman login),
    di balik lapisan terang agar teks tetap terbaca. Format JPG/PNG/WebP, maks &plusmn;5 MB.
    Kosongkan untuk latar warna saja.</p>
    {wp_html}
    <form method="post" action="/admin/tampilan/wallpaper" enctype="multipart/form-data">
      <label>Unggah / ganti wallpaper <input type="file" name="wallpaper"
        accept=".jpg,.jpeg,.png,.webp" required></label>
      <button type="submit" class="btn btn-primary">Unggah Wallpaper</button></form></div>
    <div class="card form-card"><h3>Logo Sekolah</h3>
    <p class="muted">Logo tampil sebagai tanda brand di menu samping, bilah atas,
    dan halaman login &mdash; menggantikan ikon bawaan. Nama sekolah di sebelahnya
    tidak berubah. Format JPG/PNG/WebP, maks &plusmn;5 MB. Pakai gambar persegi
    agar tampil rapi.</p>
    {logo_html}
    <form method="post" action="/admin/tampilan/logo" enctype="multipart/form-data">
      <label>Unggah / ganti logo <input type="file" name="logo"
        accept=".jpg,.jpeg,.png,.webp" required></label>
      <button type="submit" class="btn btn-primary">Unggah Logo</button></form></div>"""
    return self.send_html(layout(user, "Pengaturan Tampilan", body, flash, aktif="tampilan"))


def _post_admin_tampilan(self, f):
    warna = (f.get("bg_color") or "").strip()
    if warna == "kustom":
        warna = (f.get("bg_color_custom") or "").strip()
    if not re.match(r"^#[0-9a-fA-F]{6}$", warna):
        warna = BG_DEFAULT
    conn = db()
    set_pengaturan(conn, "bg_color", warna.lower())
    set_pengaturan(conn, "nama_sekolah", (f.get("nama_sekolah") or "").strip())
    conn.commit(); conn.close()
    return self.redirect("/admin/tampilan", "Pengaturan tampilan disimpan.")


def _post_admin_tampilan_wallpaper(self):
    length = int(self.headers.get("Content-Length", "0") or "0")
    if length > 6 * 1024 * 1024:
        return self.redirect("/admin/tampilan", "Berkas terlalu besar (maks \u00b15 MB).")
    body = self.rfile.read(length) if length else b""
    _fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
    berkas = files.get("wallpaper")
    if not berkas or not berkas[0]:
        return self.redirect("/admin/tampilan", "Pilih berkas gambar dulu.")
    filename, data = berkas
    ext = os.path.splitext(filename)[1].lower()
    if ext not in WALLPAPER_EXT:
        return self.redirect("/admin/tampilan",
                             "Format tidak didukung. Pakai JPG, PNG, atau WebP.")
    if len(data) > 5 * 1024 * 1024:
        return self.redirect("/admin/tampilan", "Berkas terlalu besar (maks \u00b15 MB).")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    lama = get_pengaturan()["wallpaper"]
    nama_baru = secrets.token_hex(12) + ext
    with open(os.path.join(UPLOAD_DIR, nama_baru), "wb") as fh:
        fh.write(data)
    conn = db()
    set_pengaturan(conn, "wallpaper", nama_baru)
    conn.commit(); conn.close()
    if lama and lama != nama_baru:
        try:
            os.remove(os.path.join(UPLOAD_DIR, lama))
        except OSError:
            pass
    return self.redirect("/admin/tampilan", "Wallpaper dipasang.")


def _post_admin_tampilan_wallpaper_hapus(self):
    lama = get_pengaturan()["wallpaper"]
    conn = db()
    set_pengaturan(conn, "wallpaper", "")
    conn.commit(); conn.close()
    if lama:
        try:
            os.remove(os.path.join(UPLOAD_DIR, lama))
        except OSError:
            pass
    return self.redirect("/admin/tampilan", "Wallpaper dihapus. Latar kembali ke warna saja.")


def _wallpaper_file(self, nama):
    if not WALLPAPER_NAMA_RE.match(nama or ""):
        return self.send_html("Tidak ditemukan.", status=404)
    path = os.path.join(UPLOAD_DIR, nama)
    if not os.path.isfile(path):
        return self.send_html("Tidak ditemukan.", status=404)
    ext = os.path.splitext(nama)[1].lower()
    with open(path, "rb") as fh:
        data = fh.read()
    return self.send_bytes(data, WALLPAPER_EXT.get(ext, "application/octet-stream"))


def _post_admin_tampilan_logo(self):
    length = int(self.headers.get("Content-Length", "0") or "0")
    if length > 6 * 1024 * 1024:
        return self.redirect("/admin/tampilan", "Berkas terlalu besar (maks \u00b15 MB).")
    body = self.rfile.read(length) if length else b""
    _fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
    berkas = files.get("logo")
    if not berkas or not berkas[0]:
        return self.redirect("/admin/tampilan", "Pilih berkas gambar dulu.")
    filename, data = berkas
    ext = os.path.splitext(filename)[1].lower()
    if ext not in WALLPAPER_EXT:
        return self.redirect("/admin/tampilan",
                             "Format tidak didukung. Pakai JPG, PNG, atau WebP.")
    if len(data) > 5 * 1024 * 1024:
        return self.redirect("/admin/tampilan", "Berkas terlalu besar (maks \u00b15 MB).")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    lama = get_pengaturan()["logo"]
    nama_baru = secrets.token_hex(12) + ext
    with open(os.path.join(UPLOAD_DIR, nama_baru), "wb") as fh:
        fh.write(data)
    conn = db()
    set_pengaturan(conn, "logo", nama_baru)
    conn.commit(); conn.close()
    if lama and lama != nama_baru:
        try:
            os.remove(os.path.join(UPLOAD_DIR, lama))
        except OSError:
            pass
    return self.redirect("/admin/tampilan", "Logo dipasang.")


def _post_admin_tampilan_logo_hapus(self):
    lama = get_pengaturan()["logo"]
    conn = db()
    set_pengaturan(conn, "logo", "")
    conn.commit(); conn.close()
    if lama:
        try:
            os.remove(os.path.join(UPLOAD_DIR, lama))
        except OSError:
            pass
    return self.redirect("/admin/tampilan", "Logo dihapus. Brand kembali ke ikon bawaan.")


def _logo_file(self, nama):
    # Logo & wallpaper sama-sama berkas unggahan bernama-token di data/uploads/,
    # jadi penyajiannya identik dengan wallpaper.
    return _wallpaper_file(self, nama)


# -- admin: backup & pulihkan --------------------------------------------------
BACKUP_APP = "absen-multi-guru"
BACKUP_MAKS = 50 * 1024 * 1024  # batas ukuran berkas backup yang diunggah


def _admin_backup(self, user, flash):
    body = f"""<h2>Backup &amp; Pulihkan</h2>
    <div class="card form-card"><h3>Unduh Backup</h3>
    <p class="muted">Unduh satu berkas ZIP berisi salinan seluruh database aplikasi
    (data guru, kelas, siswa, kehadiran, dan pengaturan) beserta gambar wallpaper.
    Simpan berkasnya di tempat yang aman. Biasakan membuat backup secara berkala,
    terutama sebelum memasang pembaruan aplikasi &mdash; jadi kalau terjadi apa-apa,
    data bisa dikembalikan dari backup, tidak perlu mengisi ulang dari awal.</p>
    <p><a class="btn btn-primary" href="/admin/backup/unduh">{icon('download')}
    Unduh Backup (.zip)</a></p></div>
    <div class="card form-card"><h3>Pulihkan dari Backup</h3>
    <p class="flash warn"><b>PERINGATAN:</b> memulihkan backup akan MENGGANTI
    SELURUH data yang ada sekarang dengan isi backup tersebut. Data apa pun yang
    masuk setelah backup dibuat akan hilang. Unduh backup terbaru dulu sebelum
    memulihkan backup yang lama.</p>
    <form method="post" action="/admin/backup/pulihkan" enctype="multipart/form-data"
      onsubmit="return confirm('Pulihkan backup ini? SELURUH data sekarang akan diganti dengan isi backup.')">
      <label>Berkas backup (.zip) <input type="file" name="berkas" accept=".zip" required></label>
      <button type="submit" class="btn btn-danger">Pulihkan Backup</button></form>
    <p class="muted"><small>Setelah pemulihan berhasil, semua pengguna yang sedang
    masuk akan dikeluarkan dan Anda diminta masuk lagi.</small></p></div>"""
    return self.send_html(layout(user, "Backup & Pulihkan", body, flash, aktif="backup"))


def _admin_backup_unduh(self):
    # Salinan database yang konsisten lewat sqlite3 backup API (bukan menyalin
    # berkas mentah yang mungkin sedang ditulis).
    fd, tmp_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = db()
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "absen.db")
            if os.path.isdir(UPLOAD_DIR):
                for nama in sorted(os.listdir(UPLOAD_DIR)):
                    p = os.path.join(UPLOAD_DIR, nama)
                    if os.path.isfile(p):
                        z.write(p, f"uploads/{nama}")
            manifest = {"app": BACKUP_APP,
                        "dibuat": waktu_sekarang().isoformat(timespec="seconds"),
                        "versi": 1}
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        data = buf.getvalue()
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass
    nama_file = f"backup-absen-{waktu_sekarang().strftime('%Y-%m-%d')}.zip"
    return self.send_bytes(data, "application/zip", filename=nama_file)


def _post_admin_backup_pulihkan(self):
    def gagal(pesan):
        return self.redirect("/admin/backup", pesan)

    length = int(self.headers.get("Content-Length", "0") or "0")
    if length > BACKUP_MAKS:
        return gagal("Berkas terlalu besar untuk berkas backup.")
    body = self.rfile.read(length) if length else b""
    _fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
    berkas = files.get("berkas")
    if not berkas or not berkas[0]:
        return gagal("Pilih berkas backup dulu.")
    _nama, data = berkas
    bio = io.BytesIO(data)
    if not zipfile.is_zipfile(bio):
        return gagal("Berkas bukan file ZIP backup yang valid.")
    bio.seek(0)
    try:
        z = zipfile.ZipFile(bio)
        names = z.namelist()
        # --- validasi: manifest harus milik aplikasi absen ini ---
        if "manifest.json" not in names:
            return gagal("Berkas ini bukan backup aplikasi absen.")
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except Exception:
            return gagal("Berkas ini bukan backup aplikasi absen.")
        if not isinstance(manifest, dict) or manifest.get("app") != BACKUP_APP:
            return gagal("Berkas ini bukan backup aplikasi absen.")
        db_entries = [n for n in names if n.endswith(".db") and "/" not in n]
        if not db_entries:
            return gagal("Backup tidak berisi file database.")
        db_bytes = z.read(db_entries[0])
        uploads_baru = {}
        for n in names:
            if n.startswith("uploads/") and not n.endswith("/"):
                dasar = os.path.basename(n[len("uploads/"):])
                if dasar:
                    uploads_baru[dasar] = z.read(n)
    except zipfile.BadZipFile:
        return gagal("Berkas bukan file ZIP backup yang valid.")
    # --- validasi isi database: harus SQLite dengan tabel aplikasi ---
    fd, tmp_db = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(db_bytes)
        try:
            cek = sqlite3.connect(tmp_db)
            ada_users = cek.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            cek.close()
        except sqlite3.Error:
            ada_users = None
        if not ada_users:
            return gagal("File database di dalam backup tidak valid.")
        # --- semua valid: baru sekarang data diganti ---
        # 1) salinan aman database saat ini, di samping file DB aktif
        aman = os.path.join(os.path.dirname(DB_PATH) or ".",
                            "absen-sebelum-pulih.db")
        if os.path.isfile(DB_PATH):
            shutil.copyfile(DB_PATH, aman)
        # 2) ganti database secara atomik
        tmp_ganti = DB_PATH + ".tmp-pulih"
        with open(tmp_ganti, "wb") as fh:
            fh.write(db_bytes)
        os.replace(tmp_ganti, DB_PATH)
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass
    # 3) ganti isi folder uploads dengan isi backup
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for nama in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, nama)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    for nama, isi in uploads_baru.items():
        with open(os.path.join(UPLOAD_DIR, nama), "wb") as fh:
            fh.write(isi)
    # 4) keluarkan semua sesi login (lebih aman), minta masuk lagi
    LOGIN_SESSIONS.clear()
    return self.redirect("/login", "Backup dipulihkan. Silakan masuk lagi.",
                         set_cookie="absen_session=; Max-Age=0; Path=/")


# Tempelkan handler-handler di atas sebagai method Handler.
Handler.cari = _cari
Handler.admin_guru = _admin_guru
Handler.post_admin_guru = _post_admin_guru
Handler._get_guru = _get_guru
Handler.admin_guru_edit = _admin_guru_edit
Handler.post_admin_guru_edit = _post_admin_guru_edit
Handler.post_admin_guru_hapus = _post_admin_guru_hapus
Handler.post_admin_guru_aktifkan = _post_admin_guru_aktifkan
Handler._simple_list = _simple_list
Handler.admin_kelas = _admin_kelas
Handler._post_simple = _post_simple
Handler.post_admin_kelas = _post_admin_kelas
Handler._simple_edit = _simple_edit
Handler.admin_kelas_edit = _admin_kelas_edit
Handler._post_simple_edit = _post_simple_edit
Handler.post_admin_kelas_edit = _post_admin_kelas_edit
Handler.post_admin_kelas_hapus = _post_admin_kelas_hapus
Handler.admin_kelas_detail = _admin_kelas_detail
Handler.admin_siswa = _admin_siswa
Handler.post_admin_siswa = _post_admin_siswa
Handler.admin_siswa_edit = _admin_siswa_edit
Handler.post_admin_siswa_edit = _post_admin_siswa_edit
Handler.post_admin_siswa_hapus = _post_admin_siswa_hapus
Handler.admin_tampilan = _admin_tampilan
Handler.post_admin_tampilan = _post_admin_tampilan
Handler.post_admin_tampilan_wallpaper = _post_admin_tampilan_wallpaper
Handler.post_admin_tampilan_wallpaper_hapus = _post_admin_tampilan_wallpaper_hapus
Handler.wallpaper_file = _wallpaper_file
Handler.post_admin_tampilan_logo = _post_admin_tampilan_logo
Handler.post_admin_tampilan_logo_hapus = _post_admin_tampilan_logo_hapus
Handler.logo_file = _logo_file
Handler.admin_backup = _admin_backup
Handler.admin_backup_unduh = _admin_backup_unduh
Handler.post_admin_backup_pulihkan = _post_admin_backup_pulihkan


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Aplikasi Absen Siswa (Gerbang Global) jalan di http://127.0.0.1:{PORT}  (DB: {DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBerhenti.")


if __name__ == "__main__":
    main()
