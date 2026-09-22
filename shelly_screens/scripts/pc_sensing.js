// Pilotage des ecrans par la consommation du PC.
//
// Ce script tourne sur la Shelly qui alimente l'unite centrale. Il est la
// piece qui permet de tout couper a l'arret : le PC eteint, plus aucun
// logiciel ne peut commander les prises, mais la multiprise, elle,
// continue de mesurer. Des qu'elle voit le PC consommer, elle rallume les
// ecrans -- a temps pour le POST et l'ecran de connexion.
//
// Il protege aussi la sortie du PC : si elle est coupee, d'ou que vienne
// l'ordre, elle est aussitot retablie. Cette surveillance tenait dans un
// second script, mais l'appareil n'en execute que trois a la fois, et le
// troisieme emplacement doit rester libre pour le releveur de paliers.
//
// La configuration est injectee par l'application ; ne pas l'editer ici.
// --- CONFIG ---

// Deux seuils, et non un seul : une consommation qui oscillerait autour
// d'une valeur unique ferait claquer le relais en boucle. Entre les deux
// seuils se trouve une zone morte ou l'etat courant se maintient.
//
// Les delais sont volontairement asymetriques. Quelques secondes suffisent
// pour allumer, mais la coupure attend beaucoup plus : lors d'un
// redemarrage de Windows, le PC passe sous le seuil pendant dix a quinze
// secondes, et couper l'ecran a cet instant precis serait le pire moment.

let active = null; // null tant qu'on ne sait pas, puis true / false
let above = 0;
let below = 0;
let restored = 0;

// Les commandes partent une par une : le firmware limite le nombre
// d'appels simultanes, et sept prises lancees d'un coup le saturent --
// les premieres passent, les suivantes sont perdues sans un mot.
// La file se lit avec un index plutot qu'avec splice, absent de mJS.
let queue = [];
let head = 0;
let sending = false;

function enqueue(index, on, attempt) {
  queue.push({ i: index, on: on, a: attempt });
  if (!sending) {
    sending = true;
    Timer.set(50, false, pump);
  }
}

function retryLater(job) {
  // Une commande perdue est reprise, mais pas indefiniment : au-dela, on
  // laisse une trace plutot que de tourner en rond.
  if (job.a + 1 >= CFG.tries) {
    print("pc_sensing: outlet " + JSON.stringify(job.i) + " gave up after "
      + JSON.stringify(CFG.tries) + " attempts");
    return;
  }
  Timer.set(CFG.gap * 2, false, function () {
    enqueue(job.i, job.on, job.a + 1);
  });
}

function pump() {
  if (head >= queue.length) {
    queue = [];
    head = 0;
    sending = false;
    return;
  }
  let job = queue[head];
  head = head + 1;
  let outlet = CFG.outlets[job.i];
  if (outlet === undefined) {
    Timer.set(CFG.gap, false, pump);
    return;
  }

  if (outlet.h === null) {
    Shelly.call("Switch.Set", { id: outlet.i, on: job.on });
  } else {
    // Une prise portee par une autre multiprise : on passe par son API, et
    // l'on verifie que l'ordre a bien ete recu. Sans ce controle, une
    // commande perdue ne se voit nulle part -- c'est ainsi que des ecrans
    // restaient eteints au reveil.
    // Les identifiants precedent l'hote quand la multiprise distante
    // demande un mot de passe. C'est la seule forme qu'accepte le client
    // HTTP du firmware.
    let prefix = "";
    if (outlet.u !== undefined && outlet.u !== null && outlet.u !== "") {
      prefix = outlet.u + "@";
    }
    Shelly.call("HTTP.GET", {
      url: "http://" + prefix + outlet.h + "/rpc/Switch.Set?id=" + JSON.stringify(outlet.i)
        + "&on=" + (job.on ? "true" : "false"),
      timeout: 5,
    }, function (res, err) {
      if (err !== 0 || res === null || res.code !== 200) {
        print("pc_sensing: remote outlet " + JSON.stringify(job.i)
          + " failed (err " + JSON.stringify(err) + "), retrying");
        retryLater(job);
      }
    });
  }
  Timer.set(CFG.gap, false, pump);
}

function switchAll(on) {
  for (let i = 0; i < CFG.outlets.length; i++) {
    enqueue(i, on, 0);
  }
}

function fallbackToBootScreen(reason) {
  // Dernier recours : le PC consomme, donc il demarre, et l'on ne sait pas
  // quoi allumer. Un ecran vaut mieux qu'un demarrage a l'aveugle.
  print("pc_sensing: " + reason + ", falling back to the boot screen");
  if (CFG.boot >= 0) {
    enqueue(CFG.boot, true, 0);
  }
}

