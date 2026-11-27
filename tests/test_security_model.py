"""Public entry points to the adopter threat model."""

from pathlib import Path


def test_security_model_is_linked_and_explains_boundaries():
    root = Path(__file__).resolve().parents[1]
    for entry in ("README.md", "SECURITY.md"):
        assert "(docs/security-model.md)" in (root / entry).read_text()
    text = (root / "docs/security-model.md").read_text()
    for phrase in ("like running code", "write access", "GITHUB_TOKEN", "can become public",
                   "before this change still dispatch", "owner and product App", "edit history"):
        assert phrase in text
