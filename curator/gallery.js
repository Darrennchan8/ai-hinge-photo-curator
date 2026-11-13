// Interactive gallery: re-ranks each category and re-picks the profile in the browser. No network calls.
"use strict";

const YEAR_S = 365.25 * 86400;

// Judges that played each category's finalists: the final judge, then the opinions. "ranker" is the
// judge that ranked everything; p.rank holds its score and p.scores the others'.
const finalJudges = (data) => (data.finalJudge ? [data.finalJudge, ...data.opinions] : []);
const score = (p, key) => (key === "ranker" ? p.rank : p.scores[key] ?? null);
const label = (data, key) => data.labels[key === "ranker" ? data.ranker : key] || key;

// Blend's plain average lets a judge with widely spread scores outvote the others, so "even" first puts
// every judge on the same spread among a category's finalists. Returns photo id -> blended score, in
// the judges' average spread so the age penalty still means roughly the same thing.
function evenBlend(data, finalists) {
  const judges = ["ranker", ...finalJudges(data)]
    .filter((j) => finalists.length && finalists.every((p) => score(p, j) !== null));
  const scores = Object.fromEntries(finalists.map((p) => [p.id, 0]));
  let spreadSum = 0;
  for (const j of judges) {
    const mean = finalists.reduce((s, p) => s + score(p, j), 0) / finalists.length;
    const spread = Math.sqrt(finalists.reduce((s, p) => s + (score(p, j) - mean) ** 2, 0) / finalists.length);
    spreadSum += spread;
    if (spread > 0) for (const p of finalists) scores[p.id] += (score(p, j) - mean) / spread;
  }
  const scale = judges.length ? spreadSum / judges.length / judges.length : 0;
  for (const id of Object.keys(scores)) scores[id] *= scale;
  return scores;
}

function computeView(data, mode, penalty, variety = false) {
  const judges = finalJudges(data);
  const isFinalist = (p) => judges.length > 0 && score(p, data.finalJudge) !== null;
  const buckets = {};
  for (const b of data.buckets) {
    const finalists = b.photos.filter(isFinalist);
    const meanRank = finalists.reduce((s, p) => s + p.rank, 0) / (finalists.length || 1);
    const even = mode === "even" ? evenBlend(data, finalists) : {};
    const rows = b.photos.map((p) => {
      const off = p.t === null ? 0 : (-penalty * (data.now - p.t)) / YEAR_S;
      const own = judges.includes(mode) ? score(p, mode) : null;
      let finalist = false;
      let base = p.rank;
      if (own !== null) {
        finalist = true;
        base = own;
      } else if (mode === "even" && isFinalist(p)) {
        finalist = true;
        base = even[p.id];
      } else if (mode === "blend" && isFinalist(p)) {
        finalist = true;
        const opinions = [p.rank - meanRank, ...judges.map((j) => score(p, j))].filter((v) => v !== null);
        base = opinions.reduce((s, v) => s + v, 0) / opinions.length;
      }
      return { photo: p, bucket: b.name, finalist, value: base + off };
    });
    rows.sort((x, y) => Number(y.finalist) - Number(x.finalist) || y.value - x.value);
    rows.forEach((row, n) => { row.standing = rows.length > 1 ? 1 - n / (rows.length - 1) : 1; });
    buckets[b.name] = rows;
  }
  return { buckets, picks: pickProfile(data, buckets, variety) };
}

