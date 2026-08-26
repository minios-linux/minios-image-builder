# Costruttore del menu di avvio

Crea il menu di avvio visibile a partire da voci indipendenti. Scegli un modello, poi personalizza quella voce con i controlli disponibili. Le voci disabilitate restano nel progetto ma vengono escluse dal menu generato.

## Menu di origine esistente

Prima della personalizzazione, il costruttore legge le voci MiniOS riconosciute, il loro valore predefinito e timeout, e i parametri supportati dal menu GRUB effettivo o SYSLINUX nativo. Modificare una voce importata sostituisce solo i parametri rappresentati dai controlli tipizzati. Gli altri argomenti della sorgente restano nel template di avvio. In un menu multilingue, ogni lingua mantiene la propria locale, fuso orario e impostazioni tastiera, a meno che tu non li sovrascriva esplicitamente.

## Come viene assemblata una voce

Il template fornisce il comportamento base di MiniOS. Gli argomenti kernel globali per esperti vengono applicati successivamente, seguiti dalle opzioni tipizzate e dai Parametri aggiuntivi per questa voce. Per le opzioni MiniOS ripetute in formato chiave-valore, prevale sempre l'ultimo valore.

## Template di sessione

Resume utilizza perchdir=resume. New utilizza perchdir=new. Choose utilizza perchdir=ask.
Fresh non ha selettore di persistenza. Copia in RAM utilizza toram.

Puoi creare più voci dallo stesso template.

## Tipi di persistenza

Native salva le modifiche in una directory. Dynfilefs utilizza un contenitore espandibile,
Raw usa un'immagine a dimensione fissa e LUKS utilizza un contenitore cifrato. SquashFS
riprende una sessione compressa esistente. L'attuale
initramfs non può creare una nuova sessione SquashFS.

## Impostazioni dipendenti

I controlli diventano non disponibili quando non sono applicabili. Disabilitare zRAM disattiva
anche i controlli di compressione e dimensione. La persistenza Native e SquashFS non utilizzano
il campo dimensione contenitore.

## Completamento e input esperto

I filtri dei moduli completano dai moduli rilevati nella sorgente selezionata. I campi di lingua,
fuso orario e tastiera si completano dai dati di sistema installati. Usa
Parametri aggiuntivi solo per opzioni senza un controllo tipizzato. Gli argomenti sconosciuti caricati da un progetto precedente vengono mantenuti lì.

## Predefiniti e nomi

Un menu personalizzato ha esattamente una voce predefinita. Disabilitare il predefinito seleziona
automaticamente un'altra voce abilitata. Un nome vuoto mantiene il titolo della sorgente o del template.
I nomi personalizzati in ASCII funzionano nei menu multilingue; un menu monolingua può utilizzare caratteri supportati dalla codifica del menu di quel bootloader.
