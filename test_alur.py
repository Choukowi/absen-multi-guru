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


def start_server(override=None, dbfile=None, extra_env=None):
    global _proc
    stop_server()
    env = dict(os.environ)
    env["ABSEN_DB"] = dbfile or DBUJI
    env["PORT"] = str(PORT)
    if override:
        env["ABSEN_NOW_OVERRIDE"] = override
    else:
        env.pop("ABSEN_NOW_OVERRIDE", None)
    # Env tambahan per skenario (mis. ABSEN_LISENSI pada Fase AA):
    # kunci yang dikenal selalu dibersihkan dulu agar tidak bocor
    # antar skenario, lalu extra_env diterapkan.
    env.pop("ABSEN_LISENSI", None)
    if extra_env:
        env.update(extra_env)
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
    for chunk in htmltxt.split("<tr>"):
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
chunk_mig = next((c for c in daftar_guru_m.split("<tr>") if "<b>Guru Migrasi</b>" in c), "")
must("Guru Migrasi" in daftar_guru_m, "MIGRASI: guru lama tetap terdaftar")
must(re.search(r"<code>G\d+</code>", chunk_mig) is not None,
     "MIGRASI: guru lama mendapat kode_qr backfill otomatis")
must("<td>-</td>" in chunk_mig,
     "MIGRASI: mapel guru lama kosong (default '') tampil '-' di kolom Mapel")
must("<td>&mdash;</td>" in chunk_mig,
     "MIGRASI: guru lama belum wali -> kolom Username tampil '—' (belum punya login)")
s, url, daftar_siswa_m = req(admin_m, "/admin/siswa")
must(all(n in daftar_siswa_m for n in
         ["Siswa Migrasi A", "Siswa Migrasi B", "Siswa Migrasi C",
          "MIG-Kelas-1", "MIG-Kelas-2"]),
     "MIGRASI: ketiga siswa lama + kedua kelas tetap tampil")
s, url, daftar_kelas_m = req(admin_m, "/admin/kelas")
must("MIG-Kelas-1" in daftar_kelas_m and "MIG-Kelas-2" in daftar_kelas_m,
     "MIGRASI: daftar kelas lama utuh")
must("Wali Kelas" in daftar_kelas_m and "Belum ada" in daftar_kelas_m,
     "MIGRASI: kolom Wali Kelas tampil; kelas lama mulai tanpa wali")
# Model baru: guru lama BUKAN wali -> login lamanya ditolak (generik),
# dan hash password tersimpannya TIDAK diubah oleh kode baru.
guru_m, url, body_m = login("gurumigrasi", "gurumigrasi123")
must("dashboard" not in url and "Username atau password salah." in body_m,
     "MIGRASI: guru lama bukan wali -> login ditolak generik (model wali kelas)")
must(dbqf(DBMIG, "SELECT password_hash FROM users WHERE username='gurumigrasi'")
     == _hash_lama("gurumigrasi123", SALT),
     "MIGRASI: hash password guru lama TIDAK diubah kode baru")
# Admin menunjuknya jadi wali MIG-Kelas-1 -> kredensial baru terbit.
mk1 = id_dari_daftar(daftar_kelas_m, "/admin/kelas", "MIG-Kelas-1")
must(mk1, "MIGRASI: id MIG-Kelas-1 terbaca dari daftar kelas")
s, url, hasil_m = req(admin_m, f"/admin/kelas/{mk1}/wali",
                      {"wali_id": "2"})
m_wm = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil_m)
must(m_wm is not None and m_wm.group(1) == "walimigkelas1",
     "MIGRASI: penunjukan wali menerbitkan username walimigkelas1 + password acak")
guru_m, url, _ = login(m_wm.group(1), m_wm.group(2))
must("dashboard" in url,
     "MIGRASI: sesudah ditunjuk wali, guru lama bisa login dengan kredensial baru")
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
# guru: tambah sebagai DATA INDUK (nama + mapel saja; kode QR otomatis).
# Username/password TIDAK lagi dibuat di sini — login lahir hanya dari
# penunjukan wali kelas (dibuktikan di Fase B & Fase AD).
req(admin, "/admin/guru", {"nama": "Guru Uji", "mapel": "UJI-Matematika"})
req(admin, "/admin/guru", {"nama": "Guru Uji Dua", "mapel": "UJI-Biologi"})
s, url, daftar_guru = req(admin, "/admin/guru")
must("UJI-Matematika" in daftar_guru, "daftar guru menampilkan mata pelajaran")
must('name="username"' not in daftar_guru and 'name="password"' not in daftar_guru,
     "form Tambah Guru: TANPA kolom username/password (guru = data induk)")
g1_id = id_dari_daftar(daftar_guru, "/admin/guru", "<b>Guru Uji</b>")
g2_id = id_dari_daftar(daftar_guru, "/admin/guru", "<b>Guru Uji Dua</b>")
chunk_g1 = next((c for c in daftar_guru.split("<tr>") if "<b>Guru Uji</b>" in c), "")
m_qr = re.search(r"<code>(G\d+)</code>", chunk_g1)
must(m_qr is not None, "kode QR guru dibuat otomatis (format G + 3 digit)")
g1_qr = m_qr.group(1)
must(g1_id and g2_id, f"id guru terbaca: {g1_id}, {g2_id}")
must("<td>&mdash;</td>" in chunk_g1,
     "guru baru belum wali: kolom Username tampil '—' (belum punya login)")
req(admin, f"/admin/guru/{g2_id}/edit",
    {"nama": "Guru Uji Dua Revisi", "mapel": "UJI-Kimia"})
s, url, daftar_guru = req(admin, "/admin/guru")
must("Guru Uji Dua Revisi" in daftar_guru and "UJI-Kimia" in daftar_guru,
     "edit guru (nama + mapel) tampil di daftar")
s, url, form_edit_g2 = req(admin, f"/admin/guru/{g2_id}/edit")
must('name="username"' not in form_edit_g2 and 'name="password"' not in form_edit_g2,
     "form Edit Guru: TANPA kolom username/password (diganti penunjukan wali)")
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
# --- model login hanya wali kelas: Guru Uji ditunjuk admin sebagai
# wali UJI-Kelas-1 lewat dropdown pada kolom Wali Kelas di Kelola Kelas
# (memilih dari daftar guru yang ada). Sistem menerbitkan username
# waliujikelas1 + password ACAK yang tampil SATU kali di pesan flash;
# kredensial itulah yang dipakai fase-fase berikutnya setiap kali
# "guru" login.
s, url, hasil = req(admin, f"/admin/kelas/{k1}/wali",
                    {"wali_id": str(g1_id)})
m_wali = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_wali is not None,
     "penunjukan wali: flash memuat username wali + password sementara acak")
WALI_G1_USER, WALI_G1_PW = m_wali.group(1), m_wali.group(2)
must(WALI_G1_USER == "waliujikelas1",
     f"username wali dibuat otomatis dari slug kelas: {WALI_G1_USER}")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (k1,)) == g1_id,
     "DB: classes.wali_id UJI-Kelas-1 menunjuk Guru Uji")
# guru yang BUKAN wali tidak bisa login (kredensial lama pun tidak ada)
g_tolak, url, body = login("guruuji", "guruuji123")
must("dashboard" not in url and "Username atau password salah." in body,
     "guru bukan wali: login ditolak dengan pesan generik")
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
must("dashboard" in url, "login wali kelas berhasil (username wali + password acak)")
# wali TIDAK bisa membuka Scan Gerbang (bukan bagian dari aksesnya)
s, url, hal_scan = req(guru, "/scan-gerbang")
must(urllib.parse.urlparse(url).path == "/dashboard"
     and "tidak tersedia untuk wali kelas" in hal_scan,
     "wali membuka Scan Gerbang -> diarahkan ke dashboard + pesan sopan")
# tabel siswa wali TERKUNCI ke kelasnya: parameter kelas lain diabaikan
s, url, tabel_g = req(guru, f"/tabel-siswa?kelas={k2}&minggu=2026-11-16")
must("Siswa Uji A" in tabel_g and "Siswa Uji E" not in tabel_g,
     "wali dipaksa ke tabel kelasnya sendiri (parameter kelas lain diabaikan)")
must('name="kelas"' not in tabel_g,
     "tabel siswa wali: pemilih kelas TIDAK dirender (langsung kelasnya)")

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

from PIL import Image as PILImage

# ===========================================================================
# Fase G — Pengaturan Tampilan: warna, nama sekolah, wallpaper
# ===========================================================================
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
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
# Sejak Fase AJ wallpaper unggahan selalu dikompres menjadi JPEG.
m_wp = re.search(r"/wallpaper/([0-9a-f]{24}\.jpg)", hal_tampilan)
must(m_wp is not None, "halaman tampilan menampilkan wallpaper tersimpan")
nama_wp = m_wp.group(1)
st, hdr, data = req_bytes(anon, f"/wallpaper/{nama_wp}")
_img_wp = PILImage.open(io.BytesIO(data))
must(st == 200 and hdr.get_content_type() == "image/jpeg"
     and _img_wp.format == "JPEG",
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
_img_logo = PILImage.open(io.BytesIO(data))
must(st == 200 and hdr.get_content_type() == "image/png"
     and _img_logo.format == "PNG" and _img_logo.mode == "RGBA",
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
g_mati, url, body = login(WALI_G1_USER, WALI_G1_PW)
must("dashboard" not in url and "Akun ini sudah dinonaktifkan." in body,
     "login wali nonaktif ditolak dengan pesan jelas (bukan pesan generik)")
s, url, hasil = req(admin, f"/admin/guru/{g1_id}/aktifkan", {})
must("Guru diaktifkan kembali." in hasil, "aktifkan guru berhasil")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (k1,)) == g1_id,
     "penonaktifan TIDAK mencabut penugasan wali (wali_id tetap)")
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
must("dashboard" in url, "wali bisa login lagi setelah diaktifkan")
# guru kedua tidak pernah scan, TAPI sudah punya Alpa otomatis (metode auto)
# sejak tabel dibuka lewat jam 09:00 -> hapus berarti dinonaktifkan juga.
s, url, hasil = req(admin, f"/admin/guru/{g2_id}/hapus", {})
must("Guru dinonaktifkan karena sudah punya riwayat kehadiran." in hasil,
     "hapus guru ber-riwayat Alpa otomatis -> dinonaktifkan")
