# Sitzung und Speicherung

Wählen Sie aus, wie dieser Eintrag dauerhafte Änderungen findet und speichert.

## Sitzungswahl

Die Eintragsvorlage steuert, ob MiniOS die letzte Sitzung fortsetzt, eine neue erstellt oder beim Start fragt. Der Speichertyp ersetzt diese Vorlagenauswahl nicht.

## Speichertyp

- **Automatisch** behält den Vorlagen- oder Gespeicherte-Sitzung-Modus bei.
- **Nativ** speichert Änderungen direkt in einem Verzeichnis auf einem Dateisystem mit Unix-Berechtigungen, wie ext4, XFS oder Btrfs.
- **Dynfilefs** verwendet einen erweiterbaren Container.
- **Raw** verwendet ein Abbild mit fester Größe.
- **LUKS** nutzt einen verschlüsselten Container.
- **SquashFS** setzt eine bestehende komprimierte Sitzung fort.

Das aktuelle initramfs kann eine SquashFS-Sitzung fortsetzen, aber nicht erstellen.

## Kapazität

**Containergröße** gilt nur für containerbasierte Sitzungen und ist daher für Nativ und SquashFS deaktiviert. **Freier Speicher zum Behalten** reserviert Platz auf dem Persistenzgerät, damit gespeicherte Änderungen es nicht vollständig füllen.

Die entsprechenden Boot-Optionen sind `perchmode=`, `perchsize=` und `perchreserve=`.

## In den RAM kopieren

`toram=full` kopiert das gesamte System in den Speicher. `toram=trim` kopiert nur die gefilterte Modulauswahl. Dies kann das Entfernen des Boot-Geräts ermöglichen, erfordert jedoch genügend RAM für die kopierten Daten.
