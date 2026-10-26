// Plate|Fold: the price list that would not fit on a page.
//
// A Bristol-theme price list lies on a desk under a lamp. Each price's
// significant digits sit on a perforated tear-off coupon. As the rates climb,
// a fold-out attached to the coupon opens panel by panel, following
// Plate|Fold's own page ladder (landscape plate, larger sheet, custom page),
// and every panel appends ",000". Rivet, tidying his nest in the mug, ignores
// it until the last panel slaps open and makes him jump. A double take, a look
// at whoever is watching, and he flies down, grips the coupon and tears it off
// in three tugs. The leftover zeros reflow into a single 0, the blank fold-out
// collapses into a flat packet, and he carries the digits home to line his
// nest.
//
// Desk space: u runs right, v runs away from the viewer, z is height. The
// camera looks down at a steep angle, so depth is foreshortened by K.

import { Rivet, DEFAULT_POSE } from "../rivet.js";
import { TAU, clamp, lerp, ease, track, rng, rgba, smoothstep, paperTexture, noise1, springResponse } from "../ink.js";
import { breathAt, blinkSchedule, idleProgramme, applyDeltas, applyDoze, crestFollower } from "../behavior.js";
import { loadArt, drawPlate, coverMatrix, rivetKit } from "../paint.js";

const STORY = 7.0;
const FREE_AT = 5.0;
const K = 0.68; // depth foreshortening
const KZ = 0.95; // height foreshortening
const RIVET_SCALE = 0.95;

// The page, in its own coordinates (sx right, sy down from the far edge).
const PAGE = { u0: -140, w: 280, h: 362 };
const CELL = 16.8; // monospace advance at 28 units
const DOLLAR_SX = 229.2;
const COUPON = { sx0: 246, sx1: 280, sy0: 140, sy1: 322 };
const ROWS = [
  { name: "Note", note: "A page or two of Markdown", digits: "12", sy: 186 },
  { name: "Report", note: "DOCX with tables and diagrams", digits: "24", sy: 238 },
  { name: "Atlas", note: "The diagram that needs its own sheet", digits: "48", sy: 290 },
];
const PANEL_W = CELL * 4; // one ",000" group per fold-out panel
const PANELS = 6;
const LADDER = ["landscape plate", "larger sheet", "custom page", "900 mm", "9 metres", "please stop"];
const OPEN_AT = [0.58, 0.9, 1.17, 1.4, 1.6, 1.82];
const OPEN_FOR = 0.3;
const CLOSED_ANGLE = 1.38; // pleats nearly upright when the fold-out is shut

// The story's beats in seconds; motion, camera, paper and props key off these.
const BEAT = {
  glance: 0.95, // a glance at the growing list, then back to the nest
  slap: 2.02, // "please stop" slaps open and he jumps
  take: 2.4, // double take at the page
  look: 2.8, // a look at whoever is watching, in a close shot
  ready: 3.2, // he decides
  takeoff: 3.34,
  land: 3.84, // on the fold-out beside the coupon
  grip: 4.0, // beak on the TEAR tab
  tugs: [4.2, 4.44, 4.66], // each tears a third of the perforation; the last rips it free
  home: 5.05, // flies home with the strip
  perch: 5.5,
  release: 5.72, // the strip drops into the nest
  fold: 5.12, // the blank fold-out collapses into a flat packet
  check: 6.1, // turns to check the page
  proud: 6.4,
};
const RIP = BEAT.tugs[2];
// The nest mug follows the painted one (art/platefold/mug-v1.png): its rim
// ellipse is centred at (470, 257) px in the manifest's crop, 467 px across
// half its width, drawn at PAINTED_MUG_SCALE world units per pixel.
const MUG = { u: 200, v: 410, h: 100, r: 56 };
const MUG_RIM = { x: MUG.u, y: -MUG.v * K - MUG.h + 6 }; // centre of the rim's ellipse on screen
const PAINTED_MUG = { rimX: 470, rimY: 257, innerRx: 392, innerRy: 210, innerDy: 10 };
const PAINTED_MUG_SCALE = 0.12;
const RELEASE_AT = BEAT.release; // the beak opens over the mug
const DROP_DEPTH = 140; // world units the strip falls into the nest
const RIM = { x: MUG_RIM.x - 467 * PAINTED_MUG_SCALE, y: MUG_RIM.y }; // Rivet's perch: the rim's left edge

const toScreen = (u, v, z = 0) => ({ x: u, y: -v * K - z * KZ });
const pageU = (sx) => PAGE.u0 + sx;
const pageV = (sy) => PAGE.h - sy;
const COUPON_GRIP = toScreen(pageU((COUPON.sx0 + COUPON.sx1) / 2), pageV(COUPON.sy0 + 4));

// ---------------------------------------------------------------- motion

// Each panel's elevation from the desk. Opening flattens them in turn; the
// last one slaps down with a bounce. Closing collapses the accordion flat:
// odd panels flip up and over, far panels first, so the stack ends as a neat
// packet lying beside the page.
function panelTheta(i, t) {
  const open = smoothstep(OPEN_AT[i], OPEN_AT[i] + OPEN_FOR, t);
  const sinceSlap = t - BEAT.slap;
  const slapBounce = i === PANELS - 1 && sinceSlap > 0 ? Math.abs(Math.sin(sinceSlap * 18)) * Math.exp(-sinceSlap * 9) * 0.35 : 0;
  const pleat = CLOSED_ANGLE * (1 - ease.out(open)) + slapBounce;
  if (i % 2 === 0) return pleat;
  const lag = (PANELS - 1 - i) * 0.07;
  const flip = ease.inOut(smoothstep(BEAT.fold + lag, BEAT.fold + 0.36 + lag, t));
  return -pleat * (1 - flip) + Math.PI * flip;
}

// Lay the fold-out panels as a chain from the coupon edge: each starts where
// the last ended, at its own elevation. (u, z) is the crease, theta the
// panel's angle above the desk; a pleat that falls starts at the fold's top.
function foldOut(t) {
  const out = [];
  let u = pageU(COUPON.sx1) - ease.inOut(smoothstep(RIP + 0.02, RIP + 0.28, t)) * (COUPON.sx1 - COUPON.sx0);
  let z = 0;
  for (let i = 0; i < PANELS; i += 1) {
    const theta = panelTheta(i, t);
    const start = { u, z: theta < 0 ? z - PANEL_W * Math.sin(theta) : z };
    out.push({ i, u: start.u, z: start.z, theta, width: PANEL_W * Math.cos(theta) });
    u += PANEL_W * Math.cos(theta);
    z = Math.max(0, start.z + PANEL_W * Math.sin(theta));
  }
  return out;
}

// The coupon's tear: the perforation opens a third at each tug, the strip
// peels up a little each time and settles back, then rips free.
const tear = (t) => BEAT.tugs.reduce((k, at) => k + smoothstep(at - 0.04, at + 0.04, t) / BEAT.tugs.length, 0);
const coupon = {
  lift: (t) => {
    const peel = BEAT.tugs.slice(0, 2).reduce((k, at) => k + Math.sin(clamp((t - at + 0.04) / 0.2) * Math.PI) * 0.3, 0);
    return Math.max(peel, ease.out(smoothstep(RIP - 0.02, RIP + 0.22, t)));
  },
  pop: (t) => Math.max(...BEAT.tugs.map((at) => Math.sin(clamp((t - at + 0.03) / 0.12) * Math.PI))),
};
// The first two tugs drag the whole price list a few units toward him; the
// paper stays where it was pulled.
const paperDrag = (t) => BEAT.tugs.slice(0, 2).reduce((d, at) => d + smoothstep(at - 0.02, at + 0.06, t) * 5 - Math.sin(clamp((t - at - 0.06) / 0.14) * Math.PI) * 1.2, 0);

