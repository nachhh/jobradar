"""Unit tests for chunking and chunk headers. Tokens are counted as words here (FakeEmbedder's rule)."""

from jobradar.retrieval.chunk import build_header, split_chunks


def words(text: str) -> int:
    return len(text.split())


def test_header_lists_known_fields_only() -> None:
    posting = {
        "title": "Senior Data Engineer",
        "company": "Acme",
        "seniority_level": "senior",
        "role_family": "unknown",  # left out
        "remote_modes": ["remote", "hybrid"],
        "latam_hiring": "yes",
        "skills_required": ["python", "spark"],
    }
    assert build_header(posting) == (
        "Senior Data Engineer at Acme · senior · remote/hybrid · LATAM: yes\nSkills: python, spark"
    )


def test_header_without_enrichment_still_names_the_job() -> None:
    assert build_header({"title": None, "company": None, "remote_modes": ["unknown"]}) == "Job"


def test_short_text_is_one_chunk() -> None:
    assert split_chunks("Build pipelines.\nPython required.", words, max_tokens=50) == [
        "Build pipelines.\nPython required."
    ]


def test_chunks_respect_the_limit_and_overlap_by_one_line() -> None:
    lines = [f"line {i} has five words" for i in range(10)]  # 5 words each
    chunks = split_chunks("\n".join(lines), words, max_tokens=12)

    assert all(words(c) <= 12 for c in chunks)
    # Each chunk after the first starts with the previous chunk's last line.
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.splitlines()[0] == previous.splitlines()[-1]
    assert chunks[-1].splitlines()[-1] == lines[-1]  # nothing lost at the end


def test_long_line_is_split_on_sentences_then_words() -> None:
    long_line = "First sentence here. " + " ".join(["word"] * 30)  # one line, 33 words
    chunks = split_chunks(long_line, words, max_tokens=10)
    assert all(words(c) <= 10 for c in chunks)
    assert " ".join(chunks).count("word") >= 30  # every word kept (some twice, through overlap)


def test_empty_description_still_gives_one_chunk() -> None:
    assert split_chunks("   \n  ", words) == [""]
