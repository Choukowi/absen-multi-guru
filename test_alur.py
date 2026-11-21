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
# Fase D — Senin 09:30: Alpa otomatis; scan sesudah 09:00 DITOLAK
# (keputusan user, dibuktikan lengkap di Fase T: scan pukul 09:00 ke atas
# tidak berlaku lagi — dulu scan masih boleh menggantikan Alpa otomatis,
# sekarang Alpa otomatisnya tetap berdiri)
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
must("Scan sudah ditutup" in hasil and "Siswa Uji D" in hasil,
     "scan siswa D pukul 09:30 DITOLAK: 'Scan sudah ditutup'")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal='2026-11-16' "
         "AND ref_type='siswa' AND ref_id=?", (D_id,)) == "Alpa/auto",
     "scan yang ditolak tidak mengubah catatan: D tetap Alpa/auto")
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
# (Tampilan kartu SESUDAH 09:00 kini mengikuti reset Fase T; rumus di
# fase ini dibuktikan pada jam-jam sebelum 09:00 dan lewat hitungan DB.)
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
# CATATAN Fase T: sejak keputusan user di Fase T, mulai 09:00 kartu
# statistik di-reset TAMPILANNYA ke nilai awal hari, jadi yang diperiksa
# di sini bukan lagi angka hasil hitungan (itu berlaku sebelum 09:00,
# dibuktikan blok-blok di atas dan Fase T): sesudah 09:00 Belum Scan
# kembali PENUH — jelas TIDAK jatuh ke 0 karena Alpa otomatis — dan
# Siswa Alpa tampil 0, sementara catatan Alpa otomatisnya sendiri tetap
# termaterialisasi di DB (reset hanya tampilan kartu).
start_server(f"{TGL_P}T09:30:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Belum Scan") == total_siswa_p,
     "sesudah 09:00: kartu Siswa Belum Scan di-reset ke total siswa "
     "(Alpa otomatis tidak membuatnya jatuh ke 0)")
must(angka_kartu(dash, "Guru Belum Scan") == total_guru_p,
     "sesudah 09:00: kartu Guru Belum Scan di-reset ke total guru aktif")
must(angka_kartu(dash, "Siswa Alpa") == 0,
     "sesudah 09:00: kartu Siswa Alpa di-reset tampilannya ke 0")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND ref_type='siswa' "
         "AND status='Alpa' AND metode='auto'", (TGL_P,)) == total_siswa_p - 2,
     "DB: Alpa otomatis tetap termaterialisasi untuk semua siswa tanpa "
     "catatan scan/manual (reset kartu hanya tampilan)")

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

# ===========================================================================
# Fase S — Panel dashboard berganti pukul 09:00: sebelum 09:00 tampil
# "Kedatangan Terakhir Hari Ini"; mulai 09:00 panel itu HILANG, diganti
# "Laporan Hari Ini" berisi DUA kartu yang bisa diklik — kolom Siswa
# dan kolom Guru — masing-masing hanya memuat angka ringkasan hari ini
# (Terlambat & Belum Scan). Daftar namanya TIDAK tampil di dashboard,
# melainkan di halaman /laporan/siswa dan /laporan/guru: dua bagian
# bernomor Terlambat & Belum Scan (Belum Scan = hanya Alpa otomatis /
# tanpa catatan sama sekali). Yang Sakit/Izin tidak muncul di daftar
# mana pun. Kartu dan halaman memakai _data_laporan yang sama, jadi
# angkanya selalu cocok. (Rancangan awal fase ini menampilkan 4 daftar
# langsung di dashboard; diubah per permintaan user menjadi 2 kartu
# yang bisa diklik — assertion daftar dipindah ke halaman laporan.)
# Daftar dihitung langsung dari catatan hari ini (bukan snapshot).
# CATATAN: dulu scan susulan sesudah 09:00 memindahkan orangnya dari
# Belum Scan ke Terlambat; keputusan user berikutnya (Fase T) menutup
# scan pukul 09:00 ke atas, jadi segmen 09:05 di fase ini kini
# membuktikan scan itu DITOLAK dan daftar tidak berubah.
# Simulasi Jumat 2026-11-20 (tanggal baru untuk fase
# ini). Tanggal ini mungkin sudah berisi Alpa otomatis milik data fase
# sebelumnya (dashboard waktu nyata memanggil pastikan_alpa untuk
# kemarin), jadi angka pada judul tiap bagian diverifikasi terhadap
# hitungan DB yang setara, dan keanggotaan diperiksa lewat nama khas.
# ===========================================================================
TGL_S = "2026-11-20"

start_server()  # waktu nyata: siapkan data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Lapor"})
s, url, form_siswa = req(admin, "/admin/siswa")
kl = opt_val(form_siswa, "UJI-Kelas-Lapor")
must(kl, "kelas UJI-Kelas-Lapor dibuat untuk uji laporan dashboard")
req(admin, "/admin/siswa", {"nama": "Siswa Lapor Telat", "kode": "UJI-R01", "class_id": kl})
req(admin, "/admin/siswa", {"nama": "Siswa Lapor Belum", "kode": "UJI-R02", "class_id": kl})
req(admin, "/admin/siswa", {"nama": "Siswa Lapor Sakit", "kode": "UJI-R03", "class_id": kl})
req(admin, "/admin/guru", {"nama": "Guru Lapor Telat", "username": "gurulapor1",
                           "password": "gurulapor123", "mapel": "UJI-LaporMapel"})
req(admin, "/admin/guru", {"nama": "Guru Lapor Belum", "username": "gurulapor2",
                           "password": "gurulapor123", "mapel": "UJI-LaporMapel"})
s, url, daftar_siswa_s = req(admin, "/admin/siswa")
c_sakit_id = id_dari_daftar(daftar_siswa_s, "/admin/siswa", "UJI-R03")
must(c_sakit_id, "id siswa UJI-R03 terbaca untuk input Sakit manual")
s, url, daftar_guru_s = req(admin, "/admin/guru")
chunk_gx = next((c for c in daftar_guru_s.split("<li>") if "gurulapor1" in c), "")
m_qr_s = re.search(r"kode QR: <code>(G\d+)</code>", chunk_gx)
must(m_qr_s is not None, "kode QR Guru Lapor Telat terbaca")
gx_qr = m_qr_s.group(1)
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND ref_type='siswa' "
         "AND ref_id IN (SELECT id FROM students WHERE kode LIKE 'UJI-R%')",
         (TGL_S,)) == 0,
     "siswa khas Fase S belum punya catatan apa pun pada 2026-11-20")


def blok_laporan(htmltxt, judul):
    m = re.search(r"<h4>" + re.escape(judul) + r" \((\d+)\)</h4>"
                  r"<ul class=\"plain-list\">(.*?)</ul>", htmltxt, re.S)
    return (int(m.group(1)), m.group(2)) if m else (None, "")


