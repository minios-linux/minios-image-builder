# Diagnostica e opzioni avanzate

Regola il logging di avvio o aggiungi parametri non disponibili nei controlli digitati.

## Messaggi di avvio

**Nascondi i messaggi di avvio di routine** aggiunge `quiet`. **Abilita la registrazione diagnostica** aggiunge
`debug`. Possono essere abilitati indipendentemente. Disabilita `quiet` quando i messaggi di avvio dettagliati
sono più utili di una schermata di avvio pulita.

## Parametri aggiuntivi

Inserisci solo parametri Linux, MiniOS initramfs o live-config che non hanno un
controllo tipizzato. Gli eventuali parametri sconosciuti esistenti vengono mantenuti qui e il completamento
continua a suggerire opzioni comuni.

I parametri specifici della voce vengono aggiunti dopo gli argomenti del kernel del modello e globali.
Per un'opzione MiniOS `key=value` ripetuta, normalmente prevale l'ultimo valore.
Parametri esperti non validi o in conflitto possono impedire l'avvio.
