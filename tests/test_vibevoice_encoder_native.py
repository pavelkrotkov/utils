"""Native MLX checks for bounded encoding without resetting transcription.

Run on Apple Silicon with mlx-audio and pytest installed. Portable CI skips
these checks; they deliberately use the real upstream encoder implementation.
"""

import sys
from functools import partial
from types import SimpleNamespace

import pytest

import audio_transcribe_vibevoice as vv

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native MLX encoder")


@pytest.mark.parametrize("length", [1, 7, 200, 1999, 2000, 2003])
def test_bounded_encoder_preserves_features_and_frame_count(length):
    mx = pytest.importorskip("mlx.core")
    encoder_module = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.audio_encoder")
    mx.random.seed(42)
    encoder = encoder_module.TokenizerEncoder(
        n_filters=2, vae_dim=3, ratios=[2, 3], depths=[1, 2, 1]
    )
    hop, history = vv.encoder_geometry(encoder)
    audio = mx.random.normal((1, 1, length))
    expected = encoder(audio)
    lengths = []

    def encode(block):
        lengths.append(block.shape[-1])
        return encoder(block)

    actual = vv.encode_audio_blocks(
        audio, encode=encode, hop=hop, history=history, block_samples=60
    )
    mx.eval(expected, actual)
    assert actual.shape == expected.shape
    assert mx.allclose(actual, expected, atol=1e-5, rtol=1e-5).item()
    assert max(lengths) <= 60 + history


def test_model_tokenizer_wrapper_preserves_native_output():
    mx = pytest.importorskip("mlx.core")
    module = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.audio_encoder")
    config_module = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.config")
    tokenizer = module.AcousticTokenizerEncoder(
        config_module.AcousticTokenizerConfig(
            encoder_n_filters=2, vae_dim=3, encoder_ratios=[2, 3], encoder_depths="1-2-1"
        )
    )
    audio = mx.random.normal((1, 1, 2003))
    expected = tokenizer.encode(audio)
    mx.eval(expected)
    hop, history = vv.encoder_geometry(tokenizer.encoder)
    tokenizer.encode = partial(
        vv.encode_audio_blocks,
        encode=tokenizer.encode,
        hop=hop,
        history=history,
        block_samples=60,
    )
    actual = tokenizer.encode(audio)
    mx.eval(actual)
    assert actual.shape == expected.shape
    assert mx.allclose(actual, expected, atol=1e-5, rtol=1e-5).item()


def test_noncausal_encoder_is_rejected():
    pytest.importorskip("mlx.core")
    module = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.audio_encoder")
    encoder = module.TokenizerEncoder(
        n_filters=2, vae_dim=3, ratios=[2, 3], depths=[1, 2, 1], causal=False
    )
    with pytest.raises(RuntimeError, match="requires causal"):
        vv.encoder_geometry(encoder)


def test_single_pass_releases_encoders_after_features_are_evaluated():
    mx = pytest.importorskip("mlx.core")
    module = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.audio_encoder")
    configs = pytest.importorskip("mlx_audio.stt.models.vibevoice_asr.config")
    options = dict(encoder_n_filters=2, vae_dim=3, encoder_ratios=[2, 3], encoder_depths="1-2-1")
    model = SimpleNamespace(
        acoustic_tokenizer=module.AcousticTokenizerEncoder(
            configs.AcousticTokenizerConfig(**options)
        ),
        semantic_tokenizer=module.SemanticTokenizerEncoder(
            configs.SemanticTokenizerConfig(**options)
        ),
        acoustic_connector=object(),
        semantic_connector=object(),
    )
    model.encode_speech = lambda audio, verbose=False: (
        model.acoustic_tokenizer.encode(audio) + model.semantic_tokenizer.encode(audio)
    )
    audio = mx.random.normal((1, 1, 2003))
    expected = model.encode_speech(audio)
    mx.eval(expected)
    vv.configure_encoder_memory(model, release_encoders=True)
    actual = model.encode_speech(audio)
    assert mx.allclose(actual, expected, atol=1e-5, rtol=1e-5).item()
    assert model.acoustic_tokenizer is None
    assert model.semantic_tokenizer is None
    assert model.acoustic_connector is None
    assert model.semantic_connector is None
