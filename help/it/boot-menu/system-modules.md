# Sistema e moduli

Seleziona i moduli e l'ambiente di avvio per questa voce.

## Filtri dei moduli

**Carica moduli** limita il caricamento ai nomi o intervalli di moduli corrispondenti.
**Salta moduli** esclude i moduli corrispondenti. I suggerimenti provengono dai moduli
detectati nella sorgente MiniOS selezionata.

Questi controlli generano `load=FILTER` e `noload=FILTER`. Usa solo le forme di filtro
supportate dall'initramfs di MiniOS.

## Ambiente di avvio

Mantieni l'immagine predefinita, avvia il desktop grafico, usa una console testuale o
entra in modalità di recupero. La modalità testo e la modalità di recupero sono pensate per
amministrazione e risoluzione dei problemi piuttosto che per l'uso normale del desktop.

## Compatibilità grafica

La modalità compatibilità aggiunge l'opzione del kernel Linux `nomodeset`. Usala quando la normale
impostazione della modalità kernel impedisce l'avvio del sistema grafico. Può ridurre
la risoluzione e l'accelerazione dello schermo.

## Montaggio automatico dei dischi

Abilita il montaggio automatico solo quando la sessione deve esporre altri filesystem collegati
dopo l'avvio.
