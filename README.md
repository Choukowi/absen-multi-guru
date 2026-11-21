# Aplikasi Absen Siswa — Gerbang Global

Aplikasi web absensi sekolah dengan model **satu perangkat scan di gerbang**:
tidak ada lagi sesi per mata pelajaran. Setiap siswa dan guru punya **kartu
QR Code biasa** (bukan QRIS pembayaran). Di gerbang, petugas membuka halaman
**Scan Gerbang** di satu HP/tablet, siswa/guru menunjukkan kartunya, kamera
membaca QR, dan kehadiran **hari itu** langsung tercatat.

Ini proyek kode mandiri yang bisa di-deploy sendiri.

## Aturan absen

- Scan sampai **07:00** = `Hadir`; scan setelah 07:00 = `Terlambat`
  (jam masuk dicatat persis, format WIB/Asia-Jakarta).
- Sampai **09:00** belum scan = otomatis `Alpa` (terisi saat tabel/dashboard
  dibuka; tidak pernah untuk hari Minggu atau tanggal masa depan).
  Scan setelah Alpa otomatis tetap diterima dan menggantikannya.
- Scan ganda diabaikan ("Sudah tercatat"). Siswa/guru yang sudah ditandai
  `Sakit`/`Izin` secara manual **tidak ditimpa** oleh scan.
- `Sakit`/`Izin`/koreksi status diisi manual oleh admin atau guru mana pun
  lewat sel tabel mingguan (ketuk sel → form ubah kehadiran).
- Hasilnya tampil sebagai **tabel mingguan Senin–Sabtu**: per kelas untuk
  siswa, dan satu tabel untuk semua guru. Semua pengguna yang login bisa
  melihat semua tabel (global), dan mengunduhnya sebagai **Excel (.xlsx)**
  yang terformat profesional (judul sekolah, pewarnaan status, total
  H/T/S/I/A, siap cetak landscape).

## Fitur

- Login per pengguna, password **hash** (PBKDF2-HMAC-SHA256 + salt),
  session cookie HttpOnly, logout, Ganti Password.
- **Admin**: kelola Guru (nama, **mata pelajaran**, username, password —
  kode QR guru dibuat otomatis, mis. `G001`), Kelas, Siswa (nama, kelas,
  **NIS** = isi QR kartunya). Edit/Hapus di semua menu; hapus diblokir bila
  data masih dipakai (kelas berisi siswa tidak bisa dihapus; guru yang
  sudah punya riwayat kehadiran dinonaktifkan, bukan dihapus, agar
  riwayatnya tetap tersimpan).
- **Cetak Kartu QR**: lembar kartu siswa per kelas (nama, kelas, QR, NIS)
  dan kartu semua guru (nama, mapel, QR, kode guru), siap cetak A4.
- **Pengaturan Tampilan** (admin): nama sekolah (tampil di aplikasi &
  judul Excel), warna latar aplikasi (8 warna contoh + pemilih warna
  kustom), dan **wallpaper sekolah** (unggah JPG/PNG/WebP maks ±5 MB,
  menjadi latar semua halaman termasuk login, bisa diganti/dihapus).
- **Dashboard**: kartu statistik hari ini (Siswa Hadir/Terlambat/
  Sakit-Izin/Alpa/Belum Scan, Guru Hadir) + daftar kedatangan terakhir.
- Tampilan modern ala aplikasi populer: sidebar di desktop, top bar +
  navigasi bawah di HP, ikon SVG inline, tanpa aset/font eksternal.

## Stack

- Python 3 (diuji di Python 3.12) — backend hanya **standard library**
  (`http.server`, `sqlite3`), tanpa framework web.
- Database: **SQLite** satu file (`data/absen.db`, dibuat otomatis).
- Paket tambahan (`requirements.txt`): `qrcode` + `Pillow` (gambar QR
  kartu), `openpyxl` (unduhan Excel).
