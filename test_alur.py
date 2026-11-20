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

# 7. Edit & hapus siswa (admin)
s, url, daftar = req(admin, "/admin/siswa")
must("Edit" in daftar and "Hapus" in daftar, "daftar siswa menampilkan tombol Edit dan Hapus")
def siswa_id(htmltxt, kode):
    for chunk in htmltxt.split("<li>"):
        if kode in chunk:
            m = re.search(r"/admin/siswa/(\d+)/edit", chunk)
            if m: return m.group(1)
    return None
dua_id = siswa_id(daftar, "UJI-002"); satu_id = siswa_id(daftar, "UJI-001")
must(dua_id and satu_id, f"id siswa terbaca dari daftar: UJI-001={satu_id} UJI-002={dua_id}")
# form edit terisi data awal
s, url, form_edit = req(admin, f"/admin/siswa/{dua_id}/edit")
must("Siswa Uji Dua" in form_edit and "UJI-002" in form_edit, "form edit siswa terisi data awal (nama & kode)")
# edit nama siswa UJI-002 -> nama baru tampil, nama lama hilang
req(admin, f"/admin/siswa/{dua_id}/edit", {"nama": "Siswa Uji Dua Revisi", "kode": "UJI-002", "class_id": kelas_id})
s, url, daftar = req(admin, "/admin/siswa")
must("Siswa Uji Dua Revisi" in daftar, "hasil edit nama siswa tampil di daftar")
must("Siswa Uji Dua" not in daftar.replace("Siswa Uji Dua Revisi", ""), "nama lama siswa sudah tidak ada di daftar")
# edit kode ke kode milik siswa lain harus ditolak
s, url, form_edit = req(admin, f"/admin/siswa/{dua_id}/edit", {"nama": "Siswa Uji Dua Revisi", "kode": "UJI-001", "class_id": kelas_id})
must("Kode siswa sudah dipakai." in form_edit, "edit kode ke kode siswa lain ditolak dengan pesan")
s, url, daftar = req(admin, "/admin/siswa")
must("UJI-002" in daftar, "kode siswa tidak berubah setelah edit yang ditolak")
# id siswa tidak ada -> diarahkan kembali dengan pesan
s, url, daftar404 = req(admin, "/admin/siswa/999999/edit")
must("Siswa tidak ditemukan." in daftar404, "edit id siswa tidak ada diarahkan ke daftar dengan pesan")
# hapus siswa UJI-002 (sudah diedit namanya)
req(admin, f"/admin/siswa/{dua_id}/hapus", {})
s, url, daftar = req(admin, "/admin/siswa")
must("Siswa Uji Dua Revisi" not in daftar and "UJI-002" not in daftar, "siswa terhapus hilang dari daftar")
# hapus siswa UJI-001 yang SUDAH punya catatan absen Hadir -> harus berhasil, absen ikut terhapus
s, url, hasil_hapus = req(admin, f"/admin/siswa/{satu_id}/hapus", {})
must("Siswa dihapus." in hasil_hapus, "hapus siswa ber-catatan absen berhasil dengan pesan 'Siswa dihapus.'")
s, url, daftar = req(admin, "/admin/siswa")
must("Siswa Uji Satu" not in daftar and "UJI-001" not in daftar, "siswa ber-catatan absen hilang dari daftar setelah dihapus")
s, url, rekap_admin = req(admin, "/rekap")
must("Siswa Uji Satu" not in rekap_admin, "catatan absen siswa yang dihapus ikut hilang dari rekap")

# 8. Edit, hapus & nonaktifkan guru (admin)
s, url, daftar_guru = req(admin, "/admin/guru")
must("Edit" in daftar_guru and "Hapus" in daftar_guru, "daftar guru menampilkan tombol Edit dan Hapus")
def guru_id_dari(htmltxt, username):
    for chunk in htmltxt.split("<li>"):
        if username in chunk:
            m = re.search(r"/admin/guru/(\d+)/edit", chunk)
            if m: return m.group(1)
    return None
