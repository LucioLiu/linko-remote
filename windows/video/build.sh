#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: CXX=x86_64-w64-mingw32-clang++ $0 /path/to/lanpilot-avc.exe" >&2
    exit 1
fi
source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
"${CXX:-x86_64-w64-mingw32-clang++}" -std=c++17 -O2 -Wall -Wextra -Werror -static \
    "$source_dir/sender.cpp" -o "$1" -lmfplat -lmfuuid -lole32 -luuid -lgdi32 -luser32
