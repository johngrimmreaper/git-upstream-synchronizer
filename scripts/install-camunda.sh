#!/usr/bin/env bash
# Install the pinned official Linux x86_64 distribution into an ignored directory.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime_dir="$project_dir/infra/camunda"
version=8.9.26
archive="camunda8-run-$version-linux-x86_64.tar.gz"
checksum=5e1759af7b220fac5977a14ae1b2d2572c559184013ea76194f139ae87c80bab
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'This demo installer requires Linux x86_64.' >&2; exit 1; }
if [[ -x "$runtime_dir/c8run-$version/c8run" ]]; then
    echo "Already installed: $runtime_dir/c8run-$version"
    exit 0
fi
mkdir -p "$runtime_dir"
free_kb=$(df --output=avail "$runtime_dir" | tail -1)
(( free_kb >= 5000000 )) || { echo 'At least 5 GB free disk space is required.' >&2; exit 1; }
staging_dir=$(mktemp -d "$runtime_dir/install.XXXXXX")
trap 'rm -rf -- "$staging_dir"' EXIT
curl --fail --location --retry 3 --output "$staging_dir/$archive" \
    "https://github.com/camunda/camunda/releases/download/$version/$archive"
(cd "$staging_dir" && echo "$checksum  $archive" | sha256sum --check)
tar --extract --gzip --no-same-owner --file "$staging_dir/$archive" --directory "$staging_dir"
[[ ! -e "$runtime_dir/c8run-$version" ]] || { echo 'Refusing to overwrite an existing installation.' >&2; exit 1; }
mv -- "$staging_dir/c8run-$version" "$runtime_dir/"
echo "Installed: $runtime_dir/c8run-$version"
