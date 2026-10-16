# ShieldCore Music Setup

ShieldCore's music commands use Wavelink and require an accessible **Lavalink v4** server. Wavelink is the Discord client integration; it does not provide the audio server itself.

## Configure Lavalink

1. Install the music dependencies for this workspace from `requirements.txt`.
2. Start your own Lavalink v4 server, or use a Lavalink host that you trust.
3. Set `LAVALINK_URI`, `LAVALINK_PASSWORD`, and optionally `LAVALINK_IDENTIFIER` in `SHIELDCORE SECURITY/.env`. The URI must be reachable from the machine running ShieldCore.
4. Alternatively, edit `music_config.json`. Environment variables override values in that JSON file. Do not commit real passwords or bot tokens.
5. Restart ShieldCore. Startup logs will report whether the Lavalink node connected.

The checked-in configuration uses the Lavalink node listed by the Eve source project. It responded successfully during setup, but it is a shared third-party service and may be rate-limited, unavailable, or changed without notice. For reliable operation, replace it with your own Lavalink host. Keep credentials out of source control; the `.env` variables override the JSON values.

## Commands

- `!play <song or URL>` (also `!sc.play`, `!p`, `!sc.p`): play or queue tracks/playlists.
- `!pause`, `!resume`, `!skip` (also `!sc.skip`, `!s`).
- `!queue` (also `!sc.queue`, `!q`), `!nowplaying` (also `!sc.nowplaying`, `!np`).
- `!volume <0-150>` (also `!sc.volume`, `!vol`), `!shuffle`.
- `!loop <track|queue|off>` (also `!sc.loop`).
- `!stop`, `!leave` (also `!sc.stop`, `!sc.leave`).

Starting playback posts a compact player card with two button rows: Previous, Play, Pause, Next, AutoPlay; then Volume and Loop. The card title uses a red music-note icon and shows the current track artwork as a small thumbnail on the right. AutoPlay starts **ON**, volume starts at **100**, and track loop starts **OFF** for each new voice session. Volume opens a modal accepting 0–150. On track changes the old player card is deleted and a fresh one is posted; the progress bar updates while a track plays.

The bot needs Connect and Speak permissions in the target voice channel. To display the provided red glyphs inside buttons and the red music-note title icon, also grant **Create Expressions** permission and leave room for eight server emojis. ShieldCore uploads/reuses those emojis in that server; without this permission or available emoji slots, the buttons/title fall back to Unicode icons. Search providers and supported URLs depend on the Lavalink server's configured source plugins.
