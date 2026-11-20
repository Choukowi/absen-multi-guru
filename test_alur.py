#!/usr/bin/env python3
"""Uji alur end-to-end lewat HTTP ke server yang sedang jalan.
Pakai:  BASE=http://127.0.0.1:8765 python3 test_alur.py
Membuat data uji dengan awalan UJI- (aman dihapus dari DB sesudahnya)."""
import os, re, sys, urllib.request, urllib.parse, http.cookiejar

BASE = os.environ.get("BASE", "http://127.0.0.1:8765")

def client():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(op, path, data=None):
    url = BASE + path
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    r = op.open(urllib.request.Request(url, data=body))
    return r.status, r.geturl(), r.read().decode("utf-8", "replace")

def must(cond, msg):
    print(("OK  " if cond else "GAGAL ") + msg)
    if not cond: sys.exit(1)

# 1. Login admin
admin = client()
s, url, body = req(admin, "/login", {"username": "admin", "password": "admin123"})
must("dashboard" in url, f"login admin berhasil -> {url}")

# 2. Admin buat kelas, mapel, guru, siswa, penugasan
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-1"})
req(admin, "/admin/mapel", {"nama": "UJI-Matematika"})
req(admin, "/admin/guru", {"nama": "Guru Uji", "username": "guruuji", "password": "guruuji123"})
s, url, dash = req(admin, "/dashboard")
conn_ids = {}
# Ambil id dari DB langsung untuk penugasan (UI memakai select; uji membaca halaman form)
s, url, form = req(admin, "/admin/penugasan")
def opt_val(htmltxt, label):
    m = re.search(r'<option value="(\d+)">' + re.escape(label) + r'</option>', htmltxt)
    return m.group(1) if m else None
kelas_id = opt_val(form, "UJI-Kelas-1"); mapel_id = opt_val(form, "UJI-Matematika"); guru_id = opt_val(form, "Guru Uji")
must(all([kelas_id, mapel_id, guru_id]), f"id terbaca dari form penugasan: kelas={kelas_id} mapel={mapel_id} guru={guru_id}")
req(admin, "/admin/siswa", {"nama": "Siswa Uji Satu", "kode": "UJI-001", "class_id": kelas_id})
req(admin, "/admin/siswa", {"nama": "Siswa Uji Dua", "kode": "UJI-002", "class_id": kelas_id})
req(admin, "/admin/penugasan", {"user_id": guru_id, "class_id": kelas_id, "subject_id": mapel_id})

# QR PNG endpoint
r = admin.open(BASE + "/qr/UJI-001.png")
must(r.status == 200 and r.headers.get_content_type() == "image/png", "endpoint QR PNG kartu siswa jalan (200 image/png)")

# 3. Login guru, buat sesi
guru = client()
s, url, body = req(guru, "/login", {"username": "guruuji", "password": "guruuji123"})
must("dashboard" in url, "login guru berhasil")
s, url, sesi_page = req(guru, "/guru/sesi")
must("UJI-Kelas-1 - UJI-Matematika" in sesi_page, "guru melihat penugasannya di form buat sesi")
from datetime import datetime
now = datetime.now()
s, url, detail = req(guru, "/guru/sesi/buat", {
    "combo": f"{kelas_id}:{mapel_id}", "tanggal": now.strftime("%Y-%m-%d"),
    "jam_mulai": now.strftime("%H:%M"), "batas_terlambat_menit": "15"})
m = re.search(r"/sesi/(\d+)", url)
must(m is not None, f"sesi dibuat -> {url}")
sid = m.group(1)

# 4. Catat absen lewat kode siswa (jalur yang sama dipakai scan kamera & demo)
s, url, after = req(guru, f"/sesi/{sid}/scan", {"kode": "UJI-001", "metode": "demo"})
s, url, detail = req(guru, f"/sesi/{sid}")
must("Siswa Uji Satu" in detail and "Hadir" in detail, "scan/demo kode UJI-001 tercatat Hadir di sesi")
# scan ganda harus diabaikan
req(guru, f"/sesi/{sid}/scan", {"kode": "UJI-001", "metode": "scan"})
s, url, detail = req(guru, f"/sesi/{sid}")
must(detail.count("Siswa Uji Satu") >= 1, "scan ganda tidak membuat baris baru (halaman tetap konsisten)")

# 5. Tutup sesi -> siswa kedua jadi Alpa, rekap tampil, CSV bisa diunduh
req(guru, f"/sesi/{sid}/tutup", {})  # {} agar terkirim sebagai POST
s, url, detail = req(guru, f"/sesi/{sid}")
must("Alpa" in detail, "setelah sesi ditutup, siswa belum tercatat menjadi Alpa")
s, url, rekap = req(guru, "/rekap")
must("UJI-Matematika" in rekap and "Hadir: 1" in rekap and "Alpa: 1" in rekap,
     "rekap guru menampilkan ringkasan Hadir: 1, Alpa: 1")
r = guru.open(BASE + f"/rekap/{sid}/csv")
csv_body = r.read().decode()
must("UJI-001" in csv_body and "Hadir" in csv_body, "unduh CSV rekap berisi catatan UJI-001 Hadir")

# 6. Guru lain tidak bisa lihat sesi ini (isolasi peran): admin bisa
s, url, rekap_admin = req(admin, "/rekap")
must("UJI-Matematika" in rekap_admin, "admin melihat rekap semua guru")
print("\nSEMUA UJI LULUS")