def kartu_laporan(htmltxt, href):
    """(Terlambat, Belum Scan) dari kartu panel laporan di dashboard
    yang menuju href; (None, None) bila kartunya tidak ada."""
    m = re.search(r'<a class="laporan-card" href="' + re.escape(href)
                  + r'">(.*?)</a>', htmltxt, re.S)
    if not m:
        return None, None
    blok = m.group(1)
    t = re.search(r"Terlambat: <b>(\d+)</b>", blok)
    b = re.search(r"Belum Scan: <b>(\d+)</b>", blok)
    return (int(t.group(1)) if t else None,
            int(b.group(1)) if b else None)


def exp_belum(tipe):
    if tipe == "siswa":
        return dbq("SELECT COUNT(*) FROM students s LEFT JOIN kehadiran k "
                   "ON k.tanggal=? AND k.ref_type='siswa' AND k.ref_id=s.id "
                   "WHERE k.id IS NULL OR (k.metode='auto' AND k.status='Alpa')",
                   (TGL_S,))
    return dbq("SELECT COUNT(*) FROM users u LEFT JOIN kehadiran k "
               "ON k.tanggal=? AND k.ref_type='guru' AND k.ref_id=u.id "
               "WHERE u.role='guru' AND u.aktif=1 "
               "AND (k.id IS NULL OR (k.metode='auto' AND k.status='Alpa'))",
               (TGL_S,))


# --- Jumat 07:30: siswa A scan Terlambat; siswa C dicatat Sakit manual ---
start_server(f"{TGL_S}T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-R01"})
must("Siswa Lapor Telat" in hasil and "Terlambat" in hasil,
     "scan Siswa Lapor Telat pukul 07:30 -> Terlambat")
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": TGL_S, "tipe": "siswa", "id": c_sakit_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "input manual Sakit Siswa Lapor Sakit tersimpan")

# --- Jumat 07:45: guru X scan Terlambat ---
start_server(f"{TGL_S}T07:45:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": gx_qr})
must("Guru Lapor Telat" in hasil and "Terlambat" in hasil,
     "scan Guru Lapor Telat pukul 07:45 -> Terlambat")

# --- Jumat 08:59: panel masih Kedatangan Terakhir Hari Ini ---
start_server(f"{TGL_S}T08:59:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Kedatangan Terakhir Hari Ini" in dash,
     "dashboard 08:59: panel 'Kedatangan Terakhir Hari Ini' tampil")
must("Laporan Hari Ini" not in dash,
     "dashboard 08:59: 'Laporan Hari Ini' belum tampil")
must('href="/laporan/siswa"' not in dash and 'href="/laporan/guru"' not in dash,
     "dashboard 08:59: kartu laporan (tautan /laporan/siswa & "
     "/laporan/guru) belum tampil")

# --- Jumat 09:01: panel berganti Laporan Hari Ini ---
start_server(f"{TGL_S}T09:01:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Laporan Hari Ini" in dash,
     "dashboard 09:01: panel 'Laporan Hari Ini' tampil")
must("Kedatangan Terakhir Hari Ini" not in dash,
     "dashboard 09:01: panel 'Kedatangan Terakhir Hari Ini' hilang")
# Panel kini hanya DUA kartu ringkasan yang bisa diklik (kolom Siswa &
# kolom Guru); daftar nama TIDAK tampil lagi di dashboard.
ks_t, ks_b = kartu_laporan(dash, "/laporan/siswa")
kg_t, kg_b = kartu_laporan(dash, "/laporan/guru")
must(ks_t is not None and ks_b is not None,
     "dashboard 09:01: kartu kolom Siswa tampil (tautan /laporan/siswa "
     "+ angka Terlambat & Belum Scan)")
must(kg_t is not None and kg_b is not None,
     "dashboard 09:01: kartu kolom Guru tampil (tautan /laporan/guru "
     "+ angka Terlambat & Belum Scan)")
must("Klik untuk lihat daftar" in dash,
     "dashboard 09:01: kartu laporan memuat petunjuk klik untuk "
     "melihat daftar")
must("Siswa Lapor Telat" not in dash and "Siswa Lapor Belum" not in dash
     and "Siswa Lapor Sakit" not in dash
     and "Guru Lapor Telat" not in dash and "Guru Lapor Belum" not in dash,
     "dashboard 09:01: TIDAK ada nama siswa/guru tampil di panel "
     "laporan (hanya dua kartu ringkasan)")
must(ks_t == dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND "
                 "ref_type='siswa' AND status='Terlambat'", (TGL_S,)),
     f"angka Terlambat pada kartu Siswa cocok dengan DB ({ks_t})")
must(kg_t == dbq("SELECT COUNT(*) FROM kehadiran k JOIN users u ON u.id=k.ref_id "
                 "WHERE k.tanggal=? AND k.ref_type='guru' AND k.status='Terlambat' "
                 "AND u.role='guru' AND u.aktif=1", (TGL_S,)),
     f"angka Terlambat pada kartu Guru cocok dengan DB ({kg_t})")
must(ks_b == exp_belum("siswa"),
     f"angka Belum Scan pada kartu Siswa cocok dengan DB ({ks_b})")
must(kg_b == exp_belum("guru"),
     f"angka Belum Scan pada kartu Guru cocok dengan DB ({kg_b})")
# Daftar namanya kini ada di halaman laporan masing-masing.
s, url, lap_s = req(admin, "/laporan/siswa")
s, url, lap_g = req(admin, "/laporan/guru")
must("Laporan Siswa Hari Ini" in lap_s and "20 November 2026" in lap_s,
     "halaman /laporan/siswa: judul 'Laporan Siswa Hari Ini' + tanggal "
     "hari ini dalam bahasa Indonesia")
must("Laporan Guru Hari Ini" in lap_g and "20 November 2026" in lap_g,
     "halaman /laporan/guru: judul 'Laporan Guru Hari Ini' + tanggal "
     "hari ini dalam bahasa Indonesia")
must("Kembali ke Dashboard" in lap_s and "Kembali ke Dashboard" in lap_g,
     "kedua halaman laporan memuat tautan 'Kembali ke Dashboard'")
n_st, b_st = blok_laporan(lap_s, "Siswa Terlambat")
n_sb, b_sb = blok_laporan(lap_s, "Siswa Belum Scan")
n_gt, b_gt = blok_laporan(lap_g, "Guru Terlambat")
n_gb, b_gb = blok_laporan(lap_g, "Guru Belum Scan")
must(n_st is not None and n_sb is not None and n_gt is not None and n_gb is not None,
     "halaman laporan memuat 4 bagian: Siswa Terlambat, Siswa Belum Scan, "
     "Guru Terlambat, Guru Belum Scan")
must(n_st == ks_t and n_sb == ks_b and n_gt == kg_t and n_gb == kg_b,
     "angka judul bagian di halaman laporan sama dengan angka pada "
     "kartu dashboard (satu sumber data yang sama)")
must("Siswa Lapor Telat" in b_st and "07:30" in b_st,
     "Siswa Terlambat: Siswa Lapor Telat tampil dengan jam scan 07:30")
must("Siswa Lapor Telat" not in b_sb,
     "Siswa Lapor Telat tidak ikut tampil di Siswa Belum Scan")
