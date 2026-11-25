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
  scan <= batas Hadir = Hadir, sesudahnya s.d. sebelum batas Alpa =
  Terlambat (jam dicatat persis). Batas Hadir & batas Alpa BISA DIEDIT
  admin di Pengaturan Tampilan > Jam Absensi (bawaan 07:00 & 09:00),
  karena tiap sekolah jam masuknya beda.
- Yang sampai batas Alpa belum scan otomatis menjadi Alpa (metode 'auto'),
  terisi saat tabel/dashboard dibuka. Sakit/Izin diisi manual.
- Scan pukul batas Alpa ke atas DITUTUP: orang yang belum punya catatan asli
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
import string
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, time as dtime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ABSEN_DB", os.path.join(BASE_DIR, "data", "absen.db"))
PORT = int(os.environ.get("PORT", "8000"))
TZ_JKT = ZoneInfo("Asia/Jakarta")

JAM_BATAS_HADIR = dtime(7, 0)    # BAWAAN: scan <= 07:00 -> Hadir
JAM_BATAS_ALPA = dtime(9, 0)     # BAWAAN: >= 09:00 belum scan -> Alpa otomatis
JAM_PANEL_SELESAI = dtime(17, 0)  # BAWAAN: >= 17:00 panel bawah dashboard hilang
# Jam absensi yang BERLAKU dibaca dari tabel settings (kunci
# 'jam_batas_hadir' / 'jam_batas_alpa' / 'jam_panel_selesai', format
# 'HH:MM'), bisa diedit admin di Pengaturan Tampilan > Jam Absensi.
# Nilai kosong/tidak sah -> jam bawaan di atas.
JAM_DEFAULT_STR = {"jam_batas_hadir": "07:00", "jam_batas_alpa": "09:00",
                   "jam_panel_selesai": "17:00"}


def _parse_jam(teks, bawaan):
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", (teks or "").strip())
    if not m:
        return bawaan
    return dtime(int(m.group(1)), int(m.group(2)))


def jam_dari_settings(rows):
    """rows: dict key->value settings. Kembalikan (hadir, alpa, panel)
    sebagai dtime, dengan bawaan bila kosong/tidak sah."""
    return (_parse_jam(rows.get("jam_batas_hadir"), JAM_BATAS_HADIR),
            _parse_jam(rows.get("jam_batas_alpa"), JAM_BATAS_ALPA),
            _parse_jam(rows.get("jam_panel_selesai"), JAM_PANEL_SELESAI))


