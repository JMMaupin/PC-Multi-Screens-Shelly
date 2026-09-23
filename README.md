# Shelly Screens

Pilotage de l'alimentation des écrans et périphériques d'un PC par une ou
plusieurs **Shelly Power Strip 4 Gen4**, depuis une icône dans la zone de
notification de Windows. Des profils d'usage décident quelles prises sont
alimentées, et la disposition des fenêtres est mémorisée puis remise en place
avec eux.

## En un coup d'œil

| | |
| --- | --- |
| Appareils | Shelly Power Strip 4 Gen4 et autres Shelly à sorties commandables |
| Découverte | nom mDNS `<device-id>.local`, puis adresse connue, puis balayage |
| Protocole | JSON-RPC sur HTTP, authentification Digest SHA-256 optionnelle |
| Dépendances | aucune — bibliothèque standard de Python 3.12+ |

## Démarrage rapide

```powershell
powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Desktop
```

Cela pose deux raccourcis — un au démarrage de session, un sur le Bureau —
qui visent directement `pythonw.exe` : **aucune console, pas même un
scintillement au lancement**. Un seul processus, contrairement à un détour
par `.cmd`, `wscript` ou l'association `.pyw`, qui en laissent un second
tourner pour rien.

Pour diagnostiquer, `run-console.cmd` ouvre une console et y affiche les
journaux en direct.

L'icône apparaît dans la zone de notification : une case par prise, verte si
alimentée, grise sinon, tout en rouge si plus rien ne répond. Au-delà de
quatre prises les cases passent sur deux rangées.

* **Clic droit** : menu des profils, état des prises, consommation.
* **Clic gauche** : fenêtre de réglages.

## Mise en route

### 1. Brancher

Écrans, concentrateurs USB et unité centrale se répartissent sur les
multiprises, **elles-mêmes branchées directement au mur**. Aucune ne doit se
trouver derrière une multiprise maîtresse : elle serait coupée avec le reste,
perdrait le réseau et mettrait une dizaine de secondes à revenir.

### 2. Déclarer les appareils

Onglet **Devices** → **Add device**. Le balayage du réseau prend une
vingtaine de secondes ; saisir l'adresse directement est instantané.

Chaque appareil reçoit une **clé courte** (`strip`, `strip2`, `plug`), et
c'est elle que les profils référencent — une prise se désigne par
`strip2:1`. La clé se renomme à tout moment, les références suivent.

La colonne **Signal** donne la puissance de la liaison Wi-Fi de l'appareil,
assortie de ce qu'elle vaut : *excellent* au-dessus de -60 dBm, *good*
jusqu'à -70, *fair* jusqu'à -78, *weak* en dessous. Un nombre négatif en
décibels ne parle qu'à qui le pratique ; le qualificatif se lit d'un coup
d'œil. Une multiprise à -79 dBm tient au bord du décrochage sans que rien
ne l'annonce, et son seuil de bascule interne est justement à -80.
Le signal est relu une fois par minute, jamais plus : une liaison ne change
pas d'un battement de cil, et chaque interrogation pèse sur l'appareil.

Deux colonnes distinguent **Reached via** et **IP address**. L'application
joint de préférence l'appareil par son nom mDNS, plus stable que son bail
DHCP, mais c'est l'adresse qu'on veut lire — pour ouvrir son interface web,
ou pour constater qu'elle a changé. **Cliquer sur l'adresse ouvre
l'interface web de l'appareil** dans le navigateur ; le curseur passe en
main au survol. Le bouton *Open web UI* fait la même chose pour l'appareil
sélectionné.

### 3. Associer chaque prise à son écran

Onglet **Outlets** → **Identify displays**. L'assistant coupe chaque prise à
tour de rôle et observe quel écran Windows retire, puis la rallume. Les
autres écrans restent allumés pendant ce temps : l'assistant ne peut pas se
couper l'herbe sous le pied. Compter une douzaine de secondes par prise.

