## Loop 10 First Post-Deploy Comparison

- Date: 2026-07-17
- Commit under test: `19a566e`
- CI run: `29572859355`, success
- Deployed image: `helloworldz1024/mysearch-stack:sha-19a566e`
- Output: `raw/loop10-remote-compare.csv`

## Integrity

- 41 rows and 41 unique benchmark IDs
- 41 captured
- 0 structural failures
- 0 MySearch timeouts
- 0 MySearch empty results
- 0 row errors

## New Finding

- `factual-accuracy-01` answered `The latest stable version of Python is 3.15.`
- Python's official downloads page in the same raw result identifies `3.15` as a development/pre-release version and `3.14.6` as the latest stable download.
- This is an actionable MySearch postprocessing regression, so this comparison is diagnostic only and is not the final Loop 10 comparison.