- Scan QR di browser: `jsQR` 1.4.0 dari CDN jsDelivr (`static/app.js`);
  kamera browser butuh konteks aman (**HTTPS** atau `localhost`) dan
  koneksi internet untuk memuat jsQR. Fallback: ketik NIS/kode guru manual.

## Menjalankan secara lokal

```bash
cd ~/workspace/absen-multi-guru
pip3 install -r requirements.txt
python3 app.py
```

Lalu buka **http://127.0.0.1:8000**. Port lain: `PORT=8080 python3 app.py`.
Lokasi DB lain: `ABSEN_DB=/path/absen.db python3 app.py`.

## Akun uji lokal

Saat database masih kosong, aplikasi membuat **satu akun admin awal**:

- Username: `admin`
- Password: `admin123`

> **WAJIB:** akun ini hanya untuk pengujian lokal. **Ganti password-nya
> segera** lewat menu **Ganti Password** setelah login pertama, dan
> **jangan pernah memakai akun/password awal ini di produksi**. Dashboard
> admin juga menampilkan peringatan selama password awal belum diganti.

Akun guru dibuat admin dari menu **Guru → Tambah Guru** (kode QR guru
dibuat otomatis dan kartunya dicetak dari menu **Cetak Kartu**). Guru di
menu itu adalah **data induk** (nama + mata pelajaran) — guru baru bisa
login hanya setelah ditunjuk sebagai wali kelas (lihat bagian berikut).

Alur pemakaian singkat:

1. Login admin → buat Kelas, Siswa (isi NIS tiap siswa), Guru (isi mata
   pelajarannya).
2. Menu **Kelola Kelas** → tunjuk wali kelas tiap kelas (login wali
   langsung terbit; sampaikan username + password sementaranya).
3. Menu **Cetak Kartu** → cetak kartu siswa per kelas + kartu guru,
   gunting, bagikan.
4. Di gerbang: login di HP/tablet gerbang → buka **Scan Gerbang** → scan
   kartu yang datang (atau ketik NIS/kode bila kamera bermasalah).
5. Lihat hasil di **Tabel Siswa** / **Tabel Guru**, unduh Excel mingguan
   bila perlu. Isi Sakit/Izin dengan mengetuk sel tabel pada tanggalnya.

## Login Wali Kelas

Hanya **wali kelas** yang bisa login sebagai guru. Modelnya:

- **Penunjukan**: admin membuka **Kelola Kelas** → Tambah/Edit kelas →
  kolom **Wali Kelas** diisi dengan *memilih* guru dari daftar guru yang
  sudah dimasukkan (tidak perlu mengetik ulang nama). Satu kelas punya
  paling banyak satu wali, dan satu guru menjadi wali untuk paling
  banyak satu kelas (guru yang sudah menjadi wali kelas lain tidak
  muncul lagi di daftar pilihan kelas lain).
- **Kredensial otomatis**: begitu wali ditunjuk (atau diganti), sistem
  membuatkan login baru — username `wali<namakelas>` (mis. kelas `1A` →
  `wali1a`, kelas `X IPA 1` → `walixipa1`) dan **password acak** 10
  karakter. Username + password itu ditampilkan ke admin **satu kali**
  lewat pesan di layar; sampaikan ke gurunya. Menyimpan edit kelas
  *tanpa* mengganti wali tidak mengubah password yang berlaku.
- **Lupa password**: admin membuka Edit guru yang bersangkutan →
  tombol **Buat Password Baru** menerbitkan password acak baru
  (ditampilkan satu kali; password lama langsung mati).
- **Diganti/dicabut**: wali lama langsung kehilangan login (username-nya
  diparkir dan penjaga login menolak guru yang bukan wali). Sesi wali
  yang sedang aktif pun langsung berakhir pada permintaan berikutnya.
  Guru yang dinonaktifkan tetap tercatat sebagai wali; loginnya tertahan
  selama nonaktif dan berlaku lagi begitu diaktifkan.
