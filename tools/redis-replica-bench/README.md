# Redis benchmark runners

These runners compare native and integrated hvisor with one root VM, a Redis
primary at `10.20.0.11`, and a read-only replica at `10.20.0.12`. The current
matched checkouts are `../tmp/hvisor-native` and `../tmp/hvisor-integrated`
relative to the hvisor repository; override them with the runner options when
needed.

## Images

Build the immutable images only when they are absent:

```sh
sudo platform/aarch64/qemu-gicv3-redis/image-build/build-images.sh
```

The builder refuses to overwrite completed images and installs the root-side
deployment, population, and workload scripts as part of the root image. QEMU
uses a disposable snapshot for every sample, so the source image is unchanged.

## Deployment timing

`run-deployment.py` measures one empty-dataset deployment. The paired runner
alternates native and integrated order and stops on the first failure unless
`--keep-going` is supplied:

```sh
python3 tools/redis-replica-bench/run-deployment-paired.py \
  --pairs 5 --qemu-cpus 0-5 \
  --root-disk platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4
```

These results cover VM readiness, Redis readiness, initial synchronization,
and the deployment marker. They do not measure steady-state workload time.

## Steady-state workload

`run-workload-sample.py` runs one sample; `run-workload-paired.py` runs balanced
pairs. Population uses one pipelined RESP stream. The measured memtier load
uses pipeline depth 1 and interprets `--get-set-ratio` as GET:SET.

The default paired command is a small diagnostic: one pair, 1,000 keys,
64-byte values, 10 seconds, and a 7:3 GET:SET ratio:

```sh
python3 tools/redis-replica-bench/run-workload-paired.py \
  --pairs 1 --key-count 1000 --value-size 64 --duration 10 \
  --get-set-ratio 7:3 --qemu-cpus 0-5 \
  --root-disk platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4 \
  --output target/redis-replica-bench/workload-paired-small
```

Use at least five pairs for a pilot after this check is stable. Every sample
starts a new QEMU process, populates a fresh primary, synchronizes the replica,
and records stage timings, memtier output, replication polls, and console logs.
The current gate checks root CPUs, Redis roles, valid replication polls, zero
reported GET misses, a final `WAIT 1` marker read on the replica, and fatal
console signatures. It does not yet enforce lag, resynchronization, eviction,
or resource-use bounds.

The verified small pair is
`target/redis-replica-bench/workload-paired-small-20260918`: both samples
passed; native/integrated throughput was 157.72/142.76 ops/s, p50 latency was
166.91/183.29 ms, and p99 latency was 860.16/872.45 ms. The integrated sample
reported a 1,746-byte sampled maximum lag, showing why a zero final lag alone
is not a sufficient continuous-replication result.

The completed four-pair pilot is
`target/redis-replica-bench/workload-paired-20260917T170051Z`. It used the
older five-second monitor, so its zero-lag values are descriptive only and do
not establish continuously zero lag.

Outputs are ignored under `target/redis-replica-bench/`; retain a result
directory when its report or raw logs are needed.
