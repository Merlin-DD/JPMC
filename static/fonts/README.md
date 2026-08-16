# Self-hosted fonts

`static/css/desk.css` references these five files by exact name:

| File | Family | Weight |
| --- | --- | --- |
| `Inter-Regular.woff2` | Inter | 400 |
| `Inter-Medium.woff2` | Inter | 500 |
| `Inter-SemiBold.woff2` | Inter | 600 |
| `JetBrainsMono-Regular.woff2` | JetBrains Mono | 400 |
| `JetBrainsMono-Medium.woff2` | JetBrains Mono | 500 |

They are **not** committed — no binaries were downloaded into the repo.

## This matters for deploys

`STORAGES.staticfiles` is WhiteNoise's `CompressedManifestStaticFilesStorage`.
Its post-processing step rewrites every `url()` in the CSS and **raises a hard
error for any referenced file it cannot find**, which fails `collectstatic`,
which fails `build.sh`, which fails the Render deploy.

So until these five files are in this directory:

- `runserver` with `DEBUG=True` is fine — the browser 404s the fonts and
  silently falls back to the system stack in `--font-ui` / `--font-mono`.
- `python manage.py collectstatic` and any Render deploy will **fail**.

Either add the files, or drop the five `@font-face` blocks at the top of
`desk.css` (the fallback stacks already cover both roles).

## Sources

Both fonts are SIL Open Font License 1.1.

- Inter — https://github.com/rsms/inter/releases (`Inter-*.woff2` under `web/`)
- JetBrains Mono — https://github.com/JetBrains/JetBrainsMono/releases
  (`webfonts/JetBrainsMono-*.woff2`)

Downloaded filenames may carry a version or subset suffix; rename them to
match the table above, or update the `@font-face` `src` URLs to match what
you downloaded.
