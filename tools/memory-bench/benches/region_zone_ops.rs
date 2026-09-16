//! Native memory software operations. Hardware maintenance is excluded on hosts.

use std::{
    hint::black_box,
    time::{Duration, Instant},
};

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};
use hvisor_memory_bench::{region_zone::mapping_at, Fixture, Mapping, PAGE_SIZE};

struct Workload {
    prefill_regions: usize,
    prefill_pages: usize,
    target_pages: usize,
}

fn parameter(name: &str, default: usize, max: usize) -> usize {
    let value = std::env::var(name)
        .map(|text| text.parse::<usize>().expect("invalid benchmark parameter"))
        .unwrap_or(default);
    assert!((1..=max).contains(&value), "invalid {name}");
    value
}

impl Workload {
    fn new() -> Self {
        for name in ["REGIONS", "ZONE_REGIONS", "ZONE_REGION_PAGES"] {
            assert!(
                std::env::var_os(name).is_none(),
                "{name} is no longer supported: use PREFILL_REGIONS and PREFILL_REGION_PAGES for region setup; zone cases are empty"
            );
        }
        let workload = Self {
            prefill_regions: parameter("PREFILL_REGIONS", 32, 4096),
            prefill_pages: parameter("PREFILL_REGION_PAGES", 1024, 32768),
            target_pages: parameter("REGION_PAGES", 1024, 32768),
        };
        assert!(
            workload.prefill_regions * workload.stride() + workload.target_pages <= 65536,
            "prefill slots and the insertion target exceed the 256 MiB test range"
        );
        workload
    }

    fn stride(&self) -> usize {
        self.prefill_pages.max(self.target_pages)
    }

    fn background(&self) -> Vec<Mapping> {
        (0..self.prefill_regions)
            .map(|index| mapping_at(index * self.stride(), self.prefill_pages))
            .collect()
    }

    fn target(&self, index: usize) -> Mapping {
        mapping_at(index * self.stride(), self.target_pages)
    }

    fn id(&self) -> String {
        format!(
            "prefill_{}_regions_{}_pages/target_{}_pages",
            self.prefill_regions, self.prefill_pages, self.target_pages
        )
    }
}

fn populate<'a>(fixture: &mut Fixture, mappings: impl IntoIterator<Item = &'a Mapping>) {
    for region in mappings {
        fixture.insert(region).expect("region setup failed");
    }
}

fn region_benches(c: &mut Criterion) {
    let workload = Workload::new();
    let background = workload.background();
    let insert_target = workload.target(workload.prefill_regions);
    let mut fixture = Fixture::new();
    fixture.assert_all_frames_returned();

    // Check the complete N -> N+1 -> N lifecycle before warmup.
    fixture.add_empty_zone().unwrap();
    populate(&mut fixture, &background);
    fixture.check_mappings(&background);
    fixture.insert(&insert_target).unwrap();
    fixture.check_mappings(background.iter().chain(std::iter::once(&insert_target)));
    fixture.check_region_pages(&insert_target, true);
    fixture.remove(&insert_target).unwrap();
    fixture.check_mappings(&background);
    fixture.check_region_pages(&insert_target, false);
    fixture.remove_zone().unwrap();
    fixture.check_removed();

    fixture.assert_all_frames_returned();

    let fixture = black_box(&mut fixture);
    let background = black_box(background.as_slice());
    let insert_target = black_box(&insert_target);
    let mut group = c.benchmark_group("region");
    // The prefill size and page count do not change the one-operation divisor.
    group.throughput(Throughput::Elements(1));
    group.bench_function(BenchmarkId::new("insert", workload.id()), |b| {
        b.iter_custom(|rounds| {
            let mut elapsed = Duration::ZERO;
            for _ in 0..rounds {
                fixture.add_empty_zone().expect("zone setup failed");
                populate(fixture, background);
                fixture.check_mappings(background);
                let start = Instant::now();
                let result = fixture.insert(black_box(insert_target));
                elapsed += start.elapsed();
                black_box(result).expect("region insert failed");
                fixture.check_mappings(background.iter().chain(std::iter::once(insert_target)));
                fixture.check_region_pages(insert_target, true);
                // Full teardown prevents retained page tables changing later runs.
                fixture.remove_zone().expect("zone cleanup failed");
                fixture.check_removed();
            }
            elapsed
        });
    });

    group.bench_function(
        BenchmarkId::new("remove", format!("after_insert/{}", workload.id())),
        |b| {
            b.iter_custom(|rounds| {
                let mut elapsed = Duration::ZERO;
                for _ in 0..rounds {
                    fixture.add_empty_zone().expect("zone setup failed");
                    populate(fixture, background);
                    // Reproduce the insert case's final state: N background regions
                    // plus the same newly inserted target, all outside removal timing.
                    fixture.insert(insert_target).expect("target setup failed");
                    fixture.check_mappings(background.iter().chain(std::iter::once(insert_target)));
                    let start = Instant::now();
                    let result = fixture.remove(black_box(insert_target));
                    elapsed += start.elapsed();
                    black_box(result).expect("region remove failed");
                    fixture.check_mappings(background);
                    fixture.check_region_pages(insert_target, false);
                    fixture.remove_zone().expect("zone cleanup failed");
                    fixture.check_removed();
                }
                elapsed
            });
        },
    );
    group.finish();
    fixture.assert_all_frames_returned();
}

