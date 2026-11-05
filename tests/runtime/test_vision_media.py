"""Synthetic image validation only; no model, GPU or network calls."""

import base64
import copy
import hashlib
import io
import json

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from a3_research_agent.host.models.vision import prepare_vision_request

JOB = "a" * 32


def picture(color="red", size=(16, 12), fmt="PNG", **kwargs):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, fmt, **kwargs)
    return stream.getvalue()


def frame(**updates):
    return {
        "job_id": JOB,
        "path": "frames/first.png",
        "episode_id": "episode-a",
        "step": 1,
        "sim_time_s": 0.1,
        "camera": "front",
        **updates,
    }


def request(*frames, **updates):
    return {
        "question": "What changed between these sampled frames?",
        "frames": list(frames or [frame()]),
        **updates,
    }


def reader(raw, calls=None):
    def read(owner, job_id, path):
        if calls is not None:
            calls.append((owner, job_id, path))
        return {"encoding": "base64", "data": base64.b64encode(raw).decode()}

    return read


def decoded_images(body):
    return [
        base64.b64decode(part["image_url"].split(",", 1)[1])
        for part in body["input"][0]["content"]
        if part["type"] == "input_image"
    ]


def test_safe_typed_request_preserves_order_owner_and_source_data():
    raw = picture()
    calls = []
    body = request(frame(), frame(path="frames/second.png", step=2, sim_time_s=0.2))
    original = copy.deepcopy(body)
    result, metadata, reserve = prepare_vision_request(body, "owner-a", reader(raw, calls))
    assert body == original
    assert calls == [("owner-a", JOB, "frames/first.png"), ("owner-a", JOB, "frames/second.png")]
    assert reserve == 2 * 32768
    assert result["tools"] == [] and result["tool_choice"] == "none"
    assert result["max_output_tokens"] == 4096
    assert "untrusted" in result["instructions"].lower()
    assert [m["step"] for m in metadata] == [1, 2]
    assert metadata[0]["source_job"] == JOB and metadata[0]["path"] == "frames/first.png"
    assert metadata[0]["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata[0]["width"] == 16 and metadata[0]["height"] == 12
    assert "base64" not in json.dumps(metadata)
    for normalized, info in zip(decoded_images(result), metadata, strict=True):
        assert info["normalized_sha256"] == hashlib.sha256(normalized).hexdigest()
        with Image.open(io.BytesIO(normalized)) as image:
            assert image.format == "JPEG" and image.mode == "RGB"
    labels = [
        part["text"] for part in result["input"][0]["content"] if part["type"] == "input_text"
    ]
    assert "0.1" in labels[1] and "0.2" in labels[2]


@pytest.mark.parametrize(
    "change",
    [
        {"question": " "},
        {"question": "x" * 2001},
        {"question": True},
        {"frames": []},
        {"frames": [frame()] * 7},
        {"url": "https://example.invalid/image.png"},
        {"base64": "YWJj"},
        {"model": "other"},
        {"tools": []},
        {"source_owner": "other"},
    ],
)
def test_top_level_schema_rejects_before_artifact_reads(change):
    calls = []
    with pytest.raises((ValueError, ValidationError)):
        prepare_vision_request(request(**change), "owner-a", reader(picture(), calls))
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "A" * 32},
        {"job_id": "b" * 31},
        {"episode_id": ""},
        {"episode_id": "e" * 101},
        {"camera": "c" * 65},
        {"camera": ""},
        {"step": True},
        {"step": 1.5},
        {"step": "1"},
        {"step": -1},
        {"sim_time_s": True},
        {"sim_time_s": "0.1"},
        {"sim_time_s": -0.1},
        {"sim_time_s": float("nan")},
        {"sim_time_s": float("inf")},
        {"source_owner": "other"},
        {"url": "https://example.invalid/x.png"},
        {"path": "../x.png"},
        {"path": "/tmp/x.png"},
        {"path": "dir/../x.png"},
        {"path": "./x.png"},
        {"path": "dir//x.png"},
        {"path": r"dir\x.png"},
        {"path": "https://host/x.png"},
        {"path": "data:image/png;base64,YWJj"},
        {"path": "x.svg"},
        {"path": "x.png\x00.jpg"},
    ],
)
def test_every_frame_is_validated_before_any_read(change):
    calls = []
    with pytest.raises((ValueError, ValidationError)):
        prepare_vision_request(
            request(frame(), frame(**change)), "owner-a", reader(picture(), calls)
        )
    assert calls == []


@pytest.mark.parametrize(
    "frames",
    [
        [frame(step=2), frame(step=1)],
        [frame(sim_time_s=0.2), frame(sim_time_s=0.1)],
        [frame(), frame(episode_id="episode-b"), frame()],
    ],
)
def test_timeline_validation_precedes_reads(frames):
    calls = []
    with pytest.raises(ValueError):
        prepare_vision_request(request(*frames), "owner-a", reader(picture(), calls))
    assert calls == []


def test_same_instant_different_cameras_and_grouped_episodes_are_valid():
    frames = [frame(), frame(camera="wrist"), frame(episode_id="episode-b", step=0, sim_time_s=0)]
    _, metadata, _ = prepare_vision_request(request(*frames), "owner-a", reader(picture()))
    assert [m["camera"] for m in metadata] == ["front", "wrist", "front"]
    assert metadata[-1]["sim_time_s"] == 0


