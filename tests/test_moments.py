from curator.prefilter import find_moments

H0, H1 = "0" * 16, "f" * 16  # phashes 64 bits apart
DISTINCT = ["0f" * 8, "f0" * 8, "33" * 8, "cc" * 8, "55" * 8]  # each >= 32 bits from the others


def row(pid, t, phash=H1):
    return {"photo_id": pid, "taken_at": t, "phash": phash}


def test_chains_photos_taken_seconds_apart():
    rows = [row(pid, t, h) for pid, t, h in zip("abcde", (100, 120, 145, 400, 1000), DISTINCT)]
    moments = sorted(find_moments(rows, gap_s=30, max_hamming=8), key=len, reverse=True)
    assert moments[0] == ["a", "b", "c"]  # 20 s then 25 s apart: one chained moment
    assert ["d"] in moments and ["e"] in moments


def test_near_identical_photos_link_without_a_date():
    rows = [row("orig", 100, H0), row("edited", None, H0), row("other", None, H1)]
    moments = find_moments(rows, gap_s=30, max_hamming=8)
    assert sorted(map(sorted, moments)) == [["edited", "orig"], ["other"]]
