# Sessione e archiviazione

Scegli come questa voce individua e memorizza le modifiche persistenti.

## Selezione della sessione

Il modello della voce controlla se MiniOS riprende l'ultima sessione, ne crea una nuova o chiede all'avvio. Il tipo di archiviazione non sostituisce questa scelta del modello.

## Tipo di archiviazione

- **Automatico** mantiene la modalità del modello o della sessione salvata.
- **Nativo** salva le modifiche in una directory su un filesystem Linux.
- **Dynfilefs** utilizza un contenitore espandibile.
- **Raw** utilizza un'immagine a dimensione fissa.
- **LUKS** utilizza un contenitore cifrato.
- **SquashFS** riprende una sessione compressa esistente.

L'attuale initramfs può riprendere ma non creare sessioni SquashFS.

## Capacità

**Dimensione del contenitore** si applica solo alle sessioni basate su contenitore, quindi è disabilitata per Nativo e SquashFS. **Spazio libero da mantenere** riserva spazio sul dispositivo di persistenza in modo che le modifiche salvate non lo riempiano completamente.

Le opzioni di avvio corrispondenti sono `perchmode=`, `perchsize=` e `perchreserve=`.

## Copia in RAM

`toram=full` copia l'intero sistema in memoria. `toram=trim` copia solo il set di moduli filtrato. Questo può consentire la rimozione del dispositivo di avvio, ma richiede abbastanza RAM per i dati copiati.