def jam_absensi(conn=None):
    """Jam absensi yang berlaku sekarang: (hadir, alpa, panel).
    Bila conn diberikan, dibaca lewat conn itu (dipakai pastikan_alpa
    yang sudah memegang koneksi); selain itu buka koneksi sendiri."""
    try:
        if conn is not None:
            rows = {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('jam_batas_hadir','jam_batas_alpa','jam_panel_selesai')")}
            return jam_dari_settings(rows)
        c = db()
        try:
            rows = {r["key"]: r["value"] for r in c.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('jam_batas_hadir','jam_batas_alpa','jam_panel_selesai')")}
        finally:
            c.close()
        return jam_dari_settings(rows)
    except Exception:
        return JAM_BATAS_HADIR, JAM_BATAS_ALPA, JAM_PANEL_SELESAI
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


def lisensi_label():
    """Nama pemegang lisensi pemasangan ini, dari env ABSEN_LISENSI.

    Diisi PENJUAL di server (mis. Variables pada layanan Railway) per
    sekolah pembeli. SENGAJA tidak disimpan di tabel settings, tidak
    tampil di formulir mana pun, dan tidak bisa diubah/dihapus dari
    dalam aplikasi — sekolah pemakai hanya melihat labelnya. Env
    dibaca setiap pemanggilan (pola yang sama seperti
    ABSEN_NOW_OVERRIDE). Kosong/tidak diisi -> "" (label tidak
    ditampilkan sama sekali, tampilan persis seperti sebelumnya).
    """
    return os.environ.get("ABSEN_LISENSI", "").strip()


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
    kode_qr TEXT,
    no_wa TEXT DEFAULT '',          -- nomor WhatsApp guru (kolom No WA di
                                    -- Kelola Guru + tombol Kirim Password via WA)
    jenis_kelamin TEXT DEFAULT '',  -- 'Laki-laki' / 'Perempuan' (tampil di
                                    -- Kartu Guru v3); '' = belum diisi
    pass_sementara TEXT DEFAULT ''  -- password guru (wali) yang SEDANG berlaku,
                                    -- teks biasa KHUSUS tampilan admin di Kelola
                                    -- Guru; login tetap diperiksa via hash PBKDF2
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT UNIQUE NOT NULL,
    wali_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT NOT NULL,
    kode TEXT UNIQUE NOT NULL,          -- NIS siswa (isi QR kartunya)
    class_id INTEGER NOT NULL REFERENCES classes(id),
    jenis_kelamin TEXT DEFAULT ''       -- 'Laki-laki' / 'Perempuan' (tampil di
                                        -- Kartu Pelajar v3); '' = belum diisi
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


# ---------------------------------------------------------------------------
# Model "login hanya wali kelas"
# ---------------------------------------------------------------------------
def slug_kelas(nama):
    """Slug nama kelas untuk username wali: huruf kecil, hanya a-z0-9.
    "1A" -> "1a", "X IPA 1" -> "xipa1", "UJI-Kelas-1" -> "ujikelas1"."""
    return re.sub(r"[^a-z0-9]", "", (nama or "").lower())


def password_acak(n=10):
    """Password acak huruf+angka (modul secrets) untuk login wali kelas.
    Ditampilkan ke admin lewat pesan flash dan disimpan juga sebagai teks
    biasa di users.pass_sementara supaya admin selalu bisa melihat
    password wali yang sedang berlaku pada kolom Password di Kelola
    Guru (permintaan admin sekolah). Pemeriksaan login tetap hanya
    memakai hash PBKDF2 — pass_sementara murni kolom tampilan."""
    abjad = string.ascii_letters + string.digits
    return "".join(secrets.choice(abjad) for _ in range(n))


def info_wali(user):
    """Baris kelas yang diwali user guru ini (dari classes.wali_id), atau
    None bila user bukan guru / bukan wali kelas mana pun. Dibaca langsung
    dari DB setiap kali dipanggil, jadi pencabutan penugasan berlaku
    seketika (sesi wali yang dicabut langsung kehilangan akses)."""
    if not user or user["role"] != "guru":
        return None
    conn = db()
    row = conn.execute("SELECT * FROM classes WHERE wali_id=?",
                       (user["id"],)).fetchone()
    conn.close()
    return row


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
    if "pass_sementara" not in kolom_users:
        conn.execute("ALTER TABLE users ADD COLUMN pass_sementara TEXT DEFAULT ''")
    if "no_wa" not in kolom_users:
        conn.execute("ALTER TABLE users ADD COLUMN no_wa TEXT DEFAULT ''")
    if "jenis_kelamin" not in kolom_users:
        conn.execute("ALTER TABLE users ADD COLUMN jenis_kelamin TEXT DEFAULT ''")
    kolom_siswa = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    if "jenis_kelamin" not in kolom_siswa:
        conn.execute("ALTER TABLE students ADD COLUMN jenis_kelamin TEXT DEFAULT ''")
    # Model "login hanya wali kelas": classes.wali_id menunjuk guru (users.id)
    # yang menjadi wali kelas itu. NULL = belum ada wali (guru belum bisa login).
    kolom_kelas = {r["name"] for r in conn.execute("PRAGMA table_info(classes)")}
    if "wali_id" not in kolom_kelas:
        conn.execute("ALTER TABLE classes ADD COLUMN wali_id INTEGER REFERENCES users(id)")
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
    jam Jakarta sudah >= batas Alpa yang berlaku (pengaturan, bawaan
    09:00). Tidak pernah untuk tanggal masa depan atau hari Minggu.
    Siswa & guru aktif yang belum punya catatan apa pun pada tanggal itu
    menjadi Alpa.
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
    if d == now.date() and now.time() < jam_absensi(conn)[1]:
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


# Ikon sekolah gambar tangan (gaya gores, mengikuti warna teks lewat
# currentColor sehingga ikut berubah mengikuti tema): siswa dan alat
# tulis/perlengkapan sekolah. Dipakai kartu statistik, kartu per kelas,
# dan keadaan kosong.
IKON_TEMA = {
    "siswa": _svg('<circle cx="12" cy="7.8" r="3.4"/>'
                  '<path d="M5 20.6c.9-3.9 3.6-5.9 7-5.9s6.1 2 7 5.9"/>'),
    "pensil": _svg('<path d="M4.8 19.2l1.2-4L16.7 4.5a2.12 2.12 0 0 1 3 3L9 18.2z"/>'
                   '<path d="M14.8 6.4l3 3"/>'),
    "buku": _svg('<path d="M5 20V5a2 2 0 0 1 2-2h12v16H7a2 2 0 0 0-2 1z"/>'
                 '<path d="M5 20a2 2 0 0 0 2 2h12v-3"/><path d="M9 7h6"/>'),
    "tas": _svg('<path d="M6 21v-11a6 6 0 0 1 12 0v11"/>'
                '<path d="M4.5 21h15"/>'
                '<path d="M9.5 21v-4.5a2.5 2.5 0 0 1 5 0V21"/>'
                '<path d="M10 6.8V5.2a2 2 0 0 1 4 0v1.6"/>'),
    "penggaris": _svg('<rect x="2.5" y="9" width="19" height="6.5" rx="1"/>'
                      '<path d="M6.5 9v3M10.5 9v3.8M14.5 9v3M18.5 9v3.8"/>'),
    "jam": _svg('<circle cx="12" cy="13" r="7.5"/>'
                '<path d="M12 9.8V13l2.4 1.6"/>'
                '<path d="M5.2 3.8L3.6 6M18.8 3.8l1.6 2.2"/>'
                '<path d="M8.5 20.5L7 22M15.5 20.5L17 22"/>'),
}


def ikon_tema(nama):
    return IKON_TEMA.get(nama, "")


# ---------------------------------------------------------------------------
# HTML layout — shell aplikasi modern (sidebar desktop + bottom-nav mobile)
# ---------------------------------------------------------------------------
def nav_items(user):
    if user["role"] == "guru" and info_wali(user):
        # Wali kelas: HANYA Dashboard, Tabel Siswa (kelasnya), Cetak Kartu
        # (kelasnya), dan Ganti Password. (Keluar ada di chip pengguna /
        # kartu Menu.)
        return [("dashboard", "/dashboard", "Dashboard", "grid"),
                ("tabel-siswa", "/tabel-siswa", "Tabel Siswa", "users"),
                ("cetak", "/cetak", "Cetak Kartu", "printer"),
                ("kartu-pelajar", "/kartu-pelajar", "Kartu Pelajar", "cap"),
                ("kartu-guru", "/kartu-guru", "Kartu Guru", "badge"),
                ("ganti-password", "/ganti-password", "Ganti Password", "key")]
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
              ("kartu-pelajar", "/kartu-pelajar", "Kartu Pelajar", "cap"),
              ("kartu-guru", "/kartu-guru", "Kartu Guru", "badge"),
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


def layout(user, title, body, flash="", aktif="", bawah_auth=""):
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    peng = get_pengaturan()
    nama_app = peng["nama_sekolah"] or "Absen Siswa"
    tema = peng.get("tema") or ""
    # Tema pastel aktif MENANG atas warna latar & warna menu: latar
    # halaman memakai --tema-bg dari style.css dan penimpa --warna-menu
    # tidak dimuat sama sekali. Pengecualian: bila wallpaper terpasang,
    # perilaku wallpaper (termasuk warna latar di belakang lapisannya)
    # tidak berubah sama sekali.
    bg_style = f' style="background-color:{peng["bg_color"]}"'
    if tema and not peng["wallpaper"]:
        bg_style = ""
    kelas_tema = f" tema-{tema}" if tema else ""
    wp_layer = ""
    if peng["wallpaper"]:
        # Tingkat kejelasan tersimpan -> variabel --wp-alpha menimpa alpha
        # bawaan .86 di style.css. Belum pernah diatur -> tanpa penimpa,
        # tampilan persis seperti sebelumnya.
        gaya_wp = ""
        mentah_wp = (peng.get("kejelasan_wp") or "").strip().lower()
        if mentah_wp in KEJELASAN_WP:
            gaya_wp = f";--wp-alpha:{KEJELASAN_WP[mentah_wp]}"
        wp_layer = (f'<div class="bg-wallpaper" '
                    f'style="background-image:url(\'/wallpaper/{esc(peng["wallpaper"])}\')'
                    f'{gaya_wp}"></div>')
    tahun = waktu_sekarang().year
    # Label lisensi terkunci (env ABSEN_LISENSI): hanya dirender bila
    # penjual memasangnya di server; selain itu halaman persis seperti
    # sebelumnya, tanpa baris kosong.
    lis = lisensi_label()
    lisensi_html = (f'<p class="lisensi">Dilisensikan untuk: {esc(lis)}</p>'
                    if lis else "")
    # Kredit pembuat: TERKUNCI di kode (bukan settings, tidak bisa
    # diubah/dihapus dari dalam aplikasi), selalu tampil di footer.
    made_html = '<p class="made-by">Made by IXOYS Inc</p>'
    if not user:
        return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - {esc(nama_app)}</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="auth-body{kelas_tema}"{bg_style}>{wp_layer}<div class="auth-wrap">
<div class="auth-card">
<div class="auth-brand">{brand_mark(peng)}
<span class="brand-name">{esc(nama_app)}</span></div>
{flash_html}{body}</div>
{bawah_auth}{lisensi_html}<p class="auth-foot">&copy; {tahun} {esc(nama_app)} &bull; Absensi sekolah dengan QR Code biasa (bukan QRIS pembayaran).</p>{made_html}
</div><script src="/static/app.js"></script></body></html>"""
    menu_style, nama_kelas_menu = gaya_menu(peng)
    if tema:
        menu_style = ""  # tema menang atas warna menu: penimpa tidak dimuat
    kelas_body = []
    if nama_kelas_menu:
        kelas_body.append(nama_kelas_menu)
    if tema:
        kelas_body.append(f"tema-{tema}")
    menu_cls = f' class="{" ".join(kelas_body)}"' if kelas_body else ""
    items = nav_items(user)
    sidenav = "".join(
        f'<a class="nav-item{" aktif" if k == aktif else ""}" href="{href}">'
        f'{icon(ic)}<span>{label}</span></a>' for k, href, label, ic in items)
    wali_row = info_wali(user)
    if wali_row:
        bawah = [("dashboard", "/dashboard", "Dashboard", "grid"),
                 ("tabel-siswa", "/tabel-siswa", "Siswa", "users"),
                 ("cetak", "/cetak", "Kartu", "printer"),
                 ("menu", "/menu", "Menu", "menu")]
    else:
        bawah = [("dashboard", "/dashboard", "Dashboard", "grid"),
                 ("scan", "/scan-gerbang", "Scan", "scan"),
                 ("tabel-siswa", "/tabel-siswa", "Siswa", "users"),
                 ("tabel-guru", "/tabel-guru", "Guru", "badge"),
                 ("menu", "/menu", "Menu", "menu")]
    bottomnav = "".join(
        f'<a class="bnav-item{" aktif" if k == aktif else ""}" href="{href}">'
        f'{icon(ic)}<span>{label}</span></a>' for k, href, label, ic in bawah)
    initial = esc((user["nama"] or "?").strip()[:1].upper() or "?")
    if user["role"] == "admin":
        role_label = "Admin"
    elif wali_row:
        role_label = f"Wali Kelas {wali_row['nama']}"
    else:
        role_label = "Guru"
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - {esc(nama_app)}</title>
<link rel="stylesheet" href="/static/style.css">{menu_style}</head>
<body{menu_cls}{bg_style}>{wp_layer}<div class="shell">
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
<footer><small>&copy; {tahun} {esc(nama_app)} &bull; Absensi gerbang sekolah &bull; QR Code biasa (bukan QRIS pembayaran)</small>{lisensi_html}{made_html}</footer>
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

    def send_bytes(self, data, ctype, filename=None, extra_headers=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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

    def wali_kelas_id(self, user):
        """id kelas yang diwali user guru ini, atau None (admin / bukan
        wali). Selalu dibaca segar dari DB per permintaan."""
        row = info_wali(user)
        return row["id"] if row else None

    def _keluarkan_sesi(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("absen_session")
        if morsel:
            LOGIN_SESSIONS.pop(morsel.value, None)

    # Rute non-admin yang TIDAK boleh dibuka wali kelas sama sekali.
    RUTE_TERLARANG_WALI = ("/tabel-guru", "/tabel-guru/excel",
                           "/laporan/siswa", "/laporan/guru",
                           "/cari", "/scan-gerbang", "/cetak/guru")
    PESAN_TERLARANG_WALI = "Halaman itu tidak tersedia untuk wali kelas."

    def form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def form_lengkap(self):
        """Seperti form(), tetapi nilai kosong DIPERTAHANKAN
        (keep_blank_values=True): dibutuhkan endpoint yang memakai
        value="" sebagai pilihan sah — dropdown wali sebaris di Kelola
        Kelas mengirim wali_id="" untuk pilihan "— Belum ada —", yang
        oleh parse_qs bawaan akan dibuang sebelum sampai ke handler."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
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
        if path in ("/static/style.css", "/static/app.js", "/static/jsQR.js"):
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
            body = """<h2>Login Admin</h2>
            <form method="post" action="/login">
              <label>Username <input name="username" required autocomplete="username"></label>
              <label>Password <input name="password" type="password" required autocomplete="current-password"></label>
              <button type="submit" class="btn btn-primary btn-block">Masuk</button></form>
            <p class="muted"><small>Akun uji lokal ada di README proyek. Jangan pakai akun awal di produksi.</small></p>"""
            # Kartu kedua: Login Wali Kelas — wali TIDAK mengetik username;
            # ia cukup MEMILIH kelasnya (hanya kelas yang sudah punya wali
            # yang ditawarkan) lalu memasukkan password loginnya.
            conn = db()
            kelas_wali = conn.execute(
                "SELECT id, nama FROM classes WHERE wali_id IS NOT NULL "
                "ORDER BY nama").fetchall()
            conn.close()
            if kelas_wali:
                opsi = "".join(
                    f'<option value="{k["id"]}">Wali Kelas {esc(k["nama"])}</option>'
                    for k in kelas_wali)
                pilih_kelas = (f'<label>Kelas <select name="kelas_id" required>'
                               f'{opsi}</select></label>')
                nonaktif, catatan_wali = "", ""
            else:
                pilih_kelas = ('<label>Kelas <select name="kelas_id" disabled>'
                               '<option>&mdash; Belum ada &mdash;</option>'
                               '</select></label>')
                nonaktif = " disabled"
                catatan_wali = ('<p class="muted"><small>Belum ada wali kelas '
                                'yang ditunjuk admin.</small></p>')
            kartu_wali = f"""<div class="auth-card auth-card-wali">
            <h2>Login Wali Kelas</h2>
            <form method="post" action="/login-wali">
              {pilih_kelas}
              <label>Password <input name="password" type="password" required autocomplete="current-password"></label>
              <button type="submit" class="btn btn-primary btn-block"{nonaktif}>Masuk sebagai Wali Kelas</button></form>
            {catatan_wali}</div>
"""
            return self.send_html(layout(None, "Masuk", body, flash,
                                         bawah_auth=kartu_wali))
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
        if user["role"] == "guru":
            # Penjaga model wali kelas, diperiksa SETIAP permintaan:
            # guru yang bukan wali (lagi) dianggap sudah keluar.
            if self.wali_kelas_id(user) is None:
                self._keluarkan_sesi()
                return self.redirect("/login", "Silakan masuk dulu.")
            if path in self.RUTE_TERLARANG_WALI:
                return self.redirect("/dashboard", self.PESAN_TERLARANG_WALI)
        if path == "/dashboard":
            return self.dashboard(user, flash)
        if path == "/cari":
            return self.cari(user, qs, flash)
        if (path.startswith("/siswa/") and path.endswith("/unduh")
                and path.split("/")[2].isdigit()):
            return self.siswa_detail_unduh(user, int(path.split("/")[2]))
        if path.startswith("/siswa/") and path.split("/")[2].isdigit():
            return self.siswa_detail(user, int(path.split("/")[2]), flash)
        if path == "/laporan/siswa":
            return self.laporan_siswa(user, flash)
        if path == "/laporan/guru":
            return self.laporan_guru(user, flash)
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
        if path == "/kartu-pelajar":
            return self.kartu_pelajar_index(user, flash)
        if path.startswith("/kartu-pelajar/kelas/"):
            return self.kartu_pelajar_kelas(user, int(path.split("/")[3]), flash)
        if path.startswith("/kartu-pelajar/siswa/"):
            return self.kartu_pelajar_siswa(user, int(path.split("/")[3]), flash)
        if path == "/kartu-guru":
            return self.kartu_guru_index(user, flash)
        if path.startswith("/kartu-guru/guru/"):
            return self.kartu_guru_satu(user, int(path.split("/")[3]), flash)
        if user["role"] == "admin":
            if path == "/admin/guru": return self.admin_guru(user, flash)
            if path.startswith("/admin/guru/") and path.endswith("/edit"):
                return self.admin_guru_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/kelas": return self.admin_kelas(user, flash)
            if path.startswith("/admin/kelas/") and path.endswith("/edit"):
                return self.admin_kelas_edit(user, int(path.split("/")[3]), flash)
            if (path.startswith("/admin/kelas/") and path.endswith("/template")
                    and path.split("/")[3].isdigit()):
                return self.admin_kelas_template(int(path.split("/")[3]))
            if path.startswith("/admin/kelas/") and path.split("/")[3].isdigit():
                return self.admin_kelas_detail(user, int(path.split("/")[3]), flash)
            if path == "/admin/siswa": return self.admin_siswa(user, qs, flash)
            if path.startswith("/admin/siswa/") and path.endswith("/edit"):
                return self.admin_siswa_edit(user, int(path.split("/")[3]), flash)
            if path == "/admin/tampilan": return self.admin_tampilan(user, flash)
            if path == "/admin/backup": return self.admin_backup(user, flash)
            if path == "/admin/siswa/template": return self.admin_siswa_template()
            if path == "/admin/guru/template": return self.admin_guru_template()
            if path == "/admin/backup/unduh": return self.admin_backup_unduh()
        return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>",
                                     aktif=""), status=404)

    # -- POST routes --------------------------------------------------------
    def route_post(self, path):
        if path == "/login":
            return self.post_login()
        if path == "/login-wali":
            return self.post_login_wali()
        user = self.current_user()
        if not user:
            return self.redirect("/login", "Silakan masuk dulu.")
        if user["role"] == "guru":
            # Penjaga wali kelas yang sama seperti route_get.
            if self.wali_kelas_id(user) is None:
                self._keluarkan_sesi()
                return self.redirect("/login", "Silakan masuk dulu.")
            if path == "/api/scan":
                return self.redirect("/dashboard", self.PESAN_TERLARANG_WALI)
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
        if path == "/admin/siswa/import":
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_siswa_import()
        if path == "/admin/guru/import":
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_guru_import()
        if path.startswith("/admin/kelas/") and path.endswith("/import"):
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            return self.post_admin_kelas_import(int(path.split("/")[3]))
        if path.startswith("/admin/kelas/") and path.endswith("/wali"):
            if user["role"] != "admin":
                return self.send_html(layout(user, "404", "<p>Halaman tidak ditemukan.</p>"),
                                      status=404)
            # Form dibaca dengan form_lengkap(): pilihan "— Belum ada —"
            # pada dropdown sebaris bernilai "" dan harus sampai.
            return self.post_admin_kelas_wali(int(path.split("/")[3]),
                                              self.form_lengkap())
        f = self.form()
        if path == "/ganti-password":
            ph, salt = hash_password(f["password"])
            conn = db()
            if user["role"] == "guru":
                # Password guru (wali kelas) yang berlaku ikut disimpan
                # sebagai teks biasa di pass_sementara, agar tampilan
                # password pada kolom Password di Kelola Guru SELALU
                # sinkron — termasuk sesudah wali mengganti passwordnya
                # sendiri (permintaan admin sekolah). Untuk akun ADMIN
                # tidak disimpan apa pun di kolom tampilan itu.
                conn.execute("UPDATE users SET password_hash=?, salt=?, "
                             "pass_sementara=? WHERE id=?",
                             (ph, salt, f["password"], user["id"]))
            else:
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
            if path == "/admin/guru/kirim-wa-semua":
                return self.post_admin_guru_kirim_wa_semua()
            if path.startswith("/admin/guru/") and path.endswith("/edit"):
                return self.post_admin_guru_edit(int(path.split("/")[3]), f)
            if path.startswith("/admin/guru/") and path.endswith("/hapus"):
                return self.post_admin_guru_hapus(int(path.split("/")[3]))
            if path.startswith("/admin/guru/") and path.endswith("/kirim-wa"):
                return self.post_admin_guru_kirim_wa(int(path.split("/")[3]))
            if path.startswith("/admin/guru/") and path.endswith("/password-baru"):
                return self.post_admin_guru_password_baru(int(path.split("/")[3]))
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
            if path == "/admin/tampilan/warna-menu":
                return self.post_admin_tampilan_warna_menu(f)
            if path == "/admin/tampilan/kejelasan-wp":
                return self.post_admin_tampilan_kejelasan_wp(f)
            if path == "/admin/tampilan/tema":
                return self.post_admin_tampilan_tema(f)
            if path == "/admin/tampilan/jam-absen":
                return self.post_admin_tampilan_jam_absen(f)
            if path == "/admin/tampilan/wa-gateway":
                return self.post_admin_tampilan_wa_gateway(f)
            if path == "/admin/tampilan/wa-gateway/putus":
                return self.post_admin_tampilan_wa_gateway_putus()
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
        if row and verify_password(f.get("password", ""), row["password_hash"], row["salt"]):
            if not row["aktif"]:
                # Akun nonaktif tidak bisa masuk; password-nya benar, jadi
                # beri pesan yang jelas (bukan kegagalan generik).
                conn.close()
                return self.redirect("/login", "Akun ini sudah dinonaktifkan.")
            if row["role"] == "guru":
                # Model login hanya wali kelas: guru biasa (bukan wali
                # kelas mana pun saat ini) ditolak dengan pesan generik,
                # sama seperti kredensial yang salah.
                wali = conn.execute("SELECT id FROM classes WHERE wali_id=?",
                                    (row["id"],)).fetchone()
                if not wali:
                    conn.close()
                    return self.redirect("/login", "Username atau password salah.")
            conn.close()
            return self._sesi_masuk(row)
        conn.close()
        return self.redirect("/login", "Username atau password salah.")

    def _sesi_masuk(self, row):
        """Buat sesi login server-side untuk user ini dan arahkan ke
        dashboard dengan sambutan biasa. Dipakai login username maupun
        login wali kelas (pilih kelas), supaya logika sesinya satu."""
        token = secrets.token_urlsafe(32)
        LOGIN_SESSIONS[token] = row["id"]
        return self.redirect("/dashboard", f"Selamat datang, {row['nama']}.",
                             set_cookie=f"absen_session={token}; HttpOnly; Path=/; SameSite=Lax")

    def post_login_wali(self):
        """Login wali kelas TANPA username: wali memilih kelasnya di
        halaman login, lalu memasukkan password loginnya (password yang
        sama dengan yang diterbitkan saat penunjukan). Kelas ditelusur ke
        walinya (classes.wali_id -> users); berhasil hanya bila user itu
        guru yang aktif dan passwordnya cocok. Semua kegagalan memakai
        pesan generik yang sama — tidak dibedakan kelas salah vs
        password salah."""
        f = self.form()
        try:
            kelas_id = int(f.get("kelas_id", ""))
        except (TypeError, ValueError):
            kelas_id = None
        conn = db()
        row = None
        if kelas_id is not None:
            kelas = conn.execute("SELECT wali_id FROM classes WHERE id=?",
                                 (kelas_id,)).fetchone()
            if kelas and kelas["wali_id"]:
                row = conn.execute("SELECT * FROM users WHERE id=?",
                                   (kelas["wali_id"],)).fetchone()
        ok = (row is not None and row["role"] == "guru" and bool(row["aktif"])
              and verify_password(f.get("password", ""),
                                  row["password_hash"], row["salt"]))
        conn.close()
        if ok:
            return self._sesi_masuk(row)
        return self.redirect("/login", "Kelas atau password salah.")

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
        # Keputusan user berikutnya: mulai pukul 17:00 panel bawah
        # HILANG sama sekali (tidak ada panel apa pun di slot itu) —
        # halaman /laporan/siswa & /laporan/guru tetap bisa dibuka,
        # hanya panel dashboard-nya yang disembunyikan. Kartu statistik
        # TIDAK ikut berubah: tetap nilai reset sesudah 17:00.
        jam_hadir, jam_alpa, jam_panel = jam_absensi(conn)
        sudah_sembilan = now.time() >= jam_alpa
        panel_selesai = now.time() >= jam_panel
        if user["role"] == "guru":
            # Wali kelas: panel Laporan Hari Ini (kartu yang bisa diklik)
            # TIDAK tampil untuk wali — halaman laporannya sendiri juga
            # terlarang. Sesudah 09:00 slot panel bawah dikosongkan.
            panel_selesai = panel_selesai or sudah_sembilan
        tampil_laporan = sudah_sembilan and not panel_selesai
        panel_laporan = self._panel_laporan(conn, tgl) if tampil_laporan else None
        # Kehadiran per kelas HARI INI dari catatan ASLI (bukan nilai
        # reset tampilan pukul 09:00): yang terhitung masuk = Hadir +
        # Terlambat, sama seperti definisi pada tabel mingguan/laporan.
        per_kelas = conn.execute(
            """SELECT c.nama AS nama, COUNT(s.id) AS total,
                      COALESCE(SUM(CASE WHEN k.status IN ('Hadir','Terlambat')
                                        THEN 1 ELSE 0 END), 0) AS masuk
               FROM classes c
               LEFT JOIN students s ON s.class_id = c.id
               LEFT JOIN kehadiran k ON k.ref_type='siswa' AND k.ref_id = s.id
                    AND k.tanggal = ?
               GROUP BY c.id ORDER BY c.nama""", (tgl,)).fetchall()
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

        def stat(ic_html, warna, angka, label):
            return (f'<div class="stat-card"><span class="stat-ic {warna}">{ic_html}</span>'
                    f'<span class="stat-txt"><b>{angka}</b><small>{esc(label)}</small></span></div>')
        cards = "".join([
            stat(ikon_tema("siswa"), "hijau", k_hadir_s, "Siswa Hadir"),
            stat(ikon_tema("jam"), "kuning", k_telat_s, "Siswa Terlambat"),
            stat(icon("users"), "biru", k_sakit_s, "Siswa Sakit/Izin"),
            stat(icon("x"), "merah", k_alpa_s, "Siswa Alpa"),
            stat(icon("scan"), "ungu", k_belum_s, "Siswa Belum Scan"),
            stat(icon("scan"), "indigo", k_belum_g, "Guru Belum Scan"),
            stat(icon("badge"), "indigo", k_hadir_g, "Guru Hadir"),
        ])
        # Kartu Kehadiran per Kelas Hari Ini: satu baris per kelas dengan
        # batang kemajuan (masuk / total siswa kelas itu). Kelas tanpa
        # siswa tampil "—" dengan batang 0%. Tanpa kelas sama sekali,
        # kartunya tidak dirender.
        baris_kelas = ""
        for r in per_kelas:
            total_k = r["total"]
            masuk_k = r["masuk"] or 0
            persen_k = round(masuk_k * 100 / total_k) if total_k else 0
            angka_k = f"{masuk_k}/{total_k}" if total_k else "&mdash;"
            baris_kelas += (
                f'<div class="kelas-row"><span class="kelas-nama">{esc(r["nama"])}</span>'
                f'<span class="kelas-bar"><span style="width:{persen_k}%"></span></span>'
                f'<span class="kelas-angka">{angka_k}</span></div>')
        kartu_kelas = ""
        if per_kelas:
            kartu_kelas = (
                '<section class="card kelas-card"><h3>' + ikon_tema("buku")
                + ' Kehadiran per Kelas Hari Ini</h3>' + baris_kelas + "</section>")
        items = "".join(
            f'<li><span class="pill p-{r["status"].lower()}">{esc(r["status"])}</span> '
            f'<b>{esc(r["nama"])}</b> <span class="muted">{esc(r["sub"] or "")}</span> '
            f'<span class="jam">{esc(r["jam"] or "")}</span></li>' for r in datang)
        items = items or '<li class="muted">Belum ada yang scan hari ini.</li>'
        if panel_laporan is None:
            # Sesudah 17:00 slot panel bawah dikosongkan (tanpa judul,
            # tanpa kartu kosong); sebelum 09:00 tampil daftar kedatangan.
            panel_laporan = "" if panel_selesai else (
                '<section class="card"><h3>Kedatangan Terakhir Hari Ini</h3>\n'
                f'<ul class="arrivals">{items}</ul></section>')
        warn = ""
        if user["role"] == "admin" and verify_password("admin123", user["password_hash"], user["salt"]):
            warn = ('<div class="flash warn">PERINGATAN: akun admin ini masih memakai '
                    'password awal lokal. Segera ganti lewat menu Ganti Password '
                    'sebelum dipakai beneran.</div>')
        if user["role"] == "guru":
            # Wali kelas: tanpa kartu pencarian (halaman /cari terlarang)
            # dan kartu cepat hanya menuju Tabel Siswa kelasnya.
            kartu_cari = ""
            kartu_cepat = ('<div class="quick-grid">'
                           f'<a class="quick-card" href="/tabel-siswa">{icon("users")}'
                           '<span>Tabel Siswa</span></a></div>')
        else:
            kartu_cari = """<div class="card search-card">
          <form method="get" action="/cari" class="search-form">
            <label class="search-label">Pencarian
              <input name="q" placeholder="Cari nama siswa, NIS, kelas, nama guru, atau mata pelajaran…"></label>
            <button type="submit" class="btn btn-primary">Cari</button>
          </form>
        </div>"""
            kartu_cepat = f"""<div class="quick-grid">
          <a class="quick-card" href="/scan-gerbang">{icon('scan')}<span>Scan Gerbang</span></a>
          <a class="quick-card" href="/tabel-siswa">{icon('users')}<span>Tabel Siswa</span></a>
          <a class="quick-card" href="/tabel-guru">{icon('badge')}<span>Tabel Guru</span></a>
        </div>"""
        body = f"""<h2>Dashboard</h2>
        <p class="muted">{esc(NAMA_HARI[today.weekday()])}, {esc(fmt_tanggal(today))} &bull;
        batas Hadir {jam_hadir.strftime('%H:%M')}, Alpa otomatis {jam_alpa.strftime('%H:%M')}</p>
        {warn}{kartu_cari}
        <div class="stat-grid">{cards}</div>
        {kartu_cepat}
        {panel_laporan}
        {kartu_kelas}"""
        return self.send_html(layout(user, "Dashboard", body, flash, aktif="dashboard"))

    def _data_laporan(self, conn, tgl):
        """Data 'Laporan Hari Ini': siapa yang Terlambat dan siapa yang
        Belum Scan hari ini, siswa dan guru terpisah. 'Belum Scan' =
        hanya punya catatan Alpa OTOMATIS (metode 'auto'), atau belum
        punya catatan sama sekali. Dihitung langsung dari catatan hari
        ini (bukan snapshot). Scan sesudah 09:00 sudah ditutup
        (keputusan user), jadi daftar ini final: yang Belum Scan tetap
        Belum Scan. Yang Sakit/Izin tidak muncul di daftar mana pun
        (sudah ada keterangan). Dipakai bersama oleh panel dashboard
        (angka ringkasan pada dua kartunya) dan halaman /laporan/siswa
        & /laporan/guru (daftar namanya), supaya definisinya sama."""
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
        return {"sis_telat": sis_telat, "sis_belum": sis_belum,
                "gur_telat": gur_telat, "gur_belum": gur_belum}

    @staticmethod
    def _bagian_laporan(judul, baris, pakai_jam):
        """Satu bagian daftar laporan: judul + jumlahnya, lalu daftar
        bernomor (nama, keterangan, dan jam scan untuk yang Terlambat);
        bagian kosong menampilkan 'Tidak ada.'."""
        isi = "".join(
            f'<li><span class="no">{i}.</span> <b>{esc(r["nama"])}</b> '
            f'<span class="muted">{esc(r["sub"] or "")}</span>'
            + (f' <span class="jam">{esc(r["jam"] or "")}</span>' if pakai_jam else "")
            + "</li>"
            for i, r in enumerate(baris, 1))
        isi = isi or '<li class="muted">Tidak ada.</li>'
        return (f"<h4>{esc(judul)} ({len(baris)})</h4>"
                f'<ul class="plain-list">{isi}</ul>')

    def _panel_laporan(self, conn, tgl):
        """Panel 'Laporan Hari Ini' untuk dashboard mulai pukul 09:00:
        DUA kartu yang bisa diklik — kolom Siswa dan kolom Guru —
        masing-masing hanya memuat angka ringkasan hari ini (Terlambat
        & Belum Scan, dari _data_laporan). Daftar namanya TIDAK tampil
        di dashboard; dibuka lewat tautan kartunya: /laporan/siswa
        dan /laporan/guru."""
        d = self._data_laporan(conn, tgl)

        def kartu(href, nama, telat, belum):
            return (f'<a class="laporan-card" href="{href}">'
                    f'<span class="laporan-nama">{esc(nama)}</span>'
                    f'<span class="laporan-baris">Terlambat: <b>{telat}</b></span>'
                    f'<span class="laporan-baris">Belum Scan: <b>{belum}</b></span>'
                    '<span class="laporan-klik">Klik untuk lihat daftar '
                    '&rarr;</span></a>')

        return ('<section class="card laporan"><h3>Laporan Hari Ini</h3>'
                '<div class="laporan-grid">'
                + kartu("/laporan/siswa", "Siswa",
                        len(d["sis_telat"]), len(d["sis_belum"]))
                + kartu("/laporan/guru", "Guru",
                        len(d["gur_telat"]), len(d["gur_belum"]))
                + "</div></section>")

    def laporan_siswa(self, user, flash):
        return self._halaman_laporan(user, flash, "siswa")

    def laporan_guru(self, user, flash):
        return self._halaman_laporan(user, flash, "guru")

    def _halaman_laporan(self, user, flash, tipe):
        """Halaman daftar Laporan Hari Ini (dari kartu panel dashboard):
        dua bagian bernomor Terlambat & Belum Scan untuk siswa ATAU
        guru, khusus HARI INI. pastikan_alpa dipanggil dulu supaya
        daftar Belum Scan lengkap walau halaman dibuka langsung."""
        now = waktu_sekarang()
        today = now.date()
        tgl = today.strftime("%Y-%m-%d")
        conn = db()
        pastikan_alpa(conn, tgl)
        d = self._data_laporan(conn, tgl)
        conn.close()
        if tipe == "siswa":
            judul = "Laporan Siswa Hari Ini"
            isi = (self._bagian_laporan("Siswa Terlambat", d["sis_telat"], True)
                   + self._bagian_laporan("Siswa Belum Scan", d["sis_belum"], False))
        else:
            judul = "Laporan Guru Hari Ini"
            isi = (self._bagian_laporan("Guru Terlambat", d["gur_telat"], True)
                   + self._bagian_laporan("Guru Belum Scan", d["gur_belum"], False))
        body = f"""<h2>{esc(judul)}</h2>
        <p class="muted">{esc(NAMA_HARI[today.weekday()])}, {esc(fmt_tanggal(today))}</p>
        <section class="card laporan">{isi}</section>
        <p><a href="/dashboard">Kembali ke Dashboard</a></p>"""
        return self.send_html(layout(user, judul, body, flash, aktif="dashboard"))

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
        _jh, _ja, _jp = jam_absensi()
        body = f"""<h2>Scan Gerbang</h2>
        <p class="muted">{esc(NAMA_HARI[now.weekday()])}, {esc(fmt_tanggal(now.date()))} &bull;
        pukul {esc(now.strftime('%H:%M'))} WIB &bull; scan &le; {_jh.strftime('%H:%M')} = Hadir, lewat = Terlambat, scan ditutup {_ja.strftime('%H:%M')}</p>
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
        <script src="/static/jsQR.js"></script>"""
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
        jam_hadir_s, jam_alpa_s, _jp_s = jam_absensi(conn)
        if now.time() >= jam_alpa_s and (ada is None or ada["metode"] == "auto"):
            conn.close()
            return {
                "jenis": "error", "judul": "Scan sudah ditutup",
                "pesan": f"Scan sudah ditutup (lewat jam {jam_alpa_s.strftime('%H.%M')}). "
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
        status = "Hadir" if now.time() <= jam_hadir_s else "Terlambat"
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
        wali_row = info_wali(user)
        if wali_row:
            # Wali kelas TERKUNCI ke kelasnya sendiri: parameter kelas
            # lain diabaikan, pemilih kelas tidak dirender sama sekali.
            kelas = next((c for c in classes if c["id"] == wali_row["id"]), wali_row)
        else:
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
            f'<tr><td colspan="14"><div class="empty-state">{ikon_tema("siswa")}'
            f'<p>Belum ada siswa di kelas ini.</p>'
            f'<small>Siswa yang ditambahkan admin akan muncul di sini.</small></div></td></tr>')
        total_row = (f'<tr class="total-row"><td></td><td class="nama">Total per hari</td>'
                     f'{self._total_bawah(per_hari)}'
                     + "".join(f'<td class="tot">{sum(per_hari[j].get(st, 0) for j in range(6)) or ""}</td>'
                               for st in STATUS_LIST) + '</tr>')
        if wali_row:
            toolbar_kelas = f"""<div class="toolbar">
            <span>Kelas Anda: <b>{esc(kelas['nama'])}</b></span>
            <a class="btn btn-excel" href="/tabel-siswa/excel?kelas={kelas['id']}&minggu={senin.strftime('%Y-%m-%d')}">{icon('download')} Download Excel</a>
          </div>"""
        else:
            toolbar_kelas = f"""<form method="get" action="/tabel-siswa" class="toolbar">
            <label>Kelas <select name="kelas">{opts}</select></label>
            <button type="submit" class="btn btn-primary">Tampilkan</button>
            <a class="btn btn-excel" href="/tabel-siswa/excel?kelas={kelas['id']}&minggu={senin.strftime('%Y-%m-%d')}">{icon('download')} Download Excel</a>
          </form>"""
        body = f"""<h2>Tabel Siswa</h2>
        <div class="card toolbar-card">
          {toolbar_kelas}
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
        if user["role"] == "guru" and (
                tipe != "siswa" or orang["class_id"] != self.wali_kelas_id(user)):
            # Wali kelas: hanya siswa kelasnya sendiri yang boleh diubah;
            # siswa kelas lain / catatan guru -> seolah tidak ditemukan.
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
        if user["role"] == "guru" and (
                tipe != "siswa" or orang["class_id"] != self.wali_kelas_id(user)):
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
    def _buat_excel(self, judul, subjudul, label_kolom3, baris, hari_list, nama_sekolah="", kop=None, logo_path=None):
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
        # Kepala kop surat sekolah (logo + nama + alamat + kontak +
        # garis pemisah); off = jumlah baris kepala yang dipakai.
        off = _tulis_kop_excel(ws, nama_sekolah, kop, nkol, logo_path)
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
        wali_row = info_wali(user)
        if wali_row:
            # Wali kelas: Excel selalu untuk kelasnya sendiri.
            kelas = next((c for c in classes if c["id"] == wali_row["id"]), wali_row)
        else:
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
        peng = get_pengaturan()
        data = self._buat_excel("ABSENSI SISWA", f"Kelas: {kelas['nama']}", "NIS",
                                baris, hari_list,
                                nama_sekolah=peng["nama_sekolah"], kop=peng,
                                logo_path=_logo_kop_path(peng))
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
        peng = get_pengaturan()
        data = self._buat_excel("ABSENSI GURU", "Semua Guru", "Mapel", baris, hari_list,
                                nama_sekolah=peng["nama_sekolah"], kop=peng,
                                logo_path=_logo_kop_path(peng))
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
        wali_row = info_wali(user)
        if user["role"] == "admin":
            role_label = "Admin"
        elif wali_row:
            role_label = f"Wali Kelas {wali_row['nama']}"
        else:
            role_label = "Guru"
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
        wali_row = info_wali(user)
        if wali_row:
            # Wali kelas: hanya kartu siswa kelasnya sendiri — tanpa
            # pemilih kelas lain dan tanpa kartu guru.
            body = f"""<h2>Cetak Kartu QR</h2>
        <div class="menu-grid">
        <a class="menu-card" href="/cetak/kelas/{wali_row['id']}">{icon("users")}<span>Kartu Siswa &mdash; {esc(wali_row['nama'])}</span></a></div>
        <p class="muted"><small>Kartu berisi QR Code biasa (bukan QRIS pembayaran),
        di-scan di gerbang sekolah.</small></p>"""
            return self.send_html(layout(user, "Cetak Kartu", body, flash, aktif="cetak"))
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
        if not kelas or (user["role"] == "guru"
                         and class_id != self.wali_kelas_id(user)):
            # Untuk wali kelas, kelas lain diperlakukan seperti tidak ada.
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

    # -- kartu pelajar --------------------------------------------------------
    # Kartu pelajar dibuat OTOMATIS dari data siswa: begitu siswa diinput
    # (form Tambah Siswa atau import Excel per kelas), kartunya langsung
    # ada di menu ini — tidak ada langkah pembuatan kartu terpisah dan
    # tidak ada data kartu yang disimpan sendiri. Templat memakai kop
    # sekolah dari Pengaturan Tampilan (nama + logo + alamat + telepon +
    # email, yang sama dengan kop Excel): baris kop yang masih kosong
    # dilewati begitu saja.
    def kartu_pelajar_index(self, user, flash):
        conn = db()
        classes = conn.execute("SELECT * FROM classes ORDER BY nama").fetchall()
        conn.close()
        wali_row = info_wali(user)
        if wali_row:
            # Wali kelas: hanya kartu pelajar kelasnya sendiri.
            body = f"""<h2>Kartu Pelajar</h2>
        <div class="menu-grid">
        <a class="menu-card" href="/kartu-pelajar/kelas/{wali_row['id']}">{icon("cap")}<span>Kartu Pelajar &mdash; {esc(wali_row['nama'])}</span></a></div>
        <p class="muted"><small>Kartu dibuat otomatis dari data siswa &mdash; begitu siswa diinput, kartunya langsung ada di sini. Tinggal cetak.</small></p>"""
            return self.send_html(layout(user, "Kartu Pelajar", body, flash, aktif="kartu-pelajar"))
        links = "".join(
            f'<a class="menu-card" href="/kartu-pelajar/kelas/{c["id"]}">{icon("cap")}'
            f'<span>Kartu Pelajar &mdash; {esc(c["nama"])}</span></a>' for c in classes)
        links = links or '<p class="muted">Belum ada kelas.</p>'
        body = f"""<h2>Kartu Pelajar</h2>
        <p class="muted">Kartu dibuat <b>otomatis</b> dari data siswa &mdash; begitu siswa diinput (form atau import Excel), kartunya langsung ada di sini. Tidak perlu dibuat satu-satu.</p>
        <h3>Kartu Pelajar per Kelas</h3><div class="menu-grid">{links}</div>"""
        return self.send_html(layout(user, "Kartu Pelajar", body, flash, aktif="kartu-pelajar"))

    def _kartu_pelajar_html(self, st, kelas_nama, wali_nama, peng):
        """Satu kartu pelajar (ukuran kartu ID 85,6 x 54 mm, desain v3):
        kepala kop sekolah (logo + nama + alamat), kotak foto 3x4
        (aplikasi belum menyimpan foto siswa, jadi kotaknya untuk tempel
        foto), data siswa (nama, NIS, Tahun Ajaran dari Pengaturan,
        Jenis Kelamin), dan kaki berisi QR NIS. TANPA kolom Kelas dan
        TANPA Masa Berlaku di depan — sesuai desain Kartu Pelajar v3
        yang disetujui Bos (25 Nov 2026). kelas_nama/wali_nama tidak
        lagi tercetak di kartu (kelas tetap tampil di judul halaman)."""
        nama_sekolah = (peng["nama_sekolah"] or "Sekolah").strip()
        alamat = (peng["kop_alamat"] or "").strip()
        if peng["logo"]:
            logo_html = (f'<img class="kp-logo" src="/logo/{esc(peng["logo"])}" '
                         f'alt="Logo sekolah">')
        else:
            logo_html = f'<span class="kp-logo-fallback">{icon("logo")}</span>'
        alamat_html = (f'<div class="kp-alamat">{esc(alamat)}</div>'
                       if alamat else "")
        ta, _masa, _belakang = tahun_ajaran_info(peng)
        jk = (st["jenis_kelamin"] or "").strip() or "—"
        return f"""<div class="kpelajar">
          <div class="kp-head">{logo_html}<div class="kp-head-teks">
            <div class="kp-sekolah">{esc(nama_sekolah)}</div>{alamat_html}</div>
            <span class="kp-judul">KARTU PELAJAR</span></div>
          <div class="kp-gold"></div>
          <div class="kp-body">
            <div class="kp-foto">{icon("badge")}<span>FOTO<br>3 &times; 4</span></div>
            <div class="kp-data">
              <div class="kp-nama">{esc(st["nama"])}</div>
              <div class="kp-baris"><span class="kp-label">NIS</span><span class="kp-nilai">{esc(st["kode"])}</span></div>
              <div class="kp-baris"><span class="kp-label">Tahun Ajaran</span><span class="kp-nilai">{esc(ta)}</span></div>
              <div class="kp-baris"><span class="kp-label">Jenis Kelamin</span><span class="kp-nilai">{esc(jk)}</span></div>
            </div>
          </div>
          <div class="kp-foot">
            <img class="kp-qr" src="/qr/{urllib.parse.quote(st["kode"])}.png" alt="QR {esc(st["kode"])}">
            <div class="kp-ket">Kartu ini adalah tanda pengenal resmi siswa {esc(nama_sekolah)}.</div>
          </div>
        </div>"""

    def _kartu_belakang_html(self, peng, jenis):
        """Sisi belakang kartu pelajar / kartu guru: kepala kop sekolah,
        5 tata tertib penggunaan kartu, baris masa berlaku 3 tahun, dan
        blok tanda tangan Kepala Sekolah. 'jenis' = 'siswa' atau 'guru'
        (aturan 1, 4, 5 berbeda)."""
        nama_sekolah = (peng["nama_sekolah"] or "Sekolah").strip()
        alamat = (peng["kop_alamat"] or "").strip()
        telp = (peng["kop_telepon"] or "").strip()
        kontak_belakang = " &bull; ".join(
            x for x in ([esc(alamat)] if alamat else []) +
            ([f"Telp. {esc(telp)}"] if telp else []))
        if peng["logo"]:
            logo_html = (f'<img class="kp-logo" src="/logo/{esc(peng["logo"])}" '
                         f'alt="Logo sekolah">')
        else:
            logo_html = f'<span class="kp-logo-fallback">{icon("logo")}</span>'
        kontak_html = (f'<div class="kp-b-kontak">{kontak_belakang}</div>'
                       if kontak_belakang else "")
        _ta, _masa, belakang = tahun_ajaran_info(peng)
        if jenis == "guru":
            aturan = [
                f"Kartu ini adalah identitas resmi guru {nama_sekolah}.",
                "Wajib dibawa setiap hari saat berada di lingkungan sekolah.",
                "Dipakai untuk absensi di gerbang — pindai QR Code di kartu.",
                "Bila hilang atau rusak, segera lapor ke bagian tata usaha.",
                "Kartu tidak boleh dipindahtangankan ke guru lain."]
        else:
            aturan = [
                f"Kartu ini adalah identitas resmi siswa {nama_sekolah}.",
                "Wajib dibawa setiap hari saat berada di lingkungan sekolah.",
                "Dipakai untuk absensi di gerbang — pindai QR Code di kartu.",
                "Bila hilang atau rusak, segera lapor ke wali kelas.",
                "Kartu tidak boleh dipindahtangankan ke siswa lain."]
        items = "".join(f"<li>{esc(a)}</li>" for a in aturan)
        kelas_belakang = "kpelajar kp-belakang" + (" kp-guru" if jenis == "guru" else "")
        return f"""<div class="{kelas_belakang}">
          <div class="kp-b-head">{logo_html}<div class="kp-b-teks">
            <div class="kp-b-sekolah">{esc(nama_sekolah)}</div>{kontak_html}</div></div>
          <div class="kp-gold"></div>
          <div class="kp-b-body">
            <div class="kp-b-judul">TATA TERTIB PENGGUNAAN KARTU</div>
            <ol class="kp-tata">{items}</ol>
          </div>
          <div class="kp-b-foot">
            <div class="kp-b-masa">{esc(belakang)}</div>
            <div class="kp-b-ttd"><div>Kepala Sekolah,</div>
            <div class="kp-ttd-kosong"></div><div>( ........................ )</div></div>
          </div>
        </div>"""

    def _kelas_dan_wali(self, conn, class_id):
        return conn.execute(
            "SELECT c.*, u.nama AS wali_nama FROM classes c "
            "LEFT JOIN users u ON u.id = c.wali_id WHERE c.id=?",
            (class_id,)).fetchone()

    def kartu_pelajar_kelas(self, user, class_id, flash):
        conn = db()
        kelas = self._kelas_dan_wali(conn, class_id)
        if not kelas or (user["role"] == "guru"
                         and class_id != self.wali_kelas_id(user)):
            # Untuk wali kelas, kelas lain diperlakukan seperti tidak ada.
            conn.close()
            return self.send_html(layout(user, "404", "<p>Kelas tidak ditemukan.</p>"), status=404)
        students = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY nama",
                                (class_id,)).fetchall()
        conn.close()
        peng = get_pengaturan()
        cards = "".join(
            self._kartu_pelajar_html(st, kelas["nama"], kelas["wali_nama"], peng)
            + self._kartu_belakang_html(peng, "siswa")
            for st in students)
        body = f"""<h2 class="noprint">Kartu Pelajar - {esc(kelas["nama"])}</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;<a class="btn btn-ghost" href="/kartu-pelajar">Pilih Kelas Lain</a>
        &nbsp;Kartu dibuat otomatis dari data siswa ({len(students)} siswa). Kotak foto dikosongkan untuk tempel foto 3&times;4; QR di tiap kartu adalah NIS siswa untuk scan di gerbang. Sisi belakang berisi tata tertib &amp; masa berlaku kartu.</p>
        <div class="lembar-kp">{cards or '<p>Belum ada siswa di kelas ini.</p>'}</div>"""
        return self.send_html(layout(user, "Kartu Pelajar", body, flash, aktif="kartu-pelajar"))

    def kartu_pelajar_siswa(self, user, sid, flash):
        conn = db()
        st = conn.execute(
            "SELECT s.*, c.nama AS kelas_nama, u.nama AS wali_nama "
            "FROM students s JOIN classes c ON c.id = s.class_id "
            "LEFT JOIN users u ON u.id = c.wali_id WHERE s.id=?",
            (sid,)).fetchone()
        if not st or (user["role"] == "guru"
                      and st["class_id"] != self.wali_kelas_id(user)):
            conn.close()
            return self.send_html(layout(user, "404", "<p>Siswa tidak ditemukan.</p>"), status=404)
        conn.close()
        peng = get_pengaturan()
        card = (self._kartu_pelajar_html(st, st["kelas_nama"], st["wali_nama"], peng)
                + self._kartu_belakang_html(peng, "siswa"))
        body = f"""<h2 class="noprint">Kartu Pelajar - {esc(st["nama"])}</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;<a class="btn btn-ghost" href="/kartu-pelajar/kelas/{st["class_id"]}">Semua Kartu Kelas {esc(st["kelas_nama"])}</a>
        &nbsp;<a class="btn btn-ghost" href="/siswa/{st["id"]}">Kembali ke Detail Siswa</a></p>
        <div class="lembar-kp">{card}</div>"""
        return self.send_html(layout(user, "Kartu Pelajar", body, flash, aktif="kartu-pelajar"))

    # -- kartu guru -----------------------------------------------------------
    # Kartu guru dibuat OTOMATIS dari data guru (seperti Kartu Pelajar
    # untuk siswa): begitu guru diinput, kartunya langsung ada di menu
    # ini. Desain Kartu Guru v3 (judul KARTU GURU; Nama, Jabatan "Guru",
    # Guru Pelajaran = mata pelajaran, Jenis Kelamin; QR = kode_qr ASLI
    # guru dari database — bukan kode contoh; warna kepala hijau zamrud
    # + emas supaya langsung beda dari Kartu Pelajar navy/emas).
    # Admin melihat semua guru aktif; wali kelas hanya kartu dirinya
    # sendiri (kartu guru lain tidak boleh dibuka wali, sama seperti
    # /cetak/guru yang terlarang untuk wali).
    def _kartu_guru_html(self, guru, peng):
        """Satu kartu guru (ukuran kartu ID 85,6 x 54 mm, desain v3).
        Jabatan selalu 'Guru'; Guru Pelajaran = mata pelajaran guru
        (atau '—' bila kosong). QR = kode_qr asli guru untuk scan gerbang."""
        nama_sekolah = (peng["nama_sekolah"] or "Sekolah").strip()
        alamat = (peng["kop_alamat"] or "").strip()
        if peng["logo"]:
            logo_html = (f'<img class="kp-logo" src="/logo/{esc(peng["logo"])}" '
                         f'alt="Logo sekolah">')
        else:
            logo_html = f'<span class="kp-logo-fallback">{icon("logo")}</span>'
        alamat_html = (f'<div class="kp-alamat">{esc(alamat)}</div>'
                       if alamat else "")
        pelajaran = (guru["mapel"] or "").strip() or "—"
        kode = guru["kode_qr"] or ""
        jk = (guru["jenis_kelamin"] or "").strip() or "—"
        return f"""<div class="kpelajar kp-guru">
          <div class="kp-head">{logo_html}<div class="kp-head-teks">
            <div class="kp-sekolah">{esc(nama_sekolah)}</div>{alamat_html}</div>
            <span class="kp-judul">KARTU GURU</span></div>
          <div class="kp-gold"></div>
          <div class="kp-body">
            <div class="kp-foto">{icon("badge")}<span>FOTO<br>3 &times; 4</span></div>
            <div class="kp-data">
              <div class="kp-nama">{esc(guru["nama"])}</div>
              <div class="kp-baris"><span class="kp-label">Jabatan</span><span class="kp-nilai">Guru</span></div>
              <div class="kp-baris"><span class="kp-label">Guru Pelajaran</span><span class="kp-nilai">{esc(pelajaran)}</span></div>
              <div class="kp-baris"><span class="kp-label">Jenis Kelamin</span><span class="kp-nilai">{esc(jk)}</span></div>
            </div>
          </div>
          <div class="kp-foot">
            <img class="kp-qr" src="/qr/{urllib.parse.quote(kode)}.png" alt="QR {esc(kode)}">
            <div class="kp-ket">Kartu ini adalah tanda pengenal resmi guru {esc(nama_sekolah)}.</div>
          </div>
        </div>"""

    def kartu_guru_index(self, user, flash):
        conn = db()
        peng = get_pengaturan()
        if user["role"] == "guru":
            # Wali kelas: hanya kartu dirinya sendiri.
            guru = conn.execute(
                "SELECT * FROM users WHERE id=? AND role='guru' AND aktif=1",
                (user["id"],)).fetchone()
            conn.close()
            if not guru:
                return self.send_html(
                    layout(user, "Kartu Guru", "<p>Kartu tidak ditemukan.</p>"),
                    status=404)
            cards = (self._kartu_guru_html(guru, peng)
                     + self._kartu_belakang_html(peng, "guru"))
            body = f"""<h2>Kartu Guru</h2>
            <p class="muted">Ini kartu guru Anda &mdash; dibuat otomatis dari data guru. Tinggal cetak.</p>
            <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button></p>
            <div class="lembar-kp">{cards}</div>"""
            return self.send_html(layout(user, "Kartu Guru", body, flash, aktif="kartu-guru"))
        gurus = conn.execute(
            "SELECT * FROM users WHERE role='guru' AND aktif=1 ORDER BY nama").fetchall()
        conn.close()
        cards = "".join(
            self._kartu_guru_html(g, peng) + self._kartu_belakang_html(peng, "guru")
            for g in gurus)
        body = f"""<h2 class="noprint">Kartu Guru</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;Kartu dibuat otomatis dari data guru ({len(gurus)} guru aktif). QR di tiap kartu adalah kode guru untuk scan di gerbang. Sisi belakang berisi tata tertib &amp; masa berlaku kartu.</p>
        <div class="lembar-kp">{cards or '<p>Belum ada guru aktif.</p>'}</div>"""
        return self.send_html(layout(user, "Kartu Guru", body, flash, aktif="kartu-guru"))

    def kartu_guru_satu(self, user, gid, flash):
        conn = db()
        guru = conn.execute(
            "SELECT * FROM users WHERE id=? AND role='guru' AND aktif=1",
            (gid,)).fetchone()
        if not guru or (user["role"] == "guru" and gid != user["id"]):
            # Untuk wali kelas, kartu guru lain diperlakukan seperti tidak ada.
            conn.close()
            return self.send_html(layout(user, "404", "<p>Guru tidak ditemukan.</p>"), status=404)
        conn.close()
        peng = get_pengaturan()
        card = (self._kartu_guru_html(guru, peng)
                + self._kartu_belakang_html(peng, "guru"))
        body = f"""<h2 class="noprint">Kartu Guru - {esc(guru["nama"])}</h2>
        <p class="noprint"><button onclick="window.print()" class="btn btn-primary">Cetak Halaman Ini</button>
        &nbsp;<a class="btn btn-ghost" href="/kartu-guru">Semua Kartu Guru</a></p>
        <div class="lembar-kp">{card}</div>"""
        return self.send_html(layout(user, "Kartu Guru", body, flash, aktif="kartu-guru"))

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
# Warna menu (sidebar/topbar/nav bawah) punya pengaturan SENDIRI, terpisah
# dari warna latar halaman: kunci settings 'warna_menu'. Kosong = tampilan
# bawaan (menu putih). Preset diawali Putih sebagai pilihan bawaan/reset.
MENU_PRESETS = ["#ffffff"] + [w for w in BG_PRESETS if w != "#ffffff"]
TEKS_MENU_GELAP = "#1c2333"  # teks di atas warna menu yang terang (= --teks)
# Kejelasan wallpaper: kekuatan lapisan putih di atas gambar wallpaper
# (style.css .bg-wallpaper::after). Kunci settings 'kejelasan_wp' menyimpan
# nama tingkatnya; kosong/tidak sah = 'samar' (tampilan seperti sekarang).
KEJELASAN_WP = {"samar": "0.86", "sedang": "0.65", "jelas": "0.45"}
KEJELASAN_WP_DEFAULT = "samar"
# Tema pastel (Pengaturan Tampilan -> Tema): kunci settings 'tema'.
# Kosong = tampilan bawaan profesional seperti sekarang (tidak berubah);
# slug yang sah mengaktifkan palet pastel di style.css lewat kelas body
# .tema-<slug>. Palet lengkapnya tinggal di style.css; di sini hanya
# nama tampilannya + dua warna gradasi untuk chip pratinjau.
TEMA = {
    "pastel-pink": "Pastel Pink Oranye",
    "mint": "Mint Toska",
    "langit": "Biru Langit Peach",
    "lilac": "Lilac Pink",
    "krem": "Krem Coral",
    "lemon": "Pastel Lemon Mint",
    "melon": "Peach Melon",
    "senja": "Langit Senja",
    "matcha": "Matcha Susu",
    "rose": "Rose Berry",
    "karamel": "Karamel Susu",
}
TEMA_GRAD = {
    "pastel-pink": ("#f472a0", "#fb923c"),
    "mint": ("#34d399", "#2dd4bf"),
    "langit": ("#60a5fa", "#fdba74"),
    "lilac": ("#a78bfa", "#f472b6"),
    "krem": ("#fbbf24", "#fb7185"),
    "lemon": ("#fde047", "#34d399"),
    "melon": ("#fdba74", "#6ee7b7"),
    "senja": ("#38bdf8", "#fb923c"),
    "matcha": ("#a3e635", "#4ade80"),
    "rose": ("#fb7185", "#c084fc"),
    "karamel": ("#f2a950", "#d97706"),
}


def kejelasan_wp(peng):
    """Nama tingkat kejelasan wallpaper yang berlaku dari pengaturan
    (nama tersimpan yang sah, selain itu tingkat bawaan 'samar')."""
    nama = (peng.get("kejelasan_wp") or "").strip().lower()
    return nama if nama in KEJELASAN_WP else KEJELASAN_WP_DEFAULT


def _rgb_hex(hexs):
    return tuple(int(hexs[i:i + 2], 16) for i in (1, 3, 5))


def _luminansi_hex(hexs):
    """Luminansi relatif (WCAG) 0..1 dari warna #rrggbb."""
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb_hex(hexs)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def teks_di_atas(warna):
    """Warna teks yang terbaca di atas bidang `warna`: putih atau teks
    gelap aplikasi, dipilih yang kontras luminansinya lebih baik."""
    lum = _luminansi_hex(warna)
    kontras_putih = 1.05 / (lum + 0.05)
    kontras_gelap = (lum + 0.05) / (_luminansi_hex(TEKS_MENU_GELAP) + 0.05)
    return "#ffffff" if kontras_putih >= kontras_gelap else TEKS_MENU_GELAP


