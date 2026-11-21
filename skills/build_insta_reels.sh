#!/usr/bin/env bash
# reels/ 의 프로그램·글꼴로 claude.ai 에 올릴 스킬 zip 을 만든다: dist/insta-reels.zip
set -euo pipefail
cd "$(dirname "$0")/.."
B=$(mktemp -d); S="$B/insta-reels"
mkdir -p "$S/scripts/fonts"
cp skills/insta-reels/SKILL.md "$S/"
cp reels/make_reel.py "$S/scripts/"
cp reels/fonts/*.otf reels/fonts/OFL.txt "$S/scripts/fonts/"
mkdir -p dist; rm -f dist/insta-reels.zip
(cd "$B" && python3 -m zipfile -c "$OLDPWD/dist/insta-reels.zip" insta-reels)
rm -rf "$B"; ls -la dist/insta-reels.zip
