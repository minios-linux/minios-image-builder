# Diagnose und erweiterte Optionen

Passen Sie das Boot-Logging an oder fügen Sie Parameter hinzu, die von den vorhandenen Eingabefeldern nicht abgedeckt werden.

## Bootmeldungen

**Routine-Bootmeldungen ausblenden** fügt `quiet` hinzu. **Diagnose-Logging aktivieren** fügt
`debug` hinzu. Beide Optionen können unabhängig voneinander aktiviert werden. Deaktivieren Sie `quiet`, wenn detaillierte Bootmeldungen nützlicher sind als ein sauberer Startbildschirm.

## Zusätzliche Parameter

Geben Sie nur Linux-, MiniOS-initramfs- oder live-config-Parameter ein, für die es kein typisiertes Steuerelement gibt. Bereits vorhandene unbekannte Parameter werden hier beibehalten, und die Vervollständigung schlägt weiterhin gängige Optionen vor.

Eintragspezifische Parameter werden nach den Template- und globalen Kernel-Argumenten angehängt. Bei einer wiederholten MiniOS `key=value`-Option gewinnt normalerweise der letzte Wert. Ungültige oder widersprüchliche Expertenparameter können den Start verhindern.