Une prise sans écran (concentrateur USB, unité centrale) ressort simplement
comme non identifiée, ce qui est normal.

L'identification repose sur le chemin d'interface du moniteur, qui contient
l'UID de la sortie graphique. Deux écrans du même modèle restent donc
distincts, et l'association survit aux redémarrages.

### 4. Dire ce qui est branché

Colonne **Type**, dans l'éditeur sous la liste :

| Type | Pour | Effet |
| --- | --- | --- |
| **Screen** | Un écran | Seul type entrant dans l'assistant d'identification |
| **Accessory** | Concentrateur USB, enceintes | Reste pilotable par les profils, mais **hors périmètre écran** |
| **Not set** | Pas encore renseigné | **Aucun automatisme n'y touche** |

**Déclarer le type de chaque écran est obligatoire.** L'assistant ne
manœuvre que ce qui porte explicitement le type *Screen* ; tant qu'une
prise reste *Not set*, il la laisse tranquille et le dit.

C'est délibérément l'inverse d'un choix permissif : ce qu'on ignore, on n'y
touche pas. Une prise oubliée est précisément celle dont on ne sait pas ce
qu'elle alimente — c'est celle-là qu'il ne faut pas couper pour voir.

Un accessoire, lui, ne fera disparaître aucun écran : le tester ne serait
que du temps perdu et une coupure pour rien.

### 5. Distribuer les rôles

Toujours dans **Outlets**, trois cases décident de ce qui ne doit jamais
s'éteindre au mauvais moment :

Le type dit *ce qui est au bout du fil* ; les rôles ci-dessous disent
*comment traiter la prise*. Les deux sont indépendants.

| Rôle | Effet | À cocher sur |
| --- | --- | --- |
| **Boot screen** | Secours si le profil mémorisé est inexploitable | L'écran principal |
| **Critical** | N'est jamais coupée, ni par un profil ni à l'arrêt | Le concentrateur USB du clavier |
| **Powers the PC** | Idem, et sa consommation dit si le PC tourne | L'unité centrale |
| **Follows the PC** | Coupée pendant la veille du PC | Les écrans ; un accessoire seulement si on le demande |

La case *Follows the PC* est cochée d'office dès qu'une prise est déclarée
*Screen* — c'est tout l'objet du montage. Les accessoires, eux, la gardent
vide : couper un concentrateur USB ou des enceintes n'a rien d'évident, et
l'avoir fait d'office a déjà surpris.

**Pourquoi ces rôles existent.** Pendant le POST, le BIOS et l'écran de
connexion, rien ne tourne sur le PC pour commander les prises. Un écran ne
peut donc pas être allumé au démarrage : il doit déjà l'être. Même chose pour
le clavier — sans son concentrateur USB alimenté, impossible d'entrer dans le
BIOS ni de saisir son code PIN.

L'onglet **Behaviour** récapitule en clair ce qui restera alimenté à l'arrêt.

### 6. Composer les profils

Onglet **Profiles** : cocher les prises alimentées par chaque profil. Les
prises protégées y apparaissent grisées et cochées, puisqu'elles ne se
coupent jamais.

Pour la disposition des fenêtres : ranger les fenêtres comme voulu, puis
**Save current layout**. Elle sera rejouée à chaque retour sur ce profil.

## La prise de l'unité centrale n'est jamais coupée

Couper le PC en marche lui fait perdre le travail en cours. La prise portant
le rôle **Powers the PC** est donc protégée par cinq verrous
indépendants, et non par un seul.

| Où | Ce qu'il fait |
| --- | --- |
| `device.py` | `set_switch(off)` sur une sortie protégée est refusé **avant tout envoi réseau** |
| `controller.py` | `set_outlet` et `_switch_many` refusent et le consignent |
| Assistant d'identification | ne manœuvre que les prises de type *Screen*, en écartant celles tirant plus de 80 W, et revalide avant chaque coupure |
| Script `pc_sensing` | sur l'appareil, rétablit la sortie si l'ordre vient d'ailleurs |
| `initial_state` de la sortie | posé à `on`, pour qu'un redémarrage de la multiprise la rende alimentée |

