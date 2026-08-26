# Parameter boot dan kernel

Ketik parameter yang dipisahkan dengan spasi. Fitur pelengkapan akan menyarankan opsi MiniOS dan Linux yang umum. Parameter global berlaku untuk setiap entri sesi MiniOS yang dibuat atau disimpan. Parameter khusus entri akan ditambahkan kemudian dan dapat menimpa opsi key-value MiniOS yang sama jika diulang.

## Sesi dan persistensi

Opsi-opsi ini memungkinkan perubahan yang persisten, melanjutkan sesi kompatibel terbaru,
membuat sesi baru, menanyakan saat startup, atau memilih sesi bernomor secara langsung:
perch, perchdir=resume, perchdir=new, perchdir=ask, dan perchdir=NUMBER.

Mode penyimpanan adalah native, dynfilefs, raw, luks, dan squashfs. SquashFS dapat
melanjutkan sesi terkompresi yang sudah ada, tetapi initramfs saat ini tidak dapat membuat
sesi baru. Ukuran kontainer menerima akhiran MB, GB, atau TB. Ruang kosong yang dicadangkan
diukur dalam MiB; nilai default-nya adalah 256 dan maksimum adalah 4096.

Opsi yang sesuai adalah perchmode=MODE, perchsize=SIZE, dan
perchreserve=MIB.

## Salin ke RAM

Opsi toram, toram=full, dan toram=trim menyalin sistem default, lengkap, atau
terfilter ke RAM.

## Modul

Filter load hanya memuat modul yang cocok; filter noload mengecualikan
modul yang cocok. Filter dapat berisi nama modul, daftar, atau rentang MiniOS
yang didukung oleh initramfs. Opsi-opsinya adalah load=FILTER dan noload=FILTER.

## Memori dan grafis

Opsi memori menonaktifkan zRAM, memilih kompresi lzo, lzo-rle, lz4, lz4hc, atau zstd,
dan mengatur ukuran zRAM dalam MiB. Mode teks memulai tanpa desktop grafis.
Nomodeset menonaktifkan pengaturan mode kernel normal dan berguna
untuk pemecahan masalah grafis. Opsi-opsinya adalah nozram, zramcomp=ALGORITHM,
zramsize=MIB, text, dan nomodeset.

## Sumber dan lokalisasi

Opsi-opsi ini memilih sumber data MiniOS dan menimpa pengaturan bahasa, zona waktu,
dan keyboard untuk entri. Opsi-opsinya adalah from=SOURCE, from=askdisk,
locales=LOCALE, timezone=ZONE, dan keyboard-layouts=LAYOUT.

## Diagnostik

Quiet mengurangi pesan boot. Debug mengaktifkan diagnostik tambahan. Gunakan hanya
parameter yang dipahami oleh Linux, initramfs MiniOS, atau live-config. Opsi-opsinya adalah quiet dan debug.
