"""List connected receivers and the SDR_DEVICE value that selects each one.

    docker compose run --rm scanner python3 -m app.devices

Stop the scanner first (docker compose stop) if it is using a receiver you
want to see: a receiver that is in use may not be listed.
"""

from collections import Counter

from .sources import DRIVER_NAMES, list_devices


def main():
    devs = list_devices()
    if not devs:
        print("No receivers found. Is it plugged in?")
        return
    dupes = Counter((d["driver"], d.get("serial")) for d in devs)
    for d in devs:
        name = DRIVER_NAMES.get(d["driver"], d["driver"])
        print(f"{name:12s} {d.get('label', '')}")
        print(f"    SDR_DEVICE: \"driver={d['driver']},serial={d.get('serial', '?')}\"")
    for (driver, serial), n in dupes.items():
        if n > 1:
            print(f"\nWarning: {n} {DRIVER_NAMES.get(driver, driver)} receivers share serial {serial}, "
                  "so they can't be told apart.")
            if driver == "rtlsdr":
                print("Give each dongle its own serial (plug in one at a time):\n"
                      "    rtl_eeprom -s 00000002")


if __name__ == "__main__":
    main()
