# BlogCast

Convert blog posts and PDF documents into a self-hosted podcast feed.

BlogCast watches one or more blogs (WordPress or generic RSS) and/or a folder
of PDF files, converts new content into spoken-word MP3 episodes using
Microsoft Edge neural voices, and publishes a private RSS feed you can
subscribe to in any podcast app.

## Features

- Automatic blog discovery (WordPress REST API, with RSS fallback)
- Full historical backfill on first add, then incremental updates
- Optional PDF-to-podcast conversion from a watched folder
- Any language edge-tts supports, not just Finnish and English
- Spoken dates in the correct month names and word order for that language,
  powered by [Babel](https://babel.pocoo.org/)'s CLDR data
- Self-hosted RSS feed with a hardened HTTP server (only the feed, audio and
  assets are exposed — no directory listings, no internal state)
- HTTP range request support, so podcast apps can seek and buffer efficiently
- Docker-first deployment, tested on Synology Container Manager

## How It Works

```
Blog URLs / PDF folder
         │
         ▼
     BlogCast
         │
         ├── Downloads blog content or reads PDFs
         ├── Detects language
         ├── Synthesises speech (edge-tts)
         └── Builds podcast.xml
         │
         ▼
   Podcast app (AntennaPod, Pocket Casts, Apple Podcasts, ...)
```

## Requirements

- Docker and Docker Compose
- Or, for native/Synology deployment: an x86_64 host (the image is built for
  `linux/amd64`)

All Python dependencies (including `ffmpeg`, required for audio processing)
are installed inside the Docker image — you don't need Python installed on
the host to run BlogCast.

## Quick Start

```bash
git clone https://github.com/jugurtti/blogcast.git
cd blogcast
cp .env.example .env
```

Edit `.env` — at minimum, set:

```env
BLOGCAST_BASE_URL=https://your-domain.example
BLOGCAST_BLOG_URLS=https://a-blog-you-like.example
```

Build and run:

```bash
docker build -t blogcast:latest .
docker compose up -d
```

Subscribe to the feed:

```
https://your-domain.example/podcast.xml
```

or, testing locally:

```
http://localhost:8000/podcast.xml
```

## Configuration Reference

All configuration lives in `.env`. See `.env.example` for a ready-to-edit
template with inline comments.

### Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `BLOGCAST_BASE_URL` | Recommended | `http://localhost:8000` | Public URL used in the RSS feed and episode links. Must be reachable by whatever device runs your podcast app. |
| `BLOGCAST_FEED_TITLE` | No | `BlogCast` | Podcast feed title. |
| `BLOGCAST_FEED_AUTHOR` | No | `BlogCast User` | Podcast feed author. |
| `BLOGCAST_FEED_DESCRIPTION` | No | `Turning blog posts into podcast episodes` | Podcast feed description. |
| `PORT` | No | `8000` | HTTP server port inside the container. |
| `UPDATE_INTERVAL` | No | `3600` | Seconds between automatic update runs. |
| `TZ` | No | system default | Container timezone, e.g. `Europe/Helsinki`. |

### Content Source Variables

| Variable | Required | Description |
|---|---|---|
| `BLOGCAST_BLOG_URLS` | No¹ | One or more blog URLs, separated by commas or line breaks. |
| `BLOGCAST_PDF_PATH` | No¹ | Host folder to watch for PDF files. Requires uncommenting the PDF volume line in `docker-compose.yml` (see [PDF Source](#pdf-source)). |

¹ At least one of the two must be configured, or BlogCast has nothing to do.

### Languages and Voices

| Variable | Required | Default | Description |
|---|---|---|---|
| `BLOGCAST_LANGUAGES` | No | `fi,en` | Languages to handle, in order of preference. The first is the fallback when detection is inconclusive, and becomes the feed's `<language>`. |
| `BLOGCAST_VOICE_<LANG>` | Conditional | `Noora` (fi), `Aria` (en) | Voice for each configured language. Finnish and English accept a friendly name; any other language requires a full edge-tts voice ID. See [Adding a Language](#adding-a-language). |

### Spoken Dates (rarely needed — see below)

| Variable | Description |
|---|---|
| `BLOGCAST_MONTHS_<LANG>` | Twelve comma-separated month names, overriding Babel. |
| `BLOGCAST_DATE_FORMAT_<LANG>` | Word order template using `{day}`, `{month}`, `{year}`. |

### Spoken Intro / Outro

| Variable | Description |
|---|---|
| `BLOGCAST_INTRO` / `BLOGCAST_OUTRO` | Generic template applied to every language, unless overridden below. Placeholders: `{blog}` `{date}` `{title}`. |
| `BLOGCAST_INTRO_<LANG>` / `BLOGCAST_OUTRO_<LANG>` | Per-language override. An **empty value disables** that part of the episode. |

## Adding a Language

BlogCast ships with curated voices for **Finnish** and **English**, but works
with any language Microsoft Edge's text-to-speech supports.

### 1. Find a voice ID

List the voices already configured, plus the built-in Finnish/English names:

```bash
docker compose exec blogcast python blogcast.py voices
```

To see every voice edge-tts supports for a language you want to add:

```bash
docker compose exec blogcast edge-tts --list-voices | grep -i de-DE
```

Voice IDs look like `de-DE-KatjaNeural` or `fr-FR-DeniseNeural` — the pattern
is `<language>-<region>-<Name>Neural`.

### 2. Add it to `.env`

```env
BLOGCAST_LANGUAGES=fi,en,de
BLOGCAST_VOICE_DE=de-DE-KatjaNeural
```

That's it for a functional setup — restart the container and German content
will be detected, synthesised in Katja's voice, and dated correctly:

```
29. Juni 2026
```

Babel's CLDR data supplies month names and word order automatically for
essentially every language it knows — German, French, Swedish, Polish,
Russian, Japanese, Portuguese, and many more — with no configuration needed.
A language Babel doesn't recognise falls back to a numeric date
(`29.6.2026`) rather than failing.

### 3. Optional: customise the spoken wording

```env
BLOGCAST_INTRO_DE={blog}. {date}. {title}.
BLOGCAST_OUTRO_DE=Ende des Beitrags: {title}.
```

Leave a variable empty to disable that part entirely:

```env
BLOGCAST_OUTRO_DE=
```

### 4. Optional: override month names or date order

Only needed if Babel doesn't know the language, or you want different
wording than CLDR provides:

```env
BLOGCAST_MONTHS_DE=Januar,Februar,März,April,Mai,Juni,Juli,August,September,Oktober,November,Dezember
BLOGCAST_DATE_FORMAT_DE={day}. {month} {year}
```

### Notes on multi-language behaviour

- If a listed language has no usable voice, BlogCast logs a warning and
  skips it rather than failing the whole container:
  ```
  ⚠️  Language 'de' has no usable voice; ignoring it.
  ```
- Content language is detected automatically per post/PDF and matched only
  against your **configured** languages — a detector hit on a language you
  haven't added falls back to your first configured language.
- You can override the voice for a single source at add-time:
  ```bash
  docker compose exec blogcast python blogcast.py add https://example.com/blog --voice de=de-DE-KatjaNeural
  ```

## Optional Assets

BlogCast works with zero assets — episodes render as plain speech, and the
feed uses each source's own image. Three optional files let you customize
the result further. None are included in this repository; add your own if
you want them.

Place these in `blogcast_data/assets/`:

| File | Purpose | If missing |
|---|---|---|
| `podcast_logo.png` | Feed-wide channel artwork and fallback artwork for episodes without a usable source image. | The first source's own image is used for the channel; episodes without source artwork have no image. |
| `pdf_icon.png` | Generic artwork for PDF episodes, which have no per-source logo of their own. | PDF episodes simply have no episode image. |
| `chime.mp3` | A short sound appended after the spoken text in every episode. | Episodes render as speech only, with no chime. |

### Recommendations

- **`podcast_logo.png`** — square, usually 1400×1400px (standard podcast
  artwork requirements for most apps and directories).
- **`pdf_icon.png`** — same square format; a simple generic document/PDF
  icon works well since it's shared across every PDF episode.
- **`chime.mp3`** — short (2–4 seconds), consistent volume level with your
  spoken audio. A brief tone or jingle works better than music with lyrics.

### Applying changes

Drop the file(s) into `blogcast_data/assets/` on the host, then restart via Synology Container Manager → Project → Action → Restart or:

```bash
docker compose restart
```

## Content Sources

### Blogs

```env
BLOGCAST_BLOG_URLS=https://first-blog.example,https://second-blog.example
```

WordPress sites are detected automatically and use the REST API (full text,
full history). Other sites fall back to RSS.

When adding a blog manually, full history is imported by default. `--new-only`
starts with posts published after the source is added. `--rss-only` instead
counts the items in the site's RSS feed and, for WordPress, fetches that many
newest posts through REST so full post content is used:

```bash
docker compose exec blogcast python blogcast.py add https://example.com/blog --new-only
docker compose exec blogcast python blogcast.py add https://example.com/blog --rss-only
```

On non-WordPress sites, `--rss-only` processes the RSS items directly.

The selected mode is saved per source and shown by `python blogcast.py list`.
To switch an existing source to RSS-sized REST updates and generate any missing
episodes, target it by slug, name, or URL:

```bash
docker compose exec blogcast python blogcast.py update --rss-only <slug-or-url>
```

URLs registered through `BLOGCAST_BLOG_URLS` continue to use full-history
backfill unless switched with the update command.

### PDF Source

PDF support is optional and disabled by default. To enable it:

1. Uncomment this line in `docker-compose.yml`:
   ```yaml
   - "${BLOGCAST_PDF_PATH}:/external-pdf-drop:ro"
   ```
2. Set the host folder in `.env`:
   ```env
   BLOGCAST_PDF_PATH=/volume1/Blogcast
   ```
3. Restart: `docker compose up -d` or via Synology Container Manager → Project → Action → Restart

Behaviour worth knowing:

- The whole folder is rescanned on every update; already-processed PDFs are
  skipped cheaply by filename.
- A PDF younger than 2 minutes is skipped until the next run, in case it's
  still being uploaded/synced.
- If the folder becomes unavailable (unmounted, deleted), BlogCast keeps
  existing episodes in the feed and only disables the source after it's been
  missing for 24 hours — a brief NAS reboot or sync delay won't drop it.

## Commands Reference

Run these inside the container, e.g. `docker compose exec blogcast <command>`.

| Command | Description |
|---|---|
| `python blogcast.py update [--rss-only <blog>]` | Fetch new content from all sources. `--rss-only <blog>` switches that source to RSS-sized REST updates and fills missing episodes. Runs automatically every `UPDATE_INTERVAL` seconds. |
| `python blogcast.py list` | Show configured sources, voices, and episode counts. |
| `python blogcast.py voices` | List configured and built-in voices. |
| `python blogcast.py add <url> [options]` | Add a blog manually, importing full history by default. |
| `python blogcast.py remove <slug\|url\|name> [--purge]` | Unsubscribe. `--purge` also deletes existing episodes. |
| `python blogcast.py serve --port 8000` | Start the HTTP server (already run automatically by `entrypoint.sh`). |

`add` fetch options (mutually exclusive): `--new-only` or `--rss-only`.
Other `add` options: `--voice LANG=VOICE` (repeatable), `--no-intro`,
`--no-outro`, `--no-chime`, `--force` (regenerate audio even if it already
exists).

## Building the Docker Image

```powershell
.\build.ps1
```

This builds `blogcast:latest`, verifies every runtime dependency (including
`babel` and the `ffmpeg` binary) inside the built image, and exports
`blogcast.tar` for import onto another machine or NAS.

To build manually instead:

```bash
docker build --platform linux/amd64 -t blogcast:latest .
docker save -o blogcast.tar blogcast:latest
```

## Deploying on Synology NAS (Container Manager)

### First-time setup

1. Build `blogcast.tar` with `build.ps1` (or `docker save`, above).
2. Copy `blogcast.tar`, `docker-compose.yml`, `entrypoint.sh`, `blogcast.py`
   and your `.env` into a project folder on the NAS, e.g.
   `/volume1/docker/blogcast`.
3. Open **Container Manager → Image**, then **Action → Add → Import →
   From file**, and select `blogcast.tar`.
4. Open **Container Manager → Project → Create**, point it at your project
   folder, and let it pick up `docker-compose.yml`.

### Updating to a new image

1. Build a new `blogcast.tar` and copy it to the NAS.
2. **Container Manager → Image → Action → Add → Import → From file** and
   select the new `blogcast.tar`. This updates the `blogcast:latest` tag.
3. **Container Manager → Project**, select `blogcast`, **Action → Stop**.
4. **Action → Start** again.
5. If the project keeps using the old image instead of picking up the new
   one, remove the old container first over SSH:
   ```bash
   sudo docker rm -f blogcast
   ```
   then start the project again from Container Manager.

### Verifying the running version

```bash
sudo docker exec blogcast python -c "import babel; print(babel.__version__)"
sudo docker exec blogcast python blogcast.py voices
sudo docker logs blogcast
```

## Troubleshooting

**`⚠️  Language 'xx' has no usable voice; ignoring it.`**
Set `BLOGCAST_VOICE_XX` to a valid edge-tts voice ID for that language.

**`SystemExit: No usable language configured.`**
Every language in `BLOGCAST_LANGUAGES` is missing a usable voice. Set at
least one `BLOGCAST_VOICE_<LANG>` to a valid edge-tts voice ID, or remove
that language from `BLOGCAST_LANGUAGES`.

**`ℹ️  No date formatting data for 'xx'; dates will be spoken as numbers.`**
Informational, not an error — Babel doesn't recognise that language code.
Dates will read as `29.6.2026` instead of a spoken form. Set
`BLOGCAST_MONTHS_XX` to fix it.

**`ℹ️  Chime file not found; rendering without it.`**
Informational. Drop an MP3 into `blogcast_data/assets/chime.mp3` if you want
a chime to appear at the end of each episode; episodes render fine without one.

**PDF folder warnings on startup.**
Confirm the volume path is uncommented in `docker-compose.yml` and
`BLOGCAST_PDF_PATH` points to a folder that exists on the host.

## Privacy

BlogCast is designed for self-hosting. No third-party service stores your
content, feed, or generated audio. Text-to-speech synthesis is performed via
Microsoft Edge's public TTS service.

## License

This project is licensed under the [LICENSE](LICENSE).
