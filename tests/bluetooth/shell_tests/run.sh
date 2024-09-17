#!/usr/bin/env bash

# We don't want to execute if we have a build error
set -eu

uart_h2c=/tmp/repl/myid/uart.h2c
uart_c2h=/tmp/repl/myid/uart.c2h

hci_sim="nrf/tests/bluetooth/shell_tests/app/hci_sim"

# Build controller image
pushd $(west topdir)/${hci_sim}
west build -b nrf52_bsim
popd

# It's ok if the FIFO already exists
set +eu

mkdir -p $(dirname ${uart_h2c})
mkfifo ${uart_h2c}
mkfifo ${uart_c2h}

# Cleanup all existing sims
"${BSIM_COMPONENTS_PATH}/common/stop_bsim.sh"


# Force sim to (kinda) real-time
"${BSIM_OUT_PATH}/bin/bs_device_handbrake" -s=myid -d=0 -r=10 &

# Start the PHY
pushd "${BSIM_OUT_PATH}/bin"
./bs_2G4_phy_v1 -s=myid -D=2 &

# This talks to the REPL
hci_uart="$(west topdir)/${hci_sim}/build/hci_sim/zephyr/zephyr.exe"
gdb -ex "r" -ex "bt" --args $hci_uart \
    -s=myid -d=1 -RealEncryption=0 -rs=70 \
    -fifo_0_rx=${uart_h2c} \
    -fifo_0_tx=${uart_c2h} &
