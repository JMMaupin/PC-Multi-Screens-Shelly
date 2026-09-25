"""Catalogue francais.

La cle est le texte anglais, tel qu'il apparait dans le code. Une entree
manquante retombe donc sur l'anglais plutot que sur un identifiant nu.

Les champs entre accolades sont nommes et doivent se retrouver a
l'identique dans la traduction ; l'ordre, lui, est libre.
"""

from __future__ import annotations

CATALOG: dict[str, str] = {
    # --- onglets et titres
    "Shelly Screens": "Shelly Screens",
    "Shelly Screens - Settings": "Shelly Screens - Reglages",
    "Devices": "Appareils",
    "Outlets": "Prises",
    "Profiles": "Profils",
    "PC power": "Alimentation PC",
    "Behaviour": "Comportement",
    # --- colonnes
    "Key / name": "Cle / nom",
    "Model": "Modele",
    "Reached via": "Joint par",
    "IP address": "Adresse IP",
    "Address": "Adresse",
    "Outlet": "Prise",
    "Type": "Type",
    "State": "Etat",
    "Power": "Puissance",
    "Display": "Ecran",
    "Role": "Role",
    "Status": "Statut",
    "Password": "Mot de passe",
    "Device ID": "Identifiant",
    "Name": "Nom",
    "Confirm": "Confirmation",
    # --- types de prise
    "Not set": "Non defini",
    "Screen": "Ecran",
    "Accessory": "Accessoire",
    # --- boutons
    "Add device...": "Ajouter un appareil...",
    "Rename key...": "Renommer la cle...",
    "Set label...": "Definir le libelle...",
    "Password...": "Mot de passe...",
    "Open web UI": "Ouvrir l'interface web",
    "Reconnect": "Reconnecter",
    "Remove": "Retirer",
    "Toggle outlet": "Basculer la prise",
    "Identify displays...": "Identifier les ecrans...",
    "Identify displays": "Identifier les ecrans",
    "Clear display link": "Oublier l'association",
    "New": "Nouveau",
    "Rename": "Renommer",
    "Delete": "Supprimer",
    "Apply this profile now": "Appliquer ce profil",
    "Start measuring": "Lancer la mesure",
    "Stop measuring": "Arreter la mesure",
    "Read now": "Relire",
    "Use suggested thresholds": "Utiliser les seuils proposes",
    "Install / update": "Installer / mettre a jour",
    "Scan network": "Balayer le reseau",
    "Add selected": "Ajouter la selection",
    "Close": "Fermer",
    "Check": "Verifier",
    "Cancel": "Annuler",
    "Apply to device": "Appliquer a l'appareil",
    "Remember only": "Memoriser seulement",
    "Remove password": "Retirer le mot de passe",
    "Lost password?": "Mot de passe perdu ?",
    "Recovery steps": "Marche a suivre",
    "Search again": "Rechercher a nouveau",
    # --- cadres
    "Selected outlet": "Prise selectionnee",
    "Powered outlets": "Prises alimentees",
    "Sleep and shutdown": "Veille et arret",
    "Appearance": "Apparence",
    "Timing": "Temporisation",
    "Measurement": "Mesure",
    "Thresholds and delays": "Seuils et delais",
    "On-device script": "Script embarque",
    "Power strip": "Multiprise",
    # --- cases a cocher
    "Critical - never switched off": "Critique - jamais coupee",
    'After a profile change, bring windows left outside every lit screen back onto the nearest one': "Apres un changement de profil, ramener sur l'ecran allume le plus proche les fenetres restees hors de tout ecran",
    'Profile shortcut': 'Raccourci des profils',
    'Disable': 'Desactiver',
    'Add Ctrl, Alt or Win: a shortcut without them would take the key away from every other program.': 'Ajouter Ctrl, Alt ou Win : sans eux, le raccourci volerait la touche a tous les autres programmes.',
    'Active: {hotkey} shows the profile buttons in the middle of the main screen.': "Actif : {hotkey} affiche les boutons des profils au centre de l'ecran principal.",
    '{hotkey} is available. Click Apply to use it.': "{hotkey} est disponible. Cliquer sur Appliquer pour l'utiliser.",
    '{hotkey} is already used by another program or by Windows.': '{hotkey} est deja utilise par un autre programme ou par Windows.',
    'No shortcut is active at the moment.': "Aucun raccourci n'est actif pour l'instant.",
    'Windows refused {hotkey}: another program took it.': "Windows a refuse {hotkey} : un autre programme l'a pris.",
    'The shortcut {hotkey} is already used by another program. Choose another one in Settings > Behaviour.': 'Le raccourci {hotkey} est deja utilise par un autre programme. En choisir un autre dans Reglages > Comportement.',
    'Arrows and Enter to choose, Esc to close': 'Fleches et Entree pour choisir, Echap pour fermer',
    'Applied.': 'Applique.',
    'Applied to {count} device(s).': 'Applique a {count} appareil(s).',
    'LEDs...': 'LED...',
    'LEDs and buttons - {device}': 'LED et boutons - {device}',
    'Each outlet has a light ring and a push button. These settings are stored in the device and apply at once. If it asks for a restart, the button below does it without switching any outlet.': "Chaque prise porte un anneau lumineux et un bouton. Ces reglages sont enregistres dans l'appareil et s'appliquent aussitot. S'il demande un redemarrage, le bouton ci-dessous s'en charge sans commuter aucune prise.",
    'Restart to apply': 'Redemarrer pour appliquer',
    'Apply to all devices': 'Appliquer a tous les appareils',
    'Apply': 'Appliquer',
    'Light rings': 'Anneaux lumineux',
    'Power: the colour follows the load': 'Puissance : la couleur suit la charge',
    'State: one colour when on, another when off': 'Etat : une couleur allumee, une autre eteinte',
    'Off': 'Eteints',
    'Brightness': 'Luminosite',
    'When on': 'Allumee',
    'When off': 'Eteinte',
    'Night mode': 'Mode nuit',
    'Dim the rings between these times': 'Attenuer les anneaux entre ces heures',
    'From': 'De',
    'to': 'a',
    'HH:MM, device clock': "HH:MM, horloge de l'appareil",
    'Push buttons': 'Boutons',
    'A detached button no longer switches its outlet: only this app does. Takes effect at once.': "Un bouton detache ne commute plus sa prise : seule l'application le fait. Effet immediat.",
    'Colour...': 'Couleur...',
    'Ring colour when on': "Couleur de l'anneau, prise allumee",
    'Ring colour when off': "Couleur de l'anneau, prise eteinte",
    'This device has no light rings or buttons to set: it is not a Power Strip.': "Cet appareil n'a ni anneau ni bouton a regler : ce n'est pas une Power Strip.",
    '{outlet}: detached - it powers the PC, the app keeps it that way': "{outlet} : detache - il alimente le PC, l'application l'impose",
    '{outlet}: detached': '{outlet} : detache',
    'Restart needed for the rings to change: {devices}. No outlet is switched by a restart.': 'Redemarrage necessaire pour que les anneaux changent : {devices}. Un redemarrage ne commute aucune prise.',
    "'{value}' is not a time: use HH:MM, for example 22:00.": "'{value}' n'est pas une heure : utiliser HH:MM, par exemple 22:00.",
    'Not reached: {devices}': 'Injoignable(s) : {devices}',
    'asleep': 'en veille',
    'Export': 'Exporter',
    'CSV, standard (comma, decimal point)...': 'CSV standard (virgule, point decimal)...',
    'CSV for Excel, regional settings ({delimiter} and {decimal})...': 'CSV pour Excel, reglages regionaux ({delimiter} et {decimal})...',
    '{count} point(s) exported to {name}': '{count} point(s) exporte(s) vers {name}',
    'Consumption history...': 'Historique de consommation...',
    'Consumption history': 'Historique de consommation',
    'Consumption history - {outlet}': 'Historique de consommation - {outlet}',
    'Keep history for': "Conserver l'historique pendant",
    'days': 'jours',
    'Open history...': "Ouvrir l'historique...",
    'Span': 'Duree',
    '{outlet} (removed)': '{outlet} (supprimee)',
    'No outlet configured.': "Aucune prise n'est configuree.",
    '1 h': '1 h',
    '6 h': '6 h',
    '24 h': '24 h',
    '7 d': '7 j',
    '30 d': '30 j',
    'Live': 'Direct',
    'Scale': 'Echelle',
    'Log': 'Log',
    'Linear': 'Lineaire',
    'Mouse wheel to zoom, drag to scroll, double-click to go back to live.': 'Molette pour zoomer, glisser pour faire defiler, double-clic pour revenir au direct.',
    'No data recorded yet.': "Aucune donnee enregistree pour l'instant.",
    'No data in this range.': 'Aucune donnee sur cette periode.',
    'no data': 'pas de donnee',
    '(on-device probe, PC asleep)': '(releveur embarque, PC en veille)',
    'Min {low} W   average {avg} W   max {high} W   energy {kwh} kWh   measured {covered} of {span}': 'Min {low} W   moyenne {avg} W   max {high} W   energie {kwh} kWh   mesure {covered} sur {span}',
    'History, up to {when}': "Historique, jusqu'a {when}",
    '{m} min': '{m} min',
    '{h} h {m:02d}': '{h} h {m:02d}',
    '{d} d {h} h': '{d} j {h} h',
    'Mon': 'lun.',
    'Tue': 'mar.',
    'Wed': 'mer.',
    'Thu': 'jeu.',
    'Fri': 'ven.',
    'Sat': 'sam.',
    'Sun': 'dim.',
    'No outlet is marked as powering the PC.': "Aucune prise n'est marquee comme alimentant le PC.",
    '(always on)': '(toujours alimentee)',
    'a password is stored': 'un mot de passe est memorise',
    'no password stored': 'aucun mot de passe memorise',
    'The device refuses the current credentials ({failure}).\nEnter the right password and use « Remember only », or reset the device with its buttons.': "L'appareil refuse les identifiants actuels ({failure}).\nSaisissez le bon mot de passe et utilisez « Memoriser seulement », ou reinitialisez l'appareil par ses boutons.",
    "Device '{key}': {state}.": "Appareil '{key}' : {state}.",
    'Signal': 'Signal',
    'excellent': 'excellent',
    'good': 'bon',
    'fair': 'moyen',
    'weak': 'faible',
    'Frozen measurement: {outlets}': 'Mesure figee : {outlets}',
    'Shelly Screens {version} - Settings': 'Shelly Screens {version} - Reglages',
    'On-device script is out of date': "Le script embarque n'est plus a jour",
    'Update it now': 'Le mettre a jour maintenant',
    'stored': 'memorise',
    'unknown': 'inconnu',
    'none, stored': 'aucun, memorise',
    'Radio for Zigbee networks, so a hub can switch the outlets. It only exists on the Zigbee firmware variant.': "Radio pour reseaux Zigbee, afin qu'un pont puisse commander les prises. N'existe que sur la variante de firmware Zigbee.",
    'Unused here, and worst of all when it cannot join a network: it keeps retrying, which is exactly the kind of load that trips the firmware watchdog. On the standard firmware the row stays greyed.': "Inutile ici, et pire encore lorsqu'il ne parvient pas a rejoindre un reseau : il reessaie sans fin, exactement le genre de charge qui declenche le chien de garde du firmware. Sur le firmware standard, la ligne reste grisee.",
    'A greyed row means this firmware does not carry that service at all, not that it is switched off. BTHome sensors have no row of their own: they ride on Bluetooth and stay inert while it is off.': "Une ligne grisee signifie que ce firmware ne porte pas ce service du tout, et non qu'il est eteint. Les capteurs BTHome n'ont pas de ligne propre : ils reposent sur le Bluetooth et restent inertes tant qu'il est eteint.",
    'Zigbee is not listed because it is not a service here: the device web page offers it as a different firmware to install in place of this one, which is a deliberate reflash. BTHome sensors ride on Bluetooth and stay inert while it is off.': "Zigbee ne figure pas ici parce que ce n'est pas un service : la page web de l'appareil le propose comme un firmware different, a installer a la place de celui-ci, ce qui releve d'un reflashage volontaire. Les capteurs BTHome reposent sur le Bluetooth et restent inertes tant qu'il est eteint.",
    'This firmware does not expose the setting; check the device web page.': "Ce firmware n'expose pas ce reglage ; voir la page web de l'appareil.",
    'Cannot reach the device: {error}': 'Appareil injoignable : {error}',
    'Smart-home standard, so Apple Home, Google Home or Alexa can switch the outlets.': "Standard domotique, pour qu'Apple Home, Google Home ou Alexa puissent commander les prises.",
    'Unused here: this app drives the outlets over the local API. Matter still keeps an IPv6 stack and a permanent mDNS announcement running for nothing.': "Inutile ici : l'application pilote les prises par l'API locale. Matter maintient pourtant une pile IPv6 et une annonce mDNS permanente pour rien.",
    "Permanent outbound link to Shelly's servers, so their phone app can reach the device from anywhere.": "Liaison sortante permanente vers les serveurs Shelly, pour que leur application mobile joigne l'appareil de n'importe ou.",
    'Unused here: everything happens on your own network. The link is also a way in that nothing on your side controls.': "Inutile ici : tout se passe sur votre reseau. Cette liaison est aussi une porte d'entree que rien chez vous ne controle.",
    'Local radio, used to set the device up and to read Shelly BLE sensors.': "Radio locale, utilisee pour configurer l'appareil et lire les capteurs Shelly BLE.",
    'Unused for switching, but it is the only way to reconfigure Wi-Fi on a device that has dropped off the network. Turning it off leaves a factory reset as the only rescue.': "Inutile au pilotage, mais c'est le seul moyen de reconfigurer le Wi-Fi d'un appareil devenu injoignable. Le couper ne laisse plus que la reinitialisation d'usine comme recours.",
    'Publishes readings to a message broker, for home-automation systems that subscribe to them.': "Publie les mesures vers un courtier de messages, pour les systemes domotiques qui s'y abonnent.",
    'Unused here: nothing subscribes, and there is no broker.': "Inutile ici : personne ne s'y abonne, et il n'y a pas de courtier.",
    'Building-automation bus, used in wired installations.': 'Bus de domotique cablee, utilise dans les installations filaires.',
    'Unused here: there is no KNX bus on this network.': "Inutile ici : il n'y a pas de bus KNX sur ce reseau.",
    'Pushes status to a server you run, without being asked.': "Pousse son etat vers un serveur que vous hebergez, sans qu'on le lui demande.",
    'Unused here: this app queries the device itself.': "Inutile ici : l'application interroge elle-meme l'appareil.",
    'Services...': 'Services...',
    'Services - {device}': 'Services - {device}',
    'None of these services is needed to switch outlets: this app talks to the device over its local API. Each one still keeps a network stack and its share of memory alive, and they are all on by default.': "Aucun de ces services n'est necessaire pour commander les prises : l'application dialogue avec l'appareil par son API locale. Chacun garde pourtant une pile reseau et sa part de memoire vivantes, et tous sont actifs par defaut.",
    'Free memory: {free} of {total} bytes, lowest since start {low}.': 'Memoire libre : {free} octets sur {total}, minimum atteint depuis le demarrage {low}.',
    'Restart the device': "Redemarrer l'appareil",
    'Some changes need a restart to take effect.': 'Certains changements exigent un redemarrage pour prendre effet.',
    'Reading the device...': "Lecture de l'appareil...",
    'Applying...': 'Application...',
    'Restarting...': 'Redemarrage...',
    'Failed: {error}': 'Echec : {error}',
    "Power steady at {watts} W since the measurement started. A point is "
    "added when it moves, or every {minutes} min.":
        "Puissance stable a {watts} W depuis le debut de la mesure. Un point "
        "est ajoute des qu'elle bouge, ou toutes les {minutes} min.",
    "The device still runs the previous settings: use « Install / update » "
    "to apply them.":
        "L'appareil applique encore les reglages precedents : utilisez "
        "« Install / update » pour les lui transmettre.",
    "Name and key...": "Nom et cle...",
    "Name and key - {device}": "Nom et cle - {device}",
    "The label is yours to choose and appears in this window only. The key "
    "is the short identifier that outlet references and profiles are built "
    "on: changing it rewrites every reference pointing at this device.":
        "L'etiquette est libre et n'apparait que dans cette fenetre. La cle "
        "est l'identifiant court sur lequel reposent les references de "
        "prises et les profils : la changer reecrit toutes les references "
        "qui designent cet appareil.",
    "Label": "Etiquette",
    "Key": "Cle",
    "letters and digits only": "lettres et chiffres uniquement",
    "The key must contain letters or digits.":
        "La cle doit contenir des lettres ou des chiffres.",
    "The key '{key}' is already taken.": "La cle '{key}' est deja prise.",
    "Save": "Enregistrer",
    "Follows the PC - switched off while it sleeps":
        "Suit le PC - coupee pendant sa veille",
    "Screens follow the PC by default. Accessories do not: tick this for "
    "a USB hub or speakers you want cut along with the screens, and leave "
    "it clear for whatever must stay powered through the night.":
        "Les ecrans suivent le PC par defaut, pas les accessoires. Cochez "
        "la case pour un concentrateur USB ou des enceintes a couper avec "
        "les ecrans, laissez-la vide pour ce qui doit rester alimente la "
        "nuit.",
    "Boot screen - fallback if the stored profile is unusable":
        "Ecran de demarrage - secours si le profil memorise est inexploitable",
    "Powers the PC itself - never switched off":
        "Alimente l'unite centrale - jamais coupee",
    "Switch outlets off when the PC sleeps or shuts down":
        "Couper les prises quand le PC se met en veille ou s'arrete",
    "Re-apply the last profile on wake-up":
        "Reappliquer le dernier profil au reveil",
    "Re-apply the last profile when this application starts":
        "Reappliquer le dernier profil au lancement",
    # --- reglages
    "Delay between outlet commands (ms)":
        "Delai entre deux commandes de prise (ms)",
    "Max wait for displays to appear (s)":
        "Attente maximale de l'apparition des ecrans (s)",
    "PC seen as running above": "PC considere actif au-dessus de",
    "PC seen as off below": "PC considere eteint en dessous de",
    "Confirm before switching on": "Confirmation avant d'allumer",
    "Confirm before switching off": "Confirmation avant de couper",
    "Follow Windows": "Suivre Windows",
    "Light": "Clair",
    "Dark": "Sombre",
    "English": "Anglais",
    "Language": "Langue",
    # --- invites
    "Profile name:": "Nom du profil :",
    "New name:": "Nouveau nom :",
    "New profile": "Nouveau profil",
    "Rename profile": "Renommer le profil",
    "Rename key": "Renommer la cle",
    "Device label": "Libelle de l'appareil",
    "Short key, used by profiles (letters and digits):":
        "Cle courte, utilisee par les profils (lettres et chiffres) :",
    "Readable name (Screens, USB hubs, PC...):":
        "Nom lisible (Ecrans, concentrateurs USB, PC...) :",
    "Address or mDNS name": "Adresse ou nom mDNS",
    "Add a Shelly device": "Ajouter un appareil Shelly",
    "Factory reset": "Retour aux reglages d'usine",
    "Identifying displays": "Identification des ecrans",
    # --- menu de l'icone
    "Settings...": "Reglages...",
    "Refresh": "Rafraichir",
    "Open log file": "Ouvrir le journal",
    "Quit": "Quitter",
    "Screens": "Ecrans",
    "Windows": "Fenetres",
    "No screen detected": "Aucun ecran detecte",
    "centre, primary": "centre, principal",
    "centre": "centre",
    "left": "gauche",
    "right": "droite",
    "top": "en haut",
    "bottom": "en bas",
    "shifted right": "decale a droite",
    "shifted left": "decale a gauche",
    "above {names}": "au-dessus de {names}",
    "below {names}": "au-dessous de {names}",
    "{first} and {last}": "{first} et {last}",
    "Add a device...": "Ajouter un appareil...",
    "Fix password...": "Corriger le mot de passe...",
    "No device configured": "Aucun appareil configure",
    "No device reachable": "Aucun appareil joignable",
    # --- messages courts
    "No Shelly device is reachable.": "Aucun appareil Shelly n'est joignable.",
    "No outlet yet - add a device first.":
        "Aucune prise pour l'instant - ajouter d'abord un appareil.",
    "First mark the outlet that powers the PC, in the Outlets tab.":
        "Designer d'abord la prise qui alimente le PC, dans l'onglet Prises.",
    "Select a device first": "Selectionner d'abord un appareil",
    "Select an outlet first": "Selectionner d'abord une prise",
    "Read a measurement first": "Relire d'abord une mesure",
    "Nothing to restore": "Rien a restaurer",
    "No measurement yet.": "Aucune mesure pour l'instant.",
    "Settings saved": "Reglages enregistres",
    "Not connected.": "Non connecte.",
    "No profile selected": "Aucun profil selectionne",
    "No active profile": "Aucun profil actif",
    "No profile configured": "Aucun profil configure",
    "Searching for devices...": "Recherche des appareils...",
    "Scanning the local network...": "Balayage du reseau local...",
    "Applying to the device...": "Application a l'appareil...",
    "Removing...": "Retrait en cours...",
    "Enter a password first.": "Saisir d'abord un mot de passe.",
    "The two entries differ.": "Les deux saisies different.",
    "Too short to be worth setting.": "Trop court pour valoir la peine.",
    "Password set on the device and stored.":
        "Mot de passe pose sur l'appareil et memorise.",
    "Password removed from the device.":
        "Mot de passe retire de l'appareil.",
    "Accepted by the device.": "Accepte par l'appareil.",
    "Unavailable until the PC outlet is set.":
        "Indisponible tant que la prise du PC n'est pas designee.",
    # --- textes d'aide
    "Shelly devices driving the outlets. Two power strips give eight "
    "outlets; a single plug can be added later for the PC itself. "
    "The short key is what profiles refer to, so keep it readable. "
    "Click an IP address to open that device's web interface.":
        "Appareils Shelly pilotant les prises. Deux multiprises donnent huit "
        "prises ; une prise simple peut s'ajouter plus tard pour l'unite "
        "centrale. La cle courte est ce que les profils referencent : la "
        "garder lisible. Cliquer sur une adresse IP ouvre l'interface web de "
        "l'appareil.",
    "Devices already configured are greyed out. Scanning the whole "
    "local network takes about twenty seconds; entering the address "
    "directly is instant.":
        "Les appareils deja configures sont grises. Balayer tout le reseau "
        "local prend une vingtaine de secondes ; saisir l'adresse "
        "directement est immediat.",
    "Start the measurement, then use the PC normally: let it idle, "
    "sleep it, shut it down, start it again. The strip records the "
    "levels on its own while the PC is off.":
        "Lancer la mesure, puis utiliser le PC normalement : le laisser au "
        "repos, le mettre en veille, l'eteindre, le rallumer. La multiprise "
        "releve les paliers toute seule pendant que le PC est eteint.",
    # --- textes d'aide longs, cles relues depuis le code
    'Every display outlet must be set to « Screen »: the wizard only touches what has been declared, and leaves anything still « Not set » alone. Accessories - USB hubs, speakers - stay switchable by profiles but are left out of the display wizard: cutting them makes no screen disappear. A USB hub carrying your keyboard should also be marked critical: without it you could not enter the BIOS or type your PIN at the next boot.':
        "Chaque prise portant un ecran doit etre reglee sur « Ecran » : l'assistant ne manoeuvre que ce qui a ete declare, et laisse tranquille tout ce qui reste « Non defini ». Les accessoires -- concentrateurs USB, enceintes -- restent pilotables par les profils mais sortent du perimetre de l'assistant : les couper ne fera disparaitre aucun ecran. Un concentrateur USB portant le clavier doit en outre etre marque critique : sans lui, impossible d'entrer dans le BIOS ni de saisir son code au prochain demarrage.",
    'Name each outlet and give it a role. Run the wizard once the screens are plugged in: it switches each outlet off in turn and watches which display Windows drops.':
        "Nommer chaque prise et lui donner un role. Lancer l'assistant une fois les ecrans branches : il coupe chaque prise a tour de role et observe quel ecran Windows retire.",
    "Protecting the device stops anyone on the local network from commanding the outlets, running scripts on it or changing its Wi-Fi settings. The user name is always 'admin'; only the password can be chosen.":
        "Proteger l'appareil empeche quiconque sur le reseau local de commander les prises, d'y executer des scripts ou de changer sa configuration Wi-Fi. L'utilisateur est toujours « admin » ; seul le mot de passe se choisit.",
    'The password is stored encrypted with Windows DPAPI: the key comes from your Windows account, not from this program, and the stored value cannot be read by another account or on another machine.':
        'Le mot de passe est chiffre par DPAPI : la cle vient de votre compte Windows, pas de ce programme, et la valeur stockee ne peut etre lue ni par un autre compte, ni sur une autre machine.',
    'Two thresholds, not one: between them lies a dead band where the current state holds, so a fluctuating draw cannot make the relay chatter. The switch-off delay is deliberately long: during a Windows restart the PC drops below the threshold for ten to fifteen seconds, and cutting the screens right then would be the worst moment.':
        "Deux seuils et non un seul : entre les deux se trouve une zone morte ou l'etat courant se maintient, de sorte qu'une consommation fluctuante ne fasse pas claquer le relais. Le delai de coupure est long a dessein : lors d'un redemarrage de Windows, le PC passe sous le seuil pendant dix a quinze secondes, et couper les ecrans a cet instant serait le pire moment.",
    'With the PC plugged into a measured outlet, the power strip can switch the screens on by itself when it sees the PC draw current. That is what allows everything to be switched off at shutdown: no software runs on the PC during POST, but the strip keeps measuring.':
        "Avec le PC branche sur une prise mesuree, la multiprise peut allumer les ecrans d'elle-meme des qu'elle voit le PC consommer. C'est ce qui permet de tout couper a l'arret : aucun logiciel ne tourne sur le PC pendant le POST, mais la multiprise, elle, continue de mesurer.",
    # --- valeurs affichees dans les tableaux
    "not identified": "non identifie",
    "{key} (not connected)": "{key} (non connecte)",
    "critical": "critique",
    "boot": "demarrage",
    "sleeps": "veille",
    "on": "on",
    "off": "off",
    "set": "defini",
    "none": "aucun",
    "online": "en ligne",
    "offline": "hors ligne",
    "auth failed": "mot de passe refuse",
    # --- phrases construites, champs nommes
    "No outlet is marked as powering the PC. Set that role in the Outlets tab first - nothing here can work without it.":
        "Aucune prise n'est designee comme alimentant le PC. Attribuer d'abord "
        "ce role dans l'onglet Prises : rien ici ne peut fonctionner sans lui.",
    "Watching {pc} ({ref}).  Boot screen: {boot}.  Outlets driven by the script: {driven}.":
        "Surveille {pc} ({ref}).  Ecran de demarrage : {boot}.  "
        "Prises pilotees par le script : {driven}.",
    "Script: {state}.  Restored at boot: {outlets}.":
        "Script : {state}.  Retabli au demarrage : {outlets}.",
    "boot screen only": "l'ecran de demarrage seul",
    "none set": "aucun",
    "not installed": "non installe",
    "running": "en service",
    "installed but stopped": "installe mais arrete",
    # --- apparence et resume d'arret
    "Stays powered through sleep and shutdown: {outlets}":
        "Reste alimente en veille et a l'arret : {outlets}",
    "Nothing stays powered through shutdown yet. Mark the outlet of your main screen as boot screen, and any USB hub carrying your keyboard as critical.":
        "Rien ne reste alimente a l'arret pour l'instant. Marquer la prise de "
        "l'ecran principal comme ecran de demarrage, et tout concentrateur USB "
        "portant le clavier comme critique.",
    "Windows is currently in {mode} mode, and this window follows it. Accent colour {accent} comes from your Windows settings.":
        "Windows est actuellement en mode {mode}, et cette fenetre le suit. "
        "La couleur d'accentuation {accent} vient de vos reglages Windows.",
    "Fixed {theme} theme. Accent colour {accent} comes from your Windows settings.":
        "Theme {theme} fixe. La couleur d'accentuation {accent} vient de vos "
        "reglages Windows.",
    "dark": "sombre",
    "PC": "PC",
    "Script {state}": "Script {state}",  # identique, mais present pour que l'audit soit complet
    "light": "clair",
    # --- suivi du releve
    "Measurement running": "Mesure en cours",
    "Measurement stopped": "Mesure arretee",
    "Measurement running - no sample recorded yet.":
        "Mesure en cours - aucun echantillon pour l'instant.",
    "{state} - {count} samples over {minutes} min, from {low} W to {high} W.":
        "{state} - {count} echantillons sur {minutes} min, de {low} W a {high} W.",
    "Off or asleep up to {standby} W, running from {active} W. Suggested: on above {on} W, off below {off} W.":
        "Eteint ou en veille jusqu'a {standby} W, en marche a partir de "
        "{active} W. Propose : allumage au-dessus de {on} W, coupure en "
        "dessous de {off} W.",
    "Only one power level was seen. Let the PC run, sleep and shut down at least once before reading the measurement.":
        "Un seul palier observe. Laisser le PC tourner, se mettre en veille "
        "et s'eteindre au moins une fois avant de relire la mesure.",
    # --- courbe de consommation
    "Show curve...": "Voir la courbe...",
    "Power over time": "Puissance dans le temps",
    "Apply these thresholds": "Appliquer ces seuils",
    "Reading the measurement...": "Lecture de la mesure...",
    "Cannot read the measurement: {error}": "Lecture impossible : {error}",
    "Drag either line to set a threshold. The upper one marks the PC as running, the lower one as off; between them nothing changes, which is what keeps the relays from chattering.":
        "Faire glisser l'une ou l'autre ligne pour placer un seuil. Celle du "
        "haut marque le PC comme actif, celle du bas comme eteint ; entre les "
        "deux rien ne change, et c'est ce qui empeche le relais de claquer.",
    "{count} ticks over {span}, from {low} W to {high} W.":
        "{count} ticks sur {span}, de {low} W a {high} W.",
    "Thresholds set to {on} / {off} W. Reinstall the script to apply them on the device.":
        "Seuils regles a {on} / {off} W. Reinstaller le script pour les poser "
        "sur l'appareil.",
    "running above": "actif au-dessus de",
    "off below": "eteint en dessous de",
    "{label}: {watts} W": "{label} {watts} W",
    "-{min} min": "-{min} min",
    "-{hours} h": "-{hours} h",
    "now": "maintenant",
}