def gaya_menu(peng):
    """(blok <style> variabel warna menu, nama kelas body) untuk layout().

    Hanya bila admin sudah memilih warna menu; belum dipilih -> ("", "")
    sehingga tampilan bawaan tidak berubah sama sekali."""
    warna = (peng.get("warna_menu") or "").strip().lower()
    if not re.match(r"^#[0-9a-f]{6}$", warna):
        return "", ""
    teks = teks_di_atas(warna)
    r, g, b = _rgb_hex(teks)
    style = (f"<style>:root{{--warna-menu:{warna};--teks-menu:{teks};"
             f"--teks-menu-redup:rgba({r},{g},{b},.72);"
             f"--menu-hover:rgba({r},{g},{b},.12);"
             f"--menu-aktif:rgba({r},{g},{b},.25);}}</style>")
    return style, "menu-kustom"
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
WALLPAPER_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}
WALLPAPER_NAMA_RE = re.compile(r"^[0-9a-f]{24}\.(jpg|jpeg|png|webp)$")


TAHUN_AJARAN_RE = re.compile(r"^(\d{4})/(\d{4})$")
TAHUN_AJARAN_DEFAULT = "2026/2027"


def _jk_bersih(nilai):
    """Normalisasi jenis kelamin: 'Laki-laki', 'Perempuan', atau ''.
    Terima juga 'L'/'P', 'pria'/'wanita', 'male'/'female' (tak peduli
    huruf besar-kecil); selain itu jadi '' (belum diisi)."""
    v = (nilai or "").strip().lower()
    if v in ("laki-laki", "laki laki", "l", "pria", "male"):
        return "Laki-laki"
    if v in ("perempuan", "p", "wanita", "female"):
        return "Perempuan"
    return ""