fn zone_benches(c: &mut Criterion) {
    let mut fixture = Fixture::new();
    fixture.assert_all_frames_returned();
    for _ in 0..2 {
        fixture.create_zone(&[]).unwrap();
        fixture.check_zone(&[]);
        fixture.remove_zone().unwrap();
        fixture.check_removed();
    }
    fixture.assert_all_frames_returned();

    let fixture = black_box(&mut fixture);
    let input = black_box(&[] as &[Mapping]);
    let mut group = c.benchmark_group("zone_memory");
    group.throughput(Throughput::Elements(1));
    group.bench_function(BenchmarkId::new("create", "empty"), |b| {
        b.iter_custom(|rounds| {
            let mut elapsed = Duration::ZERO;
            for _ in 0..rounds {
                let start = Instant::now();
                let result = fixture.create_zone(input);
                elapsed += start.elapsed();
                black_box(result).expect("empty zone create failed");
                fixture.check_zone(input);
                fixture.remove_zone().expect("zone cleanup failed");
                fixture.check_removed();
            }
            elapsed
        });
    });
    group.bench_function(BenchmarkId::new("remove", "empty"), |b| {
        b.iter_custom(|rounds| {
            let mut elapsed = Duration::ZERO;
            for _ in 0..rounds {
                fixture.create_zone(input).expect("zone setup failed");
                fixture.check_zone(input);
                let start = Instant::now();
                let result = fixture.remove_zone();
                elapsed += start.elapsed();
                black_box(result).expect("empty zone remove failed");
                fixture.check_removed();
            }
            elapsed
        });
    });
    group.finish();
    fixture.assert_all_frames_returned();
}

fn configured_criterion() -> Criterion {
    let workload = Workload::new();
    eprintln!(
        "Host software benchmark: prefill_regions={}, prefill_region_bytes={}, target_region_bytes={}; one timed region operation; zone_memory=empty; hardware maintenance excluded",
        workload.prefill_regions,
        workload.prefill_pages * PAGE_SIZE,
        workload.target_pages * PAGE_SIZE
    );
    Criterion::default()
        .sample_size(100)
        .warm_up_time(Duration::from_secs(3))
        .measurement_time(Duration::from_secs(10))
        .confidence_level(0.95)
}

criterion_group! {
    name = benches;
    config = configured_criterion();
    targets = region_benches, zone_benches
}
criterion_main!(benches);