# guru ketiga yang BARU dibuat (belum ada catatan apa pun) -> hapus permanen
req(admin, "/admin/guru", {"nama": "Guru Uji Tiga", "mapel": "UJI-Seni"})
s, url, daftar_guru = req(admin, "/admin/guru")
g3_id = id_dari_daftar(daftar_guru, "/admin/guru", "<b>Guru Uji Tiga</b>")
must(g3_id, f"id guru ketiga terbaca: {g3_id}")
s, url, hasil = req(admin, f"/admin/guru/{g3_id}/hapus", {})
must("Guru dihapus." in hasil, "hapus guru tanpa riwayat -> terhapus permanen")
s, url, daftar_guru = req(admin, "/admin/guru")
must("Guru Uji Tiga" not in daftar_guru, "guru terhapus hilang dari daftar")

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
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
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
must(re.search(r"/wallpaper/[0-9a-f]{24}\.jpg", hal_tampilan) is not None,
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
chunk_d = next((c for c in daftar_kelas.split("<tr>") if "UJI-Kelas-Detail" in c), "")
chunk_l = next((c for c in daftar_kelas.split("<tr>") if "UJI-Kelas-Lain" in c), "")
must('<td class="jml">2</td>' in chunk_d and f'href="/admin/kelas/{kd}"' in chunk_d,
     "daftar kelas: kolom Jumlah Siswa UJI-Kelas-Detail = 2 + tautan ke detailnya")
must('<td class="jml">1</td>' in chunk_l and f'href="/admin/kelas/{kl}"' in chunk_l,
     "daftar kelas: kolom Jumlah Siswa UJI-Kelas-Lain = 1 + tautan ke detailnya")
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
chunk_d = next((c for c in daftar_kelas.split("<tr>") if "UJI-Kelas-Detail" in c), "")
must('<td class="jml">3</td>' in chunk_d,
     "daftar kelas: jumlah UJI-Kelas-Detail ikut naik jadi 3 siswa")
# kelas kosong: pesan keadaan kosong + hitungan 0
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Hampa"})
s, url, form_siswa = req(admin, "/admin/siswa")
kh = opt_val(form_siswa, "UJI-Kelas-Hampa")
must(kh, "kelas hampa dibuat")
s, url, detail_kosong = req(admin, f"/admin/kelas/{kh}")
must("Belum ada siswa di kelas ini." in detail_kosong and "0 siswa" in detail_kosong,
     "detail kelas kosong: pesan 'Belum ada siswa di kelas ini.' + judul '0 siswa'")
s, url, daftar_kelas = req(admin, "/admin/kelas")
chunk_h = next((c for c in daftar_kelas.split("<tr>") if "UJI-Kelas-Hampa" in c), "")
must('<td class="jml">0</td>' in chunk_h,
     "daftar kelas: kelas kosong tampil jumlah siswa 0")
# guru tidak boleh membuka detail kelas -> 404
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
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
# siswa, NIS, nama kelas, nama guru, dan mata pelajaran (mapel). Khusus
# ADMIN: wali kelas TIDAK punya akses pencarian sama sekali (kartu
# pencarian disembunyikan dari dashboardnya dan /cari mengarahkannya ke
# dashboard); anonim -> /login.
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
req(admin, "/admin/guru", {"nama": "Guru Cari Spesial", "mapel": "UJI-Senilukis"})
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
# --- wali kelas TIDAK punya akses pencarian (di luar tiga aksesnya) ---
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
s, url, dash_guru = req(guru, "/dashboard")
must('action="/cari"' not in dash_guru,
     "dashboard wali: kartu pencarian DISEMBUNYIKAN")
s, url, hasil_guru = req(guru, "/cari?q=Cari")
must(urllib.parse.urlparse(url).path == "/dashboard",
     "wali membuka /cari -> diarahkan ke dashboard")
s, url, hasil_guru = req(guru, "/tabel-guru")
must(urllib.parse.urlparse(url).path == "/dashboard",
     "wali membuka /tabel-guru -> diarahkan ke dashboard")
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
req(admin, "/admin/guru", {"nama": "Guru Dash Satu", "mapel": "UJI-DashMapel"})
req(admin, "/admin/guru", {"nama": "Guru Dash Dua", "mapel": "UJI-DashMapel"})
s, url, daftar_siswa_n = req(admin, "/admin/siswa")
d3_id = id_dari_daftar(daftar_siswa_n, "/admin/siswa", "UJI-H03")
must(d3_id, "id siswa UJI-H03 terbaca untuk input Sakit manual")
s, url, daftar_guru_n = req(admin, "/admin/guru")
chunk_gd = next((c for c in daftar_guru_n.split("<tr>") if "<b>Guru Dash Satu</b>" in c), "")
m_qr_n = re.search(r"<code>(G\d+)</code>", chunk_gd)
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
# kolom password — login, Ganti Password — otomatis mendapat tombol mata
# dari app.js. Isi app.js/style.css memuat logika & gaya .pw-toggle/
# .pw-wrap; halaman scan tetap memuat app.js tepat 1x. (Model wali kelas:
# form Tambah/Edit Guru TIDAK lagi punya kolom password — dibuktikan
# ketiadaannya di sini, plus tombol "Buat Password Baru" pada Edit Guru
# milik seorang wali.)
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
must('type="password"' not in guru_html,
     "form Tambah Guru TIDAK lagi punya kolom password (login guru lahir "
     "dari penunjukan wali kelas)")
must('<script src="/static/app.js"></script>' in guru_html,
     "halaman Kelola Guru memuat /static/app.js")
gid_o = id_dari_daftar(guru_html, "/admin/guru", "<b>Guru Dash Satu</b>")
must(gid_o, "id guru terbaca untuk uji form Edit Guru")
s, url, edit_guru_html = req(admin, f"/admin/guru/{gid_o}/edit")
must('type="password"' not in edit_guru_html
     and "Buat Password Baru" not in edit_guru_html,
     "form Edit Guru (bukan wali): tanpa kolom password & tanpa tombol "
     "password baru")
must('<script src="/static/app.js"></script>' in edit_guru_html,
     "halaman Edit Guru memuat /static/app.js")
s, url, edit_wali_html = req(admin, f"/admin/guru/{g1_id}/edit")
must("Buat Password Baru" in edit_wali_html and WALI_G1_USER in edit_wali_html,
     "form Edit Guru untuk WALI: username tampil baca-saja + tombol "
     "'Buat Password Baru'")
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
req(admin, "/admin/guru", {"nama": "Guru Belum Satu", "mapel": "UJI-BelumMapel"})
s, url, daftar_siswa_p = req(admin, "/admin/siswa")
b2_id = id_dari_daftar(daftar_siswa_p, "/admin/siswa", "UJI-B02")
must(b2_id, "id siswa UJI-B02 terbaca untuk input Sakit manual")
s, url, daftar_guru_p = req(admin, "/admin/guru")
chunk_gb = next((c for c in daftar_guru_p.split("<tr>") if "<b>Guru Belum Satu</b>" in c), "")
m_qr_p = re.search(r"<code>(G\d+)</code>", chunk_gb)
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
# Kelola Siswa, Kelola Guru, detail kelas, dan hasil /cari berupa TABEL
# dengan kolom pertama "No" (sel <td class="no">; tiap bagian /cari
# mulai dari 1); dan kolom pertama "No" pada tabel mingguan siswa &
# guru.
# Pemeriksaan bersifat sekuens: nomor yang terambil dari HTML harus
# membentuk 1..N berurutan, tahan terhadap data uji kumulatif fase lain.
# ===========================================================================
admin, url, _ = login("admin", "admin123")


def nomor_dari(htmltxt, pola):
    return [int(x) for x in re.findall(pola, htmltxt)]


def sekuens_utuh(angka):
    return angka == list(range(1, len(angka) + 1))


# --- Kelola Siswa: setiap baris tabel bernomor urut 1..N sesuai tampil ---
s, url, hal_siswa = req(admin, "/admin/siswa")
no_siswa = nomor_dari(hal_siswa, r'<td class="no">(\d+)</td>')
must(len(no_siswa) >= 2 and sekuens_utuh(no_siswa),
     "Kelola Siswa: nomor baris membentuk urutan 1..N "
     f"(terambil {len(no_siswa)} baris)")
must(re.search(r'<td class="no">1</td><td class="nama"><b>', hal_siswa) is not None
     and re.search(r'<td class="no">2</td><td class="nama"><b>', hal_siswa) is not None,
     "Kelola Siswa: baris pertama & kedua berawalan nomor 1 dan 2 sebelum nama")
# --- Kelola Guru: pola yang sama ---
s, url, hal_guru = req(admin, "/admin/guru")
no_guru = nomor_dari(hal_guru, r'<td class="no">(\d+)</td>')
must(len(no_guru) >= 2 and sekuens_utuh(no_guru),
     "Kelola Guru: nomor baris membentuk urutan 1..N "
     f"(terambil {len(no_guru)} baris)")
# --- /cari: bagian Siswa & Guru (tabel) masing-masing bernomor dari 1 ---
s, url, hasil_cari = req(admin, "/cari?q=Cari")
m_sis = re.search(r"<h3>Siswa</h3><div class=\"table-scroll\">(.*?)</table>",
                  hasil_cari, re.S)
must(m_sis is not None, "/cari?q=Cari: bagian Siswa tampil")
no_cari_siswa = nomor_dari(m_sis.group(1), r'<td class="no">(\d+)</td>')
must(len(no_cari_siswa) >= 2 and sekuens_utuh(no_cari_siswa),
     "/cari: baris bagian Siswa bernomor 1..N dari awal "
     f"(terambil {len(no_cari_siswa)} baris)")
m_gur = re.search(r"<h3>Guru</h3><div class=\"table-scroll\">(.*?)</table>",
                  hasil_cari, re.S)
must(m_gur is not None, "/cari?q=Cari: bagian Guru tampil")
no_cari_guru = nomor_dari(m_gur.group(1), r'<td class="no">(\d+)</td>')
must(len(no_cari_guru) >= 1 and sekuens_utuh(no_cari_guru),
     "/cari: baris bagian Guru bernomor mulai dari 1 lagi "
     f"(terambil {len(no_cari_guru)} baris)")
# --- detail kelas: tabel dengan kolom "No" (dulu <ol> bernomor CSS) ---
kc_r = dbq("SELECT id FROM classes WHERE nama='UJI-Kelas-Cari'")
s, url, hal_detail = req(admin, f"/admin/kelas/{kc_r}")
must('<table class="tabel kelola">' in hal_detail,
     "detail kelas: daftar siswa kini berupa tabel")
no_detail = nomor_dari(hal_detail, r'<td class="no">(\d+)</td>')
must(len(no_detail) >= 2 and sekuens_utuh(no_detail),
     "detail kelas: nomor baris tabel membentuk urutan 1..N "
     f"(terambil {len(no_detail)} baris)")
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
req(admin, "/admin/guru", {"nama": "Guru Lapor Telat", "mapel": "UJI-LaporMapel"})
req(admin, "/admin/guru", {"nama": "Guru Lapor Belum", "mapel": "UJI-LaporMapel"})
s, url, daftar_siswa_s = req(admin, "/admin/siswa")
c_sakit_id = id_dari_daftar(daftar_siswa_s, "/admin/siswa", "UJI-R03")
must(c_sakit_id, "id siswa UJI-R03 terbaca untuk input Sakit manual")
s, url, daftar_guru_s = req(admin, "/admin/guru")
chunk_gx = next((c for c in daftar_guru_s.split("<tr>") if "<b>Guru Lapor Telat</b>" in c), "")
m_qr_s = re.search(r"<code>(G\d+)</code>", chunk_gx)
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
req(admin, "/admin/guru", {"nama": "Guru Reset Telat", "mapel": "UJI-ResetMapel"})
req(admin, "/admin/guru", {"nama": "Guru Reset Belum", "mapel": "UJI-ResetMapel"})
s, url, daftar_guru_t = req(admin, "/admin/guru")


def qr_guru_t(htmltxt, nama_guru):
    chunk = next((c for c in htmltxt.split("<tr>")
                  if f"<b>{nama_guru}</b>" in c), "")
    m = re.search(r"<code>(G\d+)</code>", chunk)
    return m.group(1) if m else None


gr1_qr = qr_guru_t(daftar_guru_t, "Guru Reset Telat")
gr2_qr = qr_guru_t(daftar_guru_t, "Guru Reset Belum")
must(gr1_qr and gr2_qr, "kode QR kedua guru Fase T terbaca")
t03_id = dbq("SELECT id FROM students WHERE kode='UJI-T03'")
gr2_id = dbq("SELECT id FROM users WHERE nama='Guru Reset Belum'")
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
# Fase U — Akses halaman laporan pada model wali kelas: anonim
# diarahkan ke /login; guru yang BUKAN wali (gurulapor1, guru Fase S)
# tidak bisa login sama sekali; dan wali kelas yang login pun DIBLOKIR
# dari /laporan/siswa & /laporan/guru (halaman laporan hanya untuk
# admin/petugas — panelnya juga disembunyikan dari dashboard wali).
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
guru_u, url, body_u = login("gurulapor1", "gurulapor123")
must(urllib.parse.urlparse(url).path == "/login"
     and "Username atau password salah." in body_u,
     "guru bukan wali (gurulapor1) TIDAK bisa login sama sekali")
wali_u, url, _ = login(WALI_G1_USER, WALI_G1_PW)
must(urllib.parse.urlparse(url).path == "/dashboard",
     "wali kelas bisa login untuk Fase U")
s, url, dash_u = req(wali_u, "/dashboard")
must("Laporan Hari Ini" not in dash_u and 'href="/laporan/' not in dash_u,
     "dashboard wali pukul 09:10: panel Laporan Hari Ini DISEMBUNYIKAN")
s, url, lap_s_u = req(wali_u, "/laporan/siswa")
must(urllib.parse.urlparse(url).path == "/dashboard",
     "wali membuka /laporan/siswa -> diarahkan ke dashboard")
s, url, lap_g_u = req(wali_u, "/laporan/guru")
must(urllib.parse.urlparse(url).path == "/dashboard",
     "wali membuka /laporan/guru -> diarahkan ke dashboard")

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
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)


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
m_wp_y = re.search(r"/wallpaper/([0-9a-f]{24}\.jpg)", hal_tampilan)
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
must("ikon-tema-cluster" not in dash,
     "dashboard TIDAK lagi memuat klaster ikon dekoratif di kepala "
     "(dihapus per permintaan user)")
must("20.6c.9-3.9" in dash,
     "SVG ikon siswa dari ikon_tema tetap tampil di kartu statistik dashboard")
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

# ===========================================================================
# Fase AA — Label lisensi TERKUNCI (env ABSEN_LISENSI): nama sekolah
# pembeli dipasang PENJUAL di server per pemasangan. Label tampil di
# bawah kartu login dan di footer aplikasi, tetapi TIDAK disimpan di
# tabel settings, TIDAK ikut backup, dan TIDAK bisa diubah dari dalam
# aplikasi (tidak ada kolom/formulir lisensi di halaman mana pun).
# Env tidak diisi -> label tidak tampil sama sekali (perilaku lama).
# ===========================================================================
start_server(extra_env={"ABSEN_LISENSI": "SEKOLAH UJI LISENSI"})
anon = client()
s, url, login_page = req(anon, "/login")
must("Dilisensikan untuk: SEKOLAH UJI LISENSI" in login_page,
     "LISENSI: halaman login menampilkan label dari env ABSEN_LISENSI")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Dilisensikan untuk: SEKOLAH UJI LISENSI" in dash,
     "LISENSI: footer dashboard menampilkan label lisensi")

# Tidak ada cara mengubahnya dari dalam aplikasi: halaman-halaman
# pengaturan tidak memuat kolom bernama lisensi, dan teks label tidak
# muncul di dalam formulir mana pun (label hanya hidup di luar form:
# bawah kartu login & footer).
for path_cek in ("/admin/tampilan", "/admin/backup", "/ganti-password"):
    s, url, hal = req(admin, path_cek)
    nama_kolom = re.findall(r'name="([^"]+)"', hal)
    must(all("lisensi" not in n.lower() for n in nama_kolom),
         f"LISENSI: {path_cek} tidak memuat kolom formulir lisensi")
    for blok_form in re.findall(r"<form.*?</form>", hal, re.S):
        must("Dilisensikan untuk" not in blok_form,
             f"LISENSI: label tidak muncul di dalam formulir {path_cek}")