function onPcOn() {
  print("PC active - restoring screens");
  // L'ecran de demarrage n'est pas allume d'office : quand le profil
  // memorise est exploitable, il fait partie des prises comme une autre et
  // s'allume -- ou non -- avec elles. Il ne ressort que si ce profil
  // manque ou ne vaut rien, pour que le PC ne demarre jamais sans image.
  Shelly.call("KVS.Get", { key: CFG.key }, function (res, err) {
    if (err !== 0 || res === null || typeof res.value !== "string") {
      fallbackToBootScreen("no stored profile");
      return;
    }
    // Le KVS ne contient qu'une liste d'index dans CFG.outlets, par
    // exemple "[0,2,3]" : sa valeur est limitee a 255 caracteres.
    //
    // La forme est verifiee AVANT l'analyse : mJS n'a pas de try/catch, et
    // un JSON.parse sur du texte invalide interromprait cette fonction
    // sans jamais atteindre le repli -- le PC demarrerait alors sans image,
    // precisement le cas que le repli doit couvrir.
    let raw = res.value;
    if (raw.length < 2 || raw.slice(0, 1) !== "[" || raw.slice(-1) !== "]") {
      fallbackToBootScreen("stored profile is not a list");
      return;
    }
    let wanted = JSON.parse(raw);
    if (wanted === null || typeof wanted.length !== "number") {
      fallbackToBootScreen("stored profile unreadable");
      return;
    }

    let applied = 0;
    for (let i = 0; i < wanted.length; i++) {
      let index = wanted[i];
      // Un index hors table vient d'une configuration qui a change depuis
      // la derniere publication : on l'ignore plutot que de commander une
      // prise au hasard.
      if (typeof index === "number" && index >= 0 && index < CFG.outlets.length) {
        enqueue(index, true, 0);
        applied = applied + 1;
      }
    }
    if (applied === 0) {
      fallbackToBootScreen("stored profile empty or out of range");
    }
  });
}

function onPcOff() {
  print("PC idle - switching screens off");
  switchAll(false);
}

function tick() {
  Shelly.call("Switch.GetStatus", { id: CFG.pc }, function (res, err) {
    if (err !== 0 || res === null) {
      return;
    }
    let watts = res.apower;

    if (watts >= CFG.onW) {
      above = above + 1;
      below = 0;
    } else if (watts <= CFG.offW) {
      below = below + 1;
      above = 0;
    }
    // Entre les deux seuils, aucun compteur n'avance : l'etat tient.

    if (active === null) {
      // Premiere mesure : demarrage du script, ou redemarrage de la
      // multiprise apres une coupure secteur. On aligne les prises sur ce
      // qu'on observe, sans attendre les delais -- dans les deux sens.
      if (watts >= CFG.onW) {
        active = true;
        onPcOn();
      } else if (watts <= CFG.offW) {
        active = false;
        onPcOff();
      }
      return;
    }

    if (!active && above >= CFG.onTicks) {
      active = true;
      above = 0;
      onPcOn();
    } else if (active && below >= CFG.offTicks) {
      active = false;
      below = 0;
      onPcOff();
    }
  });
}

// Gardien de la sortie du PC : elle est retablie des qu'elle est coupee,
// d'ou que vienne l'ordre. Sa portee a une limite qu'il faut connaitre --
// le firmware n'offre aucun moyen de REFUSER une coupure, seulement de la
// corriger, et les quelque 180 ms de reaction arrivent bien apres qu'une
// alimentation ATX a lache. Il remet le courant, il n'empeche pas l'arret.
Shelly.addStatusHandler(function (event) {
  if (event.component !== "switch:" + JSON.stringify(CFG.pc)) {
    return;
  }
  if (event.delta !== undefined && event.delta.output === false) {
    restored = restored + 1;
    print("pc_sensing: PC outlet was switched off, restoring ("
      + JSON.stringify(restored) + ")");
    Shelly.call("Switch.Set", { id: CFG.pc, on: true });
  }
});

print("pc_sensing started: on>=" + JSON.stringify(CFG.onW) + "W after "
  + JSON.stringify(CFG.onTicks) + " ticks, off<=" + JSON.stringify(CFG.offW)
  + "W after " + JSON.stringify(CFG.offTicks) + " ticks");
Timer.set(CFG.poll * 1000, true, tick);
tick();
