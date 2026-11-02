#!/bin/sh
# Create a throw-away repository with a seeded bug and run cross-model-review on it.
# This calls the real Codex and Claude Code CLIs (it costs model usage).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(mktemp -d)/demo-repo
mkdir -p "$repo"
cd "$repo"
git init -q -b main

cat > chunk.py <<'EOF'
def chunk(items, size):
    """Split items into lists of length `size`."""
    return [items]
EOF
cat > test_chunk.py <<'EOF'
from chunk import chunk

assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
assert chunk([], 3) == []
print("ok")
EOF
git add .
git -c user.name=demo -c user.email=demo@example.invalid commit -qm init

# The uncommitted change under review: it drops the last chunk.
cat > chunk.py <<'EOF'
def chunk(items, size):
    """Split items into lists of length `size` (the last one may be shorter)."""
    return [items[i:i + size] for i in range(0, len(items) - size, size)]
EOF

echo "demo repository: $repo"
cross-model-review run --repo "$repo" --config "$here/demo-config.toml"
