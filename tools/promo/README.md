# Promo video pipeline

Everything here is generated: the footage is the site's simulated tutorial player driven by a "stage" page, the
soundtrack is synthesised in `music.py` (original, no licensing), and Playwright records the stage in Chrome.

```bash
.venv/bin/pip install playwright imageio-ffmpeg && .venv/bin/playwright install ffmpeg
.venv/bin/python tools/promo/music.py ../agenticcad-admin/promo/promo-music.wav 92
cp tools/promo/stage/index.html ../agenticcad-site/promo/index.html      # served by the site's local server (promo/ is gitignored there)
.venv/bin/python tools/promo/record.py http://localhost:8790 ../agenticcad-admin/promo
```

Outputs: `AgenticCAD-promo-1080p.mp4` (16:9 master), `AgenticCAD-promo-thumbnail.png`, and a 1:1 version made with
ffmpeg (see record.py's ffmpeg call for the pattern). Edit `stage/index.html` to change scenes, captions or timing;
the stage reports its duration in `window.__promoSeconds` so the audio is trimmed to match.