**Pourquoi quatre et pas un.** Le premier défaut venait de là : la seule
protection était la bonne construction de la liste des prises à couper, et
`_switch_many` envoyait ensuite les ordres sans rien revérifier. Une liste
mal construite suffisait. Le verrou est désormais au plus près de l'appel
réseau, là où aucun chemin ne peut le contourner.

**Le seuil de 80 W ne dépend d'aucun marquage.** Un moniteur, même grand,
dépasse rarement 60 W ; une unité centrale en consomme plus de 100. Cette
limite protège donc même si le rôle n'a pas été attribué, ou a été perdu.

**Ce que le gardien embarqué peut et ne peut pas.** Le firmware n'offre
aucun moyen de *refuser* une commande de coupure : on ne peut que la
corriger après coup. Mesuré à environ **180 ms**, quand une alimentation
ATX ne tient que 16 à 20 ms sans secteur. Le gardien rétablit le courant,
**il n'empêche pas l'arrêt**. Il sert contre les ordres venus d'ailleurs —
application mobile, cloud, autre outil — pas à rattraper un défaut du
logiciel. La protection réelle est en amont.

Déplacer le rôle sur une autre prise déplace le gardien avec lui et met à
jour les verrous immédiatement, sans redémarrage.

### Le cinquième verrou vit dans la multiprise

Chaque sortie porte un réglage `initial_state` qui décide de son sort **au
démarrage de l'appareil**, hors de portée du script comme de l'application.
Livré sur `off`, il ouvre toutes les sorties au moindre redémarrage — mise à
jour du firmware, micro-coupure, chien de garde — et la prise du PC avec.

C'est arrivé : un plantage du firmware a coupé l'unité centrale en pleine
session, sans qu'aucune des protections logicielles ait eu son mot à dire.
Elles refusent toutes des *commandes* ; celle-ci n'en était pas une.

L'application impose donc ce réglage à chaque connexion d'un appareil :
`on` pour la prise du PC et les prises critiques, `restore_last` pour les
autres. Un appareil neuf, réinitialisé ou revenu d'un changement de firmware
reprend ainsi ses garanties sans que personne y pense.

Les écrans sont en `restore_last` et non en `off` pour une raison précise :
si leur multiprise redémarrait pendant que le PC tourne, `off` les laisserait
éteints indéfiniment — le script ne réagit qu'aux changements d'état du PC,
et celui-ci n'aurait pas bougé.

## Couper vraiment tout : la détection par la consommation

Onglet **PC power**. Une fois l'unité centrale branchée sur une prise mesurée
et marquée **Powers the PC**, la multiprise peut rallumer les écrans toute
seule dès qu'elle voit le PC consommer. C'est ce qui autorise à tout couper à
l'arrêt, écran de démarrage compris : pendant le POST aucun logiciel ne
tourne sur le PC, mais la multiprise, elle, continue de mesurer.

La logique vit donc dans un **script mJS embarqué**, pas dans l'application.
Celle-ci ne fait que le générer, l'installer et lui déposer, dans le KVS de
l'appareil, la liste des prises à rallumer au prochain démarrage.

### Les quatre réglages

| Réglage | Défaut | Rôle |
| --- | --- | --- |
| `PC seen as running above` | 25 W | Au-dessus, le PC est considéré actif |
| `PC seen as off below` | 15 W | En dessous, il est considéré éteint |
| `Confirm before switching on` | 3 s | Court : on veut voir le POST |
| `Confirm before switching off` | 90 s | **Long**, voir ci-dessous |

Deux seuils et non un seul : entre les deux se trouve une zone morte où
l'état courant se maintient. Sans elle, une consommation oscillant autour
d'une valeur unique ferait claquer le relais en boucle — ils sont donnés pour
environ 100 000 cycles.

