# Parametri di boot e kernel

Inserisci i parametri separati da spazi. Il completamento suggerisce le opzioni comuni di MiniOS e Linux. I parametri globali si applicano a tutte le voci di sessione MiniOS create o preservate. I parametri specifici per ogni voce vengono aggiunti successivamente e possono sovrascrivere un'opzione MiniOS ripetuta in formato chiave-valore.

## Sessione e persistenza

Queste opzioni abilitano le modifiche persistenti, riprendono l'ultima sessione compatibile,
creano una nuova sessione, chiedono all'avvio o selezionano direttamente una sessione numerata:
perch, perchdir=resume, perchdir=new, perchdir=ask e perchdir=NUMERO.

Le modalità di archiviazione sono native, dynfilefs, raw, luks e squashfs. SquashFS può
riprendere una sessione compressa esistente, ma l'attuale initramfs non può crearne una nuova.
Le dimensioni dei contenitori accettano i suffissi MB, GB o TB. Lo spazio libero riservato è misurato in MiB; il valore predefinito è 256 e il massimo è 4096.

Le opzioni corrispondenti sono perchmode=MODE, perchsize=SIZE e
perchreserve=MIB.

## Copia in RAM

Le opzioni toram, toram=full e toram=trim copiano il sistema predefinito, completo o
filtrato nella RAM.

## Moduli

Il filtro load carica solo i moduli corrispondenti; il filtro noload esclude
i moduli corrispondenti. I filtri possono contenere nomi di moduli, elenchi o intervalli MiniOS
supportati dall'initramfs. Le opzioni sono load=FILTER e noload=FILTER.

## Memoria e grafica

Le opzioni di memoria disabilitano zRAM, scelgono la compressione lzo, lzo-rle, lz4, lz4hc o zstd
e impostano la dimensione di zRAM in MiB. La modalità testo avvia senza il
desktop grafico. Nomodeset disabilita l'impostazione normale della modalità kernel ed è utile
per la risoluzione dei problemi grafici. Le opzioni sono nozram, zramcomp=ALGORITHM,
zramsize=MIB, text e nomodeset.

## Origine e localizzazione

Queste opzioni selezionano la fonte dei dati MiniOS e sovrascrivono lingua, fuso orario
e impostazioni della tastiera per la voce. Le opzioni sono from=SOURCE, from=askdisk,
locales=LOCALE, timezone=ZONE e keyboard-layouts=LAYOUT.

## Diagnostica

Quiet riduce i messaggi di avvio. Debug abilita ulteriori diagnostiche. Usa solo
parametri compresi da Linux, dall'initramfs di MiniOS o da live-config. Le
opzioni sono quiet e debug.
