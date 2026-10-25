"""Classify a proposed public tree without opening excluded local files.

Uses tracked plus non-ignored files for a Git root, or a clean exported tree.
Reports paths and reason codes only. This is not a complete secret/history audit.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

POLICY_NAME = ".public-release.json"
HOST_PATH = re.compile(r"/(?:home|Users|root|mnt|Volumes)/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\")
LOCAL_REFERENCE = re.compile(r"\.runtime/maintenance/|\.handoffrecovery-backups|(?:artifacts|results|LOGFINAL)/(?:resumebench-|formal-|smoke-|commercial-|full-qwen)")
TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b")
KEY_ASSIGNMENT = re.compile(r"\b[A-Za-z_]*(?:API_KEY|ACCESS_TOKEN|SECRET_KEY)[\"']?\s*[:=]\s*([\"'])([^\"'\r\n]+)\1", re.I)
ENV_KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?[A-Za-z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[ \t]*=[ \t]*([^\r\n]*?)[ \t]*$", re.M)


def placeholder(value):
    """Only recognizable dummy values, never a blanket exemption for tests."""
    value = value.strip().strip("\"'")
    if value in {"synthetic-secret", "synthetic-local-key", "synthetic-credential", "private-test-key", "SECRET_TEST_TOKEN"}:
        return True
    if not value or value.upper() in {"EMPTY", "NONE", "NULL", "CHANGEME", "REPLACE_ME"}:
        return True
    if re.fullmatch(r"\$\{?[A-Za-z_]\w*\}?|<[^<>]+>", value):
        return True
    if re.fullmatch(r"(?:your[-_](?:api[-_])?key(?:[-_]here)?|x{3,}|\.{3,})", value, re.I):
        return True
    return bool(re.fullmatch(
        r"(?:sk-)?(?:test|unit-test|mock|fake|dummy|example|offline|changed|file)"
        r"(?:[-_](?:key|secret|token|wire|only|not|real|a|fixture|test))+", value, re.I))


def private_reason(name, policy):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or ".." in parts:
        return "invalid_relative_path"
    if any(part in policy["private_directory_names"] for part in parts):
        return "local_directory"
    if parts[0] in policy["private_roots"]:
        return "local_root"
    if any(part in {"artifacts", "results", "LOGFINAL", "local-models", "weights", "checkpoints"} for part in parts):
        return "local_directory"
    if any(part in policy["private_names"] or part.startswith("HANDOFF_CODEX") for part in parts):
        return "assistant_or_host_guidance"
    if any(part == ".env" or part.startswith(".env.") for part in parts) and name != ".env.example":
        return "private_environment"
    if any(part.endswith((".bak", ".orig", ".rej")) or ".bak." in part for part in parts):
        return "local_backup"
    if Path(name).suffix.lower() in policy["private_extensions"]:
        return "runtime_or_sensitive_file_type"
    if name.startswith("configs/local/") or name.endswith(".local.json"):
        return "local_configuration"
    return None


def candidates(root, policy):
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           text=True, capture_output=True)
    if probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == root:
        value = subprocess.check_output(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                         "--exclude-standard", "-z"])
        return "git", sorted({os.fsdecode(n) for n in value.split(b"\0") if n})
    names = []
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in list(subdirs):
            p = Path(directory) / name
            relative = p.relative_to(root).as_posix()
            if name == ".git":
                subdirs.remove(name)
            elif p.is_symlink() or private_reason(relative, policy):
                names.append(relative)
                subdirs.remove(name)
        names.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
    return "export", sorted(set(names))


def audit(root):
    root = Path(root).resolve()
    policy_path = root / POLICY_NAME
    if policy_path.is_symlink():
        raise ValueError("Policy must be a regular local file")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy["version"] != 1:
        raise ValueError("Unsupported public-boundary policy")
    mode, names = candidates(root, policy)
    rows = []
    for name in names:
        p = root / name
        if not p.exists() and not p.is_symlink():
            continue  # A tracked deletion is not part of the proposed working tree.
        reason = private_reason(name, policy)
        if reason:
            rows.append({"path": name, "decision": "exclude", "reasons": [reason]})
            continue  # Never inspect credentials, outputs, weights, or guidance contents.
        if p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent != root and root in parent.parents):
            rows.append({"path": name, "decision": "exclude", "reasons": ["symlink"]})
            continue
        if name not in policy["public_files"] and Path(name).parts[0] not in policy["public_directories"]:
            rows.append({"path": name, "decision": "review", "reasons": ["not_allowlisted"]})
            continue
        reasons = []
        if not p.is_file() or p.stat().st_size > policy["max_text_scan_bytes"]:
            reasons.append("asset_or_large_file_review")
        else:
            data = p.read_bytes()
            try:
                text = data.decode("utf-8") if b"\0" not in data else None
            except UnicodeDecodeError:
                text = None
            if text is None:
                reasons.append("binary_asset_review")
            else:
                if HOST_PATH.search(text):
                    reasons.append("machine_path_review")
                if p.suffix.lower() == ".md" and LOCAL_REFERENCE.search(text):
                    reasons.append("maintainer_record_review")
                literals = [m.group(2) for m in KEY_ASSIGNMENT.finditer(text)]
                if any(not placeholder(m.group(0)) for m in TOKEN.finditer(text)) or any(not placeholder(v) and re.fullmatch(r"[A-Za-z0-9+/=_-]{16,}", v) for v in literals):
                    reasons.append("credential_pattern_review")
                if name == ".env.example" and any(not placeholder(m.group(1)) for m in ENV_KEY.finditer(text)):
                    reasons.append("nonplaceholder_template_credential")
        if "fixtures" in Path(name).parts or name.startswith("tests/support/"):
            reasons.append("fixture_provenance_review")
        rows.append({"path": name, "decision": "review" if reasons else "include", "reasons": reasons})
    review_path = root / ".public-release-reviews.json"
    if review_path.exists():
        if review_path.is_symlink():
            raise ValueError("Review manifest must be a regular local file")
        reviews = json.loads(review_path.read_text(encoding="utf-8"))
        if reviews.get("version") != 1:
            raise ValueError("Unsupported review manifest")
        allowed_reasons = {"fixture_provenance_review", "binary_asset_review",
                           "asset_or_large_file_review", "maintainer_record_review"}
        for row in rows:
            approval = reviews.get("files", {}).get(row["path"], {})
            if (row["decision"] == "review" and set(row["reasons"]) <= allowed_reasons
                    and set(row["reasons"]) <= set(approval.get("reasons", []))
                    and approval.get("provenance")
                    and hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == approval.get("sha256")):
                row.update(decision="include", reviewed_reasons=row["reasons"], reasons=[])
    counts = Counter(row["decision"] for row in rows)
    return {"policy_version": 1, "source": mode, "ready": not counts["exclude"] and not counts["review"],
            "counts": {key: counts[key] for key in ("include", "exclude", "review")},
            "files": rows,
            "scope": "file_boundary_only_not_history_or_full_secret_clearance"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