Le délai de coupure est volontairement long. Lors d'un redémarrage de
Windows, le PC passe sous le seuil pendant dix à quinze secondes ; couper les
écrans à cet instant précis serait le pire moment. Quatre-vingt-dix secondes
laissent passer un redémarrage sans broncher.

### Calibrer sur des mesures et non au jugé

**Start measuring** dépose un second script qui échantillonne la prise et
résume ce qu'il voit — minimum, maximum, histogramme — dans le KVS. Utiliser
le PC normalement : le laisser au repos, le mettre en veille, l'éteindre, le
rallumer. **Read now** relit le relevé et propose les seuils.

La séparation des paliers se fait sur la plus grande discontinuité de
l'histogramme. Un PC produit quatre paliers — éteint, en veille, au repos, en
charge — et c'est le vide entre « éteint ou en veille » et « allumé » qui
compte. L'assistant refuse de proposer des seuils s'il n'a vu qu'un seul
palier, ou si l'écart est trop faible pour être fiable.

### Ce qu'il fait sans l'application

Une fois installé, le script vit sa vie sur la multiprise : il fonctionne
PC éteint, application fermée, ou même désinstallée.

| Il voit | Il fait |
| --- | --- |
| La consommation du PC franchir le seuil haut | Rallume **les prises du dernier profil** |
| Elle repasser sous le seuil bas | **Coupe tout**, écran de démarrage compris |

### L'écran de démarrage est un secours, pas un privilège

Quand le profil mémorisé est exploitable, cette prise s'allume — ou non —
**avec les autres**, comme n'importe laquelle : si le profil ne la contient
pas, elle reste éteinte.

Elle n'est rallumée d'office que lorsque le profil ne vaut rien, et alors
seule : le PC consomme, donc il démarre, et mieux vaut un écran qu'un
démarrage à l'aveugle. Quatre cas déclenchent ce secours, tous vérifiés sur
l'appareil :

| Contenu du KVS | Conséquence |
| --- | --- |
| Liste valide contenant l'écran de démarrage | Il s'allume, comme les autres du profil |
| Liste valide **sans** lui | **Il reste éteint** |
| Liste vide | Secours : lui seul s'allume |
| Texte illisible | Secours |
| Index ne correspondant à aucune prise | Secours |

La forme du KVS est vérifiée **avant** d'être analysée : mJS n'a pas de
`try`/`catch`, et un `JSON.parse` sur du texte invalide interromprait la
fonction sans jamais atteindre le repli — le PC démarrerait alors sans
image, précisément le cas que ce repli doit couvrir.

Conséquence côté application : tant que la détection est active, l'écran de
démarrage **n'est plus maintenu sous tension à l'arrêt**. Le script le
rallumera si besoin, et le garder allumé contredirait le but recherché —
tout couper. Si la détection est désactivée, il redevient la seule garantie
de voir le POST, et reste donc alimenté.

Les prises à rallumer voyagent par le KVS de l'appareil : l'application y
dépose la liste à chaque changement de profil. C'est le seul moment où elle
peut le lui dire — après, elle n'est plus là.

Si aucun profil n'a jamais été appliqué, le KVS est vide et le script s'en
tient à l'écran de démarrage. Pour éviter ce démarrage à un seul écran sans
explication, l'application publie l'**état courant** au lancement quand elle
ne trouve rien : ce qui est allumé maintenant est vraisemblablement ce qu'on
veut retrouver.

### Ce que le script ne touche jamais

Les prises marquées **Critical** sont exclues du script. C'est là que doit se
trouver le concentrateur USB portant le clavier : rallumé en même temps que
les écrans, il ne serait pas énuméré assez tôt pour entrer dans le BIOS.

## Protéger l'appareil par un mot de passe

Onglet **Devices** → **Password...**. La protection est **optionnelle** et se
pose depuis l'application : saisie, confirmation, puis *Apply to device*.
L'utilisateur est toujours `admin` — seul le mot de passe se choisit.

