# Sitzung und Speicherung

Wählen Sie aus, wie dieser Eintrag dauerhafte Änderungen findet und speichert.

## Sitzungswahl

Die Eintragsvorlage steuert, ob MiniOS die letzte Sitzung fortsetzt, eine neue erstellt oder beim Start fragt. Der Speichertyp ersetzt diese Vorlagenauswahl nicht.

## Speichertyp

- **Automatisch** behält den Modus der Vorlage oder gespeicherten Sitzung bei.
- **Nativ** speichert Änderungen in einem Verzeichnis auf einem Linux-Dateisystem.
- **Dynfilefs** verwendet einen erweiterbaren Container.
- **Raw** nutzt ein Abbild mit fester Größe.
- **LUKS** verwendet einen verschlüsselten Container.
- **SquashFS** setzt eine bestehende komprimierte Sitzung fort.

Das aktuelle initramfs kann SquashFS-Sitzungen fortsetzen, aber keine neuen erstellen.

## Kapazität

**Containergröße** gilt nur für containerbasierte Sitzungen und ist daher für Nativ und SquashFS deaktiviert. **Freier Speicher zum Behalten** reserviert Platz auf dem Persistenzgerät, damit gespeicherte Änderungen es nicht vollständig füllen.

Die entsprechenden Boot-Optionen sind `perchmode=`, `perchsize=` und `perchreserve=`.

## In den RAM kopieren

`toram=full` kopiert das gesamte System in den Speicher. `toram=trim` kopiert nur die gefilterte Modulauswahl. Dies kann das Entfernen des Boot-Geräts ermöglichen, erfordert jedoch genügend RAM für die kopierten Daten.