def _jk_options(terpilih):
    """Opsi <option> Jenis Kelamin untuk form (terpilih = nilai saat ini)."""
    out = []
    for nilai, label in (("", "— Belum diisi —"), ("Laki-laki", "Laki-laki"),
                         ("Perempuan", "Perempuan")):
        sel = " selected" if (terpilih or "") == nilai else ""
        out.append(f'<option value="{nilai}"{sel}>{label}</option>')
    return "".join(out)


def _tahun_ajaran_sah(nilai):
    """'2026/2027' sah; tahun kedua harus = tahun pertama + 1."""
    m = TAHUN_AJARAN_RE.match((nilai or "").strip())
    return bool(m) and int(m.group(2)) == int(m.group(1)) + 1


def tahun_ajaran_info(peng):
    """(tahun_ajaran, masa_berlaku, masa_berlaku_belakang) untuk kartu.
    '2026/2027' -> masa '3 Tahun (2026 – 2029)' dan belakang
    'Masa berlaku 3 tahun — Tahun Ajaran 2026/2027 s.d. 2028/2029'."""
    ta = (peng.get("tahun_ajaran") or TAHUN_AJARAN_DEFAULT).strip()
    if not _tahun_ajaran_sah(ta):
        ta = TAHUN_AJARAN_DEFAULT
    awal = int(ta[:4])
    akhir = int(ta[5:])
    return (ta, f"3 Tahun ({awal} – {awal + 3})",
            f"Masa berlaku 3 tahun — Tahun Ajaran {ta} s.d. {awal + 2}/{akhir + 2}")


def get_pengaturan():
    """Baca pengaturan tampilan; selalu kembalikan dict lengkap dgn default."""
    peng = {"bg_color": BG_DEFAULT, "wallpaper": "", "nama_sekolah": "", "logo": "",
            "warna_menu": "", "kejelasan_wp": "", "tema": "",
            "wa_token": "", "kop_alamat": "", "kop_telepon": "", "kop_email": "",
            "tahun_ajaran": TAHUN_AJARAN_DEFAULT,
            "jam_batas_hadir": JAM_DEFAULT_STR["jam_batas_hadir"],
            "jam_batas_alpa": JAM_DEFAULT_STR["jam_batas_alpa"],
            "jam_panel_selesai": JAM_DEFAULT_STR["jam_panel_selesai"]}
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
    if peng["warna_menu"] and not re.match(r"^#[0-9a-fA-F]{6}$", peng["warna_menu"]):
        peng["warna_menu"] = ""
    if peng["kejelasan_wp"] and \
            peng["kejelasan_wp"].strip().lower() not in KEJELASAN_WP:
        peng["kejelasan_wp"] = ""
    peng["tema"] = (peng["tema"] or "").strip().lower()
    if peng["tema"] not in TEMA:
        peng["tema"] = ""
    for _k in ("jam_batas_hadir", "jam_batas_alpa", "jam_panel_selesai"):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", (peng[_k] or "").strip()):
            peng[_k] = JAM_DEFAULT_STR[_k]
        else:
            peng[_k] = peng[_k].strip()
    if not _tahun_ajaran_sah(peng["tahun_ajaran"]):
        peng["tahun_ajaran"] = TAHUN_AJARAN_DEFAULT
    else:
        peng["tahun_ajaran"] = peng["tahun_ajaran"].strip()
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
                tautan = (f'<span class="aksi-grup">'
                          f'<a class="btn-aksi" href="/siswa/{r["id"]}">Detail</a>'
                          f'<a class="btn-aksi" href="/admin/kelas/{r["class_id"]}">Lihat Kelas</a>'
                          f'<a class="btn-aksi" href="/admin/siswa/{r["id"]}/edit">Edit</a>'
                          f'</span>')
            else:
                tautan = (f'<span class="aksi-grup">'
                          f'<a class="btn-aksi" href="/siswa/{r["id"]}">Detail</a>'
                          f'<a class="btn-aksi" href="/tabel-siswa?kelas={r["class_id"]}">Lihat Kelas</a>'
                          f'</span>')
            items += (f'<tr><td class="no">{i + 1}</td>'
                      f'<td class="nama"><b>{esc(r["nama"])}</b></td>'
                      f'<td><code>{esc(r["kode"])}</code></td>'
                      f'<td>{esc(r["kelas"])}</td>'
                      f'<td class="aksi">{tautan}</td></tr>')
        bagian += ('<div class="card"><h3>Siswa</h3><div class="table-scroll">'
                   '<table class="tabel kelola"><thead><tr><th class="no">No</th>'
                   '<th class="nama">Nama</th><th>NIS</th><th>Kelas</th><th>Aksi</th>'
                   f'</tr></thead><tbody>{items}</tbody></table></div></div>')
    if guru_rows:
        items = ""
        for i, r in enumerate(guru_rows):
            tautan = (f'<span class="aksi-grup">'
                      f'<a class="btn-aksi" href="/admin/guru/{r["id"]}/edit">Edit</a>'
                      f'</span>' if admin else "")
            items += (f'<tr><td class="no">{i + 1}</td>'
                      f'<td class="nama"><b>{esc(r["nama"])}</b></td>'
                      f'<td>{esc(r["mapel"] or "—")}</td>'
                      f'<td><code>{esc(r["username"])}</code></td>'
                      f'<td><code>{esc(r["kode_qr"] or "-")}</code></td>'
                      f'<td class="aksi">{tautan}</td></tr>')
        bagian += ('<div class="card"><h3>Guru</h3><div class="table-scroll">'
                   '<table class="tabel kelola"><thead><tr><th class="no">No</th>'
                   '<th class="nama">Nama</th><th>Mapel</th><th>Username</th>'
                   '<th>Kode QR</th><th>Aksi</th>'
                   f'</tr></thead><tbody>{items}</tbody></table></div></div>')
    if not bagian:
        bagian = (f'<div class="card"><p class="muted">Tidak ada hasil untuk '
                  f'"{esc(q)}". Coba kata kunci lain.</p></div>')
    body = f"""<h2>Pencarian</h2>
    {form}
    {ringkasan}
    {bagian}"""
    return self.send_html(layout(user, "Pencarian", body, flash))


def _data_detail_siswa(conn, sid):
    """Data mentah Detail Siswa: (baris siswa, rekap, total, rincian).

    Dipakai bersama oleh halaman /siswa/<id> DAN unduhan Excel-nya
    (GET /siswa/<id>/unduh) agar isi berkas selalu persis sama dengan
    yang tampil di halaman. rekap: [(label bulan, [n per status]), ...]
    untuk 6 bulan kalender terakhir (bulan berjalan + 5 sebelumnya,
    Asia/Jakarta), urutan status Hadir, Terlambat, Sakit, Izin, Alpa;
    total: dict status -> jumlah; rincian: baris kehadiran (tanggal,
    status, jam) pada jendela yang sama, terbaru dulu. None bila siswa
    tidak ditemukan."""
    st = conn.execute(
        """SELECT s.*, c.nama AS kelas FROM students s
           JOIN classes c ON c.id = s.class_id WHERE s.id=?""",
        (sid,)).fetchone()
    if not st:
        return None
    hari_ini = waktu_sekarang().date()
    # Enam bulan kalender berurutan: 5 bulan lalu .. bulan berjalan.
    bulan_list = []
    y, m = hari_ini.year, hari_ini.month
    for _ in range(6):
        bulan_list.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    bulan_list.reverse()
    status_list = ("Hadir", "Terlambat", "Sakit", "Izin", "Alpa")
    total = {s: 0 for s in status_list}
    rekap = []
    for (y, m) in bulan_list:
        awal = f"{y:04d}-{m:02d}-01"
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        akhir = f"{y2:04d}-{m2:02d}-01"
        hitung = {r["status"]: r["c"] for r in conn.execute(
            """SELECT status, COUNT(*) AS c FROM kehadiran
               WHERE ref_type='siswa' AND ref_id=?
                 AND tanggal>=? AND tanggal<?
               GROUP BY status""", (sid, awal, akhir)).fetchall()}
        angka = []
        for s in status_list:
            n = hitung.get(s, 0)
            total[s] += n
            angka.append(n)
        rekap.append((f"{NAMA_BULAN[m - 1]} {y}", angka))
    # Rincian per catatan pada jendela 6 bulan yang sama, terbaru dulu.
    # Kolom Jam hanya terisi untuk catatan scan (Hadir/Terlambat);
    # Sakit/Izin/Alpa tampil "—" (keterangannya adalah tanggalnya).
    awal_window = f"{bulan_list[0][0]:04d}-{bulan_list[0][1]:02d}-01"
    y_ak, m_ak = bulan_list[-1]
    y_ak, m_ak = (y_ak + 1, 1) if m_ak == 12 else (y_ak, m_ak + 1)
    akhir_window = f"{y_ak:04d}-{m_ak:02d}-01"
    rincian_rows = conn.execute(
        """SELECT tanggal, status, jam FROM kehadiran
           WHERE ref_type='siswa' AND ref_id=? AND tanggal>=? AND tanggal<?
           ORDER BY tanggal DESC""",
        (sid, awal_window, akhir_window)).fetchall()
    return st, rekap, total, rincian_rows


def _siswa_detail(self, user, sid, flash):
    """Detail satu siswa: identitas + Rekap Absensi 6 Bulan Terakhir
    (bulan berjalan dan 5 bulan kalender sebelumnya, Asia/Jakarta),
    dihitung dari tabel kehadiran. Boleh dibuka admin & guru."""
    conn = db()
    data = _data_detail_siswa(conn, sid)
    if not data:
        conn.close()
        return self.redirect("/cari", "Data tidak ditemukan.")
    st, rekap, total, rincian_rows = data
    if user["role"] == "guru" and st["class_id"] != self.wali_kelas_id(user):
        # Wali kelas hanya boleh membuka siswa kelasnya sendiri.
        conn.close()
        return self.redirect("/dashboard", "Data tidak ditemukan.")
    conn.close()
    status_list = ("Hadir", "Terlambat", "Sakit", "Izin", "Alpa")
    baris = ""
    for label, angka in rekap:
        sel = "".join(f"<td>{n}</td>" for n in angka)
        baris += f'<tr><td class="nama">{label}</td>{sel}</tr>'
    rincian = ""
    for rr in rincian_rows:
        d = datetime.strptime(rr["tanggal"], "%Y-%m-%d").date()
        tgl_fmt = f"{NAMA_HARI[d.weekday()]}, {fmt_tanggal(d)}"
        jam_fmt = (esc(rr["jam"]) if rr["status"] in ("Hadir", "Terlambat")
                   and rr["jam"] else "&mdash;")
        rincian += (f'<tr><td class="nama">{tgl_fmt}</td>'
                    f'<td><span class="pill p-{rr["status"].lower()}">'
                    f'{esc(rr["status"])}</span></td>'
                    f'<td>{jam_fmt}</td></tr>')
    if rincian:
        rincian_html = ('<div class="card table-card"><h3>Rincian Kehadiran</h3>'
                        '<div class="table-scroll">'
                        '<table class="tabel"><thead><tr><th class="nama">Tanggal</th>'
                        '<th>Status</th><th>Jam</th></tr></thead>'
                        f'<tbody>{rincian}</tbody></table></div></div>')
    else:
        rincian_html = ('<div class="card"><h3>Rincian Kehadiran</h3>'
                        '<p class="muted">Belum ada catatan kehadiran pada '
                        '6 bulan terakhir.</p></div>')
    total_sel = "".join(f"<td>{total[s]}</td>" for s in status_list)
    edit = (f' <a class="btn-aksi" href="/admin/siswa/{st["id"]}/edit">Edit</a>'
            if user["role"] == "admin" else "")
    if user["role"] == "guru":
        bar_cari = ""
        kembali = ('<div class="card"><p style="margin:0">'
                   '<a class="btn-aksi" href="/tabel-siswa">Kembali ke tabel siswa</a>'
                   '</p></div>')
    else:
        bar_cari = """<form method="get" action="/cari" class="search-form kompak">
        <label class="search-label">Cari lagi
          <input name="q" placeholder="Cari nama siswa, NIS, kelas, nama guru, atau mata pelajaran…"></label>
        <button type="submit" class="btn btn-primary">Cari</button>
      </form>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0">
      """
        kembali = ('<div class="card"><p style="margin:0">'
                   '<a class="btn-aksi" href="/cari">Kembali ke pencarian</a>'
                   '</p></div>')
    chips = "".join(
        f'<div class="id-chip c-{s.lower()}"><span class="n">{total[s]}</span>'
        f'<span class="l">{s}</span></div>' for s in status_list)
    body = f"""<h2>Detail Siswa</h2>
    <div class="card">
      {bar_cari}<div class="id-flex">
        <div>
          <p class="id-nama"><b>{esc(st["nama"])}</b></p>
          <p class="muted" style="margin:0">NIS: <code>{esc(st["kode"])}</code> &mdash; Kelas: {esc(st["kelas"])}{edit}</p>
        </div>
        <a class="btn btn-excel" href="/siswa/{st['id']}/unduh">{icon("download")} Unduh Excel</a>
        <a class="btn" href="/kartu-pelajar/siswa/{st['id']}">{icon("cap")} Kartu Pelajar</a>
      </div>
      <p class="muted" style="margin:16px 0 0;font-size:12px">Total kehadiran 6 bulan terakhir:</p>
      <div class="id-total" style="margin-top:6px">{chips}</div>
    </div>
    <div class="card table-card"><h3>Rekap Absensi 6 Bulan Terakhir</h3>
    <div class="table-scroll">
    <table class="tabel"><thead><tr><th class="nama">Bulan</th>
    <th>Hadir</th><th>Terlambat</th><th>Sakit</th><th>Izin</th><th>Alpa</th></tr></thead>
    <tbody>{baris}<tr class="total-row"><td class="nama">Total</td>{total_sel}</tr></tbody></table>
    </div></div>
    {rincian_html}
    {kembali}"""
    return self.send_html(layout(user, "Detail Siswa", body, flash))


def _logo_kop_path(peng):
    """Jalur berkas logo sekolah untuk kop surat Excel, atau None bila
    belum ada logo terpasang."""
    nama = ((peng or {}).get("logo") or "").strip()
    if not nama:
        return None
    jalur = os.path.join(UPLOAD_DIR, nama)
    return jalur if os.path.isfile(jalur) else None


def _tempel_logo_kop(ws, logo_path):
    """Tempel logo sekolah di kiri atas kepala kop surat Excel.

    Logo dibatasi tingginya 60 px (lebar mengikuti rasio, maks 120 px)
    agar muat di blok kop. WebP diubah dulu ke PNG lewat Pillow karena
    Excel tidak menerima WebP. Mengembalikan True bila logo berhasil
    ditempel; bila berkas rusak/tak terbaca, kop tetap jalan tanpa logo
    (unduhan tidak boleh gagal hanya karena logo)."""
    try:
        from PIL import Image as PilImage
        from openpyxl.drawing.image import Image as XlImage
        with PilImage.open(logo_path) as im:
            im.load()
            lebar_asli, tinggi_asli = im.size
            if tinggi_asli <= 0 or lebar_asli <= 0:
                return False
            tinggi = 60
            lebar = round(tinggi * lebar_asli / tinggi_asli)
            if lebar > 120:
                lebar = 120
                tinggi = round(120 * tinggi_asli / lebar_asli)
            buf = io.BytesIO()
            im.convert("RGBA").save(buf, "PNG")
            buf.seek(0)
            img = XlImage(buf)
            img.width = lebar
            img.height = tinggi
            img.anchor = "A1"
            ws.add_image(img)
        return True
    except Exception:
        return False


def _tulis_kop_excel(ws, nama_sekolah, kop, nkol, logo_path=None):
    """Tulis kepala kop surat sekolah di baris atas lembar Excel.

    Dipakai semua unduhan Excel: logo sekolah di kiri atas (bila ada
    logo terpasang), nama sekolah (tebal 16, biru, tengah), lalu
    alamat, lalu baris "Telp: ... | Email: ...", lalu garis pemisah
    ganda seperti kop surat resmi. Baris yang kosong dilewati; bila
    tidak ada nama maupun isi kop, tidak menulis apa pun.
    Mengembalikan jumlah baris kepala yang dipakai (0 bila kosong)
    agar isi di bawahnya bisa bergeser."""
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    akhir = get_column_letter(nkol)
    alamat = ((kop or {}).get("kop_alamat") or "").strip()
    telp = ((kop or {}).get("kop_telepon") or "").strip()
    email = ((kop or {}).get("kop_email") or "").strip()
    nama = (nama_sekolah or "").strip()
    if not nama and not alamat and not telp and not email:
        return 0
    if logo_path and _tempel_logo_kop(ws, logo_path):
        ws.row_dimensions[1].height = 45
    r = 1
    if nama:
        ws.merge_cells(f"A{r}:{akhir}{r}")
        c = ws[f"A{r}"]
        c.value = nama
        c.font = Font(bold=True, size=16, color="1F3864")
        c.alignment = Alignment(horizontal="center", vertical="center")
        r += 1
    if alamat:
        ws.merge_cells(f"A{r}:{akhir}{r}")
        c = ws[f"A{r}"]
        c.value = alamat
        c.font = Font(size=11, color="404040")
        c.alignment = Alignment(horizontal="center", vertical="center")
        r += 1
    kontak = " | ".join(x for x in
                        ([f"Telp: {telp}"] if telp else []) +
                        ([f"Email: {email}"] if email else []))
    if kontak:
        ws.merge_cells(f"A{r}:{akhir}{r}")
        c = ws[f"A{r}"]
        c.value = kontak
        c.font = Font(size=11, color="404040")
        c.alignment = Alignment(horizontal="center", vertical="center")
        r += 1
    if alamat or telp or email:
        garis = Side(style="double", color="000000")
        for j in range(1, nkol + 1):
            ws.cell(row=r, column=j).border = Border(bottom=garis)
        ws.row_dimensions[r].height = 8
        r += 1
    return r - 1


