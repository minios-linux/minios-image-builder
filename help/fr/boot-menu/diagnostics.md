# Diagnostics et options avancées

Ajustez la journalisation du démarrage ou ajoutez des paramètres non représentés par les contrôles saisis.

## Messages de démarrage

**Masquer les messages de démarrage courants** ajoute `quiet`. **Activer la journalisation de diagnostic** ajoute
`debug`. Elles peuvent être activées indépendamment. Désactivez `quiet` lorsque des messages de démarrage détaillés
sont plus utiles qu'un écran de démarrage épuré.

## Paramètres supplémentaires

N'entrez que les paramètres Linux, MiniOS initramfs ou live-config qui n'ont pas de
contrôle typé. Les paramètres inconnus existants sont conservés ici, et la saisie
continue de suggérer des options courantes.

Les paramètres spécifiques à l'entrée sont ajoutés après le modèle et les arguments
noyau globaux. Pour une option MiniOS `key=value` répétée, la dernière valeur
prévaut généralement. Des paramètres experts invalides ou en conflit peuvent empêcher le démarrage.
