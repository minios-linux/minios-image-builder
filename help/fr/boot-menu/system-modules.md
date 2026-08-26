# Système et modules

Sélectionnez les modules et l’environnement de démarrage pour cette entrée.

## Filtres de modules

**Charger des modules** limite le chargement aux noms de modules ou plages correspondants.
**Ignorer des modules** exclut les modules correspondants. Les suggestions proviennent des modules
détectés dans la source MiniOS sélectionnée.

Ces contrôles génèrent `load=FILTER` et `noload=FILTER`. Utilisez uniquement les formes de filtres
prises en charge par l'initramfs de MiniOS.

## Environnement de démarrage

Conservez l'image par défaut, démarrez le bureau graphique, utilisez une console texte ou
entrez en mode secours. Le mode texte et le mode secours sont destinés à l'administration
et au dépannage plutôt qu'à une utilisation normale du bureau.

## Compatibilité graphique

Le mode de compatibilité ajoute l'option du noyau Linux `nomodeset`. Utilisez-le lorsque le mode normal
de gestion du noyau empêche le système graphique de démarrer. Cela peut réduire
la résolution d'affichage et l'accélération.

## Montage automatique des disques

Activez le montage automatique uniquement lorsque la session doit exposer d'autres systèmes de fichiers
attachés après le démarrage.
