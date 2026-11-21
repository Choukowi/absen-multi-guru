#!/usr/bin/env python3
"""Uji alur end-to-end model GERBANG GLOBAL lewat HTTP.

Skrip ini MENGELOLA server uji sendiri: DB scratch di /tmp, port 8877,
dan server di-restart antar skenario waktu dengan ABSEN_NOW_OVERRIDE
(jam Asia/Jakarta yang disimulasikan). Jalankan dengan python yang punya
dependensi proyek (qrcode, Pillow, openpyxl):

    /path/venv/bin/python test_alur.py

Membuat data uji berawalan UJI- di DB scratch (BUKAN DB asli).
"""
import base64
import io
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

PROJ = os.path.dirname(os.path.abspath(__file__))
PORT = 8877
BASE = f"http://127.0.0.1:{PORT}"
DBUJI = "/tmp/absen-uji.db"
UPLOAD_DIR = os.path.join(PROJ, "data", "uploads")
_proc = None


def start_server(override=None, dbfile=None):
    global _proc
    stop_server()
    env = dict(os.environ)
    env["ABSEN_DB"] = dbfile or DBUJI
    env["PORT"] = str(PORT)
    if override:
        env["ABSEN_NOW_OVERRIDE"] = override
    else:
        env.pop("ABSEN_NOW_OVERRIDE", None)
    _proc = subprocess.Popen([sys.executable, os.path.join(PROJ, "app.py")],
                             env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/login", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server uji tidak kunjung jalan")


def stop_server():
    global _proc
    if _proc:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except Exception:
            _proc.kill()
        _proc = None