# Label murni dari env: database tidak menyimpan kunci lisensi apa pun.
must(dbq("SELECT COUNT(*) FROM settings WHERE LOWER(key) LIKE '%lisensi%'")
     == 0,
     "LISENSI: tabel settings tidak memuat kunci lisensi apa pun")

# Env tidak diisi -> label hilang total, halaman seperti semula.
start_server()
anon = client()
s, url, login_page = req(anon, "/login")
must("Dilisensikan untuk" not in login_page,
     "LISENSI: tanpa env, halaman login tanpa label (perilaku lama)")
admin, url, _ = login("admin", "admin123")
s, url, dash = req(admin, "/dashboard")
must("Dilisensikan untuk" not in dash,
     "LISENSI: tanpa env, footer dashboard tanpa label (perilaku lama)")

# ===========================================================================
# Fase AB — Pembersihan tampilan per permintaan user: (1) klaster ikon
# dekoratif di kepala dashboard DIHAPUS (ikon_tema tetap dipakai kartu
# statistik, kartu per kelas, dan keadaan kosong); (2) Kelola Siswa,
# Kelola Guru, Kelola Kelas, dan daftar siswa di detail kelas kini
# berupa TABEL (kelas .tabel.kelola) dengan kepala kolom yang jelas,
# menggantikan daftar <ul>/<ol> lama.
# ===========================================================================
s, url, hal_siswa = req(admin, "/admin/siswa")
must('<table class="tabel kelola">' in hal_siswa,
     "Kelola Siswa: daftar kini berupa tabel")
for kepala in ('<th class="no">No</th>', '<th class="nama">Nama</th>',
               '<th>NIS</th>', '<th>Kelas</th>', '<th>Aksi</th>'):
    must(kepala in hal_siswa, f"Kelola Siswa: kepala kolom {kepala} tampil")
s, url, hal_guru = req(admin, "/admin/guru")
must('<table class="tabel kelola">' in hal_guru,
     "Kelola Guru: daftar kini berupa tabel")
for kepala in ('<th class="no">No</th>', '<th class="nama">Nama</th>',
               '<th>Mapel</th>', '<th>Password</th>', '<th>Kode QR</th>',
               '<th>Aksi</th>'):
    must(kepala in hal_guru, f"Kelola Guru: kepala kolom {kepala} tampil")
s, url, hal_kelas = req(admin, "/admin/kelas")
must('<table class="tabel kelola">' in hal_kelas,
     "Kelola Kelas: daftar kini berupa tabel")
for kepala in ('<th class="no">No</th>', '<th class="nama">Nama Kelas</th>',
               '<th>Jumlah Siswa</th>', '<th>Aksi</th>'):
    must(kepala in hal_kelas, f"Kelola Kelas: kepala kolom {kepala} tampil")
n_k1 = dbq("SELECT COUNT(*) FROM students s JOIN classes c "
           "ON c.id=s.class_id WHERE c.nama='UJI-Kelas-1'")
chunk_k1_ab = next((c for c in hal_kelas.split("<tr>")
                    if "UJI-Kelas-1" in c), "")
must(f'<td class="jml">{n_k1}</td>' in chunk_k1_ab,
     f"Kelola Kelas: kolom Jumlah Siswa UJI-Kelas-1 = {n_k1} (sesuai DB)")
k1_id = dbq("SELECT id FROM classes WHERE nama='UJI-Kelas-1'")
s, url, hal_detail1 = req(admin, f"/admin/kelas/{k1_id}")
must('<table class="tabel kelola">' in hal_detail1,
     "detail kelas: daftar siswa kini berupa tabel")
for kepala in ('<th class="no">No</th>', '<th class="nama">Nama</th>',
               '<th>NIS</th>', '<th>Aksi</th>'):
    must(kepala in hal_detail1, f"detail kelas: kepala kolom {kepala} tampil")
s, url, dash = req(admin, "/dashboard")
must("ikon-tema-cluster" not in dash and 'class="ikon-tema"' not in dash,
     "dashboard: klaster ikon dekoratif di kepala sudah tidak ada")
must("20.6c.9-3.9" in dash and "9.8V13l2.4 1.6" in dash,
     "dashboard: ikon siswa & jam dari ikon_tema tetap tampil "
     "di kartu statistik")

# ===========================================================================
# Fase AC — /cari berupa tabel + halaman Detail Siswa (/siswa/<id>):
# identitas, Rekap Absensi 6 Bulan Terakhir (label bulan Indonesia,
# hitungan per status dari tabel kehadiran, baris Total), dan tabel
# Rincian Kehadiran per catatan (tanggal berhari Indonesia, pil status,
# jam scan untuk Hadir/Terlambat, "—" untuk Sakit/Izin/Alpa, terbaru
# dulu). Skenario dua bulan simulasi: scan Terlambat September 2026,
# scan Hadir + Sakit manual November 2026.
# ===========================================================================
start_server()  # waktu nyata: siapkan data khas fase ini
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Rekap"})
s, url, form_siswa = req(admin, "/admin/siswa")
krek = opt_val(form_siswa, "UJI-Kelas-Rekap")
must(krek, "kelas UJI-Kelas-Rekap dibuat untuk uji detail siswa")
req(admin, "/admin/siswa", {"nama": "Siswa Rekap Uji", "kode": "UJI-RKP1",
                            "class_id": krek})
rek_id = dbq("SELECT id FROM students WHERE kode='UJI-RKP1'")
must(rek_id, "id Siswa Rekap Uji terbaca")

# --- /cari: kedua bagian kini tabel dengan kepala kolom lengkap ---
s, url, hasil_cari = req(admin, "/cari?q=Cari")
must(hasil_cari.count('<table class="tabel kelola">') == 2,
     "/cari: bagian Siswa dan Guru sama-sama berupa tabel")
for kepala in ('<th>NIS</th>', '<th>Kelas</th>', '<th>Mapel</th>',
               '<th>Username</th>', '<th>Kode QR</th>', '<th>Aksi</th>'):
    must(kepala in hasil_cari, f"/cari: kepala kolom {kepala} tampil")
s, url, hasil_rekap = req(admin, "/cari?q=Rekap")
must(f'href="/siswa/{rek_id}"' in hasil_rekap,
     "/cari: baris siswa memuat tautan Detail ke /siswa/<id>")

# --- September 2026 (Senin 14 Sep, 07:30): scan Terlambat ---
start_server("2026-09-14T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-RKP1"})
must("Siswa Rekap Uji" in hasil and "Terlambat" in hasil,
     "scan Siswa Rekap Uji 14 Sep 07:30 -> Terlambat")

# --- November 2026 (Rabu 25 Nov, 06:45): scan Hadir + Sakit 24 Nov ---
start_server("2026-11-25T06:45:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-RKP1"})
must("Siswa Rekap Uji" in hasil and "Hadir" in hasil,
     "scan Siswa Rekap Uji 25 Nov 06:45 -> Hadir")
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": "2026-11-24", "tipe": "siswa", "id": rek_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "Siswa Rekap Uji ditandai Sakit 24 Nov")

# --- halaman detail pada simulasi November 2026 ---
s, url, det = req(admin, f"/siswa/{rek_id}")
must("Siswa Rekap Uji" in det and "UJI-RKP1" in det
     and "UJI-Kelas-Rekap" in det,
     "detail siswa: identitas nama + NIS + kelas tampil")
must(f'href="/admin/siswa/{rek_id}/edit"' in det,
     "detail siswa: admin melihat tautan Edit")
must("Rekap Absensi 6 Bulan Terakhir" in det,
     "detail siswa: tabel rekap 6 bulan tampil")
label_bulan = ["Juni 2026", "Juli 2026", "Agustus 2026", "September 2026",
               "Oktober 2026", "November 2026"]
posisi = [det.find(f'<td class="nama">{lbl}</td>') for lbl in label_bulan]
must(all(p >= 0 for p in posisi) and posisi == sorted(posisi),
     "detail siswa: 6 label bulan tampil berurutan Juni..November 2026")


def baris_bulan(htmltxt, label):
    m = re.search(r'<td class="nama">' + re.escape(label) + r'</td>'
                  r'<td>(\d+)</td><td>(\d+)</td><td>(\d+)</td>'
                  r'<td>(\d+)</td><td>(\d+)</td>', htmltxt)
    return tuple(int(x) for x in m.groups()) if m else None


must(baris_bulan(det, "September 2026") == (0, 1, 0, 0, 0),
     "rekap September 2026: Terlambat 1, lainnya 0")
# November berisi Hadir (scan 25 Nov) + Sakit (manual 24 Nov) + 1 Alpa
# otomatis Senin 23 Nov: redirect sesudah input Sakit membuka tabel
# mingguan, yang mematerialkan Alpa otomatis hari lampau — perilaku
# aplikasi yang memang benar untuk siswa yang tidak scan hari itu.
must(baris_bulan(det, "November 2026") == (1, 0, 1, 0, 1),
     "rekap November 2026: Hadir 1, Sakit 1, Alpa otomatis 1")
must(baris_bulan(det, "Oktober 2026") == (0, 0, 0, 0, 0),
     "rekap Oktober 2026: semua 0 (bulan tanpa catatan)")
m_tot = re.search(r'<tr class="total-row"><td class="nama">Total</td>'
                  r'<td>(\d+)</td><td>(\d+)</td><td>(\d+)</td>'
                  r'<td>(\d+)</td><td>(\d+)</td>', det)
must(m_tot is not None
     and tuple(int(x) for x in m_tot.groups()) == (1, 1, 1, 0, 1),
     "rekap baris Total: Hadir 1, Terlambat 1, Sakit 1, Izin 0, Alpa 1")

# --- rincian kehadiran: terbaru dulu, jam scan tampil, Sakit jam "—" ---
must("Rincian Kehadiran" in det,
     "detail siswa: tabel Rincian Kehadiran tampil")
i_hadir = det.find("Rabu, 25 November 2026")
i_sakit = det.find("Selasa, 24 November 2026")
i_alpa = det.find("Senin, 23 November 2026")
i_telat = det.find("Senin, 14 September 2026")
must(0 <= i_hadir < i_sakit < i_alpa < i_telat,
     "rincian terurut terbaru dulu: 25 Nov, 24 Nov, 23 Nov, lalu 14 Sep")
chunk_telat = next((c for c in det.split("<tr>")
                    if "14 September 2026" in c), "")
must("Terlambat" in chunk_telat and "07:30" in chunk_telat,
     "rincian Terlambat: pil status + jam scan 07:30 tampil persis")
chunk_sakit = next((c for c in det.split("<tr>")
                    if "24 November 2026" in c), "")
must("Sakit" in chunk_sakit and "<td>&mdash;</td>" in chunk_sakit,
     "rincian Sakit: kolom Jam tampil '—'")
chunk_alpa = next((c for c in det.split("<tr>")
                   if "23 November 2026" in c), "")
must("Alpa" in chunk_alpa and "<td>&mdash;</td>" in chunk_alpa,
     "rincian Alpa otomatis: kolom Jam tampil '—'")
chunk_hadir = next((c for c in det.split("<tr>")
                    if "25 November 2026" in c), "")
must("Hadir" in chunk_hadir and "06:45" in chunk_hadir,
     "rincian Hadir: pil status + jam scan 06:45 tampil")

# --- detail siswa untuk wali kelas: hanya siswa kelasnya sendiri ---
guru, url, _ = login(WALI_G1_USER, WALI_G1_PW)
s, url, det_guru = req(guru, f"/siswa/{rek_id}")
must(urllib.parse.urlparse(url).path == "/dashboard"
     and "Data tidak ditemukan." in det_guru and "Detail Siswa" not in det_guru,
     "wali TIDAK bisa membuka detail siswa kelas lain (UJI-Kelas-Rekap)")
s, url, det_guru = req(guru, f"/siswa/{A_id}")
must("Siswa Uji A" in det_guru
     and "Rekap Absensi 6 Bulan Terakhir" in det_guru,
     "wali bisa membuka detail siswa kelasnya sendiri (UJI-Kelas-1)")
must(f'href="/admin/siswa/{A_id}/edit"' not in det_guru,
     "detail siswa untuk wali: tanpa tautan Edit admin")
anon = client()
s, url, hal = req(anon, f"/siswa/{rek_id}")
must(urllib.parse.urlparse(url).path == "/login",
     "anonim membuka /siswa/<id> -> diarahkan ke /login")
s, url, hasil = req(admin, "/siswa/99999")
must("Data tidak ditemukan." in hasil,
     "detail id tak dikenal -> 'Data tidak ditemukan.'")

