#!/bin/bash
set -Eeuo pipefail

# Build one native or integrated hvisor variant for the Redis experiment.
# The source checkout is left untouched; a persistent detached worktree is
# created with the selected hvisor commit and the committed Redis platform.

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$SCRIPT_DIR/../.." && pwd)
REMOTE=${REDIS_HVISOR_REMOTE:-https://github.com/liujun5817/hvisor}
SOURCE_ROOT=${REDIS_HVISOR_SOURCE_ROOT:-/tmp}
PER_CPU_SIZE=${REDIS_PER_CPU_SIZE:-1MiB}
PLATFORM_COMMIT=
WORKTREE_ROOT=${REDIS_HVISOR_WORKTREE_ROOT:-/tmp/redis-hvisor-worktrees}
OUTPUT_ROOT=$REPO/target/redis-replica-bench/hvisors

usage() {
    cat <<'EOF'
usage: build-hvisor.sh --variant native|integrated --commit COMMIT [options]

  --variant NAME               native or integrated (required)
  --commit COMMIT              hvisor commit to build (required)
  --source-dir DIR             existing source clone; clone if omitted
  --remote URL                 clone source from this remote
  --platform-commit COMMIT     Redis platform commit (default: this HEAD)
  --per-cpu-size SIZE          common per-CPU size, e.g. 512KiB or 1MiB
  --worktree-root DIR          persistent detached worktree directory
  --output-root DIR            output root for binaries and manifests
  --refresh                    replace an existing worktree for this variant
  -h, --help                   show this help
EOF
}

variant=
commit=
source_dir=
refresh=0
while (($#)); do
    case $1 in
        --variant) variant=$2; shift 2 ;;
        --commit) commit=$2; shift 2 ;;
        --source-dir) source_dir=$2; shift 2 ;;
        --remote) REMOTE=$2; shift 2 ;;
        --platform-commit) PLATFORM_COMMIT=$2; shift 2 ;;
        --per-cpu-size) PER_CPU_SIZE=$2; shift 2 ;;
        --worktree-root) WORKTREE_ROOT=$2; shift 2 ;;
        --output-root) OUTPUT_ROOT=$2; shift 2 ;;
        --refresh) refresh=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ $variant == native || $variant == integrated ]] || { echo '--variant must be native or integrated' >&2; exit 2; }
[[ -n $commit ]] || { echo '--commit is required' >&2; exit 2; }

parse_size() {
    local value=$1 number suffix multiplier
    [[ $value =~ ^([0-9]+)([KMG]i?B?|[kmg])?$ ]] || { echo "invalid per-CPU size: $value" >&2; exit 2; }
    number=${BASH_REMATCH[1]}; suffix=${BASH_REMATCH[2]:-}
    case ${suffix,,} in
        k|kb|ki|kib) multiplier=1024 ;;
        m|mb|mi|mib) multiplier=$((1024 * 1024)) ;;
        g|gb|gi|gib) multiplier=$((1024 * 1024 * 1024)) ;;
        '') multiplier=1 ;;
        *) echo "invalid per-CPU size suffix: $suffix" >&2; exit 2 ;;
    esac
    echo $((number * multiplier))
}

per_cpu_bytes=$(parse_size "$PER_CPU_SIZE")
((per_cpu_bytes > 0)) || { echo 'per-CPU size must be positive' >&2; exit 2; }
for command in cp git grep ln make realpath rm sed sha256sum tar; do
    command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

if [[ -z $source_dir ]]; then
    for candidate in "$SOURCE_ROOT/hvisor-$variant" "$REPO/../tmp/hvisor-$variant"; do
        if [[ -d "$candidate/.git" || -f "$candidate/.git" ]]; then source_dir=$candidate; break; fi
    done
fi
source_dir=${source_dir:-$SOURCE_ROOT/redis-hvisor-$variant}
if [[ ! -e $source_dir ]]; then
    mkdir -p "$(dirname "$source_dir")"
    echo "cloning $REMOTE to $source_dir"
    git clone "$REMOTE" "$source_dir"
fi
[[ -d "$source_dir/.git" || -f "$source_dir/.git" ]] || { echo "not a git checkout: $source_dir" >&2; exit 1; }
source_dir=$(realpath "$source_dir")
git -C "$source_dir" cat-file -e "$commit^{commit}" || { echo "commit unavailable: $commit" >&2; exit 1; }

