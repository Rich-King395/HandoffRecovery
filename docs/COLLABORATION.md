# Collaborator setup

**English** | [简体中文](COLLABORATION.zh-CN.md) · [Home](../README.md)

1. Follow the paired [Benchmark collaborator guide](https://github.com/Rich-King395/ResumeBench/blob/main/resumebench/docs/guides/COLLABORATION.md) to clone both repositories, install dependencies, prepare Android and configure a GUI model.
2. Use matching `collab-20261025` tags for this source snapshot, or matching reviewed `main` revisions for development.
3. Install this repository into the Benchmark controller environment and create your own `.env` from `.env.example`.
4. Follow [USER_GUIDE.md](USER_GUIDE.md) for Planner configuration and one recovery trial. Benchmark `--tasks 2`, recovery batch `--tasks 2001`, and recovery smoke `--instances 2` select the same Clock instance through different ID spaces.
5. Confirm a real VALID takeover, Planner invocation, GUI recovery actions and independent terminal scoring before scaling up.

The starting comparison profile uses a shared 30-decision budget, isolated history, native completion, condition updates disabled, `model_claim` condition authority, no host completion check, and disabled constraints/uncertain injection. These are explicit experiment settings, not replacements for historical defaults. Preserve the GUI agent's native protocol, endpoint configuration and generation settings across the baseline/recovery pair.

Each parallel job needs a dedicated emulator, Appium/systemPort, baseline, output directory and independently allocated local model endpoint. Planner calls may share a provider quota; configure adequate capacity and inspect API errors separately from task outcomes. Do not hide retries or refill GUI budgets.

Run the unit and integration commands in the user guide before device tests. Develop on a branch and submit a pull request. Keep credentials, experiment outputs, snapshots, weights and assistant instructions local. Collaborators create and verify their own baselines; maintainer snapshots and experiment results are not distributed.