def _buat_excel_detail_siswa(data, nama_sekolah, kop=None, logo_path=None):
    """Bangun .xlsx Detail Siswa dari data _data_detail_siswa.

    Lembar "Rekap 6 Bulan": kepala kop surat sekolah (logo di kiri
    atas bila ada logo terpasang + nama + alamat + kontak + garis
    pemisah, dari pengaturan) + judul "Detail Absensi Siswa" + blok
    identitas (Nama/NIS/Kelas), lalu tabel rekap persis
    halaman (Bulan, Hadir, Terlambat, Sakit, Izin, Alpa + baris Total
    tebal). Bila kop kosong, kepala kembali seperti dulu (nama sekolah
    saja di baris 1). Lembar "Rincian Kehadiran": baris rincian persis
    halaman (Tanggal, Status, Jam — Jam HH:MM untuk Hadir/Terlambat,
    "—" untuk Sakit/Izin/Alpa).
    Gaya mengikuti ekspor mingguan (_buat_excel): judul biru 1F3864,
    kepala tabel 203764 huruf putih, garis tipis, zebra, warna status."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    st, rekap, total, rincian_rows = data
    status_list = ("Hadir", "Terlambat", "Sakit", "Izin", "Alpa")
    tipis = Side(style="thin", color="9AA3B2")
    border = Border(left=tipis, right=tipis, top=tipis, bottom=tipis)
    fill_head = PatternFill("solid", fgColor="203764")
    font_head = Font(bold=True, color="FFFFFF", size=11)
    fill_status = {"Hadir": "C6EFCE", "Terlambat": "FFEB9C", "Sakit": "BDD7EE",
                   "Izin": "E4DFEC", "Alpa": "FFC7CE"}
    font_status = {"Hadir": "006100", "Terlambat": "9C6500", "Sakit": "1F4E79",
                   "Izin": "5B4A8A", "Alpa": "9C0006"}
    wb = Workbook()
    # --- lembar 1: Rekap 6 Bulan ---
    ws = wb.active
    ws.title = "Rekap 6 Bulan"
    # Kepala kop surat (logo + nama + alamat + kontak + garis
    # pemisah); bila kop kosong, baris 1 tetap dipakai nama sekolah
    # seperti dulu.
    off = _tulis_kop_excel(ws, nama_sekolah, kop, 6, logo_path)
    if off == 0:
        ws.merge_cells("A1:F1")
        c = ws["A1"]
        c.value = nama_sekolah
        c.font = Font(bold=True, size=16, color="1F3864")
        off = 1
    ws.merge_cells(f"A{1 + off}:F{1 + off}")
    c = ws[f"A{1 + off}"]
    c.value = "Detail Absensi Siswa"
    c.font = Font(bold=True, size=15, color="1F3864")
    for i, (label, nilai) in enumerate(
            (("Nama", st["nama"]), ("NIS", st["kode"]),
             ("Kelas", st["kelas"]))):
        r = 2 + off + i
        c = ws.cell(row=r, column=1, value=label)
        c.font = Font(bold=True, size=11, color="404040")
        ws.merge_cells(start_row=r, start_column=2,
                       end_row=r, end_column=6)
        ws.cell(row=r, column=2, value=nilai).font = Font(size=11)
    r_head = 6 + off
    for j, h in enumerate(["Bulan", *status_list], start=1):
        c = ws.cell(row=r_head, column=j, value=h)
        c.fill = fill_head
        c.font = font_head
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
    r = r_head + 1
    for i, (label, angka) in enumerate(rekap):
        zebra = (PatternFill("solid", fgColor="F2F5FA")
                 if i % 2 == 1 else None)
        c = ws.cell(row=r, column=1, value=label)
        c.border = border
        c.alignment = Alignment(horizontal="left", vertical="center")
        if zebra:
            c.fill = zebra
        for j, n in enumerate(angka, start=2):
            c = ws.cell(row=r, column=j, value=n)
            c.border = border
            c.alignment = Alignment(horizontal="center", vertical="center")
            if zebra:
                c.fill = zebra
        r += 1
    c = ws.cell(row=r, column=1, value="Total")
    c.font = Font(bold=True)
    c.border = border
    for j, s in enumerate(status_list, start=2):
        c = ws.cell(row=r, column=j, value=total[s])
        c.font = Font(bold=True)
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["A"].width = 22
    for j in range(2, 7):
        ws.column_dimensions[get_column_letter(j)].width = 11
    ws.freeze_panes = f"A{7 + off}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    # --- lembar 2: Rincian Kehadiran ---
    ws2 = wb.create_sheet("Rincian Kehadiran")
    for j, h in enumerate(["Tanggal", "Status", "Jam"], start=1):
        c = ws2.cell(row=1, column=j, value=h)
        c.fill = fill_head
        c.font = font_head
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
    r = 2
    for rr in rincian_rows:
        d = datetime.strptime(rr["tanggal"], "%Y-%m-%d").date()
        tgl_fmt = f"{NAMA_HARI[d.weekday()]}, {fmt_tanggal(d)}"
        jam_fmt = (rr["jam"] if rr["status"] in ("Hadir", "Terlambat")
                   and rr["jam"] else "—")
        c = ws2.cell(row=r, column=1, value=tgl_fmt)
        c.border = border
        c.alignment = Alignment(horizontal="left", vertical="center")
        c = ws2.cell(row=r, column=2, value=rr["status"])
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill("solid", fgColor=fill_status[rr["status"]])
        c.font = Font(color=font_status[rr["status"]], size=10)
        c = ws2.cell(row=r, column=3, value=jam_fmt)
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
        r += 1
    if not rincian_rows:
        ws2.merge_cells("A2:C2")
        c = ws2["A2"]
        c.value = "Belum ada catatan kehadiran pada 6 bulan terakhir."
        c.font = Font(color="667085", size=11)
    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 14
    ws2.column_dimensions["C"].width = 10
    ws2.freeze_panes = "A2"
    ws2.sheet_properties.pageSetUpPr.fitToPage = True
    ws2.page_setup.fitToWidth = 1
    ws2.page_setup.fitToHeight = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _siswa_detail_unduh(self, user, sid):
    """Unduh Detail Siswa sebagai .xlsx (GET /siswa/<id>/unduh).

    Isinya dibangun dari _data_detail_siswa — data yang sama persis
    dengan halaman detailnya — dan izinnya menyamai halaman itu:
    admin bebas; wali kelas hanya untuk siswa kelasnya sendiri
    (selain itu ditolak seperti halaman detail); siswa tak dikenal
    juga ditolak dengan cara yang sama."""
    conn = db()
    data = _data_detail_siswa(conn, sid)
    if not data:
        conn.close()
        return self.redirect("/cari", "Data tidak ditemukan.")
    st = data[0]
    if user["role"] == "guru" and st["class_id"] != self.wali_kelas_id(user):
        # Wali kelas hanya boleh mengunduh siswa kelasnya sendiri.
        conn.close()
        return self.redirect("/dashboard", "Data tidak ditemukan.")
    conn.close()
    peng = get_pengaturan()
    berkas = _buat_excel_detail_siswa(data, peng["nama_sekolah"], kop=peng,
                                      logo_path=_logo_kop_path(peng))

    def bersih(s):
        return re.sub(r"[^\w.\-]+", "_", str(s)).strip("_") or "siswa"

    nama_file = (f"Detail-Siswa-{bersih(st['nama'])}"
                 f"-{bersih(st['kode'])}.xlsx")
    return self.send_bytes(
        berkas,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=nama_file)


def _normal_no_wa(nomor):
    """Normalisasi nomor WA ke format internasional tanpa '+':
    08xx -> 628xx, 8xx -> 628xx, 628xx tetap. Kosong bila tak layak."""
    d = re.sub(r"\D", "", nomor or "")
    if d.startswith("0"):
        d = "62" + d[1:]
    elif d.startswith("8"):
        d = "62" + d
    return d if len(d) >= 10 else ""


def _link_kirim_wa(nomor, teks):
    """Link wa.me berisi pesan siap kirim. Bila nomor kosong/invalid,
    pakai tautan berbagi WhatsApp (admin memilih kontaknya sendiri)."""
    d = _normal_no_wa(nomor)
    q = urllib.parse.quote(teks)
    if d:
        return f"https://wa.me/{d}?text={q}"
    return f"https://api.whatsapp.com/send?text={q}"


def _pesan_wa_wali(nama, kelas, password, sekolah, url_login):
    return (f"Halo Bapak/Ibu {nama}, berikut password login Wali Kelas "
            f"{kelas} untuk aplikasi absensi {sekolah}.\n"
            f"Login: {url_login} lalu pilih kartu \"Login Wali Kelas\", "
            f"pilih kelas {kelas}, dan masukkan password di bawah ini.\n"
            f"Password: {password}\n"
            "Mohon ganti password setelah login pertama lewat menu "
            "Ganti Password. Terima kasih.")


WA_API_URL = os.environ.get("ABSEN_WA_API_URL", "https://api.fonnte.com/send")


def _kirim_wa_gateway(nomor_intl, pesan, token):
    """Kirim WA OTOMATIS lewat gateway Fonnte (permintaan user: klik
    Kirim WA langsung terkirim, tanpa membuka aplikasi WhatsApp).
    Mengembalikan (ok, keterangan). Token adalah token perangkat
    Fonnte milik admin yang tersimpan di settings (wa_token)."""
    body = urllib.parse.urlencode(
        {"target": nomor_intl, "message": pesan}).encode()
    req = urllib.request.Request(
        WA_API_URL, data=body,
        headers={"Authorization": token,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"tidak dapat menghubungi gateway WA ({e})"
    try:
        hasil = json.loads(data)
    except Exception:
        return False, "jawaban gateway tidak dikenal"
    if hasil.get("status"):
        return True, ""
    alasan = (hasil.get("reason") or hasil.get("detail")
              or hasil.get("message") or "gateway menolak pengiriman")
    return False, str(alasan)


def _admin_guru(self, user, flash):
    conn = db()
    rows = conn.execute(
        """SELECT u.*,
                  (SELECT COUNT(*) FROM kehadiran k WHERE k.ref_type='guru' AND k.ref_id=u.id)
                + (SELECT COUNT(*) FROM kehadiran k WHERE k.dicatat_oleh=u.id) AS jml_riwayat,
                  c.nama AS wali_kelas_nama
           FROM users u LEFT JOIN classes c ON c.wali_id = u.id
           WHERE u.role='guru' ORDER BY u.nama""").fetchall()
    peng_awal = get_pengaturan()
    nama_sekolah = (peng_awal["nama_sekolah"] or "").strip() or "sekolah"
    wa_token = (peng_awal["wa_token"] or "").strip()
    conn.close()
    host = self.headers.get("Host", "")
    skema = self.headers.get("X-Forwarded-Proto") or (
        "http" if host.startswith(("127.0.0.1", "localhost")) else "https")
    url_login = f"{skema}://{host}/login" if host else "/login"
    items = ""
    for i, r in enumerate(rows):
        nama_js = js_str(r["nama"])
        if r["wali_kelas_nama"]:
            # Guru yang sedang jadi wali: kolom Password menampilkan
            # HANYA password yang SEDANG berlaku (pass_sementara, selalu
            # sinkron: terbitan admin maupun hasil ganti sendiri oleh
            # wali) — tanpa username dan tanpa lencana wali (permintaan
            # user: kolom Username diganti kolom Password). Kosong hanya
            # untuk wali warisan dari sebelum fitur ini ada — tampilkan
            # "—" sampai admin menerbitkan yang baru.
            if r["pass_sementara"]:
                sel_user = f'<td><code>{esc(r["pass_sementara"])}</code></td>'
            else:
                sel_user = '<td>&mdash;</td>'
        else:
            # Bukan wali: belum punya login (login lahir dari penunjukan wali).
            sel_user = '<td>&mdash;</td>'
        sel_wa = f'<td>{esc(r["no_wa"])}</td>' if r["no_wa"] else '<td>&mdash;</td>'
        tombol_wa = ""
        if r["wali_kelas_nama"] and r["pass_sementara"]:
            if wa_token:
                # Gateway terhubung: tombol = form POST, server yang
                # mengirim otomatis (tanpa membuka WhatsApp).
                tombol_wa = (
                    f'<form method="post" action="/admin/guru/{r["id"]}/kirim-wa" '
                    f'class="inline"><button type="submit" class="btn-aksi">'
                    'Kirim WA</button></form>')
            else:
                pesan_wa = _pesan_wa_wali(r["nama"], r["wali_kelas_nama"],
                                          r["pass_sementara"], nama_sekolah,
                                          url_login)
                tombol_wa = (f'<a class="btn-aksi" target="_blank" rel="noopener" '
                             f'href="{esc(_link_kirim_wa(r["no_wa"], pesan_wa))}">'
                             'Kirim WA</a>')
        dasar = (f'<tr><td class="no">{i + 1}</td>'
                 f'<td class="nama"><b>{esc(r["nama"])}</b></td>'
                 f'<td>{esc(r["mapel"] or "-")}</td>'
                 f'{sel_user}'
                 f'{sel_wa}'
                 f'<td><code>{esc(r["kode_qr"] or "-")}</code></td>'
                 f'<td class="aksi"><span class="aksi-grup">'
                 f'{tombol_wa}'
                 f'<a class="btn-aksi" href="/admin/guru/{r["id"]}/edit">Edit</a>')
        if not r["aktif"]:
            items += (dasar + '(nonaktif) '
                      f'<form method="post" action="/admin/guru/{r["id"]}/aktifkan" class="inline">'
                      f'<button type="submit" class="btn btn-ghost btn-sm">Aktifkan</button></form></span></td></tr>')
        elif r["jml_riwayat"]:
            confirm_msg = (f"Nonaktifkan guru {nama_js}? Guru ini sudah punya riwayat kehadiran, "
                           f"jadi akunnya akan dinonaktifkan (bukan dihapus) dan riwayat "
                           f"kehadirannya tetap tersimpan.")
            items += (dasar +
                      f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                      f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                      f'<button type="submit" class="btn-aksi">Hapus</button></form></span></td></tr>')
        else:
            confirm_msg = f"Hapus guru {nama_js}? Akun guru ini akan dihapus permanen."
            items += (dasar +
                      f'<form method="post" action="/admin/guru/{r["id"]}/hapus" class="inline" '
                      f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
                      f'<button type="submit" class="btn-aksi">Hapus</button></form></span></td></tr>')
    items = items or '<tr><td colspan="7" class="muted">Belum ada guru.</td></tr>'
    body = f"""<h2>Kelola Guru</h2>
    <form method="post" action="/admin/guru/kirim-wa-semua"
      onsubmit="return confirm('Kirim password ke SEMUA wali kelas yang punya password &amp; nomor WA sekarang?')"
      style="margin:0 0 12px">
      <button type="submit" class="btn btn-primary">Kirim WA Semua</button>
      <span class="muted"><small>Sekali klik, password terkirim barengan ke
      semua guru yang sedang jadi wali kelas (yang nomor WA-nya kosong
      dilewati &amp; dilaporkan). Butuh gateway WA terhubung di
      Pengaturan Tampilan.</small></span></form>
    <div class="card table-card"><div class="table-scroll">
    <table class="tabel kelola"><thead><tr><th class="no">No</th><th class="nama">Nama</th>
    <th>Mapel</th><th>Password</th><th>No WA</th><th>Kode QR</th><th>Aksi</th></tr></thead>
    <tbody>{items}</tbody></table></div></div>
    <div class="card form-card"><h3>Tambah Guru</h3>
    <form method="post" action="/admin/guru">
      <label>Nama <input name="nama" required></label>
      <label>Mata Pelajaran <input name="mapel" required placeholder="mis. Matematika"></label>
      <label>Jenis Kelamin <select name="jenis_kelamin">{_jk_options("")}</select></label>
      <label>Nomor WA <input name="no_wa" inputmode="tel" placeholder="mis. 0812xxxxxxx (boleh kosong)"></label>
      <p class="muted"><small>Guru di sini adalah data induk (nama + mapel).
      Login dibuat otomatis hanya saat guru ditunjuk sebagai wali kelas
      di menu Kelola Kelas.</small></p>
      <button type="submit" class="btn btn-primary">Tambah Guru</button></form>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:18px 0 6px">
    <h4 style="margin:6px 0 4px">Atau Upload Data Guru dari Excel</h4>
    <p class="muted" style="margin:0 0 8px">Tambah banyak guru sekaligus dari
    file Excel (.xlsx) dengan kolom: <b>Nama | Mata Pelajaran | Nomor WA | Jenis Kelamin</b>
    (baris pertama judul kolom; Nomor WA dan Jenis Kelamin boleh dikosongkan). Guru dengan
    nama yang sudah terdaftar akan dilewati, tidak digandakan.</p>
    <p style="margin:0 0 4px"><a class="btn btn-ghost" href="/admin/guru/template">{icon('download')} Unduh Template Excel Guru</a></p>
    <form method="post" action="/admin/guru/import" enctype="multipart/form-data">
      <label>File Excel guru (.xlsx) <input type="file" name="berkas"
        accept=".xlsx" required></label>
      <button type="submit" class="btn btn-primary">Upload &amp; Tambah Guru</button></form></div>"""
    return self.send_html(layout(user, "Guru", body, flash, aktif="guru"))


def _post_admin_guru(self, f):
    # Guru = data induk saja (nama + mapel). Username/password TIDAK dibuat
    # di sini: kolom username diisi penanda internal unik "guru-<id>" dan
    # password di-hash dari rahasia acak yang tidak pernah ditampilkan —
    # kredensial sungguhan lahir hanya lewat penunjukan wali kelas.
    ph, salt = hash_password(secrets.token_urlsafe(24))
    conn = db()
    cur = conn.execute(
        "INSERT INTO users (username,password_hash,salt,role,nama,mapel,no_wa,jenis_kelamin) VALUES (?,?,?,'guru',?,?,?,?)",
        ("baru-" + secrets.token_hex(8), ph, salt,
         f["nama"].strip(), f.get("mapel", "").strip(),
         f.get("no_wa", "").strip(), _jk_bersih(f.get("jenis_kelamin", ""))))
    gid = cur.lastrowid
    conn.execute("UPDATE users SET username=?, kode_qr=? WHERE id=?",
                 (f"guru-{gid}", kode_qr_guru_baru(conn, gid), gid))
    conn.commit(); conn.close()
    return self.redirect("/admin/guru", "Guru ditambahkan.")


def _get_guru(self, conn, gid):
    return conn.execute("SELECT * FROM users WHERE id=? AND role='guru'", (gid,)).fetchone()


def _admin_guru_edit(self, user, gid, flash):
    conn = db()
    g = self._get_guru(conn, gid)
    wali_row = conn.execute("SELECT * FROM classes WHERE wali_id=?",
                            (gid,)).fetchone() if g else None
    conn.close()
    if not g:
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    if wali_row:
        pw_tampil = esc(g["pass_sementara"]) if g["pass_sementara"] else "&mdash;"
        tombol_wa_edit = ""
        if g["pass_sementara"]:
            peng2 = get_pengaturan()
            sekolah2 = (peng2["nama_sekolah"] or "").strip() or "sekolah"
            token2 = (peng2["wa_token"] or "").strip()
            host2 = self.headers.get("Host", "")
            skema2 = self.headers.get("X-Forwarded-Proto") or (
                "http" if host2.startswith(("127.0.0.1", "localhost")) else "https")
            url2 = f"{skema2}://{host2}/login" if host2 else "/login"
            if token2:
                tombol_wa_edit = (
                    f'<form method="post" action="/admin/guru/{g["id"]}/kirim-wa" '
                    'style="margin-top:10px"><button type="submit" '
                    'class="btn btn-primary">Kirim Password via WA</button></form>'
                    '<p class="muted"><small>Gateway WA terhubung: sekali '
                    'klik, password <b>langsung terkirim otomatis</b> ke '
                    'nomor WA guru ini &mdash; tanpa membuka WhatsApp. '
                    'Pastikan nomor WA guru sudah terisi di form Edit '
                    'Guru di atas.</small></p>')
            else:
                pesan2 = _pesan_wa_wali(g["nama"], wali_row["nama"],
                                        g["pass_sementara"], sekolah2, url2)
                tombol_wa_edit = (
                    f'<p style="margin-top:10px"><a class="btn btn-primary" '
                    f'target="_blank" rel="noopener" '
                    f'href="{esc(_link_kirim_wa(g["no_wa"], pesan2))}">'
                    'Kirim Password via WA</a></p>'
                    '<p class="muted"><small>WhatsApp terbuka ke nomor guru '
                    'ini dengan pesan berisi password yang sudah terisi; '
                    'tinggal tekan kirim. Agar sekali klik langsung '
                    'terkirim otomatis, hubungkan gateway WA di menu '
                    'Pengaturan Tampilan.</small></p>')
        blok_login = f"""
    <div class="card form-card"><h3>Login Wali Kelas</h3>
      <label>Username <input value="{esc(g['username'])}" readonly disabled></label>
      <p class="muted"><small>PW: {pw_tampil} — password yang sedang
      berlaku untuk login wali ini; selalu diperbarui, termasuk bila
      wali menggantinya sendiri.</small></p>
      <p class="muted"><small>Guru ini adalah wali kelas
      <b>{esc(wali_row['nama'])}</b>. Username dibuat otomatis dari nama
      kelas dan tidak bisa diubah. Bila perlu mengganti passwordnya,
      buat password baru lewat tombol di bawah.</small></p>
      <form method="post" action="/admin/guru/{g['id']}/password-baru">
        <button type="submit" class="btn btn-primary">Buat Password Baru</button></form>
      {tombol_wa_edit}</div>"""
    else:
        blok_login = """
    <div class="card"><p class="muted">Guru ini belum bisa login. Login
    dibuat otomatis saat guru ditunjuk sebagai wali kelas di menu
    Kelola Kelas.</p></div>"""
    body = f"""<h2>Edit Guru</h2>
    <form method="post" action="/admin/guru/{g['id']}/edit" class="card form-card">
      <label>Nama <input name="nama" value="{esc(g['nama'])}" required></label>
      <label>Mata Pelajaran <input name="mapel" value="{esc(g['mapel'] or '')}" required></label>
      <label>Jenis Kelamin <select name="jenis_kelamin">{_jk_options(g['jenis_kelamin'])}</select></label>
      <label>Nomor WA <input name="no_wa" value="{esc(g['no_wa'] or '')}" inputmode="tel" placeholder="mis. 0812xxxxxxx (boleh kosong)"></label>
      <label>Kode QR guru <input value="{esc(g['kode_qr'] or '')}" readonly disabled></label>
      <button type="submit" class="btn btn-primary">Simpan Perubahan</button></form>
    {blok_login}
    <p><a href="/admin/guru">Kembali ke daftar guru</a></p>"""
    return self.send_html(layout(user, "Edit Guru", body, flash, aktif="guru"))


def _post_admin_guru_edit(self, gid, f):
    # Hanya data induk yang diubah; username/password login wali TIDAK
    # disentuh di sini (username lahir dari penunjukan wali, password
    # diganti lewat tombol "Buat Password Baru" / oleh wali sendiri).
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    conn.execute("UPDATE users SET nama=?, mapel=?, no_wa=?, jenis_kelamin=? WHERE id=?",
                 (f["nama"].strip(), f.get("mapel", "").strip(),
                  f.get("no_wa", "").strip(), _jk_bersih(f.get("jenis_kelamin", "")), gid))
    conn.commit(); conn.close()
    return self.redirect("/admin/guru", "Guru diperbarui.")


def _post_admin_guru_password_baru(self, gid):
    """Buat password acak BARU untuk guru yang sedang jadi wali kelas;
    ditampilkan ke admin satu kali lewat pesan flash. Guru yang bukan
    wali tidak punya login, jadi ditolak."""
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    wali_row = conn.execute("SELECT * FROM classes WHERE wali_id=?",
                            (gid,)).fetchone()
    if not wali_row:
        conn.close()
        return self.redirect("/admin/guru",
                             "Guru ini bukan wali kelas; password login "
                             "hanya untuk wali kelas.")
    pw = password_acak()
    ph, salt = hash_password(pw)
    conn.execute("UPDATE users SET password_hash=?, salt=?, pass_sementara=? "
                 "WHERE id=?", (ph, salt, pw, gid))
    conn.commit(); conn.close()
    return self.redirect(
        "/admin/guru",
        f"Password baru untuk {g['nama']} (wali kelas {wali_row['nama']}): "
        f"{pw} (sampaikan ke gurunya; password bisa diganti setelah login).")


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
        # Penugasan walinya TETAP disimpan: begitu diaktifkan lagi, login
        # walinya berlaku lagi (login tetap tertahan oleh aktif=0 selama
        # nonaktif).
        conn.execute("UPDATE users SET aktif=0 WHERE id=?", (gid,))
        conn.commit(); conn.close()
        return self.redirect("/admin/guru",
                             "Guru dinonaktifkan karena sudah punya riwayat kehadiran. "
                             "Riwayat kehadirannya tetap tersimpan.")
    # Hapus permanen: lepaskan dulu penugasan wali (FK classes.wali_id),
    # kelasnya menjadi tanpa wali.
    conn.execute("UPDATE classes SET wali_id=NULL WHERE wali_id=?", (gid,))
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
            f'<span class="aksi-grup">'
            f'<a class="btn-aksi" href="{action}/{r["id"]}/edit">Edit</a>'
            f'<form method="post" action="{action}/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn-aksi">Hapus</button></form></span></li>')
    items = items or '<li class="muted">Belum ada data.</li>'
    body = f"""<h2>{esc(title)}</h2>
    <div class="card"><ul class="plain-list">{items}</ul></div>
    <div class="card form-card"><h3>Tambah {esc(label)}</h3>
    <form method="post" action="{action}">
      <label>Nama baru <input name="nama" required></label>
      <button type="submit" class="btn btn-primary">Tambah</button></form></div>"""
    return self.send_html(layout(user, title, body, flash, aktif=aktif_key))


def _opsi_wali(self, conn, kelas_id=None, nilai_kosong="0"):
    """HTML <option> untuk pemilih Wali Kelas: pilihan kosong
    "— Belum ada —" + semua guru AKTIF ("Nama — Mapel"), KECUALI
    guru yang sudah menjadi wali kelas LAIN (satu guru maksimal satu
    kelas). Wali kelas ini sendiri tetap tampil dan terpilih — termasuk
    bila sedang nonaktif, agar penugasannya tidak hilang diam-diam.
    nilai_kosong: nilai untuk pilihan kosong — "0" (bawaan, untuk form
    biasa) atau "" (untuk dropdown sebaris di tabel Kelola Kelas, yang
    endpoint-nya membaca form dengan keep_blank_values)."""
    rows = conn.execute(
        "SELECT id, nama, mapel FROM users WHERE role='guru' AND aktif=1 "
        "ORDER BY nama").fetchall()
    terpakai = {r["wali_id"]: r["id"] for r in conn.execute(
        "SELECT id, wali_id FROM classes WHERE wali_id IS NOT NULL").fetchall()}
    cur = None
    if kelas_id:
        r = conn.execute("SELECT wali_id FROM classes WHERE id=?",
                         (kelas_id,)).fetchone()
        cur = r["wali_id"] if r else None
    # Nilai kosong bawaan memakai sentinel "0": parse_qs membuang nilai
    # kosong, jadi value="" tidak akan pernah sampai ke server lewat
    # form() biasa. Dropdown sebaris memakai nilai_kosong="" karena
    # endpoint-nya membaca form dengan keep_blank_values (form_lengkap).
    sel_kosong = " selected" if nilai_kosong == "" and not cur else ""
    opts = (f'<option value="{nilai_kosong}"{sel_kosong}>'
            "&mdash; Belum ada &mdash;</option>")
    tampil = set()
    for g in rows:
        if terpakai.get(g["id"]) not in (None, kelas_id):
            continue
        tampil.add(g["id"])
        label = (f'{esc(g["nama"])} &mdash; {esc(g["mapel"])}'
                 if g["mapel"] else esc(g["nama"]))
        opts += (f'<option value="{g["id"]}"'
                 f'{" selected" if g["id"] == cur else ""}>{label}</option>')
    if cur and cur not in tampil:
        g = conn.execute("SELECT nama, mapel FROM users WHERE id=?",
                         (cur,)).fetchone()
        if g:
            label = (f'{esc(g["nama"])} &mdash; {esc(g["mapel"])}'
                     if g["mapel"] else esc(g["nama"]))
            opts += f'<option value="{cur}" selected>{label} (nonaktif)</option>'
    return opts


def _wali_dari_form(self, conn, f, kelas_id):
    """Baca & validasi pilihan wali dari form kelas.
    Kembalikan (dikirim, wali_id, pesan_error): dikirim=False bila field
    wali_id TIDAK ada di form (penugasan tidak diubah sama sekali);
    wali_id=None berarti "— Belum ada —" (penugasan dicabut)."""
    if "wali_id" not in f:
        return False, None, ""
    mentah = (f.get("wali_id") or "").strip()
    if not mentah or mentah == "0":
        # Pilihan "— Belum ada —" (sentinel "0" dari _opsi_wali).
        return True, None, ""
    try:
        gid = int(mentah)
    except ValueError:
        return True, None, "Pilihan wali kelas tidak valid."
    g = conn.execute("SELECT * FROM users WHERE id=? AND role='guru'",
                     (gid,)).fetchone()
    if not g:
        return True, None, "Guru untuk wali kelas tidak ditemukan."
    cur = None
    if kelas_id:
        r = conn.execute("SELECT wali_id FROM classes WHERE id=?",
                         (kelas_id,)).fetchone()
        cur = r["wali_id"] if r else None
    if not g["aktif"] and gid != cur:
        return True, None, "Guru nonaktif tidak bisa ditunjuk sebagai wali kelas."
    lain = conn.execute("SELECT nama FROM classes WHERE wali_id=? AND id<>?",
                        (gid, kelas_id or 0)).fetchone()
    if lain:
        return True, None, f"Guru itu sudah menjadi wali kelas {lain['nama']}."
    return True, gid, ""


def _tetapkan_wali(self, conn, kelas, wali_baru):
    """Terapkan perubahan wali pada baris kelas `kelas` (dict/row dengan
    id, nama, wali_id LAMA). Bila walinya BERUBAH:
    - wali lama: username diparkir ke "guru-<id>" (membebaskan nama
      wali<slug>) — loginnya mati juga oleh penjaga wali di setiap login
      & permintaan — dan pass_sementara dikosongkan (tidak ada lagi
      password yang perlu ditampilkan untuknya);
    - wali baru: username := "wali" + slug(nama kelas) (bila bentrok
      dengan pengguna lain, tambah id kelas), password := acak 10
      karakter (hash-nya untuk login, teks biasanya ke pass_sementara
      untuk tampilan admin), dan pesan flash kredensial dikembalikan
      untuk ditampilkan ke admin SATU kali.
    Wali TIDAK berubah -> tidak menyentuh apa pun, kembalikan None."""
    lama = kelas["wali_id"]
    if lama == wali_baru:
        return None
    if lama:
        conn.execute("UPDATE users SET username=?, pass_sementara='' WHERE id=?",
                     (f"guru-{lama}", lama))
    if not wali_baru:
        conn.execute("UPDATE classes SET wali_id=NULL WHERE id=?",
                     (kelas["id"],))
        return (f"Wali kelas {kelas['nama']} dicabut; login guru "
                "sebelumnya sudah tidak berlaku.")
    g = conn.execute("SELECT * FROM users WHERE id=? AND role='guru'",
                     (wali_baru,)).fetchone()
    uname = "wali" + slug_kelas(kelas["nama"])
    bentrok = conn.execute("SELECT id FROM users WHERE username=? AND id<>?",
                           (uname, wali_baru)).fetchone()
    if bentrok:
        uname = f"{uname}{kelas['id']}"
    pw = password_acak()
    ph, salt = hash_password(pw)
    conn.execute("UPDATE users SET username=?, password_hash=?, salt=?, "
                 "pass_sementara=? WHERE id=?",
                 (uname, ph, salt, pw, wali_baru))
    conn.execute("UPDATE classes SET wali_id=? WHERE id=?",
                 (wali_baru, kelas["id"]))
    return (f"Wali kelas {kelas['nama']}: {g['nama']} — username {uname}, "
            f"password sementara: {pw} (sampaikan ke gurunya; password "
            "bisa diganti setelah login).")


def _admin_kelas(self, user, flash):
    conn = db()
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM students s WHERE s.class_id=c.id) AS jml,
                  u.nama AS wali_nama
           FROM classes c LEFT JOIN users u ON u.id = c.wali_id
           ORDER BY c.nama""").fetchall()
    opsi_wali = self._opsi_wali(conn, None)
    # Opsi dropdown wali SEBARIS per kelas: kolom Wali Kelas adalah
    # <select> nama guru yang langsung menunjuk/mengganti/mencabut wali
    # kelas itu saat diganti (POST /admin/kelas/<id>/wali).
    opsi_baris = {r["id"]: self._opsi_wali(conn, r["id"], nilai_kosong="")
                  for r in rows}
    conn.close()
    items = ""
    for i, r in enumerate(rows):
        confirm_msg = f"Hapus kelas {js_str(r['nama'])}?"
        sel_wali = (
            f'<form method="post" action="/admin/kelas/{r["id"]}/wali" '
            f'class="inline"><select name="wali_id" class="wali-pilih" '
            f'onchange="this.form.submit()">{opsi_baris[r["id"]]}'
            "</select></form>")
        items += (
            f'<tr><td class="no">{i + 1}</td>'
            f'<td class="nama"><b><a href="/admin/kelas/{r["id"]}">{esc(r["nama"])}</a></b></td>'
            f'<td class="jml">{r["jml"]}</td>'
            f'<td>{sel_wali}</td>'
            f'<td class="aksi"><span class="aksi-grup">'
            f'<a class="btn-aksi" href="/admin/kelas/{r["id"]}/edit">Edit</a>'
            f'<form method="post" action="/admin/kelas/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn-aksi">Hapus</button></form></span></td></tr>')
    items = items or '<tr><td colspan="5" class="muted">Belum ada data.</td></tr>'
    body = f"""<h2>Kelola Kelas</h2>
    <div class="card table-card"><div class="table-scroll">
    <table class="tabel kelola"><thead><tr><th class="no">No</th><th class="nama">Nama Kelas</th>
    <th>Jumlah Siswa</th><th>Wali Kelas</th><th>Aksi</th></tr></thead>
    <tbody>{items}</tbody></table></div></div>
    <div class="card form-card"><h3>Tambah Kelas</h3>
    <form method="post" action="/admin/kelas">
      <label>Nama baru <input name="nama" required></label>
      <label>Wali Kelas <select name="wali_id">{opsi_wali}</select></label>
      <p class="muted"><small>Memilih wali kelas langsung membuatkan login
      untuk guru itu (username &amp; password acak ditampilkan satu kali
      sesudah disimpan). Wali juga bisa ditunjuk, diganti, atau dicabut
      langsung dari kolom Wali Kelas pada tabel di atas.</small></p>
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
    conn = db()
    dikirim, wali_baru, err = self._wali_dari_form(conn, f, None)
    if err:
        conn.close()
        return self.redirect("/admin/kelas", err)
    try:
        cur = conn.execute("INSERT INTO classes (nama) VALUES (?)",
                           (f["nama"].strip(),))
    except sqlite3.IntegrityError:
        conn.close()
        return self.redirect("/admin/kelas", "Nama sudah ada.")
    kid = cur.lastrowid
    pesan = None
    if dikirim and wali_baru:
        pesan = self._tetapkan_wali(
            conn, {"id": kid, "nama": f["nama"].strip(), "wali_id": None},
            wali_baru)
    conn.commit(); conn.close()
    return self.redirect("/admin/kelas", pesan or "Data ditambahkan.")


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
    conn = db()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    conn.close()
    if not row:
        return self.redirect("/admin/kelas", "Data tidak ditemukan.")
    body = f"""<h2>Edit Kelas</h2>
    <form method="post" action="/admin/kelas/{row['id']}/edit" class="card form-card">
      <label>Nama <input name="nama" value="{esc(row['nama'])}" required></label>
      <p class="muted"><small>Wali kelas TIDAK diatur di sini: penunjukan,
      penggantian, dan pencabutan wali dilakukan langsung dari kolom
      Wali Kelas pada daftar kelas (pilih nama guru pada baris kelasnya).</small></p>
      <button type="submit" class="btn btn-primary">Simpan Perubahan</button></form>
    <p><a href="/admin/kelas">Kembali ke daftar kelas</a></p>"""
    return self.send_html(layout(user, "Edit Kelas", body, flash, aktif="kelas"))


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
    # Hanya nama kelas yang diubah di sini. Penugasan wali TIDAK disentuh
    # sama sekali (field wali_id — bila ikut terkirim — diabaikan):
    # wali diatur lewat POST /admin/kelas/<id>/wali dari dropdown pada
    # kolom Wali Kelas di daftar kelas.
    conn = db()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    if not row:
        conn.close()
        return self.redirect("/admin/kelas", "Data tidak ditemukan.")
    try:
        conn.execute("UPDATE classes SET nama=? WHERE id=?",
                     (f["nama"].strip(), kid))
    except sqlite3.IntegrityError:
        conn.rollback(); conn.close()
        return self.redirect(f"/admin/kelas/{kid}/edit", "Nama sudah ada.")
    conn.commit(); conn.close()
    return self.redirect("/admin/kelas", "Kelas diperbarui.")


def _post_admin_kelas_wali(self, kid, f):
    """Terapkan pilihan dari dropdown Wali Kelas SEBARIS pada tabel
    Kelola Kelas (POST /admin/kelas/<id>/wali, field wali_id; nilai ""
    = pilihan "— Belum ada —"). Memakai mesin yang sama persis dengan
    alur penugasan wali yang sudah ada: _wali_dari_form memvalidasi
    (guru harus ada, harus aktif, dan belum menjadi wali kelas LAIN —
    pelanggaran ditolak dengan pesan kesalahan) dan _tetapkan_wali
    menerapkan perubahannya (wali baru: username wali<slug> + password
    acak yang teks biasanya ikut disimpan ke pass_sementara dan
    ditampilkan SATU kali lewat flash; wali lama: username diparkir,
    pass_sementara dikosongkan, login mati seketika; pilihan yang sama
    dengan wali saat ini = tidak menyentuh apa pun)."""
    conn = db()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    if not row:
        conn.close()
        return self.redirect("/admin/kelas", "Data tidak ditemukan.")
    dikirim, wali_baru, err = self._wali_dari_form(conn, f, kid)
    if err:
        conn.close()
        return self.redirect("/admin/kelas", err)
    if not dikirim:
        conn.close()
        return self.redirect("/admin/kelas",
                             "Pilihan wali kelas tidak valid.")
    pesan = self._tetapkan_wali(conn, row, wali_baru)
    conn.commit(); conn.close()
    return self.redirect(
        "/admin/kelas",
        pesan or f"Wali kelas {row['nama']} tidak berubah.")


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
        if row["wali_id"]:
            # Kelas terhapus = penugasan walinya ikut berakhir: kosongkan
            # password tampilan walinya (loginnya mati oleh penjaga wali
            # karena kelasnya sudah tidak ada).
            conn.execute("UPDATE users SET pass_sementara='' WHERE id=?",
                         (row["wali_id"],))
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
    for i, r in enumerate(rows):
        confirm_msg = (f"Hapus siswa {js_str(r['nama'])}? "
                       "Catatan kehadirannya juga akan terhapus.")
        items += (
            f'<tr><td class="no">{i + 1}</td>'
            f'<td class="nama"><b>{esc(r["nama"])}</b></td>'
            f'<td><code>{esc(r["kode"])}</code></td>'
            f'<td class="aksi"><span class="aksi-grup">'
            f'<a class="btn-aksi" href="/admin/siswa/{r["id"]}/edit">Edit</a>'
            f'<form method="post" action="/admin/siswa/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn-aksi">Hapus</button></form></span></td></tr>')
    if items:
        daftar = ('<div class="card table-card"><div class="table-scroll">'
                  '<table class="tabel kelola"><thead><tr><th class="no">No</th>'
                  '<th class="nama">Nama</th><th>NIS</th><th>Aksi</th></tr></thead>'
                  f'<tbody>{items}</tbody></table></div></div>')
    else:
        daftar = ('<div class="card"><div class="empty-state">'
                  + ikon_tema("tas")
                  + '<p>Belum ada siswa di kelas ini.</p>'
                    '<small>Siswa yang ditambahkan akan muncul di sini.</small>'
                    '</div></div>')
    body = f"""<h2>Kelas {esc(kelas["nama"])} &mdash; {len(rows)} siswa</h2>
    {daftar}
    <div class="card form-card"><h3>Tambah Siswa</h3>
    <form method="post" action="/admin/siswa">
      <label>Nama <input name="nama" required></label>
      <label>NIS <input name="kode" required></label>
      <input type="hidden" name="class_id" value="{kelas['id']}">
      <label>Jenis Kelamin <select name="jenis_kelamin">{_jk_options("")}</select></label>
      <button type="submit" class="btn btn-primary">Tambah Siswa</button></form>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:18px 0 6px">
    <h4 style="margin:6px 0 4px">Atau Upload Data Siswa Kelas {esc(kelas["nama"])} dari Excel</h4>
    <p class="muted" style="margin:0 0 8px">Tambah banyak siswa ke kelas
    <b>{esc(kelas["nama"])}</b> sekaligus dari file Excel (.xlsx) dengan
    kolom: <b>Nama | NIS | Jenis Kelamin</b> (baris pertama judul kolom;
    Jenis Kelamin boleh dikosongkan). Semua siswa di
    file ini masuk ke kelas ini &mdash; tidak perlu kolom Kelas. NIS yang
    sudah terdaftar akan dilewati, tidak digandakan.</p>
    <p style="margin:0 0 4px"><a class="btn btn-ghost" href="/admin/kelas/{kelas['id']}/template">{icon('download')} Unduh Template Excel Siswa Kelas Ini</a></p>
    <form method="post" action="/admin/kelas/{kelas['id']}/import" enctype="multipart/form-data">
      <label>File Excel siswa (.xlsx) <input type="file" name="berkas"
        accept=".xlsx" required></label>
      <button type="submit" class="btn btn-primary">Upload &amp; Tambah ke Kelas Ini</button></form></div>
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
            f'<tr><td class="no">{i + 1}</td>'
            f'<td class="nama"><b>{esc(r["nama"])}</b></td>'
            f'<td><code>{esc(r["kode"])}</code></td>'
            f'<td>{esc(r["kelas"])}</td>'
            f'<td class="aksi"><span class="aksi-grup">'
            f'<a class="btn-aksi" href="/siswa/{r["id"]}">Detail</a>'
            f'<a class="btn-aksi" href="/admin/siswa/{r["id"]}/edit">Edit</a>'
            f'<form method="post" action="/admin/siswa/{r["id"]}/hapus" class="inline" '
            f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
            f'<button type="submit" class="btn-aksi">Hapus</button></form></span></td></tr>')
    items = items or '<tr><td colspan="5" class="muted">Belum ada siswa.</td></tr>'
    body = f"""<h2>Kelola Siswa</h2>
    <div class="card table-card"><div class="table-scroll">
    <table class="tabel kelola"><thead><tr><th class="no">No</th><th class="nama">Nama</th>
    <th>NIS</th><th>Kelas</th><th>Aksi</th></tr></thead>
    <tbody>{items}</tbody></table></div></div>
    <div class="card form-card"><h3>Tambah Siswa</h3>
    <form method="post" action="/admin/siswa">
      <label>Nama <input name="nama" required></label>
      <label>NIS <input name="kode" required></label>
      <label>Kelas <select name="class_id">{class_opts}</select></label>
      <label>Jenis Kelamin <select name="jenis_kelamin">{_jk_options("")}</select></label>
      <button type="submit" class="btn btn-primary">Tambah Siswa</button></form></div>"""
    return self.send_html(layout(user, "Siswa", body, flash, aktif="siswa"))


