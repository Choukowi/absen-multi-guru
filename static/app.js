// Scan QR di gerbang: kamera browser (getUserMedia) + jsQR (CDN).
// Kamera MENYALA TERUS: sekali dinyalakan lewat tombol "Nyalakan Kamera",
// stream & loop scan berjalan sampai petugas menekan "Matikan Kamera" atau
// meninggalkan halaman. Setiap QR terbaca dikirim ke /api/scan lewat fetch
// (mode ajax) TANPA memuat ulang halaman — banner hasil besar ditampilkan
// dan daftar "Sudah Scan Hari Ini" diperbarui di tempat, jadi kamera tidak
// pernah mati di tengah antrean.
// Pengaman anti-dobel: JEDA_GLOBAL_MS (jeda singkat setelah tiap catatan)
// dan DINGIN_KODE_MS (masa dingin per kode), supaya kartu yang masih
// tertahan di depan kamera tidak terkirim berulang-ulang.
// Fallback: ketik NIS / kode guru secara manual (form tetap berfungsi
// sebagai kirim halaman penuh bila JavaScript tidak jalan).
(function () {
  var video = document.getElementById('cam');
  if (!video) return;
  var btnCam = document.getElementById('btnCam');
  var btnStop = document.getElementById('btnStop');
  var status = document.getElementById('scanStatus');
  var kodeInput = document.getElementById('kodeInput');
  var form = document.getElementById('scanForm');
  var hasilEl = document.getElementById('hasilScan');
  var daftarEl = document.getElementById('daftarDatang');
  var jumlahEl = document.getElementById('jumlahDatang');

  var JEDA_GLOBAL_MS = 1200;  // jeda sebelum kode berikutnya boleh diterima
  var DINGIN_KODE_MS = 6000;  // kode yang sama tidak dikirim ulang selama ini
  var stream = null, raf = null, scanning = false;
  var jedaSampai = 0;          // timestamp: abaikan hasil decode sebelum ini
  var terakhirPerKode = {};    // kode -> timestamp terakhir kali dikirim
  var sedangKirim = false;
  var canvas = document.createElement('canvas');
  var ctx = canvas.getContext('2d');

  function tampilHasil(jenis, judul, pesan) {
    if (!hasilEl) return;
    hasilEl.innerHTML = '';
    var div = document.createElement('div');
    div.className = 'hasil hasil-' + jenis;
    var j = document.createElement('div');
    j.className = 'hasil-nama';
    j.textContent = judul;
    var p = document.createElement('div');
    p.className = 'hasil-meta';
    p.textContent = pesan;
    div.appendChild(j);
    div.appendChild(p);
    hasilEl.appendChild(div);
  }

  function perbaruiDaftar(jumlah, itemsHtml) {
    if (daftarEl && typeof itemsHtml === 'string') daftarEl.innerHTML = itemsHtml;
    if (jumlahEl && typeof jumlah === 'number') jumlahEl.textContent = jumlah;
  }

  // Kirim satu kode ke server. dariKamera=true menerapkan jeda global +
  // masa dingin per kode; entri manual selalu langsung dikirim.
  function catatKode(kode, dariKamera) {
    kode = (kode || '').trim();
    if (!kode || sedangKirim) return;
    var kini = Date.now();
    if (dariKamera) {
      if (kini < jedaSampai) return;
      if (terakhirPerKode[kode] && kini - terakhirPerKode[kode] < DINGIN_KODE_MS) return;
    }
    terakhirPerKode[kode] = kini;
    jedaSampai = kini + JEDA_GLOBAL_MS;
    sedangKirim = true;
    status.textContent = 'QR terbaca: ' + kode + ' - mencatat...';
    var body = new URLSearchParams();
    body.set('kode', kode);
    body.set('ajax', '1');
    fetch('/api/scan', { method: 'POST', body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        tampilHasil(d.jenis, d.judul, d.pesan);
        perbaruiDaftar(d.jumlah, d.items);
        status.textContent = scanning
          ? 'Kamera tetap menyala. Arahkan kartu berikutnya...'
          : 'Tercatat. Nyalakan kamera atau ketik kode berikutnya.';
      })
      .catch(function () {
        tampilHasil('error', 'Gagal mencatat.',
          'Koneksi ke server bermasalah. Coba lagi, atau ketik kode secara manual.');
        status.textContent = 'Gagal mencatat kode ' + kode + '. Coba lagi.';
      })
      .then(function () { sedangKirim = false; });
  }

  function tick() {
    if (!scanning) return;
    if (Date.now() >= jedaSampai && video.readyState === video.HAVE_ENOUGH_DATA) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      if (window.jsQR) {
        var code = window.jsQR(img.data, img.width, img.height);
        if (code && code.data) catatKode(code.data, true);
      }
    }
    raf = requestAnimationFrame(tick);
  }

  function matikanKamera() {
    scanning = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    if (video.srcObject) video.srcObject = null;
    btnCam.disabled = false;
    btnStop.disabled = true;
  }

  btnCam.addEventListener('click', function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = 'Browser ini tidak mendukung kamera (getUserMedia). Pakai ketik kode manual di bawah.';
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
      .then(function (s) {
        stream = s;
        video.srcObject = s;
        scanning = true;
        btnCam.disabled = true;
        btnStop.disabled = false;
        status.textContent = 'Kamera menyala dan akan TETAP menyala. Arahkan kartu QR siswa/guru ke kamera - tiap kartu langsung tercatat. Tekan "Matikan Kamera" bila sudah selesai.';
        raf = requestAnimationFrame(tick);
      })
      .catch(function (e) {
        status.textContent = 'Kamera ditolak/gagal: ' + e.message + ' Pakai ketik kode manual di bawah.';
      });
  });
  btnStop.addEventListener('click', function () {
    matikanKamera();
    status.textContent = 'Kamera dimatikan. Tekan "Nyalakan Kamera" untuk mulai lagi, atau ketik kode manual di bawah.';
  });
  window.addEventListener('pagehide', matikanKamera);

  // Ketik manual: kirim lewat fetch juga, halaman tidak dimuat ulang.
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    catatKode(kodeInput.value, false);
    kodeInput.value = '';
    kodeInput.focus();
  });
})();