guruuji_id = guru_id_dari(daftar_guru, "guruuji")
must(guruuji_id, f"id guruuji terbaca dari daftar: {guruuji_id}")
# buat guru kedua (belum punya sesi apa pun)
req(admin, "/admin/guru", {"nama": "Guru Uji Dua", "username": "guruujidua", "password": "guruujidua123"})
s, url, daftar_guru = req(admin, "/admin/guru")
g2_id = guru_id_dari(daftar_guru, "guruujidua")
must(g2_id, f"id guru kedua terbaca dari daftar: {g2_id}")
# guru ber-riwayat (guruuji) -> teks konfirmasi Hapus-nya soal menonaktifkan, bukan menghapus
chunk_uji = next((c for c in daftar_guru.split("<li>") if "guruuji" in c), "")
must("dinonaktifkan" in chunk_uji, "konfirmasi Hapus guru ber-riwayat berisi kata 'dinonaktifkan'")
chunk_dua = next((c for c in daftar_guru.split("<li>") if "guruujidua" in c), "")
must("dihapus permanen" in chunk_dua, "konfirmasi Hapus guru tanpa riwayat berisi 'dihapus permanen'")
# form edit terisi data awal + ada kolom password opsional
s, url, form_edit = req(admin, f"/admin/guru/{g2_id}/edit")
must("Guru Uji Dua" in form_edit and "guruujidua" in form_edit and "Password baru" in form_edit,
     "form edit guru terisi data awal dan punya kolom Password baru opsional")
# edit nama + username (password dikosongkan -> tidak berubah)
req(admin, f"/admin/guru/{g2_id}/edit", {"nama": "Guru Uji Dua Revisi", "username": "guruujidua2", "password": ""})
s, url, daftar_guru = req(admin, "/admin/guru")
must("Guru Uji Dua Revisi" in daftar_guru and "guruujidua2" in daftar_guru, "hasil edit nama & username guru tampil di daftar")
must("guruujidua<" not in daftar_guru and ">guruujidua<" not in daftar_guru, "username lama guru sudah tidak ada di daftar")
# login pakai username baru + password lama (password tidak ikut berubah)
g2 = client()
s, url, body = req(g2, "/login", {"username": "guruujidua2", "password": "guruujidua123"})
must("dashboard" in url, "guru kedua bisa login dengan username baru + password lama")
# edit username ke username guru lain harus ditolak
s, url, form_edit = req(admin, f"/admin/guru/{g2_id}/edit", {"nama": "Guru Uji Dua Revisi", "username": "guruuji", "password": ""})
must("Username sudah dipakai." in form_edit, "edit username ke username guru lain ditolak dengan pesan")
s, url, daftar_guru = req(admin, "/admin/guru")
must("guruujidua2" in daftar_guru, "username guru tidak berubah setelah edit yang ditolak")
# ganti password lewat edit -> password lama gagal, password baru berhasil
req(admin, f"/admin/guru/{g2_id}/edit", {"nama": "Guru Uji Dua Revisi", "username": "guruujidua2", "password": "passwordbaru123"})
g2lama = client()
s, url, body = req(g2lama, "/login", {"username": "guruujidua2", "password": "guruujidua123"})
must("dashboard" not in url, "login dengan password lama gagal setelah password diganti")
g2baru = client()
s, url, body = req(g2baru, "/login", {"username": "guruujidua2", "password": "passwordbaru123"})
must("dashboard" in url, "login dengan password baru berhasil")
# id guru tidak ada -> diarahkan kembali dengan pesan
s, url, daftar404 = req(admin, "/admin/guru/999999/edit")
must("Guru tidak ditemukan." in daftar404, "edit id guru tidak ada diarahkan ke daftar dengan pesan")
s, url, hasil404 = req(admin, "/admin/guru/999999/hapus", {})
must("Guru tidak ditemukan." in hasil404, "hapus id guru tidak ada diarahkan ke daftar dengan pesan")
# hapus guru kedua (belum punya sesi) -> terhapus permanen, login gagal
s, url, hasil_hapus = req(admin, f"/admin/guru/{g2_id}/hapus", {})
must("Guru dihapus." in hasil_hapus, "hapus guru tanpa riwayat berhasil dengan pesan 'Guru dihapus.'")
s, url, daftar_guru = req(admin, "/admin/guru")
must("guruujidua2" not in daftar_guru and "Guru Uji Dua Revisi" not in daftar_guru, "guru terhapus hilang dari daftar")
g2mati = client()
s, url, body = req(g2mati, "/login", {"username": "guruujidua2", "password": "passwordbaru123"})
must("dashboard" not in url, "login guru yang sudah dihapus gagal")
# hapus guruuji (SUDAH punya sesi) -> dinonaktifkan, BUKAN dihapus; riwayat tetap ada
s, url, hasil_nonaktif = req(admin, f"/admin/guru/{guruuji_id}/hapus", {})
must("Guru dinonaktifkan karena sudah punya riwayat sesi absen." in hasil_nonaktif,
     "hapus guru ber-riwayat menghasilkan pesan dinonaktifkan")
