# Scientist.com deploy copy

This repo is Scientist.com's private deploy copy of
[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp). It left
GitHub's fork network so it can use the org's private build workflow.

- **`sci-prod`** is the deploy line: the fork line from `123andy/google_workspace_mcp` (`andy-prod`),
  reset onto each new upstream base, plus the two `ci(scientist)` commits re-applied last.
- **Ours:** this folder (`Dockerfile`, `requirements.txt`) and `.github/workflows/scientist-image.yaml`.
  Everything else is upstream's and stays as upstream has it; its publishing workflows are disabled
  in repo settings, not deleted.
- **Releases:** a `sci-v<base>-<n>` tag on `sci-prod` builds and pushes ECR
  `google-workspace-mcp-server:<base>-<n>`; deploying is a k3-applications PR.
- **Upstream contributions** go through `123andy/google_workspace_mcp`, never from here.

Procedure: mcp-gateway `docs/releasing-and-gitops.md`.
