# mSBC report decoder

`decode_reports.c` and `capture_core.c` read a bounded stream of 124-byte Bluetooth HID reports from standard input and write 16 kHz mono signed 16-bit little-endian PCM to standard output. They do not open a device or network connection.

The decoder accepts 2–400 reports, limits execution with a process alarm, disables core dumps, and wipes mutable input/output and codec state before exit. It is an alternative direct-HID component, not the native-provider capture owner.

Build with a target compiler and target `libsbc`:

```sh
make -C voice/tv/decoder CC=arm-linux-gnueabi-gcc PKG_CONFIG=target-pkg-config
```

Obtain and comply with the codec dependency's license separately; see the project [third-party notices](../../../THIRD_PARTY_NOTICES.md).