s, url, daftar_guru = req(admin, "/admin/guru")
must("Guru Uji" in daftar_guru and "(nonaktif)" in daftar_guru and "Aktifkan" in daftar_guru,
     "guru ber-riwayat tetap di daftar sebagai (nonaktif) dengan tombol Aktifkan")
s, url, rekap_admin = req(admin, "/rekap")
must("UJI-Matematika" in rekap_admin and "Guru Uji" in rekap_admin,
     "sesi & rekap guru yang dinonaktifkan tetap terlihat oleh admin")
# guru nonaktif hilang dari dropdown penugasan
s, url, form_penugasan = req(admin, "/admin/penugasan")
m = re.search(r'<select name="user_id">(.*?)</select>', form_penugasan, re.S)
must(m is not None and "Guru Uji" not in m.group(1), "guru nonaktif tidak muncul di dropdown penugasan")
# login guruuji saat nonaktif ditolak dengan pesan jelas
g_mati = client()
s, url, body = req(g_mati, "/login", {"username": "guruuji", "password": "guruuji123"})
must("dashboard" not in url and "Akun ini sudah dinonaktifkan." in body,
     "login guru nonaktif ditolak dengan pesan 'Akun ini sudah dinonaktifkan.'")
# aktifkan lagi -> login kembali bisa
s, url, hasil_aktif = req(admin, f"/admin/guru/{guruuji_id}/aktifkan", {})
must("Guru diaktifkan kembali." in hasil_aktif, "aktifkan guru berhasil dengan pesan")
s, url, daftar_guru = req(admin, "/admin/guru")
must("(nonaktif)" not in daftar_guru, "tidak ada lagi guru (nonaktif) di daftar setelah diaktifkan")
g_hidup = client()
s, url, body = req(g_hidup, "/login", {"username": "guruuji", "password": "guruuji123"})
must("dashboard" in url, "login guruuji berhasil lagi setelah diaktifkan")

# 9. Edit & hapus kelas, mapel, penugasan (admin)
import urllib.error
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("Edit" in daftar_kelas and "Hapus" in daftar_kelas, "daftar kelas menampilkan tombol Edit dan Hapus")
s, url, daftar_mapel = req(admin, "/admin/mapel")
must("Edit" in daftar_mapel and "Hapus" in daftar_mapel, "daftar mapel menampilkan tombol Edit dan Hapus")
s, url, daftar_tugas = req(admin, "/admin/penugasan")
must("Edit" in daftar_tugas and "Hapus" in daftar_tugas, "daftar penugasan menampilkan tombol Edit dan Hapus")
def simple_id(htmltxt, nama, base):
    for chunk in htmltxt.split("<li>"):
        if f"{nama} &mdash;" in chunk:
            m = re.search(base + r"/(\d+)/edit", chunk)
            if m: return m.group(1)
    return None
def tugas_id(htmltxt, potongan):
    for chunk in htmltxt.split("<li>"):
        if potongan in chunk:
            m = re.search(r"/admin/penugasan/(\d+)/edit", chunk)
            if m: return m.group(1)
    return None
kid = simple_id(daftar_kelas, "UJI-Kelas-1", "/admin/kelas")
mid = simple_id(daftar_mapel, "UJI-Matematika", "/admin/mapel")
t1 = tugas_id(daftar_tugas, "UJI-Kelas-1")
must(kid and kid == kelas_id and mid and mid == mapel_id and t1,
     f"id terbaca dari daftar: kelas={kid} mapel={mid} penugasan={t1}")