def _post_admin_siswa(self, f):
    try:
        conn = db()
        conn.execute("INSERT INTO students (nama,kode,class_id,jenis_kelamin) VALUES (?,?,?,?)",
                     (f["nama"].strip(), f["kode"].strip(), int(f["class_id"]),
                      _jk_bersih(f.get("jenis_kelamin", ""))))
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
      <label>Jenis Kelamin <select name="jenis_kelamin">{_jk_options(st['jenis_kelamin'])}</select></label>
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
        conn.execute("UPDATE students SET nama=?, kode=?, class_id=?, jenis_kelamin=? WHERE id=?",
                     (f["nama"].strip(), f["kode"].strip(), int(f["class_id"]),
                      _jk_bersih(f.get("jenis_kelamin", "")), sid))
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
    # Warna menu: tampilkan Putih sebagai pilihan tercentang selama belum
    # pernah dipilih (kosong = bawaan putih, belum tersimpan apa pun).
    menu_ini = peng["warna_menu"].lower() or "#ffffff"
    swatches_menu = ""
    for warna in MENU_PRESETS:
        cek = " checked" if menu_ini == warna.lower() else ""
        swatches_menu += (f'<label class="swatch" title="{warna}">'
                          f'<input type="radio" name="warna_menu" value="{warna}"{cek}>'
                          f'<span style="background:{warna}"></span></label>')
    cek_menu_kustom = "" if menu_ini in [w.lower() for w in MENU_PRESETS] else " checked"
    # Kejelasan wallpaper: radio tiga tingkat; yang tercentang = tingkat
    # yang berlaku ('samar' selama belum pernah diatur).
    jelas_ini = kejelasan_wp(peng)
    radio_jelas = ""
    for nilai_j, label_j in (("samar", "Samar (seperti sekarang)"),
                             ("sedang", "Sedang"),
                             ("jelas", "Jelas")):
        cek_j = " checked" if jelas_ini == nilai_j else ""
        radio_jelas += (f'<label><input type="radio" name="kejelasan_wp" '
                        f'value="{nilai_j}"{cek_j}> {label_j}</label>')
    # Tema: dua belas pilihan (Default + 11 tema pastel), masing-masing dengan
    # chip pratinjau gradasi warnanya; yang tercentang = tema yang
    # sedang berlaku ('' = Default).
    tema_ini = peng["tema"]
    pilihan_tema = [("", "Default", "Tampilan profesional saat ini",
                     "#4f46e5", "#2563eb")]
    for slug_t, nama_t in TEMA.items():
        g1_t, g2_t = TEMA_GRAD[slug_t]
        pilihan_tema.append((slug_t, nama_t, "", g1_t, g2_t))
    radio_tema = ""
    for slug_t, nama_t, ket_t, g1_t, g2_t in pilihan_tema:
        cek_t = " checked" if tema_ini == slug_t else ""
        ket_html = f"<small>{esc(ket_t)}</small>" if ket_t else ""
        radio_tema += (
            f'<label class="tema-pilih">'
            f'<input type="radio" name="tema" value="{slug_t}"{cek_t}>'
            f'<span class="tema-chip" '
            f'style="background:linear-gradient(135deg,{g1_t},{g2_t})"></span>'
            f'<span class="tema-nama">{esc(nama_t)}{ket_html}</span></label>')
    if (peng["wa_token"] or "").strip():
        ekor = esc(peng["wa_token"].strip()[-4:])
        wa_status = (f'<p><b style="color:#15803d">Terhubung</b> '
                     f'<span class="muted">(token tersimpan, berakhiran '
                     f'&bull;&bull;&bull;&bull;{ekor})</span></p>')
        wa_putus = ('''<form method="post" action="/admin/tampilan/wa-gateway/putus"
          class="inline" onsubmit="return confirm('Putuskan gateway WA? Tombol Kirim WA kembali membuka WhatsApp (tidak otomatis).')">
          <button type="submit" class="btn btn-danger">Putuskan Gateway</button></form>''')
    else:
        wa_status = ('<p><b style="color:#b45309">Belum terhubung</b> '
                     '<span class="muted">(tombol Kirim WA masih membuka '
                     'WhatsApp)</span></p>')
        wa_putus = ""
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
    <div class="card form-card"><h3>Jam Absensi</h3>
    <p class="muted">Jam masuk tiap sekolah bisa berbeda, jadi batas jam ini
    bisa diubah. Scan sampai Batas Hadir masih dihitung <b>Hadir</b>; lewat itu
    sampai sebelum Batas Alpa dihitung <b>Terlambat</b>; yang belum scan saat
    Batas Alpa tercapai menjadi <b>Alpa otomatis</b> dan scan sesudahnya ditutup.
    Panel Laporan Hari Ini di dashboard tampil mulai Batas Alpa dan hilang
    mulai jam Panel Laporan selesai.</p>
    <form method="post" action="/admin/tampilan/jam-absen">
      <label>Batas Hadir
        <input type="time" name="jam_batas_hadir" value="{esc(peng['jam_batas_hadir'])}" required></label>
      <label>Batas Alpa otomatis &amp; scan ditutup
        <input type="time" name="jam_batas_alpa" value="{esc(peng['jam_batas_alpa'])}" required></label>
      <label>Panel Laporan Hari Ini hilang mulai
        <input type="time" name="jam_panel_selesai" value="{esc(peng['jam_panel_selesai'])}" required></label>
      <p class="muted"><small>Urutannya harus: Batas Hadir lebih awal dari
      Batas Alpa, dan Batas Alpa lebih awal dari jam panel selesai.
      Perubahan langsung berlaku untuk scan, Alpa otomatis, dan dashboard
      &mdash; tanpa perlu memasang ulang aplikasi.</small></p>
      <button type="submit" class="btn btn-primary">Simpan Jam Absensi</button></form></div>
    <div class="card form-card"><h3>WhatsApp — Kirim Password Otomatis (Fonnte)</h3>
    {wa_status}
    <p class="muted">Agar tombol <b>Kirim WA</b> di Kelola Guru sekali klik
    <b>langsung terkirim otomatis</b> ke WA wali kelas (tanpa membuka
    aplikasi WhatsApp), hubungkan akun WhatsApp admin lewat layanan
    Fonnte: (1) daftar di <b>fonnte.com</b>, (2) hubungkan perangkat
    dengan memindai kode QR memakai WhatsApp admin, (3) salin
    <b>token</b> perangkatnya dan tempel di sini, lalu simpan.
    Sesudah itu pengiriman berjalan dari server aplikasi memakai nomor
    WA admin yang terhubung di Fonnte.</p>
    <form method="post" action="/admin/tampilan/wa-gateway">
      <label>Token Fonnte <input type="password" name="wa_token"
        autocomplete="off" placeholder="tempel token perangkat Fonnte di sini"></label>
      <p class="muted"><small>Token tersimpan aman di pengaturan aplikasi
      dan tidak ditampilkan lagi (hanya status terhubung/belum). Kosongkan
      halaman ini tidak mengubah token; untuk mengganti, tempel token
      baru lalu simpan.</small></p>
      <button type="submit" class="btn btn-primary">Simpan &amp; Hubungkan</button></form>
    {wa_putus}</div>
    <div class="card form-card"><h3>Tema</h3>
    <p class="muted">Tema pastel membuat tampilan lebih lembut: latar
    berwarna pastel, sudut kartu lebih bulat, dan aksen gradasi.
    Selama tema aktif, warna latar dan warna menu mengikuti tema.
    Wallpaper tetap tampil bila dipasang. Pilih Default untuk kembali
    ke tampilan profesional seperti sekarang.</p>
    <form method="post" action="/admin/tampilan/tema">
      <div class="tema-grid">{radio_tema}</div>
      <button type="submit" class="btn btn-primary">Simpan Tema</button></form></div>
    <div class="card form-card"><h3>Nama Sekolah &amp; Warna Latar</h3>
    <form method="post" action="/admin/tampilan">
      <label>Nama sekolah (tampil di menu &amp; judul Excel)
        <input name="nama_sekolah" value="{esc(peng['nama_sekolah'])}"
               placeholder="mis. SMA Negeri 1 Contoh"></label>
      <p class="muted"><small><b>Kop surat sekolah</b> &mdash; tampil
      sebagai kepala di atas setiap unduhan Excel (absensi mingguan
      &amp; detail siswa). Kosongkan bila tidak mau memakai kop
      surat.</small></p>
      <label>Alamat sekolah (kop surat)
        <input name="kop_alamat" value="{esc(peng['kop_alamat'])}"
               placeholder="mis. Jl. Merdeka No. 10, Jakarta"></label>
      <label>Telepon sekolah (kop surat)
        <input name="kop_telepon" value="{esc(peng['kop_telepon'])}"
               placeholder="mis. (021) 1234567"></label>
      <label>Email sekolah (kop surat)
        <input name="kop_email" value="{esc(peng['kop_email'])}"
               placeholder="mis. info@sekolah.sch.id"></label>
      <label>Tahun Ajaran (tampil di Kartu Pelajar &amp; Kartu Guru)
        <input name="tahun_ajaran" value="{esc(peng['tahun_ajaran'])}"
               placeholder="mis. 2026/2027"></label>
      <p class="muted"><small>Tahun Ajaran menentukan Masa Berlaku kartu:
      kartu berlaku 3 tahun sejak tahun pertama (mis. 2026/2027 &rarr;
      2026 &ndash; 2029). Ubah tiap tahun ajaran baru.</small></p>
      <label>Warna latar aplikasi</label>
      <div class="swatches">{swatches}
        <label class="swatch kustom" title="Warna kustom">
          <input type="radio" name="bg_color" value="kustom"{cek_kustom}>
          <input type="color" name="bg_color_custom" value="{esc(peng['bg_color'])}">
        </label>
      </div>
      <p class="muted"><small>Pilih warna contoh lalu Simpan, atau pakai
      pemilih warna di ujung kanan &mdash; warna kustom langsung diterapkan
      begitu dipilih.</small></p>
      <button type="submit" class="btn btn-primary">Simpan Pengaturan</button></form></div>
    <div class="card form-card"><h3>Warna Menu</h3>
    <p class="muted">Warna panel menu: menu samping di komputer, bilah atas,
    dan menu bawah di HP. Terpisah dari warna latar halaman di atas &mdash;
    mengubah yang satu tidak mengubah yang lain. Pilih Putih untuk tampilan
    bawaan. Warna teks menu menyesuaikan otomatis agar tetap terbaca.</p>
    <form method="post" action="/admin/tampilan/warna-menu">
      <label>Warna panel menu</label>
      <div class="swatches">{swatches_menu}
        <label class="swatch kustom" title="Warna kustom">
          <input type="radio" name="warna_menu" value="kustom"{cek_menu_kustom}>
          <input type="color" name="warna_menu_custom" value="{esc(menu_ini)}">
        </label>
      </div>
      <p class="muted"><small>Pilih warna contoh lalu Simpan, atau pakai
      pemilih warna di ujung kanan &mdash; warna kustom langsung diterapkan
      begitu dipilih.</small></p>
      <button type="submit" class="btn btn-primary">Simpan Warna Menu</button></form></div>
    <div class="card form-card"><h3>Wallpaper Sekolah</h3>
    <p class="muted">Gambar menjadi latar seluruh aplikasi (termasuk halaman login),
    di balik lapisan terang agar teks tetap terbaca. Format JPG/PNG/WebP, maks &plusmn;5 MB.
    Kosongkan untuk latar warna saja.</p>
    {wp_html}
    <form method="post" action="/admin/tampilan/wallpaper" enctype="multipart/form-data">
      <label>Unggah / ganti wallpaper <input type="file" name="wallpaper"
        accept=".jpg,.jpeg,.png,.webp" required></label>
      <button type="submit" class="btn btn-primary">Unggah Wallpaper</button></form>
    <form method="post" action="/admin/tampilan/kejelasan-wp">
      <label>Kejelasan wallpaper</label>
      <div class="pilihan-radio">{radio_jelas}</div>
      <p class="muted"><small>Seberapa jelas gambar wallpaper terlihat di
      balik lapisan terang. Samar = tampilan seperti sekarang; Jelas =
      gambar paling menonjol. Berlaku juga untuk wallpaper yang baru
      diunggah nanti.</small></p>
      <button type="submit" class="btn btn-primary">Simpan Kejelasan</button></form></div>
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
    ta = (f.get("tahun_ajaran") or "").strip()
    if ta and not _tahun_ajaran_sah(ta):
        # Nilai tidak sah: ditolak, pengaturan lama tidak berubah.
        return self.redirect("/admin/tampilan",
                             "Tahun Ajaran tidak valid; pakai format 2026/2027. "
                             "Pengaturan tidak berubah.")
    conn = db()
    set_pengaturan(conn, "bg_color", warna.lower())
    set_pengaturan(conn, "nama_sekolah", (f.get("nama_sekolah") or "").strip())
    if ta:
        set_pengaturan(conn, "tahun_ajaran", ta)
    for _k in ("kop_alamat", "kop_telepon", "kop_email"):
        set_pengaturan(conn, _k, (f.get(_k) or "").strip()[:200])
    conn.commit(); conn.close()
    return self.redirect("/admin/tampilan", "Pengaturan tampilan disimpan.")