must("Siswa Lapor Belum" in b_sb,
     "Siswa Belum Scan: Siswa Lapor Belum (tidak pernah scan) tampil")
must("Siswa Lapor Sakit" not in b_st and "Siswa Lapor Sakit" not in b_sb,
     "siswa Sakit manual tidak muncul di Terlambat maupun Belum Scan")
must("Guru Lapor Telat" in b_gt and "07:45" in b_gt,
     "Guru Terlambat: Guru Lapor Telat tampil dengan jam scan 07:45")
must("Guru Lapor Belum" in b_gb and "Guru Lapor Telat" not in b_gb,
     "Guru Belum Scan: Guru Lapor Belum tampil, Guru Lapor Telat tidak")
must(n_st == dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND "
                 "ref_type='siswa' AND status='Terlambat'", (TGL_S,)),
     f"angka Siswa Terlambat pada judul bagian cocok dengan DB ({n_st})")
must(n_gt == dbq("SELECT COUNT(*) FROM kehadiran k JOIN users u ON u.id=k.ref_id "
                 "WHERE k.tanggal=? AND k.ref_type='guru' AND k.status='Terlambat' "
                 "AND u.role='guru' AND u.aktif=1", (TGL_S,)),
     f"angka Guru Terlambat pada judul bagian cocok dengan DB ({n_gt})")
must(n_sb == exp_belum("siswa"),
     f"angka Siswa Belum Scan pada judul bagian cocok dengan DB ({n_sb})")
must(n_gb == exp_belum("guru"),
     f"angka Guru Belum Scan pada judul bagian cocok dengan DB ({n_gb})")
must(b_st.count('<span class="no">') == n_st
     and b_sb.count('<span class="no">') == n_sb
     and b_gt.count('<span class="no">') == n_gt
     and b_gb.count('<span class="no">') == n_gb,
     "jumlah baris bernomor di tiap bagian sama dengan angka pada judulnya")
must(nomor_dari(b_sb, r'<span class="no">(\d+)\.</span>') == list(range(1, n_sb + 1)),
     "Siswa Belum Scan: nomor baris berurutan 1..N")

# --- Jumat 09:05: scan susulan siswa B kini DITOLAK (aturan berubah) ---
# Keputusan user sesudah Fase S dibuat: scan pukul 09:00 ke atas tidak
# berlaku lagi. Scan B pukul 09:05 ditolak dengan pesan penutupan, B
# TETAP di Siswa Belum Scan, dan tidak ada catatan Terlambat yang dibuat.
start_server(f"{TGL_S}T09:05:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-R02"})
must("Scan sudah ditutup" in hasil and "Siswa Lapor Belum" in hasil,
     "scan susulan Siswa Lapor Belum pukul 09:05 DITOLAK: "
     "'Scan sudah ditutup'")
b_r02_id = dbq("SELECT id FROM students WHERE kode='UJI-R02'")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal=? "
         "AND ref_type='siswa' AND ref_id=?", (TGL_S, b_r02_id)) == "Alpa/auto",
     "scan yang ditolak tidak membuat catatan Terlambat: B tetap Alpa/auto")
s, url, dash = req(admin, "/dashboard")
ks_t2, ks_b2 = kartu_laporan(dash, "/laporan/siswa")
must(ks_t2 == ks_t and ks_b2 == ks_b,
     "sesudah scan 09:05 yang ditolak: angka pada kartu Siswa di "
     "dashboard tidak berubah")
s, url, lap_s2 = req(admin, "/laporan/siswa")
n_st2, b_st2 = blok_laporan(lap_s2, "Siswa Terlambat")
n_sb2, b_sb2 = blok_laporan(lap_s2, "Siswa Belum Scan")
must("Siswa Lapor Belum" not in b_st2,
     "sesudah scan 09:05 yang ditolak: B TIDAK masuk Siswa Terlambat")
must("Siswa Lapor Belum" in b_sb2,
     "sesudah scan 09:05 yang ditolak: B tetap di Siswa Belum Scan")
must(n_st2 == n_st and n_sb2 == n_sb,
     "sesudah scan 09:05 yang ditolak: angka Siswa Terlambat & "
     "Siswa Belum Scan tidak berubah")

# ===========================================================================
# Fase T — Dua keputusan user soal pukul 09:00 (Senin 2026-11-23, tanggal
# segar):
# (1) RESET KARTU: mulai 09:00 ketujuh kartu statistik dashboard kembali
#     ke nilai AWAL hari (Siswa Hadir/Terlambat/Sakit-Izin/Alpa dan Guru
#     Hadir = 0; Siswa Belum Scan = total siswa; Guru Belum Scan = total
#     guru aktif). Ini HANYA tampilan — catatan tidak diubah, dan panel
#     Laporan Hari Ini di bawahnya tetap mengarah ke daftar sebenarnya
#     (dua kartu kolom -> halaman /laporan/siswa & /laporan/guru).
# (2) SCAN DITUTUP: scan pukul 09:00 ke atas untuk orang yang belum
#     punya catatan asli hari itu DITOLAK ("Scan sudah ditutup") —
#     berlaku di alur form biasa maupun ajax kamera — dan catatan Alpa
#     otomatisnya tetap berdiri. Yang sudah tercatat asli tetap mendapat
#     "Sudah tercatat". Pukul 08:59 scan masih diterima sebagai Terlambat.
# ===========================================================================
TGL_T = "2026-11-23"

start_server()  # waktu nyata: siapkan data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Reset"})
s, url, form_siswa = req(admin, "/admin/siswa")
kr = opt_val(form_siswa, "UJI-Kelas-Reset")
must(kr, "kelas UJI-Kelas-Reset dibuat untuk uji reset & tutup scan")
req(admin, "/admin/siswa", {"nama": "Siswa Reset Hadir", "kode": "UJI-T01", "class_id": kr})
req(admin, "/admin/siswa", {"nama": "Siswa Reset Telat", "kode": "UJI-T02", "class_id": kr})
req(admin, "/admin/siswa", {"nama": "Siswa Reset Belum", "kode": "UJI-T03", "class_id": kr})
req(admin, "/admin/siswa", {"nama": "Siswa Reset Delapan", "kode": "UJI-T04", "class_id": kr})
req(admin, "/admin/guru", {"nama": "Guru Reset Telat", "username": "gurureset1",
                           "password": "gurureset123", "mapel": "UJI-ResetMapel"})
req(admin, "/admin/guru", {"nama": "Guru Reset Belum", "username": "gurureset2",
                           "password": "gurureset123", "mapel": "UJI-ResetMapel"})
s, url, daftar_guru_t = req(admin, "/admin/guru")


def qr_guru_t(htmltxt, username):
    chunk = next((c for c in htmltxt.split("<li>") if username in c), "")
    m = re.search(r"kode QR: <code>(G\d+)</code>", chunk)
    return m.group(1) if m else None