const B = BEAT;
const T = B.tugs;
const tl = {
  // Root position: the left edge of the mug rim, the fold-out beside the
  // coupon, and home again. He leaves to the right, clear of the mug.
  x: track([[0, RIM.x], [B.takeoff, RIM.x], [B.takeoff + 0.18, 230, "out"], [B.land, 180, "inOut"], [RIP, 180], [RIP + 0.14, 200, "out"], [B.home, 200], [B.perch, RIM.x, "sine"], [STORY, RIM.x]]),
  y: track([[0, RIM.y], [B.takeoff, RIM.y], [B.land, -128, "in"], [B.home, -128], [B.perch, RIM.y, "sine"], [STORY, RIM.y]]),
  lift: track([[0, 0], [B.slap, 0], [B.slap + 0.12, 26, "out"], [B.slap + 0.28, 0, "in"], [B.takeoff, 0], [B.takeoff + 0.2, 70, "out"], [B.land, 0, "in"],
    [RIP, 0], [RIP + 0.07, 12, "out"], [RIP + 0.16, 0, "in"], [B.home, 0], [B.home + 0.24, 60, "out"], [B.perch, 0, "in"]]),
  facing: track([[0, -1], [B.perch + 0.04, -1], [B.perch + 0.05, 1], [B.check - 0.01, 1], [B.check, -1]]),
  crouch: track([[0, 0], [B.slap - 0.02, 0], [B.slap + 0.02, 0.3, "out"], [B.slap + 0.06, -0.15], [B.slap + 0.32, 0.5, "out"], [B.take + 0.2, 0],
    [B.ready, 0], [B.ready + 0.12, 0.8, "out"], [B.takeoff - 0.02, 0.8], [B.takeoff + 0.04, -0.2, "out"], [B.takeoff + 0.18, 0],
    [B.land, 0], [B.land + 0.05, 0.6, "out"], [B.grip, 0.72], [T[0] - 0.06, 0.78], [T[0] + 0.04, 0.48, "snap"], [T[0] + 0.16, 0.72], [T[1] - 0.04, 0.78], [T[1] + 0.04, 0.5, "snap"], [T[1] + 0.16, 0.72], [RIP - 0.04, 0.78], [RIP + 0.02, 0.76], [RIP + 0.08, 0.45, "snap"], [RIP + 0.2, 0.65, "out"], [RIP + 0.42, 0.15],
    [B.home - 0.08, 0.15], [B.home - 0.02, 0.6, "out"], [B.home + 0.05, -0.1], [B.home + 0.18, 0], [B.perch, 0], [B.perch + 0.05, 0.6, "out"], [B.perch + 0.3, 0.1], [STORY, 0.1]]),
  squash: track([[0, 0], [B.slap, 0], [B.slap + 0.04, -0.7, "out"], [B.slap + 0.28, 0], [B.slap + 0.32, 0.6, "out"], [B.take + 0.1, 0],
    [B.takeoff, 0], [B.takeoff + 0.05, -0.6, "out"], [B.takeoff + 0.2, 0], [B.land, 0], [B.land + 0.04, 0.85, "out"], [B.land + 0.22, 0],
    [RIP + 0.16, 0], [RIP + 0.2, 0.7, "out"], [RIP + 0.34, 0], [B.perch, 0], [B.perch + 0.04, 0.8, "out"], [B.perch + 0.2, 0]]),
  bodyPitch: track([[0, 0.3], [0.5, 0.3], [0.6, 0.1], [B.glance, 0.1], [B.glance + 0.3, 0.3], [B.slap - 0.05, 0.3], [B.slap + 0.04, -0.15, "snap"], [B.take, 0], [B.take + 0.08, 0.3], [B.look, 0.25],
    [B.ready, 0.1], [B.ready + 0.1, 0.32], [B.takeoff, -0.15], [B.land, 0.05], [B.land + 0.08, 0.5], [B.grip - 0.08, 0.94],
    // The tugs come from the legs: the bow holds while the crouch drives.
    [T[0] - 0.06, 0.96], [T[0] + 0.04, 0.9, "snap"], [T[0] + 0.18, 0.95], [T[1] - 0.04, 0.96], [T[1] + 0.04, 0.92, "snap"], [T[1] + 0.16, 0.95], [RIP - 0.04, 0.96], [RIP + 0.02, 0.92], [RIP + 0.1, -0.4, "snap"], [RIP + 0.34, 0.1],
    [B.home, 0.1], [B.perch, 0], [B.release - 0.12, 0.45], [B.release + 0.1, 0.45], [B.check - 0.1, 0.1], [B.proud, -0.06], [STORY, 0.04]]),
  // headAngle + tips the bill down; headTurn 1 looks back over the shoulder,
  // into the mug while he tidies.
  headAngle: track([[0, 0.7], [0.2, 0.9], [0.3, 0.7], [0.45, 0.95], [0.55, 0.7], [B.glance - 0.02, 0.5], [B.glance + 0.04, 0.3, "snap"], [B.glance + 0.26, 0.3], [B.glance + 0.3, 0.75, "snap"],
    [1.3, 0.95], [1.42, 0.7], [1.58, 0.95], [1.7, 0.72], [B.slap, 0.72], [B.slap + 0.05, -0.3, "snap"], [B.take, -0.2], [B.take + 0.05, 0.85, "snap"], [B.look, 0.85], [B.look + 0.06, -0.06, "snap"],
    [B.ready, -0.06], [B.ready + 0.06, 0.6, "snap"], [B.takeoff, 0.4], [B.land, 0.6], [B.grip, 0.7], [RIP + 0.06, -0.1, "snap"], [RIP + 0.3, 0.2], [B.home, 0.2], [B.perch, 0.2],
    [B.release - 0.12, 1.2], [B.release + 0.12, 1.2], [B.release + 0.22, 0.4], [B.check, 0.4], [B.check + 0.06, 0.7, "snap"], [B.proud, 0.2], [STORY, 0.3]]),
  headTurn: track([[0, 1], [B.glance - 0.02, 1], [B.glance + 0.04, 0, "snap"], [B.glance + 0.26, 0], [B.glance + 0.32, 1, "snap"], [B.slap, 1], [B.slap + 0.05, 0, "snap"]]),
  beakOpen: track([[0, 0], [0.18, 0.35], [0.26, 0], [0.42, 0.35], [0.5, 0], [1.28, 0], [1.34, 0.35], [1.42, 0], [1.56, 0.35], [1.64, 0], [B.grip - 0.1, 0], [B.grip - 0.04, 0.5, "out"], [B.grip, 0.15, "snap"],
    [B.release - 0.04, 0.15], [B.release + 0.04, 0.55, "out"], [B.release + 0.16, 0], [B.proud + 0.2, 0], [B.proud + 0.26, 0.6, "out"], [B.proud + 0.36, 0], [B.proud + 0.48, 0.55, "out"], [B.proud + 0.58, 0]]),
  neck: track([[0, 1], [B.slap, 1], [B.take, 1.02], [B.take + 0.08, 1.3, "out"], [B.look, 1.26], [B.look + 0.06, 1], [B.ready + 0.08, 0.86, "out"], [B.takeoff, 0.92], [B.land, 1], [RIP, 1], [RIP + 0.1, 0.88, "out"], [RIP + 0.4, 1], [B.check, 1.05], [B.proud, 1.08], [STORY, 1.04]]),
  headY: track([[0, 0], [B.release - 0.12, 0], [B.release - 0.04, 12], [B.release + 0.12, 12], [B.release + 0.22, 0]]),
  brow: track([[0, 0], [B.glance, 0], [B.glance + 0.05, 0.8, "snap"], [B.glance + 0.3, 0], [B.ready, 0], [B.ready + 0.06, -1, "snap"], [RIP, -1], [RIP + 0.1, 0.8, "snap"], [RIP + 0.4, 0], [B.check, 0], [B.check + 0.06, 0.6], [B.proud + 0.3, 0]]),
  lid: track([[0, 0], [B.look + 0.06, 0], [B.look + 0.12, 0.5, "out"], [B.look + 0.16, 0.5], [B.look + 0.2, 1, "inOut"], [B.look + 0.32, 0.5, "inOut"], [B.ready - 0.06, 0.5], [B.ready, 0, "snap"]]),
  eyeWide: track([[0, 0], [B.slap, 0], [B.slap + 0.05, 1, "snap"], [B.take - 0.02, 0.45], [B.take + 0.05, 1, "snap"], [B.look, 1], [B.look + 0.06, 0, "snap"], [T[0], 0], [T[0] + 0.04, 0.5, "snap"], [T[0] + 0.2, 0.1],
    [T[1] + 0.04, 0.6, "snap"], [T[1] + 0.2, 0.1], [RIP + 0.04, 1, "snap"], [RIP + 0.4, 0.2], [B.check, 0.2], [B.check + 0.06, 0.7, "snap"], [B.proud, 0.1]]),
  pupil: track([[0, 0], [B.glance, 0], [B.glance + 0.06, -0.55, "out"], [B.glance + 0.3, 0], [B.slap, 0], [B.slap + 0.05, -1, "snap"], [B.look, -1], [B.look + 0.06, -0.45, "snap"], [B.ready, -0.45], [B.ready + 0.08, -0.85, "out"],
    [RIP, -0.85], [RIP + 0.06, 0.4, "snap"], [B.home, -0.2], [B.release, -0.6], [B.check + 0.06, -0.8, "snap"], [B.proud, 0.35], [STORY, 0.2]]),
  crestRaise: track([[0, 0], [B.slap, 0], [B.slap + 0.05, 1.1, "snap"], [B.take - 0.02, 0.5], [B.take + 0.05, 1.1, "snap"], [B.look, 0.9], [B.look + 0.06, -0.5, "snap"], [B.ready - 0.04, -0.5],
    [B.ready + 0.04, 0.2], [T[0] + 0.04, 0.6, "snap"], [T[0] + 0.2, 0.2], [T[1] + 0.04, 0.7, "snap"], [T[1] + 0.2, 0.2], [RIP + 0.04, 1.2, "snap"], [RIP + 0.5, 0.2], [B.proud, 0.6], [STORY, 0.2]]),
  lookX: track([[0, 0], [B.glance, 0], [B.glance + 0.05, 0.4, "snap"], [B.glance + 0.29, 0, "snap"], [B.look, 0.3], [B.look + 0.06, -0.35, "snap"], [B.ready, -0.35], [B.ready + 0.06, 0.3, "snap"],
    [RIP, 0.3], [RIP + 0.06, -0.2, "snap"], [B.check, 0], [B.check + 0.06, 0.5, "snap"], [B.proud, 0.1]]),
  lookY: track([[0, 0.6], [B.glance, 0.6], [B.glance + 0.05, 0.8, "snap"], [B.glance + 0.29, 0.6, "snap"], [B.slap + 0.05, -0.3, "snap"], [B.take, -0.2], [B.take + 0.05, 0.85, "snap"],
    [B.look, 0.85], [B.look + 0.06, 0, "snap"], [B.ready + 0.06, 0.7, "snap"], [B.grip, 0.9], [RIP, 0.9], [RIP + 0.06, 0, "snap"], [B.release - 0.1, 0.9], [B.check, 0.6], [B.check + 0.06, 0.9, "snap"], [B.proud, 0.2]]),
  tailAngle: track([[0, 0], [B.slap, 0], [B.slap + 0.05, -0.3, "out"], [B.slap + 0.35, 0.2, "out"], [B.take + 0.2, 0], [B.ready + 0.1, -0.2], [B.takeoff + 0.1, 0.25],
    [B.land + 0.04, 0.3, "out"], [B.land + 0.25, 0], [T[0] + 0.04, -0.25, "out"], [T[0] + 0.2, 0], [T[1] + 0.04, -0.3, "out"], [T[1] + 0.2, 0], [RIP + 0.06, -0.4, "out"], [RIP + 0.3, 0.1],
    [B.perch + 0.04, 0.3, "out"], [B.perch + 0.25, 0], [B.proud, -0.12, "out"], [B.proud + 0.25, 0.05], [STORY, 0]]),
  tailSpread: track([[0, 0], [B.slap, 0], [B.slap + 0.06, 0.8], [B.take, 0], [B.takeoff, 0.2], [B.takeoff + 0.1, 0.9], [B.land + 0.3, 0.2], [T[0], 0.4], [RIP + 0.06, 1], [RIP + 0.4, 0.2],
    [B.home, 0.6], [B.perch, 0.9], [B.perch + 0.35, 0]]),
  puff: track([[0, 0], [B.proud, 0], [B.proud + 0.14, 0.8, "out"], [B.proud + 0.5, 0.1], [STORY, 0]]),
};