def _post_admin_tampilan_warna_menu(self, f):
    warna = (f.get("warna_menu") or "").strip()
    if warna == "kustom":
        warna = (f.get("warna_menu_custom") or "").strip()
    if not re.match(r"^#[0-9a-fA-F]{6}$", warna):
        # Nilai tidak sah: ditolak, pengaturan lama tidak berubah.
        return self.redirect("/admin/tampilan",
                             "Warna menu tidak valid; pengaturan tidak berubah.")
    conn = db()
    set_pengaturan(conn, "warna_menu", warna.lower())
    conn.commit(); conn.close()
    return self.redirect("/admin/tampilan", "Warna menu disimpan.")


def _post_admin_tampilan_kejelasan_wp(self, f):
    nama = (f.get("kejelasan_wp") or "").strip().lower()
    if nama not in KEJELASAN_WP:
        # Nilai tidak sah: ditolak, pengaturan lama tidak berubah.
        return self.redirect("/admin/tampilan",
                             "Kejelasan wallpaper tidak valid; "
                             "pengaturan tidak berubah.")
    conn = db()
    set_pengaturan(conn, "kejelasan_wp", nama)
    conn.commit(); conn.close()
    return self.redirect("/admin/tampilan", "Kejelasan wallpaper disimpan.")


def _post_admin_tampilan_tema(self, f):
    slug = (f.get("tema") or "").strip().lower()
    if slug and slug not in TEMA:
        # Nilai tidak sah: ditolak, pengaturan lama tidak berubah.
        return self.redirect("/admin/tampilan",
                             "Tema tidak valid; pengaturan tidak berubah.")
    conn = db()
    set_pengaturan(conn, "tema", slug)
    conn.commit(); conn.close()
    return self.redirect("/admin/tampilan", "Tema disimpan.")


def _post_admin_tampilan_wa_gateway(self, f):
    """Simpan token gateway WA (Fonnte) dari Pengaturan Tampilan.
    Token kosong ditolak agar token lama tidak terhapus tak sengaja;
    memutus koneksi memakai tombol Putuskan Gateway tersendiri."""
    token = (f.get("wa_token") or "").strip()
    if not token:
        return self.redirect(
            "/admin/tampilan",
            "Token Fonnte kosong; tidak ada yang disimpan. Tempel token "
            "perangkat dari fonnte.com terlebih dahulu.")
    conn = db()
    set_pengaturan(conn, "wa_token", token)
    conn.commit(); conn.close()
    return self.redirect(
        "/admin/tampilan",
        "Gateway WA terhubung. Tombol Kirim WA di Kelola Guru sekarang "
        "mengirim otomatis ke WA wali kelas.")


def _post_admin_tampilan_wa_gateway_putus(self):
    conn = db()
    set_pengaturan(conn, "wa_token", "")
    conn.commit(); conn.close()
    return self.redirect(
        "/admin/tampilan",
        "Gateway WA diputuskan. Tombol Kirim WA kembali membuka WhatsApp.")


def _post_admin_tampilan_jam_absen(self, f):
    """Simpan jam absensi yang bisa diedit admin (permintaan user:
    tidak semua sekolah masuk jam yang sama). Tiga jam 'HH:MM' sah dan
    urutannya wajib hadir < alpa < panel selesai; selain itu ditolak
    dan pengaturan lama tidak berubah."""
    hadir = (f.get("jam_batas_hadir") or "").strip()
    alpa = (f.get("jam_batas_alpa") or "").strip()
    panel = (f.get("jam_panel_selesai") or "").strip()
    pola = r"^([01]\d|2[0-3]):[0-5]\d$"
    if not (re.match(pola, hadir) and re.match(pola, alpa)
            and re.match(pola, panel)):
        return self.redirect("/admin/tampilan",
                             "Jam absensi tidak valid; pengaturan tidak berubah.")
    if not (hadir < alpa < panel):
        return self.redirect(
            "/admin/tampilan",
            "Urutan jam salah: Batas Hadir harus lebih awal dari Batas "
            "Alpa, dan Batas Alpa lebih awal dari jam panel selesai. "
            "Pengaturan tidak berubah.")
    conn = db()
    set_pengaturan(conn, "jam_batas_hadir", hadir)
    set_pengaturan(conn, "jam_batas_alpa", alpa)
    set_pengaturan(conn, "jam_panel_selesai", panel)
    conn.commit(); conn.close()
    return self.redirect(
        "/admin/tampilan",
        f"Jam absensi disimpan: batas Hadir {hadir}, Alpa otomatis "
        f"{alpa}, panel laporan hilang {panel}.")


# Header cache untuk berkas gambar unggahan (wallpaper & logo): nama berkas
# adalah token hex acak yang BERGANTI setiap unggahan, jadi isinya tidak akan
# pernah berubah di URL yang sama — aman di-cache browser lama & immutable.
# Tanpa ini browser mengunduh ulang gambar yang sama di setiap halaman
# (penyebab latar lama muncul: berkas wallpaper bisa berukuran MB-an).
CACHE_GAMBAR_UPLOADS = "public, max-age=604800, immutable"


def _kompres_gambar_unggahan(data, ext_asli, jenis):
    """Kecilkan gambar unggahan (wallpaper/logo) dengan Pillow.

    Kembalikan (bytes_hasil, ext_hasil). Wallpaper selalu menjadi JPEG RGB
    (tidak butuh transparansi) dengan sisi terpanjang <= 1920 px, kualitas
    82 — foto 3 MB-an menyusut jadi ratusan KB. Logo dibatasi 512 px: PNG
    (atau gambar ber-alpha) dipertahankan sebagai PNG agar transparansi
    tidak hilang, JPEG tetap JPEG kualitas 85, WebP tetap WebP.

    Kegagalan Pillow dalam bentuk apa pun TIDAK boleh menggagalkan
    unggahan: kembalikan bytes & ekstensi asli apa adanya.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        buf = io.BytesIO()
        if jenis == "wallpaper":
            img = img.convert("RGB")
            img.thumbnail((1920, 1920))
            img.save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue(), ".jpg"
        punya_alpha = img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info)
        fmt = (img.format or "").upper()
        img.thumbnail((512, 512))
        if punya_alpha or fmt == "PNG":
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue(), ".png"
        if fmt == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            return buf.getvalue(), ".jpg"
        if fmt == "WEBP":
            img.convert("RGB").save(buf, "WEBP", quality=90)
            return buf.getvalue(), ".webp"
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), ".png"
    except Exception:
        return data, ext_asli


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
    data, ext = _kompres_gambar_unggahan(data, ext, "wallpaper")
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
    return self.send_bytes(
        data, WALLPAPER_EXT.get(ext, "application/octet-stream"),
        extra_headers=[("Cache-Control", CACHE_GAMBAR_UPLOADS)])


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
    data, ext = _kompres_gambar_unggahan(data, ext, "logo")
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


# -- admin: import data dari Excel (siswa & guru) ------------------------------
IMPORT_MAKS = 5 * 1024 * 1024  # batas ukuran file Excel import
IMPORT_BARIS_MAKS = 2000       # batas baris data per file import


def _sel_excel(v):
    """Nilai sel Excel -> teks rapi: kosong -> '', angka bulat (NIS yang
    diketik sebagai angka) -> tanpa ',0', selain itu str apa adanya."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def _ambil_file_excel(self, kembali):
    """Baca file .xlsx dari form multipart import. Kembalikan
    (data_bytes, None) bila sah, atau (None, respon_redirect) bila tidak."""
    length = int(self.headers.get("Content-Length", "0") or "0")
    if length > IMPORT_MAKS + 1024 * 1024:
        return None, self.redirect(kembali, "File terlalu besar (maks 5 MB).")
    body = self.rfile.read(length) if length else b""
    _fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
    berkas = files.get("berkas")
    if not berkas or not berkas[0]:
        return None, self.redirect(kembali, "Pilih file Excel dulu.")
    nama_file, data = berkas
    if not nama_file.lower().endswith(".xlsx"):
        return None, self.redirect(
            kembali, "Format file harus .xlsx (Excel). Simpan ulang file-nya "
                     "sebagai .xlsx lalu coba lagi.")
    if len(data) > IMPORT_MAKS:
        return None, self.redirect(kembali, "File terlalu besar (maks 5 MB).")
    return data, None


