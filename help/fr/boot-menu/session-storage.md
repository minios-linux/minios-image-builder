# Session et stockage

Choisissez comment cette entrée détecte et enregistre les modifications persistantes.

## Sélection de session

Le modèle d'entrée contrôle si MiniOS reprend la dernière session, en crée une nouvelle ou demande au démarrage. Le type de stockage ne remplace pas ce choix de modèle.

## Type de stockage

- **Automatique** conserve le mode modèle ou session enregistrée.
- **Natif** enregistre les modifications directement dans un répertoire sur un système de fichiers avec des permissions Unix, comme ext4, XFS ou Btrfs.
- **Dynfilefs** utilise un conteneur extensible.
- **Raw** utilise une image de taille fixe.
- **LUKS** utilise un conteneur chiffré.
- **SquashFS** reprend une session compressée existante.

L'initramfs actuel peut reprendre mais ne peut pas créer de sessions SquashFS.

## Capacité

**Taille du conteneur** s'applique uniquement aux sessions basées sur un conteneur, donc elle est désactivée pour Natif et SquashFS. **Espace libre à conserver** réserve de la place sur le périphérique de persistance afin que les modifications sauvegardées ne le remplissent pas complètement.

Les options de démarrage correspondantes sont `perchmode=`, `perchsize=` et `perchreserve=`.

## Copier en RAM

`toram=full` copie l'ensemble du système en mémoire. `toram=trim` ne copie que l'ensemble filtré des modules. Cela peut permettre de retirer le périphérique de démarrage, mais nécessite suffisamment de RAM pour les données copiées.