Sans elle, n'importe quel appareil du réseau local peut commander les
prises, exécuter un script sur la multiprise ou changer sa configuration
Wi-Fi, sans rien avoir à fournir.

Trois actions :

| Bouton | Effet |
| --- | --- |
| *Apply to device* | Pose le mot de passe sur l'appareil et le mémorise |
| *Remember only* | Mémorise un mot de passe déjà posé ailleurs, sans toucher à l'appareil |
| *Remove password* | Retire la protection de l'appareil |

### Où le mot de passe est rangé

Chiffré par **DPAPI**, le service de Windows prévu pour cela, et le chiffré
est rangé dans `config.json`. La clé dérive de ton compte Windows : elle
n'est ni dans le fichier, ni dans le code source. Un `config.json` recopié
ailleurs, lu par un autre compte ou restauré sur une autre machine ne donne
rien.

Ce n'est pas un coffre-fort : un programme lancé **sous ta session** peut
demander à Windows de déchiffrer. Cela protège du fichier qui voyage, pas
d'un logiciel malveillant déjà en place. Un chiffrement à clé embarquée dans
le code n'aurait, lui, été que de l'obfuscation.

### Si le mot de passe est perdu

L'application le détecte : l'appareil passe en `auth failed` dans la liste,
un bandeau apparaît dans l'onglet **Devices**, le menu de l'icône affiche
*Password refused*, et une bulle prévient une fois. Le bouton **Recovery
steps** rappelle la marche à suivre :

1. Débrancher la multiprise, puis la rebrancher.
2. Dans les **60 premières secondes**, presser ensemble les boutons **1 et 4**.
3. Les maintenir **10 secondes pleines**, puis relâcher.

Relâcher vers 5 secondes ne fait qu'une *réinitialisation réseau*, qui
rallume le point d'accès Wi-Fi intégré — **ouvert** sur ce modèle. Tenir les
10 secondes.

Une réinitialisation d'usine efface tout : mot de passe, identifiants Wi-Fi,
scripts et noms de prises.

## Icône

Le jeu d'icônes est dans `windows-icons/`, à la racine, tel que le
générateur `icongen_windows.py` du projet `App web ico` le produit — le
dossier est recopié sans être réorganisé, pour qu'une régénération se
résume à un remplacement.

Neuf tailles — 16, 20, 24, 32, 40, 48, 64, 96 et 256 — rassemblées dans
`icon.ico`, en BMP jusqu'à 48 px et en PNG au-delà, toutes avec canal
alpha. Les PNG à côté servent à la composition de l'icône de notification
et à l'icône de fenêtre.

Dans la zone de notification, l'icône n'est pas figée : le visuel porte une
**pastille d'état** en bas à droite — verte quand des écrans sont
alimentés, grise quand tout est coupé, orange quand un appareil manque à
l'appel ou refuse son mot de passe, rouge quand plus rien ne répond. Le
décompte des prises et la puissance tiennent dans l'infobulle : à seize
pixels de côté, une pastille se lit, un décompte non.

L'icône est composée à la volée, à partir de la taille exacte demandée
plutôt que d'une seule image que Windows réduirait. Cela suppose de lire
les pixels du PNG, ce que la bibliothèque standard ne fait pas :
`win/images.py` contient un décodeur minimal, limité à ce dont
l'application a besoin.

Pour changer l'icône, régénérer le jeu avec `icongen_windows.py` et
remplacer le dossier `windows-icons/` par celui qu'il produit. Les
contraintes Windows sont consignées dans
[docs/windows-icons.md](docs/windows-icons.md).

## Langue

Onglet **Behaviour** → **Appearance** → **Language** : *Follow Windows*,
*English* ou *Français*. Anglais par défaut, comme le reste de
l'application.

Changer de langue **reconstruit la fenêtre**. Les widgets Tk lisent leur
texte une fois, à la construction ; les retraduire après coup demanderait
de tenir un registre de chacun, et le moindre oubli laisserait un libellé
dans l'ancienne langue. Une fenêtre à moitié traduite est pire que pas de
traduction du tout : elle oblige à traduire mentalement à chaque coup
d'œil.