// Same rules as report.suggested_profile: the best of each category in order, never two photos from
// one outing, no obscured face first, and at most max_obscured obscured faces overall. Variety, which
// only the gallery has, also looks for a photo that keeps to maxSameLook photos with one expression. It searches only a category's top varietyDepth, because a photo far below them is a worse pick
// than one more repeat; with nothing there, the slot takes the category's best as usual.
function pickProfile(data, buckets, variety) {
  const picks = [];
  const used = {};
  const sameOuting = (p) =>
    p.t !== null && picks.some((q) => q.photo.t !== null && Math.abs(q.photo.t - p.t) < data.sessionGap);
  const overused = (p) =>
    variety && p.look && picks.filter((q) => q.photo.look === p.look).length >= data.maxSameLook;
  const blocked = (row) => {
    if (sameOuting(row.photo)) return true;
    if (!row.photo.obscured) return false;
    return !picks.length || picks.filter((q) => q.photo.obscured).length >= data.maxObscured;
  };
  for (const name of data.profileOrder) {
    if (picks.length >= data.profileSize) break;
    const rows = buckets[name] || [];
    let k = used[name] || 0;
    while (k < rows.length && blocked(rows[k])) k++;
    if (variety) {
      const fresh = rows.slice(k, data.varietyDepth).findIndex((row) => !blocked(row) && !overused(row.photo));
      if (fresh >= 0) k += fresh;
    }
    if (k < rows.length) picks.push(rows[k++]);
    used[name] = k;
  }
  return picks;
}

