# Aplikasi Absen Siswa Multi-Guru

Aplikasi web absensi siswa **per mata pelajaran di kelas**, dipakai bersama oleh
banyak guru dalam satu sekolah dengan **satu database pusat**.

Cara absen: siswa **tidak memakai HP**. Guru membuka sesi mata pelajaran di
aplikasinya, lalu **scan QR Code biasa milik siswa di dalam aplikasi**
(pakai kamera HP guru). QR Code di sini adalah QR Code umum berisi kode unik
siswa — **bukan QRIS pembayaran**. Begitu QR terbaca, kehadiran siswa itu
tercatat di sesi tersebut.

Ini proyek kode mandiri yang bisa di-deploy sendiri. Proyek ini **belum**
di-deploy ke layanan publik mana pun — keputusan hosting ada di tangan Anda.

## Fitur yang sudah ada dan teruji

- Login terpisah per pengguna, password tersimpan sebagai **hash**
  (PBKDF2-HMAC-SHA256 + salt), session login lewat cookie HttpOnly, logout,
  dan halaman Ganti Password.
- **Peran Admin**: kelola guru, kelas, mata pelajaran, siswa, dan penugasan
  guru (guru mengajar kelas + mapel apa), melihat rekap semua guru.
- **Peran Guru**: hanya melihat kelas/mapel sesuai penugasannya; membuat sesi
  (kelas + mapel + tanggal + jam mulai + batas terlambat); scan QR di dalam
  aplikasi; input manual Sakit/Izin/Alpa/Hadir/Terlambat; melihat rekapnya
  sendiri. Guru tidak bisa membuka sesi milik guru lain.
- **Status otomatis**: scan sebelum batas terlambat = `Hadir`, lewat batas =
  `Terlambat`. Scan ganda diabaikan dengan pesan. Kode tak dikenal / kode
  milik kelas lain ditolak dengan pesan.
- **Tutup sesi**: siswa yang belum tercatat otomatis menjadi `Alpa`.
- **Scan QR di dalam aplikasi**: kamera browser (`getUserMedia`) + decoder
  jsQR dari CDN. Fallback: ketik kode siswa secara manual, dan **Mode
  Demo/Simulasi Scan** (klik nama siswa = simulasikan scan) untuk pengujian
  tanpa kamera.
- **Cetak kartu QR per kelas**: halaman cetak berisi kartu tiap siswa
  (nama, kelas, QR Code asli yang dibuat server dari kode unik siswa/NIS,
  dan kode terbaca di bawahnya), siap dicetak ke A4 lalu digunting.
- **Rekap** per sesi dengan ringkasan jumlah per status, dan **unduh CSV**
  per sesi.
- UI Bahasa Indonesia, ramah HP.

## Stack

- Python 3 (diuji di Python 3.12) — backend hanya memakai **standard
  library** (`http.server`, `sqlite3`), tanpa framework web.
- Database: **SQLite** satu file (`data/absen.db`, dibuat otomatis).
- Gambar QR kartu: library `qrcode` + `Pillow` (lihat `requirements.txt`).
- Scan QR di browser: `jsQR` 1.4.0 dari CDN jsDelivr
  (`static/app.js`); butuh koneksi internet di HP guru untuk memuatnya,
  dan kamera browser hanya jalan di konteks aman (**HTTPS** atau
  `localhost`).

## Menjalankan secara lokal (perintah persis)

```bash
cd ~/workspace/absen-multi-guru
pip3 install -r requirements.txt
python3 app.py
```

Lalu buka **http://127.0.0.1:8000** di browser.

Port lain: `PORT=8080 python3 app.py`
Lokasi DB lain: `ABSEN_DB=/path/absen.db python3 app.py`

Untuk mencoba dari HP di jaringan Wi-Fi yang sama, jalankan server seperti di
atas (server mendengarkan `0.0.0.0`), lalu buka `http://<IP-komputer>:8000`
dari HP. Catatan: kamera scan kemungkinan **tidak** bisa dipakai lewat
`http://` IP lokal karena browser menuntut HTTPS untuk kamera — pakai mode
demo / ketik manual untuk uji di jaringan lokal, atau deploy dengan HTTPS
(lihat bagian deploy) untuk memakai kamera sungguhan.

## Akun uji lokal

Saat database masih kosong, aplikasi membuat **satu akun admin awal**:

- Username: `admin`
- Password: `admin123`

