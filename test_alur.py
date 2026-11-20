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

stop_server()
if os.path.exists(DBUJI):
    os.remove(DBUJI)
if os.path.isdir(UPLOAD_DIR):
    for nama in set(os.listdir(UPLOAD_DIR)) - uploads_awal:
        try:
            os.remove(os.path.join(UPLOAD_DIR, nama))
        except OSError:
            pass
print("\nSEMUA UJI LULUS")