gr1_qr = qr_guru_t(daftar_guru_t, "gurureset1")
gr2_qr = qr_guru_t(daftar_guru_t, "gurureset2")
must(gr1_qr and gr2_qr, "kode QR kedua guru Fase T terbaca")
t03_id = dbq("SELECT id FROM students WHERE kode='UJI-T03'")
gr2_id = dbq("SELECT id FROM users WHERE username='gurureset2'")
total_siswa_t = dbq("SELECT COUNT(*) FROM students")
total_guru_t = dbq("SELECT COUNT(*) FROM users WHERE role='guru' AND aktif=1")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=?", (TGL_T,)) == 0,
     "tanggal uji Fase T 2026-11-23 belum punya catatan kehadiran apa pun")

# --- Senin 06:55: siswa T01 scan Hadir ---
start_server(f"{TGL_T}T06:55:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-T01"})
must("Siswa Reset Hadir" in hasil and "Hadir" in hasil and "06:55" in hasil,
     "scan Siswa Reset Hadir pukul 06:55 -> Hadir")

# --- Senin 07:30: siswa T02 scan Terlambat ---
start_server(f"{TGL_T}T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-T02"})
must("Siswa Reset Telat" in hasil and "Terlambat" in hasil,
     "scan Siswa Reset Telat pukul 07:30 -> Terlambat")

# --- Senin 07:45: Guru Reset Telat scan Terlambat ---
start_server(f"{TGL_T}T07:45:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": gr1_qr})
must("Guru Reset Telat" in hasil and "Terlambat" in hasil,
     "scan Guru Reset Telat pukul 07:45 -> Terlambat")

# --- Senin 08:59: scan MASIH diterima; kartu masih menampilkan nilai riil ---
start_server(f"{TGL_T}T08:59:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-T04"})
must("Siswa Reset Delapan" in hasil and "Terlambat" in hasil,
     "scan pukul 08:59 masih diterima -> Terlambat (penutupan mulai 09:00)")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Hadir") == 3,
     "dashboard 08:59: Siswa Hadir riil = 3 (1 Hadir + 2 Terlambat)")
must(angka_kartu(dash, "Siswa Terlambat") == 2,
     "dashboard 08:59: Siswa Terlambat riil = 2")
must(angka_kartu(dash, "Siswa Alpa") == 0,
     "dashboard 08:59: Siswa Alpa riil = 0 (Alpa otomatis belum terisi)")
must(angka_kartu(dash, "Guru Hadir") == 1,
     "dashboard 08:59: Guru Hadir riil = 1")
exp_belum_s = total_siswa_t - dbq(
    "SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND ref_type='siswa' "
    "AND NOT (metode='auto' AND status='Alpa')", (TGL_T,))
exp_belum_g = total_guru_t - dbq(
    "SELECT COUNT(*) FROM kehadiran k JOIN users u ON u.id=k.ref_id "
    "WHERE k.tanggal=? AND k.ref_type='guru' AND u.role='guru' AND u.aktif=1 "
    "AND NOT (k.metode='auto' AND k.status='Alpa')", (TGL_T,))
must(angka_kartu(dash, "Siswa Belum Scan") == exp_belum_s,
     f"dashboard 08:59: Siswa Belum Scan riil = {exp_belum_s} "
     "(total - yang sudah tercatat, rumus Fase P)")
must(angka_kartu(dash, "Guru Belum Scan") == exp_belum_g,
     f"dashboard 08:59: Guru Belum Scan riil = {exp_belum_g}")

# --- Senin 09:00 TEPAT: scan siswa yang belum scan DITOLAK ---
start_server(f"{TGL_T}T09:00:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-T03"})
must("Scan sudah ditutup" in hasil and "Siswa Reset Belum" in hasil,
     "scan tepat 09:00 untuk siswa yang belum scan DITOLAK: "
     "'Scan sudah ditutup'")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal=? "
         "AND ref_type='siswa' AND ref_id=?", (TGL_T, t03_id)) == "Alpa/auto",
     "sesudah penolakan 09:00: Siswa Reset Belum tetap Alpa/auto")

# --- Senin 09:01: kartu RESET ke nilai awal; panel laporan tetap riil ---
start_server(f"{TGL_T}T09:01:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must(angka_kartu(dash, "Siswa Hadir") == 0,
     "dashboard 09:01: kartu Siswa Hadir di-reset ke 0")
must(angka_kartu(dash, "Siswa Terlambat") == 0,
     "dashboard 09:01: kartu Siswa Terlambat di-reset ke 0")
must(angka_kartu(dash, "Siswa Sakit/Izin") == 0,
     "dashboard 09:01: kartu Siswa Sakit/Izin di-reset ke 0")
must(angka_kartu(dash, "Siswa Alpa") == 0,
     "dashboard 09:01: kartu Siswa Alpa di-reset ke 0")
must(angka_kartu(dash, "Guru Hadir") == 0,
     "dashboard 09:01: kartu Guru Hadir di-reset ke 0")
must(angka_kartu(dash, "Siswa Belum Scan") == total_siswa_t,
     f"dashboard 09:01: Siswa Belum Scan di-reset ke total siswa "
     f"{total_siswa_t}")
must(angka_kartu(dash, "Guru Belum Scan") == total_guru_t,
     f"dashboard 09:01: Guru Belum Scan di-reset ke total guru aktif "
     f"{total_guru_t}")
must("Laporan Hari Ini" in dash,
     "dashboard 09:01: panel Laporan Hari Ini tampil di bawah kartu reset")
must('href="/laporan/siswa"' in dash and 'href="/laporan/guru"' in dash,
     "dashboard 09:01: panel laporan berupa dua kartu yang bisa diklik "
     "(kolom Siswa & kolom Guru)")
s, url, lap_s_t = req(admin, "/laporan/siswa")
s, url, lap_g_t = req(admin, "/laporan/guru")
n_st_t, b_st_t = blok_laporan(lap_s_t, "Siswa Terlambat")
n_gt_t, b_gt_t = blok_laporan(lap_g_t, "Guru Terlambat")
n_sb_t, b_sb_t = blok_laporan(lap_s_t, "Siswa Belum Scan")
must("Siswa Reset Telat" in b_st_t and "Siswa Reset Delapan" in b_st_t,
     "laporan 09:01 tetap riil: kedua siswa Terlambat tampil di "
     "halaman /laporan/siswa (data TIDAK ikut di-reset)")
must("Guru Reset Telat" in b_gt_t,
     "laporan 09:01 tetap riil: Guru Reset Telat tampil di "
     "halaman /laporan/guru")
must("Siswa Reset Belum" in b_sb_t,
     "laporan 09:01: Siswa Reset Belum tampil di Siswa Belum Scan "
     "halaman /laporan/siswa")

# --- Senin 09:30: penolakan guru (form + ajax) & duplikat riil tetap info ---
start_server(f"{TGL_T}T09:30:00")
admin, url, _ = login("admin", "admin123")
s, url, resp = req(admin, "/api/scan", {"kode": "UJI-T03", "ajax": "1"})
data_tutup = json.loads(resp)
must(data_tutup["jenis"] == "error"
     and "Scan sudah ditutup" in data_tutup["judul"]
     and "Siswa Reset Belum" in data_tutup["pesan"],
     "ajax 09:30: scan siswa belum scan -> JSON error 'Scan sudah ditutup' "
     "(banner kamera berkelanjutan menerima pesan penutupan)")
