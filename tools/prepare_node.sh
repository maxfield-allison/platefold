#!/usr/bin/env bash
# Install the reviewed Linux build toolchain without changing the system Node.
set -euo pipefail

target=${1:?usage: prepare_node.sh OUTPUT_DIRECTORY}
test "$(uname -s)" = Linux
case "$(uname -m)" in
  x86_64)
    node_arch=x64
    node_sha256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
    ;;
  aarch64)
    node_arch=arm64
    node_sha256=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b
    ;;
  *) echo "unsupported Node build architecture" >&2; exit 1 ;;
esac

archive="${target}.tar.xz"
mkdir -p "$target"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
  --retry 2 --connect-timeout 20 --max-time 300 \
  "https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-${node_arch}.tar.xz" \
  -o "$archive"
printf '%s  %s\n' "$node_sha256" "$archive" | sha256sum -c -
tar -xJf "$archive" -C "$target" --strip-components=1
test "$("$target/bin/node" --version)" = v22.23.4
