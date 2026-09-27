// Logger of the PC's power levels.
//
// The application cannot measure what the PC draws while off: it shuts down
// with it. This script, on the other hand, runs on the power strip. It keeps
// two complementary records:
//
//   * a histogram, telling how long was spent at each level -- this is what
//     separates the levels and what the thresholds are based on;
//   * a series of timestamped ticks, showing when the power draw changed
//     and making it possible to place the thresholds by eye.
//
// Ticks are only written when the power really moves. A PC idling, or
// asleep for a whole night, then produces a single point: where regular
// sampling would have filled the memory with identical readings, whole
// days fit in the same space. This is how stock-market ticks work -- record
// the event, not the clock.
//
// Each tick fits in three characters: the power level, then the time
// elapsed since the previous tick on two characters. The whole thing must
// fit in one KVS value, limited to 255 characters.
//
// The configuration is injected by the application; do not edit it here.
// --- CONFIG ---

let ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_";
let EDGES = [2, 5, 10, 20, 40, 80, 160];
let counts = [0, 0, 0, 0, 0, 0, 0, 0];
let seen = 0;
let lowest = -1;
let highest = 0;
let sinceWrite = 0;

// Tick series, and the state of the last one recorded.
let ticks = "";
let lastLevel = -1;
let sinceTick = 0;
// Time of the last tick, as Unix time. Ticks only encode the gaps between
// them: without this anchor, the application would know what happened
// during a sleep, but not when, to within a quarter of an hour.
let lastTickTime = 0;

function bucketOf(watts) {
  for (let i = 0; i < EDGES.length; i++) {
    if (watts < EDGES[i]) {
      return i;
    }
  }
  return EDGES.length;
}

// Logarithmic scale: precision is concentrated below fifteen watts, where
// sleep and shutdown play out, and relaxes above a hundred, where a few
// watts' difference changes nothing in the choice of a threshold.
// mJS has no log1p; Math.log(1 + w) does the same job.
function levelOf(watts) {
  if (watts <= 0) {
    return 0;
  }
  let ratio = Math.log(1 + watts) / Math.log(1 + CFG.maxW);
  let level = Math.round(ratio * 63);
  // The range is required outright rather than clamped by two tests:
  // a NaN slips past both `level < 0` and `level > 63`, and would come
  // out as is. It would then yield no character, and the truncated tick
  // would shift the whole series.
  if (!(level >= 0 && level <= 63)) { return 0; }
  return level;
}

function charOf(value) {
  return ALPHABET.slice(value, value + 1);
}

function recordTick(level, elapsed) {
  // The elapsed time fits in two characters, i.e. 4095 intervals at
  // most. Beyond that, it is capped: such a long gap only happens when
  // the power has not moved, and its exact value teaches nothing.
  if (elapsed > 4095) {
    elapsed = 4095;
  }
  let encoded = charOf(level)
    + charOf(Math.floor(elapsed / 64))
    + charOf(elapsed % 64);
  // Three characters, never two: an incomplete encoding would shift
  // every following tick, and the whole curve would become unreadable.
  if (encoded.length !== 3) {
    return;
  }
  // Sliding queue: the oldest ticks make room.
  if (ticks.length + 3 > CFG.maxChars) {
    ticks = ticks.slice(ticks.length + 3 - CFG.maxChars);
  }
  ticks = ticks + encoded;
  // The device clock is synchronised by SNTP; until it is, unixtime is
  // null and the previous anchor is kept.
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
    // Right after a boot, the power strip has no reading yet: `apower`
    // is null, and any computation on it yields NaN. Wait.
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

    // A tick is kept when the power moves clearly away from the last
    // recorded level -- the small fluctuations of a running PC say
    // nothing useful -- or when too much time has passed without
    // recording anything, so that the curve keeps an anchor point.
    let level = levelOf(watts);
    let gap = level - lastLevel;
    if (gap < 0) { gap = -gap; }
    if (lastLevel < 0 || gap >= CFG.minStep || sinceTick >= CFG.maxSilence) {
      recordTick(level, sinceTick);
      lastLevel = level;
      sinceTick = 0;
      changed = true;
    }

    // Write on a new level or a new tick, otherwise at regular
    // intervals: enough to follow the measurement without wearing the flash.
    if (changed || sinceWrite >= CFG.writeEvery) {
      store();
    }
  });
}

// On startup, pick up the series already written to the KVS.
//
// RAM survives neither a power cut nor a crash of the power strip, and
// without this re-read the logger restarted from an empty curve that it
// immediately wrote over the old one. The history therefore vanished at
// the very moment one was trying to understand what had just happened.
// The duration of the gap is unknown, but a curve missing one restart is
// better than no curve at all.
//
// The timer only starts once the re-read is done: started earlier, the
// first reading would overwrite the series being recovered.
function start() {
  print("pc_probe started on switch " + JSON.stringify(CFG.pc)
    + " with " + JSON.stringify(ticks.length / 3) + " tick(s) restored");
  Timer.set(CFG.poll * 1000, true, tick);
  tick();
}

Shelly.call("KVS.Get", { key: CFG.seriesKey }, function (res, err) {
  if (err === 0 && res !== null && typeof res.value === "string") {
    let kept = res.value;
    // A valid series is made of groups of three characters. A partial
    // remainder is dropped from the head: keeping it would shift the whole plot.
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
