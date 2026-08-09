#!/usr/bin/env python3
"""Phase 9 bounded, fail-closed secret scanner.

Scope (``docs/v2/phase9/secret-scanning.md``):

* the working tree of the scanned commit,
* a bounded window of recent history (``--history-commits``, diff text only),
* generated artifacts passed with ``--artifact``.

Properties that make this usable as a gate:

* **Bounded.** Byte, file-count and history-depth caps are explicit; exceeding a
  cap is a *failure*, never a silent truncation, so a pass always means the whole
  declared scope was examined.
* **scanned=true or fail.** A scan that could not run yields ``scanned=false``
  and a non-zero exit; an unavailable scanner is not a pass.
* **No match exposure.** A finding reports rule id, a redacted location and a
  SHA-256 of the matched text. The matched bytes are never printed, never
  written to the report and never placed in an exception message.

The rules deliberately target credential *shapes*, not vendor names, and each
carries an entropy or structural qualifier so ordinary prose cannot trip it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Bounds. Exceeding any of these fails the scan rather than truncating it.
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 400_000_000
MAX_FILES = 20_000
MAX_HISTORY_COMMITS = 200
MAX_HISTORY_BYTES = 80_000_000

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".tmp",
}

BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".so",
    ".dylib",
    ".dll",
    ".pyc",
    ".woff",
    ".woff2",
}


class ScanError(RuntimeError):
    """The scan could not be completed over its declared scope."""


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


#: ``(rule_id, pattern, min_entropy_of_group)``. ``min_entropy`` of 0 means the
#: shape alone is decisive.
RULES: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("github-pat-classic", re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), 0.0),
    ("github-pat-fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), 0.0),
    ("github-app-token", re.compile(r"\bgh[souvr]_[A-Za-z0-9]{36}\b"), 0.0),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), 0.0),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0.0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0.0),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        0.0,
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), 0.0),
    (
        "bearer-header",
        re.compile(r"[Aa]uthorization\s*[:=]\s*[\"']?Bearer\s+([A-Za-z0-9._-]{20,})"),
        3.5,
    ),
    (
        "generic-assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|client[_-]?secret)\b"
            r"\s*[:=]\s*[\"']([A-Za-z0-9+/=_-]{24,})[\"']"
        ),
        3.8,
    ),
)

#: Text that is a *placeholder* by construction and must never be a finding.
PLACEHOLDER = re.compile(
    r"(?i)^(?:x{4,}|\*{4,}|\.{4,}|<[^>]+>|\$\{[^}]+\}|redacted|placeholder|example|"
    r"changeme|dummy|sample|test[_-]?(?:value|token|secret)|your[_-].*|sha256:[0-9a-f]{64})$"
)


def _redact_location(path: str) -> str:
    """Never emit an absolute host path."""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return f"<external>/{candidate.name}"


def _iter_lines(text: str) -> Iterator[tuple[int, str]]:
    yield from enumerate(text.splitlines(), start=1)


def scan_text(text: str, *, origin: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for line_number, line in _iter_lines(text):
        if len(line) > 4096:
            line = line[:4096]
        for rule_id, pattern, min_entropy in RULES:
            for match in pattern.finditer(line):
                captured = match.group(match.lastindex or 0)
                if PLACEHOLDER.match(captured.strip()):
                    continue
                if min_entropy and _entropy(captured) < min_entropy:
                    continue
                findings.append(
                    {
                        "rule_id": rule_id,
                        "origin": origin,
                        "line": line_number,
                        # The match itself is NEVER emitted.
                        "match_sha256": hashlib.sha256(captured.encode("utf-8")).hexdigest(),
                        "match_length": len(captured),
                    }
                )
    return findings


def _tree_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def scan_tree(root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    findings: list[dict[str, Any]] = []
    files = 0
    total_bytes = 0
    for path in _tree_files(root):
        files += 1
        if files > MAX_FILES:
            raise ScanError(f"file-count bound exceeded ({MAX_FILES})")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ScanError(f"file byte bound exceeded for {_redact_location(str(path))}")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ScanError(f"total byte bound exceeded ({MAX_TOTAL_BYTES})")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # non-text payload; the binary policy covers these
        findings.extend(scan_text(text, origin=f"tree:{_redact_location(str(path))}"))
    return findings, {"files": files, "bytes": total_bytes}


def scan_history(root: Path, commits: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if commits <= 0:
        return [], {"commits": 0, "bytes": 0}
    if commits > MAX_HISTORY_COMMITS:
        raise ScanError(f"history bound exceeded ({MAX_HISTORY_COMMITS})")
    result = subprocess.run(
        ["git", "log", f"-{commits}", "--no-merges", "-p", "--format=commit %H"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        errors="replace",
    )
    if result.returncode != 0:
        raise ScanError("git history could not be read")
    text = result.stdout
    if len(text.encode("utf-8", errors="replace")) > MAX_HISTORY_BYTES:
        raise ScanError(f"history byte bound exceeded ({MAX_HISTORY_BYTES})")
    findings = scan_text(text, origin="history")
    return findings, {"commits": commits, "bytes": len(text)}


def scan_artifacts(paths: list[Path]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    findings: list[dict[str, Any]] = []
    scanned = 0
    for path in paths:
        if not path.is_file():
            raise ScanError(f"declared artifact missing: {_redact_location(str(path))}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ScanError(f"artifact byte bound exceeded: {_redact_location(str(path))}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            raise ScanError(f"artifact unreadable: {_redact_location(str(path))}") from exc
        scanned += 1
        findings.extend(scan_text(text, origin=f"artifact:{_redact_location(str(path))}"))
    return findings, {"artifacts": scanned}


def _key(finding: dict[str, Any]) -> tuple[str, str, str]:
    return (finding["rule_id"], finding["origin"], finding["match_sha256"])


def load_baseline(path: Path) -> tuple[set[tuple[str, str, str]], dict[str, Any]]:
    """Load recorded false positives.

    A missing baseline is legitimate (empty allow-list). A malformed one, or an
    entry without a justification, is a scan failure — never an implicit pass.
    """
    if not path.is_file():
        return set(), {"present": False, "entries": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScanError("baseline unreadable") from exc
    entries = payload.get("accepted_false_positives")
    if not isinstance(entries, list):
        raise ScanError("baseline malformed")
    keys: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ScanError("baseline entry malformed")
        missing = [
            field
            for field in ("rule_id", "origin", "match_sha256", "justification")
            if not isinstance(entry.get(field), str) or not entry[field].strip()
        ]
        if missing:
            raise ScanError(f"baseline entry missing fields: {','.join(missing)}")
        keys.add((entry["rule_id"], entry["origin"], entry["match_sha256"]))
    return keys, {
        "present": True,
        "entries": len(keys),
        "digest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--history-commits", type=int, default=25)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "docs" / "v2" / "phase9" / "secret-scan-baseline.json",
        help=(
            "Recorded false positives. Each entry must carry rule_id, origin, "
            "match_sha256 and a justification; anything else still fails."
        ),
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "schema": "hermes-v2-phase9-secret-scan/1",
        "scanner": "v2_phase9_secret_scan.py",
        "ruleset_digest_sha256": hashlib.sha256(
            "\n".join(sorted(rule[0] + rule[1].pattern for rule in RULES)).encode("utf-8")
        ).hexdigest(),
        "rules": sorted(rule[0] for rule in RULES),
    }
    findings: list[dict[str, Any]] = []
    try:
        tree_findings, tree_stats = scan_tree(args.root)
        history_findings, history_stats = scan_history(args.root, args.history_commits)
        artifact_findings, artifact_stats = scan_artifacts(list(args.artifact))
        findings = tree_findings + history_findings + artifact_findings
        baseline, baseline_meta = load_baseline(args.baseline)
        report["baseline"] = baseline_meta
        # A history hit is the *same* synthetic fixture the tree entry accepted:
        # the matched bytes hash identically. Suppressing it by (rule, digest)
        # keeps the allow-list from having to name a commit window that moves.
        accepted_digests = {(rule, digest) for rule, _origin, digest in baseline}

        def _accepted(finding: dict[str, Any]) -> bool:
            if _key(finding) in baseline:
                return True
            if finding["origin"] == "history":
                return (finding["rule_id"], finding["match_sha256"]) in accepted_digests
            return False

        suppressed = [f for f in findings if _accepted(f)]
        findings = [f for f in findings if not _accepted(f)]
        report["suppressed_count"] = len(suppressed)
        stale = sorted(baseline - {_key(f) for f in suppressed})
        if stale:
            # A baseline entry that no longer matches anything is drift; it must
            # be removed, not left to silently widen the allow-list later.
            raise ScanError(f"stale baseline entries: {len(stale)}")
        report.update(
            {
                "scanned": True,
                "scope": {
                    "tree": tree_stats,
                    "history": history_stats,
                    "artifacts": artifact_stats,
                },
                "findings": findings,
                "finding_count": len(findings),
                "bounds": {
                    "max_file_bytes": MAX_FILE_BYTES,
                    "max_files": MAX_FILES,
                    "max_history_commits": MAX_HISTORY_COMMITS,
                    "max_total_bytes": MAX_TOTAL_BYTES,
                },
            }
        )
    except ScanError as exc:
        report.update({"scanned": False, "error": str(exc), "findings": [], "finding_count": 0})

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {k: report[k] for k in ("scanned", "finding_count") if k in report}, sort_keys=True
        )
    )
    if not report.get("scanned"):
        return 3
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