// Wings in mesh terms (wingLift + raises them).
function wingsAt(t) {
  const flutter = (rate, amount) => Math.sin(t * TAU * rate) * amount;
  const air = (a, b) => smoothstep(a - 0.04, a + 0.04, t) * (1 - smoothstep(b - 0.04, b + 0.12, t));
  const startle = Math.sin(clamp((t - B.slap - 0.02) / 0.3) * Math.PI);
  const flight = air(B.takeoff + 0.02, B.land + 0.06);
  const home = air(B.home + 0.02, B.perch + 0.06);
  // Each tug throws the wings out for balance as he leans back.
  const tugs = T.reduce((k, at, i) => Math.max(k, Math.sin(clamp((t - at + 0.02) / (i === 2 ? 0.42 : 0.24)) * Math.PI) * (i === 2 ? 1 : 0.6)), 0);
  const open = Math.max(startle * 0.6, flight * 0.95, home * 0.95, tugs);
  const lift = startle * 1.0 + flight * (0.8 + flutter(6.5, 0.42)) + home * (0.85 + flutter(6.5, 0.4)) + tugs * 0.85;
  const weight = Math.max(startle, flight, home, tugs, 1e-3);
  return { open, lift: lift / Math.max(1, weight), tuck: -0.9 * Math.sin(clamp((t - B.proud - 0.55) / 0.22) * Math.PI) };
}

