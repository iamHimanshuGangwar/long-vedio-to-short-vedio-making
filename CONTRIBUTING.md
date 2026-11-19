# Contributing to Clipsmith

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Quick start

1. Fork the repo and clone your fork.
2. Follow the install steps in the [README](README.md) (`setup.bat` or `./setup.sh`).
3. Start the app with `run.bat` or `./run.sh` and open http://127.0.0.1:8765.

## Where things are

| File | What it does |
|---|---|
| `app/source.py` | Downloads the video and builds a word-level transcript |
| `app/selector.py` | Picks and scores the moments (AI or rule-based) |
| `app/local_llm.py` | Talks to Ollama |
| `app/render.py` | Face tracking, 9:16 crop, captions, ffmpeg encode |
| `app/main.py` | Web server and job queue |
| `static/index.html` | The whole web page |

## Good places to start

Look for issues labelled [`good first issue`](https://github.com/AbdulSamad94/clipsmith/labels/good%20first%20issue). Items on the README roadmap are fair game too; comment on the issue first so two people don't build the same thing.

## UI and design changes

The whole page is one file, `static/index.html`: plain HTML, CSS and JavaScript, no build step. Edit it and reload the browser.

For any change to how the page looks or works, include **screenshots or a short screen recording** in the pull request (desktop and phone width), so it can be judged without running it. Keep it accessible: visible labels on every control, a visible keyboard focus ring, buttons at least 44px tall, text contrast of at least 4.5:1, and no information shown by color alone.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

Write the test first, watch it fail, then make it pass. Tests must not need the network, an AI model or a real YouTube video: test pure functions directly and patch anything slow (see `tests/conftest.py` and `tests/test_api.py`). CI runs the suite on Windows and Linux for every pull request.

## Pull requests

- Keep each PR to one change.
- Run at least one real video through the app before opening it.
- Clipsmith must keep working **with no API key and no GPU**. That's the point of the project.
- Don't commit videos, clips or `.env`.

## Reporting bugs

Use the bug report template and include the error text from the page and the terminal window.
