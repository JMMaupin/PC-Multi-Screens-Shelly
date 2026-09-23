// Releve des paliers de consommation de l'unite centrale.
//
// L'application ne peut pas mesurer ce que consomme le PC eteint : elle
// s'eteint avec lui. Ce script, lui, tourne sur la multiprise. Il en tient
// deux traces complementaires :
//
//   * un histogramme, qui dit combien de temps a ete passe a chaque
//     niveau -- c'est lui qui separe les paliers et fonde les seuils ;
//   * une suite de ticks horodates, qui montre quand la consommation a
//     change et permet de placer les seuils a l'oeil.
//
// Les ticks ne sont ecrits que lorsque la puissance bouge vraiment. Un PC
// au repos, ou en veille toute une nuit, ne produit alors qu'un point :
// la ou un echantillonnage regulier aurait sature la memoire de mesures
// identiques, on garde des journees entieres dans la meme place. C'est le
// principe des ticks boursiers -- on enregistre l'evenement, pas l'horloge.
//
// Chaque tick tient en trois caracteres : le niveau de puissance, puis le
// temps ecoule depuis le tick precedent sur deux caracteres. Le tout doit
// entrer dans une valeur du KVS, limitee a 255 caracteres.
//
// La configuration est injectee par l'application ; ne pas l'editer ici.
// --- CONFIG ---

let ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_";
let EDGES = [2, 5, 10, 20, 40, 80, 160];
let counts = [0, 0, 0, 0, 0, 0, 0, 0];
let seen = 0;
let lowest = -1;
let highest = 0;
let sinceWrite = 0;

// Suite de ticks, et etat du dernier enregistre.
let ticks = "";
let lastLevel = -1;
let sinceTick = 0;
// Instant du dernier tick, en temps Unix. Les ticks n'encodent que des
// ecarts entre eux : sans ce repere, l'application saurait ce qui s'est
// passe pendant une veille, mais pas quand, a un quart d'heure pres.
let lastTickTime = 0;

function bucketOf(watts) {
  for (let i = 0; i < EDGES.length; i++) {
    if (watts < EDGES[i]) {
      return i;
    }
  }
  return EDGES.length;
}

// Echelle logarithmique : la precision se concentre sous quinze watts, la
// ou se jouent la veille et l'arret, et se relache au-dela de cent, ou
// quelques watts d'ecart ne changent rien au choix d'un seuil.
// mJS n'offre pas log1p ; Math.log(1 + w) fait le meme office.
function levelOf(watts) {
  if (watts <= 0) {
    return 0;
  }
  let ratio = Math.log(1 + watts) / Math.log(1 + CFG.maxW);
  let level = Math.round(ratio * 63);
  // L'encadrement est exige franchement plutot que borne par deux tests :
  // un NaN echappe a `level < 0` comme a `level > 63`, et ressortirait
  // tel quel. Il ne donnerait alors aucun caractere, et le tick ampute
  // decalerait toute la suite.
  if (!(level >= 0 && level <= 63)) { return 0; }
  return level;
}

function charOf(value) {
  return ALPHABET.slice(value, value + 1);
}

function recordTick(level, elapsed) {
  // Le temps ecoule tient sur deux caracteres, soit 4095 intervalles au
  // plus. Au-dela, on plafonne : un ecart aussi long ne se produit que si
  // la puissance n'a pas bouge, et sa valeur exacte n'apprend rien.
  if (elapsed > 4095) {
    elapsed = 4095;
  }
  let encoded = charOf(level)
    + charOf(Math.floor(elapsed / 64))
    + charOf(elapsed % 64);
  // Trois caracteres, jamais deux : un encodage incomplet decalerait
  // tous les ticks suivants, et la courbe entiere deviendrait illisible.
  if (encoded.length !== 3) {
    return;
  }
  // File glissante : les ticks les plus anciens cedent la place.
  if (ticks.length + 3 > CFG.maxChars) {
    ticks = ticks.slice(ticks.length + 3 - CFG.maxChars);
  }
  ticks = ticks + encoded;
  // L'horloge de l'appareil est synchronisee par SNTP ; tant qu'elle ne
  // l'est pas, unixtime vaut null et l'on garde le repere precedent.
  let sys = Shelly.getComponentStatus("sys");
  if (sys !== null && typeof sys.unixtime === "number") {
    lastTickTime = sys.unixtime;
  }
}