const signed = (x) => (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(2);

// t = 0 (worst) to 1 (best) -> hue: red, yellow, green, blue.
function scoreHue(t) {
  const stops = [0, 50, 130, 210];
  const at = Math.min(Math.max(t, 0), 1) * (stops.length - 1);
  const k = Math.min(Math.floor(at), stops.length - 2);
  return stops[k] + (stops[k + 1] - stops[k]) * (at - k);
}

// Each judge's lowest and highest score in a category. All but the ranker only cover its finalists.
function scoreRanges(data, photos) {
  const ranges = {};
  for (const key of ["ranker", ...finalJudges(data)]) {
    const values = photos.map((p) => score(p, key)).filter((v) => v !== null);
    if (values.length) ranges[key] = [Math.min(...values), Math.max(...values)];
  }
  return ranges;
}

function chip(text, t) {
  const node = el("span", "score", text);
  node.style.background = `hsl(${Math.round(scoreHue(t))} 80% 80%)`;
  return node;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildCard(p) {
  const card = el("div", "card");
  const link = el("a");
  link.href = p.href;
  link.target = "_blank";
  link.rel = "noopener";
  const img = el("img");
  img.loading = "lazy";
  img.src = p.img;
  img.alt = "";
  link.append(img);
  const meta = el("div", "meta");
  const rank = el("span", "rank");
  const stats = el("span", "muted");
  const scores = el("div", "scores");
  meta.append(rank, " ", stats, scores);
  if (p.zoomed) meta.append(el("span", "badge", "zoomed, as you'd crop in Hinge"));
  for (const b of p.badges) meta.append(el("span", "badge", b));
  if (p.lookLabel) meta.append(el("div", "muted", p.lookLabel));
  if (p.reason) meta.append(el("div", "", p.reason));
  if (p.crop) {
    const wrap = el("div");
    const a = el("a", "", "Suggested crop");
    a.href = p.crop;
    a.target = "_blank";
    a.rel = "noopener";
    wrap.append(a);
    meta.append(wrap);
  }
  card.append(link, meta);
  card._rank = rank;
  card._stats = stats;
  card._scores = scores;
  return card;
}

function updateCard(data, card, row, title, ranges) {
  const p = row.photo;
  card._rank.textContent = title;
  const judged = (key) => {
    const [low, high] = ranges[key];
    const v = score(p, key);
    return chip(label(data, key) + " " + signed(v), high > low ? (v - low) / (high - low) : 1);
  };
  const year = p.t === null ? "undated" : String(new Date(p.t * 1000).getUTCFullYear());
  card._stats.textContent = "· " + year + " · " + p.n + " matchups";
  const line = (...nodes) => { const row = el("div"); row.append(...nodes); return row; };
  const lines = [line(chip("score " + signed(row.value), row.standing)),
                 line(judged("ranker"), " ±" + (2 * p.err).toFixed(2))];
  for (const key of finalJudges(data)) {
    if (score(p, key) !== null) lines.push(line(judged(key)));
  }
  card._scores.replaceChildren(...lines);
}

// The judge choices this gallery has data for: the ranker, each finalist judge, and blends of them.
function modes(data) {
  const judges = finalJudges(data);
  const out = [["ranker", label(data, "ranker")], ...judges.map((j) => [j, label(data, j)])];
  if (judges.length) out.push(["blend", "Blend"], ["even", "Even blend"]);
  return out;
}

function init(data) {
  const KEY = "photo-curator-controls";
  const choices = modes(data);
  const defaults = { mode: choices.length > 1 ? "blend" : "ranker", penalty: data.defaultPenalty, variety: false };
  let state = { ...defaults };
  try {
    state = { ...defaults, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch (e) { /* private window or blocked storage: use the defaults */ }
  if (!choices.some(([value]) => value === state.mode)) state.mode = defaults.mode;

  const fieldset = document.getElementById("modes");
  for (const [value, text] of choices) {
    const input = el("input");
    input.type = "radio";
    input.name = "mode";
    input.value = value;
    const wrap = el("label");
    wrap.append(input, " " + text);
    fieldset.append(wrap);
  }
  fieldset.hidden = choices.length < 2;

  const profile = document.getElementById("profile");
  const sections = document.getElementById("categories");
  const slider = document.getElementById("penalty");
  const output = document.getElementById("penalty-value");
  const variety = document.getElementById("variety");
  variety.parentElement.hidden = !data.buckets.some((b) => b.photos.some((p) => p.look));
  const SHOWN = 24;

  const cards = new Map();
  const grids = {};
  const ranges = Object.fromEntries(data.buckets.map((b) => [b.name, scoreRanges(data, b.photos)]));
  for (const b of data.buckets) {
    for (const p of b.photos) cards.set(p.id, buildCard(p));
    const section = el("section");
    section.append(el("h2", "", b.label),
                   el("p", "sub", b.description + " · " + b.photos.length + " photos"));
    const top = el("div", "grid");
    section.append(top);
    const rest = el("div", "grid");
    if (b.photos.length > SHOWN) {
      const more = el("details");
      more.append(el("summary", "", "The other " + (b.photos.length - SHOWN)), rest);
      section.append(more);
    }
    grids[b.name] = { top, rest };
    sections.append(section);
  }

  function render() {
    const view = computeView(data, state.mode, state.penalty, state.variety);
    output.textContent = state.penalty.toFixed(2);
    slider.value = state.penalty;
    variety.checked = state.variety;
    for (const input of document.querySelectorAll("input[name=mode]")) input.checked = input.value === state.mode;
    profile.replaceChildren(...view.picks.map((row, n) => {
      const card = buildCard(row.photo);
      updateCard(data, card, row, (n + 1) + ". " + row.bucket.replace(/_/g, " "), ranges[row.bucket]);
      return card;
    }));
    for (const b of data.buckets) {
      const nodes = view.buckets[b.name].map((row, n) => {
        const card = cards.get(row.photo.id);
        updateCard(data, card, row, "#" + (n + 1), ranges[b.name]);
        return card;
      });
      grids[b.name].top.replaceChildren(...nodes.slice(0, SHOWN));
      grids[b.name].rest.replaceChildren(...nodes.slice(SHOWN));
    }
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
    } catch (e) { /* not saved; the controls still work */ }
  }

  let queued = false;
  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; render(); });
  };
  slider.addEventListener("input", () => { state.penalty = Number(slider.value); schedule(); });
  variety.addEventListener("change", () => { state.variety = variety.checked; schedule(); });
  for (const input of document.querySelectorAll("input[name=mode]")) {
    input.addEventListener("change", () => { state.mode = input.value; schedule(); });
  }
  document.getElementById("reset").addEventListener("click", () => { state = { ...defaults }; schedule(); });
  render();
}

if (typeof document !== "undefined") init(JSON.parse(document.getElementById("gallery-data").textContent));
if (typeof module !== "undefined") module.exports = { computeView, modes, scoreHue, scoreRanges };