def _baris_excel(data):
    """Buka .xlsx -> list baris (tuple nilai sel) dari lembar pertama.
    Melempar Exception bila file bukan Excel yang bisa dibaca."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _buat_template_excel(judul_sheet, kolom, lebar, petunjuk):
    """Template .xlsx import: lembar data berisi HANYA baris judul kolom
    (ber-gaya seperti ekspor lain) + lembar Petunjuk berisi format &
    contoh, supaya contoh tidak ikut ter-import sebagai data."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = judul_sheet
    tipis = Side(style="thin", color="9AA3B2")
    border = Border(left=tipis, right=tipis, top=tipis, bottom=tipis)
    fill_head = PatternFill("solid", fgColor="203764")
    font_head = Font(bold=True, color="FFFFFF", size=11)
    for j, h in enumerate(kolom, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.fill = fill_head; c.font = font_head; c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(j)].width = lebar[j - 1]
    ws2 = wb.create_sheet("Petunjuk")
    for i, baris in enumerate(petunjuk, start=1):
        c = ws2.cell(row=i, column=1, value=baris)
        if i == 1:
            c.font = Font(bold=True, size=12)
    ws2.column_dimensions["A"].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _admin_siswa_template(self):
    data = _buat_template_excel(
        "Data Siswa", ["Nama", "NIS", "Kelas", "Jenis Kelamin"], [30, 16, 16, 16],
        ["Template Import Data Siswa",
         "Isi data siswa mulai baris ke-2 pada lembar 'Data Siswa'.",
         "Kolom: Nama | NIS | Kelas (nama kelas, mis. 1A) | Jenis Kelamin (boleh kosong).",
         "Jenis Kelamin diisi 'Laki-laki' atau 'Perempuan' (untuk Kartu Pelajar).",
         "Contoh baris: Budi Santoso | 1001 | 1A | Laki-laki",
         "Kelas yang belum ada akan dibuat otomatis saat upload.",
         "NIS yang sudah terdaftar akan dilewati (tidak digandakan).",
         "Simpan sebagai .xlsx, lalu upload di menu Kelola Siswa."])
    return self.send_bytes(
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Template-Siswa.xlsx")


def _admin_guru_template(self):
    data = _buat_template_excel(
        "Data Guru", ["Nama", "Mata Pelajaran", "Nomor WA", "Jenis Kelamin"], [30, 24, 20, 16],
        ["Template Import Data Guru",
         "Isi data guru mulai baris ke-2 pada lembar 'Data Guru'.",
         "Kolom: Nama | Mata Pelajaran | Nomor WA | Jenis Kelamin (Nomor WA dan Jenis Kelamin boleh kosong).",
         "Jenis Kelamin diisi 'Laki-laki' atau 'Perempuan' (untuk Kartu Guru).",
         "Contoh baris: Ibu Ani Wijaya | Matematika | 081234567890 | Perempuan",
         "Guru dengan nama yang sudah terdaftar akan dilewati.",
         "Simpan sebagai .xlsx, lalu upload di menu Kelola Guru."])
    return self.send_bytes(
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Template-Guru.xlsx")


def _post_admin_siswa_import(self):
    data, err = _ambil_file_excel(self, "/admin/siswa")
    if err is not None:
        return err
    try:
        baris = _baris_excel(data)
    except Exception:
        return self.redirect("/admin/siswa",
                             "File tidak bisa dibaca sebagai Excel (.xlsx). "
                             "Periksa file-nya lalu coba lagi.")
    conn = db()
    ditambah = dilewati = kelas_baru = 0
    nis_ada = {r["kode"] for r in conn.execute("SELECT kode FROM students")}
    kelas_peta = {}
    for r in conn.execute("SELECT id, nama FROM classes"):
        kelas_peta[r["nama"].strip().lower()] = r["id"]
    for i, row in enumerate(baris):
        if i == 0:
            # Baris judul kolom template: lewati bila sel pertamanya 'nama'.
            if row and _sel_excel(row[0]).lower() in ("nama", "nama siswa"):
                continue
        if i > IMPORT_BARIS_MAKS:
            break
        nama = _sel_excel(row[0]) if len(row) > 0 else ""
        kode = _sel_excel(row[1]) if len(row) > 1 else ""
        kelas_nama = _sel_excel(row[2]) if len(row) > 2 else ""
        jk = _sel_excel(row[3]) if len(row) > 3 else ""
        if not nama and not kode and not kelas_nama:
            continue  # baris kosong: abaikan diam-diam
        if not nama or not kode or not kelas_nama:
            dilewati += 1
            continue
        if kode in nis_ada:
            dilewati += 1
            continue
        kunci = kelas_nama.lower()
        if kunci not in kelas_peta:
            cur = conn.execute("INSERT INTO classes (nama) VALUES (?)",
                               (kelas_nama,))
            kelas_peta[kunci] = cur.lastrowid
            kelas_baru += 1
        conn.execute("INSERT INTO students (nama, kode, class_id, jenis_kelamin) "
                     "VALUES (?,?,?,?)", (nama, kode, kelas_peta[kunci], _jk_bersih(jk)))
        nis_ada.add(kode)
        ditambah += 1
    conn.commit(); conn.close()
    pesan = f"Import siswa selesai: {ditambah} siswa ditambahkan"
    if kelas_baru:
        pesan += f", {kelas_baru} kelas baru dibuat"
    if dilewati:
        pesan += (f", {dilewati} baris dilewati (NIS sudah ada / data "
                  "tidak lengkap)")
    return self.redirect("/admin/siswa", pesan + ".")


def _tambah_guru_conn(conn, nama, mapel, no_wa="", jk=""):
    """Buat satu guru (data induk) persis seperti Tambah Guru manual:
    username penanda diganti 'guru-<id>' + kode QR otomatis; login
    sungguhan lahir hanya lewat penunjukan wali kelas."""
    ph, salt = hash_password(secrets.token_urlsafe(24))
    cur = conn.execute(
        "INSERT INTO users (username,password_hash,salt,role,nama,mapel,no_wa,jenis_kelamin) "
        "VALUES (?,?,?,'guru',?,?,?,?)",
        ("baru-" + secrets.token_hex(8), ph, salt, nama, mapel, no_wa,
         _jk_bersih(jk)))
    gid = cur.lastrowid
    conn.execute("UPDATE users SET username=?, kode_qr=? WHERE id=?",
                 (f"guru-{gid}", kode_qr_guru_baru(conn, gid), gid))
    return gid


def _admin_kelas_template(self, kid):
    """Template Excel import siswa UNTUK SATU KELAS: kolom
    Nama | NIS | Jenis Kelamin (opsional) — kelasnya sudah pasti dari
    halaman kelas yang dibuka."""
    conn = db()
    kelas = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    conn.close()
    if not kelas:
        return self.redirect("/admin/kelas", "Kelas tidak ditemukan.")
    data = _buat_template_excel(
        "Data Siswa", ["Nama", "NIS", "Jenis Kelamin"], [30, 16, 16],
        [f"Template Import Data Siswa — Kelas {kelas['nama']}",
         "Isi data siswa mulai baris ke-2 pada lembar 'Data Siswa'.",
         "Kolom: Nama | NIS | Jenis Kelamin (boleh kosong). Tidak perlu kolom Kelas: semua siswa di",
         f"file ini masuk ke kelas {kelas['nama']}.",
         "Jenis Kelamin diisi 'Laki-laki' atau 'Perempuan' (untuk Kartu Pelajar).",
         "Contoh baris: Budi Santoso | 1001 | Laki-laki",
         "NIS yang sudah terdaftar akan dilewati (tidak digandakan).",
         "Simpan sebagai .xlsx, lalu upload di halaman kelas ini."])
    bersih = re.sub(r"[^\w.\-]+", "_", kelas["nama"]).strip("_") or "kelas"
    return self.send_bytes(
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Template-Siswa-Kelas-{bersih}.xlsx")


def _post_admin_kelas_import(self, kid):
    """Import siswa dari Excel ke SATU kelas (halaman detail kelas):
    file Nama | NIS | Jenis Kelamin (kolom ketiga opsional — bila ada
    diisi 'Laki-laki'/'Perempuan' untuk Kartu Pelajar); kelas selalu
    kelas halaman ini. NIS duplikat dilewati."""
    conn = db()
    kelas = conn.execute("SELECT * FROM classes WHERE id=?", (kid,)).fetchone()
    conn.close()
    if not kelas:
        return self.redirect("/admin/kelas", "Kelas tidak ditemukan.")
    kembali = f"/admin/kelas/{kid}"
    data, err = _ambil_file_excel(self, kembali)
    if err is not None:
        return err
    try:
        baris = _baris_excel(data)
    except Exception:
        return self.redirect(kembali,
                             "File tidak bisa dibaca sebagai Excel (.xlsx). "
                             "Periksa file-nya lalu coba lagi.")
    conn = db()
    ditambah = dilewati = 0
    nis_ada = {r["kode"] for r in conn.execute("SELECT kode FROM students")}
    for i, row in enumerate(baris):
        if i == 0:
            if row and _sel_excel(row[0]).lower() in ("nama", "nama siswa"):
                continue
        if i > IMPORT_BARIS_MAKS:
            break
        nama = _sel_excel(row[0]) if len(row) > 0 else ""
        kode = _sel_excel(row[1]) if len(row) > 1 else ""
        jk = _sel_excel(row[2]) if len(row) > 2 else ""
        if not nama and not kode:
            continue
        if not nama or not kode:
            dilewati += 1
            continue
        if kode in nis_ada:
            dilewati += 1
            continue
        conn.execute("INSERT INTO students (nama, kode, class_id, jenis_kelamin) "
                     "VALUES (?,?,?,?)", (nama, kode, kid, _jk_bersih(jk)))
        nis_ada.add(kode)
        ditambah += 1
    conn.commit(); conn.close()
    pesan = (f"Import siswa kelas {kelas['nama']} selesai: "
             f"{ditambah} siswa ditambahkan")
    if dilewati:
        pesan += (f", {dilewati} baris dilewati (NIS sudah ada / data "
                  "tidak lengkap)")
    return self.redirect(kembali, pesan + ".")


def _post_admin_guru_kirim_wa(self, gid):
    """Kirim password wali kelas ke WA gurunya SECARA OTOMATIS lewat
    gateway Fonnte (token admin di settings wa_token). Validasi: guru
    ada, sedang jadi wali, punya password berlaku, nomor WA terisi,
    dan token gateway sudah dihubungkan."""
    conn = db()
    g = self._get_guru(conn, gid)
    if not g:
        conn.close()
        return self.redirect("/admin/guru", "Guru tidak ditemukan.")
    wali_row = conn.execute("SELECT * FROM classes WHERE wali_id=?",
                            (gid,)).fetchone()
    token = (get_pengaturan()["wa_token"] or "").strip()
    sekolah = (get_pengaturan()["nama_sekolah"] or "").strip() or "sekolah"
    conn.close()
    if not wali_row or not g["pass_sementara"]:
        return self.redirect(
            "/admin/guru",
            "Password hanya bisa dikirim untuk guru yang sedang menjadi "
            "wali kelas dan punya password berlaku.")
    if not token:
        return self.redirect(
            "/admin/guru",
            "Kirim otomatis belum aktif: hubungkan dulu token gateway WA "
            "di Pengaturan Tampilan > WhatsApp (Fonnte).")
    nomor = _normal_no_wa(g["no_wa"])
    if not nomor:
        return self.redirect(
            f"/admin/guru/{gid}/edit",
            f"Nomor WA {g['nama']} belum diisi atau tidak valid; isi dulu "
            "di form Edit Guru ini, lalu kirim lagi.")
    host = self.headers.get("Host", "")
    skema = self.headers.get("X-Forwarded-Proto") or (
        "http" if host.startswith(("127.0.0.1", "localhost")) else "https")
    url_login = f"{skema}://{host}/login" if host else "/login"
    pesan = _pesan_wa_wali(g["nama"], wali_row["nama"],
                           g["pass_sementara"], sekolah, url_login)
    ok, info = _kirim_wa_gateway(nomor, pesan, token)
    if ok:
        return self.redirect(
            "/admin/guru",
            f"Password wali kelas {wali_row['nama']} terkirim otomatis "
            f"ke WA {g['nama']} ({g['no_wa']}).")
    return self.redirect(
        "/admin/guru",
        f"Pengiriman WA ke {g['nama']} GAGAL: {info}. Periksa koneksi "
        "perangkat di Fonnte (masih terhubung & kuota cukup), lalu coba lagi.")


def _post_admin_guru_kirim_wa_semua(self):
    """Kirim password ke SEMUA wali kelas sekaligus lewat gateway
    Fonnte (permintaan user: habis menunjuk beberapa wali, kirimnya
    barengan, tidak satu-satu). Penerima: guru yang SEDANG jadi wali
    kelas dan punya password berlaku. Yang nomor WA-nya kosong/invalid
    dilewati dan namanya dilaporkan; yang gateway-nya menolak masuk
    daftar gagal beserta alasannya. Pesan tiap guru memakai kelas &
    password-nya sendiri (template yang sama dengan kirim tunggal)."""
    token = (get_pengaturan()["wa_token"] or "").strip()
    if not token:
        return self.redirect(
            "/admin/guru",
            "Kirim WA Semua butuh gateway WA terhubung dulu: daftar "
            "Fonnte (gratis), hubungkan WA admin, lalu tempel tokennya "
            "di Pengaturan Tampilan > WhatsApp — Kirim Password "
            "Otomatis (Fonnte).")
    sekolah = (get_pengaturan()["nama_sekolah"] or "").strip() or "sekolah"
    conn = db()
    rows = conn.execute(
        """SELECT u.*, c.nama AS kelas_nama FROM users u
           JOIN classes c ON c.wali_id = u.id
           WHERE u.role='guru' AND u.pass_sementara <> ''
           ORDER BY u.nama""").fetchall()
    conn.close()
    if not rows:
        return self.redirect(
            "/admin/guru",
            "Belum ada wali kelas yang punya password untuk dikirim. "
            "Tunjuk dulu wali kelasnya di menu Kelola Kelas.")
    host = self.headers.get("Host", "")
    skema = self.headers.get("X-Forwarded-Proto") or (
        "http" if host.startswith(("127.0.0.1", "localhost")) else "https")
    url_login = f"{skema}://{host}/login" if host else "/login"
    terkirim, dilewati, gagal = [], [], []
    for r in rows:
        nomor = _normal_no_wa(r["no_wa"])
        if not nomor:
            dilewati.append(r["nama"])
            continue
        pesan = _pesan_wa_wali(r["nama"], r["kelas_nama"],
                               r["pass_sementara"], sekolah, url_login)
        ok, info = _kirim_wa_gateway(nomor, pesan, token)
        if ok:
            terkirim.append(r["nama"])
        else:
            gagal.append(f"{r['nama']} ({info})")
    ringkas = (f"Kirim WA Semua selesai: {len(terkirim)} terkirim")
    if terkirim:
        ringkas += " (" + ", ".join(terkirim) + ")"
    if dilewati:
        ringkas += (f"; {len(dilewati)} dilewati karena nomor WA "
                    "kosong: " + ", ".join(dilewati))
    if gagal:
        ringkas += f"; {len(gagal)} GAGAL: " + ", ".join(gagal)
    return self.redirect("/admin/guru", ringkas + ".")


def _post_admin_guru_import(self):
    data, err = _ambil_file_excel(self, "/admin/guru")
    if err is not None:
        return err
    try:
        baris = _baris_excel(data)
    except Exception:
        return self.redirect("/admin/guru",
                             "File tidak bisa dibaca sebagai Excel (.xlsx). "
                             "Periksa file-nya lalu coba lagi.")
    conn = db()
    ditambah = dilewati = 0
    nama_ada = {(r["nama"] or "").strip().lower() for r in conn.execute(
        "SELECT nama FROM users WHERE role='guru'")}
    for i, row in enumerate(baris):
        if i == 0:
            if row and _sel_excel(row[0]).lower() in ("nama", "nama guru"):
                continue
        if i > IMPORT_BARIS_MAKS:
            break
        nama = _sel_excel(row[0]) if len(row) > 0 else ""
        mapel = _sel_excel(row[1]) if len(row) > 1 else ""
        no_wa = _sel_excel(row[2]) if len(row) > 2 else ""
        jk = _sel_excel(row[3]) if len(row) > 3 else ""
        if not nama and not mapel:
            continue
        if not nama:
            dilewati += 1
            continue
        if nama.lower() in nama_ada:
            dilewati += 1
            continue
        _tambah_guru_conn(conn, nama, mapel, no_wa, jk)
        nama_ada.add(nama.lower())
        ditambah += 1
    conn.commit(); conn.close()
    pesan = f"Import guru selesai: {ditambah} guru ditambahkan"
    if dilewati:
        pesan += (f", {dilewati} baris dilewati (nama sudah ada / data "
                  "tidak lengkap)")
    return self.redirect("/admin/guru", pesan + ".")


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
Handler.siswa_detail = _siswa_detail
Handler.siswa_detail_unduh = _siswa_detail_unduh
Handler.admin_guru = _admin_guru
Handler.post_admin_guru = _post_admin_guru
Handler._get_guru = _get_guru
Handler.admin_guru_edit = _admin_guru_edit
Handler.post_admin_guru_edit = _post_admin_guru_edit
Handler.post_admin_guru_hapus = _post_admin_guru_hapus
Handler.post_admin_guru_password_baru = _post_admin_guru_password_baru
Handler.post_admin_guru_aktifkan = _post_admin_guru_aktifkan
Handler._opsi_wali = _opsi_wali
Handler._wali_dari_form = _wali_dari_form
Handler._tetapkan_wali = _tetapkan_wali
Handler._simple_list = _simple_list
Handler.admin_kelas = _admin_kelas
Handler._post_simple = _post_simple
Handler.post_admin_kelas = _post_admin_kelas
Handler._simple_edit = _simple_edit
Handler.admin_kelas_edit = _admin_kelas_edit
Handler._post_simple_edit = _post_simple_edit
Handler.post_admin_kelas_edit = _post_admin_kelas_edit
Handler.post_admin_kelas_wali = _post_admin_kelas_wali
Handler.post_admin_kelas_hapus = _post_admin_kelas_hapus
Handler.admin_kelas_detail = _admin_kelas_detail
Handler.admin_siswa = _admin_siswa
Handler.post_admin_siswa = _post_admin_siswa
Handler.admin_siswa_edit = _admin_siswa_edit
Handler.post_admin_siswa_edit = _post_admin_siswa_edit
Handler.post_admin_siswa_hapus = _post_admin_siswa_hapus
Handler.admin_tampilan = _admin_tampilan
Handler.post_admin_tampilan = _post_admin_tampilan
Handler.post_admin_tampilan_warna_menu = _post_admin_tampilan_warna_menu
Handler.post_admin_tampilan_kejelasan_wp = _post_admin_tampilan_kejelasan_wp
Handler.post_admin_tampilan_tema = _post_admin_tampilan_tema
Handler.post_admin_tampilan_jam_absen = _post_admin_tampilan_jam_absen
Handler.post_admin_tampilan_wa_gateway = _post_admin_tampilan_wa_gateway
Handler.post_admin_tampilan_wa_gateway_putus = _post_admin_tampilan_wa_gateway_putus
Handler.post_admin_guru_kirim_wa = _post_admin_guru_kirim_wa
Handler.post_admin_guru_kirim_wa_semua = _post_admin_guru_kirim_wa_semua
Handler.post_admin_tampilan_wallpaper = _post_admin_tampilan_wallpaper
Handler.post_admin_tampilan_wallpaper_hapus = _post_admin_tampilan_wallpaper_hapus
Handler.wallpaper_file = _wallpaper_file
Handler.post_admin_tampilan_logo = _post_admin_tampilan_logo
Handler.post_admin_tampilan_logo_hapus = _post_admin_tampilan_logo_hapus
Handler.logo_file = _logo_file
Handler.admin_siswa_template = _admin_siswa_template
Handler.admin_guru_template = _admin_guru_template
Handler.post_admin_siswa_import = _post_admin_siswa_import
Handler.admin_kelas_template = _admin_kelas_template
Handler.post_admin_kelas_import = _post_admin_kelas_import
Handler.post_admin_guru_import = _post_admin_guru_import
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
