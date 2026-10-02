"""Record the playground answering a real conversation, with two synthetic voices on top.

    uv run python demo/playground.py &                         # model key in ~/.config
    ~/.venvs/video/bin/python video/record_demo.py              # -> video/build/demo.mp4

Nothing in the page is scripted: each sentence goes through the live router to the real
server. The script only does what a person at the kitchen counter would - says a sentence,
waits for the answer - and writes down when each thing happened, so the voices can be laid
on afterwards at the right moments. Sam's voice is placed while the words appear in the box
(as live transcription would show them); Tally's voice starts when its answer lands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import subprocess
import time

import edge_tts
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).resolve().parent
BUILD = HERE / "build"
URL = "http://127.0.0.1:8978/"
SAM = "en-US-BrianNeural"
DEVICE = "en-US-AvaNeural"

SENTENCES = [
    "I paid a hundred and thirty two dollars for dinner, split with Chris and Maya.",
    "Chris paid thirty four fifty for the Uber home, just him and me.",
    "Who owes what?",
    "Maya paid me back forty four dollars.",
    "No, cancel that.",
    "How much do I owe Chris?",
    "What's the weather like?",
]


def voiced(text: str, voice: str) -> pathlib.Path:
    """Named by what is said: an index-named cache replays the last take's answer over this one."""
    return BUILD / f"{voice}-{hashlib.sha256(text.encode()).hexdigest()[:12]}.mp3"


async def speak(text: str, voice: str, path: pathlib.Path) -> float:
    if not path.exists():
        await edge_tts.Communicate(text, voice, rate="+4%").save(str(path))
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout)


async def main() -> None:
    BUILD.mkdir(exist_ok=True)
    events: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=1,
            record_video_dir=str(BUILD / "raw"),
            record_video_size={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        t0 = time.monotonic()
        await page.goto(URL)
        await page.click("#reset")  # a fresh household for the take
        await page.wait_for_timeout(2500)

        for sentence in SENTENCES:
            said = voiced(sentence, SAM)
            length = await speak(sentence, SAM, said)
            events.append({"voice": str(said), "at": time.monotonic() - t0})
            # the ring lights and the words arrive as they are spoken
            await page.evaluate("document.getElementById('mic').classList.add('on')")
            words = sentence.split()
            for k in range(1, len(words) + 1):
                await page.fill("#text", " ".join(words[:k]))
                await page.wait_for_timeout(int(1000 * length / len(words)))
            await page.evaluate("document.getElementById('mic').classList.remove('on')")
            async with page.expect_response(lambda r: r.url.endswith("/say")) as waited:
                await page.press("#text", "Enter")
            answer = await (await waited.value).json()
            landed = time.monotonic() - t0
            reply = voiced(answer["spoken"], DEVICE)
            reply_length = await speak(answer["spoken"], DEVICE, reply)
            events.append({"voice": str(reply), "at": landed + 0.15, "router": answer["routed"]["router"]})
            print(f"{landed:6.1f}s  {answer['routed']['router']:26}  {answer['spoken'][:70]}")
            await page.wait_for_timeout(int(1000 * (reply_length + 1.1)))

        await page.wait_for_timeout(1500)
        total = time.monotonic() - t0
        await context.close()
        raw = await page.video.path()
        await browser.close()

    (BUILD / "events.json").write_text(json.dumps(events, indent=1))
    if any("fallback" in e.get("router", "") for e in events):
        print("WARNING: the fallback router answered at least once; retake for the video")

    # lay the voices on the screen recording
    inputs, filters = ["-i", raw], []
    for n, e in enumerate(events, start=1):
        inputs += ["-i", e["voice"]]
        filters.append(f"[{n}]adelay={int(e['at'] * 1000)}:all=1[a{n}]")
    mix = "".join(f"[a{n}]" for n in range(1, len(events) + 1))
    filters.append(f"{mix}amix=inputs={len(events)}:normalize=0,apad[aout]")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[aout]", "-t", f"{total:.2f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            str(BUILD / "demo.mp4"),
        ],
        check=True,
    )  # fmt: skip
    print(f"video/build/demo.mp4  {total:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