# ===========================================================================
# Fase AD — Model LOGIN HANYA WALI KELAS secara khusus: penunjukan lewat
# Kelola Kelas (memilih guru yang SUDAH ada, tanpa mengetik ulang),
# kredensial otomatis (username wali<slug kelas> + password ACAK 10
# karakter yang tampil SATU kali), pembatasan akses wali (nav, tabel
# terkunci, cetak terkunci, edit manual terkunci, rute terlarang),
# siklus hidup penugasan (tidak berubah -> password tetap; "Buat
# Password Baru"; diganti -> login lama mati; dicabut -> login mati;
# sesi terputus di tengah jalan), eksklusi wali ganda, dan penugasan
# yang ikut tersimpan & pulih lewat Backup & Pulihkan. Plus perbaikan
# keterbacaan label lisensi/footer di style.css (huruf tebal + halo +
# selubung footer di atas wallpaper).
# ===========================================================================
start_server()  # waktu nyata: data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Wali"})
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Samping"})
s, url, form_siswa = req(admin, "/admin/siswa")
kw_id = opt_val(form_siswa, "UJI-Kelas-Wali")
ks_id = opt_val(form_siswa, "UJI-Kelas-Samping")
must(kw_id and ks_id, "AD: kelas UJI-Kelas-Wali & UJI-Kelas-Samping dibuat")
req(admin, "/admin/siswa", {"nama": "Siswa Wali Satu", "kode": "UJI-W01", "class_id": kw_id})
req(admin, "/admin/siswa", {"nama": "Siswa Samping Satu", "kode": "UJI-S01", "class_id": ks_id})
req(admin, "/admin/guru", {"nama": "Guru Wali Satu", "mapel": "UJI-WaliMapel"})
req(admin, "/admin/guru", {"nama": "Guru Wali Dua", "mapel": "UJI-WaliMapel"})
req(admin, "/admin/guru", {"nama": "Guru Bebas", "mapel": "UJI-WaliMapel"})
s, url, daftar_guru_ad = req(admin, "/admin/guru")
gw1 = id_dari_daftar(daftar_guru_ad, "/admin/guru", "<b>Guru Wali Satu</b>")
gw2 = id_dari_daftar(daftar_guru_ad, "/admin/guru", "<b>Guru Wali Dua</b>")
gb = id_dari_daftar(daftar_guru_ad, "/admin/guru", "<b>Guru Bebas</b>")
must(gw1 and gw2 and gb, f"AD: id ketiga guru terbaca: {gw1}, {gw2}, {gb}")
w01_id = dbq("SELECT id FROM students WHERE kode='UJI-W01'")
s01_id = dbq("SELECT id FROM students WHERE kode='UJI-S01'")

# --- Kelola Kelas: kolom Wali Kelas di tabel yang sama (tabel kelola) ---
s, url, daftar_kelas_ad = req(admin, "/admin/kelas")
must("<th>Wali Kelas</th>" in daftar_kelas_ad,
     "AD: Kelola Kelas punya kolom 'Wali Kelas'")
chunk_kw = next((c for c in daftar_kelas_ad.split("<tr>") if "UJI-Kelas-Wali" in c), "")
must("Belum ada" in chunk_kw,
     "AD: kelas baru tampil '— Belum ada' di kolom Wali Kelas")

# --- form Edit Kelas TIDAK lagi memuat pemilih wali: penugasan pindah
# ke dropdown sebaris pada kolom Wali Kelas di tabel Kelola Kelas ---
s, url, form_edit_kw = req(admin, f"/admin/kelas/{kw_id}/edit")
must('name="wali_id"' not in form_edit_kw,
     "AD: form Edit Kelas TANPA pemilih Wali Kelas (pindah ke kolom tabel)")
must('name="nama"' in form_edit_kw,
     "AD: form Edit Kelas tetap punya kolom nama kelas")
must(f'action="/admin/kelas/{kw_id}/wali"' in chunk_kw
     and '<select name="wali_id"' in chunk_kw
     and 'onchange="this.form.submit()"' in chunk_kw,
     "AD: kolom Wali Kelas = form dropdown sebaris (terkirim saat diganti)")
for nm in ("Guru Wali Satu", "Guru Wali Dua", "Guru Bebas"):
    must(nm in chunk_kw, f"AD: dropdown wali memuat guru yang ada: {nm}")
must("UJI-WaliMapel" in chunk_kw,
     "AD: pilihan wali tampil dalam format 'Nama — Mapel'")
must('<option value="" selected>' in chunk_kw,
     "AD: kelas tanpa wali: pilihan '— Belum ada —' (value='') terpilih")

# --- penunjukan: kredensial otomatis terbit lewat flash satu kali ---
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": str(gw1)})
m_ad = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_ad is not None and m_ad.group(1) == "waliujikelaswali",
     "AD: penunjukan menerbitkan username waliujikelaswali + password acak")
pw_ad1 = m_ad.group(2)
must(len(pw_ad1) == 10 and pw_ad1.isalnum(),
     "AD: password sementara acak tepat 10 karakter huruf+angka")
s, url, daftar_kelas_ad = req(admin, "/admin/kelas")
chunk_kw = next((c for c in daftar_kelas_ad.split("<tr>") if "UJI-Kelas-Wali" in c), "")
must("Guru Wali Satu" in chunk_kw,
     "AD: kolom Wali Kelas kini memuat nama walinya")
s, url, daftar_guru_ad = req(admin, "/admin/guru")
chunk_gw1 = next((c for c in daftar_guru_ad.split("<tr>") if "<b>Guru Wali Satu</b>" in c), "")
must(f"<code>{pw_ad1}</code>" in chunk_gw1
     and "waliujikelaswali" not in chunk_gw1 and "wali-badge" not in chunk_gw1,
     "AD: Kelola Guru: baris wali hanya menampilkan password di kolom Password")
chunk_gb = next((c for c in daftar_guru_ad.split("<tr>") if "<b>Guru Bebas</b>" in c), "")
must("<td>&mdash;</td>" in chunk_gb,
     "AD: Kelola Guru: guru bukan wali tampil '—' di kolom Password")

# --- login wali & navigasi terbatas ---
wali, url, _ = login("waliujikelaswali", pw_ad1)
must("dashboard" in url, "AD: wali login dengan password dari flash penunjukan")
s, url, dash_w = req(wali, "/dashboard")
must("Scan Gerbang" not in dash_w and "Tabel Guru" not in dash_w,
     "AD: nav wali TANPA Scan Gerbang & Tabel Guru")
must('href="/tabel-siswa"' in dash_w and 'href="/cetak"' in dash_w
     and 'href="/ganti-password"' in dash_w,
     "AD: nav wali memuat Tabel Siswa, Cetak Kartu, Ganti Password")
must("/admin/" not in dash_w,
     "AD: halaman wali tanpa tautan /admin/ apa pun")
must('action="/cari"' not in dash_w,
     "AD: dashboard wali tanpa kartu pencarian")
s, url, menu_w = req(wali, "/menu")
must("Scan Gerbang" not in menu_w and "Wali Kelas UJI-Kelas-Wali" in menu_w,
     "AD: halaman Menu wali terbatas + label 'Wali Kelas UJI-Kelas-Wali'")

# --- tabel siswa terkunci ke kelas sendiri ---
s, url, tab_w = req(wali, "/tabel-siswa")
must("Siswa Wali Satu" in tab_w and "Siswa Samping Satu" not in tab_w,
     "AD: tabel wali langsung menampilkan kelasnya sendiri")
must('name="kelas"' not in tab_w and "Kelas Anda:" in tab_w,
     "AD: tabel wali tanpa pemilih kelas; ada label 'Kelas Anda:'")
s, url, tab_w2 = req(wali, f"/tabel-siswa?kelas={ks_id}")
must("Siswa Wali Satu" in tab_w2 and "Siswa Samping Satu" not in tab_w2,
     "AD: parameter kelas lain diabaikan (tetap kelas sendiri)")

# --- rute terlarang untuk wali ---
for path_blk in ("/tabel-guru", "/tabel-guru/excel", "/laporan/siswa",
                 "/laporan/guru", "/cari", "/scan-gerbang", "/cetak/guru"):
    s, url, hal = req(wali, path_blk)
    must(urllib.parse.urlparse(url).path == "/dashboard",
         f"AD: wali membuka {path_blk} -> diarahkan ke dashboard")
s, url, hal = req(wali, "/api/scan", {"kode": "UJI-W01"})
must(urllib.parse.urlparse(url).path == "/dashboard",
     "AD: wali POST /api/scan -> diarahkan ke dashboard (scan bukan akses wali)")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE ref_type='siswa' "
         "AND ref_id=? AND metode='scan'", (w01_id,)) == 0,
     "AD: POST scan oleh wali tidak mencatat kehadiran apa pun")
try:
    req(wali, "/admin/kelas")
    must(False, "AD: wali membuka /admin/kelas harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "AD: wali membuka /admin/kelas -> 404")

# --- cetak kartu terkunci ke kelas sendiri ---
s, url, cetak_w = req(wali, "/cetak")
must(f"/cetak/kelas/{kw_id}" in cetak_w,
     "AD: cetak wali: tautan kelasnya sendiri tampil")
must(f"/cetak/kelas/{ks_id}" not in cetak_w and "/cetak/guru" not in cetak_w,
     "AD: cetak wali: TANPA kelas lain & tanpa kartu guru")
s, url, lembar_w = req(wali, f"/cetak/kelas/{kw_id}")
must("Siswa Wali Satu" in lembar_w and "UJI-W01" in lembar_w,
     "AD: lembar kartu kelas wali berisi siswa kelasnya")
try:
    req(wali, f"/cetak/kelas/{ks_id}")
    must(False, "AD: wali membuka kartu kelas lain harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "AD: wali membuka kartu kelas lain -> 404")

# --- detail siswa & edit manual: hanya kelas sendiri ---
s, url, det_w = req(wali, f"/siswa/{w01_id}")
must("Siswa Wali Satu" in det_w, "AD: wali membuka detail siswa kelasnya")
s, url, det_w = req(wali, f"/siswa/{s01_id}")
must("Data tidak ditemukan." in det_w,
     "AD: wali membuka detail siswa kelas lain -> 'Data tidak ditemukan.'")
s, url, hasil = req(wali, "/kehadiran/edit",
                    {"tanggal": "2026-11-26", "tipe": "siswa", "id": w01_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil,
     "AD: wali bisa mencatat Sakit untuk siswa kelasnya")
must(dbq("SELECT status FROM kehadiran WHERE tanggal='2026-11-26' "
         "AND ref_type='siswa' AND ref_id=?", (w01_id,)) == "Sakit",
     "AD: catatan Sakit oleh wali tersimpan di DB")
s, url, hasil = req(wali, "/kehadiran/edit",
                    {"tanggal": "2026-11-26", "tipe": "siswa", "id": s01_id,
                     "status": "Sakit", "jam": ""})
must("Data tidak ditemukan." in hasil,
     "AD: wali mencatat siswa kelas lain -> ditolak 'Data tidak ditemukan.'")
must(dbq("SELECT COUNT(*) FROM kehadiran WHERE tanggal='2026-11-26' "
         "AND ref_type='siswa' AND ref_id=?", (s01_id,)) == 0,
     "AD: tidak ada catatan yang dibuat untuk siswa kelas lain")
s, url, form_edit_w = req(wali, f"/kehadiran/edit?tanggal=2026-11-26&tipe=siswa&id={s01_id}")
must("Data tidak ditemukan." in form_edit_w,
     "AD: form edit siswa kelas lain juga tertutup untuk wali")

# --- edit kelas TIDAK menyentuh penugasan wali sama sekali ---
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/edit",
                    {"nama": "UJI-Kelas-Wali"})
must("password sementara" not in hasil,
     "AD: edit kelas (nama saja): tidak ada password baru diterbitkan")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (kw_id,)) == gw1,
     "AD: edit kelas tidak mengubah penugasan wali (tetap Guru Wali Satu)")
wali2, url, _ = login("waliujikelaswali", pw_ad1)
must("dashboard" in url,
     "AD: password lama wali tetap berlaku sesudah edit tanpa ganti wali")

# --- "Buat Password Baru" (Edit Guru): yang lama mati, yang baru jalan ---
s, url, hasil = req(admin, f"/admin/guru/{gw1}/password-baru", {})
m_pb = re.search(r"Password baru untuk .*: (\S+) \(sampaikan", hasil)
must(m_pb is not None,
     "AD: 'Buat Password Baru' menerbitkan password acak (flash satu kali)")
pw_ad2 = m_pb.group(1)
must(pw_ad2 != pw_ad1, "AD: password baru berbeda dari password sebelumnya")
g_old, url, body = login("waliujikelaswali", pw_ad1)
must("dashboard" not in url,
     "AD: password lama MATI sesudah 'Buat Password Baru'")
wali3, url, _ = login("waliujikelaswali", pw_ad2)
must("dashboard" in url, "AD: password baru dari tombol admin bisa dipakai login")

# --- eksklusi: wali suatu kelas tidak ditawarkan ke kelas lain ---
s, url, daftar_kelas_ks = req(admin, "/admin/kelas")
chunk_ks = next((c for c in daftar_kelas_ks.split("<tr>")
                 if "UJI-Kelas-Samping" in c), "")
must("Guru Wali Satu" not in chunk_ks,
     "AD: guru yang sudah wali kelas lain TIDAK ditawarkan di dropdown kelas lain")
must("Guru Wali Dua" in chunk_ks and "Guru Bebas" in chunk_ks,
     "AD: guru yang bebas tetap ditawarkan di dropdown kelas lain")

