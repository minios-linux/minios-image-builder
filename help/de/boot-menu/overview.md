# Boot-Menü-Konfigurator

Erstellen Sie das sichtbare Startmenü aus unabhängigen Einträgen. Wählen Sie eine Vorlage und passen Sie den Eintrag anschließend mit den verfügbaren Steuerelementen an. Deaktivierte Einträge bleiben im Projekt erhalten, werden aber im generierten Menü ausgelassen.

## Vorhandenes Quellmenü

Vor der Anpassung liest der Konfigurator erkannte MiniOS-Einträge, deren Standard und Timeout sowie unterstützte Parameter aus dem aktiven GRUB- oder nativen SYSLINUX-Menü aus. Beim Bearbeiten eines importierten Eintrags werden nur die Parameter ersetzt, die durch typisierte Steuerelemente dargestellt werden. Andere Quellargumente bleiben in der Boot-Vorlage erhalten. In einem mehrsprachigen Menü behält jede Sprache ihre Quell-Locale, Zeitzone und Tastatur-Argumente, sofern Sie diese nicht explizit überschreiben.

## Wie ein Eintrag zusammengesetzt wird

Die Vorlage liefert das grundlegende MiniOS-Verhalten. Globale Experten-Kernel-Argumente werden anschließend angewendet, gefolgt von den typisierten Optionen und zusätzlichen Parametern für diesen Eintrag. Bei mehrfachen MiniOS-Schlüssel-Wert-Optionen gilt der zuletzt angegebene Wert.

## Sitzungsvorlagen

Resume verwendet perchdir=resume. New verwendet perchdir=new. Choose verwendet perchdir=ask.
Fresh hat keinen Persistenzselektor. Kopieren in den RAM verwendet toram.

Sie können mehrere Einträge aus derselben Vorlage erstellen.

## Persistenztypen

Native speichert Änderungen in einem Verzeichnis. Dynfilefs verwendet einen erweiterbaren Container,
Raw nutzt ein Abbild mit fester Größe und LUKS einen verschlüsselten Container. SquashFS
setzt eine bestehende komprimierte Sitzung fort. Das aktuelle
initramfs kann keine neue SquashFS-Sitzung erstellen.

## Abhängige Einstellungen

Steuerelemente werden deaktiviert, wenn sie nicht zutreffen. Das Deaktivieren von zRAM schaltet
auch dessen Kompression und Größensteuerung ab. Native und SquashFS-Persistenz verwenden das Feld für die Containergröße nicht.

## Vervollständigung und Experteneingabe

Modulfilter vervollständigen sich aus Modulen, die in der gewählten Quelle erkannt wurden. Locale-, Zeitzonen- und Tastaturfelder werden aus den auf dem System installierten Daten vervollständigt. Verwenden Sie zusätzliche Parameter nur für Optionen ohne typisiertes Steuerelement. Unbekannte Argumente aus einem älteren Projekt bleiben dort erhalten.

## Vorgaben und Namen

Ein angepasstes Menü hat genau einen Standard-Eintrag. Das Deaktivieren des Standards wählt automatisch einen anderen aktivierten Eintrag aus. Ein leerer Name übernimmt den Titel der Quelle oder Vorlage. ASCII-Benutzerdefinierte Namen funktionieren in mehrsprachigen Menüs; ein einsprachiges Menü kann Zeichen verwenden, die von der Zeichencodierung des jeweiligen Bootloader-Menüs unterstützt werden.