function feetFor(t) {
  const airborne = (t > B.takeoff + 0.04 && t < B.land) || (t > B.home + 0.04 && t < B.perch) || (t > B.slap + 0.04 && t < B.slap + 0.28) || (t > RIP + 0.04 && t < RIP + 0.16);
  if (airborne) {
    const k = t > B.takeoff && t < B.land ? smoothstep(B.land - 0.2, B.land - 0.02, t) : t > B.home ? smoothstep(B.perch - 0.2, B.perch - 0.02, t) : 0.5;
    return { near: { x: lerp(-14, 12, k), y: lerp(-26, 2, k) }, far: { x: lerp(-22, -4, k), y: lerp(-28, 3, k) }, grip: 0.6 };
  }
  // Bracing on the fold-out while he pulls: feet wide.
  if (t > B.land && t < B.home) return { near: { x: 22, y: 0 }, far: { x: -10, y: 0 }, grip: 0.3 };
  // On the mug rim his toes curl over the edge.
  return { near: { x: 10, y: 0 }, far: { x: -6, y: -2 }, grip: 0.85 };
}

// ---------------------------------------------------------------- scene

export function createPlatefoldScene() {
  const rivet = new Rivet({ seed: 29 });
  const blink = blinkSchedule(53);
  const idle = idleProgramme(11);
  const motes = Array.from({ length: 26 }, (_, i) => ({ nx: noise1(200 + i), ny: noise1(300 + i), s: 0.8 + (i % 4) * 0.4, home: { x: -260 + (i * 37) % 520, y: -380 + ((i * 53) % 300) } }));
  const r = rng(3);
  const grainSpots = Array.from({ length: 160 }, () => ({ u: r.range(-900, 1300), v: r.range(-200, 700), len: r.range(80, 420), w: r.range(0.6, 2.2), dark: r.next() < 0.6 }));
  const crest = crestFollower((t) => tl.headAngle(Math.min(t, STORY)) * 1.2 + tl.squash(Math.min(t, STORY)) * 0.4);
  const CLOSE = { x: RIM.x - 10, y: RIM.y - 95 };
  const B = BEAT;
  const stripSway = springResponse((t) => (tl.x(t + 0.03) - tl.x(t)) * -0.9 + (tl.lift(t + 0.03) - tl.lift(t)) * 0.4, { stiffness: 30, damping: 3 });
  const textures = { paper: null };
  let art = null;
  let camera = { s: 1, w: 1, h: 1, zoom: 1, fx: 0, fy: 0 };
  let layoutMode = "wide";
  // Wide frames open on the page and the mug, pull back as the fold-out runs
  // off the page, push in close on Rivet for his look at the viewer, follow
  // him to the coupon and settle on the page and the nest together.
  const shots = {
    wide: {
      zoom: track([[0, 1.0], [0.55, 1.0], [1.9, 0.8, "inOut"], [B.take + 0.12, 0.8], [B.look - 0.02, 2.0, "inOut"], [B.ready, 2.0], [B.takeoff + 0.2, 1.1, "inOut"], [B.grip, 1.22, "inOut"],
        [RIP + 0.3, 1.12], [B.perch, 1.0, "inOut"], [STORY, 1.0]]),
      fx: track([[0, 60], [0.55, 60], [1.9, 220, "inOut"], [B.take + 0.12, 220], [B.look - 0.02, CLOSE.x, "inOut"], [B.ready, CLOSE.x], [B.grip, 120, "inOut"], [RIP + 0.3, 140], [B.perch, 60, "inOut"], [STORY, 60]]),
      fy: track([[0, -280], [0.55, -280], [1.9, -250, "inOut"], [B.take + 0.12, -250], [B.look - 0.02, CLOSE.y, "inOut"], [B.ready, CLOSE.y], [B.grip, -170, "inOut"], [RIP + 0.3, -190], [B.perch, -285, "inOut"], [STORY, -285]]),
    },
    tall: {
      zoom: track([[0, 0.95], [0.55, 0.95], [1.9, 0.82, "inOut"], [B.take + 0.12, 0.82], [B.look - 0.02, 1.8, "inOut"], [B.ready, 1.8], [B.takeoff + 0.2, 1.0, "inOut"], [B.grip, 0.95, "inOut"],
        [RIP + 0.3, 0.95], [B.perch, 0.95, "inOut"], [STORY, 0.95]]),
      fx: track([[0, 60], [0.55, 60], [1.5, 230, "inOut"], [1.86, 300, "inOut"], [B.slap + 0.2, 300], [B.take + 0.12, 230, "inOut"], [B.look - 0.02, CLOSE.x + 20, "inOut"], [B.ready, CLOSE.x + 20], [B.grip, 130, "inOut"], [RIP + 0.3, 110], [B.perch, 60, "inOut"], [STORY, 60]]),
      fy: track([[0, -280], [0.55, -280], [1.9, -250, "inOut"], [B.take + 0.12, -250], [B.look - 0.02, CLOSE.y, "inOut"], [B.ready, CLOSE.y], [B.grip, -180, "inOut"], [RIP + 0.3, -200], [B.perch, -285, "inOut"], [STORY, -285]]),
    },
  };

  const scene = {
    storyDuration: STORY,
    freeAt: FREE_AT,
    idleEndsAt: idle.asleepAt,
    artIds: () => (art ? art.ids() : []),
    onUnavailable: handler => { rivet.onUnavailable = handler; },

    async prepare() {
      await rivet.ready;
      const [loaded] = await Promise.all([
        loadArt((a) => a.id.startsWith("platefold-") || (a.piece || "").startsWith("rivet-")),
        document.fonts.load("700 30px Sora"),
        document.fonts.load("400 14px Sora"),
        document.fonts.load("400 28px 'JetBrains Mono'"),
      ]);
      art = loaded;
      if (art?.size) rivet.usePaint(rivetKit(art));
      textures.paper = paperTexture(256, 8, { tone: 128, spread: 22, fibers: 500 });
    },

    layout(w, h) {
      layoutMode = h > w * 1.05 ? "tall" : "wide";
      const view = layoutMode === "tall" ? { w: 400, h: 590 } : { w: 1020, h: 600 };
      camera = { ...camera, s: Math.min(w / view.w, h / view.h), w, h };
    },

    render(ctx, frame) {
      const { t, dpr, w, h } = frame;
      const idleT = frame.idle;
      const now = t + idleT;
      const sleepK = smoothstep(idle.awake, idle.asleepAt, idleT);
      const shot = shots[layoutMode];
      camera.zoom = shot.zoom(t);
      camera.fx = shot.fx(t);
      camera.fy = shot.fy(t);
      // A jolt when the last panel slaps down and when the coupon rips.
      const jolt = [BEAT.slap, RIP].reduce((sum, at) => sum + (t > at ? Math.exp(-(t - at) * 12) : 0), 0) * 3;
      const base = new DOMMatrix()
        .scale(dpr, dpr)
        .translate(w / 2 + Math.sin(t * 61) * jolt, h / 2 + Math.cos(t * 47) * jolt * 0.6)
        .scale(camera.s * camera.zoom)
        .translate(-camera.fx, -camera.fy);

      ctx.setTransform(base);
      const desk = art?.get("platefold-far");
      const mug = art?.get("platefold-mug");
      if (desk) {
        // Follow the page plane, extending coverage when a wide frame would
        // expose the painting's edge. The action keeps its exact coordinates.
        drawPlate(ctx, desk, coverMatrix(desk, base, w * dpr, h * dpr));
        ctx.setTransform(base);
        paintLampFalloff(ctx, now, sleepK);
      } else {
        paintDesk(ctx, grainSpots);
        paintLampPool(ctx, now, sleepK);
      }
      if (mug) {
        paintPaintedMug(ctx, mug);
      } else {
        paintMugBack(ctx);
        paintMugFront(ctx);
      }
      const panels = foldOut(t);
      const paper = base.translate(paperDrag(t), 0);
      ctx.setTransform(paper);
      paintPageShadow(ctx, panels);
      paintPage(ctx, paper, t, textures.paper);
      paintFoldOut(ctx, paper, panels, t);
      paintReflow(ctx, paper, panels, t);

      const pose = poseAt(t, frame, idleT, sleepK);
      const light = { dir: { x: -0.55, y: -0.84 }, rim: "#ffd9a0", rimStrength: 0.6, rimWidth: 1.1, ambient: "#6b4520", ambientStrength: 0.1 };
      rivet.drawShadow(ctx, pose, base, { strength: 0.32, color: "#24140a", width: 0.8 });
      paintCoupon(ctx, base, t, rivet.holdMatrix(pose, base), stripSway, "before");
      const hold = t < RELEASE_AT ? rivet.holdMatrix(pose, base) : rivet.holdMatrix(poseAt(RELEASE_AT, frame, 0, 0), base);
      if (t > RELEASE_AT - 0.08) {
        // Into the nest: the mug's near wall covers the strip as it sinks.
        paintCoupon(ctx, base, t, hold, stripSway, "after", mug);
        if (!mug) {
          ctx.setTransform(base);
          paintMugFront(ctx);
        }
        rivet.draw(ctx, pose, base, light);
      } else {
        rivet.draw(ctx, pose, base, light);
        paintCoupon(ctx, base, t, hold, stripSway, "after");
      }
      if (!desk) paintLamp(ctx);
      paintMotes(ctx, motes, now, sleepK);

    },
  };

  function poseAt(t, frame, idleT, sleepK) {
    const feet = feetFor(t);
    const sample = Object.fromEntries(Object.entries(tl).map(([k, f]) => [k, f(t)]));
    const wings = wingsAt(t);
    let pose = {
      ...DEFAULT_POSE,
      ...sample,
      x: sample.x,
      y: sample.y - sample.lift,
      facing: sample.facing < 0 ? -1 : 1,
      scale: RIVET_SCALE,
      footNear: feet.near,
      footFar: feet.far,
      gripNear: feet.grip,
      gripFar: feet.grip,
      wingOpen: wings.open,
      farWingOpen: wings.open * 0.92,
      wingLift: wings.lift,
      farWingLift: wings.lift + 0.08,
      wingTuck: wings.tuck,
      wingFan: wings.open * 0.6,
      breath: breathAt(t + idleT),
      blink: blink(t + idleT, [[BEAT.slap - 0.1, BEAT.release + 0.2]]),
      crest: crest(t) * 0.9,
    };
    // The beak holds the TEAR tab from the grip until the rip: the body does
    // the bending and the tugs, the neck takes up the residual.
    const hold = smoothstep(BEAT.grip - 0.12, BEAT.grip, t) * (1 - smoothstep(RIP + 0.02, RIP + 0.06, t));
    if (hold > 0) {
      const peel = coupon.lift(t);
      // The bill lifts the TEAR tab a little off the page as it takes it.
      pose = rivet.reachBeak(pose, { x: COUPON_GRIP.x + paperDrag(t) + peel * 14, y: COUPON_GRIP.y - 6 - peel * 40 }, hold);
    }
    if (t >= STORY) {
      const d = idle.at(idleT);
      pose = applyDeltas(pose, d);
      if (d.crestKick) pose.crest += Math.sin(idleT * 30) * 0.25 * d.crestKick;
      pose = gazeAtPointer(pose, frame);
      // Waking eases out of the doze; the programme's own doze still ends
      // the idle that follows, so a woken Rivet dozes off again.
      const wakeFade = frame.woke ? 1 - smoothstep(0, 1.3, idleT) : 0;
      pose = applyDoze(pose, Math.max(wakeFade, d.doze || 0, sleepK));
      pose.breath = breathAt(t + idleT, lerp(1.7, 0.9, pose.sleep));
    }
    pose._time = t + idleT;
    return pose;
  }

  function gazeAtPointer(pose, frame) {
    if (!frame.pointer) return pose;
    const k = camera.s * camera.zoom;
    const wx = (frame.pointer.x - camera.w / 2) / k + camera.fx;
    const wy = (frame.pointer.y - camera.h / 2) / k + camera.fy;
    const dx = (wx - pose.x) * pose.facing;
    const dy = wy - (pose.y - 120);
    const a = clamp(Math.atan2(dy, Math.max(20, dx)), -0.5, 0.45);
    return { ...pose, headAngle: lerp(pose.headAngle, a * 0.7, 0.75), lookX: clamp(dx / 300, -1, 1), lookY: clamp(dy / 300, -1, 1) };
  }

  return scene;
}

