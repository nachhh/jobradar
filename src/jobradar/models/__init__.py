"""ORM models, one module per area of the system.

- `runs`: the run ledger (pipeline_runs, pipeline_run_sources).
- `postings`: the ingestion layers (raw_postings, postings, rejected_postings).
- `enrichment`: LLM-extracted fields and the log of LLM calls (posting_enrichments, llm_calls).
- `retrieval`: chunks with their keyword index and embedding (posting_chunks).

Concepts:
- SQLAlchemy 2 "typed" mappings: `Mapped[str]` declares the Python type of an attribute and,
  through it, NOT NULL; `Mapped[str | None]` makes the column nullable. `mapped_column()` adds
  the SQL details (type, default, index) the Python type can't express.
- These classes describe the schema; the migrations in migrations/versions/ create it. Alembic's
  autogenerate (and the test `test_models_match_migrations`) compare the two so they can't drift.
- The pipelines write mostly with Core SQL (bulk INSERT … ON CONFLICT) rather than one ORM
  object per row, but they reuse these Table definitions, so column names live in one place.

Enumerated values (statuses, sources) are TEXT columns with a CHECK constraint, not Postgres
ENUM types: adding a value to a CHECK is a one-line migration, while ENUM types can't drop
values and are awkward to change inside a transaction.

Importing this package imports every module, which registers every table on `Base.metadata`.
Alembic (migrations/env.py) relies on that, and the re-exports below let the rest of the code
keep writing `from jobradar.models import Posting` without knowing which module holds it.
"""

from jobradar.models.enrichment import (
    EmploymentType,
    EnrichmentStatus,
    LatamHiring,
    LlmCall,
    LlmCallStatus,
    LlmPurpose,
    PostingEnrichment,
    RoleFamily,
    SalaryBasis,
    Seniority,
)
from jobradar.models.postings import Posting, RawPosting, RejectedPosting, Remote, SalaryPeriod, Source
from jobradar.models.retrieval import EMBEDDING_DIM, PostingChunk
from jobradar.models.runs import (
    Pipeline,
    PipelineRun,
    PipelineRunSource,
    RunStatus,
    SourceStatus,
    Stage,
    Trigger,
)

# __all__ lists the public names: it tells linters these imports are re-exports, not unused.
__all__ = [
    "EMBEDDING_DIM",
    "EmploymentType",
    "EnrichmentStatus",
    "LatamHiring",
    "LlmCall",
    "LlmCallStatus",
    "LlmPurpose",
    "Pipeline",
    "PipelineRun",
    "PipelineRunSource",
    "Posting",
    "PostingChunk",
    "PostingEnrichment",
    "RawPosting",
    "RejectedPosting",
    "Remote",
    "RoleFamily",
    "RunStatus",
    "SalaryBasis",
    "SalaryPeriod",
    "Seniority",
    "Source",
    "SourceStatus",
    "Stage",
    "Trigger",
]