# --- penggantian wali: login lama mati, login baru jalan ---
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": str(gw2)})
m_rp = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_rp is not None and "Guru Wali Dua" in hasil,
     "AD: penggantian wali menerbitkan kredensial untuk wali baru")
pw_ad3 = m_rp.group(2)
must(dbq("SELECT wali_id FROM classes WHERE id=?", (kw_id,)) == gw2,
     "AD: DB: wali UJI-Kelas-Wali kini Guru Wali Dua")
must(dbq("SELECT username FROM users WHERE id=?", (gw1,)) == f"guru-{gw1}",
     "AD: username wali lama diparkir ke 'guru-<id>'")
g_old2, url, body = login("waliujikelaswali", pw_ad2)
must("dashboard" not in url and "Username atau password salah." in body,
     "AD: login wali LAMA mati sesudah diganti")
wali4, url, _ = login("waliujikelaswali", pw_ad3)
must("dashboard" in url, "AD: wali BARU bisa login dengan kredensial barunya")

# --- pencabutan wali: login mati ---
# (dropdown sebaris mengirim wali_id="" untuk pilihan "— Belum ada —")
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": ""})
must("Wali kelas UJI-Kelas-Wali dicabut" in hasil,
     "AD: pencabutan wali dikonfirmasi lewat pesan flash")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (kw_id,)) is None,
     "AD: DB: wali_id NULL sesudah dicabut")
g_old3, url, body = login("waliujikelaswali", pw_ad3)
must("dashboard" not in url, "AD: login wali mati sesudah penugasan dicabut")

# --- sesi terputus di tengah jalan begitu penugasan dicabut ---
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": str(gb)})
m_ms = re.search(r"password sementara: (\S+)", hasil)
must(m_ms is not None, "AD: penunjukan Guru Bebas menerbitkan password")
pw_ad4 = m_ms.group(1)
wali5, url, _ = login("waliujikelaswali", pw_ad4)
must("dashboard" in url, "AD: Guru Bebas bisa login sesudah ditunjuk wali")
s, url, dash5 = req(wali5, "/dashboard")
must("Dashboard" in dash5, "AD: sesi wali aktif bisa membuka dashboard")
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": ""})
s, url, hal5 = req(wali5, "/dashboard")
must(urllib.parse.urlparse(url).path == "/login",
     "AD: sesi wali yang dicabut di tengah jalan -> permintaan berikutnya "
     "diarahkan ke /login")

# --- keterbacaan lisensi & footer di style.css (perbaikan dari user) ---
s, headers, css_ad_bytes = req_bytes(admin, "/static/style.css")
css_ad = css_ad_bytes.decode("utf-8", "replace")
m_lis = re.search(r"(?m)^\.lisensi\s*\{([^}]*)\}", css_ad)
must(m_lis is not None and "font-weight: 700" in m_lis.group(1),
     "CSS: .lisensi memakai font-weight 700 (huruf tebal)")
must(m_lis is not None and "#14213d" in m_lis.group(1)
     and "text-shadow" in m_lis.group(1),
     "CSS: .lisensi gelap #14213d + halo text-shadow (terbaca di "
     "wallpaper terang/ramai)")
must(re.search(r"footer small\s*\{[^}]*font-weight:\s*6\d\d", css_ad) is not None,
     "CSS: teks kecil footer (baris ©) ditebalkan")
must(".bg-wallpaper ~ .shell footer" in css_ad
     and "backdrop-filter: blur(3px)" in css_ad,
     "CSS: footer di atas wallpaper memakai selubung tembus pandang + blur")
must(not any(".lisensi" in ln and "tema-" in ln
             for ln in css_ad.splitlines()),
     "CSS: tidak ada aturan tema pastel yang menimpa .lisensi")

# --- Backup & Pulihkan mempertahankan penugasan wali ---
s, url, hasil = req(admin, f"/admin/kelas/{kw_id}/wali",
                    {"wali_id": str(gw1)})
m_bk = re.search(r"password sementara: (\S+)", hasil)
must(m_bk is not None, "AD: penunjukan ulang Guru Wali Satu menerbitkan password")
pw_ad5 = m_bk.group(1)
st, hdr, zipdata_ad = req_bytes(admin, "/admin/backup/unduh")
zf_ad = zipfile.ZipFile(io.BytesIO(zipdata_ad))
DBAD = "/tmp/absen-uji-ad.db"
with open(DBAD, "wb") as fh:
    fh.write(zf_ad.read("absen.db"))
must(dbqf(DBAD, "SELECT wali_id FROM classes WHERE nama='UJI-Kelas-Wali'") == gw1,
     "AD: backup (ZIP) memuat penugasan wali di classes.wali_id")
os.remove(DBAD)
DBPULIH_AD = "/tmp/absen-pulih-wali.db"
if os.path.exists(DBPULIH_AD):
    os.remove(DBPULIH_AD)
start_server(dbfile=DBPULIH_AD)
admin3, url, _ = login("admin", "admin123")
must("dashboard" in url, "AD: DB kosong untuk pulih: login admin awal berhasil")
s, url, hasil = unggah(admin3, "/admin/backup/pulihkan", "berkas",
                       "backup-wali.zip", zipdata_ad, "application/zip")
must("Backup dipulihkan. Silakan masuk lagi." in hasil,
     "AD: pulihkan backup berisi penugasan wali berhasil")
admin3, url, _ = login("admin", "admin123")
s, url, daftar_kelas_ad2 = req(admin3, "/admin/kelas")
chunk_kw2 = next((c for c in daftar_kelas_ad2.split("<tr>")
                  if "UJI-Kelas-Wali" in c), "")
must("Guru Wali Satu" in chunk_kw2,
     "AD: sesudah pulih, kolom Wali Kelas tetap memuat Guru Wali Satu")
wali6, url, _ = login("waliujikelaswali", pw_ad5)
must("dashboard" in url,
     "AD: sesudah pulih, login wali tetap berlaku dengan kredensial yang sama")
os.remove(DBPULIH_AD)

# ===========================================================================
# Fase AE — LOGIN WALI KELAS DENGAN MEMILIH KELAS (permintaan user):
# halaman login kini jelas punya DUA login — kartu "Login Admin"
# (username + password, tidak berubah) dan kartu "Login Wali Kelas"
# (wali MEMILIH kelasnya dari daftar kelas yang sudah punya wali, lalu
# memasukkan password; TANPA mengetik username). POST /login-wali
# menelusur kelas -> classes.wali_id -> user guru; berhasil hanya bila
# user itu aktif & password cocok; semua kegagalan memakai pesan
# generik "Kelas atau password salah." Kelas tanpa wali tidak
# ditawarkan & tidak bisa dipakai; sesudah wali dicabut jalurnya mati;
# tanpa wali sama sekali tampil catatan "Belum ada wali kelas yang
# ditunjuk admin." dengan pemilih nonaktif.
# ===========================================================================
start_server()  # kembali ke DB scratch utama (DBUJI)
admin, url, _ = login("admin", "admin123")
must("dashboard" in url,
     "AE: login admin (username + password) tidak berubah, tetap bisa")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Pilih"})
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Polos"})
s, url, form_siswa_ae = req(admin, "/admin/siswa")
kp_id = opt_val(form_siswa_ae, "UJI-Kelas-Pilih")
kpol_id = opt_val(form_siswa_ae, "UJI-Kelas-Polos")
must(kp_id and kpol_id, "AE: kelas UJI-Kelas-Pilih & UJI-Kelas-Polos dibuat")
req(admin, "/admin/guru", {"nama": "Guru Pilih Satu", "mapel": "UJI-PilihMapel"})
s, url, daftar_guru_ae = req(admin, "/admin/guru")
gp_id = id_dari_daftar(daftar_guru_ae, "/admin/guru", "<b>Guru Pilih Satu</b>")
must(gp_id, "AE: id Guru Pilih Satu terbaca")
s, url, hasil = req(admin, f"/admin/kelas/{kp_id}/wali",
                    {"wali_id": str(gp_id)})
m_ae = re.search(r"password sementara: (\S+)", hasil)
must(m_ae is not None, "AE: penunjukan wali menerbitkan password sementara")
pw_ae = m_ae.group(1)

# --- halaman login: dua kartu, pilihan hanya kelas yang sudah bervali ---
s, url, login_ae = req(client(), "/login")
must("<h2>Login Admin</h2>" in login_ae
     and "<h2>Login Wali Kelas</h2>" in login_ae,
     "AE: halaman login menampilkan DUA login: 'Login Admin' & "
     "'Login Wali Kelas'")
must('action="/login-wali"' in login_ae,
     "AE: kartu wali mengirim ke POST /login-wali")
must(opt_val(login_ae, "Wali Kelas UJI-Kelas-Pilih") == kp_id,
     "AE: pilihan wali memuat 'Wali Kelas UJI-Kelas-Pilih' dengan id kelasnya")
must("Wali Kelas UJI-Kelas-Polos" not in login_ae,
     "AE: kelas TANPA wali tidak ditawarkan di pilihan wali")
must(login_ae.count('type="password"') >= 2,
     "AE: kedua kartu punya kolom password (toggle mata app.js berlaku "
     "untuk keduanya)")

# --- password salah: gagal generik, tanpa sesi ---
salah = client()
s, url, hal = req(salah, "/login-wali",
                  {"kelas_id": str(kp_id), "password": "salah-total"})
must(urllib.parse.urlparse(url).path == "/login"
     and "Kelas atau password salah." in hal,
     "AE: password salah -> kembali ke /login dengan pesan generik")
s, url, hal = req(salah, "/dashboard")
must(urllib.parse.urlparse(url).path == "/login",
     "AE: login wali yang gagal tidak menyisakan sesi")

# --- kelas tanpa wali dipakai: gagal generik juga ---
s, url, hal = req(client(), "/login-wali",
                  {"kelas_id": str(kpol_id), "password": pw_ae})
must(urllib.parse.urlparse(url).path == "/login"
     and "Kelas atau password salah." in hal,
     "AE: kelas tanpa wali -> POST /login-wali gagal generik")

# --- berhasil: masuk sebagai wali dengan akses terbatas yang sama ---
wali_ae = client()
s, url, hal = req(wali_ae, "/login-wali",
                  {"kelas_id": str(kp_id), "password": pw_ae})
must("dashboard" in url,
     "AE: wali masuk hanya dengan memilih kelas + password (tanpa username)")
s, url, dash_ae = req(wali_ae, "/dashboard")
must("Scan Gerbang" not in dash_ae and "Tabel Guru" not in dash_ae
     and 'href="/tabel-siswa"' in dash_ae,
     "AE: sesi dari login pilih-kelas = sesi wali terbatas yang sama")
s, url, menu_ae = req(wali_ae, "/menu")
must("Wali Kelas UJI-Kelas-Pilih" in menu_ae,
     "AE: label sesi terbaca 'Wali Kelas UJI-Kelas-Pilih'")

# --- wali dicabut: pilihan hilang & jalur login mati ---
s, url, hasil = req(admin, f"/admin/kelas/{kp_id}/wali",
                    {"wali_id": ""})
must("dicabut" in hasil, "AE: wali UJI-Kelas-Pilih dicabut untuk uji jalur mati")
s, url, login_ae2 = req(client(), "/login")
must("Wali Kelas UJI-Kelas-Pilih" not in login_ae2,
     "AE: sesudah dicabut, kelas tidak lagi ditawarkan di pilihan wali")
s, url, hal = req(client(), "/login-wali",
                  {"kelas_id": str(kp_id), "password": pw_ae})
must(urllib.parse.urlparse(url).path == "/login"
     and "Kelas atau password salah." in hal,
     "AE: sesudah dicabut, POST /login-wali kelas itu gagal generik")

# --- tanpa wali sama sekali: catatan + pemilih nonaktif (DB segar) ---
DBPILIH = "/tmp/absen-pilih.db"
if os.path.exists(DBPILIH):
    os.remove(DBPILIH)
start_server(dbfile=DBPILIH)
s, url, login_kosong = req(client(), "/login")
must("Belum ada wali kelas yang ditunjuk admin." in login_kosong,
     "AE: tanpa wali: catatan 'Belum ada wali kelas yang ditunjuk admin.' tampil")
must('<select name="kelas_id" disabled>' in login_kosong,
     "AE: tanpa wali: pemilih kelas nonaktif (disabled)")
must("<h2>Login Admin</h2>" in login_kosong
     and "<h2>Login Wali Kelas</h2>" in login_kosong,
     "AE: tanpa wali pun kedua judul login tetap tampil")
s, url, hal = req(client(), "/login-wali",
                  {"kelas_id": "1", "password": "apa-saja"})
must(urllib.parse.urlparse(url).path == "/login"
     and "Kelas atau password salah." in hal,
     "AE: tanpa wali: POST /login-wali selalu gagal generik")
os.remove(DBPILIH)