// ---------------------------------------------------------------- painting

// Affine map from a panel's own (d, sy) coordinates to the screen. A pleat
// either rises from its left crease or falls toward its right one.
function panelMatrix(base, p) {
  // A panel from its crease (u, z) at elevation theta: d runs along the
  // panel, sy across the page's depth.
  return base.multiply(new DOMMatrix([Math.cos(p.theta), -Math.sin(p.theta) * KZ, 0, K, p.u, -PAGE.h * K - p.z * KZ]));
}

function pageMatrix(base) {
  return base.multiply(new DOMMatrix([1, 0, 0, K, PAGE.u0, -PAGE.h * K]));
}

function paintDesk(ctx, grainSpots) {
  ctx.fillStyle = "#3a2415";
  ctx.fillRect(-2000, -1500, 4400, 3200);
  // Walnut grain, foreshortened like everything else on the desk.
  for (const g of grainSpots) {
    ctx.strokeStyle = g.dark ? "rgba(25,12,5,0.35)" : "rgba(120,78,44,0.18)";
    ctx.lineWidth = g.w;
    ctx.beginPath();
    const y = -g.v * K;
    ctx.moveTo(g.u, y);
    ctx.bezierCurveTo(g.u + g.len * 0.3, y - 3, g.u + g.len * 0.7, y + 3, g.u + g.len, y);
    ctx.stroke();
  }
}

function paintLampPool(ctx, now, sleepK) {
  const flicker = 1 + Math.sin(now * 2.1) * 0.01 * (1 - sleepK);
  const g = ctx.createRadialGradient(40, -170, 30, 40, -150, 720 * flicker);
  g.addColorStop(0, "rgba(255,214,150,0.42)");
  g.addColorStop(0.45, "rgba(255,190,120,0.16)");
  g.addColorStop(1, "rgba(0,0,0,0.35)");
  ctx.fillStyle = g;
  ctx.fillRect(-1600, -1200, 3600, 2600);
}

