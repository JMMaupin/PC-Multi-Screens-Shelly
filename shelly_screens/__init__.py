"""Pilotage de l'alimentation des ecrans par une ou plusieurs Shelly.

Le numero de version suit deux nombres et rien de plus.

La **majeure** change quand la configuration existante ne suffit plus telle
quelle -- un format de fichier qui evolue, un reglage dont le sens change,
un script embarque incompatible avec l'ancien. Autrement dit : quand une
mise a jour demande de verifier quelque chose plutot que de se contenter de
redemarrer.

La **mineure** change a chaque iteration : correction, ajout, mesure de
robustesse. Elle s'incremente meme pour un detail, parce que son role n'est
pas de resumer l'ampleur du travail mais de repondre a une seule question,
posee un jour de panne : *quelle version tourne devant moi ?* Un journal qui
ne dit pas de quel code il parle fait perdre plus de temps qu'il n'en fait
gagner.
"""

__version__ = "1.5"
