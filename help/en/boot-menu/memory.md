# Memory

Control the compressed zRAM swap created for this entry.

## zRAM state

**Automatic** keeps the MiniOS memory defaults. **Disabled** adds `nozram` and
disables the compression and size controls because they no longer apply.

## Compression

`zramcomp=` selects the compression algorithm. Available choices are `lzo`,
`lzo-rle`, `lz4`, `lz4hc`, and `zstd`. Algorithm availability also depends on
the running kernel.

## Size

`zramsize=` sets the zRAM size in MiB. Leave the field empty to let MiniOS
calculate the size automatically. A larger value is not free physical memory:
compressed pages still consume RAM.