function paintPageShadow(ctx, panels) {
  ctx.fillStyle = "rgba(20,10,4,0.35)";
  const right = Math.max(...panels.map((p) => p.u + Math.max(0, p.width)));
  ctx.beginPath();
  ctx.moveTo(PAGE.u0 + 10, 8);
  ctx.lineTo(right + 8, 8);
  ctx.lineTo(right + 4, -PAGE.h * K + 6);
  ctx.lineTo(PAGE.u0 + 12, -PAGE.h * K + 6);
  ctx.closePath();
  ctx.fill();
}

function paperFill(ctx, paper, tone = "#fffbf3") {
  ctx.fillStyle = tone;
  ctx.fill();
  if (!paper) return;
  ctx.save();
  ctx.clip();
  ctx.globalCompositeOperation = "multiply";
  ctx.globalAlpha = 0.18;
  ctx.fillStyle = ctx.createPattern(paper, "repeat");
  ctx.fillRect(-50, -50, 600, 600);
  ctx.restore();
}

function paintPage(ctx, base, t, paper) {
  ctx.save();
  ctx.setTransform(pageMatrix(base));
  // The page without the coupon; the coupon is drawn on its own.
  ctx.beginPath();
  ctx.rect(0, 0, COUPON.sx0, PAGE.h);
  ctx.rect(COUPON.sx0, 0, PAGE.w - COUPON.sx0, COUPON.sy0);
  ctx.rect(COUPON.sx0, COUPON.sy1, PAGE.w - COUPON.sx0, PAGE.h - COUPON.sy1);
  paperFill(ctx, paper);
  // Title block in Bristol: brown ink, amber rule.
  ctx.fillStyle = "#34220f";
  ctx.font = "700 30px Sora, sans-serif";
  ctx.textBaseline = "alphabetic";
  ctx.fillText("Plate|Fold", 24, 56);
  ctx.fillStyle = "#74614a";
  ctx.font = "400 14px Sora, sans-serif";
  ctx.fillText("Price list · per PDF", 24, 82);
  ctx.fillStyle = "#d8b276";
  ctx.fillRect(24, 100, 232, 2.5);
  ctx.fillStyle = "#99500f";
  ctx.font = "600 11px Sora, sans-serif";
  ctx.fillText("PLAN", 24, 140);
  ctx.textAlign = "right";
  ctx.fillText("PRICE", 190, 140);
  ctx.textAlign = "left";
  const shift = smoothstep(RIP + 0.2, RIP + 0.55, t) * CELL;
  for (const row of ROWS) {
    ctx.fillStyle = "#322b23";
    ctx.font = "600 21px Sora, sans-serif";
    ctx.fillText(row.name, 24, row.sy);
    ctx.fillStyle = "#74614a";
    ctx.font = "400 11px Sora, sans-serif";
    ctx.fillText(row.note, 24, row.sy + 17);
    ctx.fillStyle = "#99500f";
    ctx.font = "400 28px 'JetBrains Mono', monospace";
    ctx.fillText("$", DOLLAR_SX - shift, row.sy + 4);
    ctx.fillStyle = "#ded0b6";
    ctx.fillRect(24, row.sy + 26, 210, 1.2);
  }
  ctx.fillStyle = "#74614a";
  ctx.font = "400 9.5px Sora, sans-serif";
  ctx.fillText("Prices shown are a joke. Plate|Fold is MIT-licensed.", 24, 346);
  // Perforation along the coupon's left edge: torn from the top down, a
  // third at each tug, dashed below the tear.
  const torn = COUPON.sy0 + tear(t) * (COUPON.sy1 - COUPON.sy0);
  ctx.lineWidth = 1.2;
  ctx.strokeStyle = "#c9b48f";
  ctx.beginPath();
  ctx.moveTo(COUPON.sx0, COUPON.sy0);
  for (let y = COUPON.sy0; y <= torn; y += 6) ctx.lineTo(COUPON.sx0 + ((y / 6) % 2 ? 2.4 : -0.8), y);
  ctx.stroke();
  if (torn < COUPON.sy1) {
    ctx.strokeStyle = "#b7a37f";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(COUPON.sx0, torn);
    ctx.lineTo(COUPON.sx0, COUPON.sy1);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();
}

function paintFoldOut(ctx, base, panels, t) {
  const fade = 1 - smoothstep(RIP + 0.04, RIP + 0.1, t); // glyphs hand over to the reflow
  for (const p of panels) {
    const m = panelMatrix(base, p);
    ctx.save();
    ctx.setTransform(m);
    ctx.beginPath();
    ctx.rect(0, 0, PANEL_W, PAGE.h);
    // Pleats facing the lamp are brighter than those facing away, and a
    // panel flipped face down shows its plainer back.
    const facing = Math.cos(p.theta) < 0 ? 0.86 : p.theta >= 0 ? 1 - Math.sin(p.theta) * 0.12 : 1 + Math.sin(p.theta) * 0.32;
    const tone = Math.round(255 * facing);
    paperFill(ctx, null, `rgb(${tone},${Math.round(251 * facing)},${Math.round(243 * facing)})`);
    ctx.strokeStyle = "rgba(150,120,80,0.45)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(0, PAGE.h);
    ctx.stroke();
    if (fade > 0) {
      ctx.globalAlpha = fade;
      ctx.font = "400 28px 'JetBrains Mono', monospace";
      for (const row of ROWS) {
        ctx.fillStyle = "#a3311f";
        ctx.fillText(",000", 0, row.sy + 4);
      }
      ctx.fillStyle = "#99500f";
      ctx.font = "600 10px Sora, sans-serif";
      ctx.fillText(LADDER[p.i].toUpperCase(), 4, 346);
      ctx.globalAlpha = 1;
    }
    ctx.restore();
  }
}

// After the coupon leaves, the leftover zeros fly back and merge into one.
function paintReflow(ctx, base, panels, t) {
  const k = clamp((t - RIP - 0.06) / 0.5);
  if (k <= 0) return;
  ctx.save();
  ctx.setTransform(pageMatrix(base));
  ctx.font = "400 28px 'JetBrains Mono', monospace";
  const target = DOLLAR_SX;
  const fall = clamp(k / 0.35);
  if (fall < 1) {
    ctx.fillStyle = "#a3311f";
    ctx.globalAlpha = 1 - fall;
    for (const row of ROWS) {
      for (let i = 0; i < PANELS; i += 1) {
        ctx.fillText(",", COUPON.sx1 + i * PANEL_W - (COUPON.sx1 - COUPON.sx0) * ease.inOut(smoothstep(RIP + 0.02, RIP + 0.28, t)), row.sy + 4 + fall * fall * 30);
      }
    }
    ctx.globalAlpha = 1;
  }
  for (const row of ROWS) {
    for (let i = 0; i < PANELS; i += 1) {
      for (let j = 1; j < 4; j += 1) {
        const from = COUPON.sx1 + i * PANEL_W + j * CELL;
        const delay = (i * 3 + j) * 0.012;
        const kk = ease.inOut(clamp((k - delay) / (1 - delay * 1.3)));
        if (kk >= 1 && !(i === 0 && j === 1)) continue;
        const x = lerp(from, target, kk);
        const arc = Math.sin(kk * Math.PI) * (10 + i * 4);
        const survivor = i === 0 && j === 1;
        const size = survivor ? 1 + Math.sin(kk * Math.PI) * 0.25 : 1 - kk * 0.7;
        // The others fold into the survivor and are gone before it lands.
        ctx.globalAlpha = survivor ? 1 : (1 - kk) ** 2;
        ctx.fillStyle = survivor && kk > 0.9 ? "#1f5a3b" : "#a3311f";
        ctx.save();
        ctx.translate(x + CELL / 2, row.sy - 6 - arc);
        ctx.scale(size, size);
        ctx.textAlign = "center";
        ctx.fillText("0", 0, 10);
        ctx.restore();
      }
    }
  }
  ctx.globalAlpha = 1;
  ctx.restore();
}

function couponMatrix(base, t, hold, sway) {
  // Both drawing passes share the page's translation while the coupon peels.
  const flat = pageMatrix(base.translate(paperDrag(t), 0)).translate(COUPON.sx0, COUPON.sy0);
  const lift = coupon.lift(t);
  if (lift <= 0) return flat;
  // Hanging from the beak: unforeshortened, swinging a little.
  const scale = Math.hypot(base.a, base.b);
  const hb = hold.transformPoint(new DOMPoint(0, 0));
  const hanging = new DOMMatrix()
    .translate(hb.x, hb.y)
    .rotate((clamp(sway, -0.6, 0.6) * 180) / Math.PI)
    .scale(scale)
    .translate(-(COUPON.sx1 - COUPON.sx0) / 2, -4);
  const k = ease.inOut(lift);
  return new DOMMatrix([
    lerp(flat.a, hanging.a, k), lerp(flat.b, hanging.b, k), lerp(flat.c, hanging.c, k),
    lerp(flat.d, hanging.d, k), lerp(flat.e, hanging.e, k), lerp(flat.f, hanging.f, k),
  ]);
}

// The coupon is drawn beneath Rivet while it lies on the page and over his
// body once it hangs from his beak; it disappears into the nest.
function paintCoupon(ctx, base, t, hold, stripSway, pass, mug = null) {
  const lifted = coupon.lift(t) > 0.3;
  if ((pass === "before") === lifted) return;
  const drop = smoothstep(RELEASE_AT, RELEASE_AT + 0.27, t);
  if (drop >= 1) return;
  // The strip stops swinging as he lines it up over the opening, then falls
  // straight down from where he let go, whatever his head does next.
  const sway = stripSway(t) * (1 - smoothstep(RELEASE_AT - 0.23, RELEASE_AT, t));
  const m = couponMatrix(base, t, hold, sway);
  m.f += drop * DROP_DEPTH * Math.hypot(base.a, base.b);
  ctx.save();
  if (mug && t > RELEASE_AT - 0.08) {
    // Subtract the near wall from this paper layer. Painting the mug again
    // would double its translucent edge and inherit the page's moving frame.
    ctx.resetTransform();
    ctx.beginPath();
    ctx.rect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.setTransform(base);
    traceMugNearWall(ctx, mug);
    ctx.clip("evenodd");
  } else if (drop > 0) {
    // Going into the mug: hide whatever has passed below the rim.
    const rim = toScreen(MUG.u, MUG.v, MUG.h / KZ);
    ctx.setTransform(base);
    ctx.beginPath();
    ctx.rect(rim.x - 200, rim.y - 600, 400, 600 + 40 * K);
    ctx.clip();
  }
  ctx.setTransform(m);
  const w = COUPON.sx1 - COUPON.sx0;
  const hgt = COUPON.sy1 - COUPON.sy0;
  const pop = coupon.pop(t);
  ctx.beginPath();
  ctx.rect(0, 0, w, hgt);
  ctx.fillStyle = pop > 0.4 && coupon.lift(t) < 0.05 ? "#fff5e2" : "#fffbf3";
  ctx.fill();
  ctx.strokeStyle = "rgba(150,120,80,0.6)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#99500f";
  ctx.font = "600 7px Sora, sans-serif";
  ctx.fillText("TEAR", 4, 12);
  ctx.font = "400 28px 'JetBrains Mono', monospace";
  ctx.fillStyle = "#322b23";
  for (const row of ROWS) ctx.fillText(row.digits, 0, row.sy - COUPON.sy0 + 4);
  ctx.restore();
}

function cylinder(ctx, u, v, radius, height, top, side) {
  const p = toScreen(u, v);
  const ry = radius * K;
  ctx.fillStyle = side;
  ctx.beginPath();
  ctx.ellipse(p.x, p.y, radius, ry, 0, 0, Math.PI);
  ctx.lineTo(p.x - radius, p.y - height);
  ctx.ellipse(p.x, p.y - height, radius, ry, 0, Math.PI, 0, true);
  ctx.closePath();
  ctx.fill();
  return { x: p.x, y: p.y - height, ry, top };
}

// The painted desk carries its own lamp light; this only adds the lamp's slow
// breathing and darkens the edges toward the room.
function paintLampFalloff(ctx, now, sleepK) {
  const flicker = 1 + Math.sin(now * 2.1) * 0.01 * (1 - sleepK);
  const g = ctx.createRadialGradient(40, -170, 60, 40, -150, 760 * flicker);
  g.addColorStop(0, "rgba(255,214,150,0.08)");
  g.addColorStop(0.55, "rgba(255,190,120,0)");
  g.addColorStop(1, "rgba(10,5,2,0.4)");
  ctx.fillStyle = g;
  ctx.fillRect(-1600, -1200, 3600, 2600);
}

function paintedMugRect(mug) {
  const s = PAINTED_MUG_SCALE;
  return { x: MUG_RIM.x - PAINTED_MUG.rimX * s, y: MUG_RIM.y - PAINTED_MUG.rimY * s, w: mug.image.width * s, h: mug.image.height * s };
}

function paintPaintedMug(ctx, mug) {
  const r = paintedMugRect(mug);
  // A soft penumbra anchors the cup without a hard cutout around its base.
  ctx.save();
  ctx.translate(MUG_RIM.x + 8, r.y + r.h - 8);
  ctx.scale(62, 16);
  const shadow = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
  shadow.addColorStop(0, "rgba(20,10,4,0.4)");
  shadow.addColorStop(0.5, "rgba(20,10,4,0.24)");
  shadow.addColorStop(1, "rgba(20,10,4,0)");
  ctx.fillStyle = shadow;
  ctx.beginPath();
  ctx.arc(0, 0, 1, 0, TAU);
  ctx.fill();
  ctx.restore();
  ctx.drawImage(mug.image, r.x, r.y, r.w, r.h);
}

// Append the near lip and the region beneath it to the current path. Once
// the paper enters the mug, keep it hidden below the opening until removed.
function traceMugNearWall(ctx, mug) {
  const s = PAINTED_MUG_SCALE;
  const cx = MUG_RIM.x;
  const cy = MUG_RIM.y + PAINTED_MUG.innerDy * s;
  const rx = PAINTED_MUG.innerRx * s;
  const ry = PAINTED_MUG.innerRy * s;
  const r = paintedMugRect(mug);
  ctx.moveTo(cx - rx, cy);
  ctx.ellipse(cx, cy, rx, ry, 0, Math.PI, 0, true);
  const bottom = r.y + r.h + DROP_DEPTH + COUPON.sy1 - COUPON.sy0;
  ctx.lineTo(r.x + r.w, bottom);
  ctx.lineTo(r.x, bottom);
  ctx.closePath();
}

function paintMugBack(ctx) {
  const p = toScreen(MUG.u, MUG.v);
  const h = MUG.h;
  ctx.fillStyle = "rgba(20,10,4,0.4)";
  ctx.beginPath();
  ctx.ellipse(p.x + 10, p.y + 4, MUG.r + 8, (MUG.r + 8) * K * 0.7, 0, 0, TAU);
  ctx.fill();
  // Inside of the mug and the nest that fills it.
  ctx.fillStyle = "#cdb48a";
  ctx.beginPath();
  ctx.ellipse(p.x, p.y - h, MUG.r, MUG.r * K, 0, 0, TAU);
  ctx.fill();
  ctx.fillStyle = "#5a3c22";
  ctx.beginPath();
  ctx.ellipse(p.x, p.y - h + 3, MUG.r - 6, (MUG.r - 6) * K, 0, 0, TAU);
  ctx.fill();
  const r = rng(9);
  ctx.lineCap = "round";
  for (let i = 0; i < 22; i += 1) {
    const a = r.next() * TAU;
    const rad = r.range(8, MUG.r - 6);
    const x = p.x + Math.cos(a) * rad;
    const y = p.y - h + Math.sin(a) * rad * K;
    ctx.strokeStyle = r.next() < 0.5 ? "#7b5631" : "#3b2615";
    ctx.lineWidth = r.range(1.4, 3);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + r.signed(22), y - r.range(4, 26));
    ctx.stroke();
  }
  // Old price slips: a jackdaw's idea of lining.
  for (const [dx, dy, a, label] of [[-14, -12, -0.4, "$9.99"], [10, -20, 0.3, "$49"], [-2, -30, 0.1, "$199"]]) {
    ctx.save();
    ctx.translate(p.x + dx, p.y - h + dy);
    ctx.rotate(a);
    ctx.fillStyle = "#f6ecd8";
    ctx.fillRect(-12, -7, 24, 13);
    ctx.fillStyle = "#99500f";
    ctx.font = "600 7px 'JetBrains Mono', monospace";
    ctx.fillText(label, -10, 3);
    ctx.restore();
  }
}

