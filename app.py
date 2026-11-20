#!/usr/bin/env python3
"""
Aplikasi Absen Siswa Multi-Guru
Stack: Python 3 stdlib (http.server + sqlite3) + library `qrcode`/`Pillow`
untuk membuat gambar QR kartu siswa. Tanpa framework web eksternal.

Jalankan:  python3 app.py            (default http://127.0.0.1:8000)
           PORT=8080 python3 app.py
"""
import hashlib
import html
import io
import os
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ABSEN_DB", os.path.join(BASE_DIR, "data", "absen.db"))
PORT = int(os.environ.get("PORT", "8000"))

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
    aktif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT NOT NULL,
    kode TEXT UNIQUE NOT NULL,          -- kode unik siswa (boleh = NIS)
    class_id INTEGER NOT NULL REFERENCES classes(id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    UNIQUE (user_id, class_id, subject_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    teacher_id INTEGER NOT NULL REFERENCES users(id),
    tanggal TEXT NOT NULL,              -- YYYY-MM-DD
    jam_mulai TEXT NOT NULL,            -- HH:MM
    batas_terlambat_menit INTEGER NOT NULL DEFAULT 15,
    status TEXT NOT NULL DEFAULT 'buka' CHECK (status IN ('buka','tutup'))
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    status TEXT NOT NULL CHECK (status IN ('Hadir','Terlambat','Sakit','Izin','Alpa')),
    metode TEXT NOT NULL DEFAULT 'manual',   -- scan / manual / demo / otomatis
    waktu TEXT,                              -- ISO datetime saat dicatat
    recorded_by INTEGER REFERENCES users(id),
    UNIQUE (session_id, student_id)
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
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


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
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


# ---------------------------------------------------------------------------
# Session login (server-side, cookie hanya menyimpan token acak)
# ---------------------------------------------------------------------------
LOGIN_SESSIONS = {}  # token -> user_id


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


# ---------------------------------------------------------------------------
# HTML layout
# ---------------------------------------------------------------------------
def layout(user, title, body, flash=""):
    nav = ""
    if user:
        if user["role"] == "admin":
            nav = ('<a href="/dashboard">Dashboard</a><a href="/admin/guru">Guru</a>'
                   '<a href="/admin/kelas">Kelas</a><a href="/admin/mapel">Mapel</a>'
                   '<a href="/admin/siswa">Siswa</a><a href="/admin/penugasan">Penugasan</a>'
                   '<a href="/rekap">Rekap</a>')
        else:
            nav = ('<a href="/dashboard">Dashboard</a><a href="/guru/sesi">Sesi Saya</a>'
                   '<a href="/rekap">Rekap</a>')
        nav += '<a href="/ganti-password">Ganti Password</a><a href="/logout">Keluar</a>'
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    user_html = (f'<div class="userbar">{esc(user["nama"])} '
                 f'({esc(user["role"])})</div>') if user else ""
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - Absen Siswa</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><header><h1>Aplikasi Absen Siswa</h1>{user_html}<nav>{nav}</nav></header>
<main>{flash_html}{body}</main>
<footer><small>Absen per mata pelajaran &bull; scan QR Code biasa (bukan QRIS pembayaran)</small></footer>
</body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AbsenMultiGuru/1.0"

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
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/qr/") and path.endswith(".png"):
            return self.qr_png(path[4:-4])
        if path == "/":
            return self.redirect("/dashboard")
        if path == "/login":
            body = """<h2>Masuk</h2>
            <form method="post" action="/login">
              <label>Username <input name="username" required autocomplete="username"></label>
              <label>Password <input name="password" type="password" required autocomplete="current-password"></label>
              <button type="submit">Masuk</button></form>
            <p><small>Akun uji lokal ada di README proyek. Jangan pakai akun awal di produksi.</small></p>"""
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
        if path == "/ganti-password":
            body = """<h2>Ganti Password</h2>
            <form method="post" action="/ganti-password">
              <label>Password baru <input name="password" type="password" required minlength="8"></label>
              <button type="submit">Simpan</button></form>"""
            return self.send_html(layout(user, "Ganti Password", body, flash))
        if path == "/rekap":
            return self.rekap(user, flash)
        if path.startswith("/rekap/") and path.endswith("/csv"):
            return self.rekap_csv(user, int(path.split("/")[2]))
        if path.startswith("/cetak/"):
            return self.cetak(user, int(path.split("/")[2]), flash)
        if user["role"] == "admin":
            if path == "/admin/guru": return self.admin_guru(user, flash)
            if path.startswith("/admin/guru/") and path.endswith("/edit"):
                return self.admin_guru_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/kelas": return self.admin_kelas(user, flash)
            if path.startswith("/admin/kelas/") and path.endswith("/edit"):
                return self.admin_kelas_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/mapel": return self.admin_mapel(user, flash)
            if path.startswith("/admin/mapel/") and path.endswith("/edit"):
                return self.admin_mapel_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/siswa": return self.admin_siswa(user, qs, flash)
            if path.startswith("/admin/siswa/") and path.endswith("/edit"):
                return self.admin_siswa_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/penugasan": return self.admin_penugasan(user, flash)
            if path.startswith("/admin/penugasan/") and path.endswith("/edit"):
                return self.admin_penugasan_edit(user, int(path.split("/")[3]), flash)
        if user["role"] == "guru":
            if path == "/guru/sesi": return self.guru_sesi(user, flash)
            if path.startswith("/sesi/"): return self.sesi_detail(user, int(path.split("/")[2]), flash)
        return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"), status=404)

    # -- POST routes --------------------------------------------------------
    def route_post(self, path):
        if path == "/login":
            return self.post_login()
        user = self.current_user()
        if not user:
            return self.redirect("/login", "Silakan masuk dulu.")
        f = self.form()
        if path == "/ganti-password":
            ph, salt = hash_password(f["password"])
            conn = db()
            conn.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                         (ph, salt, user["id"]))
            conn.commit(); conn.close()
            return self.redirect("/dashboard", "Password berhasil diganti.")
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
            if path == "/admin/mapel": return self.post_admin_mapel(f)
            if path.startswith("/admin/mapel/") and path.endswith("/edit"):
                return self.post_admin_mapel_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/mapel/") and path.endswith("/hapus"):
                return self.post_admin_mapel_hapus(int(path.split("/")[3]))
            if path == "/admin/siswa": return self.post_admin_siswa(f)
            if path.startswith("/admin/siswa/") and path.endswith("/edit"):
                return self.post_admin_siswa_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/siswa/") and path.endswith("/hapus"):
                return self.post_admin_siswa_hapus(int(path.split("/")[3]))
            if path == "/admin/penugasan": return self.post_admin_penugasan(f)
            if path.startswith("/admin/penugasan/") and path.endswith("/edit"):
                return self.post_admin_penugasan_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/penugasan/") and path.endswith("/hapus"):
                return self.post_admin_penugasan_hapus(int(path.split("/")[3]))
        if user["role"] == "guru":
            if path == "/guru/sesi/buat": return self.post_buat_sesi(user, f)
            if path.startswith("/sesi/"):
                sid = int(path.split("/")[2])
                if path.endswith("/scan"): return self.post_scan(user, sid, f)
                if path.endswith("/manual"): return self.post_manual(user, sid, f)
                if path.endswith("/tutup"): return self.post_tutup(user, sid)
        return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"), status=404)

    # -- auth ---------------------------------------------------------------
    def post_login(self):
        f = self.form()
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           (f.get("username", "").strip(),)).fetchone()
        conn.close()
        if row and not row["aktif"]:
            # Akun nonaktif tidak bisa masuk (query utama di bawah memfilter aktif=1);
            # beri pesan yang jelas bila password-nya memang benar.
            if verify_password(f.get("password", ""), row["password_hash"], row["salt"]):
                return self.redirect("/login", "Akun ini sudah dinonaktifkan.")
            return self.redirect("/login", "Username atau password salah.")
        if row and verify_password(f.get("password", ""), row["password_hash"], row["salt"]):
            token = secrets.token_urlsafe(32)
            LOGIN_SESSIONS[token] = row["id"]
            return self.redirect("/dashboard", f"Selamat datang, {row['nama']}.",
                                 set_cookie=f"absen_session={token}; HttpOnly; Path=/; SameSite=Lax")
        return self.redirect("/login", "Username atau password salah.")

    def dashboard(self, user, flash):
        conn = db()
        if user["role"] == "admin":
            stats = {}
            for key, q in [("Guru", "SELECT COUNT(*) c FROM users WHERE role='guru'"),
                           ("Kelas", "SELECT COUNT(*) c FROM classes"),
                           ("Mapel", "SELECT COUNT(*) c FROM subjects"),
                           ("Siswa", "SELECT COUNT(*) c FROM students"),
                           ("Sesi", "SELECT COUNT(*) c FROM sessions")]:
                stats[key] = conn.execute(q).fetchone()["c"]
            cards = "".join(f'<div class="card"><b>{v}</b><span>{esc(k)}</span></div>'
                            for k, v in stats.items())
            warn = ""
            if verify_password("admin123", user["password_hash"], user["salt"]):
                warn = ('<div class="flash warn">PERINGATAN: akun admin ini masih memakai '
                        'password awal lokal. Segera ganti lewat menu Ganti Password '
                        'sebelum dipakai beneran.</div>')
            body = f"<h2>Dashboard Admin</h2>{warn}<div class='grid'>{cards}</div>"
        else:
            rows = conn.execute(
                """SELECT s.*, c.nama kelas, sub.nama mapel FROM sessions s
                   JOIN classes c ON c.id=s.class_id JOIN subjects sub ON sub.id=s.subject_id
                   WHERE s.teacher_id=? ORDER BY s.id DESC LIMIT 10""", (user["id"],)).fetchall()
            items = "".join(
                f'<li><a href="/sesi/{r["id"]}">{esc(r["tanggal"])} &bull; {esc(r["kelas"])} &bull; '
                f'{esc(r["mapel"])} &bull; {esc(r["status"])}</a></li>' for r in rows) or "<li>Belum ada sesi.</li>"
            body = f"<h2>Dashboard Guru</h2><p>Sesi terakhir Anda:</p><ul>{items}</ul>"
        conn.close()
        return self.send_html(layout(user, "Dashboard", body, flash))


    # -- admin: guru --------------------------------------------------------
    def admin_guru(self, user, flash):
        conn = db()
        rows = conn.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.teacher_id=u.id) AS jml_sesi,
                      (SELECT COUNT(*) FROM attendance a WHERE a.recorded_by=u.id) AS jml_catat
               FROM users u WHERE u.role='guru' ORDER BY u.nama""").fetchall()
        conn.close()
        items = ""
        for r in rows:
            nama_js = r["nama"].replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
            dasar = (f'<li>{esc(r["nama"])} &mdash; username: <code>{esc(r["username"])}</code> '
                     f'&mdash; <a href="/admin/guru/{r["id"]}/edit">Edit</a> ')
            if not r["aktif"]:
                items += (dasar + '(nonaktif) '
                          f'<form method="post" action="/admin/guru/{r["id"]}/aktifkan" class="inline">'
                          f'<button type="submit">Aktifkan</button></form></li>')
            elif r["jml_sesi"] or r["jml_catat"]:
                # Punya riwayat absen: tombol Hapus akan menonaktifkan, bukan menghapus.
                confirm_msg = (f"Nonaktifkan guru {nama_js}? Guru ini sudah punya riwayat sesi absen, "
                               f"jadi akunnya akan dinonaktifkan (bukan dihapus) dan riwayat "
                               f"absennya tetap tersimpan.")
                items += (dasar +
                          f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                          f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                          f'<button type="submit">Hapus</button></form></li>')
            else:
                confirm_msg = f"Hapus guru {nama_js}? Akun guru ini akan dihapus permanen."
                items += (dasar +
                          f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                          f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                          f'<button type="submit">Hapus</button></form></li>')
        items = items or "<li>Belum ada guru.</li>"
        body = f"""<h2>Kelola Guru</h2><ul>{items}</ul>
        <h3>Tambah Guru</h3>
        <form method="post" action="/admin/guru">
          <label>Nama <input name="nama" required></label>
          <label>Username <input name="username" required></label>
          <label>Password awal <input name="password" type="password" required minlength="8"></label>
          <button type="submit">Tambah Guru</button></form>"""
        return self.send_html(layout(user, "Guru", body, flash))

    def post_admin_guru(self, f):
        try:
            ph, salt = hash_password(f["password"])
            conn = db()
            conn.execute("INSERT INTO users (username,password_hash,salt,role,nama) VALUES (?,?,?,'guru',?)",
                         (f["username"].strip(), ph, salt, f["nama"].strip()))
            conn.commit(); conn.close()
            return self.redirect("/admin/guru", "Guru ditambahkan.")
        except sqlite3.IntegrityError:
            return self.redirect("/admin/guru", "Username sudah dipakai.")

    def _get_guru(self, conn, gid):
        return conn.execute("SELECT * FROM users WHERE id=? AND role='guru'", (gid,)).fetchone()

    def admin_guru_edit(self, user, gid, flash):
        conn = db()
        g = self._get_guru(conn, gid)
        conn.close()
        if not g:
            return self.redirect("/admin/guru", "Guru tidak ditemukan.")
        body = f"""<h2>Edit Guru</h2>
        <form method="post" action="/admin/guru/{g['id']}/edit">
          <label>Nama <input name="nama" value="{esc(g['nama'])}" required></label>
          <label>Username <input name="username" value="{esc(g['username'])}" required></label>
          <label>Password baru (kosongkan jika tidak diganti)
            <input name="password" type="password" minlength="8"></label>
          <button type="submit">Simpan Perubahan</button></form>
        <p><a href="/admin/guru">Kembali ke daftar guru</a></p>"""
        return self.send_html(layout(user, "Edit Guru", body, flash))

    def post_admin_guru_edit(self, gid, f):
        conn = db()
        g = self._get_guru(conn, gid)
        if not g:
            conn.close()
            return self.redirect("/admin/guru", "Guru tidak ditemukan.")
        try:
            pw = f.get("password", "")
            if pw:
                ph, salt = hash_password(pw)
                conn.execute("UPDATE users SET nama=?, username=?, password_hash=?, salt=? WHERE id=?",
                             (f["nama"].strip(), f["username"].strip(), ph, salt, gid))
            else:
                conn.execute("UPDATE users SET nama=?, username=? WHERE id=?",
                             (f["nama"].strip(), f["username"].strip(), gid))
            conn.commit(); conn.close()
            return self.redirect("/admin/guru", "Guru diperbarui.")
        except sqlite3.IntegrityError:
            conn.close()
            return self.redirect(f"/admin/guru/{gid}/edit", "Username sudah dipakai.")

    def post_admin_guru_hapus(self, gid):
        conn = db()
        g = self._get_guru(conn, gid)
        if not g:
            conn.close()
            return self.redirect("/admin/guru", "Guru tidak ditemukan.")
        jml_sesi = conn.execute("SELECT COUNT(*) c FROM sessions WHERE teacher_id=?",
                                (gid,)).fetchone()["c"]
        jml_catat = conn.execute("SELECT COUNT(*) c FROM attendance WHERE recorded_by=?",
                                 (gid,)).fetchone()["c"]
        if jml_sesi or jml_catat:
            # Sudah punya riwayat absen: jangan hapus, cukup nonaktifkan agar
            # sesi dan catatan absennya tetap tersimpan.
            conn.execute("UPDATE users SET aktif=0 WHERE id=?", (gid,))
            conn.commit(); conn.close()
            return self.redirect("/admin/guru",
                                 "Guru dinonaktifkan karena sudah punya riwayat sesi absen. "
                                 "Riwayat absennya tetap tersimpan.")
        # Belum punya riwayat: hapus permanen (penugasan dulu, baru akunnya, satu transaksi).
        conn.execute("DELETE FROM assignments WHERE user_id=?", (gid,))
        conn.execute("DELETE FROM users WHERE id=?", (gid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru", "Guru dihapus.")

    def post_admin_guru_aktifkan(self, gid):
        conn = db()
        g = self._get_guru(conn, gid)
        if not g:
            conn.close()
            return self.redirect("/admin/guru", "Guru tidak ditemukan.")
        conn.execute("UPDATE users SET aktif=1 WHERE id=?", (gid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru", "Guru diaktifkan kembali.")

    # -- admin: kelas & mapel (pola sama) -----------------------------------
    def _simple_list(self, user, flash, table, title, action, label):
        conn = db()
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY nama").fetchall()
        conn.close()
        items = ""
        for r in rows:
            nama_js = r["nama"].replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
            confirm_msg = f"Hapus {label.lower()} {nama_js}?"
            items += (
                f'<li>{esc(r["nama"])} '
                f'&mdash; <a href="{action}/{r["id"]}/edit">Edit</a> '
                f'<form method="post" action="{action}/{r["id"]}/hapus" class="inline" '
                f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                f'<button type="submit">Hapus</button></form></li>')
        items = items or "<li>Belum ada data.</li>"
        body = f"""<h2>{esc(title)}</h2><ul>{items}</ul>
        <form method="post" action="{action}">
          <label>Nama baru <input name="nama" required></label>
          <button type="submit">Tambah</button></form>"""
        return self.send_html(layout(user, title, body, flash))

    def admin_kelas(self, user, flash):
        return self._simple_list(user, flash, "classes", "Kelola Kelas", "/admin/kelas", "Kelas")

    def admin_mapel(self, user, flash):
        return self._simple_list(user, flash, "subjects", "Kelola Mata Pelajaran", "/admin/mapel", "Mapel")

    def _post_simple(self, table, f, back):
        try:
            conn = db()
            conn.execute(f"INSERT INTO {table} (nama) VALUES (?)", (f["nama"].strip(),))
            conn.commit(); conn.close()
            return self.redirect(back, "Data ditambahkan.")
        except sqlite3.IntegrityError:
            return self.redirect(back, "Nama sudah ada.")

    def post_admin_kelas(self, f):
        return self._post_simple("classes", f, "/admin/kelas")

    def post_admin_mapel(self, f):
        return self._post_simple("subjects", f, "/admin/mapel")

    def _simple_edit(self, user, table, action, label, item_id, flash):
        conn = db()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if not row:
            return self.redirect(action, "Data tidak ditemukan.")
        body = f"""<h2>Edit {esc(label)}</h2>
        <form method="post" action="{action}/{row['id']}/edit">
          <label>Nama <input name="nama" value="{esc(row['nama'])}" required></label>
          <button type="submit">Simpan Perubahan</button></form>
        <p><a href="{action}">Kembali ke daftar {esc(label.lower())}</a></p>"""
        return self.send_html(layout(user, f"Edit {label}", body, flash))

    def admin_kelas_edit(self, user, kid, flash):
        return self._simple_edit(user, "classes", "/admin/kelas", "Kelas", kid, flash)

    def admin_mapel_edit(self, user, mid, flash):
        return self._simple_edit(user, "subjects", "/admin/mapel", "Mapel", mid, flash)

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

    def post_admin_kelas_edit(self, kid, f):
        return self._post_simple_edit("classes", "/admin/kelas", "Kelas", kid, f)

    def post_admin_mapel_edit(self, mid, f):
        return self._post_simple_edit("subjects", "/admin/mapel", "Mapel", mid, f)

    def _post_simple_hapus(self, table, action, label, item_id, refs):
        conn = db()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row:
            conn.close()
            return self.redirect(action, "Data tidak ditemukan.")
        # Hapus aman: tolak bila data ini masih dipakai tabel lain, agar riwayat
        # absen (sesi) dan relasi data tidak rusak.
        dipakai = []
        for ref_table, ref_col, sebutan in refs:
            jml = conn.execute(f"SELECT COUNT(*) c FROM {ref_table} WHERE {ref_col}=?",
                               (item_id,)).fetchone()["c"]
            if jml:
                dipakai.append(sebutan)
        if dipakai:
            conn.close()
            return self.redirect(
                action,
                f"{label} tidak bisa dihapus karena masih dipakai "
                f"(ada {'/'.join(dipakai)}). Hapus atau pindahkan dulu data "
                f"yang memakai {label.lower()} ini.")
        conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        conn.commit(); conn.close()
        return self.redirect(action, f"{label} dihapus.")

    def post_admin_kelas_hapus(self, kid):
        return self._post_simple_hapus(
            "classes", "/admin/kelas", "Kelas", kid,
            [("students", "class_id", "siswa"), ("sessions", "class_id", "sesi"),
             ("assignments", "class_id", "penugasan")])

    def post_admin_mapel_hapus(self, mid):
        return self._post_simple_hapus(
            "subjects", "/admin/mapel", "Mapel", mid,
            [("sessions", "subject_id", "sesi"), ("assignments", "subject_id", "penugasan")])

    # -- admin: siswa --------------------------------------------------------
    def admin_siswa(self, user, qs, flash):
        conn = db()
        classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        class_opts = "".join(f'<option value="{c["id"]}">{esc(c["nama"])}</option>' for c in classes)
        rows = conn.execute(
            """SELECT s.*, c.nama kelas FROM students s JOIN classes c ON c.id=s.class_id
               ORDER BY c.nama, s.nama""").fetchall()
        conn.close()
        items = ""
        for r in rows:
            nama_js = r["nama"].replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
            confirm_msg = f"Hapus siswa {nama_js}? Catatan absennya juga akan terhapus."
            items += (
                f'<li>{esc(r["nama"])} &mdash; kode: <code>{esc(r["kode"])}</code> &mdash; {esc(r["kelas"])} '
                f'&mdash; <a href="/cetak/{r["class_id"]}">Cetak kartu kelas ini</a> '
                f'&mdash; <a href="/admin/siswa/{r["id"]}/edit">Edit</a> '
                f'<form method="post" action="/admin/siswa/{r["id"]}/hapus" class="inline" '
                f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                f'<button type="submit">Hapus</button></form></li>')
        items = items or "<li>Belum ada siswa.</li>"
        body = f"""<h2>Kelola Siswa</h2><ul>{items}</ul>
        <h3>Tambah Siswa</h3>
        <form method="post" action="/admin/siswa">
          <label>Nama <input name="nama" required></label>
          <label>Kode unik / NIS <input name="kode" required></label>
          <label>Kelas <select name="class_id">{class_opts}</select></label>
          <button type="submit">Tambah Siswa</button></form>"""
        return self.send_html(layout(user, "Siswa", body, flash))

    def post_admin_siswa(self, f):
        try:
            conn = db()
            conn.execute("INSERT INTO students (nama,kode,class_id) VALUES (?,?,?)",
                         (f["nama"].strip(), f["kode"].strip(), int(f["class_id"])))
            conn.commit(); conn.close()
            return self.redirect("/admin/siswa", "Siswa ditambahkan.")
        except sqlite3.IntegrityError:
            return self.redirect("/admin/siswa", "Kode siswa sudah dipakai.")

    def admin_siswa_edit(self, user, sid, flash):
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
        <form method="post" action="/admin/siswa/{st['id']}/edit">
          <label>Nama <input name="nama" value="{esc(st['nama'])}" required></label>
          <label>Kode unik / NIS <input name="kode" value="{esc(st['kode'])}" required></label>
          <label>Kelas <select name="class_id">{class_opts}</select></label>
          <button type="submit">Simpan Perubahan</button></form>
        <p><a href="/admin/siswa">Kembali ke daftar siswa</a></p>"""
        return self.send_html(layout(user, "Edit Siswa", body, flash))

    def post_admin_siswa_edit(self, sid, f):
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
            return self.redirect(f"/admin/siswa/{sid}/edit", "Kode siswa sudah dipakai.")

    def post_admin_siswa_hapus(self, sid):
        conn = db()
        st = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        if not st:
            conn.close()
            return self.redirect("/admin/siswa", "Siswa tidak ditemukan.")
        # Satu transaksi: hapus catatan absen siswa dulu, baru siswanya.
        conn.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
        conn.execute("DELETE FROM students WHERE id=?", (sid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/siswa", "Siswa dihapus.")

    # -- admin: penugasan -----------------------------------------------------
    def admin_penugasan(self, user, flash):
        conn = db()
        guru = conn.execute("SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        kelas = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        mapel = conn.execute("SELECT * FROM subjects ORDER BY nama").fetchall()
        rows = conn.execute(
            """SELECT a.*, u.nama guru, c.nama kelas, s.nama mapel FROM assignments a
               JOIN users u ON u.id=a.user_id JOIN classes c ON c.id=a.class_id
               JOIN subjects s ON s.id=a.subject_id ORDER BY u.nama, c.nama, s.nama""").fetchall()
        conn.close()
        def opts(rows_, label):
            return "".join(f'<option value="{r["id"]}">{esc(r[label])}</option>' for r in rows_)
        items = ""
        for r in rows:
            combo_js = (f"{r['guru']} — {r['kelas']} — {r['mapel']}"
                        .replace("\\", "\\\\").replace("'", "\\'").replace("\n", " "))
            confirm_msg = f"Hapus penugasan {combo_js}?"
            items += (
                f'<li>{esc(r["guru"])} &mdash; {esc(r["kelas"])} &mdash; {esc(r["mapel"])} '
                f'&mdash; <a href="/admin/penugasan/{r["id"]}/edit">Edit</a> '
                f'<form method="post" action="/admin/penugasan/{r["id"]}/hapus" class="inline" '
                f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                f'<button type="submit">Hapus</button></form></li>')
        items = items or "<li>Belum ada penugasan.</li>"
        body = f"""<h2>Penugasan Guru</h2><ul>{items}</ul>
        <form method="post" action="/admin/penugasan">
          <label>Guru <select name="user_id">{opts(guru, 'nama')}</select></label>
          <label>Kelas <select name="class_id">{opts(kelas, 'nama')}</select></label>
          <label>Mapel <select name="subject_id">{opts(mapel, 'nama')}</select></label>
          <button type="submit">Simpan Penugasan</button></form>"""
        return self.send_html(layout(user, "Penugasan", body, flash))

    def post_admin_penugasan(self, f):
        try:
            conn = db()
            conn.execute("INSERT INTO assignments (user_id,class_id,subject_id) VALUES (?,?,?)",
                         (int(f["user_id"]), int(f["class_id"]), int(f["subject_id"])))
            conn.commit(); conn.close()
            return self.redirect("/admin/penugasan", "Penugasan disimpan.")
        except sqlite3.IntegrityError:
            return self.redirect("/admin/penugasan", "Penugasan itu sudah ada.")

    def admin_penugasan_edit(self, user, aid, flash):
        conn = db()
        a = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        if not a:
            conn.close()
            return self.redirect("/admin/penugasan", "Data tidak ditemukan.")
        guru = conn.execute("SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        if not any(g["id"] == a["user_id"] for g in guru):
            # Guru pemilik penugasan ini sedang nonaktif: tetap tampilkan di pilihan
            # agar nilai awal formulir tidak diam-diam berubah.
            pemilik = conn.execute("SELECT * FROM users WHERE id=?", (a["user_id"],)).fetchone()
            if pemilik:
                guru = list(guru) + [pemilik]
        kelas = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        mapel = conn.execute("SELECT * FROM subjects ORDER BY nama").fetchall()
        conn.close()
        def opts(rows_, label, terpilih):
            return "".join(
                f'<option value="{r["id"]}"{" selected" if r["id"] == terpilih else ""}>'
                f'{esc(r[label])}</option>' for r in rows_)
        body = f"""<h2>Edit Penugasan</h2>
        <form method="post" action="/admin/penugasan/{a['id']}/edit">
          <label>Guru <select name="user_id">{opts(guru, 'nama', a['user_id'])}</select></label>
          <label>Kelas <select name="class_id">{opts(kelas, 'nama', a['class_id'])}</select></label>
          <label>Mapel <select name="subject_id">{opts(mapel, 'nama', a['subject_id'])}</select></label>
          <button type="submit">Simpan Perubahan</button></form>
        <p><a href="/admin/penugasan">Kembali ke daftar penugasan</a></p>"""
        return self.send_html(layout(user, "Edit Penugasan", body, flash))

    def post_admin_penugasan_edit(self, aid, f):
        conn = db()
        a = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        if not a:
            conn.close()
            return self.redirect("/admin/penugasan", "Data tidak ditemukan.")
        try:
            conn.execute("UPDATE assignments SET user_id=?, class_id=?, subject_id=? WHERE id=?",
                         (int(f["user_id"]), int(f["class_id"]), int(f["subject_id"]), aid))
            conn.commit(); conn.close()
            return self.redirect("/admin/penugasan", "Penugasan diperbarui.")
        except sqlite3.IntegrityError:
            conn.close()
            return self.redirect(f"/admin/penugasan/{aid}/edit", "Penugasan itu sudah ada.")

    def post_admin_penugasan_hapus(self, aid):
        conn = db()
        a = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        if not a:
            conn.close()
            return self.redirect("/admin/penugasan", "Data tidak ditemukan.")
        # Hanya baris penugasan yang dihapus; sesi absen yang sudah ada tidak disentuh.
        conn.execute("DELETE FROM assignments WHERE id=?", (aid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/penugasan", "Penugasan dihapus.")


    # -- guru: sesi -----------------------------------------------------------
    def _assignments_of(self, conn, user_id):
        return conn.execute(
            """SELECT a.*, c.nama kelas, s.nama mapel FROM assignments a
               JOIN classes c ON c.id=a.class_id JOIN subjects s ON s.id=a.subject_id
               WHERE a.user_id=? ORDER BY c.nama, s.nama""", (user_id,)).fetchall()

    def guru_sesi(self, user, flash):
        conn = db()
        assigns = self._assignments_of(conn, user["id"])
        rows = conn.execute(
            """SELECT s.*, c.nama kelas, sub.nama mapel FROM sessions s
               JOIN classes c ON c.id=s.class_id JOIN subjects sub ON sub.id=s.subject_id
               WHERE s.teacher_id=? ORDER BY s.id DESC""", (user["id"],)).fetchall()
        conn.close()
        combo_opts = "".join(
            f'<option value="{a["class_id"]}:{a["subject_id"]}">{esc(a["kelas"])} - {esc(a["mapel"])}</option>'
            for a in assigns)
        today = datetime.now().strftime("%Y-%m-%d")
        nowtime = datetime.now().strftime("%H:%M")
        items = "".join(
            f'<li><a href="/sesi/{r["id"]}">{esc(r["tanggal"])} {esc(r["jam_mulai"])} &bull; '
            f'{esc(r["kelas"])} &bull; {esc(r["mapel"])} &bull; {esc(r["status"])}</a></li>'
            for r in rows) or "<li>Belum ada sesi.</li>"
        form = ("""<h3>Buat Sesi Baru</h3>
        <form method="post" action="/guru/sesi/buat">
          <label>Kelas - Mapel (sesuai penugasan) <select name="combo">%s</select></label>
          <label>Tanggal <input name="tanggal" type="date" value="%s" required></label>
          <label>Jam mulai <input name="jam_mulai" type="time" value="%s" required></label>
          <label>Batas terlambat (menit setelah jam mulai)
            <input name="batas_terlambat_menit" type="number" value="15" min="0" required></label>
          <button type="submit">Buat &amp; Buka Sesi</button></form>""" % (combo_opts, today, nowtime)) \
            if assigns else "<p>Anda belum punya penugasan. Minta admin menambahkan penugasan dulu.</p>"
        body = f"<h2>Sesi Saya</h2><ul>{items}</ul>{form}"
        return self.send_html(layout(user, "Sesi Saya", body, flash))

    def post_buat_sesi(self, user, f):
        class_id, subject_id = (int(x) for x in f["combo"].split(":"))
        conn = db()
        allowed = conn.execute("SELECT 1 FROM assignments WHERE user_id=? AND class_id=? AND subject_id=?",
                               (user["id"], class_id, subject_id)).fetchone()
        if not allowed:
            conn.close()
            return self.redirect("/guru/sesi", "Kombinasi kelas-mapel itu bukan penugasan Anda.")
        cur = conn.execute(
            """INSERT INTO sessions (class_id,subject_id,teacher_id,tanggal,jam_mulai,batas_terlambat_menit)
               VALUES (?,?,?,?,?,?)""",
            (class_id, subject_id, user["id"], f["tanggal"], f["jam_mulai"],
             int(f["batas_terlambat_menit"])))
        conn.commit()
        sid = cur.lastrowid
        conn.close()
        return self.redirect(f"/sesi/{sid}", "Sesi dibuka. Silakan scan QR siswa.")

    def _get_session_for_teacher(self, conn, user, sid):
        return conn.execute(
            """SELECT s.*, c.nama kelas, sub.nama mapel FROM sessions s
               JOIN classes c ON c.id=s.class_id JOIN subjects sub ON sub.id=s.subject_id
               WHERE s.id=? AND s.teacher_id=?""", (sid, user["id"])).fetchone()

    def sesi_detail(self, user, sid, flash):
        conn = db()
        s = self._get_session_for_teacher(conn, user, sid)
        if not s:
            conn.close()
            return self.send_html(layout(user, "404", "<p>Sesi tidak ditemukan / bukan milik Anda.</p>"), status=404)
        students = conn.execute(
            """SELECT st.*, a.status, a.metode, a.waktu FROM students st
               LEFT JOIN attendance a ON a.student_id=st.id AND a.session_id=?
               WHERE st.class_id=? ORDER BY st.nama""", (sid, s["class_id"])).fetchall()
        conn.close()
        rows_html, demo_btns = "", ""
        for st in students:
            status = st["status"] or "-"
            manual_form = ""
            if s["status"] == "buka":
                manual_form = f"""<form method="post" action="/sesi/{sid}/manual" class="inline">
                  <input type="hidden" name="student_id" value="{st['id']}">
                  <select name="status"><option>Hadir</option><option>Terlambat</option>
                  <option>Sakit</option><option>Izin</option><option>Alpa</option></select>
                  <button type="submit">Simpan</button></form>"""
                demo_btns += f"""<form method="post" action="/sesi/{sid}/scan" class="inline">
                  <input type="hidden" name="kode" value="{esc(st['kode'])}">
                  <input type="hidden" name="metode" value="demo">
                  <button type="submit">Simulasikan scan: {esc(st['nama'])}</button></form>"""
            rows_html += (f'<li>{esc(st["nama"])} (<code>{esc(st["kode"])}</code>) &mdash; '
                          f'<b>{esc(status)}</b> {esc(st["metode"] or "")} {esc(st["waktu"] or "")} {manual_form}</li>')
        scan_ui = ""
        if s["status"] == "buka":
            scan_ui = f"""<section class="panel"><h3>Scan QR di Dalam Aplikasi</h3>
            <video id="cam" autoplay playsinline muted></video>
            <div><button type="button" id="btnCam">Mulai Kamera</button>
            <button type="button" id="btnStop" disabled>Stop Kamera</button></div>
            <p id="scanStatus"><small>Kamera memakai getUserMedia + jsQR. Jika kamera tidak tersedia,
            pakai ketik kode manual atau mode demo di bawah.</small></p>
            <form method="post" action="/sesi/{sid}/scan" id="scanForm">
              <label>Kode siswa (hasil scan / ketik manual)
                <input name="kode" id="kodeInput" required autocomplete="off"></label>
              <input type="hidden" name="metode" id="metodeInput" value="scan">
              <button type="submit">Catat dari Kode</button></form></section>
            <section class="panel"><h3>Mode Demo / Simulasi Scan</h3>{demo_btns}</section>
            <form method="post" action="/sesi/{sid}/tutup"
              onsubmit="return confirm('Tutup sesi? Siswa yang belum tercatat akan menjadi Alpa.')">
              <button type="submit" class="danger">Tutup Sesi (yang belum tercatat = Alpa)</button></form>
            <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"></script>
            <script src="/static/app.js"></script>"""
        body = f"""<h2>Sesi: {esc(s['kelas'])} - {esc(s['mapel'])}</h2>
        <p>{esc(s['tanggal'])} jam {esc(s['jam_mulai'])} &bull; batas terlambat
        {s['batas_terlambat_menit']} menit &bull; status sesi: <b>{esc(s['status'])}</b></p>
        {scan_ui}<h3>Daftar Siswa</h3><ul>{rows_html}</ul>"""
        return self.send_html(layout(user, "Sesi", body, flash))

    def _record(self, conn, user, s, student, metode, forced_status=None):
        """Catat satu kehadiran. Kembalikan pesan untuk guru."""
        existing = conn.execute("SELECT status FROM attendance WHERE session_id=? AND student_id=?",
                                (s["id"], student["id"])).fetchone()
        if existing:
            return f"{student['nama']} sudah tercatat: {existing['status']} (scan ganda diabaikan)."
        now = datetime.now()
        if forced_status:
            status = forced_status
        else:
            mulai = datetime.strptime(f"{s['tanggal']} {s['jam_mulai']}", "%Y-%m-%d %H:%M")
            batas = mulai + timedelta(minutes=int(s["batas_terlambat_menit"]))
            status = "Terlambat" if now > batas else "Hadir"
        conn.execute(
            """INSERT INTO attendance (session_id,student_id,status,metode,waktu,recorded_by)
               VALUES (?,?,?,?,?,?)""",
            (s["id"], student["id"], status, metode, now.strftime("%Y-%m-%d %H:%M:%S"), user["id"]))
        conn.commit()
        return f"{status} - {student['nama']} - dicatat pukul {now.strftime('%H:%M:%S')} ({metode})."

    def post_scan(self, user, sid, f):
        conn = db()
        s = self._get_session_for_teacher(conn, user, sid)
        if not s or s["status"] != "buka":
            conn.close()
            return self.redirect("/guru/sesi", "Sesi tidak ditemukan / sudah ditutup.")
        kode = (f.get("kode") or "").strip()
        student = conn.execute("SELECT * FROM students WHERE kode=?", (kode,)).fetchone()
        if not student:
            conn.close()
            return self.redirect(f"/sesi/{sid}", f"Kode '{kode}' tidak dikenal.")
        if student["class_id"] != s["class_id"]:
            conn.close()
            return self.redirect(f"/sesi/{sid}", f"Kode '{kode}' milik kelas lain, ditolak.")
        metode = f.get("metode") if f.get("metode") in ("scan", "demo") else "scan"
        msg = self._record(conn, user, s, student, metode)
        conn.close()
        return self.redirect(f"/sesi/{sid}", msg)

    def post_manual(self, user, sid, f):
        conn = db()
        s = self._get_session_for_teacher(conn, user, sid)
        if not s or s["status"] != "buka":
            conn.close()
            return self.redirect("/guru/sesi", "Sesi tidak ditemukan / sudah ditutup.")
        student = conn.execute("SELECT * FROM students WHERE id=? AND class_id=?",
                               (int(f["student_id"]), s["class_id"])).fetchone()
        if not student:
            conn.close()
            return self.redirect(f"/sesi/{sid}", "Siswa tidak ditemukan di kelas ini.")
        status = f["status"]
        conn.execute(
            """INSERT INTO attendance (session_id,student_id,status,metode,waktu,recorded_by)
               VALUES (?,?,?,'manual',?,?)
               ON CONFLICT(session_id,student_id) DO UPDATE SET status=excluded.status, metode='manual'""",
            (sid, student["id"], status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user["id"]))
        conn.commit(); conn.close()
        return self.redirect(f"/sesi/{sid}", f"{student['nama']} ditandai {status} (manual).")

    def post_tutup(self, user, sid):
        conn = db()
        s = self._get_session_for_teacher(conn, user, sid)
        if not s:
            conn.close()
            return self.redirect("/guru/sesi", "Sesi tidak ditemukan.")
        cur = conn.execute(
            """INSERT INTO attendance (session_id,student_id,status,metode,waktu,recorded_by)
               SELECT ?, st.id, 'Alpa', 'otomatis', ?, ?
               FROM students st WHERE st.class_id=?
               AND NOT EXISTS (SELECT 1 FROM attendance a WHERE a.session_id=? AND a.student_id=st.id)""",
            (sid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user["id"], s["class_id"], sid))
        marked = cur.rowcount
        conn.execute("UPDATE sessions SET status='tutup' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        return self.redirect(f"/sesi/{sid}", f"Sesi ditutup. {marked} siswa ditandai Alpa.")


    # -- rekap ----------------------------------------------------------------
    def _visible_sessions(self, conn, user):
        if user["role"] == "admin":
            return conn.execute(
                """SELECT s.*, c.nama kelas, sub.nama mapel, u.nama guru FROM sessions s
                   JOIN classes c ON c.id=s.class_id JOIN subjects sub ON sub.id=s.subject_id
                   JOIN users u ON u.id=s.teacher_id ORDER BY s.id DESC""").fetchall()
        return conn.execute(
            """SELECT s.*, c.nama kelas, sub.nama mapel, u.nama guru FROM sessions s
               JOIN classes c ON c.id=s.class_id JOIN subjects sub ON sub.id=s.subject_id
               JOIN users u ON u.id=s.teacher_id WHERE s.teacher_id=? ORDER BY s.id DESC""",
            (user["id"],)).fetchall()

    def rekap(self, user, flash):
        conn = db()
        sessions = self._visible_sessions(conn, user)
        blocks = ""
        for s in sessions:
            rows = conn.execute(
                """SELECT st.nama, st.kode, a.status, a.metode, a.waktu FROM attendance a
                   JOIN students st ON st.id=a.student_id WHERE a.session_id=? ORDER BY st.nama""",
                (s["id"],)).fetchall()
            counts = {}
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            ringkas = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "belum ada catatan"
            detail = "".join(f'<li>{esc(r["nama"])} (<code>{esc(r["kode"])}</code>) - {esc(r["status"])}</li>'
                             for r in rows)
            blocks += f"""<section class="panel"><h3>{esc(s['tanggal'])} - {esc(s['kelas'])} -
            {esc(s['mapel'])} (guru: {esc(s['guru'])}, sesi {esc(s['status'])})</h3>
            <p>{esc(ringkas)} &bull; <a href="/rekap/{s['id']}/csv">Unduh CSV sesi ini</a></p>
            <ul>{detail}</ul></section>"""
        conn.close()
        body = "<h2>Rekap Kehadiran</h2>" + (blocks or "<p>Belum ada sesi.</p>")
        return self.send_html(layout(user, "Rekap", body, flash))

    def rekap_csv(self, user, sid):
        conn = db()
        sessions = {s["id"]: s for s in self._visible_sessions(conn, user)}
        if sid not in sessions:
            conn.close()
            return self.send_html(layout(user, "404", "<p>Sesi tidak ditemukan.</p>"), status=404)
        rows = conn.execute(
            """SELECT st.nama, st.kode, a.status, a.metode, a.waktu FROM attendance a
               JOIN students st ON st.id=a.student_id WHERE a.session_id=? ORDER BY st.nama""",
            (sid,)).fetchall()
        conn.close()
        import csv
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["nama", "kode", "status", "metode", "waktu"])
        for r in rows:
            w.writerow([r["nama"], r["kode"], r["status"], r["metode"], r["waktu"]])
        data = out.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="rekap-sesi-{sid}.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- cetak kartu QR ---------------------------------------------------------
    def cetak(self, user, class_id, flash):
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
                <div class="kkode">{esc(st['kode'])}</div></div>""" for st in students)
        body = f"""<h2 class="noprint">Cetak Kartu QR - {esc(kelas['nama'])}</h2>
        <p class="noprint"><button onclick="window.print()">Cetak Halaman Ini</button>
        &nbsp;Kartu berisi QR Code biasa dari kode unik siswa (bukan QRIS). Gunting tiap kartu untuk dibagikan.</p>
        <div class="lembar">{cards or '<p>Belum ada siswa di kelas ini.</p>'}</div>"""
        return self.send_html(layout(user, "Cetak Kartu QR", body, flash))

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
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Aplikasi Absen Siswa Multi-Guru jalan di http://127.0.0.1:{PORT}  (DB: {DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBerhenti.")


if __name__ == "__main__":
    main()
