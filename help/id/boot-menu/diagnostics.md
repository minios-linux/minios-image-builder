# Diagnostik dan opsi lanjutan

Sesuaikan pencatatan boot atau tambahkan parameter yang tidak tersedia pada kontrol yang dapat diketik.

## Pesan boot

**Sembunyikan pesan boot rutin** menambahkan `quiet`. **Aktifkan log diagnostik** menambahkan
`debug`. Keduanya dapat diaktifkan secara independen. Nonaktifkan `quiet` ketika pesan boot
yang detail lebih berguna daripada tampilan startup yang bersih.

## Parameter tambahan

Masukkan hanya parameter Linux, MiniOS initramfs, atau live-config yang tidak memiliki
kontrol bertipe. Parameter tidak dikenal yang sudah ada akan dipertahankan di sini, dan pelengkapan
akan terus menyarankan opsi umum.

Parameter khusus entri akan ditambahkan setelah argumen kernel template dan global.
Untuk opsi MiniOS `key=value` yang berulang, nilai terakhir biasanya
yang digunakan. Parameter ahli yang tidak valid atau bertentangan dapat mencegah startup.