function store() {
  sinceWrite = 0;
  Shelly.call("KVS.Set", {
    key: CFG.key,
    value: JSON.stringify({ n: seen, mn: lowest, mx: highest, b: counts, t: lastTickTime }),
  });
  Shelly.call("KVS.Set", { key: CFG.seriesKey, value: ticks });
}

function tick() {
  Shelly.call("Switch.GetStatus", { id: CFG.pc }, function (res, err) {
    if (err !== 0 || res === null) {
      return;
    }
    let watts = res.apower;
    // Juste apres un demarrage, la multiprise n'a pas encore de mesure :
    // `apower` vaut null, et tout calcul en tire un NaN. On attend.
    if (typeof watts !== "number") {
      return;
    }
    seen = seen + 1;
    sinceWrite = sinceWrite + 1;
    sinceTick = sinceTick + 1;

    let changed = false;
    if (lowest < 0 || watts < lowest) {
      lowest = watts;
      changed = true;
    }
    if (watts > highest) {
      highest = watts;
      changed = true;
    }
    let slot = bucketOf(watts);
    counts[slot] = counts[slot] + 1;

    // Un tick est retenu quand la puissance s'ecarte nettement du dernier
    // niveau enregistre -- les petites fluctuations d'un PC en marche ne
    // disent rien d'utile -- ou quand trop de temps a passe sans rien
    // noter, pour que la courbe garde un point d'ancrage.
    let level = levelOf(watts);
    let gap = level - lastLevel;
    if (gap < 0) { gap = -gap; }
    if (lastLevel < 0 || gap >= CFG.minStep || sinceTick >= CFG.maxSilence) {
      recordTick(level, sinceTick);
      lastLevel = level;
      sinceTick = 0;
      changed = true;
    }

    // On ecrit sur un palier inedit ou un nouveau tick, sinon a intervalle
    // regulier : de quoi suivre la mesure sans user la flash.
    if (changed || sinceWrite >= CFG.writeEvery) {
      store();
    }
  });
}

// Au demarrage, on reprend la suite deja ecrite dans le KVS.
//
// La memoire vive ne survit ni a une coupure ni a un plantage de la
// multiprise, et sans cette relecture le releveur repartait d'une courbe
// vide qu'il ecrivait aussitot par-dessus l'ancienne. L'historique
// disparaissait donc au moment precis ou l'on cherchait a comprendre ce
// qui venait de se passer. Le chronometrage du trou n'est pas connu, mais
// mieux vaut une courbe amputee d'un redemarrage qu'aucune courbe.
//
// Le minuteur ne part qu'une fois la relecture faite : demarre avant, le
// premier releve ecraserait la suite qu'on essaie de recuperer.
function start() {
  print("pc_probe started on switch " + JSON.stringify(CFG.pc)
    + " with " + JSON.stringify(ticks.length / 3) + " tick(s) restored");
  Timer.set(CFG.poll * 1000, true, tick);
  tick();
}

Shelly.call("KVS.Get", { key: CFG.seriesKey }, function (res, err) {
  if (err === 0 && res !== null && typeof res.value === "string") {
    let kept = res.value;
    // Une suite valide est faite de groupes de trois caracteres. Un reste
    // partiel est ecarte par la tete : le garder decalerait tout le trace.
    let extra = kept.length % 3;
    if (extra !== 0) {
      kept = kept.slice(extra);
    }
    if (kept.length > CFG.maxChars) {
      kept = kept.slice(kept.length - CFG.maxChars);
    }
    ticks = kept;
  }
  start();
});