s, url, hasil = req(admin, "/api/scan", {"kode": gr2_qr})
must("Scan sudah ditutup" in hasil and "Guru Reset Belum" in hasil,
     "scan guru yang belum scan pukul 09:30 DITOLAK: 'Scan sudah ditutup'")
must(dbq("SELECT status || '/' || metode FROM kehadiran WHERE tanggal=? "
         "AND ref_type='guru' AND ref_id=?", (TGL_T, gr2_id)) == "Alpa/auto",
     "sesudah penolakan 09:30: Guru Reset Belum tetap Alpa/auto")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-T02"})
must("Sudah tercatat" in hasil,
     "scan ulang siswa yang sudah tercatat asli pukul 09:30 tetap "
     "'Sudah tercatat' (bukan penolakan tutup)")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal=? AND ref_type='siswa' "
         "AND ref_id=?", (TGL_T, t03_id)) == 1,
     "Siswa Reset Belum tetap punya tepat 1 catatan (Alpa otomatis) — "
     "penolakan tidak membuat/mengubah catatan apa pun")

# ===========================================================================
# Fase V — Keputusan user: panel bawah dashboard HILANG mulai pukul 17:00.
# Pemilihan panel kini tiga arah: < 09:00 daftar Kedatangan Terakhir;
# 09:00–<17:00 panel Laporan Hari Ini (dua kartu bisa diklik); >= 17:00
# TIDAK ada panel apa pun di slot itu (tanpa judul, tanpa kartu kosong,
# tanpa tautan /laporan/). Kartu statistik TIDAK ikut berubah (tetap
# nilai reset sesudah 09:00), dan halaman /laporan/* tetap ada.
# Memakai ulang tanggal & data Fase T (Senin 2026-11-23).
# ===========================================================================

# --- Senin 06:30: panel kedatangan tampil, panel laporan belum ada ---
start_server(f"{TGL_T}T06:30:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Kedatangan Terakhir Hari Ini" in dash,
     "dashboard 06:30: panel Kedatangan Terakhir Hari Ini tampil")
must("Laporan Hari Ini" not in dash and 'href="/laporan/' not in dash,
     "dashboard 06:30: panel Laporan Hari Ini belum tampil")

# --- Senin 16:59: panel Laporan Hari Ini masih tampil penuh ---
start_server(f"{TGL_T}T16:59:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Laporan Hari Ini" in dash,
     "dashboard 16:59: panel Laporan Hari Ini masih tampil")
must('href="/laporan/siswa"' in dash and 'href="/laporan/guru"' in dash,
     "dashboard 16:59: kedua kartu laporan (Siswa & Guru) masih tertaut")

# --- Senin 17:00 & 17:30: panel bawah HILANG; kartu tetap nilai reset ---
for jam_v in ("17:00", "17:30"):
    start_server(f"{TGL_T}T{jam_v}:00")
    admin, url, _ = login("admin", "admin123")
    s, url, dash = req(admin, "/dashboard")
    must("Laporan Hari Ini" not in dash,
         f"dashboard {jam_v}: panel Laporan Hari Ini sudah hilang")
    must("Kedatangan Terakhir Hari Ini" not in dash,
         f"dashboard {jam_v}: panel Kedatangan Terakhir juga tidak tampil")
    must('href="/laporan/' not in dash,
         f"dashboard {jam_v}: tidak ada tautan /laporan/ di dashboard")
    must(angka_kartu(dash, "Siswa Belum Scan") == total_siswa_t,
         f"dashboard {jam_v}: kartu Siswa Belum Scan tetap nilai reset "
         f"(= total siswa {total_siswa_t})")
    must(angka_kartu(dash, "Guru Belum Scan") == total_guru_t,
         f"dashboard {jam_v}: kartu Guru Belum Scan tetap nilai reset "
         f"(= total guru aktif {total_guru_t})")
    must(angka_kartu(dash, "Siswa Hadir") == 0,
         f"dashboard {jam_v}: kartu Siswa Hadir tetap 0 (reset)")

# Halaman laporannya sendiri tetap bisa dibuka sesudah 17:00.
s, url, lap_v = req(admin, "/laporan/siswa")
must("Laporan Siswa Hari Ini" in lap_v,
     "halaman /laporan/siswa tetap bisa dibuka sesudah 17:00 "
     "(hanya panel dashboard yang disembunyikan)")

# ===========================================================================
# Fase U — Halaman laporan bisa dibuka SEMUA pengguna yang login (bukan
# hanya admin): anonim diarahkan ke /login, dan guru yang login bisa
# membuka /laporan/siswa & /laporan/guru dan melihat daftar yang sama.
# Memakai ulang tanggal & data Fase S (Jumat 2026-11-20) yang masih ada
# di DB uji, pada pukul 09:10 (sesudah panel laporan aktif).
# ===========================================================================
start_server(f"{TGL_S}T09:10:00")
anon = client()
s, url, hal = req(anon, "/laporan/siswa")
must(urllib.parse.urlparse(url).path == "/login",
     "anonim membuka /laporan/siswa -> diarahkan ke /login")
s, url, hal = req(anon, "/laporan/guru")
must(urllib.parse.urlparse(url).path == "/login",
     "anonim membuka /laporan/guru -> diarahkan ke /login")
guru_u, url, _ = login("gurulapor1", "gurulapor123")
must(urllib.parse.urlparse(url).path == "/dashboard",
     "guru Fase S (gurulapor1) bisa login untuk Fase U")
s, url, lap_s_u = req(guru_u, "/laporan/siswa")
must("Laporan Siswa Hari Ini" in lap_s_u,
     "guru bisa membuka /laporan/siswa")
n_st_u, b_st_u = blok_laporan(lap_s_u, "Siswa Terlambat")
must("Siswa Lapor Telat" in b_st_u and "07:30" in b_st_u,
     "guru melihat daftar yang sama: Siswa Lapor Telat ada di "
     "Siswa Terlambat dengan jam 07:30")
s, url, lap_g_u = req(guru_u, "/laporan/guru")
must("Laporan Guru Hari Ini" in lap_g_u,
     "guru bisa membuka /laporan/guru")
n_gb_u, b_gb_u = blok_laporan(lap_g_u, "Guru Belum Scan")
must("Guru Lapor Belum" in b_gb_u,
     "guru melihat daftar yang sama: Guru Lapor Belum ada di "
     "Guru Belum Scan")

# ===========================================================================
# Fase W — Warna Menu: panel navigasi (sidebar desktop, bilah atas &
# menu bawah mobile) punya pengaturan warna SENDIRI di Pengaturan
# Tampilan (kunci settings 'warna_menu'), terpisah dari warna latar
# halaman (bg_color). Belum dipilih -> tidak ada penimpa apa pun
# (tampilan bawaan menu putih). Nilai tidak sah ditolak tanpa mengubah
# pengaturan; guru tidak boleh mengubah; teks menu dihitung dari
# luminansi agar tetap terbaca; warna ikut tersimpan di backup (tabel
# settings tercakup salinan DB utuh — dibuktikan dari isi ZIP).
# ===========================================================================
start_server("2026-11-16T08:00:00")
admin, url, _ = login("admin", "admin123")
guru, url, _ = login("guruuji", "guruuji123")


