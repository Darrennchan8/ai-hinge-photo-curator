# Advanced

## Changing the persona

`[persona] audience` is part of every comparison question. Comparisons are cached under a version taken
from the question's exact wording, so changing the persona starts a fresh set of answers rather than
mixing two kinds. The old answers stay in the database.

A full re-rank under a new persona costs about as much as the first run. To save money, you can let the
ranking keep using the old answers for pairs that haven't been asked again:

1. Find the old version's name in `uv run python -m curator status`; it's the `prompt p-…` that isn't
   marked current.
2. List it in `config_overrides.toml`:

   ```toml
   [rank]
   reuse_prompt_versions = ["p-0123456789"]
   ```

3. Run `run` again. Ranking reuses the old answers, `refine` re-asks each category's top contenders under
   the new wording, and `final` replays the finalists.

Where a pair has both, the answer under the current wording wins. Rationales from `explain` aren't
versioned; use `explain --redo` to rewrite them.

## Reading photos from cloud storage

If your Takeout export went to Google Drive, or your photos live in any other cloud storage, `ingest` and
`run` can read them through [rclone](https://rclone.org) without downloading everything first:

```sh
rclone config                                        # set up a remote, e.g. "gdrive" (read-only is enough)
uv run python -m curator run 'gdrive:Takeout'        # a folder of archives or loose photos
```

Archives are streamed, loose files are fetched in batches, and videos and photos already ingested are
never downloaded. If `rclone` isn't on your `PATH`, set `[rclone] binary`.

Large local archives work the same way: pass their paths, and they're read in place. You can also pass
`-` to read a `.tgz` from stdin.

## Viewing the gallery on your phone

`out/index.html` only needs the files next to it (`out/` and `data/judge/`, `data/crops/`), so any static
web server works. `publish` copies exactly those into a folder a web server already serves, under a
random folder name:

```toml
[publish]
www_dir = "/srv/www"                       # a folder your web server serves
base_url = "http://my-server.local:8080"   # the URL that folder is served at
auto = true                                # re-publish after each report
```

The random name is the only protection, so keep the server on your home network. Don't serve the repo
folder itself: it holds your `.env` keys and every working copy.

## Testing without API calls

Set `ranker = "fake"` and `classifier = "fake"` under `[judge]` (and `fake` for any optional judge). The
fake judge answers at random, which is enough to try the whole pipeline and the gallery.

Run the tests with `uv run pytest`. The gallery tests also need Node.js.

## Alpine Linux and other musl systems

PyPI has no OpenCV builds for musl, so install it from the Home Assistant wheel index, then the rest.
The index only has OpenCV 4.8.1.78 for Python 3.11, which needs numpy below 2:

```sh
uv venv --python 3.11
uv pip install --index https://wheels.home-assistant.io/musllinux-index/ "opencv-python-headless==4.8.1.78" "numpy<2"
uv pip install -r pyproject.toml --group dev
uv run --no-sync python -m curator doctor
```

Use `uv run --no-sync` for every command, so uv doesn't try to swap in the locked OpenCV.
