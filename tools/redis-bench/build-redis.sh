#!/usr/bin/env bash
# Build pinned Redis binaries without installing packages or changing the host.
# Usage: build-redis.sh [BUILD_DIR] [--memtier]
set -euo pipefail
bench_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
bench_build=${1:-"$bench_repo/target/redis-bench/redis-build"}
mkdir -p "$bench_build"
bench_build=$(cd -- "$bench_build" && pwd)
bench_jobs=${BENCH_BUILD_JOBS:-8}
mkdir -p "$bench_build"/{downloads,logs,bin/native,bin/aarch64,cmake/Modules}

fetch() {
    local name=$1 url=$2 hash=$3 file="$bench_build/downloads/$1"
    if [[ ! -f "$file" ]]; then
        curl --fail --location --retry 2 --max-time 180 "$url" --output "$file"
    fi
    printf '%s  %s\n' "$hash" "$file" | sha256sum --check --status
}

extract() {
    local archive=$1 destination=$2
    if [[ ! -d "$bench_build/$destination" ]]; then
        mkdir -p "$bench_build/$destination"
        tar -xf "$bench_build/downloads/$archive" --strip-components=1 -C "$bench_build/$destination"
    fi
}

fetch redis-7.2.5.tar.gz https://download.redis.io/releases/redis-7.2.5.tar.gz \
    5981179706f8391f03be91d951acafaeda91af7fac56beffb2701963103e423d
fetch musl-1.2.5.tar.gz https://musl.libc.org/releases/musl-1.2.5.tar.gz \
    a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4
fetch compiler-rt-14.0.6.src.tar.xz \
    https://github.com/llvm/llvm-project/releases/download/llvmorg-14.0.6/compiler-rt-14.0.6.src.tar.xz \
    88df303840ca8fbff944e15e61c141226fe79f5d2b8e89fb024264d77841a02e
for bench_spec in \
    ExtendPath:37c372634b9797e8986e36ad6f2b4ee5fbb7ca85f7d210bcea8c432515f94061 \
    SetPlatformToolchainTools:a1e12387e6b1a8a0113e8d4ac4d981935105562c8f9655b331364e0d2cfdd1a3 \
    HandleCompilerRT:624227b8653743723f8e1213fa37ff0d25dd1c495add6f751da770fdd44eff11; do
    bench_module=${bench_spec%%:*}
    fetch "$bench_module.cmake" \
        "https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-14.0.6/cmake/Modules/$bench_module.cmake" \
        "${bench_spec#*:}"
    cp "$bench_build/downloads/$bench_module.cmake" "$bench_build/cmake/Modules/"
done
extract musl-1.2.5.tar.gz musl-src
extract compiler-rt-14.0.6.src.tar.xz compiler-rt-src
extract redis-7.2.5.tar.gz redis-arm
extract redis-7.2.5.tar.gz redis-native

(
    cd "$bench_build/musl-src"
    ./configure --target=aarch64-linux-musl --prefix="$bench_build/musl-arm" --disable-shared \
        CC='clang --target=aarch64-linux-musl -fuse-ld=lld -mno-outline-atomics' \
        AR=llvm-ar RANLIB=llvm-ranlib > "$bench_build/logs/musl-configure.log" 2>&1
    make -j"$bench_jobs" > "$bench_build/logs/musl-build.log" 2>&1
    make install > "$bench_build/logs/musl-install.log" 2>&1
)
cmake -S "$bench_build/compiler-rt-src/lib/builtins" -B "$bench_build/compiler-rt-arm" -G Ninja \
    -DCMAKE_C_COMPILER=clang -DCMAKE_ASM_COMPILER=clang \
    -DCMAKE_C_COMPILER_TARGET=aarch64-linux-musl -DCMAKE_ASM_COMPILER_TARGET=aarch64-linux-musl \
    -DCMAKE_SYSROOT="$bench_build/musl-arm" \
    -DCMAKE_C_FLAGS='-mno-outline-atomics -fno-stack-protector' \
    -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCMAKE_AR="$(command -v llvm-ar)" -DCMAKE_RANLIB="$(command -v llvm-ranlib)" \
    -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON -DCOMPILER_RT_INCLUDE_TESTS=OFF \
    -DLLVM_CONFIG_PATH="$(command -v llvm-config-14)" \
    > "$bench_build/logs/compiler-rt-configure.log" 2>&1