> **WAJIB:** akun ini hanya untuk pengujian lokal. **Ganti password-nya
> segera** lewat menu **Ganti Password** setelah login pertama, dan
> **jangan pernah memakai akun/password awal ini di produksi**. Dashboard
> admin juga menampilkan peringatan selama password awal belum diganti.

Akun guru **tidak** dibuat otomatis: admin membuatnya dari menu
**Guru → Tambah Guru** (nama, username, password awal), lalu menugaskannya
di menu **Penugasan**.

Alur pemakaian singkat:

1. Login sebagai admin → buat Kelas, Mapel, Guru, Siswa (isi kode unik/NIS
   tiap siswa), dan Penugasan guru.
2. Admin (atau guru) membuka halaman **Cetak Kartu QR** per kelas dari menu
   Siswa, mencetak, dan membagikan kartu ke siswa.
3. Guru login dari HP → menu **Sesi Saya** → buat sesi → scan QR kartu siswa
   di dalam aplikasi (atau demo/manual) → akhiri dengan **Tutup Sesi**.
4. Lihat hasil di menu **Rekap**, unduh CSV bila perlu.

## Struktur data (SQLite, `data/absen.db`)

| Tabel | Isi |
|---|---|
| `users` | akun login: `username` (unik), `password_hash`, `salt`, `role` (`admin`/`guru`), `nama`, `aktif` |
| `classes` | kelas: `nama` (unik) |
| `subjects` | mata pelajaran: `nama` (unik) |
| `students` | siswa: `nama`, `kode` (unik, boleh = NIS; isi QR kartunya kode ini), `class_id` |
| `assignments` | penugasan: `user_id` guru + `class_id` + `subject_id` (unik per kombinasi) |
| `sessions` | sesi absen: kelas, mapel, `teacher_id`, `tanggal`, `jam_mulai`, `batas_terlambat_menit`, `status` (`buka`/`tutup`) |
| `attendance` | catatan: `session_id`, `student_id`, `status` (`Hadir`/`Terlambat`/`Sakit`/`Izin`/`Alpa`), `metode` (`scan`/`manual`/`demo`/`otomatis`), `waktu`, `recorded_by`; unik per (sesi, siswa) |

Session login tersimpan di memori server (bukan di tabel), jadi me-restart
server akan mengeluarkan (logout) semua pengguna. Itu disengaja untuk versi
sederhana ini.

## Menguji

Server harus sedang jalan, lalu:

```bash
cd ~/workspace/absen-multi-guru
# contoh: server uji di port 8765 dengan DB sementara
ABSEN_DB=/tmp/absen-test.db PORT=8765 python3 app.py &
BASE=http://127.0.0.1:8765 python3 test_alur.py
```

`test_alur.py` membuktikan: login admin, pembuatan guru/kelas/mapel/siswa/
penugasan, endpoint gambar QR, login guru, pembuatan sesi, pencatatan lewat
kode siswa (jalur demo/scan), penolakan scan ganda, penutupan sesi menjadi
Alpa, rekap, unduh CSV, dan rekap admin.

## Panduan deploy ke hosting (belum dilakukan — lakukan sendiri)

Aplikasi ini aplikasi Python sederhana satu proses + satu file SQLite.
Aplikasi membaca `PORT` dan alamat database (`ABSEN_DB`) dari environment,
dan mendengarkan `0.0.0.0`, jadi cocok dipasang di layanan hosting umum.
Berkas bantu deploy sudah disiapkan di folder proyek: `render.yaml`
(blueprint Render), `Procfile`, `.python-version`, dan `runtime.txt`
(menetapkan Python 3.12).

> Catatan umum: karena sesi login disimpan di memori satu proses, jalankan
> **satu instance saja**. Jangan menskalakan ke banyak instance tanpa
> mengubah penyimpanan sesi dan database terlebih dulu. Di semua opsi,
> **ganti password admin awal (`admin123`) segera setelah login pertama**,
> sebelum alamat aplikasi dibagikan ke guru.

### Pilihan utama untuk COBA-COBA: Render paket Free (gratis)

Cocok untuk tujuan awal: **menguji scan kamera QR di iPhone lewat HTTPS**
dan mencoba alur aplikasi dari awal sampai akhir dalam sekali pakai.
Daftar tidak butuh kartu kredit (cukup login pakai akun GitHub), HTTPS
otomatis aktif di alamat `*.onrender.com`, dan ada region **Singapore**
(dekat dari Indonesia). Proyek ini sudah dilengkapi `render.yaml`, jadi
pengaturan di Render sebagian besar otomatis.