def setting_w(key):
    conn = sqlite3.connect(DBUJI)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


must(setting_w("warna_menu") is None,
     "warna menu belum pernah diatur: kunci warna_menu belum ada di settings")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu" not in dash and "menu-kustom" not in dash,
     "tanpa warna menu: halaman tidak memuat penimpa warna menu apa pun "
     "(tampilan bawaan dipertahankan)")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must("Warna Menu" in hal_tampilan
     and 'action="/admin/tampilan/warna-menu"' in hal_tampilan,
     "Pengaturan Tampilan memuat bagian Warna Menu dengan form-nya sendiri")

s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "#334155"})
must("Warna menu disimpan." in hasil, "simpan warna menu preset berhasil")
must(setting_w("warna_menu") == "#334155",
     "warna menu #334155 tersimpan di settings")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu:#334155" in dash and 'class="menu-kustom"' in dash,
     "dashboard memuat variabel --warna-menu dan kelas menu-kustom "
     "(sidebar memakai warna pilihan)")
must("--teks-menu:#ffffff" in dash,
     "warna menu gelap -> teks menu dihitung putih agar terbaca")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must('name="warna_menu" value="#334155" checked' in hal_tampilan,
     "halaman tampilan menandai swatch warna menu yang sedang aktif")

s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "bukan-warna"})
must("Warna menu tidak valid" in hasil,
     "nilai warna menu tidak sah ditolak dengan pesan jelas")
must(setting_w("warna_menu") == "#334155",
     "nilai tidak sah tidak mengubah warna menu tersimpan")
s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "kustom", "warna_menu_custom": "#12345"})
must("Warna menu tidak valid" in hasil
     and setting_w("warna_menu") == "#334155",
     "warna kustom tidak sah (5 digit) juga ditolak, pengaturan utuh")

try:
    req(guru, "/admin/tampilan/warna-menu", {"warna_menu": "#123abc"})
    must(False, "guru mengubah warna menu harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru POST warna menu -> 404")
must(setting_w("warna_menu") == "#334155",
     "percobaan guru tidak mengubah warna menu")

s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "kustom", "warna_menu_custom": "#FEF3C7"})
must("Warna menu disimpan." in hasil
     and setting_w("warna_menu") == "#fef3c7",
     "warna menu kustom dari color picker tersimpan (dinormalkan huruf kecil)")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu:#fef3c7" in dash and "--teks-menu:#1c2333" in dash,
     "warna menu terang -> teks menu dihitung gelap agar terbaca")

s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "#ffffff"})
must("Warna menu disimpan." in hasil
     and setting_w("warna_menu") == "#ffffff",
     "memilih Putih #ffffff tersimpan bersih sebagai warna menu")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu:#ffffff" in dash and "--teks-menu:#1c2333" in dash,
     "warna menu putih terpasang dengan teks gelap")

# Kemandirian dua arah: warna latar dan warna menu tidak saling mengubah.
s, url, hasil = req(admin, "/admin/tampilan",
                    {"nama_sekolah": setting_w("nama_sekolah") or "",
                     "bg_color": "#e0f2fe", "bg_color_custom": "#e0f2fe"})
must("Pengaturan tampilan disimpan." in hasil, "ganti warna latar berhasil")
s, url, dash = req(admin, "/dashboard")
must("background-color:#e0f2fe" in dash and "--warna-menu:#ffffff" in dash,
     "mengubah warna latar tidak mengubah warna menu")
s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "#334155"})
must("Warna menu disimpan." in hasil, "ganti warna menu lagi berhasil")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu:#334155" in dash and "background-color:#e0f2fe" in dash,
     "mengubah warna menu tidak mengubah warna latar")

# Warna menu ikut di backup: ZIP berisi salinan DB utuh termasuk settings.
st, hdr, zipdata = req_bytes(admin, "/admin/backup/unduh")
zf_w = zipfile.ZipFile(io.BytesIO(zipdata))
DBW = "/tmp/absen-uji-w.db"
with open(DBW, "wb") as fh:
    fh.write(zf_w.read("absen.db"))
must(dbqf(DBW, "SELECT value FROM settings WHERE key='warna_menu'") == "#334155",
     "backup (ZIP) memuat warna menu di tabel settings")
os.remove(DBW)

# ===========================================================================
# Fase X — Pemilih warna kustom Pengaturan Tampilan (warna latar & warna
# menu): swatch ".kustom" berisi radio tersembunyi "kustom" + color picker.
# Mengetuk picker TIDAK mencentang radio itu (browser tidak meneruskan klik
# elemen interaktif di dalam <label> ke radio), jadi warna pilihan picker
# tidak pernah diterapkan — dilaporkan user dari iPhone. Perbaikan di
# app.js: picker pada "input"/"change" mencentang radio saudara, dan pada
# "change" formulir langsung dikirim. Suite HTTP tidak menjalankan JS ->
# cakupan statis pada isi app.js yang disajikan + teks petunjuk halaman
# tampilan + regresi server bahwa posting kustom tetap menyimpan warna.
# ===========================================================================
s, headers, js_bytes = req_bytes(admin, "/static/app.js")
js_x = js_bytes.decode("utf-8", "replace")
must('.swatch.kustom input[type="color"]' in js_x,
     "app.js memuat penanganan pemilih warna .swatch.kustom")
must("addEventListener('input'" in js_x
     and "addEventListener('change'" in js_x,
     "app.js mendengarkan peristiwa input & change pada pemilih warna")
must("radio.checked = true" in js_x,
     "app.js mencentang radio 'kustom' saat warna dipilih dari picker")
must("requestSubmit" in js_x
     and "HTMLFormElement.prototype.submit.call" in js_x,
     "app.js mengirim formulir pada change (requestSubmit, cadangan "
     "submit prototipe — string 'form.submit()' tetap dilarang oleh Fase J)")
must("pw-toggle" in js_x and "catatKode" in js_x,
     "app.js tetap memuat toggle mata password & logika scan gerbang")

s, url, hal_tampilan = req(admin, "/admin/tampilan")
must("pilih radio terakhir" not in hal_tampilan,
     "petunjuk halaman tampilan tidak lagi menyuruh memilih radio manual")
must("langsung diterapkan" in hal_tampilan
     and "begitu dipilih" in hal_tampilan,
     "petunjuk halaman tampilan: warna kustom langsung diterapkan "
     "begitu dipilih")

# Regresi server: posting kustom untuk KEDUA pemilih tetap menyimpan warna.
s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "kustom", "warna_menu_custom": "#123abc"})
must("Warna menu disimpan." in hasil
     and setting_w("warna_menu") == "#123abc",
     "posting warna_menu=kustom dengan #123abc tetap tersimpan")
