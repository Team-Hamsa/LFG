// webapp/client/swap_pure.js
// Pure logic for the Trait Swapper's before/after preview, kept free of
// DOM/network code so it can be executed and unit-tested under Node
// (tests/test_swap_pure_js.py) — same split as build_pure.js.
//
// A paid swap used to show only "Crown ↔ Cap" text, while the free Build
// panel previews instantly; this module decides what each resulting character
// wears so the chooser can stack the result the same way Build does
// (build_pure.orderedLayers), plus the ape structural art the server injects
// at compose time. Every rule here mirrors a server function, and the tests
// check each one for parity against it — keep them in step.

// Mirrors trait_config.TraitConfig.swap_allowed() so the trait checklist can
// be filtered client-side to what the server will actually accept for the
// selected pair's bodies (#30 Task 15). `matrix` is /api/nfts `swap_matrix`.
// The server re-enforces this in handle_swap_start — this is UI-only.
export function swapAllowed(matrix, bodyA, bodyB, layer) {
  if (bodyA === bodyB || matrix.universal_layers.includes(layer)) return true;
  return matrix.pairs.some((p) => {
    if (!p.bodies.includes(bodyA) || !p.bodies.includes(bodyB)) return false;
    if (p.layers) return p.layers.includes(layer);
    return !p.layers_except.includes(layer);
  });
}

// The swappable traits the chooser offers for this pair, in `swappable` order.
// Without a matrix (an API that predates it) every swappable trait is offered
// and the server's 400 stays the gate.
export function offeredTraits(swappable, matrix, bodyA, bodyB) {
  const all = swappable || [];
  return matrix ? all.filter((t) => swapAllowed(matrix, bodyA, bodyB, t)) : [...all];
}

function attrValue(attributes, slot) {
  const a = (attributes || []).find((x) => x.trait_type === slot);
  return a ? a.value : null;
}

// swap_meta.swap_traits: both resulting characters as {slot: value} maps over
// `order` (trait_order), the picked slots exchanged. Body is never swappable,
// so each result keeps its own body — and its own layer directory. A slot the
// attribute list lacks is null, exactly the server's get_attr None.
export function swapResults(order, attrsA, attrsB, traits) {
  const picked = new Set(traits || []);
  const a = {};
  const b = {};
  for (const slot of order || []) {
    const va = attrValue(attrsA, slot);
    const vb = attrValue(attrsB, slot);
    if (picked.has(slot)) {
      a[slot] = vb;
      b[slot] = va;
    } else {
      a[slot] = va;
      b[slot] = vb;
    }
  }
  return [a, b];
}

function isEmpty(value) {
  return !value || value === 'None';
}

// traits.fill_missing_face_traits: an APE whose face slot is empty after the
// swap gets a rarity-weighted random value rolled into it server-side, so the
// preview cannot show what it will be. Returns those slots in `faceTraits`
// order (the served face_traits).
export function rolledFaceSlots(body, values, faceTraits) {
  if (body !== 'ape') return [];
  const vals = values || {};
  return (faceTraits || []).filter((slot) => isEmpty(vals[slot]));
}

function pairIn(list, slot, value) {
  return (list || []).some((p) => p.trait_type === slot && p.value === value);
}

// ape_face.should_mask: clip this face feature's right side on a melt/xray ape.
function shouldMask(apeFace, slot, value, bodyValue) {
  return apeFace.masked_bodies.includes(bodyValue)
    && apeFace.masked_traits.includes(slot)
    && !pairIn(apeFace.top_traits, slot, value)
    && !pairIn(apeFace.no_mask, slot, value);
}

// ape_face._nose_index: directly above the first non-effect Eyes layer (below
// it when that Eyes value is full-face art), else at the canonical Eyes slot —
// before the first effect layer or the first slot that sorts after Eyes.
function noseIndex(apeFace, order, layers) {
  for (const [i, { slot, value }] of layers.entries()) {
    if (slot === 'Eyes' && !pairIn(apeFace.top_traits, slot, value)) {
      return apeFace.nose_below_eyes.includes(value) ? i : i + 1;
    }
  }
  const eyesRank = order.indexOf('Eyes');
  for (const [i, { slot, value }] of layers.entries()) {
    if (pairIn(apeFace.top_traits, slot, value)) return i;
    if (order.includes(slot) && order.indexOf(slot) > eyesRank) return i;
  }
  return layers.length;
}

// ape_face.inject_and_mask as a drawing plan. `layers` is orderedLayers'
// output (bottom first). Every entry gains `masked` (true: clip its right half
// with the ape mask), and an ape gets the fixed nose inserted as
// {slot: 'Nose', value: 'Nose', asset: 'nose'} — an asset the server serves
// from the ape layer root, not a trait. `mask` says whether this body is a
// melt/xray ape at all, i.e. whether the mask art is part of the composition
// (swap_compose.missing_layers requires it then). Non-ape bodies, and an API
// that sent no ape rules, pass through unchanged.
export function withApeExtras(apeFace, order, layers, body, bodyValue) {
  const plan = (layers || []).map((l) => ({ ...l, masked: false }));
  if (body !== 'ape' || !apeFace) return { layers: plan, mask: false };
  const mask = apeFace.masked_bodies.includes(bodyValue);
  if (mask) {
    for (const l of plan) l.masked = shouldMask(apeFace, l.slot, l.value, bodyValue);
  }
  // The injected nose is a face feature too: clipped on melt/xray bodies.
  const nose = { slot: 'Nose', value: 'Nose', asset: 'nose', masked: mask };
  plan.splice(noseIndex(apeFace, order || [], plan), 0, nose);
  return { layers: plan, mask };
}

function sentence(list) {
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

// The note under the preview, and whether the Swap button must stay disabled.
//   picked    — how many traits are ticked
//   missing   — [{who, what}] resulting layers /api/layer answered 404 for:
//               swap_compose.missing_layers fails the same swap server-side,
//               so it cannot go ahead
//   blanks    — [who] blank sides: handle_swap_start refuses them (#523)
//   unchecked — layers whose art failed to load for any other reason (network):
//               the PREVIEW is incomplete, the swap itself is unaffected
//   rolls     — [{who, slots}] ape face slots the server fills at random
export function previewStatus({ picked, missing, blanks, unchecked, rolls }) {
  if ((blanks || []).length) {
    const one = blanks.length === 1;
    return {
      blocked: true,
      tone: 'error',
      // Same advice as the server's trait_economy.BLANK_CHARACTER_ERROR.
      text: `⚠️ ${sentence(blanks)} ${one ? 'is a blank' : 'are blanks'} and can’t swap traits — `
        + `use Build this GO to dress ${one ? 'it' : 'them'} first.`,
    };
  }
  const seen = new Set();
  const gaps = [];
  for (const { who, what } of missing || []) {
    const key = `${what} on ${who}`;
    if (!seen.has(key)) {
      seen.add(key);
      gaps.push(key);
    }
  }
  if (gaps.length) {
    return {
      blocked: true,
      tone: 'error',
      text: `⚠️ No art for ${sentence(gaps)}, so this swap can’t be made. Pick different traits.`,
    };
  }
  const lines = [];
  if (!picked) lines.push('Tick traits to see both GOs after the swap.');
  for (const { who, slots } of rolls || []) {
    lines.push(`🎲 ${who} has no ${sentence(slots)} — the swap rolls ${slots.length === 1 ? 'one' : 'them'} at random.`);
  }
  if (unchecked) lines.push('Some preview art didn’t load, so the preview may be incomplete.');
  return { blocked: false, tone: unchecked ? 'warn' : 'info', text: lines.join(' ') };
}
