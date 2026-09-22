// Gardien de la prise de l'unite centrale.
//
// Il surveille la sortie declaree comme alimentant le PC et la retablit
// aussitot qu'elle est coupee, d'ou que vienne l'ordre : application
// locale, application mobile, cloud, ou autre script.
//
// Sa portee a une limite qu'il faut connaitre. Le firmware n'offre aucun
// moyen de REFUSER une commande de coupure : on ne peut que la corriger
// apres coup. La mesure donne environ 180 ms de reaction, quand une
// alimentation ATX ne tient que 16 a 20 ms sans secteur. Ce gardien ne
// sauvera donc pas une session de travail : il remet le courant, il
// n'empeche pas l'arret.
//
// La vraie protection est en amont, dans le logiciel qui envoie les
// ordres. Celui-ci est le dernier filet, pour ce qui ne passe pas par lui.
//
// La configuration est injectee par l'application ; ne pas l'editer ici.
// --- CONFIG ---

let restored = 0;

function protect() {
  restored = restored + 1;
  print("pc_guard: output " + JSON.stringify(CFG.pc)
    + " was switched off, restoring (" + JSON.stringify(restored) + ")");
  Shelly.call("Switch.Set", { id: CFG.pc, on: true });
  // Trace durable : le journal du script se perd au redemarrage, pas le KVS.
  Shelly.call("KVS.Set", {
    key: CFG.key,
    value: JSON.stringify({ n: restored, at: Shelly.getUptimeMs() }),
  });
}

Shelly.addStatusHandler(function (event) {
  if (event.component !== "switch:" + JSON.stringify(CFG.pc)) {
    return;
  }
  if (event.delta !== undefined && event.delta.output === false) {
    protect();
  }
});

// Au demarrage du script -- donc aussi au retour du courant -- la sortie
// doit etre alimentee : le PC ne peut pas demander lui-meme son allumage.
Shelly.call("Switch.GetStatus", { id: CFG.pc }, function (res, err) {
  if (err === 0 && res !== null && res.output === false) {
    print("pc_guard: output was off at startup, switching it on");
    Shelly.call("Switch.Set", { id: CFG.pc, on: true });
  }
});

print("pc_guard armed on switch:" + JSON.stringify(CFG.pc));
