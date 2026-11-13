# Exporting your photos from Google Photos

The curator works best on an export that holds only photos you're in. Google Photos can already find
those, so the steps are: collect them in an album, then export that album with Google Takeout.

Google changes these screens from time to time, so the button names below may differ slightly.

## 1. Put the photos you're in into an album

1. Open [photos.google.com](https://photos.google.com) on a computer.
2. Search for yourself: click the search box and pick your face under **People**. If no faces show up,
   turn on face grouping in Google Photos settings first and give it a while.
3. Select the photos: tick the first one, scroll to the end, then hold **Shift** and tick the last one.
   Recent years are what matter: by default, photos more than 8 years old are dropped anyway
   (`[prefilter] max_age_years`).
4. Click **+** (Add to) → **Album** → **New album**, and give it a name such as "Photos of me".
   Adding thousands of photos can take a minute.

Videos are skipped later, so there's no need to deselect them.

## 2. Export the album with Google Takeout

1. Open [takeout.google.com](https://takeout.google.com).
2. Click **Deselect all**, then tick **Google Photos**.
3. Click **All photo albums included**, then **Deselect all**, and tick only your new album.
4. Click **Next step** and choose:
   - **Destination:** a download link by email, or Google Drive if you want to read it from the cloud
     (see [Advanced](advanced.md#reading-photos-from-cloud-storage)).
   - **Frequency:** export once.
   - **File type and size:** `.zip` or `.tgz`. Pick the largest size so you get fewer files.
5. Click **Create export**. Google emails you when it's ready, which can take from minutes to a day.

## 3. Hand it to the curator

Download the archives and put them in `input/`. There's no need to unzip them: archives are read in
place, and you can delete them once `ingest` has run. If they don't fit on your disk, leave them on a USB
drive and pass their paths instead:

```sh
uv run python -m curator run /Volumes/USB/takeout-*.zip
```

Takeout puts a small `.json` file next to each photo. The curator reads it for the date the photo was
taken and for its Google Photos link, which is what makes the gallery's photos open in Google Photos.

## Other sources

- **A small album:** open the album, use the ⋮ menu → **Download all**, and put the zip in `input/`.
  Dates then come from the photos' own metadata, and the gallery can't link back to Google Photos.
- **Loose files from anywhere** (a phone backup, an Apple Photos export of unmodified originals): JPEG,
  PNG, HEIC/HEIF and WebP files in `input/`, in any folders. Dates come from the photos' metadata.
- **Not supported:** RAW files, videos, and the motion part of Live Photos.
- **A whole camera roll** rather than just photos of you: add reference photos and set
  `keep_unlocated = false` under `[prefilter]` in `config_overrides.toml`, so only photos where your face
  is found go on. This hasn't been tested much; check `out/prefilter.html` before going further.
