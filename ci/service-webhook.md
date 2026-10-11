# Connecting a service repository

A CI job in the service repository is **not needed**: the engine clones the repository with a token and compares two revisions itself.

1. In the GitLab group (or in every project) add a webhook:
   * URL: `https://n8n.example.com/webhook/docsync/gitlab`
   * Trigger: **Push events**, branch `main` (branch filter in the webhook settings)
   * Secret token: the value of `GITLAB_WEBHOOK_SECRET`
2. Put `docsync.yaml` in the repository root (see `docs/04-model.md`): the service name in the model and target aliases.
3. For MRs that must not change documentation, add the `no-docs` label.