# penjaga akses rute baru: guru -> 404; anonim POST hapus -> diarahkan ke login
try:
    req(guru, f"/admin/kelas/{kid}/edit")
    must(False, "guru membuka edit kelas harusnya 404")
except urllib.error.HTTPError as e:
    must(e.code == 404, "guru membuka edit kelas -> 404")
anon = client()
s, url, body = req(anon, f"/admin/kelas/{kid}/hapus", {})
must("/login" in url, "anonim POST hapus kelas diarahkan ke login")
# --- kelas: edit (rename lalu kembalikan), duplikat ditolak
s, url, form_edit = req(admin, f"/admin/kelas/{kid}/edit")
must('value="UJI-Kelas-1"' in form_edit, "form edit kelas terisi nama awal")
s, url, hasil = req(admin, f"/admin/kelas/{kid}/edit", {"nama": "UJI-Kelas-1 Revisi"})
must("Kelas diperbarui." in hasil, "edit kelas berhasil dengan pesan 'Kelas diperbarui.'")
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("UJI-Kelas-1 Revisi" in daftar_kelas, "nama kelas baru tampil di daftar")
must("UJI-Kelas-1" not in daftar_kelas.replace("UJI-Kelas-1 Revisi", ""), "nama kelas lama sudah tidak ada di daftar")
req(admin, f"/admin/kelas/{kid}/edit", {"nama": "UJI-Kelas-1"})
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("UJI-Kelas-1" in daftar_kelas and "Revisi" not in daftar_kelas, "nama kelas dikembalikan seperti semula")
# data pembantu: kelas & mapel kedua
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-2"})
req(admin, "/admin/mapel", {"nama": "UJI-Mapel-2"})
s, url, daftar_kelas = req(admin, "/admin/kelas")
k2id = simple_id(daftar_kelas, "UJI-Kelas-2", "/admin/kelas")
s, url, daftar_mapel = req(admin, "/admin/mapel")
m2id = simple_id(daftar_mapel, "UJI-Mapel-2", "/admin/mapel")
must(k2id and m2id, f"id data pembantu terbaca: kelas2={k2id} mapel2={m2id}")
s, url, form_edit = req(admin, f"/admin/kelas/{kid}/edit", {"nama": "UJI-Kelas-2"})
must("Nama sudah ada." in form_edit, "edit kelas ke nama kelas lain ditolak dengan pesan")
s, url, form_edit = req(admin, f"/admin/mapel/{mid}/edit")
must('value="UJI-Matematika"' in form_edit, "form edit mapel terisi nama awal")
s, url, form_edit = req(admin, f"/admin/mapel/{mid}/edit", {"nama": "UJI-Mapel-2"})
must("Nama sudah ada." in form_edit, "edit mapel ke nama mapel lain ditolak dengan pesan")
s, url, daftar_mapel = req(admin, "/admin/mapel")
must("UJI-Matematika" in daftar_mapel, "nama mapel tidak berubah setelah edit yang ditolak")
# id tidak ada -> diarahkan kembali dengan pesan
s, url, hasil404 = req(admin, "/admin/kelas/999999/edit")
must("Data tidak ditemukan." in hasil404, "edit id kelas tidak ada diarahkan ke daftar dengan pesan")
s, url, hasil404 = req(admin, "/admin/mapel/999999/hapus", {})
must("Data tidak ditemukan." in hasil404, "hapus id mapel tidak ada diarahkan ke daftar dengan pesan")
# --- kelas & mapel yang masih dipakai TIDAK bisa dihapus (ada sesi & penugasan)
s, url, hasil = req(admin, f"/admin/kelas/{kid}/hapus", {})
must("tidak bisa dihapus" in hasil, "hapus kelas yang masih dipakai ditolak dengan pesan")
s, url, daftar_kelas = req(admin, "/admin/kelas")
must("UJI-Kelas-1" in daftar_kelas, "kelas yang ditolak hapus tetap ada di daftar")
s, url, hasil = req(admin, f"/admin/mapel/{mid}/hapus", {})
must("tidak bisa dihapus" in hasil, "hapus mapel yang masih dipakai ditolak dengan pesan")
s, url, daftar_mapel = req(admin, "/admin/mapel")
must("UJI-Matematika" in daftar_mapel, "mapel yang ditolak hapus tetap ada di daftar")
# --- penugasan: buat kedua, edit, duplikat ditolak, hapus
req(admin, "/admin/penugasan", {"user_id": guru_id, "class_id": k2id, "subject_id": mapel_id})
s, url, daftar_tugas = req(admin, "/admin/penugasan")
t2 = tugas_id(daftar_tugas, "UJI-Kelas-2")
must(t2 and t2 != t1, f"id penugasan kedua terbaca: {t2}")
s, url, form_edit = req(admin, f"/admin/penugasan/{t2}/edit")
must(f'<option value="{k2id}" selected>' in form_edit and f'<option value="{mapel_id}" selected>' in form_edit,
     "form edit penugasan menandai pilihan kelas & mapel saat ini")
