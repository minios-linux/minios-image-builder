# Sesi dan penyimpanan

Pilih cara entri ini menemukan dan menyimpan perubahan yang bersifat permanen.

## Pemilihan sesi

Template entri mengontrol apakah MiniOS melanjutkan sesi terbaru, membuat sesi baru, atau menanyakan saat startup. Jenis penyimpanan tidak menggantikan pilihan template tersebut.

## Tipe penyimpanan

- **Otomatis** mempertahankan mode template atau sesi yang disimpan.
- **Native** menyimpan perubahan langsung di direktori pada filesystem dengan izin Unix, seperti ext4, XFS, atau Btrfs.
- **Dynfilefs** menggunakan kontainer yang dapat diperluas.
- **Raw** menggunakan image berukuran tetap.
- **LUKS** menggunakan kontainer terenkripsi.
- **SquashFS** melanjutkan sesi terkompresi yang sudah ada.

Initramfs saat ini dapat melanjutkan tetapi tidak dapat membuat sesi SquashFS.

## Kapasitas

**Ukuran kontainer** hanya berlaku untuk sesi yang didukung kontainer, sehingga dinonaktifkan untuk Native dan SquashFS. **Ruang kosong yang dipertahankan** menyisakan ruang pada perangkat persistensi agar perubahan yang disimpan tidak memenuhi seluruhnya.

Opsi boot yang sesuai adalah `perchmode=`, `perchsize=`, dan `perchreserve=`.

## Salin ke RAM

`toram=full` menyalin seluruh sistem ke memori. `toram=trim` hanya menyalin kumpulan modul yang difilter. Ini memungkinkan perangkat boot dilepas, tetapi membutuhkan RAM yang cukup untuk data yang disalin.
