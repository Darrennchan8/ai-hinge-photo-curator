# How it works

`run` does these steps in order. Each is also its own command (`uv run python -m curator <step>`), and
each skips work it has already done.

| Step | What it does | Who |
|---|---|---|
| `ingest` | Reads archives and photos from `input/` (and any paths you pass), applies EXIF rotation, and saves 1568 px working copies in `data/work/`. Dates and Google Photos links come from Takeout's `.json` files, else from the photo's metadata. | No |
| `prefilter` | Finds faces with OpenCV (YuNet + SFace), works out which one is you from `input/reference/` (or from the most common face), and drops old photos, plus tiny or blurry faces if you set those thresholds. Groups photos taken seconds apart into "moments". Writes `out/prefilter.html`. | No |
| `classify` | Sorts each photo into a category (headshot, full body, upper body, activity, social, travel, other) and flags problems. "Hard" flags (screenshots, very low quality) drop a photo; "soft" ones (sunglasses, mirror selfie…) show as badges. | Classifier |
| `playoff` | Keeps the best photo of each category from each moment, so a burst of ten near-identical shots doesn't crowd a category. | Ranker |
| `rank` | Ranks each category with pairwise comparisons, each asked in both orders, and fits a [Bradley-Terry](https://en.wikipedia.org/wiki/Bradley%E2%80%93Terry_model) model. It picks informative pairs and stops once the top 15 settles. | Ranker |
| `fullbody`, `headshot` | A direct yes/no check that moves waist-up shots out of those two categories, then re-ranks both. | Classifier |
| `refine` | Gives each category's top 30 extra comparisons, since that's where close calls matter. | Ranker |
| `final` | Optional: `[final] judge` plays every pair among each category's top 7, and its order decides those spots. | Optional judge |
| `opinion` | Optional: `[final] opinions` judges play the same finalists, shown as gallery toggles. | Optional judges |
| `looks` | Tags each ranked photo's expression, gaze and pose, for the gallery's **Vary expressions** toggle. | Classifier |
| `explain` | Optional: a one-line rationale and crop suggestion for each shortlisted photo. | Optional judge |
| `report` | Writes `out/index.html`. | No |

The ranker and classifier are Jev unless you set `[judge] ranker` and `classifier`.

Other commands:

- `doctor` checks your setup without paid calls.
- `status` shows progress and spend.
- `refit` re-fits the rankings from saved answers, without calls.
- `pilot` compares two judges on the same random pairs.
- `publish` copies the gallery to a web server (see [Advanced](advanced.md)).

## The judges

Every judge sees a resized copy of each photo (768 px) and is asked, as the person in `[persona] audience`,
which of two photos would make a better first impression on Hinge. For group photos and photos where your
face wasn't located, it also gets a reference photo of you and a hint about where you are in the frame.
When your face is very small, the judge sees a portrait crop around you, as you'd zoom in when uploading
to Hinge.

- **Jev** (TypeSafe) is the default for everything. It returns a probability for each answer rather than
  a bare pick, so a near tie counts as a near tie. That suits Bradley-Terry, and it's cheap enough for
  tens of thousands of comparisons.
- **Gemini Decision 1** can take Jev's place as ranker, classifier or both. It answers the same way, with
  a probability for each option.
- **Claude, Gemini and OpenAI** are optional. They're too costly to rank everything, so they only
  re-judge each category's finalists.

### Jev and Gemini Decision compared

One person's 2,183 photos, November 2026, each judge with the same prompt:

| | Jev 1.14 | Gemini Decision 1 |
|---|---|---|
| Agrees with Claude / Gemini / OpenAI on the 105 finalist pairs | 74% / 66% / 74% | 79% / 76% / 77% |
| Agrees with the three's majority | 78% | 81% |
| Bias toward the photo shown first (logits) | −0.45 | −0.12 |
| Pairs it calls decisively (margin over 0.2) | 73% | 45% |
| Input tokens per comparison | ~3,600 | ~2,800 |
| Cost per 1,000 comparisons | ~$0.15 | ~$0.28 |
| Median time per call | 0.2 s | 0.8 s |

- **Rankings:** re-ranking the headshot, full body, upper body and activity categories with Decision
  alone (5,650 calls, $1.61) gave rankings that correlate 0.76–0.87 with Jev's.
  - Both picked the same top headshot.
  - Their top 7s shared 1–5 photos, or 4–6 outings when counted by outing.
- **Categories:** on 100 photos, the two judges put 80% in the same category.
  - Decision flagged far fewer closed eyes, 9 against Jev's 30. Jev often reads squinting smiles as
    closed eyes.

## Scores and the gallery

Scores are relative and only meaningful within a category. `±` is roughly a 95% range.

**Old-photo penalty.** Older photos lose `age_penalty_per_year` (0.15 by default) per year. The judges
never see dates; the penalty is applied afterwards, and the gallery's slider changes it live.

**Suggested profile.** It takes the best of each category in this order: headshot, full body, activity,
travel, social, headshot. It never uses two photos taken within 2 hours of each other
(`session_gap_s`), and it never puts a face-obscured photo first.

**Judge choices** (shown only when optional judges ran):
- **A single judge** orders that category's finalists.
- **Blend** averages every judge's scores.
- **Even blend** puts each judge on the same spread before averaging, so one judge with extreme scores
  can't outvote the rest.

**Vary expressions.** It aims for at most 3 photos with the same expression (big smile, soft smile…),
looking only within each category's top 30. If nothing there fits, it keeps the category's best photo.

## Main settings

All settings are in [`config_base.toml`](../config_base.toml); override them in `config_overrides.toml`.

| Setting | Default | What it changes |
|---|---|---|
| `[persona] audience` | someone on Hinge | Who the judges imagine as the viewer. Worth setting. |
| `[prefilter] max_age_years` | 8 | Photos older than this are dropped (undated ones are kept). |
| `[prefilter] keep_unlocated` | true | Keep photos where your face isn't found. Use false for a whole camera roll. |
| `[rank] comparisons_per_photo` | 10 | Ranking budget per photo. More is steadier and costs more. |
| `[rank] age_penalty_per_year` | 0.15 | Recency bias. |
| `[final] judge`, `opinions` | none | Optional judges for the finalists. |
| `[judge] explainer` | none | Optional rationales. |
| `[<judge>] budget_usd` | 5 | Spending cap per judge. |
| `[[buckets]]` | 7 categories | The categories and their descriptions. Replacing them replaces the whole list. |
