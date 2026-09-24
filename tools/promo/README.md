# Promo video pipeline

Two ways to make footage; the live one is what ships.

**Live (real app, real agent):**
```bash
.venv/bin/pip install playwright imageio-ffmpeg && .venv/bin/playwright install ffmpeg
.venv/bin/python tools/promo/music.py ../agenticcad-admin/promo/promo-music.wav 92     # original soundtrack
cp tools/promo/stage/index.html ../agenticcad-site/promo/index.html                     # title/montage/end cards (site server, promo/ is gitignored there)
.venv/bin/python tools/promo/live.py ../agenticcad-admin/promo                          # records the real app (needs a Claude login)
.venv/bin/python tools/promo/edit.py http://localhost:8790 ../agenticcad-admin/promo    # speeds up waits only, captions, cards, music
```
`live.py` starts a server on a fresh workspace, drives the UI in headless Chrome (typing, sending, clicking faces
by projecting model geometry to screen, ribbon tools) and logs event timestamps. `edit.py` compresses the
`wait:*`→`done:*` spans to ~2.4 s each, keeps everything else at 1×, overlays lower-thirds rendered from the
stage CSS, and prepends/appends the title, montage (with the drawing the agent actually made) and end card.

**Simulated (no agent, deterministic):** `record.py` records the stage page's replay of the site tutorials.
