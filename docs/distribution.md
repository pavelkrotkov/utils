# Distribution and transcription data

## Code and contributions

`utils` has no root `LICENSE` yet. A public GitHub repository is not itself a
grant to modify or redistribute its original code or packaged launcher. Before
publishing a reusable release, the maintainer must approve a license, confirm
the copyright holder/year, and confirm ownership of the app icon artwork.
Until then, do not assume redistribution permission.

Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for changes, and identify any
third-party code or assets in a PR. A future repository license will not
override the separate terms of tools, libraries, model weights, or services.

## What's in the macOS app

`TranscriptionLauncher/Scripts/make-app.sh` packages the Swift executable,
`Info.plist`, `PkgInfo`, and `AppIcon.icns`. SwiftPM declares **no external
package dependencies**. The icon is made from committed PNGs drawn by
`Scripts/generate_icon.py` using Pillow; the script references no external
artwork. The maintainer must still confirm the artwork's provenance.

The `.app` **does not include** the Python scripts, `uv`, `ffmpeg`, `jq`,
`whisper.cpp`, Python packages, or model weights. Users choose a separate
`utils` checkout, install tools, and download models themselves.

## Third-party terms

These components are fetched/installed separately, **not redistributed in
the app**. Licenses below describe upstream projects/model cards, not a grant
from `utils` to repackage them.

| Component | Upstream terms | Usage |
| --- | --- | --- |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE) | MIT | User-installed local ASR executable |
| [Whisper large-v3-turbo Q8 model](https://huggingface.co/ggerganov/whisper.cpp) ([original model](https://huggingface.co/openai/whisper-large-v3-turbo)) | MIT per model cards | User downloads the GGML weights |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) and [PyTorch](https://github.com/pytorch/pytorch/blob/main/LICENSE) | MIT; PyTorch BSD-style with additional notices | `uv` installs for optional speaker diarization |
| [pyannote diarization 3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) and [segmentation 3.0](https://huggingface.co/pyannote/segmentation-3.0) | MIT; gated access and accepted terms required | Default diarization models, fetched with user's `HF_TOKEN` |
| [pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | **CC BY 4.0**; gated access and accepted terms required | Fallback diarization model; attribution rules differ from MIT |
| [mlx-audio](https://github.com/Blaizzy/mlx-audio/blob/main/LICENSE) and [MLX VibeVoice-ASR 4-bit](https://huggingface.co/mlx-community/VibeVoice-ASR-4bit) ([original](https://huggingface.co/microsoft/VibeVoice-ASR)) | MIT for library and model cards | `uv` installs the library; user downloads the model |
| [FFmpeg](https://ffmpeg.org/legal.html), [jq](https://github.com/jqlang/jq), [uv](https://github.com/astral-sh/uv) | FFmpeg LGPL/GPL depending on build; jq MIT; uv MIT OR Apache-2.0 | User-installed CLI tools |
| [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE) | PIL/Pillow license (MIT-CMU) | Icon generation only; not in the app |

If a future release bundles any of these, audit that exact version/build,
its transitive dependencies, model access conditions, and required copyright
notices; include the applicable notices and license texts. **Do not bundle
vendor assets or model files without verifying redistribution rights.**

## Audio and network boundaries

| Route | Where audio is processed | Network activity |
| --- | --- | --- |
| Local Whisper (`audio_transcribe_whisper.py`) | On the user's computer via `ffmpeg` and `whisper.cpp` | User downloads model/tools; no audio upload in this script |
| Whisper with speakers | Local Whisper plus local `pyannote.audio` | Authenticated model download from Hugging Face; no audio upload to pyannoteAI |
| Apple Silicon VibeVoice (`audio_transcribe_vibevoice.py`) | Locally via `mlx-audio` | User downloads the model from Hugging Face; no audio upload in this script |
| OpenAI cloud (`audio_transcribe_openai.sh`) | OpenAI's external API | Sends audio (or locally prepared chunks) via HTTPS to `api.openai.com`; API key and charges apply |

**Model downloads are not transcription uploads.** Local transcription can
run without network access once tools and weights are installed/cached.
The launcher asks for cloud-upload consent, and oversized cloud recordings
require explicit `--chunk` consent after a local size/chunk plan. For the
actual commands and limitations, see the
[launcher setup guide](../TranscriptionLauncher/README.md).
