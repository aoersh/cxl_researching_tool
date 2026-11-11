#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
    echo "Usage: sudo bash $0 [before|after|snapshot]"
    echo 'Reads endpoint/upstream link, AER, firmware and health state into a new /tmp directory.'
    exit 0
fi
if (( EUID != 0 )); then
    echo "Run: sudo bash $0 [before|after|snapshot]" >&2
    exit 1
fi
label=${1:-snapshot}
if [[ ! "$label" =~ ^(before|after|snapshot)$ ]]; then
    echo 'Label must be before, after or snapshot.' >&2
    exit 1
fi
result_dir=$(mktemp -d "/tmp/cxl-link-${label}.XXXXXX")
finish() {
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown -R -- "$SUDO_UID:$SUDO_GID" "$result_dir"
    fi
    echo "Results: $result_dir"
}
trap finish EXIT
date -Is > "$result_dir/time.txt"
uname -a > "$result_dir/kernel.txt"
for address in 0000:32:02.0 0000:33:00.0 0000:b3:02.0 0000:b4:00.0; do
    lspci -D -vv -s "$address" > "$result_dir/pci-${address}.txt" 2>&1 || true
    device_path="/sys/bus/pci/devices/$address"
    for field in current_link_speed current_link_width max_link_speed max_link_width \
                 aer_dev_correctable aer_dev_nonfatal aer_dev_fatal \
                 aer_rootport_total_err_cor aer_rootport_total_err_nonfatal aer_rootport_total_err_fatal; do
        if [[ -r "$device_path/$field" ]]; then
            cat "$device_path/$field" > "$result_dir/${address}-${field}.txt"
        fi
    done
done
cxl list -M -H > "$result_dir/cxl-health.txt" 2>&1 || true
for device in mem0 mem1; do
    for field in firmware_version serial; do
        if [[ -r "/sys/bus/cxl/devices/$device/$field" ]]; then
            cat "/sys/bus/cxl/devices/$device/$field" > "$result_dir/${device}-${field}.txt"
        fi
    done
done
dmesg --ctime > "$result_dir/dmesg.txt" 2>&1 || true
