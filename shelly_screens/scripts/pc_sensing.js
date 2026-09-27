// Driving the screens from the PC's power draw.
//
// This script runs on the Shelly that powers the PC. It is the piece that
// makes it possible to cut everything at shutdown: once the PC is off, no
// software can command the outlets any more, but the power strip keeps
// measuring. As soon as it sees the PC drawing power, it switches the
// screens back on -- in time for the POST and the login screen.
//
// It also protects the PC's outlet: if it is switched off, wherever the
// order comes from, it is restored at once. This watch used to live in a
// second script, but the device only runs three at a time, and the third
// slot must stay free for the power-level logger.
//
// The configuration is injected by the application; do not edit it here.
// --- CONFIG ---

// Two thresholds, not one: a power draw hovering around a single value
// would make the relay chatter endlessly. Between the two thresholds lies
// a dead band where the current state holds.
//
// The delays are deliberately asymmetric. A few seconds are enough to
// switch on, but switching off waits much longer: during a Windows
// restart, the PC drops below the threshold for ten to fifteen seconds,
// and cutting the screen at that exact moment would be the worst timing.

let active = null; // null while unknown, then true / false
let above = 0;
let below = 0;
let restored = 0;

// Commands go out one at a time: the firmware limits the number of
// concurrent calls, and seven outlets fired at once saturate it --
// the first ones go through, the rest are silently lost.
// The queue is read with an index rather than with splice, which mJS lacks.
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
  // A lost command is retried, but not forever: past the limit, leave a
  // trace rather than going round in circles.
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
    // An outlet on another power strip: go through its API, and check
    // that the order was actually received. Without this check, a lost
    // command shows up nowhere -- that is how screens stayed dark on
    // resume.
    // The credentials go before the host when the remote power strip
    // requires a password. It is the only form the firmware's HTTP
    // client accepts.
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
  // Last resort: the PC draws power, so it is booting, and we do not know
  // what to switch on. One screen is better than booting blind.
  print("pc_sensing: " + reason + ", falling back to the boot screen");
  if (CFG.boot >= 0) {
    enqueue(CFG.boot, true, 0);
  }
}

function onPcOn() {
  print("PC active - restoring screens");
  // The boot screen is not switched on by default: when the stored
  // profile is usable, it is an outlet like any other and switches on
  // -- or not -- along with them. It only comes into play if that profile
  // is missing or worthless, so that the PC never boots without a picture.
  Shelly.call("KVS.Get", { key: CFG.key }, function (res, err) {
    if (err !== 0 || res === null || typeof res.value !== "string") {
      fallbackToBootScreen("no stored profile");
      return;
    }
    // The KVS only holds a list of indexes into CFG.outlets, for
    // example "[0,2,3]": its value is limited to 255 characters.
    //
    // The shape is checked BEFORE parsing: mJS has no try/catch, and a
    // JSON.parse on invalid text would abort this function without ever
    // reaching the fallback -- the PC would then boot without a picture,
    // precisely the case the fallback must cover.
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
      // An out-of-range index comes from a configuration that changed
      // since the last publication: ignore it rather than command a
      // random outlet.
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
    // Between the two thresholds, neither counter moves: the state holds.

    if (active === null) {
      // First reading: the script is starting, or the power strip is
      // rebooting after a mains outage. Align the outlets with what is
      // observed, without waiting for the delays -- in both directions.
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

// Guardian of the PC's outlet: it is restored as soon as it is switched off,
// wherever the order comes from. Its reach has a limit worth knowing --
// the firmware offers no way to REFUSE a switch-off, only to correct it,
// and the roughly 180 ms reaction comes well after an ATX power supply
// has dropped out. It restores power; it does not prevent the shutdown.
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