# ===========================================================================
# Fase AF — PASSWORD WALI SELALU TERLIHAT ADMIN DI SEBELAH USERNAME
# (permintaan user: "password wali kelas buat di sebelah username aja",
# lalu ditegaskan: "ketika wali kelas ubah passwordnya, yg di sebelah
# username jga berubah sesuai password yg di ubah wali kelas").
# Password guru yang SEDANG berlaku disimpan teks biasa di
# users.pass_sementara KHUSUS sebagai kolom tampilan admin: diisi saat
# penunjukan wali (password acak), saat admin menekan "Buat Password
# Baru", dan saat wali mengganti passwordnya sendiri lewat
# /ganti-password (hanya akun guru; password admin tidak pernah
# disimpan di situ). Tabel Kelola Guru menampilkan password itu pada
# kolom Password (tanpa username dan tanpa lencana wali), selalu
# sinkron; halaman Edit Guru menampilkan info yang sama di dekat
# username. Penugasan
# dicabut/diganti -> nilai dikosongkan & baris PW hilang; guru bukan
# wali tetap "—" tanpa baris PW. Hash PBKDF2 tetap satu-satunya dasar
# pemeriksaan login, dan nilai tampilan ini ikut tersimpan & pulih
# lewat Backup & Pulihkan.
# ===========================================================================
start_server()  # kembali ke DB scratch utama (DBUJI)
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-PW"})
s, url, form_siswa_af = req(admin, "/admin/siswa")
kpw_id = opt_val(form_siswa_af, "UJI-Kelas-PW")
must(kpw_id, "AF: kelas UJI-Kelas-PW dibuat")
req(admin, "/admin/guru", {"nama": "Guru PW Satu", "mapel": "UJI-PWMapel"})
req(admin, "/admin/guru", {"nama": "Guru PW Bebas", "mapel": "UJI-PWMapel"})
s, url, daftar_guru_af = req(admin, "/admin/guru")
gpw = id_dari_daftar(daftar_guru_af, "/admin/guru", "<b>Guru PW Satu</b>")
gpb = id_dari_daftar(daftar_guru_af, "/admin/guru", "<b>Guru PW Bebas</b>")
must(gpw and gpb, "AF: id kedua guru terbaca dari daftar")

# --- guru baru (belum wali): tanpa baris PW, kolom tampilan kosong ---
chunk_bebas = next((c for c in daftar_guru_af.split("<tr>")
                    if "<b>Guru PW Bebas</b>" in c), "")
must("<td>&mdash;</td>" in chunk_bebas and "PW:" not in chunk_bebas,
     "AF: guru bukan wali: sel Password tetap '—' tanpa password tampil")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == "",
     "AF: guru baru: pass_sementara kosong di DB")

# --- penunjukan: password flash tampil juga di kolom Password ---
s, url, hasil = req(admin, f"/admin/kelas/{kpw_id}/wali",
                    {"wali_id": str(gpw)})
m_af = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_af is not None, "AF: penunjukan menerbitkan kredensial (flash tetap ada)")
pw_af1 = m_af.group(2)
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == pw_af1,
     "AF: DB: pass_sementara = password dari flash penunjukan")
s, url, daftar_guru_af = req(admin, "/admin/guru")
chunk_pw = next((c for c in daftar_guru_af.split("<tr>")
                 if "<b>Guru PW Satu</b>" in c), "")
must(f"<code>{pw_af1}</code>" in chunk_pw,
     "AF: Kelola Guru: kolom Password menampilkan password flash penunjukan")
s, url, edit_pw = req(admin, f"/admin/guru/{gpw}/edit")
must(f"PW: {pw_af1}" in edit_pw,
     "AF: halaman Edit Guru menampilkan PW yang sama di dekat username")

# --- "Buat Password Baru": tampilan ikut berganti, yang lama hilang ---
s, url, hasil = req(admin, f"/admin/guru/{gpw}/password-baru", {})
m_af2 = re.search(r"Password baru untuk .*: (\S+) \(sampaikan", hasil)
must(m_af2 is not None,
     "AF: 'Buat Password Baru' menerbitkan password (flash tetap ada)")
pw_af2 = m_af2.group(1)
must(pw_af2 != pw_af1, "AF: password baru berbeda dari sebelumnya")
s, url, daftar_guru_af = req(admin, "/admin/guru")
chunk_pw = next((c for c in daftar_guru_af.split("<tr>")
                 if "<b>Guru PW Satu</b>" in c), "")
must(f"<code>{pw_af2}</code>" in chunk_pw and pw_af1 not in chunk_pw,
     "AF: sesudah 'Buat Password Baru', kolom Password = password BARU (lama hilang)")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == pw_af2,
     "AF: DB: pass_sementara diperbarui oleh 'Buat Password Baru'")

# --- wali ganti password SENDIRI: tampilan ikut password pilihannya ---
wali_af = client()
s, url, hal = req(wali_af, "/login-wali",
                  {"kelas_id": str(kpw_id), "password": pw_af2})
must("dashboard" in url, "AF: wali login lewat pilih kelas dengan password terakhir")
s, url, hal = req(wali_af, "/ganti-password", {"password": "PilihanWali#9"})
must("Password berhasil diganti." in hal,
     "AF: wali mengganti passwordnya sendiri lewat Ganti Password")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == "PilihanWali#9",
     "AF: DB: pass_sementara kini = password PILIHAN wali sendiri")
s, url, daftar_guru_af = req(admin, "/admin/guru")
chunk_pw = next((c for c in daftar_guru_af.split("<tr>")
                 if "<b>Guru PW Satu</b>" in c), "")
must("<code>PilihanWali#9</code>" in chunk_pw and pw_af2 not in chunk_pw,
     "AF: Kelola Guru: kolom Password menampilkan password BARU pilihan wali (yang lama hilang)")
s, url, edit_pw = req(admin, f"/admin/guru/{gpw}/edit")
must("PW: PilihanWali#9" in edit_pw,
     "AF: halaman Edit Guru ikut menampilkan password pilihan wali")
# password pilihan wali bisa login; password terbitan admin sudah mati
wali_af2 = client()
s, url, hal = req(wali_af2, "/login-wali",
                  {"kelas_id": str(kpw_id), "password": "PilihanWali#9"})
must("dashboard" in url,
     "AF: login /login-wali dengan password pilihan wali berhasil")
s, url, hal = req(client(), "/login-wali",
                  {"kelas_id": str(kpw_id), "password": pw_af2})
must(urllib.parse.urlparse(url).path == "/login"
     and "Kelas atau password salah." in hal,
     "AF: password terbitan admin mati sesudah wali mengganti password")

# --- ganti password ADMIN tidak menyimpan apa pun di kolom tampilan ---
admin_id_af = dbq("SELECT id FROM users WHERE role='admin'")
s, url, hal = req(admin, "/ganti-password", {"password": "AdminPilihan#7"})
must("Password berhasil diganti." in hal, "AF: admin mengganti passwordnya sendiri")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (admin_id_af,)) == "",
     "AF: pass_sementara ADMIN tetap kosong (password admin tidak disimpan)")
admin, url, _ = login("admin", "AdminPilihan#7")
must("dashboard" in url, "AF: admin login lagi dengan password barunya")
s, url, hal = req(admin, "/ganti-password", {"password": "admin123"})
must("Password berhasil diganti." in hal,
     "AF: password admin dikembalikan ke semula untuk fase berikutnya")
admin, url, _ = login("admin", "admin123")
must("dashboard" in url, "AF: admin kembali memakai password awal")

# --- Backup & Pulihkan mempertahankan password tampilan ---
s, url, hasil = req(admin, f"/admin/guru/{gpw}/password-baru", {})
m_af3 = re.search(r"Password baru untuk .*: (\S+) \(sampaikan", hasil)
must(m_af3 is not None, "AF: password baru diterbitkan lagi untuk uji backup")
pw_af3 = m_af3.group(1)
st, hdr, zipdata_af = req_bytes(admin, "/admin/backup/unduh")
zf_af = zipfile.ZipFile(io.BytesIO(zipdata_af))
DBAF = "/tmp/absen-uji-af.db"
with open(DBAF, "wb") as fh:
    fh.write(zf_af.read("absen.db"))
must(dbqf(DBAF, "SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == pw_af3,
     "AF: backup (ZIP) memuat pass_sementara di DB")
os.remove(DBAF)
DBPULIH_AF = "/tmp/absen-pulih-pw.db"
if os.path.exists(DBPULIH_AF):
    os.remove(DBPULIH_AF)
start_server(dbfile=DBPULIH_AF)
admin_af, url, _ = login("admin", "admin123")
must("dashboard" in url, "AF: DB kosong untuk pulih: login admin awal berhasil")
s, url, hasil = unggah(admin_af, "/admin/backup/pulihkan", "berkas",
                       "backup-pw.zip", zipdata_af, "application/zip")
must("Backup dipulihkan. Silakan masuk lagi." in hasil,
     "AF: pulihkan backup berisi pass_sementara berhasil")
admin_af, url, _ = login("admin", "admin123")
s, url, daftar_guru_af2 = req(admin_af, "/admin/guru")
chunk_pw2 = next((c for c in daftar_guru_af2.split("<tr>")
                  if "<b>Guru PW Satu</b>" in c), "")
must(f"<code>{pw_af3}</code>" in chunk_pw2,
     "AF: sesudah pulih, Kelola Guru tetap menampilkan password yang sama")
wali_af3 = client()
s, url, hal = req(wali_af3, "/login-wali",
                  {"kelas_id": str(kpw_id), "password": pw_af3})
must("dashboard" in url,
     "AF: sesudah pulih, wali login dengan password tampilan yang sama")
os.remove(DBPULIH_AF)

# --- penugasan dicabut: baris PW hilang & nilai dibersihkan ---
start_server()  # kembali ke DB scratch utama (DBUJI)
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, f"/admin/kelas/{kpw_id}/wali",
                    {"wali_id": ""})
must("dicabut" in hasil, "AF: penugasan wali UJI-Kelas-PW dicabut")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpw,)) == "",
     "AF: DB: pass_sementara dibersihkan saat penugasan dicabut")
s, url, daftar_guru_af = req(admin, "/admin/guru")
chunk_pw = next((c for c in daftar_guru_af.split("<tr>")
                 if "<b>Guru PW Satu</b>" in c), "")
must("<td>&mdash;</td>" in chunk_pw and "PW:" not in chunk_pw,
     "AF: sesudah dicabut, password hilang dari kolom Password (kembali '—')")

# --- kelas dihapus: penugasan berakhir, password tampilan dibersihkan ---
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-PW2"})
s, url, form_siswa_af = req(admin, "/admin/siswa")
kpw2_id = opt_val(form_siswa_af, "UJI-Kelas-PW2")
must(kpw2_id, "AF: kelas UJI-Kelas-PW2 dibuat")
s, url, hasil = req(admin, f"/admin/kelas/{kpw2_id}/wali",
                    {"wali_id": str(gpb)})
m_af4 = re.search(r"password sementara: (\S+)", hasil)
must(m_af4 is not None, "AF: Guru PW Bebas ditunjuk wali UJI-Kelas-PW2")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpb,)) == m_af4.group(1),
     "AF: DB: pass_sementara Guru PW Bebas terisi sesudah penunjukan")
s, url, hasil = req(admin, f"/admin/kelas/{kpw2_id}/hapus", {})
must("Kelas dihapus." in hasil, "AF: kelas UJI-Kelas-PW2 (tanpa siswa) dihapus")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gpb,)) == "",
     "AF: DB: pass_sementara dibersihkan saat kelas walinya dihapus")
s, url, daftar_guru_af = req(admin, "/admin/guru")
chunk_bebas = next((c for c in daftar_guru_af.split("<tr>")
                    if "<b>Guru PW Bebas</b>" in c), "")
must("PW:" not in chunk_bebas,
     "AF: sesudah kelasnya dihapus, Guru PW Bebas tanpa baris PW lagi")

# ===========================================================================
# Fase AG — WALI KELAS DIPILIH LANGSUNG DARI KOLOM TABEL (permintaan
# user: "di kolom ini wali kelas nya dibuat jadi pilihan nama guru aja,
# jadi ga usah buat wali kelas di kelas lagi"): kolom Wali Kelas pada
# Kelola Kelas adalah <select> nama guru yang terkirim otomatis saat
# diganti (POST /admin/kelas/<id>/wali, field wali_id; "" = cabut).
# Penunjukan/penggantian/pencabutan memakai mesin yang sama persis
# (_wali_dari_form + _tetapkan_wali): flash kredensial + baris "PW:"
# sinkron, login lama mati & pass_sementara lama dibersihkan, pilihan
# yang sama = no-op, wali kelas lain DITOLAK, guru nonaktif DITOLAK,
# dan sesi wali DIBLOKIR dari endpoint ini (khusus admin).
# ===========================================================================
start_server()  # waktu nyata: data khas fase ini lewat alur admin
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Sebaris-1"})
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Sebaris-2"})
s, url, form_siswa_ag = req(admin, "/admin/siswa")
ki1 = opt_val(form_siswa_ag, "UJI-Kelas-Sebaris-1")
ki2 = opt_val(form_siswa_ag, "UJI-Kelas-Sebaris-2")
must(ki1 and ki2, "AG: kelas UJI-Kelas-Sebaris-1 & -2 dibuat")
req(admin, "/admin/guru", {"nama": "Guru Sebaris Satu", "mapel": "UJI-SebarisMapel"})
req(admin, "/admin/guru", {"nama": "Guru Sebaris Dua", "mapel": "UJI-SebarisMapel"})
req(admin, "/admin/guru", {"nama": "Guru Sebaris Nonaktif", "mapel": "UJI-SebarisMapel"})
s, url, daftar_guru_ag = req(admin, "/admin/guru")
gi1 = id_dari_daftar(daftar_guru_ag, "/admin/guru", "<b>Guru Sebaris Satu</b>")
gi2 = id_dari_daftar(daftar_guru_ag, "/admin/guru", "<b>Guru Sebaris Dua</b>")
gina = id_dari_daftar(daftar_guru_ag, "/admin/guru", "<b>Guru Sebaris Nonaktif</b>")
must(gi1 and gi2 and gina, "AG: id ketiga guru terbaca")