cmake --build "$bench_build/compiler-rt-arm" -j"$bench_jobs" > "$bench_build/logs/compiler-rt-build.log" 2>&1

cat > "$bench_build/aarch64-musl-clang" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
bench_build=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bench_sysroot="$bench_build/musl-arm"
bench_runtime="$bench_build/compiler-rt-arm/lib/linux/libclang_rt.builtins-aarch64.a"
bench_link=1
for bench_arg in "$@"; do
    case "$bench_arg" in
        -c|-S|-E|-M|-MM|-fsyntax-only|--version|-dumpmachine|-dumpversion|-print-*) bench_link=0 ;;
    esac
done
bench_common=(--target=aarch64-linux-musl --sysroot="$bench_sysroot" -isystem "$bench_sysroot/include" -L"$bench_sysroot/lib" -mno-outline-atomics -fuse-ld=lld -Qunused-arguments)
if (( bench_link )); then
    exec clang "${bench_common[@]}" -static -nostdlib "$bench_sysroot/lib/crt1.o" "$bench_sysroot/lib/crti.o" "$@" -Wl,--start-group "$bench_runtime" "$bench_sysroot/lib/libc.a" -Wl,--end-group "$bench_sysroot/lib/crtn.o"
fi
exec clang "${bench_common[@]}" "$@"
WRAPPER
chmod +x "$bench_build/aarch64-musl-clang"

make -C "$bench_build/redis-native" -j"$bench_jobs" MALLOC=libc BUILD_TLS=no USE_SYSTEMD=no \
    OPTIMIZATION=-O2 redis-server redis-cli redis-benchmark > "$bench_build/logs/redis-native-build.log" 2>&1
make -C "$bench_build/redis-arm" -j"$bench_jobs" MALLOC=libc BUILD_TLS=no USE_SYSTEMD=no \
    OPTIMIZATION=-O2 CC="$bench_build/aarch64-musl-clang" AR=llvm-ar RANLIB=llvm-ranlib \
    uname_M=aarch64 LDFLAGS=-static redis-server redis-cli redis-benchmark \
    > "$bench_build/logs/redis-arm-build.log" 2>&1
for bench_binary in redis-server redis-cli redis-benchmark; do
    cp "$bench_build/redis-native/src/$bench_binary" "$bench_build/bin/native/"
    cp "$bench_build/redis-arm/src/$bench_binary" "$bench_build/bin/aarch64/"
