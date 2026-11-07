# Local warm male Thai voice

Engine: [Wayu-Paxa-TTS-Edge](https://huggingface.co/wayu-ai/wayu-paxa-tts-edge),
model revision `8196688df56a8d08ccfa186505343adff412ab6b`.
Inference code: [wayu-tts-inference](https://github.com/wayu-research/wayu-tts-inference),
revision `07ab898d516649313926f0b843b809a962f993ad`.
Code Apache-2.0; model CC-BY-NC-4.0, for personal/non-commercial use.
Credit: Kunat Pipatanakul and the Wayu Research team. See the model card for full
attribution, terms, and the paper *Building and Evaluating Fixed-Voice Thai TTS
from Synthetic Speech* (2026).

`worker.py` runs under `brain/.tts-venv` as a child of the brain. It only handles
text and WAV files; microphone and playback remain in the permission-bearing
Jarvis brain. There is no HTTP listener. Python network connections are blocked
inside the worker and model paths must already exist. English pronunciation
assets are checked and warmed at startup. Parent exit also terminates the worker.

Settings: `voice.engine=wayu`, `voice.voice=m_mid_warm`, `voice.speed=1.0`,
and `voice.model_dir` pointing to the downloaded model snapshot. The UI voice and
speed controls still work. Other male presets: `m_young_clear`, `m_elderly_deep`,
`m_teen_bright`.

`m_jarvis_smooth` is a local derived preset: the young male voice's prosody,
with 85% young / 15% warm decoder-timbre vectors. The original calibrated young
speaking rate is retained. It is generated in memory from the supplied synthetic
voices; no recording or cloned real-person reference is used. It carries the same
model license. Previewed at user speed 0.93.

The runtime uses four CPU threads, leaving the GPU for recognition and the LLM.
Warm speech generation measured about 0.5–0.7 seconds for short Thai and mixed
Thai/English replies on this M1 Pro. Startup is slower because dictionaries and
models are loaded. Text normalization expands numbers and percentages. The
assistant name is passed as `Jarvis` to avoid a Thai dictionary mispronunciation.
This remains synthetic speech; dictionary segmentation can still misread names.

If loading or generation fails, the brain logs the failure and uses the existing
macOS voices until restart, so it can still answer. To revert deliberately, set
`voice.engine` to `macos` and restart Jarvis. No memory database changes are needed.

## Recreating the isolated environment

Use Python 3.12 to create `brain/.tts-venv`, install `requirements.txt`, then:

```sh
brain/.tts-venv/bin/python -m pip install --no-deps tltk==1.11 \
  'wayu-tts @ git+https://github.com/wayu-research/wayu-tts-inference@07ab898d516649313926f0b843b809a962f993ad'
brain/.tts-venv/bin/python -m spacy download en_core_web_sm
```

TLTK's metadata pins scikit-learn 1.2, which has no Python 3.12 wheel. The tested
runtime uses 1.9.1 and installs TLTK without resolving that obsolete pin. This
voice path uses TLTK's pronunciation dictionaries, not its classification APIs.
Keep this compatibility exception isolated from the main brain environment.
Download the pinned model snapshot before enabling offline inference.
