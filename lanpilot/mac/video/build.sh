#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 OUTPUT" >&2
    exit 2
fi

sources=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
xcrun swiftc -O -swift-version 6 -warnings-as-errors \
    "$sources/AnnexB.swift" "$sources/H264Decoder.swift" "$sources/DecodeCLI.swift" -o "$1"