@pytest.mark.parametrize(
    "artifact",
    [
        {},
        {"encoding": "utf8", "data": "x"},
        {"encoding": "base64", "data": "!!!"},
        {"encoding": "base64", "data": b"YQ=="},
        {"encoding": "base64", "data": "data:image/png;base64,YQ=="},
    ],
)
def test_invalid_artifact_envelopes_are_rejected(artifact):
    with pytest.raises(ValueError):
        prepare_vision_request(request(), "owner-a", lambda *args: artifact)


@pytest.mark.parametrize(
    "raw", [b"", b"not an image", picture()[:40], picture(fmt="JPEG")[:-20], picture(fmt="GIF")]
)
def test_malformed_truncated_or_non_png_jpeg_images_are_rejected(raw):
    with pytest.raises(ValueError):
        prepare_vision_request(request(), "owner-a", reader(raw))


def test_pixel_and_original_byte_limits_are_enforced():
    for raw in (picture(size=(1025, 1)), picture(size=(1, 1025)), picture() + b"x" * (512 * 1024)):
        with pytest.raises(ValueError):
            prepare_vision_request(request(), "owner-a", reader(raw))
    _, metadata, _ = prepare_vision_request(
        request(), "owner-a", reader(picture(size=(1024, 1024)))
    )
    assert metadata[0]["width"] * metadata[0]["height"] == 1024**2


def test_animated_png_and_decompression_bombs_are_rejected(monkeypatch):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        stream,
        "PNG",
        save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError):
        prepare_vision_request(request(), "owner-a", reader(stream.getvalue()))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    for size in ((12, 1), (21, 1)):
        with pytest.raises(ValueError):
            prepare_vision_request(request(), "owner-a", reader(picture(size=size)))


def test_reencoding_strips_metadata_and_appended_bytes():
    canary = "PRIVATE-EXIF-TRAILER-CANARY"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", canary)
    raw = picture(pnginfo=metadata) + canary.encode()
    result, frames, _ = prepare_vision_request(request(), "owner-a", reader(raw))
    normalized = decoded_images(result)[0]
    assert canary.encode() not in normalized
    assert frames[0]["original_sha256"] == hashlib.sha256(raw).hexdigest()
    with Image.open(io.BytesIO(normalized)) as image:
        assert "private" not in image.info and not image.getexif()


def test_permission_and_symlink_rejection_remain_reader_owned(tmp_path):
    from a3_research_agent.host.sessions.containers import workspace_path

    outside = tmp_path / "outside.png"
    outside.write_bytes(picture())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "link.png").symlink_to(outside)

    def check_symlink(owner, job, path):
        workspace_path(workspace, path)
        raise AssertionError("symlink should not be accepted")

    with pytest.raises(ValueError, match="link"):
        prepare_vision_request(request(frame(path="link.png")), "owner-a", check_symlink)

    def denied(*args):
        raise PermissionError("not owned")

    with pytest.raises(PermissionError):
        prepare_vision_request(request(), "owner-a", denied)


def test_total_limit_is_enforced_independently_of_per_frame_limit(monkeypatch):
    from a3_research_agent.host.models import vision as vision_media

    raw = picture()
    monkeypatch.setattr(vision_media, "MAX_TOTAL_BYTES", len(raw) * 2 - 1)
    with pytest.raises(ValueError, match="total"):
        prepare_vision_request(
            request(frame(), frame(path="frames/second.png")), "owner-a", reader(raw)
        )


def test_artifact_metadata_cannot_override_requested_source_or_owner():
    raw = picture()
    artifact = {
        "encoding": "base64",
        "data": base64.b64encode(raw).decode(),
        "source_owner": "foreign-owner",
        "path": "foreign.png",
        "job_id": "b" * 32,
    }
    original = copy.deepcopy(artifact)
    calls = []

    def read(owner, job, path):
        calls.append((owner, job, path))
        return artifact

    _, metadata, _ = prepare_vision_request(request(), "owner-a", read)
    assert artifact == original and calls == [("owner-a", JOB, "frames/first.png")]
    assert metadata[0]["source_job"] == JOB and metadata[0]["path"] == "frames/first.png"
    assert "source_owner" not in metadata[0]


def test_jpeg_exif_is_removed_and_normalization_is_deterministic():
    exif = Image.Exif()
    exif[270] = "PRIVATE-EXIF-CANARY"
    raw = picture(fmt="JPEG", exif=exif)
    body = request(frame(path="frames/first.JPEG"))
    first, first_metadata, _ = prepare_vision_request(body, "owner-a", reader(raw))
    second, second_metadata, _ = prepare_vision_request(body, "owner-a", reader(raw))
    assert first == second and first_metadata == second_metadata
    normalized = decoded_images(first)[0]
    assert b"PRIVATE-EXIF-CANARY" not in normalized
    with Image.open(io.BytesIO(normalized)) as image:
        assert not image.getexif()


def test_transparency_is_composited_on_white_without_revealing_hidden_rgb():
    stream = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(stream, "PNG")
    body, _, _ = prepare_vision_request(request(), "owner-a", reader(stream.getvalue()))
    with Image.open(io.BytesIO(decoded_images(body)[0])) as normalized:
        assert normalized.getpixel((0, 0)) == (255, 255, 255)


def test_six_bounded_frames_have_exact_conservative_reserve():
    body = request(*(frame(step=index, sim_time_s=index) for index in range(6)))
    _, metadata, reserve = prepare_vision_request(body, "owner-a", reader(picture()))
    assert len(metadata) == 6 and reserve == 6 * 32768
