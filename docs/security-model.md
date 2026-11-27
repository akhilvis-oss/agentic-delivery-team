# Approving stories safely

Approving a story is like running code. Its text becomes instructions for a
coding agent, and required tests execute code the agent writes. Read the whole
story before you approve it.

Only the configured owner and product App can author approvable stories. Only
the owner can approve them. To use an idea from someone else, have the owner or
product agent write a new story. GitHub lets an issue's author and collaborators
with write access edit it. A write collaborator can change a trusted author's
story, repository code, and workflows. Give write access only to people you
trust with those changes.

The guard checks the author GitHub reports and records a hash of the approved
body. Edits to that body need fresh approval. The kit does not check edit history.
Stories approved before this change still dispatch with their existing approval.

HTML comment openers and link-reference definition lines are refused wherever
they appear, including inside code. Use inline links and write `&lt;!--` to
mention an HTML comment. The guard also refuses Unicode tags, bidirectional
overrides and isolates, and consecutive invisible characters of certain kinds.
Individual joiners, direction marks, soft hyphens and variation selectors used
in ordinary text are allowed. The rejection identifies the construct or code
point and the line to fix before approving again.

If you reapprove an eligible story while the guard is still processing a
rejection, the story may stop again. Wait for the rejection to finish, then
approve once more.

No other workflow may post comments with the repository's `GITHUB_TOKEN`. The
lifecycle uses that workflow identity to recognize approval and guard records.
A second workflow using the same identity could publish records the lifecycle
would trust.

On a public repository, anything an agent can read can become public through
code, comments, or evidence. Keep private data and credentials outside the
agent's reach.
