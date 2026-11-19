"""Error types shared by all layers.

Every error carries a stable machine-readable ``code`` (used by the native
messaging host and the extension to choose a message) and a human message.
"""

from __future__ import annotations


class RegistryError(Exception):
    """Base class. ``code`` is stable; ``message`` is for people."""

    code = "registry_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class ChemistryError(RegistryError):
    """The structure cannot be turned into registry keys.

    Codes: ``structure_empty``, ``structure_parse_failed``,
    ``structure_has_placeholders`` (R-groups, query atoms, ``*``, abbreviations),
    ``structure_unsupported`` (polymer or mixture brackets),
    ``inchi_failed``, ``too_many_stereo_groups``, ``invalid_stereo_groups``,
    ``atropisomer_unsupported`` (a chiral axis that cannot be keyed safely),
    ``invalid_mode``.
    """

    code = "structure_parse_failed"


class ConfigError(RegistryError):
    """config.toml is missing a value or contains an invalid one."""

    code = "config_invalid"


class FormatMismatchError(RegistryError):
    """The ID format in config.toml differs from the one stored in the database."""

    code = "id_format_mismatch"


class IdOverflowError(RegistryError):
    """A CORE / STEREO / BATCH number would exceed its fixed width."""

    code = "id_overflow"


class NotFoundError(RegistryError):
    """An ID that was asked for does not exist."""

    code = "not_found"


class SlotConflictError(RegistryError):
    """(experiment_ref, product_index) already holds an active batch of a
    different compound. ``existing`` is that batch (a models.BatchRecord)."""

    code = "slot_conflict"

    def __init__(self, message: str, existing=None):
        super().__init__(message)
        self.existing = existing

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.existing is not None:
            d["existing_id"] = self.existing.full_id
        return d


class StorageError(RegistryError):
    """The database could not be opened, read or written."""

    code = "storage_error"
