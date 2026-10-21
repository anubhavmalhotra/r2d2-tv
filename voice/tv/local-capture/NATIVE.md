# Native capture and supervision

The preload adapts one exact ARM provider image. The provider retains its hardware/codec work; decoded PCM is redirected to a private inherited socket. The supervisor controls the child and its threads. Neither a build nor matching dependency versions proves target loading or physical capture.

## Native contract

- `owned_provider.cpp` resolves the main image's load bias and validates the known BLE post-decode callback. The accepted path uses the first PCM buffer with a null second buffer. Unknown layouts are refused.
- `startup_lock.hpp` admits the exact original `open` request for an existing empty, root-owned, private, single-link regular lockfile. It removes creation, adds no-follow/nonblocking/close-on-exec flags, and verifies inode identity around the open. It does not grant a general write-open or `openat` exception.
- `native_handoff.cpp` bounds accepted audio and couples STOP, release/cancel, and watchdog state. The owner compares actual drained bytes with the native completion report before sending an accepted utterance.
- `provider_supervisor.c` and `supervised_policy.h` deny internet sockets and escape operations, restrict the supported local descriptor handoff, and report actual image verification and child retirement. Owner loss has a bounded retirement path; an unconfirmed stop/reap remains a failure.

The native source pins the provider image and callback offsets. Porting requires inspecting the new image and its calling convention, local bus protocol, dependencies, and stop behavior. Do not reuse offsets based only on a product family or OS version label.

## Build

```sh
python3 voice/tv/local-capture/build_native.py \
  --sdk-root "$ARM_SDK" \
  --provider-image "$PRIVATE_ORIGINAL_IMAGE" \
  --output "$NEW_NATIVE_BUILD" --build
```

Supply `--runtime-library SONAME=PATH` for each captured target dependency when checking version requirements. The builder records source/artifact hashes and ABI metadata; absent dependency checks must not be treated as successful load qualification. Original binaries are private inputs, not redistributed project source.

```sh
python3 -B -m unittest discover -s voice/tv/local-capture -p 'test_*.py'
```

The host tests compile portable native logic and regression controls. A target run must separately confirm service registration, callback selection, decoded audio, native STOP, complete pipe draining, child retirement, and original-state recovery. No fixed exit-time guarantee is implied.