function paintMugFront(ctx) {
  const p = toScreen(MUG.u, MUG.v);
  const h = MUG.h;
  const g = ctx.createLinearGradient(p.x - MUG.r, 0, p.x + MUG.r, 0);
  g.addColorStop(0, "#e9dcc2");
  g.addColorStop(0.35, "#fbf3e3");
  g.addColorStop(1, "#b9a27c");
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.ellipse(p.x, p.y, MUG.r, MUG.r * K, 0, 0, Math.PI);
  ctx.lineTo(p.x - MUG.r, p.y - h);
  ctx.ellipse(p.x, p.y - h, MUG.r, MUG.r * K, 0, Math.PI, 0, true);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#99500f";
  ctx.fillRect(p.x - MUG.r, p.y - h * 0.55, MUG.r * 2, 7);
  ctx.strokeStyle = "rgba(60,40,20,0.5)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.ellipse(p.x, p.y - h, MUG.r, MUG.r * K, 0, 0, Math.PI);
  ctx.stroke();
  // Handle.
  ctx.strokeStyle = "#d9c7a5";
  ctx.lineWidth = 9;
  ctx.beginPath();
  ctx.arc(p.x + MUG.r + 6, p.y - h * 0.5, 16, -1.2, 1.2);
  ctx.stroke();
}

