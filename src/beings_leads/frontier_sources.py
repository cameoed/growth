from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

DOL_LCA_SCANNER_ID = "dol_lca_workforce_v1"
SEC_FORM_D_SCANNER_ID = "sec_form_d_capital_v1"
DATASF_SCANNER_ID = "sf_ai_capital_constellation_v1"

SUPPORTED_SOURCE_SCANNERS = (
    DOL_LCA_SCANNER_ID,
    SEC_FORM_D_SCANNER_ID,
    DATASF_SCANNER_ID,
)

DEFAULT_SOURCE_FILENAMES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        DOL_LCA_SCANNER_ID: ("dol_lca.csv", "dol_lca.jsonl", "dol_lca.json"),
        SEC_FORM_D_SCANNER_ID: (
            "sec_form_d.csv",
            "sec_form_d.jsonl",
            "sec_form_d.json",
        ),
        DATASF_SCANNER_ID: (
            "datasf_registered_businesses.csv",
            "datasf_registered_businesses.jsonl",
            "datasf_registered_businesses.json",
        ),
    }
)

SourceLoadStatus = Literal["disabled", "absent", "loaded", "failed"]
SourceFileFormat = Literal["csv", "jsonl", "json_array"]

_ALLOWED_SUFFIXES = {".csv", ".jsonl", ".ndjson", ".json"}
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CSV_LIMIT_LOCK = threading.Lock()
_NAME_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        DOL_LCA_SCANNER_ID: (
            "EMPLOYER_NAME",
            "EMPLOYER_BUSINESS_NAME",
            "EMPLOYER",
        ),
        SEC_FORM_D_SCANNER_ID: (
            "ISSUER_NAME",
            "ENTITY_NAME",
            "ISSUERENTITYNAME",
            "NAME_OF_ISSUER",
        ),
        DATASF_SCANNER_ID: (
            "full_business_name",
            "Ownership Name",
            "legal_name",
            "dba_name",
            "DBA Name",
            "business_name",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class FrontierSourceLimits:
    """Hard resource ceilings for a single local catalog build."""

    max_files_per_scanner: int = 16
    max_file_bytes: int = 512_000_000
    max_scanner_bytes: int = 1_024_000_000
    max_rows_per_file: int = 2_000_000
    max_rows_per_scanner: int = 4_000_000
    max_fields_per_row: int = 512
    max_field_bytes: int = 64_000
    max_record_bytes: int = 1_000_000
    max_json_array_bytes: int = 32_000_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Frontier source limit {name} must be a positive integer")
        if self.max_file_bytes > self.max_scanner_bytes:
            raise ValueError("max_file_bytes cannot exceed max_scanner_bytes")
        if self.max_rows_per_file > self.max_rows_per_scanner:
            raise ValueError("max_rows_per_file cannot exceed max_rows_per_scanner")
        if self.max_field_bytes > self.max_record_bytes:
            raise ValueError("max_field_bytes cannot exceed max_record_bytes")
        if self.max_json_array_bytes > self.max_file_bytes:
            raise ValueError("max_json_array_bytes cannot exceed max_file_bytes")


@dataclass(frozen=True, slots=True)
class FrontierSourceError:
    scanner_id: str
    filename: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class FrontierSourceProvenance:
    scanner_id: str
    filename: str
    path: str
    file_format: SourceFileFormat
    byte_count: int
    sha256: str
    rows_seen: int
    rows_with_legal_name: int


@dataclass(frozen=True, slots=True)
class FrontierSourceStats:
    scanner_id: str
    status: SourceLoadStatus
    configured_files: int
    discovered_files: int
    loaded_files: int
    bytes_read: int
    rows_seen: int
    rows_with_legal_name: int
    rows_without_legal_name: int
    rows_indexed: int
    unique_legal_names: int


@dataclass(frozen=True, slots=True)
class IndexedFrontierSourceRecord:
    """One immutable row plus its exact local-file locator."""

    record: Mapping[str, Any]
    filename: str
    row_number: int

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(_thaw_json(self.record))


@dataclass(frozen=True, slots=True)
class FrontierSourceLookup:
    scanner_id: str
    requested_legal_name: str
    normalized_legal_name: str
    entries: tuple[IndexedFrontierSourceRecord, ...]
    provenance: tuple[FrontierSourceProvenance, ...]
    stats: FrontierSourceStats
    errors: tuple[FrontierSourceError, ...]

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        """Return defensive row copies suitable for the existing scanner parsers."""
        return tuple(item.to_dict() for item in self.entries)

    @property
    def matched(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True, slots=True)
class LocalFrontierSourceCatalog:
    """One-time local dataset index with constant-time exact-name lookups."""

    _indexes: Mapping[
        str, Mapping[str, tuple[IndexedFrontierSourceRecord, ...]]
    ] = field(repr=False)
    provenance: tuple[FrontierSourceProvenance, ...]
    stats: Mapping[str, FrontierSourceStats]
    errors: tuple[FrontierSourceError, ...]

    def lookup(self, scanner_id: str, legal_name: str) -> FrontierSourceLookup:
        if scanner_id not in SUPPORTED_SOURCE_SCANNERS:
            raise ValueError(f"Unsupported frontier source scanner: {scanner_id}")
        normalized = normalize_legal_name(legal_name)
        entries = self._indexes.get(scanner_id, {}).get(normalized, ()) if normalized else ()
        return FrontierSourceLookup(
            scanner_id=scanner_id,
            requested_legal_name=str(legal_name),
            normalized_legal_name=normalized,
            entries=entries,
            provenance=tuple(
                item for item in self.provenance if item.scanner_id == scanner_id
            ),
            stats=self.stats[scanner_id],
            errors=tuple(item for item in self.errors if item.scanner_id == scanner_id),
        )

    def records_for(self, scanner_id: str, legal_name: str) -> tuple[dict[str, Any], ...]:
        return self.lookup(scanner_id, legal_name).records

    def lookup_dol_lca(self, legal_name: str) -> FrontierSourceLookup:
        return self.lookup(DOL_LCA_SCANNER_ID, legal_name)

    def lookup_sec_form_d(self, legal_name: str) -> FrontierSourceLookup:
        return self.lookup(SEC_FORM_D_SCANNER_ID, legal_name)

    def lookup_datasf(self, legal_name: str) -> FrontierSourceLookup:
        return self.lookup(DATASF_SCANNER_ID, legal_name)


@dataclass(slots=True)
class _ParseState:
    bytes_read: int = 0
    rows_seen: int = 0
    rows_with_legal_name: int = 0
    rows_without_legal_name: int = 0


@dataclass(frozen=True, slots=True)
class _LoadedFile:
    index: Mapping[str, tuple[IndexedFrontierSourceRecord, ...]]
    provenance: FrontierSourceProvenance
    rows_without_legal_name: int


class _SourceLoadFailure(Exception):
    def __init__(self, code: str, message: str, state: _ParseState | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.state = state or _ParseState()


class _DecodedLineStream:
    def __init__(
        self,
        raw: Any,
        *,
        state: _ParseState,
        maximum_bytes: int,
        maximum_line_bytes: int,
    ) -> None:
        self._raw = raw
        self._state = state
        self._maximum_bytes = maximum_bytes
        self._maximum_line_bytes = maximum_line_bytes
        self._first_line = True
        self.digest = hashlib.sha256()

    def __iter__(self) -> _DecodedLineStream:
        return self

    def __next__(self) -> str:
        try:
            line = self._raw.readline(self._maximum_line_bytes + 1)
        except OSError as exc:
            raise _SourceLoadFailure(
                "read_failed", "The local source file could not be read.", self._state
            ) from exc
        if not line:
            raise StopIteration
        self._state.bytes_read += len(line)
        if self._state.bytes_read > self._maximum_bytes:
            raise _SourceLoadFailure(
                "file_bytes_exceeded", "The local source exceeded its byte limit.", self._state
            )
        if len(line) > self._maximum_line_bytes:
            raise _SourceLoadFailure(
                "record_bytes_exceeded", "A source record exceeded its byte limit.", self._state
            )
        if b"\x00" in line:
            raise _SourceLoadFailure(
                "malformed_text", "The local source contains a NUL byte.", self._state
            )
        self.digest.update(line)
        encoding = "utf-8-sig" if self._first_line else "utf-8"
        self._first_line = False
        try:
            return line.decode(encoding)
        except UnicodeDecodeError as exc:
            raise _SourceLoadFailure(
                "invalid_encoding", "The local source must be valid UTF-8.", self._state
            ) from exc


def normalize_legal_name(value: str) -> str:
    """Normalize punctuation, whitespace, and case only; never expand or remove suffixes."""
    normalized = unicodedata.normalize("NFKC", str(value or "").casefold())
    words = "".join(
        character if character.isalnum() else " " for character in normalized
    ).split()
    return " ".join(words)


def load_local_frontier_sources(
    directory: str | os.PathLike[str],
    *,
    filenames: Mapping[str, str | Sequence[str]] | None = None,
    limits: FrontierSourceLimits | None = None,
) -> LocalFrontierSourceCatalog:
    """Load only explicitly named local files; this function performs no network I/O.

    A malformed or over-budget file invalidates only its scanner's complete index. Missing
    configured filenames are not treated as evidence that a legal name is absent.
    """
    active_limits = limits or FrontierSourceLimits()
    configured = _validated_filename_config(filenames)
    root = Path(directory).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"Frontier source directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Frontier source path is not a directory: {root}")
    root = root.resolve(strict=True)

    indexes: dict[str, Mapping[str, tuple[IndexedFrontierSourceRecord, ...]]] = {}
    provenance: list[FrontierSourceProvenance] = []
    all_errors: list[FrontierSourceError] = []
    all_stats: dict[str, FrontierSourceStats] = {}

    for scanner_id in SUPPORTED_SOURCE_SCANNERS:
        scanner_filenames = configured[scanner_id]
        if not scanner_filenames:
            indexes[scanner_id] = MappingProxyType({})
            all_stats[scanner_id] = _empty_stats(scanner_id, "disabled", 0)
            continue

        discovered: list[tuple[str, Path, os.stat_result]] = []
        discovered_count = 0
        scanner_errors: list[FrontierSourceError] = []
        for filename in scanner_filenames:
            path = root / filename
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                scanner_errors.append(
                    _error(scanner_id, filename, "stat_failed", "The local source cannot be read.")
                )
                continue
            discovered_count += 1
            if stat.S_ISLNK(path_stat.st_mode):
                scanner_errors.append(
                    _error(
                        scanner_id,
                        filename,
                        "symlink_not_allowed",
                        "Symlinked local sources are not allowed.",
                    )
                )
                continue
            if not stat.S_ISREG(path_stat.st_mode):
                scanner_errors.append(
                    _error(
                        scanner_id,
                        filename,
                        "not_regular_file",
                        "The configured local source is not a regular file.",
                    )
                )
                continue
            if path_stat.st_size > active_limits.max_file_bytes:
                scanner_errors.append(
                    _error(
                        scanner_id,
                        filename,
                        "file_bytes_exceeded",
                        "The local source exceeds its byte limit.",
                    )
                )
                continue
            discovered.append((filename, path, path_stat))

        if discovered_count > active_limits.max_files_per_scanner:
            scanner_errors.append(
                _error(
                    scanner_id,
                    "",
                    "file_count_exceeded",
                    "The scanner has too many configured source files.",
                )
            )
        if sum(item[2].st_size for item in discovered) > active_limits.max_scanner_bytes:
            scanner_errors.append(
                _error(
                    scanner_id,
                    "",
                    "scanner_bytes_exceeded",
                    "The scanner's local sources exceed the aggregate byte limit.",
                )
            )

        configured_count = len(scanner_filenames)
        if scanner_errors:
            indexes[scanner_id] = MappingProxyType({})
            all_errors.extend(scanner_errors)
            all_stats[scanner_id] = FrontierSourceStats(
                scanner_id=scanner_id,
                status="failed",
                configured_files=configured_count,
                discovered_files=discovered_count,
                loaded_files=0,
                bytes_read=0,
                rows_seen=0,
                rows_with_legal_name=0,
                rows_without_legal_name=0,
                rows_indexed=0,
                unique_legal_names=0,
            )
            continue
        if not discovered:
            indexes[scanner_id] = MappingProxyType({})
            all_stats[scanner_id] = _empty_stats(scanner_id, "absent", configured_count)
            continue

        staged: dict[str, list[IndexedFrontierSourceRecord]] = defaultdict(list)
        loaded_files = 0
        bytes_read = 0
        rows_seen = 0
        rows_with_name = 0
        rows_without_name = 0
        scanner_provenance: list[FrontierSourceProvenance] = []
        for filename, path, expected_stat in discovered:
            remaining_rows = active_limits.max_rows_per_scanner - rows_seen
            if remaining_rows <= 0:
                scanner_errors.append(
                    _error(
                        scanner_id,
                        filename,
                        "scanner_rows_exceeded",
                        "The scanner exceeded its aggregate row limit.",
                    )
                )
                break
            try:
                loaded = _load_source_file(
                    scanner_id,
                    filename,
                    path,
                    expected_stat,
                    active_limits,
                    maximum_rows=min(active_limits.max_rows_per_file, remaining_rows),
                )
            except _SourceLoadFailure as exc:
                bytes_read += exc.state.bytes_read
                rows_seen += exc.state.rows_seen
                rows_with_name += exc.state.rows_with_legal_name
                rows_without_name += exc.state.rows_without_legal_name
                scanner_errors.append(
                    _error(scanner_id, filename, exc.code, exc.safe_message)
                )
                break
            loaded_files += 1
            bytes_read += loaded.provenance.byte_count
            rows_seen += loaded.provenance.rows_seen
            rows_with_name += loaded.provenance.rows_with_legal_name
            rows_without_name += loaded.rows_without_legal_name
            scanner_provenance.append(loaded.provenance)
            for legal_name, records in loaded.index.items():
                staged[legal_name].extend(records)

        provenance.extend(scanner_provenance)
        if scanner_errors:
            indexes[scanner_id] = MappingProxyType({})
            all_errors.extend(scanner_errors)
            all_stats[scanner_id] = FrontierSourceStats(
                scanner_id=scanner_id,
                status="failed",
                configured_files=configured_count,
                discovered_files=discovered_count,
                loaded_files=loaded_files,
                bytes_read=bytes_read,
                rows_seen=rows_seen,
                rows_with_legal_name=rows_with_name,
                rows_without_legal_name=rows_without_name,
                rows_indexed=0,
                unique_legal_names=0,
            )
            continue

        frozen_index = MappingProxyType(
            {
                key: tuple(staged[key])
                for key in sorted(staged)
            }
        )
        indexes[scanner_id] = frozen_index
        all_stats[scanner_id] = FrontierSourceStats(
            scanner_id=scanner_id,
            status="loaded",
            configured_files=configured_count,
            discovered_files=discovered_count,
            loaded_files=loaded_files,
            bytes_read=bytes_read,
            rows_seen=rows_seen,
            rows_with_legal_name=rows_with_name,
            rows_without_legal_name=rows_without_name,
            rows_indexed=rows_with_name,
            unique_legal_names=len(frozen_index),
        )

    return LocalFrontierSourceCatalog(
        _indexes=MappingProxyType(indexes),
        provenance=tuple(provenance),
        stats=MappingProxyType(all_stats),
        errors=tuple(all_errors),
    )


def _validated_filename_config(
    filenames: Mapping[str, str | Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    configured = {key: tuple(value) for key, value in DEFAULT_SOURCE_FILENAMES.items()}
    if filenames is not None:
        unknown = set(filenames) - set(SUPPORTED_SOURCE_SCANNERS)
        if unknown:
            unexpected = ", ".join(sorted(unknown))
            raise ValueError(f"Unsupported frontier source scanner(s): {unexpected}")
        for scanner_id, raw_names in filenames.items():
            names = (raw_names,) if isinstance(raw_names, str) else tuple(raw_names)
            configured[scanner_id] = names

    occupied: dict[str, str] = {}
    for scanner_id in SUPPORTED_SOURCE_SCANNERS:
        names = configured[scanner_id]
        if any(not isinstance(name, str) for name in names):
            raise ValueError(f"Unsafe frontier source filename for {scanner_id}")
        if len(names) > len(set(name.casefold() for name in names)):
            raise ValueError(f"Duplicate source filename configured for {scanner_id}")
        validated: list[str] = []
        for name in names:
            if not isinstance(name, str) or not _SAFE_FILENAME.fullmatch(name):
                raise ValueError(f"Unsafe frontier source filename for {scanner_id}")
            if Path(name).suffix.casefold() not in _ALLOWED_SUFFIXES:
                raise ValueError(f"Unsupported frontier source format for {scanner_id}: {name}")
            folded = name.casefold()
            owner = occupied.get(folded)
            if owner is not None and owner != scanner_id:
                raise ValueError(f"Source filename {name} is assigned to more than one scanner")
            occupied[folded] = scanner_id
            validated.append(name)
        configured[scanner_id] = tuple(sorted(validated))
    return configured


def _load_source_file(
    scanner_id: str,
    filename: str,
    path: Path,
    expected_stat: os.stat_result,
    limits: FrontierSourceLimits,
    *,
    maximum_rows: int,
) -> _LoadedFile:
    state = _ParseState()
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _SourceLoadFailure(
            "open_failed", "The local source could not be opened safely.", state
        ) from exc

    try:
        actual_stat = os.fstat(descriptor)
        if not stat.S_ISREG(actual_stat.st_mode):
            raise _SourceLoadFailure(
                "not_regular_file", "The configured local source is not a regular file.", state
            )
        if (actual_stat.st_dev, actual_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ) or (actual_stat.st_size, actual_stat.st_mtime_ns) != (
            expected_stat.st_size,
            expected_stat.st_mtime_ns,
        ):
            raise _SourceLoadFailure(
                "file_changed", "The local source changed before it could be read.", state
            )
        if actual_stat.st_size > limits.max_file_bytes:
            raise _SourceLoadFailure(
                "file_bytes_exceeded", "The local source exceeds its byte limit.", state
            )
        with os.fdopen(descriptor, "rb", closefd=False) as raw:
            magic = raw.read(min(512, actual_stat.st_size))
            raw.seek(0)
            if _looks_archived(magic):
                raise _SourceLoadFailure(
                    "archive_not_allowed",
                    "Archived or compressed local sources are not allowed.",
                    state,
                )
            file_format = _file_format(filename)
            if file_format == "json_array":
                rows, digest = _read_json_array(
                    raw, state=state, limits=limits, maximum_rows=maximum_rows
                )
            else:
                line_stream = _DecodedLineStream(
                    raw,
                    state=state,
                    maximum_bytes=limits.max_file_bytes,
                    maximum_line_bytes=limits.max_record_bytes,
                )
                if file_format == "csv":
                    rows = _iter_csv_rows(
                        line_stream,
                        scanner_id=scanner_id,
                        state=state,
                        limits=limits,
                        maximum_rows=maximum_rows,
                    )
                else:
                    rows = _iter_jsonl_rows(
                        line_stream,
                        state=state,
                        limits=limits,
                        maximum_rows=maximum_rows,
                    )
                loaded_index = _index_rows(
                    scanner_id,
                    filename,
                    rows,
                    state=state,
                    limits=limits,
                )
                if file_format == "jsonl" and state.rows_seen == 0:
                    raise _SourceLoadFailure(
                        "empty_dataset",
                        "A JSONL source must contain at least one object record.",
                        state,
                    )
                digest = line_stream.digest.hexdigest()
                _assert_file_unchanged(raw, actual_stat, state)
                if state.bytes_read != actual_stat.st_size:
                    raise _SourceLoadFailure(
                        "file_changed", "The local source changed while it was read.", state
                    )
                return _loaded_file(
                    scanner_id,
                    filename,
                    path,
                    file_format,
                    digest,
                    loaded_index,
                    state,
                )

            loaded_index = _index_rows(
                scanner_id,
                filename,
                rows,
                state=state,
                limits=limits,
            )
            if state.rows_seen == 0:
                raise _SourceLoadFailure(
                    "empty_dataset",
                    "A JSON array source must contain at least one object record.",
                    state,
                )
            _assert_file_unchanged(raw, actual_stat, state)
            if state.bytes_read != actual_stat.st_size:
                raise _SourceLoadFailure(
                    "file_changed", "The local source changed while it was read.", state
                )
            return _loaded_file(
                scanner_id,
                filename,
                path,
                file_format,
                digest,
                loaded_index,
                state,
            )
    except _SourceLoadFailure:
        raise
    except (csv.Error, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise _SourceLoadFailure(
            "malformed_input", "The local source is malformed.", state
        ) from exc
    except OSError as exc:
        raise _SourceLoadFailure(
            "read_failed", "The local source file could not be read.", state
        ) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _loaded_file(
    scanner_id: str,
    filename: str,
    path: Path,
    file_format: SourceFileFormat,
    digest: str,
    index: Mapping[str, tuple[IndexedFrontierSourceRecord, ...]],
    state: _ParseState,
) -> _LoadedFile:
    return _LoadedFile(
        index=index,
        provenance=FrontierSourceProvenance(
            scanner_id=scanner_id,
            filename=filename,
            path=str(path),
            file_format=file_format,
            byte_count=state.bytes_read,
            sha256=digest,
            rows_seen=state.rows_seen,
            rows_with_legal_name=state.rows_with_legal_name,
        ),
        rows_without_legal_name=state.rows_without_legal_name,
    )


def _iter_csv_rows(
    lines: _DecodedLineStream,
    *,
    scanner_id: str,
    state: _ParseState,
    limits: FrontierSourceLimits,
    maximum_rows: int,
) -> Iterator[tuple[int, Mapping[str, Any]]]:
    with _CSV_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(limits.max_record_bytes)
        try:
            reader = csv.reader(lines, strict=True)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise _SourceLoadFailure(
                    "missing_header", "A CSV source must contain a header row.", state
                ) from exc
            _validate_csv_headers(headers, scanner_id, limits, state)
            for values in reader:
                if not values:
                    continue
                state.rows_seen += 1
                if state.rows_seen > maximum_rows:
                    raise _SourceLoadFailure(
                        "row_limit_exceeded", "The local source exceeded its row limit.", state
                    )
                if len(values) != len(headers):
                    raise _SourceLoadFailure(
                        "malformed_csv",
                        "A CSV row does not match the declared header width.",
                        state,
                    )
                yield reader.line_num, dict(zip(headers, values, strict=True))
        except csv.Error as exc:
            raise _SourceLoadFailure(
                "malformed_csv", "The local CSV source is malformed.", state
            ) from exc
        finally:
            csv.field_size_limit(previous_limit)


def _iter_jsonl_rows(
    lines: _DecodedLineStream,
    *,
    state: _ParseState,
    limits: FrontierSourceLimits,
    maximum_rows: int,
) -> Iterator[tuple[int, Mapping[str, Any]]]:
    del limits
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        state.rows_seen += 1
        if state.rows_seen > maximum_rows:
            raise _SourceLoadFailure(
                "row_limit_exceeded", "The local source exceeded its row limit.", state
            )
        try:
            value = _strict_json_loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise _SourceLoadFailure(
                "malformed_jsonl", "A JSONL record is malformed.", state
            ) from exc
        if not isinstance(value, dict):
            raise _SourceLoadFailure(
                "non_object_record", "Each JSONL record must be an object.", state
            )
        yield line_number, value


def _read_json_array(
    raw: Any,
    *,
    state: _ParseState,
    limits: FrontierSourceLimits,
    maximum_rows: int,
) -> tuple[Iterator[tuple[int, Mapping[str, Any]]], str]:
    try:
        body = raw.read(limits.max_json_array_bytes + 1)
    except OSError as exc:
        raise _SourceLoadFailure(
            "read_failed", "The local source file could not be read.", state
        ) from exc
    state.bytes_read = len(body)
    if len(body) > limits.max_json_array_bytes:
        raise _SourceLoadFailure(
            "json_array_bytes_exceeded",
            "The JSON array source exceeds its in-memory byte limit.",
            state,
        )
    if b"\x00" in body:
        raise _SourceLoadFailure(
            "malformed_text", "The local source contains a NUL byte.", state
        )
    try:
        value = _strict_json_loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _SourceLoadFailure(
            "malformed_json", "The JSON array source is malformed.", state
        ) from exc
    if not isinstance(value, list):
        raise _SourceLoadFailure(
            "non_array_json", "A .json local source must contain one bounded array.", state
        )
    if len(value) > maximum_rows:
        state.rows_seen = maximum_rows + 1
        raise _SourceLoadFailure(
            "row_limit_exceeded", "The local source exceeded its row limit.", state
        )
    rows: list[tuple[int, Mapping[str, Any]]] = []
    for position, record in enumerate(value, start=1):
        state.rows_seen += 1
        if not isinstance(record, dict):
            raise _SourceLoadFailure(
                "non_object_record", "Each JSON array record must be an object.", state
            )
        rows.append((position, record))
    return iter(rows), hashlib.sha256(body).hexdigest()


def _index_rows(
    scanner_id: str,
    filename: str,
    rows: Iterator[tuple[int, Mapping[str, Any]]],
    *,
    state: _ParseState,
    limits: FrontierSourceLimits,
) -> Mapping[str, tuple[IndexedFrontierSourceRecord, ...]]:
    indexed: dict[str, list[IndexedFrontierSourceRecord]] = defaultdict(list)
    recognized_schema = False
    for row_number, raw_record in rows:
        record = _validated_record(raw_record, limits, state)
        names, has_name_field = _record_legal_names(scanner_id, record, state)
        recognized_schema = recognized_schema or has_name_field
        if not names:
            state.rows_without_legal_name += 1
            continue
        state.rows_with_legal_name += 1
        entry = IndexedFrontierSourceRecord(
            record=_freeze_json(record),
            filename=filename,
            row_number=row_number,
        )
        for name in names:
            indexed[name].append(entry)
    if state.rows_seen and not recognized_schema:
        raise _SourceLoadFailure(
            "missing_legal_name_field",
            "The local source has no recognized legal-name field.",
            state,
        )
    return MappingProxyType({key: tuple(indexed[key]) for key in sorted(indexed)})


def _validated_record(
    raw_record: Mapping[str, Any],
    limits: FrontierSourceLimits,
    state: _ParseState,
) -> dict[str, Any]:
    if len(raw_record) > limits.max_fields_per_row:
        raise _SourceLoadFailure(
            "field_count_exceeded", "A source record has too many fields.", state
        )
    record: dict[str, Any] = {}
    total_bytes = 0
    normalized_keys: set[str] = set()
    for raw_key, value in raw_record.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise _SourceLoadFailure(
                "invalid_field_name", "Every source field must have a non-empty string name.", state
            )
        key_bytes = _utf8_bytes(raw_key, state)
        value_bytes = _json_field_bytes(value, state)
        if key_bytes > limits.max_field_bytes or value_bytes > limits.max_field_bytes:
            raise _SourceLoadFailure(
                "field_bytes_exceeded", "A source field exceeded its byte limit.", state
            )
        normalized_key = _normalize_field_name(raw_key)
        if normalized_key in normalized_keys:
            raise _SourceLoadFailure(
                "duplicate_field_name",
                "A source record has colliding normalized field names.",
                state,
            )
        normalized_keys.add(normalized_key)
        total_bytes += key_bytes + value_bytes
        if total_bytes > limits.max_record_bytes:
            raise _SourceLoadFailure(
                "record_bytes_exceeded", "A source record exceeded its byte limit.", state
            )
        record[raw_key] = value
    return record


def _validate_csv_headers(
    headers: Sequence[str],
    scanner_id: str,
    limits: FrontierSourceLimits,
    state: _ParseState,
) -> None:
    if not headers or len(headers) > limits.max_fields_per_row:
        raise _SourceLoadFailure(
            "invalid_header", "The CSV header is missing or has too many fields.", state
        )
    normalized: set[str] = set()
    for header in headers:
        if not header.strip() or len(header.encode("utf-8")) > limits.max_field_bytes:
            raise _SourceLoadFailure(
                "invalid_header", "The CSV header contains an invalid field name.", state
            )
        key = _normalize_field_name(header)
        if key in normalized:
            raise _SourceLoadFailure(
                "duplicate_field_name",
                "The CSV header has colliding normalized field names.",
                state,
            )
        normalized.add(key)
    expected = {_normalize_field_name(name) for name in _NAME_FIELDS[scanner_id]}
    if not normalized & expected:
        raise _SourceLoadFailure(
            "missing_legal_name_field",
            "The CSV source has no recognized legal-name field.",
            state,
        )


def _record_legal_names(
    scanner_id: str,
    record: Mapping[str, Any],
    state: _ParseState,
) -> tuple[tuple[str, ...], bool]:
    values = {_normalize_field_name(key): value for key, value in record.items()}
    names: set[str] = set()
    found = False
    for expected in _NAME_FIELDS[scanner_id]:
        key = _normalize_field_name(expected)
        if key not in values:
            continue
        found = True
        raw_name = values[key]
        if raw_name in (None, ""):
            continue
        if not isinstance(raw_name, str):
            raise _SourceLoadFailure(
                "invalid_legal_name",
                "A legal-name field must contain text or be empty.",
                state,
            )
        normalized = normalize_legal_name(raw_name)
        if normalized:
            names.add(normalized)
    if scanner_id != DATASF_SCANNER_ID and len(names) > 1:
        raise _SourceLoadFailure(
            "ambiguous_legal_names",
            "A source record contains conflicting legal-name fields.",
            state,
        )
    return tuple(sorted(names)), found


def _strict_json_loads(value: str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    return json.loads(
        value,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


def _json_field_bytes(value: Any, state: _ParseState) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise _SourceLoadFailure(
            "invalid_field_value", "A source field is not bounded valid JSON.", state
        ) from exc
    return len(encoded)


def _utf8_bytes(value: str, state: _ParseState) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _SourceLoadFailure(
            "invalid_encoding", "A source field is not valid UTF-8 text.", state
        ) from exc


def _assert_file_unchanged(raw: Any, initial: os.stat_result, state: _ParseState) -> None:
    try:
        final = os.fstat(raw.fileno())
    except OSError as exc:
        raise _SourceLoadFailure(
            "read_failed", "The local source file could not be verified.", state
        ) from exc
    if (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) != (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ):
        raise _SourceLoadFailure(
            "file_changed", "The local source changed while it was read.", state
        )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_field_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _file_format(filename: str) -> SourceFileFormat:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        return "csv"
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    return "json_array"


def _looks_archived(prefix: bytes) -> bool:
    signatures = (
        b"PK\x03\x04",
        b"PK\x05\x06",
        b"PK\x07\x08",
        b"\x1f\x8b",
        b"BZh",
        b"\xfd7zXZ\x00",
        b"7z\xbc\xaf\x27\x1c",
        b"Rar!\x1a\x07",
        b"\x28\xb5\x2f\xfd",
    )
    return prefix.startswith(signatures) or (
        len(prefix) >= 262 and prefix[257:262] == b"ustar"
    )


def _error(scanner_id: str, filename: str, code: str, message: str) -> FrontierSourceError:
    return FrontierSourceError(
        scanner_id=scanner_id,
        filename=filename,
        code=code,
        message=message,
    )


def _empty_stats(
    scanner_id: str,
    status: Literal["disabled", "absent"],
    configured_files: int,
) -> FrontierSourceStats:
    return FrontierSourceStats(
        scanner_id=scanner_id,
        status=status,
        configured_files=configured_files,
        discovered_files=0,
        loaded_files=0,
        bytes_read=0,
        rows_seen=0,
        rows_with_legal_name=0,
        rows_without_legal_name=0,
        rows_indexed=0,
        unique_legal_names=0,
    )


__all__ = [
    "DATASF_SCANNER_ID",
    "DEFAULT_SOURCE_FILENAMES",
    "DOL_LCA_SCANNER_ID",
    "SEC_FORM_D_SCANNER_ID",
    "SUPPORTED_SOURCE_SCANNERS",
    "FrontierSourceError",
    "FrontierSourceLimits",
    "FrontierSourceLookup",
    "FrontierSourceProvenance",
    "FrontierSourceStats",
    "IndexedFrontierSourceRecord",
    "LocalFrontierSourceCatalog",
    "load_local_frontier_sources",
    "normalize_legal_name",
]
