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

// ---------------------------------------------------------------------------
// Toggle "mata" untuk SEMUA kolom password: bungkus tiap input[type=password]
// dengan .pw-wrap dan tambahkan tombol mata di dalamnya. Klik tombol untuk
// melihat isi password (type menjadi "text") atau menyembunyikannya lagi
// (kembali "password"), supaya pengguna bisa mengecek salah ketik.
// Blok ini BERDIRI SENDIRI: berjalan di halaman mana pun app.js dimuat
// (login, ganti password, form guru, dst.) dan tidak tergantung elemen QR.
// Tombol selalu type="button" agar tidak pernah mengirim formulir.
// ---------------------------------------------------------------------------
(function () {
  var IKON_MATA = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" '
    + 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/>'
    + '<circle cx="12" cy="12" r="3"/></svg>';
  var IKON_MATA_CORET = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" '
    + 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M17.94 17.94A10.61 10.61 0 0 1 12 19c-6.5 0-10-7-10-7a17.63 '
    + '17.63 0 0 1 4.06-4.94M9.9 4.24A9.12 9.12 0 0 1 12 5c6.5 0 10 7 10 7a17.65 '
    + '17.65 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/>'
    + '<path d="M2 2l20 20"/></svg>';

  function pasangToggle(input) {
    if (input.getAttribute('data-pw-toggle')) return;
    input.setAttribute('data-pw-toggle', '1');
    var bungkus = document.createElement('span');
    bungkus.className = 'pw-wrap';
    input.parentNode.insertBefore(bungkus, input);
    bungkus.appendChild(input);
    var tombol = document.createElement('button');
    tombol.type = 'button';
    tombol.className = 'pw-toggle';
    tombol.setAttribute('aria-label', 'Tampilkan password');
    tombol.innerHTML = IKON_MATA;
    bungkus.appendChild(tombol);
    tombol.addEventListener('click', function () {
      var terlihat = input.type === 'text';
      input.type = terlihat ? 'password' : 'text';
      tombol.innerHTML = terlihat ? IKON_MATA : IKON_MATA_CORET;
      tombol.setAttribute('aria-label',
        terlihat ? 'Tampilkan password' : 'Sembunyikan password');
      input.focus();
    });
  }

  function pasangSemua() {
    var kolom = document.querySelectorAll('input[type="password"]');
    for (var i = 0; i < kolom.length; i++) pasangToggle(kolom[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', pasangSemua);
  } else {
    pasangSemua();
  }
})();

// ---------------------------------------------------------------------------
// Pemilih warna kustom di Pengaturan Tampilan: swatch ".kustom" berisi radio
// tersembunyi (value "kustom") + <input type="color"> yang terlihat. Mengetuk
// color picker TIDAK mencentang radio itu — browser tidak meneruskan klik
// pada elemen interaktif di dalam <label> ke radio — jadi warna hasil picker
// tidak pernah terkirim (form mengirim preset yang masih tercentang).
// Blok ini memperbaikinya: pada "input" dan "change" picker, radio saudara
// di label yang sama dicentang; pada "change" formulir langsung dikirim,
// jadi warna kustom terterap begitu dipilih, tanpa perlu mencari radio atau
// menekan Simpan. Menekan Simpan manual sesudah memilih juga tetap
// berfungsi, karena radio sudah tercentang oleh penangan "input".
// Blok ini BERDIRI SENDIRI: hanya menyentuh .swatch.kustom, tidak mengubah
// perilaku blok scan QR maupun toggle mata password.
// ---------------------------------------------------------------------------
(function () {
  function pasangPicker(picker) {
    var label = picker.closest ? picker.closest('label.swatch.kustom') : null;
    if (!label) {
      var el = picker.parentNode;
      while (el && !(el.tagName === 'LABEL' &&
                     (' ' + el.className + ' ').indexOf(' kustom ') >= 0)) {
        el = el.parentNode;
      }
      label = el;
    }
    if (!label) return;
    var radio = label.querySelector('input[type="radio"]');
    if (!radio) return;
    function centangRadio() { radio.checked = true; }
    picker.addEventListener('input', centangRadio);
    picker.addEventListener('change', function () {
      centangRadio();
      var form = picker.form || (label.closest ? label.closest('form') : null);
      if (!form) return;
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else HTMLFormElement.prototype.submit.call(form);
    });
  }

  function pasangSemua() {
    var pickers = document.querySelectorAll(
      '.swatch.kustom input[type="color"]');
    for (var i = 0; i < pickers.length; i++) pasangPicker(pickers[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', pasangSemua);
  } else {
    pasangSemua();
  }
})();