Le texte anglais sert de clé de traduction. Il reste donc lisible dans le
code, et une entrée manquante retombe sur l'anglais plutôt que d'afficher
un identifiant nu. Le catalogue est dans
[shelly_screens/locale_fr.py](shelly_screens/locale_fr.py) ; il n'y a ni
fichier à compiler, ni dépendance.

Les phrases à valeurs variables utilisent des champs nommés plutôt que des
f-strings : une f-string serait évaluée avant la traduction, et la chaîne
obtenue ne servirait plus de clé.

## Thèmes

Onglet **Behaviour** → **Appearance** : *Follow Windows*, *Light* ou *Dark*.
Le changement est immédiat, sans rouvrir la fenêtre, et en mode *Follow
Windows* un basculement du thème de Windows est suivi dans les trois
secondes.

La couleur d'accentuation est reprise de tes réglages Windows. Elle n'est
pas utilisée telle quelle : sur fond sombre, le bleu par défaut ne laisserait
que 2,3:1 de contraste à un texte blanc. Elle est donc éclaircie juste ce
qu'il faut, sans toucher à sa teinte, et la couleur du texte posé dessus est
choisie par calcul de contraste WCAG. En mode clair l'accent est conservé tel
quel, puisqu'il ressort déjà.

L'interface utilise le thème ttk `clam` dans les trois cas. Le thème natif
`vista` est plus joli en clair, mais il dessine ses widgets avec des images
du système : ses fonds ne se colorent pas et le mode sombre y resterait
blanc par endroits. La barre de titre, elle, n'appartient pas à Tk — elle
bascule via `DwmSetWindowAttribute`.

## Ce que fait l'application toute seule

* **Mise en veille et arrêt de Windows** — coupe toutes les prises sauf
  l'écran de démarrage, les prises critiques et celle du PC. La coupure est
  synchrone et sans temporisation : Windows n'accorde que quelques instants
  avant de suspendre le processus.
* **Réveil** — attend trois secondes que le réseau revienne, puis réapplique
  le dernier profil.
* **Changement de profil** — mémorise la disposition des fenêtres du profil
  quitté, **allume** les écrans manquants, attend que Windows les voie,
  **puis seulement** coupe le reste, et rejoue la disposition mémorisée.

Allumer avant de couper évite de se retrouver, ne serait-ce qu'un instant,
sans aucun écran, et laisse aux dalles leurs quelques secondes
d'initialisation.

Un appareil injoignable n'empêche pas les autres de répondre : ses prises
sont laissées telles quelles et signalées dans le menu.

## Organisation du code

```
main.py                      point d'entrée
windows-icons/               jeu d'icônes, produit par icongen_windows.py
install-startup.ps1          raccourcis de lancement sans console
run-console.cmd              lancement de diagnostic, avec console
config.json                  configuration (généré au premier lancement)
shelly-screens.log           journal (généré, rotatif)
shelly_screens/
  device.py                  client JSON-RPC Shelly Gen2+
  discovery.py               localisation : adresse connue, mDNS, balayage
  config.py                  modèle de configuration, migration, persistance
  controller.py              enchaînement appareils / prises / écrans / fenêtres
  app.py                     icône, menu, événements système
  win/
    api.py                   ctypes communs, conscience du DPI
    monitors.py              énumération des écrans, clé stable
    layout.py                capture et restauration des fenêtres
    icon.py                  génération de l'icône
    shell.py                 fenêtre cachée, zone de notification, messages
  ui/settings.py             fenêtre de réglages (tkinter)
```

## Points techniques

**Références de prise.** Une prise se désigne par `<clé appareil>:<sortie>`,
par exemple `strip2:1`. La clé est stable même si l'adresse IP change, et le
renommage d'une clé se propage aux prises et aux profils.

