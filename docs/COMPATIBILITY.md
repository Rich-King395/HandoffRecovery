# Source compatibility

This Harness source snapshot is paired with HandoffBench commit
[`59da2eca8b11c920465d0af24d61988b7122967a`](https://github.com/Rich-King395/ResumeBench/commit/59da2eca8b11c920465d0af24d61988b7122967a).
Both repositories carry the tag `collab-20261025`. Use that pair for reproducible
source selection; record newer paired commits explicitly when developing.

The pair registers 200 active Benchmark instances and the current native GUI
adapters. Installation and configuration are in [collaborator setup](COLLABORATION.md)
and [使用指南](COLLABORATION.zh-CN.md). The Benchmark's
[known validation issues](https://github.com/Rich-King395/ResumeBench/blob/59da2eca8b11c920465d0af24d61988b7122967a/resumebench/docs/guides/KNOWN_ISSUES.md)
remain separate from Harness unit/integration acceptance. Model weights, app
packages and device baselines must be prepared and smoke-tested on your server.
