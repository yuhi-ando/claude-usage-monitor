#!/bin/sh
# Claude トークンモニターを起動してブラウザを開く
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。ターミナルで 'xcode-select --install' を実行してください。" >&2
  exit 1
fi

exec python3 server.py --port "${PORT:-18931}" --open