function paintLamp(ctx) {
  // An enamel desk lamp leaning in from the top left: arm, cone shade, lit rim.
  ctx.save();
  ctx.strokeStyle = "#232326";
  ctx.lineWidth = 10;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(-700, -760);
  ctx.lineTo(-420, -600);
  ctx.lineTo(-318, -540);
  ctx.stroke();
  ctx.fillStyle = "#3a3a3e";
  ctx.beginPath();
  ctx.arc(-420, -600, 9, 0, TAU);
  ctx.fill();
  const apex = { x: -312, y: -534 };
  const mouth = { x: -248, y: -442, rx: 74, ry: 24, a: -0.42 };
  const edge = (s) => ({
    x: mouth.x + Math.cos(s) * mouth.rx * Math.cos(mouth.a) - Math.sin(s) * mouth.ry * Math.sin(mouth.a),
    y: mouth.y + Math.cos(s) * mouth.rx * Math.sin(mouth.a) + Math.sin(s) * mouth.ry * Math.cos(mouth.a),
  });
  const left = edge(Math.PI);
  const right = edge(0);
  const body = ctx.createLinearGradient(left.x, left.y, right.x, right.y);
  body.addColorStop(0, "#173029");
  body.addColorStop(0.45, "#2f5f50");
  body.addColorStop(0.7, "#4b8a76");
  body.addColorStop(1, "#1e3b33");
  ctx.fillStyle = body;
  ctx.beginPath();
  ctx.moveTo(apex.x - 12, apex.y + 4);
  ctx.lineTo(left.x, left.y);
  ctx.ellipse(mouth.x, mouth.y, mouth.rx, mouth.ry, mouth.a, Math.PI, 0, true);
  ctx.lineTo(apex.x + 12, apex.y - 4);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#2a2a2e";
  ctx.beginPath();
  ctx.ellipse(apex.x, apex.y, 13, 6, mouth.a, 0, TAU);
  ctx.fill();
  // Specular stripe down the enamel.
  ctx.strokeStyle = "rgba(220,255,240,0.25)";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(apex.x + 4, apex.y + 2);
  ctx.lineTo(edge(-0.35).x, edge(-0.35).y);
  ctx.stroke();
  // The warm rim where the bulb spills out.
  ctx.strokeStyle = "rgba(255,226,170,0.85)";
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  ctx.ellipse(mouth.x, mouth.y, mouth.rx, mouth.ry, mouth.a, 0, Math.PI);
  ctx.stroke();
  const glow = ctx.createRadialGradient(mouth.x + 6, mouth.y + 14, 2, mouth.x + 6, mouth.y + 14, 110);
  glow.addColorStop(0, "rgba(255,244,214,0.75)");
  glow.addColorStop(0.3, "rgba(255,214,150,0.25)");
  glow.addColorStop(1, "rgba(255,214,150,0)");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.ellipse(mouth.x + 6, mouth.y + 14, 130, 60, mouth.a, 0, TAU);
  ctx.fill();
  ctx.restore();
}

function paintMotes(ctx, motes, now, sleepK) {
  for (const m of motes) {
    const x = m.home.x + m.nx(now * 0.12) * 60;
    const y = m.home.y + m.ny(now * 0.1) * 50 + ((now * 4) % 40) * (1 - sleepK) * 0;
    ctx.fillStyle = `rgba(255,190,200,${0.25 + 0.2 * Math.sin(now + m.s)})`;
    ctx.beginPath();
    ctx.arc(x, y, m.s, 0, TAU);
    ctx.fill();
  }
}
