"""One module per job source, each exposing two functions:

- `extract(fetcher) -> list[RawRecord]`: stage 1, fetch payloads as received.
- `parse(payload) -> PostingIn`: stage 2, one stored payload into the shared clean shape.

The registries below map a source to its functions, so the pipeline can loop over sources without
knowing any of them, and adding a source is one module plus a line in each registry.
"""

from collections.abc import Callable

from jobradar.ingest.posting import PostingIn
from jobradar.ingest.sources import getonboard, himalayas, hn, jobicy, remotive
from jobradar.ingest.sources.base import Extractor, Payload
from jobradar.models import Source

Parser = Callable[[Payload], PostingIn]

EXTRACTORS: dict[Source, Extractor] = {
    Source.GETONBOARD: getonboard.extract,
    Source.REMOTIVE: remotive.extract,
    Source.HN: hn.extract,
    Source.HIMALAYAS: himalayas.extract,
    Source.JOBICY: jobicy.extract,
}

PARSERS: dict[Source, Parser] = {
    Source.GETONBOARD: getonboard.parse,
    Source.REMOTIVE: remotive.parse,
    Source.HN: hn.parse,
    Source.HIMALAYAS: himalayas.parse,
    Source.JOBICY: jobicy.parse,
}
