#!/usr/bin/env bash
# check-ascii.sh - fail if any source file contains non-ASCII bytes.
# Catches "hidden characters": box-drawing, smart quotes, em-dashes, and the
# dangerous class (zero-width joiners, bidirectional overrides / Trojan-Source).
# Wire into CI or a pre-commit hook so source stays ASCII-only.
set -euo pipefail
status=0
while IFS= read -r -d '' f; do
    if LC_ALL=C grep -nP '[^\x00-\x7f]' "$f" >/dev/null 2>&1; then
        echo "non-ASCII in $f:"
        LC_ALL=C grep -nP '[^\x00-\x7f]' "$f" | sed 's/^/  /'
        status=1
    fi
done < <(find . -type f \( -name '*.py' -o -name '*.sh' \) -not -path './.git/*' -print0)
[ "$status" -eq 0 ] && echo "ASCII check: clean"
exit "$status"