- **Akses wali** (sengaja dibatasi): **Dashboard** (tanpa kartu
  pencarian dan tanpa panel Laporan Hari Ini), **Tabel Siswa** untuk
  kelasnya sendiri saja (tanpa pemilih kelas; koreksi Sakit/Izin hanya
  untuk siswa kelasnya), **Cetak Kartu** untuk kelasnya sendiri saja,
  plus **Ganti Password** (untuk mengganti password sementara) dan
  **Keluar**. Scan Gerbang, Tabel Guru, halaman laporan, pencarian, dan
  semua menu admin tidak bisa dibuka wali.
- Guru yang belum ditunjuk sebagai wali **tidak bisa login sama
  sekali**; pada data lama, semua kelas mulai tanpa wali
  (`classes.wali_id` kosong) sampai admin menunjuknya.

## Struktur data (SQLite, `data/absen.db`)

| Tabel | Isi |
|---|---|
| `users` | akun login: `username` (unik), `password_hash`, `salt`, `role` (`admin`/`guru`), `nama`, `aktif`, `mapel` (mata pelajaran guru), `kode_qr` (kode QR guru, mis. `G001`) |
| `classes` | kelas: `nama` (unik) |
| `students` | siswa: `nama`, `kode` (= **NIS**, unik, isi QR kartunya), `class_id` |
| `kehadiran` | satu baris per orang per hari: `tanggal`, `ref_type` (`siswa`/`guru`), `ref_id`, `status` (`Hadir`/`Terlambat`/`Sakit`/`Izin`/`Alpa`), `jam` (HH:MM, hanya untuk scan/manual hadir), `metode` (`scan`/`manual`/`auto`), `dicatat_oleh`; unik per (tanggal, tipe, orang) |
| `settings` | pengaturan tampilan key-value: `bg_color`, `wallpaper` (nama berkas), `nama_sekolah` |

Wallpaper tersimpan sebagai berkas di `data/uploads/` (nama acak) dan
disajikan hanya lewat route `/wallpaper/<nama>`.

### Migrasi dari versi lama (sesi per mapel)

Aman untuk database versi lama: saat start, aplikasi hanya menjalankan
`CREATE TABLE IF NOT EXISTS` dan `ALTER TABLE ... ADD COLUMN` yang dijaga
(kolom `mapel` & `kode_qr` di `users`, plus tabel baru `kehadiran` &
`settings`). **Tidak ada tabel yang di-drop atau dibuat ulang.** Guru lama
otomatis mendapat `kode_qr` backfill (`G` + id 3 digit) dan `mapel`
kosong untuk diisi admin lewat Edit Guru. Tabel lama (`subjects`,
`assignments`, `sessions`, `attendance`) dibiarkan apa adanya di file DB
dan tidak dipakai lagi. Data guru/kelas/siswa yang sudah terlanjur diisi
**tidak hilang**. (Teruji otomatis: lihat Fase M di `test_alur.py`.)

Session login tersimpan di memori server (bukan di tabel), jadi
me-restart server akan mengeluarkan (logout) semua pengguna.

## Menguji

`test_alur.py` mengelola server ujinya sendiri (DB scratch di `/tmp`,
port 8877) dan me-restart server antar skenario waktu:

```bash
cd ~/workspace/absen-multi-guru
/tmp/absen-venv/bin/python test_alur.py   # python yang punya dependensi proyek
```

Uji mencakup: migrasi DB lama, login & penjaga akses, CRUD guru/kelas/
siswa (termasuk penolakan duplikat & hapus terblokir), scan Hadir/
Terlambat/ganda/tak dikenal, Sakit manual yang tidak ditimpa scan, Alpa
otomatis jam 09:00 yang bisa digantikan scan, hari Minggu tanpa Alpa,
pemisahan tabel antar kelas, isi & pewarnaan Excel, pengaturan tampilan
(warna preset/kustom, nama sekolah, unggah/saji/hapus wallpaper, guru
ditolak 404), dan nonaktif/aktifkan guru.

