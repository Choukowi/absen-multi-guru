// Scan QR di gerbang: kamera browser (getUserMedia) + jsQR (CDN).
// QR terbaca -> isi kolom kode lalu kirim form ke /api/scan (server mencatat
// kehadiran hari itu). Fallback: ketik NIS / kode guru secara manual.
(function () {
  var video = document.getElementById('cam');
  if (!video) return;
  var btnCam = document.getElementById('btnCam');
  var btnStop = document.getElementById('btnStop');
  var status = document.getElementById('scanStatus');
  var kodeInput = document.getElementById('kodeInput');
  var form = document.getElementById('scanForm');
  var stream = null, raf = null, scanning = false;

  function stop() {
    scanning = false;
    if (raf) cancelAnimationFrame(raf);
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    btnCam.disabled = false; btnStop.disabled = true;
  }

  function tick() {
    if (!scanning) return;
    if (video.readyState === video.HAVE_ENOUGH_DATA) {
      var canvas = document.createElement('canvas');
      canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      var ctx = canvas.getContext('2d');
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      if (window.jsQR) {
        var code = window.jsQR(img.data, img.width, img.height);
        if (code && code.data) {
          status.textContent = 'QR terbaca: ' + code.data + ' - mencatat...';
          kodeInput.value = code.data;
          stop();
          form.submit();
          return;
        }
      }
    }
    raf = requestAnimationFrame(tick);
  }

  btnCam.addEventListener('click', function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = 'Browser ini tidak mendukung kamera (getUserMedia). Pakai ketik kode manual di bawah.';
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
      .then(function (s) {
        stream = s; video.srcObject = s; scanning = true;
        btnCam.disabled = true; btnStop.disabled = false;
        status.textContent = 'Kamera aktif. Arahkan kartu QR siswa/guru ke kamera...';
        raf = requestAnimationFrame(tick);
      })
      .catch(function (e) {
        status.textContent = 'Kamera ditolak/gagal: ' + e.message + ' Pakai ketik kode manual di bawah.';
      });
  });
  btnStop.addEventListener('click', stop);
})();