**Conscience du DPI.** Le processus passe en Per-Monitor V2 dès l'import de
`win.api`. Sans cela, Windows virtualise les coordonnées des fenêtres sur les
écrans mis à l'échelle et les positions relevées sont fausses.

**Identification des écrans.** `\.\DISPLAY1` n'est qu'un rang d'énumération
qui change dès qu'un écran s'allume ou s'éteint ; le modèle ne distingue pas
deux écrans identiques. On utilise le chemin d'interface renvoyé par
`EnumDisplayDevices` avec `EDD_GET_DEVICE_INTERFACE_NAME`, réduit à
`<matériel>#UID<sortie>`.

**Appariement des fenêtres.** Dans une même session, le handle suffit. Après
un redémarrage il ne vaut plus rien : on rapproche alors les fenêtres par
`(exécutable, classe)`, puis par titre, puis par ordre d'apparition.

**Authentification.** Digest SHA-256, utilisateur `admin` imposé. Le calcul
est celui du RFC 7616, avec `ha2` dérivé de la méthode et de l'URI. La
documentation de Shelly décrit pour d'autres firmwares un `ha2` constant
calculé sur `dummy_method:dummy_uri` ; **ce firmware-ci le refuse** —
vérifié sur l'appareil, seul le calcul standard passe. La différence
compte : un `ha2` constant rendrait la réponse indépendante de la requête,
donc rejouable pour déclencher une autre commande.

**Adresses IP.** La résolution essaie d'abord le **nom mDNS**
`<device-id>.local`, puis l'adresse mémorisée, puis un balayage des
sous-réseaux locaux, en vérifiant à chaque fois l'adresse MAC.

Le nom passe en premier même quand l'adresse répond encore : un bail DHCP se
renouvelle sans prévenir, et l'adresse retenue finit gravée ailleurs — dans le
script embarqué, qui ne se corrige pas tout seul. Le nom, lui, suit
l'appareil : il survit à un changement de sous-réseau, de firmware, et même à
une réinitialisation d'usine, puisqu'il dérive du modèle et de l'adresse MAC.

Une première résolution mDNS interroge le réseau en multicast et demande
jusqu'à trois secondes, là où une adresse connue répond en quelques
millisecondes ; elle dispose donc d'un délai propre, plus généreux.

Les requêtes visent explicitement l'**IPv4** : ces appareils annoncent aussi
une adresse lien-local `fe80::`, sur laquelle la connexion échoue faute
d'identifiant de portée.

**Reconnexions bridées.** Une lecture qui échoue ne déclenche plus une
résolution complète : l'appareil est laissé tranquille 15 s, puis 30, puis 60,
et le compteur repart à zéro dès qu'il répond. Une même résolution ne se
retente pas plus d'une fois par demi-minute. L'ancien comportement faisait
l'inverse — il accablait de requêtes un appareil déjà en difficulté, ce qui a
précédé deux réinitialisations par chien de garde.

**Format de configuration.** La version 2 remplace l'appareil unique de la
version 1 par une liste. Une configuration en version 1 est migrée à la
volée, sans intervention.

## Version

Deux nombres, et rien de plus. Le numéro est dans
[shelly_screens/__init__.py](shelly_screens/__init__.py), affiché dans le
titre de la fenêtre de réglages et sur la première ligne du journal.

La **majeure** change quand la configuration existante ne suffit plus telle
quelle : un format de fichier qui évolue, un réglage dont le sens change, un
script embarqué incompatible avec l'ancien. Autrement dit, quand une mise à
jour demande de vérifier quelque chose plutôt que de simplement redémarrer.

La **mineure** change à chaque itération — correction, ajout, mesure de
robustesse — même pour un détail. Son rôle n'est pas de résumer l'ampleur du
travail mais de répondre à une seule question, posée un jour de panne :
*quelle version tourne devant moi ?* Un journal qui ne dit pas de quel code
il parle fait perdre plus de temps qu'il n'en fait gagner.

## Dépannage

