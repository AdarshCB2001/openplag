# Demo video source

The demo video in `docs/demo/` is generated from code:

| File | What it does |
|---|---|
| `animation.html` | Time-driven animation (`render(t)`), 1920×1080, 56 s. Open it in a browser and add `#12` to the URL to preview second 12. |
| `render.js` | Captures every frame with Playwright and encodes an MP4 with ffmpeg. |
| `compose_music.py` | Synthesises the original background music (numpy/scipy), so it is royalty-free. |

```bash
npm i playwright && npx playwright install chromium
node render.js silent.mp4
python compose_music.py                      # writes music.wav
ffmpeg -i silent.mp4 -i music.wav -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest OpenPlag-demo.mp4
```
