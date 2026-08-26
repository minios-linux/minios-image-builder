# System und Module

Wählen Sie die Module und die Startumgebung für diesen Eintrag aus.

## Modulfiter

**Module laden** beschränkt das Laden auf passende Modulnamen oder Bereiche.
**Module überspringen** schließt passende Module aus. Vorschläge stammen von Modulen,
die in der gewählten MiniOS-Quelle erkannt wurden.

Diese Einstellungen erzeugen `load=FILTER` und `noload=FILTER`. Verwenden Sie nur Filterformen,
die vom MiniOS-initramfs unterstützt werden.

## Startumgebung

Behalten Sie das Standard-Image bei, starten Sie die grafische Desktopumgebung, verwenden Sie eine Textkonsole oder
wechseln Sie in den Rettungsmodus. Textmodus und Rettungsmodus sind für Administration
und Fehlerbehebung gedacht und nicht für den normalen Desktopbetrieb.

## Grafikkompatibilität

Der Kompatibilitätsmodus fügt die Linux-Kernel-Option `nomodeset` hinzu. Verwenden Sie ihn, wenn der normale
Kernel-Modus das Starten des grafischen Systems verhindert. Dies kann die
Bildschirmauflösung und die Beschleunigung reduzieren.

## Automatisches Einbinden von Datenträgern

Aktivieren Sie das automatische Einbinden nur, wenn die Sitzung nach dem Start andere angeschlossene
Dateisysteme bereitstellen soll.
