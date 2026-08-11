"""Evidence-bound section revision submodule (stage 5E.2A, spec G-M)."""

from app.revision.errors import RevisionError
from app.revision.model import RevisionWriterModel

__all__ = ["RevisionError", "RevisionWriterModel"]
