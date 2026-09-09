# Brand assets

Everything here is rendered by `make_brand.py` from two sources of truth, and nothing is drawn by
hand: the mark is `site/src/components/Logo.astro` and the palette and type are
`site/src/styles/tokens.css`. Change either of those and re-run the script rather than editing a PNG.

```bash
.venv/Scripts/python assets/brand/make_brand.py
```

Space Grotesk and JetBrains Mono are fetched once into `.fonts/` (gitignored) — the same two families
the site loads from Google Fonts, so the artwork and the pages set the wordmark identically.

| file | where it goes |
|---|---|
| `tg-avatar-512.png` | Bot profile photo — BotFather → `/mybots` → Edit Bot → Edit Botpic |
| `tg-avatar-solid-512.png` | The same mark with white rings, for the channel, when the two should read as siblings rather than twins |
| `tg-description-640x360.png` | BotFather → Edit Bot → Edit Description Picture (the card shown before anyone presses Start) |
| `tg-start-1280x640.png` | The masthead the bot itself sends on `/start`, help text as its caption — see `START_BANNER` in `fomo_agent/bot.py`. Doubles as the GitHub social preview, which wants exactly this size |
| `og-1200x630.png` | Link preview for the site |

**The avatars are composed for a circle, not a square.** Telegram masks them, and the web mark is a
square frame whose corners the mask would slice off. Scaling that frame to fit the inscribed circle
works but costs a third of the canvas, and at the ~40px a chat list actually renders, the frame plus
three rings collapse into a smudge. So the crop becomes the bezel: the sweep origin moves inside the
circle, the rings grow to fill it, and the quiet rings are struck in graphite rather than carbon,
which is the one deviation from the web mark and exists because carbon disappears at that size.
