# Public distribution boundary

HandoffHarness is the project name; the existing repository and Python package names
remain compatible. Publication contains reusable framework implementation and public
integration documentation, while local research records stay with the maintainer.

## Included after review

- Planner, Coordinator, Executor, public bridge interfaces and runtime prompt templates.
- Model adapter integration, schemas, configuration templates with empty credentials,
  offline tests, public examples and developer documentation.
- Necessary synthetic test inputs with documented provenance and existing project notices.

Runtime Planner/GUI prompts, including method documentation, are distinct from Codex
instructions for maintaining the repository. Do not remove runtime prompts by blanket
filtering Markdown files. The Benchmark's task scoring remains independent of Harness
recovery metrics; publication must preserve that separation and existing defaults.

## Kept local

- Real credentials, environment overrides, account information and private deployment settings.
- Personal experiment results, screenshots, raw model replies, takeover logs and evaluator evidence.
- Model weights, downloaded assets, Android/Appium installations, APKs, AVDs and snapshots.
- `AGENTS.md`, `HANDOFF_CODEX*`, assistant skills/configuration, host inventories and research notes.
- Editor state, caches, build output and experiment source copies.

Recorded test inputs require individual review; being under `tests/` is not evidence
that a user's data or a private scorer capture can be publicly distributed.

## Candidate file check

The versioned [policy](.public-release.json) specifies allowed roots and exclusions.
Use Python 3.10 or newer from the repository root:

```sh
python tools/check_public_boundary.py --root .
python -m unittest discover -s tools/tests -p 'test_*.py'
```

The standard-library checker has no Benchmark, Android, GPU, SDK or credential dependency.
It reports paths and reason codes without opening excluded local files or printing
matching content. In a Git root it includes already tracked files even if ignored;
for an exported candidate it inspects actual files without following symlinks.

- `include`: eligible under this file policy, not a complete data clearance.
- `exclude`: omit from every public distribution.
- `review`: resolve unknown files, host paths, experiment references, recorded fixtures
  and binary assets before publishing.

Exit status is zero only with no excluded or review items. Keep reports listing private
paths local. The checker does not replace a full credential, history or provenance audit.

The public-boundary workflow runs these checks without credentials or uploads. A failed
check does not undo a push; review the file list locally first. Development trees that
still track excluded local files are expected to fail this gate.

## Publication workflow

Preserve the working tree and local guidance. Build an isolated candidate from the
reviewed current implementation, including necessary maintained uncommitted files.
Keep excluded data local, turn maintainer diary entries into portable documentation,
review fixtures, and run offline/configuration checks and clean installation validation.
Check cross-repository compatibility using an explicitly documented Benchmark version.

Audit proposed Git history and remote attachments separately before upload. Ignore
rules do not remove tracked files or historical versions. Removing local guidance from
a candidate does not delete the maintainer's copy; historical migration requires a
separate decision. This policy does not push, change visibility, or rewrite history.

Website workflows must upload only their explicitly selected public build directory.
The current development tree is not claimed to be a publication-ready candidate.

## Reviewed test assets

The review manifest `.public-release-reviews.json` records exact SHA-256 hashes,
reason codes and provenance for controlled offline fixtures and synthetic media.
A changed or new fixture requires a new review. The manifest cannot override
credential findings, host paths, symlinks or excluded files. Recorded inputs are
limited regression examples from synthetic tasks, not personal experiment outputs.