| Symptôme | Piste |
| --- | --- |
| Icône rouge | Aucun appareil joignable. `Devices` → `Reconnect`, ou vérifier l'alimentation des multiprises. |
| Un appareil « offline » | Les autres continuent de fonctionner. Ses prises apparaissent avec un état `-` et ne sont pas manœuvrées. |
| Une prise reste « not identified » | Normal pour un concentrateur USB ou l'unité centrale. Pour un écran : relancer l'assistant, l'écran mettait peut-être plus de douze secondes à se déconnecter. |
| Les fenêtres ne reviennent pas en place | La disposition n'a pas été enregistrée pour ce profil, ou les applications concernées ont été fermées depuis. |
| Changement de profil très lent | Un écran attendu ne revient pas : l'attente va jusqu'au délai maximal (20 s par défaut, réglable dans `Behaviour`). |
| Rien au démarrage du PC | Vérifier qu'une prise porte le rôle **Boot screen**, et que le concentrateur USB du clavier est **Critical**. |
| Une veille ne coupe plus rien | La voie de mesure du firmware peut se figer : l'application le détecte et le signale dans le menu de l'icône, avec un bouton pour redémarrer la multiprise. Un redémarrage est sans danger — relais bistables, et `initial_state` ramène la prise du PC sous tension. |
| Les écrans se coupent alors que le PC tourne | Seuil de coupure trop haut. Relancer une mesure, ou le baisser dans `PC power`. |
| Rien ne se rallume au démarrage du PC | Vérifier dans `PC power` que le script est `running`, et qu'une prise porte le rôle **Boot screen**. |
| Le clavier ne répond pas dans le BIOS | Son concentrateur USB doit être marqué **Critical**, pas seulement piloté par le script. |
| Une console s'ouvre au lancement | Le raccourci doit viser `pythonw.exe`, pas `python.exe`. Le réinstaller avec `install-startup.ps1`. |
| Aucune trace de ce que fait l'application | Menu de l'icône → **Open log file**, ou ouvrir `shelly-screens.log`. |
| Un appareil en `auth failed` | Il répond mais refuse le mot de passe. `Devices` → `Password...`, ou réinitialisation par les boutons si perdu. |
| Fenêtre claire alors que Windows est sombre | Le mode doit être sur *Follow Windows* dans `Behaviour` → `Appearance`. Le réglage lu est `AppsUseLightTheme` dans le registre. |

## Une seule instance

Un second lancement ne démarre pas : il demande à l'instance en place
d'ouvrir ses réglages, puis se retire. C'est le comportement attendu d'un
programme à icône, dont la fenêtre est souvent fermée.

Ce n'est pas un confort mais une **nécessité**. Deux instances gardent
chacune sa configuration en mémoire et l'écrivent entière à chaque
enregistrement : la dernière à écrire efface le travail de l'autre. Une
calibration fraîchement relevée a ainsi disparu, remplacée par une copie
plus ancienne.

Le verrou est un mutex nommé de Windows. Il appartient au processus et
disparaît avec lui, même tué brutalement — un fichier verrou, lui,
survivrait et bloquerait tout lancement ultérieur.

## Journaux

Tout est écrit dans `shelly-screens.log`, à côté de la configuration, avec
rotation à 512 Ko sur trois fichiers. Le menu de l'icône propose **Open log
file** pour l'ouvrir directement.

C'est indispensable et non accessoire. Sans console, `sys.stdout` vaut
`None` et `print` ne lève aucune erreur : il n'écrit simplement nulle part.
Un programme qui se contenterait de `print` tournerait donc parfaitement
muet, sans le moindre moyen de savoir ce qu'il fait. Les exceptions non
rattrapées, y compris dans les threads, sont également déroutées vers ce
fichier.

`run-console.cmd` ajoute l'affichage à l'écran, en plus du fichier.

Chaque ligne est écrite **jusqu'au disque** (`fsync`), et pas seulement
remise au système. Sans cela, les dernières lignes se perdent quand la
machine s'arrête brutalement — précisément celles qui expliqueraient
pourquoi.
