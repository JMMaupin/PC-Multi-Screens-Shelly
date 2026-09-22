# Jeu d'icônes pour une application Windows

Spécification des images à ajouter au générateur, à partir de ce qu'il
produit déjà pour le web.

> **État.** Les points 1, 2 et 3 sont implémentés dans
> `icongen_windows.py`, et le jeu qu'il produit est en service dans
> `shelly_screens/assets/`. Restent les points 4 et 5 — le dessin
> simplifié pour les petites tailles et la lisibilité sur fond sombre :
> ils relèvent du dessin, pas du calcul.

## Le jeu web est-il utilisable tel quel ?

Il fonctionne, mais il n'est pas complet. Trois écarts, du plus visible au
plus discret.

| | Jeu web actuel | Attendu sous Windows |
| --- | --- | --- |
| Transparence | **aucune** — tout est en PNG RVB | canal alpha obligatoire |
| Tailles dans le `.ico` | 16, 32, 48 | 16, 20, 24, 32, 40, 48, 64, 96, 256 |
| Format interne du `.ico` | PNG pour toutes les tailles | BMP jusqu'à 48, PNG au-delà |

## 1. Canal alpha — le manque le plus visible

Les huit PNG du jeu sont en **type couleur 2 (RVB), sans canal alpha**. Le
coin supérieur gauche de `icon-192.png` vaut `(16, 22, 26)` : un gris très
sombre, opaque.

Sur le web, ça ne se voit pas : l'icône est affichée dans un cadre qui lui
applique ses propres coins arrondis. Sous Windows, personne ne le fait à sa
place. L'icône apparaît donc comme un **carré plein**, y compris là où le
dessin suggère des coins arrondis — dans la barre des tâches, la zone de
notification, l'Explorateur et la barre de titre. Sur un thème clair, le
carré sombre tranche franchement.

**À produire :** les mêmes images en **PNG type couleur 6 (RVBA)**, avec les
coins arrondis réellement transparents et un léger adoucissement du bord
(une bordure nette crée un escalier visible aux petites tailles).

## 2. Tailles manquantes

Le `.ico` ne contient que 16, 32 et 48. Windows en réclame davantage, et
**redimensionne lui-même** ce qui manque — d'où un rendu trouble.

| Taille | À quoi elle sert |
| --- | --- |
| 16 | Barre de titre, zone de notification, listes compactes |
| **20** | Les mêmes, sur un écran à 125 % |
| **24** | Les mêmes, à 150 % |
| 32 | Bureau, barre des tâches, Alt+Tab |
| **40** | Bureau à 125 % |
| 48 | Explorateur, vue « Icônes moyennes » |
| **64** | Explorateur à 125–150 % |
| **96** | Vue « Grandes icônes » |
| **256** | Vue « Très grandes icônes », fenêtre Propriétés, installateurs |

Les tailles en gras sont celles qui manquent. Les intermédiaires — 20, 24,
40 — ne sont pas du luxe : elles correspondent aux facteurs d'échelle
courants, et beaucoup d'écrans sont configurés à 125 % ou 150 %.

Chaque taille doit être **dessinée à sa définition**, non interpolée depuis
une plus grande : c'est précisément ce qu'on cherche à éviter.

## 3. Format interne du fichier `.ico`

Un `.ico` est un conteneur : chaque taille y est stockée soit en **BMP/DIB
32 bits**, soit en **PNG compressé**. Le jeu actuel met du PNG partout.

La règle admise :

* **jusqu'à 48 px : BMP 32 bits.** Windows 10 et 11 lisent le PNG sans
  problème, mais certains contextes anciens ne l'acceptent pas et affichent
  une icône vide. Le surcoût est négligeable à ces tailles.
* **à partir de 64 px : PNG.** Sans compression, une image 256×256 en BMP
  pèse 256 Ko à elle seule, et le fichier devient absurde.

## 4. Une variante simplifiée pour les petites tailles

Le dessin comporte deux écrans, un symbole d'alimentation et des éclairs.
À 48 px tout se lit. À 16 px — la taille réelle dans la zone de
notification et la barre de titre — il ne reste qu'une tache.

Les jeux d'icônes soignés prévoient un **dessin distinct pour les petites
tailles** : un seul écran, ou la seule silhouette du symbole
d'alimentation, avec des traits plus épais. Ce dessin sert pour 16, 20 et
24 px ; le dessin complet prend le relais à partir de 32.

## 5. Lisibilité sur fond clair comme sombre

La barre des tâches suit le thème de Windows. L'icône ayant un fond très
sombre, elle se détache mal sur une barre sombre — le cas par défaut de
Windows 11.

Deux façons de traiter :

* un **liseré clair** d'un pixel sur le contour, qui suffit généralement ;
* ou **deux variantes**, claire et sombre, l'application choisissant selon
  le thème du système.

## 6. Si un installateur est prévu

Ces images n'ont rien à voir avec l'icône et sont souvent oubliées.

| Outil | Fichier | Dimensions | Format |
| --- | --- | --- | --- |
| Inno Setup | `WizardImageFile` | 164×314 | BMP 24 bits |
| Inno Setup | `WizardSmallImageFile` | 55×58 | BMP 24 bits |
| WiX / MSI | `WixUIBannerBmp` | 493×58 | BMP 24 bits |
| WiX / MSI | `WixUIDialogBmp` | 493×312 | BMP 24 bits |

Le BMP 24 bits est imposé par ces outils : ni PNG, ni transparence.

## Ce qui ne sert à rien sous Windows

* `icon-180.png` — spécifique à l'écran d'accueil iOS.
* `icon-maskable-*.png` — propre aux PWA, où le système rogne l'image dans
  une forme quelconque. Windows ne rogne rien.
* `manifest-icons.json` et `head-snippet.html` — déclarations web.

Rien à supprimer pour autant : ces fichiers ne gênent pas, ils sont
simplement ignorés.

## Récapitulatif à implémenter

1. Passer **toutes** les sorties en PNG **RVBA**, coins réellement transparents.
2. Générer les tailles **20, 24, 40, 64, 96, 256** en plus des trois existantes.
3. Assembler le `.ico` avec **BMP jusqu'à 48** et **PNG à partir de 64**.
4. Ajouter un dessin simplifié pour **16, 20 et 24**.
5. Ajouter un liseré clair, ou une seconde variante pour thème sombre.
6. Si installateur : les quatre BMP du tableau ci-dessus.

Les points 1 et 2 sont ceux qui se voient. Les autres relèvent du fini.