Langkah-langkahnya:

1. **Buat akun GitHub** (github.com) bila belum punya — Render memasang
   aplikasi dari GitHub.
2. Di GitHub, buat repository baru (mis. nama `absen-multi-guru`), lalu
   unggah **seluruh isi folder proyek ini** ke repository itu. Bisa lewat
   tombol **uploading an existing file** di halaman repository — tidak
   perlu memakai git di komputer. (Folder `data/` berisi database lokal
   Anda; file `.db` sudah dikecualikan oleh `.gitignore` proyek, jadi
   database lokal tidak ikut terunggah.)
3. **Daftar di render.com** dengan tombol **Get Started** → login memakai
   akun GitHub tadi, dan izinkan Render mengakses repository Anda.
4. Di dashboard Render: **New → Web Service** → pilih repository
   `absen-multi-guru`. Render akan membaca `render.yaml` dan mengisi
   sendiri: region Singapore, paket Free, build command
   `pip install -r requirements.txt`, start command `python3 app.py`.
   Klik **Create Web Service** dan tunggu proses build selesai
   (biasanya beberapa menit).
5. Render memberi alamat seperti `https://absen-siswa.onrender.com`.
   Buka alamat itu: halaman login harus muncul. Login `admin` /
   `admin123`, **langsung ganti password**, lalu coba buat kelas, siswa,
   dan sesi contoh.
6. **Uji kameranya dari iPhone**: buka alamat yang sama di Safari iPhone
   (alamatnya sudah HTTPS, jadi izin kamera berjalan normal seperti situs
   biasa), login, buka sesi, dan scan kartu QR siswa.

> #### ⚠️ Risiko paket gratis Render — baca sebelum memakai
>
> 1. **Aplikasi TIDUR setelah 15 menit tidak diakses.** Saat dibuka lagi,
>    tunggu sekitar **1 menit** sampai aplikasi bangun (layar loading
>    lama di kunjungan pertama itu normal, bukan error).
> 2. **SEMUA DATA TERHAPUS setiap kali aplikasi tidur, restart, atau
>    deploy ulang.** Paket gratis Render **tidak menyediakan disk
>    persisten**; file database `data/absen.db` disimpan di penyimpanan
>    sementara yang dibuang platform. Akibatnya: kelas, siswa, akun guru,
>    dan catatan absen yang Anda buat akan **hilang**, dan aplikasi
>    kembali seperti baru (login awal `admin` / `admin123` berlaku lagi).
> 3. Kuota gratis **750 jam per bulan** per workspace — cukup untuk satu
>    aplikasi uji coba, tidak untuk banyak aplikasi sekaligus.
>
> **Kesimpulan: paket gratis ini HANYA untuk tes kamera dan tes alur.**
> Jangan memasukkan data asli sekolah di paket gratis. Untuk pemakaian
> resmi, naik kelas dulu (lihat bagian berikutnya).

Alternatif gratis lain yang sudah dinilai: **Railway paket Free** memberi
kredit pemakaian $1/bulan dan volume persisten 0,5 GB (data tidak hilang
saat restart), tetapi kredit $1 itu bisa **habis di tengah bulan** —
aplikasi berhenti sampai bulan berikutnya. Bila ingin mencobanya: pasang
Volume di service, mount ke `/data`, dan set environment
`ABSEN_DB=/data/absen.db`. **Fly.io tidak lagi punya paket gratis** untuk
akun baru dan pemasangannya lewat perintah terminal + Docker (kurang
cocok untuk pemula). **PythonAnywhere tidak bisa dipakai**: layanan itu
khusus aplikasi WSGI dan melarang aplikasi membuka server/port sendiri,
sedangkan aplikasi ini adalah server `http.server` mandiri — hanya bisa
jalan di sana bila aplikasinya ditulis ulang.

### Jalur naik kelas (bila sekolah jadi memakai beneran)

Begitu uji coba berhasil dan sekolah memutuskan memakai aplikasi ini
secara resmi, naik kelas ke paket berbayar **yang menyediakan disk
persisten** supaya data absen aman. Tiga pilihan, dari yang paling mudah:

**1. Tetap di Render, naik ke Starter (± $7,25/bulan ≈ Rp120 ribu)**
Tidak perlu pindah rumah — cukup ubah paket dan tambahkan disk:

1. Di dashboard Render, buka service `absen-siswa` → **Settings** →
   ubah **Instance Type** dari Free ke **Starter** ($7/bulan, aplikasi
   tidak tidur lagi). Render akan meminta kartu untuk pembayaran.
2. Tambahkan **Disk**: menu **Disks → Add Disk**, nama bebas
   (mis. `data-absen`), **Mount Path** `/var/data`, ukuran **1 GB**
   ($0,25/bulan — sangat cukup untuk database absen).
3. Tambahkan environment variable: `ABSEN_DB` = `/var/data/absen.db`
   (menu **Environment → Add Environment Variable**), lalu simpan —
   aplikasi akan restart dan mulai memakai disk persisten.
4. Setelah naik kelas, isi ulang data sekolah dari awal (data dari masa
   gratis memang sudah terhapus), ganti password admin, dan mulai pakai.
   Mulai titik ini data **tidak** hilang saat restart/deploy.

   Isi `render.yaml` versi berbayar (bila ingin mengatur lewat file):

   ```yaml
   services:
     - type: web
       name: absen-siswa
       env: python
       region: singapore
       plan: starter
       buildCommand: pip install -r requirements.txt
       startCommand: python3 app.py
       healthCheckPath: /login
       envVars:
         - key: PYTHON_VERSION
           value: 3.12.3
         - key: ABSEN_DB
           value: /var/data/absen.db
       disk:
         name: data-absen
         mountPath: /var/data
         sizeGB: 1
   ```

**2. Railway paket Hobby ($5/bulan, termasuk kredit pemakaian $5)**
Deploy dari repository GitHub yang sama, tambahkan **Volume** (mount
`/data`, tarif $0,15/GB/bulan) dan set `ABSEN_DB=/data/absen.db`.
Aplikasi kecil ini pemakaian sumber dayanya
rendah, jadi total tagihan umumnya tertahan di angka minimum $5/bulan.
HTTPS otomatis; region Singapore tersedia. Volume Railway juga dicadangkan
otomatis oleh platform.

**3. VPS (data sepenuhnya di tangan sendiri, ± Rp50–120 ribu/bulan)**
Disk VPS adalah disk sungguhan, jadi database persisten secara alami.
Penyedia Indonesia seperti **IDCloudHost** (Cloud VPS mulai sekitar
Rp50–87 ribu/bulan) menerima pembayaran transfer bank, e-wallet
(GoPay/OVO/dll.), dan QRIS, dengan dukungan Bahasa Indonesia — tidak
perlu kartu kredit. VPS luar seperti Hetzner mulai sekitar €4–6/bulan
tetapi umumnya butuh kartu. Kekurangannya: pemasangannya paling teknis
(SSH, systemd, HTTPS sendiri) — minta dibantu orang yang paham server,
atau ikuti ringkasan teknis ini:

1. Salin folder proyek ke VPS, pasang Python 3 dan dependensi:
   `pip3 install -r requirements.txt`
2. Jalankan sebagai service, contoh `systemd`:

   ```ini
   [Unit]
   Description=Absen Siswa Multi-Guru
   After=network.target

   [Service]
   WorkingDirectory=/opt/absen-multi-guru
   Environment=PORT=8000
   ExecStart=/usr/bin/python3 /opt/absen-multi-guru/app.py
   Restart=always
   User=www-data

   [Install]
   WantedBy=multi-user.target
   ```

3. Pasang reverse proxy (Nginx/Caddy) di depan aplikasi dan **aktifkan
   HTTPS** (mis. sertifikat gratis Let's Encrypt lewat Caddy/certbot).
   HTTPS **wajib** agar kamera HP guru bisa dipakai untuk scan.
4. Pastikan file `data/absen.db` berada di direktori yang bisa ditulis
   user service, dan **backup file itu secara berkala** — seluruh data
   sekolah ada di file tersebut.

## Keterbatasan yang diketahui

- Kamera fisik **tidak dapat diuji di VM pengembangan**; logika scan diuji
  lewat jalur kode yang sama persis dengan hasil decode QR (form kode →
  pencatatan), plus mode demo. Uji kamera sungguhan harus dilakukan di HP
  pada deployment HTTPS.
- Decoder jsQR dimuat dari CDN — HP guru butuh internet saat membuka
  halaman sesi. Tanpa internet, gunakan ketik kode manual.
- Belum ada: impor siswa massal (CSV), lupa/reset password lewat email,
  notifikasi orang tua, multi-sekolah, dan audit log. Data siswa adalah
  data anak — amankan akses server dan backup-nya.