PLATFORM_COMMIT=${PLATFORM_COMMIT:-$(git -C "$REPO" rev-parse HEAD)}
git -C "$REPO" cat-file -e "$PLATFORM_COMMIT^{commit}" || { echo "platform commit unavailable: $PLATFORM_COMMIT" >&2; exit 1; }
git -C "$REPO" ls-tree -d --name-only "$PLATFORM_COMMIT" platform/aarch64/qemu-gicv3-redis | grep -qx platform/aarch64/qemu-gicv3-redis || {
    echo "platform commit does not contain qemu-gicv3-redis: $PLATFORM_COMMIT" >&2; exit 1;
}

WORKTREE_ROOT=$(realpath -m "$WORKTREE_ROOT")
OUTPUT_ROOT=$(realpath -m "$OUTPUT_ROOT")
worktree=$WORKTREE_ROOT/$variant
output=$OUTPUT_ROOT/$variant
mkdir -p "$WORKTREE_ROOT" "$OUTPUT_ROOT"
if [[ -e $worktree ]]; then
    if ((refresh)); then
        git -C "$source_dir" worktree remove --force "$worktree" >/dev/null 2>&1 || rm -rf "$worktree"
    else
        [[ $(git -C "$worktree" rev-parse HEAD) == "$commit" ]] || {
            echo "$worktree exists at another commit; use --refresh" >&2; exit 1;
        }
    fi
fi
if [[ ! -e $worktree ]]; then
    git -C "$source_dir" worktree add --detach "$worktree" "$commit" >/dev/null
fi

rm -rf "$worktree/platform/aarch64/qemu-gicv3-redis"
git -C "$REPO" archive "$PLATFORM_COMMIT" platform/aarch64/qemu-gicv3-redis | tar -x -C "$worktree"
git -C "$worktree" add platform/aarch64/qemu-gicv3-redis
git -C "$worktree" -c user.name='Redis benchmark builder' -c user.email='redis-benchmark@localhost' \
    commit --quiet -m "redis: add platform $PLATFORM_COMMIT"

# Reuse the repository's already-installed Kconfig virtualenv when available.
# This keeps a clean detached worktree buildable without downloading packages.
if [[ -x "$REPO/tools/kconfig/.venv/bin/python" && ! -e "$worktree/tools/kconfig/.venv" ]]; then
    ln -s "$REPO/tools/kconfig/.venv" "$worktree/tools/kconfig/.venv"
    printf 'tools/kconfig/.venv\n' >>"$(git -C "$worktree" rev-parse --git-path info/exclude)"
fi

const_file=$worktree/src/consts.rs
[[ $(grep -c '^pub const PER_CPU_SIZE: usize =' "$const_file") == 1 ]] || { echo "cannot find PER_CPU_SIZE in $const_file" >&2; exit 1; }
sed -i -E "s/^pub const PER_CPU_SIZE: usize = .*/pub const PER_CPU_SIZE: usize = ${per_cpu_bytes}; \/\/ configured by build-hvisor.sh/" "$const_file"
git -C "$worktree" add src/consts.rs
git -C "$worktree" -c user.name='Redis benchmark builder' -c user.email='redis-benchmark@localhost' \
    commit --quiet -m "redis: set per-CPU size ${per_cpu_bytes}"
printf 'hvisor_commit=%s\nplatform_commit=%s\nper_cpu_size_bytes=%s\n' \
    "$commit" "$PLATFORM_COMMIT" "$per_cpu_bytes" >"$worktree/.redis-build-info"
printf '.redis-build-info\n' >>"$(git -C "$worktree" rev-parse --git-path info/exclude)"

make -C "$worktree" BID=aarch64/qemu-gicv3-redis LOG=info all
binary=$worktree/target/aarch64-unknown-none/release/hvisor.bin
elf=$worktree/target/aarch64-unknown-none/release/hvisor
[[ -f $binary && -f $elf ]] || { echo "build output is missing under $worktree/target" >&2; exit 1; }
rm -rf "$output"; mkdir -p "$output"
cp "$binary" "$output/hvisor.bin"
cp "$elf" "$output/hvisor"
sha256sum "$output/hvisor.bin" >"$output/hvisor.bin.sha256"
printf 'variant=%s\nhvisor_commit=%s\nplatform_commit=%s\nsource=%s\nworktree=%s\nper_cpu_size_bytes=%s\n' \
    "$variant" "$commit" "$PLATFORM_COMMIT" "$source_dir" "$worktree" "$per_cpu_bytes" >"$output/build-info.txt"
printf 'built %s\ncommit=%s\nplatform=%s\nper_cpu_size_bytes=%s\nworktree=%s\nbinary=%s\n' \
    "$variant" "$commit" "$PLATFORM_COMMIT" "$per_cpu_bytes" "$worktree" "$output/hvisor.bin"