s, url, hasil = req(admin, "/admin/tampilan",
                    {"nama_sekolah": setting_w("nama_sekolah") or "",
                     "bg_color": "kustom", "bg_color_custom": "#123abc"})
must("Pengaturan tampilan disimpan." in hasil
     and setting_w("bg_color") == "#123abc",
     "posting bg_color=kustom dengan #123abc tetap tersimpan")

# ===========================================================================
# Fase Y — Kejelasan Wallpaper: lapisan putih di atas wallpaper (CSS
# .bg-wallpaper::after, alpha bawaan .86) bisa diatur kekuatannya dari
# Pengaturan Tampilan (kunci settings 'kejelasan_wp'): samar .86 =
# tampilan seperti sekarang & bawaan, sedang .65, jelas .45. Belum
# diatur -> halaman ber-wallpaper dirender TANPA penimpa --wp-alpha.
# Nilai tidak sah ditolak tanpa mengubah pengaturan; guru tidak boleh
# mengubah; bisa disimpan walau belum ada wallpaper (berlaku begitu
# wallpaper diunggah).
# ===========================================================================
must(setting_w("kejelasan_wp") is None,
     "kejelasan wallpaper belum pernah diatur: kunci kejelasan_wp "
     "belum ada di settings")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must('action="/admin/tampilan/kejelasan-wp"' in hal_tampilan,
     "Pengaturan Tampilan memuat form Kejelasan Wallpaper")
must('name="kejelasan_wp" value="samar" checked' in hal_tampilan
     and 'name="kejelasan_wp" value="sedang"' in hal_tampilan
     and 'name="kejelasan_wp" value="jelas"' in hal_tampilan,
     "tiga pilihan kejelasan tampil; tingkat yang berlaku ('samar') "
     "tercentang")

# Bisa disimpan SEBELUM ada wallpaper apa pun.
s, url, hasil = req(admin, "/admin/tampilan/kejelasan-wp",
                    {"kejelasan_wp": "sedang"})
must("Kejelasan wallpaper disimpan." in hasil,
     "simpan kejelasan 'sedang' berhasil (belum ada wallpaper)")
must(setting_w("kejelasan_wp") == "sedang",
     "kejelasan 'sedang' tersimpan di settings")

# Pasang wallpaper -> lapisan memakai alpha tingkat tersimpan (.65).
s, url, hasil = unggah(admin, "/admin/tampilan/wallpaper", "wallpaper",
                       "wallpaper.png", PNG_1PX)
must("Wallpaper dipasang." in hasil, "unggah wallpaper untuk Fase Y berhasil")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
m_wp_y = re.search(r"/wallpaper/([0-9a-f]{24}\.png)", hal_tampilan)
must(m_wp_y is not None, "halaman tampilan menampilkan wallpaper Fase Y")
nama_wp_y = m_wp_y.group(1)
s, url, login_page = req(anon, "/login")
must(f"/wallpaper/{nama_wp_y}" in login_page
     and "--wp-alpha:0.65" in login_page,
     "halaman login: wallpaper tampil dengan --wp-alpha:0.65 (sedang)")

s, url, hasil = req(admin, "/admin/tampilan/kejelasan-wp",
                    {"kejelasan_wp": "jelas"})
must("Kejelasan wallpaper disimpan." in hasil
     and setting_w("kejelasan_wp") == "jelas",
     "simpan kejelasan 'jelas' berhasil")
s, url, login_page = req(anon, "/login")
must("--wp-alpha:0.45" in login_page,
     "halaman login memakai --wp-alpha:0.45 (jelas)")

s, url, hasil = req(admin, "/admin/tampilan/kejelasan-wp",
                    {"kejelasan_wp": "samar"})
must("Kejelasan wallpaper disimpan." in hasil
     and setting_w("kejelasan_wp") == "samar",
     "simpan kejelasan 'samar' berhasil")
s, url, login_page = req(anon, "/login")
must("--wp-alpha:0.86" in login_page,
     "halaman login memakai --wp-alpha:0.86 (samar, eksplisit)")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must('name="kejelasan_wp" value="samar" checked' in hal_tampilan,
     "halaman tampilan menandai tingkat 'samar' yang sedang aktif")

s, url, hasil = req(admin, "/admin/tampilan/kejelasan-wp",
                    {"kejelasan_wp": "acak"})
must("Kejelasan wallpaper tidak valid" in hasil,
     "nilai kejelasan tidak sah ditolak dengan pesan jelas")
must(setting_w("kejelasan_wp") == "samar",
     "nilai tidak sah tidak mengubah kejelasan tersimpan")
s, url, login_page = req(anon, "/login")
must("--wp-alpha:0.86" in login_page,
     "sesudah penolakan, alpha tetap .86")

try:
    req(guru, "/admin/tampilan/kejelasan-wp", {"kejelasan_wp": "jelas"})
    must(False, "guru mengubah kejelasan wallpaper harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru POST kejelasan wallpaper -> 404")
must(setting_w("kejelasan_wp") == "samar",
     "percobaan guru tidak mengubah kejelasan wallpaper")

# Kembali ke keadaan belum diatur: halaman ber-wallpaper TANPA penimpa.
conn = sqlite3.connect(DBUJI)
conn.execute("DELETE FROM settings WHERE key='kejelasan_wp'")
conn.commit(); conn.close()
s, url, login_page = req(anon, "/login")
must(f"/wallpaper/{nama_wp_y}" in login_page
     and "--wp-alpha" not in login_page,
     "belum diatur: wallpaper tampil tanpa penimpa --wp-alpha "
     "(CSS memakai alpha bawaan .86)")
s, url, hasil = req(admin, "/admin/tampilan/wallpaper/hapus", {})
must("Wallpaper dihapus." in hasil, "wallpaper Fase Y dibersihkan lagi")

# ===========================================================================
# Fase Z — Tema pastel (Pengaturan Tampilan -> Tema; kunci settings
# 'tema'): kosong = tampilan bawaan profesional (tidak berubah); 5 slug
# sah: pastel-pink, mint, langit, lilac, krem. Tema aktif memberi kelas
# body .tema-<slug> dan MENANG atas warna menu (penimpa --warna-menu
# tidak dimuat) & warna latar; wallpaper tetap tampil bila dipasang.
# Nilai tidak sah ditolak tanpa mengubah pengaturan; guru tidak boleh
# mengubah. Ikon sekolah (ikon_tema) tampil di dashboard, dan kartu
# "Kehadiran per Kelas Hari Ini" menghitung Hadir+Terlambat ASLI per
# kelas dari catatan hari itu. Tema ikut tersimpan di backup (tabel
# settings tercakup salinan DB utuh — dibuktikan dari isi ZIP).
# ===========================================================================
def setting_z(key):
    conn = sqlite3.connect(DBUJI)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


must(setting_z("tema") is None,
     "tema belum pernah diatur: kunci tema belum ada di settings")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must('action="/admin/tampilan/tema"' in hal_tampilan,
     "Pengaturan Tampilan memuat kartu Tema dengan form-nya sendiri")
