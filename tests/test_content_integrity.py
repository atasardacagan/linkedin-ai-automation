from pathlib import Path

import pytest

from linkedin_automation.models import Post
from linkedin_automation.service import _content_addressed_image, canonical_commentary

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_canonical_commentary_includes_normalized_unique_hashtags():
    post = Post(
        topic="AI",
        content_type="analysis",
        draft="Exact approved text",
        hashtags=["AI", "#ai", "Data Science"],
    )
    assert canonical_commentary(post) == "Exact approved text\n\n#AI #DataScience"


def test_canonical_commentary_removes_hashtag_control_characters():
    post = Post(
        topic="AI",
        content_type="analysis",
        draft="Exact approved text",
        hashtags=["safe\nInjected", "data/ai"],
    )
    assert canonical_commentary(post) == "Exact approved text\n\n#safeInjected #dataai"


def test_commentary_over_linkedin_limit_is_rejected():
    post = Post(topic="AI", content_type="analysis", draft="x" * 3001, hashtags=[])
    with pytest.raises(ValueError):
        canonical_commentary(post)


def test_content_addressed_image_is_loaded_once_and_verified(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "storage" / "images"
    directory.mkdir(parents=True)
    content = PNG_SIGNATURE + b"exact-approved-png-bytes"
    path = _write_content_addressed(directory, content)
    loaded, loaded_digest = _content_addressed_image(path)

    assert loaded == content
    assert loaded_digest == Path(path).stem


def test_content_addressed_image_rejects_mutated_bytes(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "storage" / "images"
    directory.mkdir(parents=True)
    path = _write_content_addressed(directory, PNG_SIGNATURE + b"original")
    path = Path(path)
    path.write_bytes(PNG_SIGNATURE + b"mutated")

    with pytest.raises(ValueError, match="content-addressed"):
        _content_addressed_image(str(path))


def test_content_addressed_image_rejects_file_outside_storage(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "storage" / "images").mkdir(parents=True)
    path = _write_content_addressed(tmp_path, PNG_SIGNATURE + b"outside")

    with pytest.raises(ValueError, match="content-addressed"):
        _content_addressed_image(path)


def test_content_addressed_image_rejects_symlink(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "storage" / "images"
    directory.mkdir(parents=True)
    target = _write_content_addressed(tmp_path, PNG_SIGNATURE + b"target")
    digest = Path(target).stem
    link = directory / f"{digest}.png"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="content-addressed"):
        _content_addressed_image(str(link))


def _write_content_addressed(directory: Path, content: bytes) -> str:
    from hashlib import sha256

    path = directory / f"{sha256(content).hexdigest()}.png"
    path.write_bytes(content)
    return str(path)