# --- rendering: satu form dropdown per baris, "— Belum ada —" terpilih ---
s, url, daftar_kelas_ag = req(admin, "/admin/kelas")
chunk_i1 = next((c for c in daftar_kelas_ag.split("<tr>")
                 if "UJI-Kelas-Sebaris-1" in c), "")
chunk_i2 = next((c for c in daftar_kelas_ag.split("<tr>")
                 if "UJI-Kelas-Sebaris-2" in c), "")
for chunk, kid, ket in ((chunk_i1, ki1, "1"), (chunk_i2, ki2, "2")):
    must(f'action="/admin/kelas/{kid}/wali"' in chunk
         and '<select name="wali_id"' in chunk
         and 'onchange="this.form.submit()"' in chunk,
         f"AG: baris Sebaris-{ket}: form dropdown wali sebaris "
         "(terkirim otomatis saat diganti)")
    must('<option value="" selected>' in chunk,
         f"AG: baris Sebaris-{ket}: '— Belum ada —' (value='') terpilih "
         "selama belum ada wali")
    must("Guru Sebaris Satu" in chunk and "Guru Sebaris Dua" in chunk,
         f"AG: baris Sebaris-{ket}: dropdown memuat guru yang bebas")

# --- penunjukan lewat endpoint sebaris: flash + baris PW sinkron ---
s, url, hasil = req(admin, f"/admin/kelas/{ki1}/wali", {"wali_id": str(gi1)})
m_ag = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_ag is not None and m_ag.group(1) == "waliujikelassebaris1",
     "AG: penunjukan sebaris menerbitkan username waliujikelassebaris1 "
     "+ password acak")
pw_ag1 = m_ag.group(2)
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki1,)) == gi1,
     "AG: DB: wali Sebaris-1 = Guru Sebaris Satu")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gi1,)) == pw_ag1,
     "AG: DB: pass_sementara = password dari flash penunjukan sebaris")
s, url, daftar_guru_ag = req(admin, "/admin/guru")
chunk_gi1 = next((c for c in daftar_guru_ag.split("<tr>")
                  if "<b>Guru Sebaris Satu</b>" in c), "")
must(f"<code>{pw_ag1}</code>" in chunk_gi1,
     "AG: Kelola Guru: kolom Password menampilkan password penunjukan sebaris")

# --- rendering sesudah penunjukan: terpilih di barisnya, tersisih di lain ---
s, url, daftar_kelas_ag = req(admin, "/admin/kelas")
chunk_i1 = next((c for c in daftar_kelas_ag.split("<tr>")
                 if "UJI-Kelas-Sebaris-1" in c), "")
chunk_i2 = next((c for c in daftar_kelas_ag.split("<tr>")
                 if "UJI-Kelas-Sebaris-2" in c), "")
must(re.search(r'<option value="%d" selected>Guru Sebaris Satu' % gi1,
               chunk_i1) is not None,
     "AG: dropdown baris Sebaris-1: wali saat ini TERPILIH (selected)")
must("Guru Sebaris Satu" not in chunk_i2,
     "AG: dropdown baris Sebaris-2: wali kelas lain TIDAK ditawarkan")
must("Guru Sebaris Dua" in chunk_i2,
     "AG: dropdown baris Sebaris-2: guru bebas tetap ditawarkan")

# --- login wali dengan password terbitan alur sebaris ---
wali_ag = client()
s, url, hal = req(wali_ag, "/login-wali",
                  {"kelas_id": str(ki1), "password": pw_ag1})
must("dashboard" in url,
     "AG: /login-wali berhasil dengan password terbitan dropdown sebaris")

# --- pilihan yang sama = no-op: password TIDAK diterbitkan ulang ---
s, url, hasil = req(admin, f"/admin/kelas/{ki1}/wali", {"wali_id": str(gi1)})
must("password sementara" not in hasil,
     "AG: memilih wali yang sama: tidak ada password baru (no-op)")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gi1,)) == pw_ag1,
     "AG: pass_sementara tidak berubah pada pilihan yang sama")

# --- penolakan: guru yang sudah wali kelas lain tidak bisa diambil ---
s, url, hasil = req(admin, f"/admin/kelas/{ki2}/wali", {"wali_id": str(gi1)})
must("sudah menjadi wali kelas" in hasil,
     "AG: menunjuk wali kelas lain ke kelas ini -> DITOLAK dengan pesan salah")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki2,)) is None,
     "AG: DB: Sebaris-2 tetap tanpa wali sesudah penolakan")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki1,)) == gi1,
     "AG: DB: penugasan Sebaris-1 tidak terganggu oleh penolakan itu")

# --- penolakan: guru nonaktif tidak bisa ditunjuk ---
# (guru diberi riwayat dulu lewat catatan manual agar Hapus berarti
# menonaktifkan, bukan menghapus permanen)
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": "2026-11-26", "tipe": "guru", "id": gina,
                     "status": "Sakit", "jam": ""})
must(dbq("SELECT status FROM kehadiran WHERE tanggal='2026-11-26' "
         "AND ref_type='guru' AND ref_id=?", (gina,)) == "Sakit",
     "AG: catatan manual Sakit untuk guru tersimpan (prasyarat nonaktif)")
s, url, hasil = req(admin, f"/admin/guru/{gina}/hapus", {})
must("dinonaktifkan" in hasil,
     "AG: guru ber-riwayat dinonaktifkan lewat tombol Hapus")
s, url, hasil = req(admin, f"/admin/kelas/{ki2}/wali", {"wali_id": str(gina)})
must("Guru nonaktif tidak bisa ditunjuk" in hasil,
     "AG: menunjuk guru nonaktif lewat dropdown -> DITOLAK")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki2,)) is None,
     "AG: DB: Sebaris-2 tetap tanpa wali sesudah penolakan guru nonaktif")
s, url, hasil = req(admin, f"/admin/guru/{gina}/aktifkan", {})
must("diaktifkan kembali" in hasil, "AG: guru diaktifkan lagi (pembersihan)")

# --- penggantian lewat dropdown: login lama mati, PW lama dibersihkan ---
s, url, hasil = req(admin, f"/admin/kelas/{ki1}/wali", {"wali_id": str(gi2)})
m_ag2 = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_ag2 is not None and "Guru Sebaris Dua" in hasil,
     "AG: penggantian sebaris menerbitkan kredensial wali baru")
pw_ag2 = m_ag2.group(2)
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gi1,)) == "",
     "AG: DB: pass_sementara wali LAMA dibersihkan saat diganti")
must(dbq("SELECT username FROM users WHERE id=?", (gi1,)) == f"guru-{gi1}",
     "AG: username wali lama diparkir ke 'guru-<id>'")
g_ag_old, url, body = login("waliujikelassebaris1", pw_ag1)
must("dashboard" not in url,
     "AG: login wali LAMA mati sesudah penggantian sebaris")
wali_ag2 = client()
s, url, hal = req(wali_ag2, "/login-wali",
                  {"kelas_id": str(ki1), "password": pw_ag2})
must("dashboard" in url,
     "AG: wali BARU bisa login (pilih kelas) sesudah penggantian sebaris")

# --- sesi wali DIBLOKIR dari endpoint sebaris (khusus admin) ---
try:
    req(wali_ag2, f"/admin/kelas/{ki2}/wali", {"wali_id": str(gi1)})
    must(False, "AG: wali POST endpoint sebaris harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "AG: wali POST /admin/kelas/<id>/wali -> 404 (khusus admin)")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki2,)) is None,
     "AG: POST oleh wali tidak mengubah penugasan apa pun")

# --- pencabutan lewat dropdown (wali_id kosong): login mati, PW bersih ---
s, url, hasil = req(admin, f"/admin/kelas/{ki1}/wali", {"wali_id": ""})
must("dicabut" in hasil,
     "AG: memilih '— Belum ada —' mencabut wali (flash konfirmasi)")
must(dbq("SELECT wali_id FROM classes WHERE id=?", (ki1,)) is None,
     "AG: DB: wali_id NULL sesudah pencabutan sebaris")
must(dbq("SELECT pass_sementara FROM users WHERE id=?", (gi2,)) == "",
     "AG: DB: pass_sementara dibersihkan sesudah pencabutan sebaris")
g_ag_old2, url, body = login("waliujikelassebaris1", pw_ag2)
must("dashboard" not in url,
     "AG: login wali mati sesudah pencabutan sebaris")

# ===========================================================================
# Fase AH — UNDUH EXCEL DETAIL SISWA (permintaan user: "buat detail
# siswa ini bisa di donlot ya"): tombol "Unduh Excel" pada /siswa/<id>
# mengarah ke GET /siswa/<id>/unduh yang mengembalikan .xlsx dua
# lembar — "Rekap 6 Bulan" (kepala nama sekolah + judul "Detail
# Absensi Siswa" + identitas Nama/NIS/Kelas, tabel rekap persis
# halaman termasuk baris Total) dan "Rincian Kehadiran"
# (Tanggal/Status/Jam persis halaman) — dibangun dari data yang sama
# dengan halamannya (_data_detail_siswa). Izin unduh menyamai izin
# membuka halaman detail. Skenario meniru Fase AC: scan Terlambat
# 14 Sep 2026, scan Hadir 25 Nov 2026, Sakit manual 24 Nov (+ Alpa
# otomatis 23 Nov dari tabel mingguan).
# ===========================================================================
start_server()  # waktu nyata: siapkan data khas fase ini
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Unduh"})
s, url, form_siswa_ah = req(admin, "/admin/siswa")
kdl = opt_val(form_siswa_ah, "UJI-Kelas-Unduh")
must(kdl, "AH: kelas UJI-Kelas-Unduh dibuat")
req(admin, "/admin/siswa", {"nama": "Siswa Unduh Uji", "kode": "UJI-DL1",
                            "class_id": kdl})
dl_id = dbq("SELECT id FROM students WHERE kode='UJI-DL1'")
must(dl_id, "AH: id Siswa Unduh Uji terbaca")
start_server("2026-09-14T07:30:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-DL1"})
must("Terlambat" in hasil, "AH: scan Siswa Unduh Uji 14 Sep 07:30 -> Terlambat")
start_server("2026-11-25T06:45:00")
admin, url, _ = login("admin", "admin123")
s, url, hasil = req(admin, "/api/scan", {"kode": "UJI-DL1"})
must("Hadir" in hasil, "AH: scan Siswa Unduh Uji 25 Nov 06:45 -> Hadir")
s, url, hasil = req(admin, "/kehadiran/edit",
                    {"tanggal": "2026-11-24", "tipe": "siswa", "id": dl_id,
                     "status": "Sakit", "jam": ""})
must("ditandai Sakit" in hasil, "AH: Siswa Unduh Uji ditandai Sakit 24 Nov")

# --- tombol pada halaman detail ---
s, url, det_ah = req(admin, f"/siswa/{dl_id}")
must(f'href="/siswa/{dl_id}/unduh"' in det_ah and "Unduh Excel" in det_ah,
     "AH: halaman detail memuat tombol 'Unduh Excel' ke /siswa/<id>/unduh")

# --- unduhan: 200 + tipe xlsx + nama berkas ---
st, hdr, xdata = req_bytes(admin, f"/siswa/{dl_id}/unduh")
must(st == 200, "AH: unduhan detail siswa berstatus 200")
must(hdr.get_content_type()
     == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "AH: unduhan bertipe konten .xlsx (spreadsheetml)")
disp = hdr.get("Content-Disposition", "")
must("Detail-Siswa-" in disp and "UJI-DL1" in disp
     and disp.endswith('.xlsx"'),
     "AH: nama berkas unduhan Detail-Siswa-<Nama>-<NIS>.xlsx")

# --- isi berkas: persis halaman (dibaca balik dengan openpyxl) ---
from openpyxl import load_workbook
wb_ah = load_workbook(io.BytesIO(xdata))
must(wb_ah.sheetnames == ["Rekap 6 Bulan", "Rincian Kehadiran"],
     "AH: berkas punya dua lembar: 'Rekap 6 Bulan' & 'Rincian Kehadiran'")
ws1 = wb_ah["Rekap 6 Bulan"]
must(ws1["A1"].value
     == dbq("SELECT value FROM settings WHERE key='nama_sekolah'"),
     "AH: lembar rekap memuat nama sekolah dari pengaturan pada kepala")
must(ws1["A2"].value == "Detail Absensi Siswa",
     "AH: lembar rekap: judul 'Detail Absensi Siswa'")
must(ws1["A3"].value == "Nama" and ws1["B3"].value == "Siswa Unduh Uji",
     "AH: identitas Nama pada berkas")
must(ws1["A4"].value == "NIS" and ws1["B4"].value == "UJI-DL1",
     "AH: identitas NIS pada berkas")
must(ws1["A5"].value == "Kelas" and ws1["B5"].value == "UJI-Kelas-Unduh",
     "AH: identitas Kelas pada berkas")
