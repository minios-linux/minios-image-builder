# Boot- und Kernel-Parameter

Geben Sie die Parameter durch Leerzeichen getrennt ein. Die Autovervollständigung schlägt gängige Optionen für MiniOS und Linux vor. Globale Parameter gelten für jeden erstellten oder beibehaltenen MiniOS-Sitzungseintrag. Eintragsspezifische Parameter werden später angehängt und können eine wiederholte MiniOS-Schlüssel-Wert-Option überschreiben.

## Sitzung und Persistenz

Diese Optionen ermöglichen dauerhafte Änderungen, das Fortsetzen der letzten kompatiblen Sitzung, das Erstellen einer neuen Sitzung, eine Abfrage beim Start oder die direkte Auswahl einer nummerierten Sitzung: perch, perchdir=resume, perchdir=new, perchdir=ask und perchdir=NUMBER.

Speichermodi sind native, dynfilefs, raw, luks und squashfs. SquashFS kann eine vorhandene komprimierte Sitzung fortsetzen, aber das aktuelle initramfs kann keine neue erstellen. Containergrößen akzeptieren die Suffixe MB, GB oder TB. Der reservierte freie Speicherplatz wird in MiB gemessen; der Standardwert ist 256 und das Maximum beträgt 4096.

Die entsprechenden Optionen sind perchmode=MODE, perchsize=SIZE und perchreserve=MIB.

## In den RAM kopieren

Die Optionen toram, toram=full und toram=trim kopieren das Standard-, vollständige oder gefilterte System in den RAM.

## Module

Der load-Filter lädt nur passende Module; der noload-Filter schließt passende Module aus. Filter können Modulnamen, Listen oder von initramfs unterstützte MiniOS-Bereiche enthalten. Die Optionen sind load=FILTER und noload=FILTER.

## Speicher und Grafik

Die Speicheroptionen deaktivieren zRAM, wählen lzo, lzo-rle, lz4, lz4hc oder zstd-Komprimierung und setzen die zRAM-Größe in MiB. Textmodus startet ohne grafische Desktop-Umgebung. Nomodeset deaktiviert das normale Kernel-Mode-Setting und ist nützlich zur Fehlerbehebung bei Grafikproblemen. Die Optionen sind nozram, zramcomp=ALGORITHM, zramsize=MIB, text und nomodeset.

## Quelle und Lokalisierung

Diese Optionen wählen die MiniOS-Datenquelle und überschreiben Sprach-, Zeitzonen- und Tastatureinstellungen für den Eintrag. Die Optionen sind from=SOURCE, from=askdisk, locales=LOCALE, timezone=ZONE und keyboard-layouts=LAYOUT.

## Diagnose

Quiet reduziert Bootmeldungen. Debug aktiviert zusätzliche Diagnosen. Verwenden Sie nur Parameter, die von Linux, dem MiniOS-initramfs oder live-config verstanden werden. Die Optionen sind quiet und debug.
