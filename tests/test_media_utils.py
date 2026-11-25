from __future__ import annotations

from copper_pilot_cli.media_utils import get_video_from_path


def test_truncated_mp4_file_type_box_is_not_accepted(tmp_path) -> None:
    path = tmp_path / "truncated.mp4"
    path.write_bytes(b"1234ftyp")

    assert get_video_from_path(path) is None


def test_mp4_file_type_box_with_major_brand_is_accepted(tmp_path) -> None:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00")

    video = get_video_from_path(path)

    assert video is not None
    assert video.format == "mp4"