def client():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def req(op, path, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    r = op.open(urllib.request.Request(BASE + path, data=body))
    return r.status, r.geturl(), r.read().decode("utf-8", "replace")


def req_bytes(op, path):
    r = op.open(BASE + path)
    return r.status, r.headers, r.read()


def login(username, password):
    op = client()
    s, url, body = req(op, "/login", {"username": username, "password": password})
    return op, url, body


def must(cond, msg):
    print(("OK  " if cond else "GAGAL ") + msg)
    if not cond:
        stop_server()
        sys.exit(1)


def dbq(sql, args=()):
    return dbqf(DBUJI, sql, args)


def dbqf(dbfile, sql, args=()):
    conn = sqlite3.connect(dbfile)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def id_dari_daftar(htmltxt, base, penanda):
    for chunk in htmltxt.split("<li>"):
        if penanda in chunk:
            m = re.search(base + r"/(\d+)/edit", chunk)
            if m:
                return int(m.group(1))
    return None


def opt_val(htmltxt, label):
    m = re.search(r'<option value="(\d+)">' + re.escape(label) + r'</option>', htmltxt)
    return int(m.group(1)) if m else None


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

uploads_awal = set(os.listdir(UPLOAD_DIR)) if os.path.isdir(UPLOAD_DIR) else set()
if os.path.exists(DBUJI):
    os.remove(DBUJI)

# ===========================================================================
# Fase M — MIGRASI: DB skema LAMA (versi sesi per mapel) berisi data asli,
# dibuka oleh kode BARU. Data TIDAK boleh hilang/diubah; guru lama mendapat
# kode_qr backfill; tabel lama (subjects/sessions/attendance/assignments)
# dibiarkan utuh.
# ===========================================================================
import hashlib

DBMIG = "/tmp/absen-migrasi.db"
if os.path.exists(DBMIG):
    os.remove(DBMIG)

SKEMA_LAMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','guru')),
    nama TEXT NOT NULL,
    aktif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE classes (id INTEGER PRIMARY KEY AUTOINCREMENT, nama TEXT UNIQUE NOT NULL);
CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, nama TEXT UNIQUE NOT NULL);
CREATE TABLE students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nama TEXT NOT NULL,
    kode TEXT UNIQUE NOT NULL,
    class_id INTEGER NOT NULL REFERENCES classes(id)
);
CREATE TABLE assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    UNIQUE (user_id, class_id, subject_id)
);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    teacher_id INTEGER NOT NULL REFERENCES users(id),
    tanggal TEXT NOT NULL, jam_mulai TEXT NOT NULL,
    batas_terlambat_menit INTEGER NOT NULL DEFAULT 15,
    status TEXT NOT NULL DEFAULT 'buka' CHECK (status IN ('buka','tutup'))
);
CREATE TABLE attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    status TEXT NOT NULL CHECK (status IN ('Hadir','Terlambat','Sakit','Izin','Alpa')),
    metode TEXT NOT NULL DEFAULT 'manual',
    waktu TEXT,
    recorded_by INTEGER REFERENCES users(id),
    UNIQUE (session_id, student_id)
);
"""


def _hash_lama(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(),
                               bytes.fromhex(salt_hex), 200_000).hex()


conn = sqlite3.connect(DBMIG)
conn.executescript(SKEMA_LAMA)
SALT = "aa" * 16
conn.execute("INSERT INTO users (id,username,password_hash,salt,role,nama) VALUES (1,'admin',?,?, 'admin','Admin Migrasi')",
             (_hash_lama("admin123", SALT), SALT))
conn.execute("INSERT INTO users (id,username,password_hash,salt,role,nama) VALUES (2,'gurumigrasi',?,?,'guru','Guru Migrasi')",
             (_hash_lama("gurumigrasi123", SALT), SALT))
conn.execute("INSERT INTO classes (id,nama) VALUES (1,'MIG-Kelas-1'),(2,'MIG-Kelas-2')")
conn.execute("INSERT INTO subjects (id,nama) VALUES (1,'MIG-Matematika')")
conn.execute("INSERT INTO students (id,nama,kode,class_id) VALUES "
             "(1,'Siswa Migrasi A','MIG-001',1),(2,'Siswa Migrasi B','MIG-002',1),"
             "(3,'Siswa Migrasi C','MIG-003',2)")
conn.execute("INSERT INTO assignments (user_id,class_id,subject_id) VALUES (2,1,1)")
conn.execute("INSERT INTO sessions (id,class_id,subject_id,teacher_id,tanggal,jam_mulai,status) "
             "VALUES (1,1,1,2,'2026-11-13','07:00','tutup')")
conn.execute("INSERT INTO attendance (session_id,student_id,status,metode,waktu,recorded_by) "
             "VALUES (1,1,'Hadir','scan','2026-11-13 06:55:00',2)")
conn.commit()
conn.close()

start_server("2026-11-16T06:45:00", dbfile=DBMIG)
admin_m, url, _ = login("admin", "admin123")
must("dashboard" in url, "MIGRASI: server baru jalan di DB lama & login admin lama berhasil")
s, url, daftar_guru_m = req(admin_m, "/admin/guru")
chunk_mig = next((c for c in daftar_guru_m.split("<li>") if "gurumigrasi" in c), "")
must("Guru Migrasi" in daftar_guru_m, "MIGRASI: guru lama tetap terdaftar")
must(re.search(r"kode QR: <code>G\d+</code>", chunk_mig) is not None,
     "MIGRASI: guru lama mendapat kode_qr backfill otomatis")
must("&mdash; - &mdash;" in chunk_mig,
     "MIGRASI: mapel guru lama kosong (default '') tanpa error")
s, url, daftar_siswa_m = req(admin_m, "/admin/siswa")
must(all(n in daftar_siswa_m for n in
         ["Siswa Migrasi A", "Siswa Migrasi B", "Siswa Migrasi C",
          "MIG-Kelas-1", "MIG-Kelas-2"]),
     "MIGRASI: ketiga siswa lama + kedua kelas tetap tampil")
s, url, daftar_kelas_m = req(admin_m, "/admin/kelas")
must("MIG-Kelas-1" in daftar_kelas_m and "MIG-Kelas-2" in daftar_kelas_m,
     "MIGRASI: daftar kelas lama utuh")
guru_m, url, _ = login("gurumigrasi", "gurumigrasi123")
must("dashboard" in url, "MIGRASI: login guru lama tetap bisa (password tidak berubah)")
s, url, hasil = req(admin_m, "/api/scan", {"kode": "MIG-001"})
must("Siswa Migrasi A" in hasil and "Hadir" in hasil,
     "MIGRASI: scan gerbang untuk siswa lama tercatat")
must(dbqf(DBMIG, "SELECT status FROM kehadiran WHERE tanggal='2026-11-16' "
                 "AND ref_type='siswa' AND ref_id=1") == "Hadir",
     "MIGRASI: scan siswa lama masuk tabel kehadiran baru")
must(dbqf(DBMIG, "SELECT COUNT(*) FROM subjects") == 1
     and dbqf(DBMIG, "SELECT COUNT(*) FROM sessions") == 1
     and dbqf(DBMIG, "SELECT COUNT(*) FROM attendance") == 1
     and dbqf(DBMIG, "SELECT COUNT(*) FROM assignments") == 1,
     "MIGRASI: tabel lama (subjects/sessions/attendance/assignments) tidak dihapus/diubah")
must(dbqf(DBMIG, "SELECT COUNT(*) FROM users") == 2
     and dbqf(DBMIG, "SELECT COUNT(*) FROM students") == 3
     and dbqf(DBMIG, "SELECT COUNT(*) FROM classes") == 2,
     "MIGRASI: jumlah baris users/students/classes sama seperti sebelum migrasi")
stop_server()
os.remove(DBMIG)

# ===========================================================================
# Fase A — Senin 2026-11-16 06:30: login, shell UI, CRUD master, rute lama mati
# ===========================================================================
start_server("2026-11-16T06:30:00")
anon = client()
s, url, login_page = req(anon, "/login")
must("auth-card" in login_page and "Absen Siswa" in login_page,
     "halaman login memakai shell auth baru")
must("background-color:#f3f5fb" in login_page,
     "login memakai warna latar bawaan #f3f5fb")
s, url, body = req(anon, "/dashboard")
must("/login" in url, "anonim membuka dashboard diarahkan ke login")

admin, url, body = login("admin", "admin123")
must("dashboard" in url, "login admin berhasil")
s, url, dash = req(admin, "/dashboard")
must('class="sidebar"' in dash and 'class="bottomnav"' in dash,
     "dashboard memakai shell sidebar + bottom-nav")
must("Scan Gerbang" in dash, "nav memuat 'Scan Gerbang'")
for href_lama in ('href="/admin/mapel"', 'href="/admin/penugasan"',
                  'href="/rekap"', 'href="/guru/sesi"'):
    must(href_lama not in dash, f"menu lama tidak ada di nav: {href_lama}")
for path_lama in ("/admin/mapel", "/admin/penugasan", "/rekap", "/guru/sesi"):
    try:
        req(admin, path_lama)
        must(False, f"{path_lama} harusnya 404")
    except urllib.error.HTTPError as e:
        must(e.code == 404, f"rute lama {path_lama} -> 404")

# --- master data: kelas, siswa, guru ---
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-1"})
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-2"})
s, url, form_siswa = req(admin, "/admin/siswa")
k1 = opt_val(form_siswa, "UJI-Kelas-1")
k2 = opt_val(form_siswa, "UJI-Kelas-2")
must(k1 and k2, f"id kelas terbaca: {k1}, {k2}")
for nama, kode, kid in [("Siswa Uji A", "UJI-001", k1), ("Siswa Uji B", "UJI-002", k1),
                        ("Siswa Uji C", "UJI-003", k1), ("Siswa Uji D", "UJI-004", k1),
                        ("Siswa Uji E", "UJI-005", k2)]:
    req(admin, "/admin/siswa", {"nama": nama, "kode": kode, "class_id": kid})
s, url, daftar_siswa = req(admin, "/admin/siswa")
must("NIS" in daftar_siswa and "Siswa Uji A" in daftar_siswa, "daftar siswa tampil dengan label NIS")
s, url, hasil = req(admin, "/admin/siswa",
                    {"nama": "Siswa Duplikat", "kode": "UJI-001", "class_id": k1})
must("NIS sudah dipakai." in hasil, "tambah siswa dengan NIS duplikat ditolak")
A_id = id_dari_daftar(daftar_siswa, "/admin/siswa", "UJI-001")
B_id = id_dari_daftar(daftar_siswa, "/admin/siswa", "UJI-002")
C_id = id_dari_daftar(daftar_siswa, "/admin/siswa", "UJI-003")
D_id = id_dari_daftar(daftar_siswa, "/admin/siswa", "UJI-004")
must(all([A_id, B_id, C_id, D_id]), f"id siswa terbaca: {A_id},{B_id},{C_id},{D_id}")
# edit siswa: rename lalu kembalikan; kode duplikat ditolak
req(admin, f"/admin/siswa/{B_id}/edit", {"nama": "Siswa Uji B Revisi", "kode": "UJI-002", "class_id": k1})
s, url, daftar_siswa = req(admin, "/admin/siswa")
must("Siswa Uji B Revisi" in daftar_siswa, "edit nama siswa tampil di daftar")
s, url, form_edit = req(admin, f"/admin/siswa/{B_id}/edit",
                        {"nama": "Siswa Uji B Revisi", "kode": "UJI-001", "class_id": k1})
must("NIS sudah dipakai." in form_edit, "edit NIS ke NIS siswa lain ditolak")
req(admin, f"/admin/siswa/{B_id}/edit", {"nama": "Siswa Uji B", "kode": "UJI-002", "class_id": k1})
# kelas berisi siswa tidak bisa dihapus; kelas kosong bisa
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("Edit" in daftar_kelas and "Hapus" in daftar_kelas, "daftar kelas ada Edit/Hapus")
s, url, hasil = req(admin, f"/admin/kelas/{k1}/hapus", {})
must("tidak bisa dihapus" in hasil, "hapus kelas berisi siswa ditolak")
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("UJI-Kelas-1" in daftar_kelas, "kelas yang ditolak hapus tetap ada")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Kosong"})
s, url, daftar_kelas = req(admin, "/admin/kelas")
kk_id = id_dari_daftar(daftar_kelas, "/admin/kelas", "UJI-Kelas-Kosong")
s, url, hasil = req(admin, f"/admin/kelas/{kk_id}/hapus", {})
must("Kelas dihapus." in hasil, "hapus kelas kosong berhasil")
# guru: tambah (mapel + kode QR otomatis), edit, duplikat ditolak
req(admin, "/admin/guru", {"nama": "Guru Uji", "username": "guruuji",
                           "password": "guruuji123", "mapel": "UJI-Matematika"})
req(admin, "/admin/guru", {"nama": "Guru Uji Dua", "username": "guruujidua",
                           "password": "guruujidua123", "mapel": "UJI-Biologi"})
s, url, daftar_guru = req(admin, "/admin/guru")
must("UJI-Matematika" in daftar_guru, "daftar guru menampilkan mata pelajaran")
g1_id = id_dari_daftar(daftar_guru, "/admin/guru", "guruuji")
g2_id = id_dari_daftar(daftar_guru, "/admin/guru", "guruujidua")
chunk_g1 = next((c for c in daftar_guru.split("<li>") if "guruuji" in c), "")
m_qr = re.search(r"kode QR: <code>(G\d+)</code>", chunk_g1)
must(m_qr is not None, "kode QR guru dibuat otomatis (format G + 3 digit)")
g1_qr = m_qr.group(1)
must(g1_id and g2_id, f"id guru terbaca: {g1_id}, {g2_id}")
req(admin, f"/admin/guru/{g2_id}/edit",
    {"nama": "Guru Uji Dua Revisi", "username": "guruujidua", "mapel": "UJI-Kimia", "password": ""})
s, url, daftar_guru = req(admin, "/admin/guru")
must("Guru Uji Dua Revisi" in daftar_guru and "UJI-Kimia" in daftar_guru,
     "edit guru (nama + mapel) tampil di daftar")
s, url, form_edit = req(admin, f"/admin/guru/{g2_id}/edit",
                        {"nama": "Guru Uji Dua Revisi", "username": "guruuji",
                         "mapel": "UJI-Kimia", "password": ""})
must("Username sudah dipakai." in form_edit, "edit username guru duplikat ditolak")
# QR PNG untuk siswa & guru; halaman cetak
st, hdr, data = req_bytes(admin, "/qr/UJI-001.png")
must(st == 200 and hdr.get_content_type() == "image/png", "QR PNG siswa jalan")
st, hdr, data = req_bytes(admin, f"/qr/{g1_qr}.png")
must(st == 200 and hdr.get_content_type() == "image/png", "QR PNG guru jalan")
s, url, cetak_idx = req(admin, "/cetak")
must("/cetak/guru" in cetak_idx and f"/cetak/kelas/{k1}" in cetak_idx,
     "halaman Cetak Kartu memuat tautan kelas & guru")
s, url, lembar_siswa = req(admin, f"/cetak/kelas/{k1}")
must("Siswa Uji A" in lembar_siswa and "NIS UJI-001" in lembar_siswa,
     "lembar kartu siswa berisi nama + NIS")
s, url, lembar_guru = req(admin, "/cetak/guru")
must("Guru Uji" in lembar_guru and g1_qr in lembar_guru,
     "lembar kartu guru berisi nama + kode QR guru")
s, url, menu = req(admin, "/menu")
must("Scan Gerbang" in menu and "Pengaturan Tampilan" in menu,
     "halaman Menu memuat semua bagian termasuk Pengaturan Tampilan")

# ===========================================================================
# Fase B — Senin 06:45: scan Hadir, tabel per kelas, scan ganda, Sakit manual
# ===========================================================================
start_server("2026-11-16T06:45:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-001"})
must("Siswa Uji A" in hasil and "Hadir" in hasil and "06:45" in hasil,
     "scan siswa A pukul 06:45 -> Hadir 06:45")
s, url, tabel1 = req(admin, f"/tabel-siswa?kelas={k1}&minggu=2026-11-16")
must("Siswa Uji A" in tabel1 and "Hadir" in tabel1 and "06.45" in tabel1,
     "tabel kelas 1 Senin memuat A Hadir 06.45")
s, url, tabel2 = req(admin, f"/tabel-siswa?kelas={k2}&minggu=2026-11-16")
must("Siswa Uji A" not in tabel2 and "Siswa Uji E" in tabel2,
     "tabel kelas 2 tidak memuat siswa kelas 1 (pemisahan kelas benar)")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-001"})
must("Sudah tercatat" in hasil, "scan ganda siswa A -> 'Sudah tercatat'")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal='2026-11-16' "
         "AND ref_type='siswa' AND ref_id=?", (A_id,)) == 1,
     "scan ganda tidak membuat baris kehadiran kedua")
# Sakit manual untuk C, lalu scan C tidak menimpa
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": "2026-11-16", "tipe": "siswa", "id": C_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "input manual Sakit untuk siswa C tersimpan")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-003"})
must("tidak mengubahnya" in hasil, "scan siswa C yang Sakit tidak menimpa status manual")
must(dbq("SELECT status FROM kehadiran WHERE tanggal='2026-11-16' "
         "AND ref_type='siswa' AND ref_id=?", (C_id,)) == "Sakit",
     "status C tetap Sakit setelah scan")
# guru bisa membuka halaman scan & tabel (akses global)
guru, url, _ = login("guruuji", "guruuji123")
must("dashboard" in url, "login guru berhasil")
s, url, hal_scan = req(guru, "/scan-gerbang")
must("Scan Gerbang" in hal_scan and "Sudah Scan Hari Ini" in hal_scan,
     "guru bisa membuka halaman Scan Gerbang")
s, url, tabel_g = req(guru, f"/tabel-siswa?kelas={k1}&minggu=2026-11-16")
must("Siswa Uji A" in tabel_g, "guru bisa melihat tabel semua kelas (global)")

# ===========================================================================
# Fase C — Senin 07:12: scan Terlambat siswa & guru; kode tak dikenal
# ===========================================================================
start_server("2026-11-16T07:12:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-002"})
must("Siswa Uji B" in hasil and "Terlambat" in hasil and "07:12" in hasil,
     "scan siswa B pukul 07:12 -> Terlambat 07:12")
s, url, hasil = req(admin, "/api/scan", {"kode": g1_qr})
must("Guru Uji" in hasil and "Terlambat" in hasil and "07:12" in hasil,
     "scan guru pukul 07:12 -> Terlambat tercatat")
s, url, tabel_guru = req(admin, "/tabel-guru?minggu=2026-11-16")
must("Guru Uji" in tabel_guru and "UJI-Matematika" in tabel_guru
     and "Terlambat" in tabel_guru and "07.12" in tabel_guru,
     "tabel guru Senin memuat Guru Uji + mapel + Terlambat 07.12")
s, url, hasil = req(admin, "/api/scan", {"kode": "TIDAK-ADA"})
must("Kode tidak dikenal" in hasil, "kode asing ditolak 'Kode tidak dikenal'")

# ===========================================================================
# Fase D — Senin 09:30: Alpa otomatis; scan menggantikan Alpa otomatis
# ===========================================================================
start_server("2026-11-16T09:30:00")
admin, url, _ = login("admin", "admin123")
s, url, tabel1 = req(admin, f"/tabel-siswa?kelas={k1}&minggu=2026-11-16")
chunk_d = next((c for c in tabel1.split("<tr>") if "Siswa Uji D" in c), "")
must("Alpa" in chunk_d, "pukul 09:30 siswa D yang belum scan tampil Alpa di tabel")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal='2026-11-16' "
         "AND ref_type='siswa' AND ref_id=?", (D_id,)) == "Alpa/auto",
     "Alpa siswa D tercatat metode auto di DB")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-004"})
must("Terlambat" in hasil and "09:30" in hasil,
     "scan siswa D pukul 09:30 tetap diterima -> Terlambat")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal='2026-11-16' "
         "AND ref_type='siswa' AND ref_id=?", (D_id,)) == "Terlambat/scan",
     "scan menggantikan Alpa otomatis siswa D")
s, url, dash = req(admin, "/dashboard")
must("Siswa Terlambat" in dash and "Guru Hadir" in dash,
     "dashboard menampilkan kartu statistik hari ini")

# ===========================================================================
# Fase E — Senin 09:35: hari Minggu TIDAK pernah diisi Alpa
# ===========================================================================
start_server("2026-11-16T09:35:00")
admin, url, _ = login("admin", "admin123")
req(admin, "/dashboard")
req(admin, f"/tabel-siswa?kelas={k1}&minggu=2026-11-16")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal='2026-11-15'") == 0,
     "tidak ada catatan kehadiran untuk hari Minggu 2026-11-15")

# ===========================================================================
# Fase F — unduhan Excel siswa & guru (openpyxl memverifikasi isi)
# ===========================================================================
from openpyxl import load_workbook
st, hdr, data = req_bytes(admin, f"/tabel-siswa/excel?kelas={k1}&minggu=2026-11-16")
must("spreadsheetml" in (hdr.get_content_type() or ""), "unduh Excel siswa: tipe konten xlsx")
wb = load_workbook(io.BytesIO(data))
ws = wb.active
semua = [[c.value for c in row] for row in ws.iter_rows()]
rata = [str(v) for baris in semua for v in baris if v is not None]
must("ABSENSI SISWA" in rata, "Excel siswa memuat judul ABSENSI SISWA")
must("Kelas: UJI-Kelas-1" in rata, "Excel siswa memuat nama kelas")
must("NIS" in rata and all(x in rata for x in ["H", "T", "S", "I", "A"]),
     "Excel siswa memuat kolom NIS + total H/T/S/I/A")
baris_a = next(r for r in ws.iter_rows() if r[1].value == "Siswa Uji A")
sel_senin = baris_a[3]
must(sel_senin.value and "Hadir" in str(sel_senin.value),
     "sel Senin siswa A di Excel berisi Hadir")
must(sel_senin.fill.patternType == "solid"
     and str(sel_senin.fill.fgColor.rgb).endswith("C6EFCE"),
     "sel Hadir di Excel punya warna isian hijau")
st, hdr, data = req_bytes(admin, "/tabel-guru/excel?minggu=2026-11-16")
wb = load_workbook(io.BytesIO(data))
ws = wb.active
rata = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
must("ABSENSI GURU" in rata and "Guru Uji" in rata and "Mapel" in rata,
     "Excel guru memuat judul, nama guru, dan kolom Mapel")

# ===========================================================================
# Fase G — Pengaturan Tampilan: warna, nama sekolah, wallpaper
# ===========================================================================
guru, url, _ = login("guruuji", "guruuji123")
try:
    req(guru, "/admin/tampilan")
    must(False, "guru membuka Pengaturan Tampilan harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru membuka Pengaturan Tampilan -> 404")
s, url, hasil = req(admin, "/admin/tampilan",
                    {"nama_sekolah": "SMA Uji Global", "bg_color": "#e6f4ea",
                     "bg_color_custom": "#e6f4ea"})
must("Pengaturan tampilan disimpan." in hasil, "simpan warna preset + nama sekolah berhasil")
s, url, dash = req(admin, "/dashboard")
must("background-color:#e6f4ea" in dash, "dashboard memakai warna latar pilihan admin")
must("SMA Uji Global" in dash, "nama sekolah tampil di shell aplikasi")
s, url, hasil = req(admin, "/admin/tampilan",
                    {"nama_sekolah": "SMA Uji Global", "bg_color": "kustom",
                     "bg_color_custom": "#123abc"})
s, url, dash = req(admin, "/dashboard")
must("background-color:#123abc" in dash, "warna latar kustom dari color picker terpasang")
st, hdr, data = req_bytes(admin, f"/tabel-siswa/excel?kelas={k1}&minggu=2026-11-16")
wb = load_workbook(io.BytesIO(data))
must(wb.active["A1"].value == "SMA Uji Global"
     and wb.active["A2"].value == "ABSENSI SISWA",
     "Excel menaruh nama sekolah di baris teratas, judul di bawahnya")
req(admin, "/admin/tampilan",
    {"nama_sekolah": "SMA Uji Global", "bg_color": "#f3f5fb", "bg_color_custom": "#f3f5fb"})


def unggah(op, path, field, filename, data_bytes, ctype="image/png"):
    boundary = "----uji" + os.urandom(6).hex()
    body = (f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
            + f"Content-Type: {ctype}\r\n\r\n".encode()
            + data_bytes
            + f"\r\n--{boundary}--\r\n".encode())
    r = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = op.open(r)
    return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")


s, url, hasil = unggah(admin, "/admin/tampilan/wallpaper", "wallpaper",
                       "wallpaper.png", PNG_1PX)
must("Wallpaper dipasang." in hasil, "unggah wallpaper PNG berhasil")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
m_wp = re.search(r"/wallpaper/([0-9a-f]{24}\.png)", hal_tampilan)
must(m_wp is not None, "halaman tampilan menampilkan wallpaper tersimpan")
nama_wp = m_wp.group(1)
st, hdr, data = req_bytes(anon, f"/wallpaper/{nama_wp}")
must(st == 200 and hdr.get_content_type() == "image/png" and data == PNG_1PX,
     "route /wallpaper/<nama> menyajikan berkas gambar (juga untuk halaman login)")
s, url, dash = req(admin, "/dashboard")
must(f"/wallpaper/{nama_wp}" in dash, "dashboard mereferensikan wallpaper sebagai latar")
s, url, login_page = req(anon, "/login")
must(f"/wallpaper/{nama_wp}" in login_page, "halaman login ikut memakai wallpaper")
s, url, hasil = req(admin, "/admin/tampilan/wallpaper/hapus", {})
must("Wallpaper dihapus." in hasil, "hapus wallpaper berhasil")
try:
    req_bytes(anon, f"/wallpaper/{nama_wp}")
    must(False, "wallpaper yang dihapus harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "wallpaper terhapus -> 404")
s, url, dash = req(admin, "/dashboard")
must("/wallpaper/" not in dash, "setelah dihapus latar kembali warna saja")

# --- Logo sekolah: unggah -> brand memakai logo; hapus -> ikon bawaan ---
try:
    unggah(guru, "/admin/tampilan/logo", "logo", "logo.png", PNG_1PX)
    must(False, "guru mengunggah logo harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru mengunggah logo -> 404")
s, url, hasil = unggah(admin, "/admin/tampilan/logo", "logo", "logo.png", PNG_1PX)
must("Logo dipasang." in hasil, "unggah logo PNG berhasil")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
m_logo = re.search(r"/logo/([0-9a-f]{24}\.png)", hal_tampilan)
must(m_logo is not None, "halaman tampilan menampilkan logo tersimpan")
nama_logo = m_logo.group(1)
st, hdr, data = req_bytes(anon, f"/logo/{nama_logo}")
must(st == 200 and hdr.get_content_type() == "image/png" and data == PNG_1PX,
     "route /logo/<nama> menyajikan berkas logo (juga untuk halaman login)")
s, url, dash = req(admin, "/dashboard")
must(f"/logo/{nama_logo}" in dash and "has-logo" in dash,
     "dashboard memakai logo sekolah sebagai brand (kelas has-logo)")
s, url, login_page = req(anon, "/login")
must(f"/logo/{nama_logo}" in login_page, "halaman login ikut memakai logo sekolah")
s, url, hasil = req(admin, "/admin/tampilan/logo/hapus", {})
must("Logo dihapus." in hasil, "hapus logo berhasil")
try:
    req_bytes(anon, f"/logo/{nama_logo}")
    must(False, "logo yang dihapus harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "logo terhapus -> 404")
s, url, dash = req(admin, "/dashboard")
must("/logo/" not in dash, "setelah logo dihapus brand kembali ke ikon bawaan")

# ===========================================================================
# Fase H — hapus/nonaktifkan guru berdasarkan riwayat kehadiran
# ===========================================================================
s, url, hasil = req(admin, f"/admin/guru/{g1_id}/hapus", {})
must("Guru dinonaktifkan karena sudah punya riwayat kehadiran." in hasil,
     "hapus guru ber-riwayat scan -> dinonaktifkan")
s, url, daftar_guru = req(admin, "/admin/guru")
must("(nonaktif)" in daftar_guru and "Aktifkan" in daftar_guru,
     "guru nonaktif tampil dengan tombol Aktifkan")
g_mati, url, body = login("guruuji", "guruuji123")
must("dashboard" not in url and "Akun ini sudah dinonaktifkan." in body,
     "login guru nonaktif ditolak dengan pesan jelas")
s, url, hasil = req(admin, f"/admin/guru/{g1_id}/aktifkan", {})
must("Guru diaktifkan kembali." in hasil, "aktifkan guru berhasil")
guru, url, _ = login("guruuji", "guruuji123")
must("dashboard" in url, "guru bisa login lagi setelah diaktifkan")
# guru kedua tidak pernah scan, TAPI sudah punya Alpa otomatis (metode auto)
# sejak tabel dibuka lewat jam 09:00 -> hapus berarti dinonaktifkan juga.
s, url, hasil = req(admin, f"/admin/guru/{g2_id}/hapus", {})
must("Guru dinonaktifkan karena sudah punya riwayat kehadiran." in hasil,
     "hapus guru ber-riwayat Alpa otomatis -> dinonaktifkan")
# guru ketiga yang BARU dibuat (belum ada catatan apa pun) -> hapus permanen
req(admin, "/admin/guru", {"nama": "Guru Uji Tiga", "username": "guruujitiga",
                           "password": "guruujitiga123", "mapel": "UJI-Seni"})
s, url, daftar_guru = req(admin, "/admin/guru")
g3_id = id_dari_daftar(daftar_guru, "/admin/guru", "guruujitiga")
must(g3_id, f"id guru ketiga terbaca: {g3_id}")
s, url, hasil = req(admin, f"/admin/guru/{g3_id}/hapus", {})
must("Guru dihapus." in hasil, "hapus guru tanpa riwayat -> terhapus permanen")
s, url, daftar_guru = req(admin, "/admin/guru")
must("guruujitiga" not in daftar_guru, "guru terhapus hilang dari daftar")

# ===========================================================================
# Fase I — Backup & Pulihkan: unduh ZIP, musibah (DB kosong), pulihkan,
# berkas salah ditolak, guru tidak boleh akses
# ===========================================================================
import json
import zipfile

DBPULIH = "/tmp/absen-pulih.db"
DBAMAN = "/tmp/absen-sebelum-pulih.db"
for _p in (DBPULIH, DBAMAN):
    if os.path.exists(_p):
        os.remove(_p)

start_server("2026-11-16T10:00:00")
admin, url, _ = login("admin", "admin123")
# guru tidak boleh membuka halaman maupun unduhan backup
guru, url, _ = login("guruuji", "guruuji123")
try:
    req(guru, "/admin/backup")
    must(False, "guru membuka halaman Backup harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru membuka /admin/backup -> 404")
try:
    req_bytes(guru, "/admin/backup/unduh")
    must(False, "guru mengunduh backup harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru membuka /admin/backup/unduh -> 404")
# data khusus backup: kelas + siswa baru
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Backup"})
s, url, form_siswa = req(admin, "/admin/siswa")
kb = opt_val(form_siswa, "UJI-Kelas-Backup")
must(kb, "kelas khusus backup dibuat")
req(admin, "/admin/siswa", {"nama": "Siswa Uji Backup", "kode": "UJI-BKP", "class_id": kb})
# wallpaper dipasang agar ZIP backup berisi folder uploads/
s, url, hasil = unggah(admin, "/admin/tampilan/wallpaper", "wallpaper",
                       "wp-backup.png", PNG_1PX)
must("Wallpaper dipasang." in hasil, "wallpaper terpasang sebelum backup dibuat")
# halaman backup untuk admin: tautan unduh + peringatan penggantian data
s, url, hal_backup = req(admin, "/admin/backup")
must("Backup & Pulihkan" in hal_backup and "/admin/backup/unduh" in hal_backup
     and "MENGGANTI" in hal_backup and "SELURUH data" in hal_backup,
     "halaman Backup tampil dengan tombol unduh + peringatan jelas")
# unduh backup & periksa isi ZIP
st, hdr, zipdata = req_bytes(admin, "/admin/backup/unduh")
must(st == 200 and hdr.get_content_type() == "application/zip",
     "unduh backup -> 200 dengan content-type zip")
cd = hdr.get("Content-Disposition") or ""
must("backup-absen-2026-11-16.zip" in cd,
     f"nama berkas backup memakai tanggal Jakarta: {cd}")
zf = zipfile.ZipFile(io.BytesIO(zipdata))
names = zf.namelist()
must(any(n.endswith(".db") and "/" not in n for n in names),
     f"ZIP backup berisi file database: {names}")
must("manifest.json" in names, "ZIP backup berisi manifest.json")
manifes = json.loads(zf.read("manifest.json").decode("utf-8"))
must(manifes.get("app") == "absen-multi-guru" and manifes.get("versi") == 1
     and bool(manifes.get("dibuat")), "manifest backup: app, versi & waktu dibuat benar")
must(any(n.startswith("uploads/") and not n.endswith("/") for n in names),
     "ZIP backup berisi berkas di folder uploads/ (wallpaper)")

# --- skenario musibah: server baru dengan database yang benar-benar kosong ---
start_server("2026-11-16T10:05:00", dbfile=DBPULIH)
admin2, url, _ = login("admin", "admin123")
must("dashboard" in url, "DB kosong baru: login admin awal berhasil")
s, url, daftar_kosong = req(admin2, "/admin/siswa")
must("Siswa Uji Backup" not in daftar_kosong,
     "DB baru masih kosong (data backup belum ada)")
# pulihkan dari ZIP yang tadi diunduh
s, url, hasil = unggah(admin2, "/admin/backup/pulihkan", "berkas",
                       "backup-absen-2026-11-16.zip", zipdata, "application/zip")
must("/login" in url and "Backup dipulihkan. Silakan masuk lagi." in hasil,
     "pulihkan backup -> diarahkan ke login dengan pesan sukses")
must(os.path.exists(DBAMAN),
     "salinan aman DB sebelum pulih tersimpan (absen-sebelum-pulih.db)")
admin2, url, _ = login("admin", "admin123")
must("dashboard" in url, "login lagi setelah pulih berhasil (akun dari backup)")
s, url, daftar_siswa = req(admin2, "/admin/siswa")
must("Siswa Uji Backup" in daftar_siswa and "UJI-BKP" in daftar_siswa,
     "setelah pulih: siswa dari backup kembali tampil di daftar")
s, url, daftar_kelas = req(admin2, "/admin/kelas")
must("UJI-Kelas-Backup" in daftar_kelas,
     "setelah pulih: kelas dari backup kembali tampil di daftar")
s, url, hal_tampilan = req(admin2, "/admin/tampilan")
must(re.search(r"/wallpaper/[0-9a-f]{24}\.png", hal_tampilan) is not None,
     "setelah pulih: wallpaper dari backup ikut kembali terpasang")
# berkas yang salah ditolak & data tidak berubah
s, url, hasil = unggah(admin2, "/admin/backup/pulihkan", "berkas",
                       "bukan-backup.png", PNG_1PX)
must("Berkas bukan file ZIP" in hasil, "unggah PNG (bukan ZIP) ditolak dengan pesan jelas")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps({"app": "data-jemaat-gereja", "versi": 1}))
    z.writestr("jemaat.db", b"dummy")
s, url, hasil = unggah(admin2, "/admin/backup/pulihkan", "berkas",
                       "backup-gereja.zip", buf.getvalue(), "application/zip")
must("Berkas ini bukan backup aplikasi absen." in hasil,
     "ZIP backup aplikasi lain (gereja) ditolak")
s, url, daftar_siswa = req(admin2, "/admin/siswa")
must("Siswa Uji Backup" in daftar_siswa,
     "data tidak berubah setelah dua pemulihan yang ditolak")

# ===========================================================================
# Fase J — Kamera gerbang MENYALA TERUS (scan berkelanjutan, halaman tidak
# reload) + mode ajax /api/scan membalas JSON berisi banner & daftar terbaru
# ===========================================================================
start_server("2026-11-17T06:50:00")
admin, url, _ = login("admin", "admin123")
s, url, hal_scan = req(admin, "/scan-gerbang")
must("Nyalakan Kamera" in hal_scan and "Matikan Kamera" in hal_scan,
     "halaman scan: tombol 'Nyalakan Kamera' & 'Matikan Kamera'")
must('id="hasilScan"' in hal_scan and 'id="daftarDatang"' in hal_scan
     and 'id="jumlahDatang"' in hal_scan,
     "halaman scan: wadah hasil + daftar kedatangan untuk update tanpa reload")
st, hdr, js_bytes = req_bytes(admin, "/static/app.js")
js = js_bytes.decode("utf-8")
must("JEDA_GLOBAL_MS" in js and "DINGIN_KODE_MS" in js
     and "terakhirPerKode" in js and "catatKode" in js,
     "app.js: mekanisme jeda global + masa dingin per kode (anti scan dobel)")
must("matikanKamera" in js and "getTracks" in js and "pagehide" in js,
     "app.js: kamera hanya mati lewat Matikan Kamera / meninggalkan halaman")
must("form.submit()" not in js,
     "app.js: TIDAK ada lagi kirim form (reload halaman) setelah QR terbaca")
must("fetch('/api/scan'" in js and "'ajax'" in js,
     "app.js: hasil scan dikirim lewat fetch mode ajax ke /api/scan")
# mode ajax: scan pertama membalas JSON Hadir + daftar kedatangan terisi
s, url, resp = req(admin, "/api/scan", {"kode": "UJI-001", "ajax": "1"})
data_scan = json.loads(resp)
must(data_scan["jenis"] == "hadir" and data_scan["judul"] == "Siswa Uji A"
     and "06:50" in data_scan["pesan"],
     "ajax scan siswa A 06:50 -> JSON Hadir 06:50")
must(data_scan["jumlah"] == 1 and "Siswa Uji A" in data_scan["items"],
     "ajax: daftar kedatangan terbarui memuat siswa A (jumlah 1)")
# mode ajax: scan ulang -> info 'Sudah tercatat', daftar tidak berubah
s, url, resp = req(admin, "/api/scan", {"kode": "UJI-001", "ajax": "1"})
data_scan = json.loads(resp)
must(data_scan["jenis"] == "info" and "Sudah tercatat" in data_scan["judul"]
     and data_scan["jumlah"] == 1,
     "ajax scan ulang -> 'Sudah tercatat', jumlah tetap 1")
# mode ajax: kode asing -> JSON error
s, url, resp = req(admin, "/api/scan", {"kode": "TIDAK-ADA", "ajax": "1"})
data_scan = json.loads(resp)
must(data_scan["jenis"] == "error" and "tidak dikenal" in data_scan["judul"],
     "ajax kode asing -> JSON error 'Kode tidak dikenal'")
# tanpa ajax: perilaku lama (balasan halaman penuh) tetap jalan persis sama
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-002"})
must("Siswa Uji B" in hasil and "Scan Gerbang" in hasil and "Hadir" in hasil,
     "POST /api/scan tanpa ajax tetap membalas halaman penuh seperti sebelumnya")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal='2026-11-17' "
         "AND ref_type='siswa'") == 2,
     "dua scan (ajax + non-ajax) tercatat sebagai kehadiran 2026-11-17")

# ===========================================================================
# Fase K — Detail kelas: klik nama kelas di Kelola Kelas -> daftar siswa
# kelas itu (nama + NIS), tambah-cepat, keadaan kosong, hak akses
# ===========================================================================
start_server()
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Detail"})
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Lain"})
s, url, form_siswa = req(admin, "/admin/siswa")
kd = opt_val(form_siswa, "UJI-Kelas-Detail")
kl = opt_val(form_siswa, "UJI-Kelas-Lain")
must(kd and kl, f"id kelas detail/lain terbaca: {kd}, {kl}")
req(admin, "/admin/siswa", {"nama": "Siswa Detail Satu", "kode": "UJI-D01", "class_id": kd})
req(admin, "/admin/siswa", {"nama": "Siswa Detail Dua", "kode": "UJI-D02", "class_id": kd})
req(admin, "/admin/siswa", {"nama": "Siswa Lain Kelas", "kode": "UJI-L01", "class_id": kl})
# daftar kelas: jumlah siswa + tautan detail pada baris kelas yang benar
s, url, daftar_kelas = req(admin, "/admin/kelas")
chunk_d = next((c for c in daftar_kelas.split("<li>") if "UJI-Kelas-Detail" in c), "")
chunk_l = next((c for c in daftar_kelas.split("<li>") if "UJI-Kelas-Lain" in c), "")
must("2 siswa" in chunk_d and f'href="/admin/kelas/{kd}"' in chunk_d,
     "daftar kelas: UJI-Kelas-Detail tampil '2 siswa' + tautan ke detailnya")
must("1 siswa" in chunk_l and f'href="/admin/kelas/{kl}"' in chunk_l,
     "daftar kelas: UJI-Kelas-Lain tampil '1 siswa' + tautan ke detailnya")
# halaman detail: hanya siswa kelas itu sendiri, lengkap nama + NIS
s, url, detail = req(admin, f"/admin/kelas/{kd}")
must("Kelas UJI-Kelas-Detail" in detail and "2 siswa" in detail,
     "detail kelas: judul memuat nama kelas + jumlah siswanya")
must("Siswa Detail Satu" in detail and "UJI-D01" in detail
     and "Siswa Detail Dua" in detail and "UJI-D02" in detail,
     "detail kelas: memuat nama + NIS kedua siswa kelas itu")
must("Siswa Lain Kelas" not in detail and "UJI-L01" not in detail,
     "detail kelas: TIDAK memuat siswa kelas lain")
must('action="/admin/siswa"' in detail
     and f'name="class_id" value="{kd}"' in detail,
     "detail kelas: formulir Tambah Siswa dengan kelas sudah terpilih (hidden)")
must("Kembali ke daftar kelas" in detail,
     "detail kelas: ada tautan 'Kembali ke daftar kelas'")
# tambah-cepat dari halaman detail -> siswa tampil & hitungan naik
req(admin, "/admin/siswa", {"nama": "Siswa Detail Tiga", "kode": "UJI-D03", "class_id": kd})
s, url, detail = req(admin, f"/admin/kelas/{kd}")
must("Siswa Detail Tiga" in detail and "UJI-D03" in detail and "3 siswa" in detail,
     "tambah-cepat: siswa baru tampil di detail, jumlah naik jadi 3 siswa")
s, url, daftar_kelas = req(admin, "/admin/kelas")
chunk_d = next((c for c in daftar_kelas.split("<li>") if "UJI-Kelas-Detail" in c), "")
must("3 siswa" in chunk_d, "daftar kelas: jumlah UJI-Kelas-Detail ikut naik jadi 3 siswa")
# kelas kosong: pesan keadaan kosong + hitungan 0
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Hampa"})
s, url, form_siswa = req(admin, "/admin/siswa")
kh = opt_val(form_siswa, "UJI-Kelas-Hampa")
must(kh, "kelas hampa dibuat")
s, url, detail_kosong = req(admin, f"/admin/kelas/{kh}")
must("Belum ada siswa di kelas ini." in detail_kosong and "0 siswa" in detail_kosong,
     "detail kelas kosong: pesan 'Belum ada siswa di kelas ini.' + judul '0 siswa'")
s, url, daftar_kelas = req(admin, "/admin/kelas")
chunk_h = next((c for c in daftar_kelas.split("<li>") if "UJI-Kelas-Hampa" in c), "")
must("0 siswa" in chunk_h, "daftar kelas: kelas kosong tampil '0 siswa'")
# guru tidak boleh membuka detail kelas -> 404
guru, url, _ = login("guruuji", "guruuji123")
try:
    req(guru, f"/admin/kelas/{kd}")
    must(False, "guru membuka detail kelas harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru membuka detail kelas -> 404")
# id kelas tidak dikenal -> kembali ke daftar + pesan
s, url, hasil = req(admin, "/admin/kelas/99999")
must("Data tidak ditemukan." in hasil
     and urllib.parse.urlparse(url).path == "/admin/kelas",
     "detail id tak dikenal -> diarahkan ke daftar kelas + 'Data tidak ditemukan.'")

# ===========================================================================
# Fase L — Pencarian global dari Dashboard: GET /cari?q= cocok pada nama
# siswa, NIS, nama kelas, nama guru, dan mata pelajaran (mapel). Guru boleh
# memakai & melihat hasil, tapi TANPA tautan /admin/; anonim -> /login.
# ===========================================================================
start_server()
admin, url, _ = login("admin", "admin123")
# --- siapkan data khas pencarian lewat alur admin yang sudah ada ---
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Cari"})
s, url, form_siswa = req(admin, "/admin/siswa")
kc = opt_val(form_siswa, "UJI-Kelas-Cari")
must(kc, "kelas UJI-Kelas-Cari dibuat untuk uji pencarian")
req(admin, "/admin/siswa", {"nama": "Cari Siswa Unik", "kode": "UJI-C01", "class_id": kc})
req(admin, "/admin/siswa", {"nama": "Cari Teman Sekelas", "kode": "UJI-C02", "class_id": kc})
req(admin, "/admin/guru", {"nama": "Guru Cari Spesial", "username": "gurucari",
                           "password": "gurucari123", "mapel": "UJI-Senilukis"})
# --- dashboard memuat formulir pencarian ---
s, url, dash = req(admin, "/dashboard")
must('action="/cari"' in dash and 'name="q"' in dash
     and "Cari nama siswa, NIS, kelas, nama guru, atau mata pelajaran" in dash,
     "dashboard: formulir pencarian (GET /cari, input q, placeholder lengkap)")
# --- cari potongan nama siswa: siswa itu + NIS + kelas; siswa lain tidak ikut ---
s, url, hasil = req(admin, "/cari?q=Siswa+Unik")
must("Cari Siswa Unik" in hasil and "UJI-C01" in hasil and "UJI-Kelas-Cari" in hasil,
     "cari nama siswa: ketemu nama + NIS + kelasnya")
must("Siswa Lain Kelas" not in hasil and "Cari Teman Sekelas" not in hasil,
     "cari nama siswa: siswa tak terkait TIDAK ikut tampil")
must(f'href="/admin/kelas/{kc}"' in hasil and "/admin/siswa/" in hasil
     and "/edit" in hasil,
     "hasil siswa untuk admin: tautan Lihat Kelas + Edit")
must('Ditemukan 1 siswa dan 0 guru untuk "Siswa Unik"' in hasil,
     "ringkasan hasil: 'Ditemukan 1 siswa dan 0 guru'")
# huruf kecil tetap ketemu (tidak peka huruf besar/kecil)
s, url, hasil = req(admin, "/cari?q=siswa+unik")
must("Cari Siswa Unik" in hasil, "pencarian tidak peka huruf besar/kecil")
# --- cari potongan NIS ---
s, url, hasil = req(admin, "/cari?q=C02")
must("Cari Teman Sekelas" in hasil and "UJI-C02" in hasil,
     "cari potongan NIS: ketemu siswa pemilik NIS itu")
# --- cari nama kelas: semua siswa kelas itu tampil ---
s, url, hasil = req(admin, "/cari?q=UJI-Kelas-Cari")
must("Cari Siswa Unik" in hasil and "Cari Teman Sekelas" in hasil,
     "cari nama kelas: semua siswa kelas itu tampil")
# --- cari nama guru: guru + mapelnya; admin dapat tautan Edit guru ---
s, url, hasil = req(admin, "/cari?q=Guru+Cari")
must("Guru Cari Spesial" in hasil and "UJI-Senilukis" in hasil,
     "cari nama guru: ketemu guru + mata pelajarannya")
must("/admin/guru/" in hasil and "/edit" in hasil,
     "hasil guru untuk admin: tautan Edit guru")
# --- cari potongan mapel ---
s, url, hasil = req(admin, "/cari?q=Matematika")
must("Guru Uji" in hasil and "UJI-Matematika" in hasil,
     "cari mapel 'Matematika': ketemu guru yang mengajar mapel itu")
# --- kata kunci ngawur: keadaan kosong ---
s, url, hasil = req(admin, "/cari?q=zzzqqq")
must('Tidak ada hasil untuk "zzzqqq". Coba kata kunci lain.' in hasil,
     "tanpa hasil: pesan keadaan kosong yang ramah")
# --- q kosong: ajakan mengetik + formulir tampil lagi ---
s, url, hasil = req(admin, "/cari?q=")
must("Ketik kata kunci untuk mencari." in hasil and 'action="/cari"' in hasil,
     "q kosong: ajakan 'Ketik kata kunci untuk mencari.' + formulir pencarian")
# --- guru BOLEH mencari & melihat hasil, tapi TANPA tautan /admin/ apa pun ---
guru, url, _ = login("guruuji", "guruuji123")
s, url, dash_guru = req(guru, "/dashboard")
must('action="/cari"' in dash_guru and 'name="q"' in dash_guru,
     "dashboard guru juga memuat formulir pencarian")
s, url, hasil_guru = req(guru, "/cari?q=Cari")
must("Cari Siswa Unik" in hasil_guru and "Guru Cari Spesial" in hasil_guru,
     "guru bisa memakai /cari dan melihat hasil siswa + guru")
must("/admin/" not in hasil_guru,
     "hasil pencarian untuk guru: TIDAK ada tautan /admin/ sama sekali")
must(f"/tabel-siswa?kelas={kc}" in hasil_guru,
     "hasil siswa untuk guru: tautan 'Lihat Kelas' ke tabel mingguan kelas itu")
# --- anonim diarahkan ke /login ---
anon = client()
s, url, hal = req(anon, "/cari?q=Cari")
must(urllib.parse.urlparse(url).path == "/login",
     "anonim membuka /cari -> diarahkan ke /login")

# ===========================================================================
# Fase N — Dashboard: penghitung HARI INI. "Siswa Hadir" mencakup Hadir +
# Terlambat (yang terlambat tetap dihitung hadir), "Guru Hadir" juga begitu,
# dan "Siswa Belum Scan" turun 1 per scan / catatan manual. (Definisi
# "Belum Scan" disempurnakan di Fase P: Alpa OTOMATIS tidak menguranginya;
# semua pemeriksaan fase ini berjalan sebelum 09:00 sehingga tetap berlaku.)
# Simulasi Rabu 2026-11-18: tanggal baru yang belum punya catatan sama
# sekali, jadi angka awal pasti 0 / total siswa. Delta dihitung dari angka
# awal yang dibaca dari dashboard sendiri agar tahan data fase sebelumnya.
# ===========================================================================
TGL_N = "2026-11-18"


def angka_kartu(dash_html, label):
    m = re.search(r"<b>(\d+)</b><small>" + re.escape(label) + r"</small>", dash_html)
    return int(m.group(1)) if m else None


start_server()  # waktu nyata: siapkan data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Dash"})
s, url, form_siswa = req(admin, "/admin/siswa")
kd = opt_val(form_siswa, "UJI-Kelas-Dash")
must(kd, "kelas UJI-Kelas-Dash dibuat untuk uji dashboard")
for i in range(1, 6):
    req(admin, "/admin/siswa",
        {"nama": f"Siswa Dash {i}", "kode": f"UJI-H0{i}", "class_id": kd})
req(admin, "/admin/guru", {"nama": "Guru Dash Satu", "username": "gurudash1",
                           "password": "gurudash123", "mapel": "UJI-DashMapel"})
req(admin, "/admin/guru", {"nama": "Guru Dash Dua", "username": "gurudash2",
                           "password": "gurudash123", "mapel": "UJI-DashMapel"})
s, url, daftar_siswa_n = req(admin, "/admin/siswa")
d3_id = id_dari_daftar(daftar_siswa_n, "/admin/siswa", "UJI-H03")
must(d3_id, "id siswa UJI-H03 terbaca untuk input Sakit manual")
s, url, daftar_guru_n = req(admin, "/admin/guru")
chunk_gd = next((c for c in daftar_guru_n.split("<li>") if "gurudash1" in c), "")
m_qr_n = re.search(r"kode QR: <code>(G\d+)</code>", chunk_gd)
must(m_qr_n is not None, "kode QR Guru Dash Satu terbaca")
gd1_qr = m_qr_n.group(1)
total_siswa_n = dbq("SELECT COUNT(*) FROM students")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=?", (TGL_N,)) == 0,
     "tanggal uji dashboard 2026-11-18 belum punya catatan kehadiran apa pun")

# --- Rabu 06:30, sebelum scan apa pun ---
start_server(f"{TGL_N}T06:30:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
sh0 = angka_kartu(dash, "Siswa Hadir")
gh0 = angka_kartu(dash, "Guru Hadir")
bs0 = angka_kartu(dash, "Siswa Belum Scan")
must(sh0 == 0, f"dashboard 06:30 sebelum scan: Siswa Hadir = 0 (terbaca {sh0})")
must(gh0 == 0, f"dashboard 06:30 sebelum scan: Guru Hadir = 0 (terbaca {gh0})")
must(bs0 == total_siswa_n,
     f"dashboard 06:30 sebelum scan: Siswa Belum Scan = total siswa "
     f"{total_siswa_n} (terbaca {bs0})")
# scan siswa 1 pukul 06:30 -> Hadir
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-H01"})
must("Siswa Dash 1" in hasil and "Hadir" in hasil,
     "scan Siswa Dash 1 pukul 06:30 -> Hadir")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Hadir") == sh0 + 1,
     "setelah 1 scan Hadir: Siswa Hadir naik 1")
must(angka_kartu(dash, "Siswa Belum Scan") == bs0 - 1,
     "setelah 1 scan Hadir: Siswa Belum Scan turun 1")

# --- Rabu 07:30: scan Terlambat tetap dihitung Hadir ---
start_server(f"{TGL_N}T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-H02"})
must("Siswa Dash 2" in hasil and "Terlambat" in hasil,
     "scan Siswa Dash 2 pukul 07:30 -> Terlambat")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Hadir") == sh0 + 2,
     "scan Terlambat tetap dihitung hadir: Siswa Hadir naik lagi 1")
must(angka_kartu(dash, "Siswa Terlambat") == 1,
     "kartu info Siswa Terlambat tetap menunjukkan 1")
must(angka_kartu(dash, "Siswa Belum Scan") == bs0 - 2,
     "setelah scan Terlambat: Siswa Belum Scan turun lagi 1")
# guru scan Terlambat -> Guru Hadir naik (terlambat dihitung hadir)
s, url, hasil = req(admin, "/api/scan", {"kode": gd1_qr})
must("Guru Dash Satu" in hasil and "Terlambat" in hasil,
     "scan Guru Dash Satu pukul 07:30 -> Terlambat")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Guru Hadir") == gh0 + 1,
     "guru Terlambat dihitung hadir: Guru Hadir naik 1")
# Sakit manual: Siswa Hadir TIDAK berubah, Siswa Belum Scan turun 1
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": TGL_N, "tipe": "siswa", "id": d3_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "input manual Sakit Siswa Dash 3 tersimpan")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Hadir") == sh0 + 2,
     "Sakit manual: Siswa Hadir tidak berubah")
must(angka_kartu(dash, "Siswa Belum Scan") == bs0 - 3,
     "Sakit manual: Siswa Belum Scan turun 1 (sudah ada catatan hari ini)")

# ===========================================================================
# Fase O — Toggle "mata" password: layout kini menyertakan /static/app.js
# di SEMUA halaman (varian login maupun shell yang sudah masuk), jadi tiap
# kolom password — login, Ganti Password, Tambah Guru, Edit Guru — otomatis
# mendapat tombol mata dari app.js. Isi app.js/style.css memuat logika &
# gaya .pw-toggle/.pw-wrap; halaman scan tetap memuat app.js tepat 1x.
# ===========================================================================
start_server()
s, url, login_html = req(client(), "/login")
must('type="password"' in login_html, "halaman login punya kolom password")
must('<script src="/static/app.js"></script>' in login_html,
     "halaman login memuat /static/app.js (sumber toggle mata password)")
admin, url, _ = login("admin", "admin123")
s, url, gp_html = req(admin, "/ganti-password")
must('type="password"' in gp_html, "halaman Ganti Password punya kolom password")
must('<script src="/static/app.js"></script>' in gp_html,
     "halaman Ganti Password memuat /static/app.js")
s, url, guru_html = req(admin, "/admin/guru")
must('type="password"' in guru_html, "form Tambah Guru punya kolom password awal")
must('<script src="/static/app.js"></script>' in guru_html,
     "halaman Kelola Guru memuat /static/app.js")
gid_o = id_dari_daftar(guru_html, "/admin/guru", "gurudash1")
must(gid_o, "id guru terbaca untuk uji form Edit Guru")
s, url, edit_guru_html = req(admin, f"/admin/guru/{gid_o}/edit")
must('type="password"' in edit_guru_html,
     "form Edit Guru punya kolom password baru")
must('<script src="/static/app.js"></script>' in edit_guru_html,
     "halaman Edit Guru memuat /static/app.js")
s, headers, js_bytes = req_bytes(admin, "/static/app.js")
js = js_bytes.decode("utf-8", "replace")
must("pw-toggle" in js and "pw-wrap" in js,
     "app.js memuat logika toggle mata password (pw-toggle/pw-wrap)")
must("input.type = terlihat ? 'password' : 'text'" in js,
     "app.js mengganti type password <-> text saat tombol mata diklik")
must("Tampilkan password" in js and "Sembunyikan password" in js,
     "app.js mengganti aria-label Tampilkan/Sembunyikan password")
s, headers, css_bytes = req_bytes(admin, "/static/style.css")
css = css_bytes.decode("utf-8", "replace")
must(".pw-toggle" in css and ".pw-wrap" in css,
     "style.css memuat gaya .pw-toggle dan .pw-wrap")
s, url, scan_html = req(admin, "/scan-gerbang")
must(scan_html.count("/static/app.js") == 1,
     "halaman Scan Gerbang memuat app.js tepat satu kali (dari layout)")
must("jsQR" in scan_html, "halaman Scan Gerbang tetap memuat jsQR dari CDN")

# ===========================================================================
# Fase P — "Belum Scan" TIDAK dikurangi Alpa otomatis. Definisi:
# Siswa Belum Scan = total siswa - siswa yang hari ini SUDAH scan (Hadir/
# Terlambat) atau dicatat manual (Sakit/Izin/Alpa manual/dll). Siswa yang
# hanya punya catatan Alpa otomatis (metode='auto') TIDAK mengurangi —
# mereka tidak pernah scan, jadi tetap "belum scan" sepanjang hari, juga
# sesudah 09:00 saat Alpa otomatis termaterialisasi. Kartu baru "Guru
# Belum Scan" memakai prinsip yang sama terhadap total guru AKTIF.
# Simulasi Kamis 2026-11-19 (tanggal segar, belum dipakai fase lain);
# delta dibaca dari dashboard sendiri agar tahan data fase sebelumnya.
# ===========================================================================
TGL_P = "2026-11-19"

start_server()  # waktu nyata: siapkan data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Belum"})
s, url, form_siswa = req(admin, "/admin/siswa")
kb_p = opt_val(form_siswa, "UJI-Kelas-Belum")
must(kb_p, "kelas UJI-Kelas-Belum dibuat untuk uji Belum Scan")
for i in range(1, 4):
    req(admin, "/admin/siswa",
        {"nama": f"Siswa Belum {i}", "kode": f"UJI-B0{i}", "class_id": kb_p})
req(admin, "/admin/guru", {"nama": "Guru Belum Satu", "username": "gurubelum1",
                           "password": "gurubelum123", "mapel": "UJI-BelumMapel"})
s, url, daftar_siswa_p = req(admin, "/admin/siswa")
b2_id = id_dari_daftar(daftar_siswa_p, "/admin/siswa", "UJI-B02")
must(b2_id, "id siswa UJI-B02 terbaca untuk input Sakit manual")
s, url, daftar_guru_p = req(admin, "/admin/guru")
chunk_gb = next((c for c in daftar_guru_p.split("<li>") if "gurubelum1" in c), "")
m_qr_p = re.search(r"kode QR: <code>(G\d+)</code>", chunk_gb)
must(m_qr_p is not None, "kode QR Guru Belum Satu terbaca")
gb1_qr = m_qr_p.group(1)
total_siswa_p = dbq("SELECT COUNT(*) FROM students")
total_guru_p = dbq("SELECT COUNT(*) FROM users WHERE role='guru' AND aktif=1")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=?", (TGL_P,)) == 0,
     "tanggal uji Belum Scan 2026-11-19 belum punya catatan kehadiran apa pun")

# --- Kamis 06:30, sebelum ada catatan apa pun hari itu ---
start_server(f"{TGL_P}T06:30:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Guru Belum Scan") is not None,
     "dashboard memuat kartu 'Guru Belum Scan'")
sh_p0 = angka_kartu(dash, "Siswa Hadir")
gh_p0 = angka_kartu(dash, "Guru Hadir")
bs_p0 = angka_kartu(dash, "Siswa Belum Scan")
gb_p0 = angka_kartu(dash, "Guru Belum Scan")
must(bs_p0 == total_siswa_p,
     f"dashboard 06:30 sebelum scan: Siswa Belum Scan = total siswa "
     f"{total_siswa_p} (terbaca {bs_p0})")
must(gb_p0 == total_guru_p,
     f"dashboard 06:30 sebelum scan: Guru Belum Scan = total guru aktif "
     f"{total_guru_p} (terbaca {gb_p0})")
# scan siswa 1 tepat waktu -> Hadir
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-B01"})
must("Siswa Belum 1" in hasil and "Hadir" in hasil,
     "scan Siswa Belum 1 pukul 06:30 -> Hadir")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Belum Scan") == bs_p0 - 1,
     "setelah 1 scan Hadir: Siswa Belum Scan turun 1")
must(angka_kartu(dash, "Siswa Hadir") == sh_p0 + 1,
     "setelah 1 scan Hadir: Siswa Hadir naik 1")
# Sakit manual siswa 2: Belum Scan turun 1, Siswa Hadir tidak berubah
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": TGL_P, "tipe": "siswa", "id": b2_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "input manual Sakit Siswa Belum 2 tersimpan")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Belum Scan") == bs_p0 - 2,
     "Sakit manual: Siswa Belum Scan turun 1")
must(angka_kartu(dash, "Siswa Hadir") == sh_p0 + 1,
     "Sakit manual: Siswa Hadir tidak berubah")

# --- Kamis 07:30: guru scan Terlambat ---
start_server(f"{TGL_P}T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": gb1_qr})
must("Guru Belum Satu" in hasil and "Terlambat" in hasil,
     "scan Guru Belum Satu pukul 07:30 -> Terlambat")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Guru Belum Scan") == gb_p0 - 1,
     "guru scan Terlambat: Guru Belum Scan turun 1")
must(angka_kartu(dash, "Guru Hadir") == gh_p0 + 1,
     "guru scan Terlambat: Guru Hadir naik 1")
bs_pre = angka_kartu(dash, "Siswa Belum Scan")
gb_pre = angka_kartu(dash, "Guru Belum Scan")
must(bs_pre == bs_p0 - 2 and gb_pre == gb_p0 - 1,
     "angka Belum Scan pra-09:00 tercatat sebagai pembanding regresi")

# --- Kamis 09:30: Alpa otomatis termaterialisasi untuk semua sisanya ---
start_server(f"{TGL_P}T09:30:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Belum Scan") == bs_pre,
     "sesudah 09:00: Siswa Belum Scan TIDAK berubah "
     "(Alpa otomatis tidak menguranginya, tidak jatuh ke 0)")
must(angka_kartu(dash, "Guru Belum Scan") == gb_pre,
     "sesudah 09:00: Guru Belum Scan TIDAK berubah "
     "(Alpa otomatis tidak menguranginya, tidak jatuh ke 0)")
must(angka_kartu(dash, "Siswa Alpa") == total_siswa_p - 2,
     "sesudah 09:00: kartu Siswa Alpa memuat Alpa otomatis untuk semua "
     "siswa tanpa catatan scan/manual")

# ===========================================================================
# Fase Q — Kartu statistik dashboard SATU baris. style.css: .stat-grid
# memakai grid 7 kolom sama lebar (repeat(7, minmax(0, 1fr))) di layar
# lebar, dan di layar kecil menjadi baris flex overflow-x:auto (kartu
# flex: 0 0 auto, bisa digeser menyamping, tidak membungkus ke baris
# kedua). Dashboard tetap merender tepat 7 kartu statistik.
# ===========================================================================
start_server()
admin, url, _ = login("admin", "admin123")
s, headers, css_bytes = req_bytes(admin, "/static/style.css")
css_q = css_bytes.decode("utf-8", "replace")
m_grid = re.search(r"\.stat-grid\s*\{([^}]*)\}", css_q)
must(m_grid is not None and "repeat(7, minmax(0, 1fr))" in m_grid.group(1),
     "style.css: .stat-grid memakai repeat(7, minmax(0, 1fr)) "
     "(7 kartu statistik satu baris sama lebar)")
must(re.search(r"\.stat-grid\s*\{[^}]*overflow-x:\s*auto", css_q) is not None,
     "style.css: .stat-grid layar kecil memakai overflow-x: auto "
     "(satu baris, geser menyamping)")
must(re.search(r"\.stat-card\s*\{[^}]*flex:\s*0 0 auto", css_q) is not None
     and re.search(r"\.stat-card\s*\{[^}]*min-width:\s*126px", css_q) is not None,
     "style.css: .stat-card layar kecil flex: 0 0 auto + min-width 126px")
s, url, dash_q = req(admin, "/dashboard")
must(dash_q.count('<div class="stat-card">') == 7,
     "dashboard merender tepat 7 kartu statistik")

# ===========================================================================
# Fase R — Nomor urut (No. 1, 2, 3, ...) di daftar nama siswa & guru:
# Kelola Siswa, Kelola Guru, hasil /cari (tiap bagian mulai dari 1), dan
# kolom pertama "No" pada tabel mingguan siswa & guru. Detail kelas sudah
# memakai <ol> bernomor (marker desimal di CSS) — diverifikasi apa adanya.
# Pemeriksaan bersifat sekuens: nomor yang terambil dari HTML harus
# membentuk 1..N berurutan, tahan terhadap data uji kumulatif fase lain.
# ===========================================================================
admin, url, _ = login("admin", "admin123")


def nomor_dari(htmltxt, pola):
    return [int(x) for x in re.findall(pola, htmltxt)]


def sekuens_utuh(angka):
    return angka == list(range(1, len(angka) + 1))


# --- Kelola Siswa: setiap baris bernomor urut 1..N sesuai urutan tampil ---
s, url, hal_siswa = req(admin, "/admin/siswa")
no_siswa = nomor_dari(hal_siswa, r'<span class="no">(\d+)\.</span>')
must(len(no_siswa) >= 2 and sekuens_utuh(no_siswa),
     "Kelola Siswa: nomor baris membentuk urutan 1..N "
     f"(terambil {len(no_siswa)} baris)")
must(re.search(r'<span class="no">1\.</span> <b>', hal_siswa) is not None
     and re.search(r'<span class="no">2\.</span> <b>', hal_siswa) is not None,
     "Kelola Siswa: baris pertama & kedua berawalan '1.' dan '2.' sebelum nama")
# --- Kelola Guru: pola yang sama ---
s, url, hal_guru = req(admin, "/admin/guru")
no_guru = nomor_dari(hal_guru, r'<span class="no">(\d+)\.</span>')
must(len(no_guru) >= 2 and sekuens_utuh(no_guru),
     "Kelola Guru: nomor baris membentuk urutan 1..N "
     f"(terambil {len(no_guru)} baris)")
# --- /cari: bagian Siswa & Guru masing-masing bernomor mulai dari 1 ---
s, url, hasil_cari = req(admin, "/cari?q=Cari")
m_sis = re.search(r"<h3>Siswa</h3><ul class=\"plain-list\">(.*?)</ul>",
                  hasil_cari, re.S)
must(m_sis is not None, "/cari?q=Cari: bagian Siswa tampil")
no_cari_siswa = nomor_dari(m_sis.group(1), r'<span class="no">(\d+)\.</span>')
must(len(no_cari_siswa) >= 2 and sekuens_utuh(no_cari_siswa),
     "/cari: baris bagian Siswa bernomor 1..N dari awal "
     f"(terambil {len(no_cari_siswa)} baris)")
m_gur = re.search(r"<h3>Guru</h3><ul class=\"plain-list\">(.*?)</ul>",
                  hasil_cari, re.S)
must(m_gur is not None, "/cari?q=Cari: bagian Guru tampil")
no_cari_guru = nomor_dari(m_gur.group(1), r'<span class="no">(\d+)\.</span>')
must(len(no_cari_guru) >= 1 and sekuens_utuh(no_cari_guru),
     "/cari: baris bagian Guru bernomor mulai dari 1 lagi "
     f"(terambil {len(no_cari_guru)} baris)")
# --- detail kelas: <ol class="plain-list"> bernomor via marker desimal CSS ---
kc_r = dbq("SELECT id FROM classes WHERE nama='UJI-Kelas-Cari'")
s, url, hal_detail = req(admin, f"/admin/kelas/{kc_r}")
must('<ol class="plain-list">' in hal_detail,
     "detail kelas: daftar siswa memakai <ol> (nomor urut bawaan)")
s, headers, css_r_bytes = req_bytes(admin, "/static/style.css")
css_r = css_r_bytes.decode("utf-8", "replace")
must(re.search(r"ol\.plain-list\s*\{[^}]*list-style:\s*decimal", css_r) is not None,
     "style.css: ol.plain-list memakai list-style: decimal (nomor tampil)")
must(re.search(r"\.plain-list li \.no\s*\{", css_r) is not None,
     "style.css: .plain-list li .no ada (nomor daftar tampil redup)")
# --- tabel mingguan siswa: kolom pertama "No", sel data 1, 2, ... ---
s, url, tab_siswa = req(admin, f"/tabel-siswa?kelas={kc_r}")
must('<th class="no">No</th>' in tab_siswa,
     "tabel siswa: kepala kolom pertama 'No'")
no_tab_siswa = nomor_dari(tab_siswa, r'<td class="no">(\d+)</td>')
must(len(no_tab_siswa) >= 2 and no_tab_siswa[0] == 1 and no_tab_siswa[1] == 2
     and sekuens_utuh(no_tab_siswa),
     "tabel siswa: sel No baris pertama '1', kedua '2', lalu berurutan "
     f"(terambil {len(no_tab_siswa)} baris)")
# --- tabel mingguan guru: pola yang sama ---
s, url, tab_guru = req(admin, "/tabel-guru")
must('<th class="no">No</th>' in tab_guru,
     "tabel guru: kepala kolom pertama 'No'")
no_tab_guru = nomor_dari(tab_guru, r'<td class="no">(\d+)</td>')
must(len(no_tab_guru) >= 2 and no_tab_guru[0] == 1 and no_tab_guru[1] == 2
     and sekuens_utuh(no_tab_guru),
     "tabel guru: sel No baris pertama '1', kedua '2', lalu berurutan "
     f"(terambil {len(no_tab_guru)} baris)")

stop_server()
if os.path.exists(DBUJI):
    os.remove(DBUJI)
for _p in (DBPULIH, DBAMAN):
    if os.path.exists(_p):
        os.remove(_p)
if os.path.isdir(UPLOAD_DIR):
    for nama in set(os.listdir(UPLOAD_DIR)) - uploads_awal:
        try:
            os.remove(os.path.join(UPLOAD_DIR, nama))
        except OSError:
            pass
print("\nSEMUA UJI LULUS")