### Hook waktu khusus tes: `ABSEN_NOW_OVERRIDE`

Semua logika "sekarang/hari ini" memakai zona **Asia/Jakarta**. Untuk
pengujian, env var `ABSEN_NOW_OVERRIDE` (format ISO, mis.
`ABSEN_NOW_OVERRIDE=2026-11-16T06:45:00`) menggantikan jam asli; nilainya
dibaca setiap pemanggilan, jadi server uji cukup di-restart dengan env
baru untuk berpindah skenario waktu. **Jangan pakai di produksi.**

## Panduan deploy ke hosting

Aplikasi membaca `PORT` dan `ABSEN_DB` dari environment dan mendengarkan
`0.0.0.0`. Berkas bantu: `render.yaml`, `Procfile`, `.python-version`,
`runtime.txt` (Python 3.12). Build command: `pip install -r requirements.txt`,
start command: `python3 app.py`, health check `/login`.

> Catatan umum: sesi login disimpan di memori satu proses — jalankan
> **satu instance saja**. **Ganti password admin awal (`admin123`) segera
> setelah login pertama.** Kamera scan butuh **HTTPS**.

- **Render Free**: hanya untuk tes kamera/alur — aplikasi tidur setelah
  15 menit dan **semua data terhapus** setiap tidur/restart/deploy ulang
  (tidak ada disk persisten). Jangan isi data asli di paket gratis.
- **Render Starter (± $7,25/bln)** + Disk 1 GB di-mount ke `/var/data`,
  set `ABSEN_DB=/var/data/absen.db` — data persisten, aplikasi tidak tidur.
- **Railway Hobby ($5/bln)** + Volume (mount `/data`), set
  `ABSEN_DB=/data/absen.db`. Bila memakai Railway, folder wallpaper juga
  harus di volume: pindahkan `data/` seutuhnya ke volume tersebut.
- **VPS (± Rp50–120 rb/bln)**: disk sungguhan, data persisten alami.
  Jalankan di balik reverse proxy (Nginx/Caddy) dengan HTTPS
  (Let's Encrypt), dan **backup berkala** file DB + folder `data/uploads/`
  — seluruh data sekolah ada di keduanya.

## Lisensi per pemasangan

Setiap pemasangan untuk sekolah pembeli dapat diberi label lisensi
terkunci: isi variabel environment **`ABSEN_LISENSI`** di server
(mis. Variables pada layanan Railway) dengan nama sekolah pembeli,
contoh `ABSEN_LISENSI="KIRIN ART SCHOOL"`. Nama itu tampil sebagai
baris kecil "Dilisensikan untuk: …" di bawah kartu login dan di
footer aplikasi. Label ini **hanya** berasal dari environment server:
tidak disimpan di database, tidak ikut dalam Backup & Pulihkan, tidak
muncul di halaman pengaturan mana pun, dan **tidak bisa diubah atau
dihapus dari dalam aplikasi** — sekolah pemakai tidak punya cara
menggantinya lewat web. Kosongkan/hapus variabelnya bila label tidak
diinginkan (mis. pada pemasangan demo milik sendiri); halaman lalu
tampil persis seperti tanpa fitur ini.

## Keterbatasan yang diketahui

- Kamera fisik tidak dapat diuji di VM pengembangan; logika scan diuji
  lewat jalur kode yang sama persis dengan hasil decode QR (form kode →
  pencatatan). Uji kamera sungguhan harus di HP pada deployment HTTPS.
- Belum ada: impor siswa massal (CSV), lupa/reset password lewat email,
  notifikasi orang tua, multi-sekolah, dan audit log. Data siswa adalah
  data anak — amankan akses server dan backup-nya.