kepala1 = [ws1.cell(row=7, column=j).value for j in range(1, 7)]
must(kepala1 == ["Bulan", "Hadir", "Terlambat", "Sakit", "Izin", "Alpa"],
     "AH: kepala tabel rekap persis halaman")


def baris_rekap_xlsx(ws, label):
    for r in range(8, 14):
        if ws.cell(row=r, column=1).value == label:
            return tuple(ws.cell(row=r, column=j).value
                         for j in range(2, 7))
    return None


must(baris_rekap_xlsx(ws1, "September 2026") == (0, 1, 0, 0, 0),
     "AH: rekap September 2026 pada berkas: Terlambat 1 (sama dgn halaman)")
must(baris_rekap_xlsx(ws1, "November 2026") == (1, 0, 1, 0, 1),
     "AH: rekap November 2026 pada berkas: Hadir 1, Sakit 1, Alpa 1")
must(ws1.cell(row=14, column=1).value == "Total"
     and tuple(ws1.cell(row=14, column=j).value for j in range(2, 7))
     == (1, 1, 1, 0, 1),
     "AH: baris Total pada berkas: Hadir 1, Terlambat 1, Sakit 1, "
     "Izin 0, Alpa 1")
ws2 = wb_ah["Rincian Kehadiran"]
kepala2 = [ws2.cell(row=1, column=j).value for j in range(1, 4)]
must(kepala2 == ["Tanggal", "Status", "Jam"],
     "AH: kepala tabel rincian persis halaman")
baris_rincian = [tuple(ws2.cell(row=r, column=j).value for j in range(1, 4))
                 for r in range(2, 6)]
must(baris_rincian == [("Rabu, 25 November 2026", "Hadir", "06:45"),
                       ("Selasa, 24 November 2026", "Sakit", "—"),
                       ("Senin, 23 November 2026", "Alpa", "—"),
                       ("Senin, 14 September 2026", "Terlambat", "07:30")],
     "AH: baris rincian pada berkas persis halaman (terbaru dulu, jam "
     "HH:MM untuk scan, '—' untuk Sakit/Alpa)")
must(ws2.cell(row=6, column=1).value is None,
     "AH: rincian pada berkas tepat 4 baris (tidak lebih)")

# --- izin unduh menyamai izin halaman detail ---
guru_ah, url, _ = login(WALI_G1_USER, WALI_G1_PW)
s, url, hal = req(guru_ah, f"/siswa/{dl_id}/unduh")
must(urllib.parse.urlparse(url).path == "/dashboard"
     and "Data tidak ditemukan." in hal,
     "AH: wali kelas lain DIBLOKIR mengunduh detail siswa ini")
st, hdr, xdata_w = req_bytes(guru_ah, f"/siswa/{A_id}/unduh")
must(st == 200
     and hdr.get_content_type()
     == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "AH: wali BISA mengunduh detail siswa kelasnya sendiri")
anon_ah = client()
s, url, hal = req(anon_ah, f"/siswa/{dl_id}/unduh")
must(urllib.parse.urlparse(url).path == "/login",
     "AH: anonim mengunduh -> diarahkan ke /login")
s, url, hasil = req(admin, "/siswa/99999/unduh")
must("Data tidak ditemukan." in hasil,
     "AH: unduh id tak dikenal -> 'Data tidak ditemukan.'")

# ===========================================================================
# Fase AI — KOLOM USERNAME DI KELOLA GURU DIGANTI KOLOM PASSWORD
# (permintaan user: "username ganti password aja, trus wali1a dan
# wali 1a nya hapus aja" — ditegaskan: TAMPILAN SAJA). Kepala kolom
# Kelola Guru kini "Password"; baris guru yang sedang jadi wali hanya
# menampilkan password yang sedang berlaku (pass_sementara), tanpa
# username, tanpa lencana "Wali <kelas>", tanpa awalan "PW:"; guru
# bukan wali tetap "—". Murni tampilan: login wali lewat username
# (cadangan) dan lewat /login-wali (pilih kelas) tidak berubah.
# ===========================================================================
start_server()  # kembali ke DB scratch utama (DBUJI)
admin, url, _ = login("admin", "admin123")
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-KolomPW"})
s, url, form_siswa_ai = req(admin, "/admin/siswa")
kkp_id = opt_val(form_siswa_ai, "UJI-Kelas-KolomPW")
must(kkp_id, "AI: kelas UJI-Kelas-KolomPW dibuat")
req(admin, "/admin/guru", {"nama": "Guru Kolom Wali", "mapel": "UJI-KolomMapel"})
req(admin, "/admin/guru", {"nama": "Guru Kolom Bebas", "mapel": "UJI-KolomMapel"})
s, url, daftar_guru_ai = req(admin, "/admin/guru")
gkw = id_dari_daftar(daftar_guru_ai, "/admin/guru", "<b>Guru Kolom Wali</b>")
gkb = id_dari_daftar(daftar_guru_ai, "/admin/guru", "<b>Guru Kolom Bebas</b>")
must(gkw and gkb, "AI: id kedua guru terbaca dari daftar")
s, url, hasil = req(admin, f"/admin/kelas/{kkp_id}/wali",
                    {"wali_id": str(gkw)})
m_ai = re.search(r"username (wali[^\s,]+), password sementara: (\S+)", hasil)
must(m_ai is not None, "AI: penunjukan wali menerbitkan kredensial (flash)")
user_ai, pw_ai = m_ai.group(1), m_ai.group(2)

# --- (1) kepala kolom: Password ada, Username hilang dari tabel guru ---
s, url, daftar_guru_ai = req(admin, "/admin/guru")
must("<th>Password</th>" in daftar_guru_ai
     and "<th>Username</th>" not in daftar_guru_ai,
     "AI: Kelola Guru: kepala kolom kini 'Password', bukan 'Username'")

# --- (2) baris wali: hanya password; tanpa username & tanpa lencana ---
chunk_wali_ai = next((c for c in daftar_guru_ai.split("<tr>")
                      if "<b>Guru Kolom Wali</b>" in c), "")
must(f"<code>{pw_ai}</code>" in chunk_wali_ai,
     "AI: baris wali menampilkan password yang berlaku di kolom Password")
must(user_ai not in chunk_wali_ai and "wali-badge" not in chunk_wali_ai
     and "PW:" not in chunk_wali_ai,
     "AI: baris wali TANPA username, TANPA lencana wali, TANPA awalan 'PW:'")
must("wali-badge" not in daftar_guru_ai,
     "AI: seluruh tabel Kelola Guru bebas markup lencana wali")

# --- (3) guru bukan wali: sel kolom Password tetap tanda pisah ---
chunk_bebas_ai = next((c for c in daftar_guru_ai.split("<tr>")
                       if "<b>Guru Kolom Bebas</b>" in c), "")
must("<td>&mdash;</td>" in chunk_bebas_ai,
     "AI: guru bukan wali: sel kolom Password tetap '—'")

# --- (4) regresi login: username cadangan & pilih kelas tetap jalan ---
wali_ai, url, _ = login(user_ai, pw_ai)
must("dashboard" in url,
     "AI: regresi: wali tetap bisa login lewat username (cadangan)")
wali_ai2 = client()
s, url, hal = req(wali_ai2, "/login-wali",
                  {"kelas_id": str(kkp_id), "password": pw_ai})
must("dashboard" in url,
     "AI: regresi: wali tetap bisa login lewat /login-wali (pilih kelas)")

# ===========================================================================
# Fase AJ — KOMPRESI GAMBAR UNGGAHAN + CACHE (perbaikan permanen latar
# lama muncul). Dua penyebab yang diverifikasi di layanan live: berkas
# wallpaper 3,1 MB (foto resolusi penuh) dan route penyajian gambar tanpa
# Cache-Control sehingga browser mengunduh ulang di setiap halaman.
# Kini: unggah wallpaper otomatis dikecilkan jadi JPEG <=1920 px; logo
# PNG ber-alpha tetap PNG <=512 px; /wallpaper/<nama> & /logo/<nama>
# mengirim "Cache-Control: public, max-age=604800, immutable" (nama
# berkas token acak berganti tiap unggahan, jadi aman di-cache lama).
# ===========================================================================
import random

start_server()  # DB scratch utama (DBUJI)
admin, url, _ = login("admin", "admin123")
anon = client()

# --- gambar uji besar: foto-noise 3000x2000 JPEG (±4 MB, di bawah batas) ---
random.seed(20261122)
_img_besar = PILImage.frombytes("RGB", (3000, 2000),
                                random.randbytes(3000 * 2000 * 3))
_buf = io.BytesIO()
_img_besar.save(_buf, "JPEG", quality=80)
data_foto_besar = _buf.getvalue()
must(len(data_foto_besar) > 1024 * 1024,
     f"AJ: gambar uji foto besar terbentuk ({len(data_foto_besar)} byte > 1 MB)")

# --- (1) unggah wallpaper besar -> tersimpan sebagai JPEG kecil <=1920 ---
s, url, hasil = unggah(admin, "/admin/tampilan/wallpaper", "wallpaper",
                       "foto-besar.jpg", data_foto_besar, "image/jpeg")
must("Wallpaper dipasang." in hasil, "AJ: unggah wallpaper besar berhasil")
nama_wp_aj = dbq("SELECT value FROM settings WHERE key='wallpaper'")
must(nama_wp_aj is not None
     and re.fullmatch(r"[0-9a-f]{24}\.(jpg|jpeg|png|webp)", nama_wp_aj)
     and nama_wp_aj.endswith(".jpg"),
     f"AJ: settings wallpaper = nama token .jpg yang lolos pola nama berkas: {nama_wp_aj}")
path_wp_aj = os.path.join(UPLOAD_DIR, nama_wp_aj)
must(os.path.isfile(path_wp_aj), "AJ: berkas wallpaper tersimpan ada di uploads/")
_img_wp_aj = PILImage.open(path_wp_aj)
_img_wp_aj.load()
must(_img_wp_aj.format == "JPEG"
     and _img_wp_aj.size[0] <= 1920 and _img_wp_aj.size[1] <= 1920,
     f"AJ: wallpaper tersimpan JPEG {_img_wp_aj.size} (kedua sisi <= 1920 px)")
ukuran_wp_aj = os.path.getsize(path_wp_aj)
must(ukuran_wp_aj * 2 < len(data_foto_besar),
     f"AJ: ukuran wallpaper menyusut drastis: {len(data_foto_besar)} -> "
     f"{ukuran_wp_aj} byte (kurang dari separuh unggahan)")

# --- (2) route wallpaper: konten benar + header Cache-Control ---
st, hdr, data = req_bytes(anon, f"/wallpaper/{nama_wp_aj}")
must(st == 200 and hdr.get_content_type() == "image/jpeg",
     "AJ: GET /wallpaper/<nama> -> 200 image/jpeg")
cc_wp = hdr.get("Cache-Control") or ""
must("max-age=604800" in cc_wp and "immutable" in cc_wp,
     f"AJ: /wallpaper mengirim Cache-Control immutable: {cc_wp}")
s, url, dash = req(admin, "/dashboard")
must(f"/wallpaper/{nama_wp_aj}" in dash,
     "AJ: dashboard mereferensikan wallpaper hasil kompresi sebagai latar")

# --- (3) logo PNG ber-alpha 800x600 -> tetap PNG ber-alpha, <=512 px ---
_img_logo_aj = PILImage.new("RGBA", (800, 600), (30, 120, 200, 128))
_buf = io.BytesIO()
_img_logo_aj.save(_buf, "PNG")
data_logo_aj = _buf.getvalue()
s, url, hasil = unggah(admin, "/admin/tampilan/logo", "logo",
                       "logo-alpha.png", data_logo_aj)
must("Logo dipasang." in hasil, "AJ: unggah logo PNG ber-alpha berhasil")
nama_logo_aj = dbq("SELECT value FROM settings WHERE key='logo'")
must(nama_logo_aj is not None and nama_logo_aj.endswith(".png"),
     f"AJ: settings logo tetap bernama .png: {nama_logo_aj}")
_img_logo_tersimpan = PILImage.open(os.path.join(UPLOAD_DIR, nama_logo_aj))
_img_logo_tersimpan.load()
must(_img_logo_tersimpan.format == "PNG"
     and _img_logo_tersimpan.mode == "RGBA"
     and _img_logo_tersimpan.size[0] <= 512
     and _img_logo_tersimpan.size[1] <= 512,
     f"AJ: logo tersimpan PNG ber-alpha {_img_logo_tersimpan.size} (<= 512 px)")
st, hdr, data = req_bytes(anon, f"/logo/{nama_logo_aj}")
cc_logo = hdr.get("Cache-Control") or ""
must(st == 200 and "max-age=604800" in cc_logo,
     f"AJ: /logo ikut mengirim Cache-Control immutable: {cc_logo}")

# --- bersihkan lagi agar keadaan akhir netral ---
s, url, hasil = req(admin, "/admin/tampilan/wallpaper/hapus", {})
must("Wallpaper dihapus." in hasil, "AJ: wallpaper uji dibersihkan")
s, url, hasil = req(admin, "/admin/tampilan/logo/hapus", {})
must("Logo dihapus." in hasil, "AJ: logo uji dibersihkan")

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
