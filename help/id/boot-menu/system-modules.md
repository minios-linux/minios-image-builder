# Sistem dan modul

Pilih modul dan lingkungan awal untuk entri ini.

## Penyaring modul

**Load modules** membatasi pemuatan hanya pada nama atau rentang modul yang cocok.
**Skip modules** mengecualikan modul yang cocok. Saran berasal dari modul
yang terdeteksi di sumber MiniOS yang dipilih.

Kontrol ini menghasilkan `load=FILTER` dan `noload=FILTER`. Gunakan hanya bentuk filter
yang didukung oleh initramfs MiniOS.

## Lingkungan startup

Pertahankan gambar default, mulai desktop grafis, gunakan konsol teks, atau
masuk ke mode penyelamatan. Mode teks dan mode penyelamatan ditujukan untuk administrasi
dan pemecahan masalah, bukan untuk penggunaan desktop normal.

## Kompatibilitas grafis

Mode kompatibilitas menambahkan opsi kernel Linux `nomodeset`. Gunakan saat mode kernel normal
menghalangi sistem grafis untuk memulai. Ini dapat menurunkan resolusi
dan akselerasi tampilan.

## Automount disk

Aktifkan pemasangan otomatis hanya jika sesi harus menampilkan filesystem lain
yang terpasang setelah startup.