done
if [[ ${2:-} == --memtier ]]; then
    fetch libevent-2.1.12-stable.tar.gz \
        https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz \
        92e6de1be9ec176428fd2367677e61ceffc2ee1cb119035037a27d346b0403bb
    # 2.4.4 fixed timestamp averaging that could add/drop whole seconds in the
    # throughput denominator. Use a release containing that upstream fix.
    # https://github.com/redis/memtier_benchmark/releases/tag/2.4.4
    fetch memtier_benchmark-2.5.1.tar.gz \
        https://github.com/redis/memtier_benchmark/archive/refs/tags/2.5.1.tar.gz \
        9b34e17a0d1d7e70b152eb442c6362161b5b764ce2ea98e97b7c74815bdd90b7
    fetch autoconf_2.71-2_all.deb \
        https://archive.ubuntu.com/ubuntu/pool/main/a/autoconf/autoconf_2.71-2_all.deb \
        96b528889794c4134015a63c75050f93d8aecdf5e3f2a20993c1433f4c61b80e
    fetch automake_1.16.5-1.3_all.deb \
        https://archive.ubuntu.com/ubuntu/pool/main/a/automake-1.16/automake_1.16.5-1.3_all.deb \
        59e3890fc8407bcf8ccc9f709d6513156346d5c942e8c624dc90435e58f6f978
    fetch libtool_2.4.6-15build2_all.deb \
        https://archive.ubuntu.com/ubuntu/pool/main/libt/libtool/libtool_2.4.6-15build2_all.deb \
        fb994bc0152f4e77a791b51bc54fd5e38963aa6473e60c45369ef373b6119124
    fetch libpcre3-dev_8.39-13ubuntu0.22.04.1_amd64.deb \
        https://archive.ubuntu.com/ubuntu/pool/main/p/pcre3/libpcre3-dev_8.39-13ubuntu0.22.04.1_amd64.deb \
        79762f3fcd3a29ee9c9c0faf61c0d0292b83b9d07e0bd37566401f0e2109ca87
    extract libevent-2.1.12-stable.tar.gz libevent-src
    extract memtier_benchmark-2.5.1.tar.gz memtier-2.5.1-src
    mkdir -p "$bench_build/build-tools"
    for bench_package in autoconf_2.71-2_all.deb automake_1.16.5-1.3_all.deb \
        libtool_2.4.6-15build2_all.deb libpcre3-dev_8.39-13ubuntu0.22.04.1_amd64.deb; do
        dpkg-deb -x "$bench_build/downloads/$bench_package" "$bench_build/build-tools"
    done
    # Debian's build tools contain absolute data paths. Relocate this private copy.
    python3 - "$bench_build/build-tools" <<'RELOCATE'
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
for path in root.rglob('*'):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        original = path.read_text()
    except (UnicodeError, OSError):
        continue
    updated = original
    for relative in ['share/autoconf', 'share/automake-1.16', 'share/aclocal',
                     'share/libtool', 'bin/autoconf', 'bin/autoheader', 'bin/autom4te']:
        updated = updated.replace('/usr/' + relative, str(root / 'usr' / relative))
    if updated != original:
        path.write_text(updated)
for name in ('automake', 'aclocal'):
    link = root / 'usr/bin' / name
    if not link.exists():
        link.symlink_to(name + '-1.16')
RELOCATE
    (
        cd "$bench_build/libevent-src"
        ./configure --prefix="$bench_build/native-deps" --disable-shared --enable-static \
            --disable-openssl --disable-libevent-regress --disable-samples \
            > "$bench_build/logs/libevent-configure.log" 2>&1
        make -j"$bench_jobs" > "$bench_build/logs/libevent-build.log" 2>&1
        make install > "$bench_build/logs/libevent-install.log" 2>&1
    )
    (
        cd "$bench_build/memtier-2.5.1-src"
        cp "$bench_build/libevent-src/build-aux/"{config.guess,config.sub} .
        PATH="$bench_build/build-tools/usr/bin:$PATH" \
            ACLOCAL_PATH="$bench_build/build-tools/usr/share/aclocal:/usr/share/aclocal" \
            autoreconf -fiv > "$bench_build/logs/memtier251-autoreconf.log" 2>&1
        PKG_CONFIG_PATH="$bench_build/native-deps/lib/pkgconfig" \
            CPPFLAGS="-I$bench_build/build-tools/usr/include" \
            LDFLAGS="-L$bench_build/build-tools/usr/lib/x86_64-linux-gnu" \
            ./configure --disable-tls --prefix="$bench_build/native-deps" \
            > "$bench_build/logs/memtier251-configure.log" 2>&1
        make -j"$bench_jobs" > "$bench_build/logs/memtier251-build.log" 2>&1
        cp memtier_benchmark "$bench_build/bin/native/"
    )
fi
sha256sum "$bench_build"/bin/{native,aarch64}/* > "$bench_build/binary-sha256.txt"
bench_manifest_args=("$bench_build")
if [[ ${2:-} == --memtier ]]; then
    bench_manifest_args+=(--memtier)
fi
python3 "$bench_repo/tools/redis-bench/write_build_manifest.py" "${bench_manifest_args[@]}"
printf 'Redis binaries: %s/bin/{native,aarch64}\n' "$bench_build"
printf 'Guest C compiler: %s/aarch64-musl-clang\n' "$bench_build"