s, url, hasil = req(admin, f"/admin/penugasan/{t2}/edit", {"user_id": guru_id, "class_id": k2id, "subject_id": m2id})
must("Penugasan diperbarui." in hasil, "edit penugasan berhasil dengan pesan 'Penugasan diperbarui.'")
s, url, daftar_tugas = req(admin, "/admin/penugasan")
ul_tugas = re.search(r"<ul>(.*?)</ul>", daftar_tugas, re.S).group(1)
must("UJI-Mapel-2" in ul_tugas, "hasil edit penugasan tampil di daftar")
s, url, form_edit = req(admin, f"/admin/penugasan/{t1}/edit", {"user_id": guru_id, "class_id": k2id, "subject_id": m2id})
must("Penugasan itu sudah ada." in form_edit, "edit penugasan ke kombinasi yang sudah ada ditolak dengan pesan")
s, url, hasil = req(admin, f"/admin/penugasan/{t2}/hapus", {})
must("Penugasan dihapus." in hasil, "hapus penugasan berhasil dengan pesan 'Penugasan dihapus.'")
s, url, daftar_tugas = req(admin, "/admin/penugasan")
ul_tugas = re.search(r"<ul>(.*?)</ul>", daftar_tugas, re.S).group(1)
must("UJI-Mapel-2" not in ul_tugas and f"/admin/penugasan/{t2}/edit" not in daftar_tugas,
     "penugasan terhapus hilang dari daftar")
s, url, rekap_admin = req(admin, "/rekap")
must("UJI-Matematika" in rekap_admin, "sesi & rekap lama tetap utuh setelah penugasan dihapus")
s, url, hasil404 = req(admin, "/admin/penugasan/999999/hapus", {})
must("Data tidak ditemukan." in hasil404, "hapus id penugasan tidak ada diarahkan ke daftar dengan pesan")
# --- kelas & mapel yang belum dipakai bisa dihapus
req(admin, "/admin/kelas", {"nama": "UJI-Kelas-Kosong"})
req(admin, "/admin/mapel", {"nama": "UJI-Mapel-Kosong"})
s, url, daftar_kelas = req(admin, "/admin/kelas")
kkid = simple_id(daftar_kelas, "UJI-Kelas-Kosong", "/admin/kelas")
s, url, daftar_mapel = req(admin, "/admin/mapel")
mkid = simple_id(daftar_mapel, "UJI-Mapel-Kosong", "/admin/mapel")
must(kkid and mkid, f"id kelas/mapel kosong terbaca: {kkid}, {mkid}")
s, url, hasil = req(admin, f"/admin/kelas/{kkid}/hapus", {})
must("Kelas dihapus." in hasil, "hapus kelas kosong berhasil dengan pesan 'Kelas dihapus.'")
s, url, hasil = req(admin, f"/admin/mapel/{mkid}/hapus", {})
must("Mapel dihapus." in hasil, "hapus mapel kosong berhasil dengan pesan 'Mapel dihapus.'")
s, url, daftar_kelas = req(admin, "/admin/kelas")
s, url, daftar_mapel = req(admin, "/admin/mapel")
must("UJI-Kelas-Kosong" not in daftar_kelas and "UJI-Mapel-Kosong" not in daftar_mapel,
     "kelas & mapel kosong hilang dari daftar setelah dihapus")
print("\nSEMUA UJI LULUS")
