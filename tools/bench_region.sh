#!/usr/bin/env bash
# Run on the host. Actual region operations execute inside hvisor at EL2.
set -euo pipefail

# Workload: create REGIONS regions, then delete all of them; repeat ROUNDS.
REGIONS=${REGIONS:-100}
REGION_PAGES=${REGION_PAGES:-1}  # 4 KiB per page
ROUNDS=${ROUNDS:-500}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
for name in REGIONS REGION_PAGES ROUNDS; do
    [[ ${!name} =~ ^[1-9][0-9]{0,4}$ ]] || { echo "Invalid $name: ${!name}" >&2; exit 1; }
done
if (( REGIONS > 4096 || ROUNDS > 10000 || REGIONS * REGION_PAGES > 32768 )); then
    echo "Require REGIONS<=4096, ROUNDS<=10000, mapped RAM<=128 MiB" >&2
    exit 1
fi
if [[ ${ARCH:-aarch64}/${BOARD:-qemu-gicv3}/${MODE:-release} != aarch64/qemu-gicv3/release ||
      ( -n ${BID:-} && ${BID} != aarch64/qemu-gicv3 ) ]]; then
    echo "bench_region.sh supports only aarch64/qemu-gicv3 release" >&2
    exit 1
fi
export ARCH=aarch64 BOARD=qemu-gicv3 MODE=release LOG=error
export MEMBENCH_REGIONS="$REGIONS" MEMBENCH_REGION_PAGES="$REGION_PAGES" MEMBENCH_ROUNDS="$ROUNDS"
mkdir -p target
OUTFILE=target/bench_region.txt
BUILD_LOG=target/bench_region.log
: > "$OUTFILE"
echo "Region benchmark: $REGIONS regions, $REGION_PAGES pages/region, $ROUNDS rounds"
echo "Building and running hvisor; full log: $BUILD_LOG"

# Reuse the existing bare-metal test runner, without downloading a guest OS.
# Detach stdin so QEMU does not access the terminal from timeout's process group.
if make --no-print-directory -j1 defconfig gen_cargo_config test-pre > "$BUILD_LOG" 2>&1 &&
   timeout --verbose --kill-after=5s 300s cargo test --features membench --release \
       --target aarch64-unknown-none -Z build-std=core,alloc \
       -Z build-std-features=compiler-builtins-mem < /dev/null >> "$BUILD_LOG" 2>&1; then
    grep -q '^=== Region Benchmark Done ===' "$BUILD_LOG" || {
        echo "Benchmark did not finish; see $BUILD_LOG" >&2; exit 1;
    }
    {
        echo "Date: $(date)"
        sed -n '/^=== Region Benchmark ===/,/^=== Region Benchmark Done ===/p' "$BUILD_LOG"
    } | tee "$OUTFILE"
    echo "Result: $OUTFILE"
else
    status=$?
    tail -n 30 "$BUILD_LOG" >&2
    echo "Region benchmark failed (exit $status); see $BUILD_LOG" >&2
    exit "$status"
fi
