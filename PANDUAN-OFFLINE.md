# Panduan Mode Offline — Aplikasi Absen (Gerbang Global)

Aplikasi absen bisa jalan **penuh tanpa internet** di sekolah. Semua data
tersimpan di satu laptop/PC sekolah (database SQLite lokal), dan scan QR
kamera tetap jalan karena pustaka `jsQR` sudah tersimpan di dalam aplikasi
(`static/jsQR.js`) — tidak ada lagi yang diambil dari internet.

Yang **tidak jalan** saat offline (butuh internet): kirim password wali via
WhatsApp (Fonnte / wa.me). Absensinya sendiri — scan, dashboard, tabel,
unduh Excel, backup — jalan penuh.

## Yang dibutuhkan

1. Satu **laptop/PC di sekolah** jadi "server" (Windows, Mac, atau Linux).
2. **Python 3.12** terpasang di laptop itu.
3. Satu **HP/tablet petugas** di gerbang (opsional — bisa juga pakai kamera
   laptop itu sendiri, lihat langkah 4).
4. **WiFi sekolah** atau **hotspot dari laptop** — yang penting HP dan
   laptop satu jaringan yang sama. WiFi-nya **tidak perlu ada internetnya**.

> Catatan: instalasi Python dan paket-paket di langkah 1 butuh internet
> **satu kali saja**. Sesudah terpasang, pemakaian hariannya 100% offline.

## Langkah 1 — Siapkan laptop server (sekali saja)

1. Salin folder aplikasi ini ke laptop sekolah (mis. dari flashdisk, atau
   unduh dari GitHub: `https://github.com/Choukowi/absen-multi-guru`).
2. Pasang Python 3.12 (dari `python.org`) — saat instalasi di Windows,
   centang **"Add Python to PATH"**.
3. Buka terminal/command prompt di dalam folder aplikasi, lalu jalankan:

   ```bash
   pip install -r requirements.txt
   ```

   (Hanya sekali; kalau paketnya sudah terpasang, langkah ini bisa dilewati.)

## Langkah 2 — Jalankan servernya (tiap pagi)

Di dalam folder aplikasi, jalankan:

```bash
python3 app.py
```

Di Windows: `python app.py`. Kalau berhasil akan muncul tulisan
`Aplikasi Absen Siswa (Gerbang Global) jalan di http://127.0.0.1:8000`.

**Biarkan jendela terminal ini tetap terbuka** selama jam absen. Untuk
berhenti: tekan `Ctrl+C` di terminal itu.

## Langkah 3 — Cari alamat IP laptop

Supaya HP petugas bisa membuka aplikasi dari laptop ini:

- **Windows:** buka Command Prompt, ketik `ipconfig`, cari baris
  `IPv4 Address` (contoh: `192.168.1.5`).
- **Mac:** Pengaturan → Wi-Fi → klik jaringan yang dipakai → lihat alamat IP.
- **Linux:** ketik `hostname -I` di terminal.

Alamat aplikasinya menjadi: `http://192.168.1.5:8000` (ganti dengan IP
laptop Bos; port tetap `8000`).

## Langkah 4 — Scan di gerbang (pilih salah satu cara)

### Cara A — Kamera laptop itu sendiri (paling mudah, tanpa setting apa pun)

Buka browser di laptop server, pergi ke `http://127.0.0.1:8000`, login,
buka halaman **Scan Gerbang**, klik **Nyalakan Kamera**. Kamera laptop
membaca kartu QR siswa/guru yang dihadapkan ke layar.

### Cara B — Kamera HP petugas

1. Sambungkan HP ke **WiFi yang sama** dengan laptop (WiFi sekolah, atau
   hotspot yang dinyalakan dari laptop).
2. Di HP, buka browser ke `http://192.168.1.5:8000` (IP laptop Bos).
3. Login, buka **Scan Gerbang**, klik **Nyalakan Kamera**.

> **Penting — soal kamera HP:** browser modern hanya mengizinkan kamera di
> alamat yang aman (HTTPS atau `localhost`). Alamat `http://192.168.x.x`
> dianggap tidak aman, jadi kamera HP bisa ditolak browsernya. Solusinya:
>
> - **Opsi 1 (disarankan):** di HP, buka Chrome lalu ketik
>   `chrome://flags`, cari **"Insecure origins treated as secure"**,
>   isi dengan alamat aplikasi (mis. `http://192.168.1.5:8000`), pilih
>   **Enabled**, lalu **Relaunch**. Sesudah itu kamera HP bisa dipakai.
>   Setting ini sekali saja per HP.
> - **Opsi 2:** pakai **Cara A** (kamera laptop) untuk scan, HP hanya untuk
>   melihat dashboard/tabel.
> - **Opsi 3:** kalau kamera tetap tidak mau menyala, ketik **NIS siswa /
>   kode guru secara manual** di kolom bawah tombol kamera, lalu tekan
>   **Catat dari Kode** — cara ini selalu jalan, tanpa kamera.

## Langkah 5 — Selesai jam absen

1. Di laptop server, tekan `Ctrl+C` di terminal untuk menghentikan aplikasi.
2. **Backup harian (penting):** sebelum menutup, login sebagai admin →
   menu **Backup & Pulihkan** → **Unduh Backup**. Simpan file backup-nya
   (berisi semua data hari itu).
3. Kalau nanti ada internet lagi, backup itu bisa **dipulihkan** ke versi
   online (`kirinabsen.pythonanywhere.com`) lewat menu yang sama, supaya
   salinan online dan salinan sekolah tetap sama.

## Yang tetap berlaku saat offline

- Jam absensi sama persis: scan sampai jam batas hadir = **Hadir**, lewat =
  **Terlambat**, scan ditutup jam batas alpa (standar 07:00 / 09:00, bisa
  diubah admin di Pengaturan Tampilan → Jam Absensi).
- Scan ganda diabaikan ("Sudah tercatat"); Sakit/Izin manual tidak ditimpa
  scan.
- Dashboard, Laporan Hari Ini, tabel mingguan, dan unduh Excel semuanya
  jalan seperti biasa.

## Kalau ada masalah

| Gejala | Periksa |
|---|---|
| HP tidak bisa membuka alamat IP laptop | HP dan laptop satu WiFi/hotspot yang sama? Firewall Windows mengizinkan Python? (saat pertama jalan, Windows biasanya bertanya — klik **Allow**.) |
| Kamera HP tidak menyala | Lihat Cara B opsi 1 (chrome://flags) atau pakai Cara A / ketik manual. |
| `python3 app.py` error `No module named ...` | Jalankan dulu `pip install -r requirements.txt` (butuh internet sekali). |
| Halaman tidak muncul di laptop sendiri | Pastikan terminal masih jalan dan buka `http://127.0.0.1:8000`. |
| Data kemarin hilang | Data tersimpan di `data/absen.db` di folder aplikasi — jangan hapus folder `data`. Pulihkan dari file backup harian bila perlu. |
