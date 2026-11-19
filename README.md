# AI Hinge photo curator

Picks your best Hinge photos from a Google Photos export. It filters thousands of photos on your own
computer (finding your face, dropping blurry shots and duplicates), sorts the rest into Hinge-style
categories, and ranks each category by asking an AI model "which of these two photos makes the better
first impression?" thousands of times. You get a local web page with every category ranked and a
suggested six-photo profile.

People are bad at judging their own photos; this gives you a second opinion at scale. It doesn't replace
real feedback: upload the picks with Hinge's Smart Photos on and let real likes reorder them.

## What you need

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/), on macOS, Linux or Windows.
- **A TypeSafe API key** for [Jev](https://typesafe.ai), which does the ranking. Image input is still an
  experimental Jev feature. A run over about 2,500 photos cost about $3. Or a Gemini API key, to use
  [Gemini Decision](#gemini-decision-instead-of-jev) instead.
- **Optional:** Anthropic, Gemini and/or OpenAI keys for a second opinion and written rationales
  (see [More judges](#optional-more-judges)).
- **Your photos:** a Google Takeout export of the photos you're in ([how to export them](docs/google-takeout.md)).
  Loose JPEG, PNG, HEIC/HEIF and WebP files work too.
- About 0.5 MB of disk per photo for working copies.

## Quick start

```sh
git clone https://github.com/Darrennchan8/ai-hinge-photo-curator.git && cd ai-hinge-photo-curator
uv sync
cp .env.example .env                                     # paste your TypeSafe key into .env
cp config_overrides.example.toml config_overrides.toml   # describe who you want to appeal to
```

Put your Takeout `.zip`/`.tgz` files in `input/`, and 3–5 clear photos of just you in `input/reference/`.
The reference photos are optional, but they make finding you in group shots more reliable. Then:

```sh
uv run python -m curator doctor   # checks packages, keys and photos; makes no paid calls
uv run python -m curator run      # every step, start to finish
```

Then open `out/index.html` in a browser.

- **Time:** a first run over a few thousand photos can take a few hours, mostly waiting on the API.
- **Resuming:** every answer is cached in `data/curator.db`, so you can stop `run` and start it again, and
  it picks up where it left off.
- **Spending caps:** each judge stops at its `budget_usd`, $5 by default.
- **Progress:** `uv run python -m curator status` shows progress and spend so far.

To check the face filtering before paying for anything, run `ingest` and `prefilter` on their own and
look at `out/prefilter.html`.

## The gallery

- **Suggested profile:** the best photo from each category, headshot first, never two from the same outing.
- **Categories:** every ranked photo, best first, with each judge's score. Tapping a photo opens it in
  Google Photos when the export included its link.
- **Controls:**
  - choose which judge's ranking to use, or a blend
  - set how much to penalize old photos
  - turn on **Vary expressions**, so the profile isn't six of the same smile

## Optional: more judges

Jev is cheap and answers with probabilities, which suits thousands of comparisons. Other models can add a
final round where they re-judge only each category's top 7. Add their keys to `.env`, then add this to
`config_overrides.toml`:

```toml
[final]
judge = "claude"                 # its order decides each category's top spots
opinions = ["gemini", "openai"]  # extra views, shown as gallery toggles; they change no ranking

[judge]
explainer = "openai"             # a one-line rationale and crop suggestion for shortlisted photos
```

Then run `uv run python -m curator run` again; only the new steps make calls. Each extra judge cost
roughly $2–5 on a full run.

## Gemini Decision instead of Jev

[Gemini Decision 1](https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/)
answers the same way as Jev, with a probability for each option, so it can do every step Jev does. It
only needs `GEMINI_API_KEY`. Add this to `config_overrides.toml`:

```toml
[judge]
ranker = "gemini_decision"
classifier = "gemini_decision"
```

In a test on one person's photos, it was a closer match to Claude, Gemini and OpenAI than Jev was, but a
full run costs more: about $5 (raise `[gemini_decision] budget_usd`), against about $3 for Jev. See
[How it works](docs/how-it-works.md#the-judges) for the numbers.

## Privacy

Your photos stay on your computer, except for resized copies (768 px) sent to the judges you turn on.
Requests to OpenAI and Gemini ask them not to store anything. The Gemini Decisions API has no such
option, and Google's free tier may use requests to improve its products. Check each provider's data policy.
`.env`, `config_overrides.toml`, `input/`, `data/` and `out/` are git-ignored.

## More

- [Exporting from Google Photos](docs/google-takeout.md)
- [How it works](docs/how-it-works.md): the pipeline, its commands and the main settings
- [Advanced](docs/advanced.md): reading from cloud storage, viewing on your phone, changing the persona,
  and Alpine Linux

Every setting is listed, with comments, in [`config_base.toml`](config_base.toml). Don't edit that file;
put your changes in `config_overrides.toml`.

## License

MIT
