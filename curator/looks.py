"""Tag how the owner looks in each ranked photo. Pairwise judging can't see that a profile repeats
itself, so the gallery's "Vary expressions" toggle uses these tags to limit repeats."""

from .config import Config
from .db import DB, dumps
from .judges import make
from .judging import check_budget, parallel, photos

QUESTIONS = {
    "expression": ("What is the profile owner's facial expression?", {
        "big_smile": "A broad smile with teeth showing",
        "soft_smile": "A slight or closed-mouth smile",
        "neutral": "Serious or not smiling",
        "other_expression": "Something else, such as laughing, a goofy face, or a face too hidden to read",
    }),
    "gaze": ("Where is the profile owner looking?", {
        "at_camera": "At the camera, including from behind sunglasses",
        "looking_away": "Away from the camera: to the side, down, or at something else",
    }),
    "pose": ("How does the photo come across?", {
        "posed": "The profile owner is posing for the photo and is aware of the camera",
        "candid": "The profile owner is caught in the moment or absorbed in something, not posing",
    }),
}


def run(cfg: Config, db: DB, limit: int | None = None) -> None:
    judge = make(cfg, cfg["judge"]["classifier"])
    done = {r["photo_id"] for r in db.all("SELECT photo_id FROM looks WHERE judge = ?", (judge.name,))}
    # Photo ids are content hashes, so a limited run is effectively a random sample.
    todo = [r["photo_id"] for r in db.all("SELECT photo_id FROM ratings WHERE judge = ? ORDER BY photo_id",
                                          (cfg["judge"]["ranker"],)) if r["photo_id"] not in done][:limit]
    print(f"Tagging the look of {len(todo)} photos with {judge.name}...")
    lookup = photos(cfg, db, todo)

    def one(pid):
        check_budget(cfg, db, judge.name)
        answers, tokens = judge.choose(lookup[pid], QUESTIONS)
        db.execute("INSERT OR REPLACE INTO looks VALUES (?,?,?)", (pid, judge.name, dumps(answers)))
        db.execute("INSERT INTO spend (judge, tokens_in, tokens_out) VALUES (?, ?, 0)", (judge.name, tokens))

    failed = parallel(cfg, todo, one, label="look")
    if failed:
        print(f"  {failed} calls failed; re-run to retry them.")
