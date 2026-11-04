"""One module per job source, each exposing `extract(fetcher) -> list[RawRecord]`.

EXTRACTORS maps a source to its function, so the pipeline (step 4) can loop over sources without
knowing any of them, and adding a source is one module plus one line here.
"""

from jobradar.ingest.sources import getonboard, hn, remotive
from jobradar.ingest.sources.base import Extractor
from jobradar.models import Source

EXTRACTORS: dict[Source, Extractor] = {
    Source.GETONBOARD: getonboard.extract,
    Source.REMOTIVE: remotive.extract,
    Source.HN: hn.extract,
}
