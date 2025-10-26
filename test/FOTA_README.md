# FOTA Demo Steps

1) Build your firmware to produce the raw binary, e.g. `build/ecowatt.bin`.
2) Run:
   ./generate_fota_artifacts.sh ./build/ecowatt.bin 32768 1.2.0 ./fota_out
3) Copy all files from `./fota_out/logs/` into your server's `logs/` directory.
4) In server admin, trigger the device to pull the manifest:
   - First use `fota_manifest_bad.json` to demo **verify fail → rollback**.
   - Then swap to `fota_manifest_good.json` to demo **success → reboot → boot_ok**.
