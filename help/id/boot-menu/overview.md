# Konstruktor menu boot

Buat menu startup yang terlihat dari entri-entri independen. Pilih template, lalu sesuaikan entri tersebut dengan kontrol bertipe. Entri yang dinonaktifkan tetap ada di proyek tetapi tidak akan ditampilkan pada menu yang dihasilkan.

## Menu sumber yang sudah ada

Sebelum kustomisasi, konstruktor membaca entri MiniOS yang dikenali, nilai default dan timeout-nya, serta parameter yang didukung dari menu GRUB atau SYSLINUX asli yang berlaku. Mengedit entri yang diimpor hanya akan mengganti parameter yang diwakili oleh kontrol bertipe. Argumen sumber lainnya tetap berada di template boot. Dalam menu multibahasa, setiap bahasa mempertahankan argumen lokal, zona waktu, dan keyboard sumbernya kecuali Anda secara eksplisit menimpanya.

## Cara sebuah entri dirakit

Template menyediakan perilaku dasar MiniOS. Argumen kernel global untuk pengguna ahli diterapkan berikutnya, diikuti oleh opsi bertipe dan parameter tambahan untuk entri ini. Untuk opsi MiniOS berpasangan kunci-nilai yang berulang, nilai terakhir yang digunakan.

## Template sesi

Resume menggunakan perchdir=resume. New menggunakan perchdir=new. Choose menggunakan perchdir=ask.
Fresh tidak memiliki pemilih persistensi. Copy to RAM menggunakan toram.

Anda dapat membuat beberapa entri dari template yang sama.

## Jenis persistensi

Native menyimpan perubahan di dalam direktori. Dynfilefs menggunakan kontainer yang dapat diperluas, Raw menggunakan image berukuran tetap, dan LUKS menggunakan kontainer terenkripsi. SquashFS melanjutkan sesi terkompresi yang sudah ada. Initramfs saat ini tidak dapat membuat sesi SquashFS baru.

## Pengaturan yang saling bergantung

Kontrol akan menjadi tidak tersedia saat tidak berlaku. Menonaktifkan zRAM akan menonaktifkan kontrol kompresi dan ukurannya. Persistensi Native dan SquashFS tidak menggunakan kolom ukuran kontainer.

## Penyelesaian dan input ahli

Filter modul akan melengkapi dari modul yang terdeteksi di sumber yang dipilih. Kolom lokal, zona waktu, dan keyboard akan melengkapi dari data sistem yang terpasang. Gunakan parameter tambahan hanya untuk opsi yang tidak memiliki kontrol bertipe. Argumen tidak dikenal yang dimuat dari proyek lama akan tetap dipertahankan di sana.

## Default dan nama

Menu yang dikustomisasi memiliki tepat satu entri default. Menonaktifkan default akan otomatis memilih entri lain yang aktif. Nama yang dikosongkan akan mempertahankan judul dari sumber atau template. Nama kustom ASCII dapat digunakan di menu multibahasa; menu satu bahasa dapat menggunakan karakter yang didukung oleh encoding menu bootloader tersebut.
