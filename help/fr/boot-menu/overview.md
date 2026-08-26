# Constructeur du menu de démarrage

Créez le menu de démarrage visible à partir d’entrées indépendantes. Choisissez un modèle, puis affinez cette entrée à l’aide des contrôles typés. Les entrées désactivées restent dans le projet mais sont exclues du menu généré.

## Menu source existant

Avant toute personnalisation, le constructeur lit les entrées MiniOS reconnues, leur entrée par défaut et leur délai d’expiration, ainsi que les paramètres pris en charge depuis le menu GRUB effectif ou SYSLINUX natif. L’édition d’une entrée importée ne remplace que les paramètres représentés par des contrôles typés. Les autres arguments de la source restent dans le modèle de démarrage. Dans un menu multilingue, chaque langue conserve ses arguments de locale, de fuseau horaire et de clavier, sauf si vous les remplacez explicitement.

## Assemblage d’une entrée

Le modèle fournit le comportement de base de MiniOS. Les arguments noyau experts globaux sont appliqués ensuite, suivis des options typées et des paramètres supplémentaires pour cette entrée. Pour les options MiniOS répétées sous forme clé-valeur, la dernière valeur prévaut.

## Modèles de session

Reprise utilise perchdir=resume. Nouveau utilise perchdir=new. Choisir utilise perchdir=ask. Neuf n’a pas de sélecteur de persistance. Copier en RAM utilise toram.

Vous pouvez créer plusieurs entrées à partir du même modèle.

## Types de persistance

Native enregistre les modifications dans un dossier. Dynfilefs utilise un conteneur extensible, Raw utilise une image de taille fixe, et LUKS utilise un conteneur chiffré. SquashFS reprend une session compressée existante. L’initramfs actuel ne peut pas créer une nouvelle session SquashFS.

## Paramètres dépendants

Les contrôles deviennent indisponibles lorsqu’ils ne sont pas applicables. Désactiver zRAM désactive ses contrôles de compression et de taille. La persistance Native et SquashFS n’utilise pas le champ taille du conteneur.

## Saisie experte et complétion

Les filtres de modules proposent automatiquement les modules détectés dans la source sélectionnée. Les champs de langue, fuseau horaire et clavier sont complétés à partir des données système installées. Utilisez les paramètres supplémentaires uniquement pour les options sans contrôle typé. Les arguments inconnus chargés depuis un ancien projet y sont conservés.

## Valeurs par défaut et noms

Un menu personnalisé comporte exactement une entrée par défaut. Désactiver l’entrée par défaut en sélectionne automatiquement une autre activée. Un nom vide conserve le titre de la source ou du modèle. Les noms personnalisés en ASCII fonctionnent dans les menus multilingues ; un menu monolingue peut utiliser les caractères pris en charge par l’encodage du menu du bootloader.
