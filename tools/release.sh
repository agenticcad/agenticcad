#!/usr/bin/env bash
# Cut a release from version.py: tag, source zip (git archive, tracked files only — never workspace/ or secrets),
# GitHub release with the matching CHANGELOG section as notes. Usage: tools/release.sh   (run on a clean main)
set -euo pipefail
cd "$(dirname "$0")/.."
R=agenticcad/agenticcad
V=$(python3 -c 'import version; print(version.__version__)')
TAG="v$V"; ZIP="agenticcad-$V.zip"
[ -z "$(git status --porcelain)" ] || { echo "working tree not clean"; exit 1; }
grep -q "^## \[$V\]" CHANGELOG.md || { echo "CHANGELOG.md has no [$V] section"; exit 1; }
grep -q "^version = \"$V\"" pyproject.toml || { echo "pyproject.toml version is not $V (Briefcase reads it)"; exit 1; }
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git tag -a "$TAG" -m "Release $V"
git push origin main "$TAG"
git archive --format=zip --prefix="agenticcad-$V/" -o "/tmp/$ZIP" "$TAG"
NOTES=$(awk -v v="$V" '$0 ~ "^## \\["v"\\]" {p=1; next} /^## \[/ {p=0} p' CHANGELOG.md)
printf '%s\n\n**Install**\n```\nunzip %s && cd agenticcad-%s\npython3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n.venv/bin/python server.py   # http://127.0.0.1:8765\n```\nFree for non-commercial use (PolyForm Noncommercial 1.0.0); commercial licences: agenticcad@prodevelop.com.au. Site: https://agenticcad.github.io/agenticcad/\n' "$NOTES" "$ZIP" "$V" > "/tmp/notes-$V.md"
if gh release view "$TAG" -R $R >/dev/null 2>&1; then gh release upload "$TAG" "/tmp/$ZIP" --clobber -R $R
else gh release create "$TAG" "/tmp/$ZIP" -R $R --title "AgenticCAD $V" --notes-file "/tmp/notes-$V.md" --latest; fi
echo "installers: the package workflow builds the macOS .dmg and Windows .msi and attaches them within ~20 min"
echo "released $TAG: $(gh release view "$TAG" -R $R --json url --jq .url)"
