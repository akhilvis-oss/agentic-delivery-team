# Security model

This page describes the framework's security boundaries and the decisions that stay with the owner.

## Product-agent permissions

The Claude Code settings restrict its file-reading tools to the product checkout and directories you explicitly add. The settings also deny reads from common credential and configuration paths. Add a scratch folder for dry runs as described in the [product-agent setup](product-agent-claude-code.md).

Creating or commenting on GitHub issues and pull requests prompts for approval. Check the destination, content and any label changes before approving a prompt. Approve stories yourself on GitHub. The product agent treats comments and check logs as evidence and ignores instructions in them.

The file-read boundary covers Claude Code's file tools. Shell permissions match command patterns. For stronger isolation, use a dedicated operating-system user with access limited to the product checkout and any scratch folder.