for nama_tema in ("Pastel Pink Oranye", "Mint Toska", "Biru Langit Peach",
                  "Lilac Pink", "Krem Coral"):
    must(nama_tema in hal_tampilan, f"pilihan tema tampil: {nama_tema}")
must("Selama tema aktif, warna latar dan warna menu mengikuti tema."
     in hal_tampilan,
     "petunjuk tema tampil: selama tema aktif latar & menu mengikuti tema")
must("Wallpaper tetap tampil bila dipasang." in hal_tampilan,
     "petunjuk tema tampil: wallpaper tetap tampil bila dipasang")
must('name="tema" value="" checked' in hal_tampilan,
     "keadaan awal: pilihan Default tema yang tercentang")

# Tanpa tema: penimpa warna menu tetap dimuat seperti biasa (Fase W/X).
s, url, hasil = req(admin, "/admin/tampilan/warna-menu",
                    {"warna_menu": "#334155"})
must("Warna menu disimpan." in hasil, "warna menu dipasang untuk uji tema")
s, url, dash = req(admin, "/dashboard")
must("--warna-menu:#334155" in dash,
     "tanpa tema: penimpa --warna-menu dimuat (perilaku lama utuh)")

# Simpan tiap tema -> tersimpan + kelas body berubah + penimpa menu hilang.
for slug in ("pastel-pink", "mint", "langit", "lilac", "krem"):
    s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": slug})
    must("Tema disimpan." in hasil, f"simpan tema {slug} berhasil")
    must(setting_z("tema") == slug, f"tema {slug} tersimpan di settings")
    s, url, dash = req(admin, "/dashboard")
    must(f"tema-{slug}" in dash, f"dashboard memakai kelas body tema-{slug}")
    must("--warna-menu" not in dash,
         f"tema {slug} aktif: penimpa --warna-menu TIDAK dimuat (tema menang)")
s, url, hal_tampilan = req(admin, "/admin/tampilan")
must('name="tema" value="krem" checked' in hal_tampilan,
     "halaman tampilan menandai tema krem yang sedang aktif")

# Nilai tidak sah ditolak, tema lama utuh.
s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": "acak"})
must("Tema tidak valid" in hasil, "tema tidak sah ditolak dengan pesan jelas")
must(setting_z("tema") == "krem",
     "nilai tidak sah tidak mengubah tema tersimpan")

# Guru tidak boleh mengubah tema.
try:
    req(guru, "/admin/tampilan/tema", {"tema": "mint"})
    must(False, "guru mengubah tema harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru POST tema -> 404")
must(setting_z("tema") == "krem", "percobaan guru tidak mengubah tema")

# Wallpaper tetap tampil walau tema aktif.
s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": "pastel-pink"})
must("Tema disimpan." in hasil, "tema pastel-pink dipasang untuk uji wallpaper")
s, url, hasil = unggah(admin, "/admin/tampilan/wallpaper", "wallpaper",
                       "wp-tema.png", PNG_1PX)
must("Wallpaper dipasang." in hasil, "unggah wallpaper saat tema aktif berhasil")
s, url, dash = req(admin, "/dashboard")
must("/wallpaper/" in dash and "tema-pastel-pink" in dash,
     "tema aktif + wallpaper terpasang: wallpaper tetap tampil di dashboard")
s, url, hasil = req(admin, "/admin/tampilan/wallpaper/hapus", {})
must("Wallpaper dihapus." in hasil, "wallpaper uji tema dibersihkan lagi")

# Kembali ke Default: kelas tema hilang, penimpa warna menu tampil lagi.
s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": ""})
must("Tema disimpan." in hasil, "simpan tema Default (kosong) berhasil")
must(setting_z("tema") in ("", None), "tema Default tersimpan kosong")
s, url, dash = req(admin, "/dashboard")
must(all(f"tema-{s2}" not in dash for s2 in
         ("pastel-pink", "mint", "langit", "lilac", "krem")),
     "Default: dashboard tanpa kelas tema apa pun")
must("--warna-menu:#334155" in dash,
     "Default: penimpa --warna-menu dimuat lagi seperti semula")

# --- Kartu Kehadiran per Kelas + ikon tema: pagi sekolah yang disimulasi ---
start_server("2026-11-25T06:45:00")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("ikon-tema-cluster" in dash,
     "dashboard memuat klaster ikon sekolah (siswa, pensil, buku, tas, "
     "penggaris, jam)")
must("16.7 4.5a2.12" in dash,
     "SVG ikon pensil dari ikon_tema tampil di dashboard")
must("Kehadiran per Kelas Hari Ini" in dash,
     "dashboard memuat kartu Kehadiran per Kelas Hari Ini")
total_k1 = dbq("SELECT COUNT(*) FROM students s JOIN classes c "
               "ON c.id=s.class_id WHERE c.nama='UJI-Kelas-1'")
must(total_k1 == 4, f"kelas UJI-Kelas-1 berisi 4 siswa (terbaca {total_k1})")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-001"})
must("Siswa Uji A" in hasil and "Hadir" in hasil,
     "scan UJI-001 pukul 06:45 tercatat Hadir")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-002"})
must("Siswa Uji B" in hasil and "Hadir" in hasil,
     "scan UJI-002 pukul 06:45 tercatat Hadir")
s, url, dash = req(admin, "/dashboard")
seksi_kelas = dash.split("Kehadiran per Kelas Hari Ini", 1)[1]
chunk_k1 = next((c for c in seksi_kelas.split("kelas-row")
                 if "UJI-Kelas-1" in c), "")
must(f"2/{total_k1}" in chunk_k1,
     f"kartu per kelas: UJI-Kelas-1 tampil 2/{total_k1} sesudah 2 scan")
must(f"width:{round(2 * 100 / total_k1)}%" in chunk_k1,
     "kartu per kelas: batang UJI-Kelas-1 terisi sesuai persentase masuk")
chunk_hampa = next((c for c in seksi_kelas.split("kelas-row")
                    if "UJI-Kelas-Hampa" in c), "")
must("&mdash;" in chunk_hampa and "width:0%" in chunk_hampa,
     "kartu per kelas: kelas kosong tampil '—' dengan batang 0%")

# --- Tema ikut tersimpan di backup (tabel settings dalam ZIP) ---
s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": "pastel-pink"})
must("Tema disimpan." in hasil and setting_z("tema") == "pastel-pink",
     "tema pastel-pink dipasang sebelum backup Fase Z")
st, hdr, zipdata = req_bytes(admin, "/admin/backup/unduh")
zf_z = zipfile.ZipFile(io.BytesIO(zipdata))
DBZ = "/tmp/absen-uji-z.db"
with open(DBZ, "wb") as fh:
    fh.write(zf_z.read("absen.db"))
must(dbqf(DBZ, "SELECT value FROM settings WHERE key='tema'") == "pastel-pink",
     "backup (ZIP) memuat tema di tabel settings")
os.remove(DBZ)
s, url, hasil = req(admin, "/admin/tampilan/tema", {"tema": ""})
must("Tema disimpan." in hasil and setting_z("tema") in ("", None),
     "tema dikembalikan ke Default di akhir Fase Z")

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
