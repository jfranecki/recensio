# How the README clips are made

Dev-only. Nothing in this folder is used by the tool.

```
pip install pillow playwright      # no `playwright install` needed: it drives your Chrome or Edge
python docs/demo/build_demo.py     # writes docs/media/*.gif
python docs/demo/build_demo.py --only apply --frames /tmp/frames   # one clip, plus every frame as a PNG
```

| File | What it is |
|---|---|
| `demo_world.py` | A made-up home folder: two fake projects, five fake transcripts, a small memory store. It runs the **real** `tools/dreamctl.py` over them (with `HOME` pointed at the throwaway folder, so your own `~/.claude` is never read) and captures what it prints. The digests and proposals a model would write are written here by hand. |
| `scenes.html` | The four drawn clips (overview, digest, dream, apply) as one page. Each scene is plain HTML whose elements carry the step they appear at (`data-s`), so a scene is a list of stills, not a video. |
| `build_demo.py` | Opens `scenes.html` in a headless browser, feeds it the captured data and photographs each step. For the review clip it starts the real review server and clicks through the real page, drawing a cursor on top. Then it writes the GIFs with one shared palette and no dithering, which keeps flat pages sharp and small. |

Rules worth keeping if you change the clips:

- **Never point this at real data.** The clips are images; nobody can search them for a
  name or a path afterwards. `build_demo.py` refuses to build if the captured output
  contains your user name or a home-folder path.
- **Show only what the tool prints.** Numbers and messages come from the demo run. If a
  scene needs a new number, capture it in `demo_world.py`; do not type it into the scene.
- **The page must read without the clips.** Every clip in the README has alt text, and the
  prose around it says the same thing.
- **Stepped, not smooth.** A step is a still frame held for a set time. That is what keeps
  each clip under a megabyte.
