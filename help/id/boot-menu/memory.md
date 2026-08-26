# Memori

Kelola swap zRAM terkompresi yang dibuat untuk entri ini.

## Status zRAM

**Otomatis** menjaga pengaturan memori MiniOS secara default. **Nonaktif** menambahkan `nozram` dan menonaktifkan kontrol kompresi serta ukuran karena tidak lagi berlaku.

## Kompresi

`zramcomp=` memilih algoritma kompresi. Pilihan yang tersedia adalah `lzo`, `lzo-rle`, `lz4`, `lz4hc`, dan `zstd`. Ketersediaan algoritma juga tergantung pada kernel yang sedang berjalan.

## Ukuran

`zramsize=` mengatur ukuran zRAM dalam MiB. Biarkan kolom kosong agar MiniOS menghitung ukuran secara otomatis. Nilai yang lebih besar bukan berarti memori fisik gratis: halaman yang terkompresi tetap menggunakan RAM.
