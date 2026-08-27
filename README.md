# psp2_err

Offline PS Vita error-code lookup.

## Install

```sh
python -m pip install .
```

Use without installation:
```sh
PYTHONPATH=src python -m psp2_err
```
## Use

Look up a display code, hexadecimal or signed decimal value, or SDK symbol:

```sh
psp2_err C2-2000-2
psp2_err 0x80100600
psp2_err -2146433536
psp2_err SCE_APPUTIL_ERROR_PARAMETER
```

Look up several codes:

```sh
psp2_err C1-2738-0 NW-2029-3 0x800f0516
```

Search names and remarks:

```sh
psp2_err --search "savedata slot"
psp2_err --search "network" --limit 10
```

Use `--limit 0` for all search results.

Output JSON:

```sh
psp2_err --json 0x80100600
psp2_err --json --search "CMA data verify"
```

Show all options:

```sh
psp2_err --help
```
