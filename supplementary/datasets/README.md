# Datasets

The project uses no external dataset: the workloads generate their own data, and the "dataset" is the raw eBPF traces recorded in the final experiments.

| File | Contents | Size |
|---|---|---|
| `raw-wsl.tar.gz` | main environment (WSL2, kernel 6.6): `final-wsl/` (204 runs, the main evaluation) and `delay-sweep-wsl/` (108 runs, injected-delay sweep) | 11 MB |
| `raw-vm.tar.gz` | robustness VM (Ubuntu 24.04, kernel 6.8): `final-vm/` (204 runs) and `delay-sweep-vm/` (108 runs) | 9 MB |

Each run directory holds `events.bin`, `gt.jsonl`, `stats.json` and `meta.json`; the formats are described in `documentation/reproducibility_guide.md` (Section 8).

**Not included:** the raw traces of the overhead experiments (about 1.6 GB for WSL), because of their size. Their processed results are in `../experimental_results/wsl/overhead-v2.*` and `../experimental_results/vm/overhead.*`; `code/scripts/run_final_wsl.sh` regenerates the raw data (Section 7 of the guide).

Extract with, from `code/`:

```bash
mkdir -p ../raw && tar xzf ../supplementary/datasets/raw-wsl.tar.gz -C ../raw
```
