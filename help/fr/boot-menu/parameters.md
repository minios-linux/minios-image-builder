# Paramètres de démarrage et du noyau

Saisissez les paramètres en les séparant par des espaces. La saisie semi-automatique propose des options courantes pour MiniOS et Linux. Les paramètres globaux s'appliquent à chaque entrée de session MiniOS créée ou conservée. Les paramètres spécifiques à une entrée sont ajoutés ensuite et peuvent remplacer une option clé-valeur MiniOS répétée.

## Session et persistance

Ces options permettent d'activer les modifications persistantes, de reprendre la dernière session compatible,
de créer une nouvelle session, de demander au démarrage, ou de sélectionner directement une session numérotée :
perch, perchdir=resume, perchdir=new, perchdir=ask, et perchdir=NUMBER.

Les modes de stockage sont native, dynfilefs, raw, luks, et squashfs. SquashFS peut
reprendre une session compressée existante, mais l'initramfs actuel ne peut pas en créer une nouvelle. Les tailles de conteneur acceptent les suffixes MB, GB ou TB. L'espace libre réservé est mesuré en MiB ; sa valeur par défaut est 256 et son maximum est 4096.

Les options correspondantes sont perchmode=MODE, perchsize=SIZE, et
perchreserve=MIB.

## Copie en RAM

Les options toram, toram=full, et toram=trim copient le système par défaut, complet ou filtré en RAM.

## Modules

Le filtre load charge uniquement les modules correspondants ; le filtre noload exclut
les modules correspondants. Les filtres peuvent contenir des noms de modules, des listes ou des plages MiniOS
prises en charge par l'initramfs. Les options sont load=FILTER et noload=FILTER.

## Mémoire et graphisme

Les options de mémoire désactivent zRAM, choisissent la compression lzo, lzo-rle, lz4, lz4hc ou zstd,
et définissent la taille zRAM en MiB. Le mode texte démarre sans le bureau graphique. Nomodeset désactive le mode graphique normal du noyau et est utile pour le dépannage graphique. Les options sont nozram, zramcomp=ALGORITHM,
zramsize=MIB, text, et nomodeset.

## Source et localisation

Ces options sélectionnent la source de données MiniOS et remplacent les paramètres de langue, de fuseau horaire
et de clavier pour l'entrée. Les options sont from=SOURCE, from=askdisk,
locales=LOCALE, timezone=ZONE, et keyboard-layouts=LAYOUT.

## Diagnostics

Quiet réduit les messages de démarrage. Debug active des diagnostics supplémentaires. Utilisez uniquement
les paramètres compris par Linux, l'initramfs MiniOS ou live-config. Les options sont quiet et debug.
