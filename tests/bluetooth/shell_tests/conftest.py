##########################################################################################
# Copyright (c) Nordic Semiconductor ASA. All Rights Reserved.
#
# The information contained herein is confidential property of Nordic Semiconductor ASA.
# The use, copying, transfer or disclosure of such information is prohibited except by
# express written agreement with Nordic Semiconductor ASA.
##########################################################################################

import os
import sys
import threading
import time
from pathlib import Path
import re
import queue
import subprocess
import pytest


class PipeSerial:
    def __init__(self, h2c_fifo: Path):
        self.fifo_tx = Path(h2c_fifo)
        self.fifo_rx = Path(str(h2c_fifo).replace("h2c", "c2h"))
        self.name = str(str(h2c_fifo).replace("-h2c.fifo", ""))
        self.lock = Path(str(h2c_fifo).replace("-h2c.fifi", ".lock"))

    def open(self):
        if self.lock.is_file():
            print(f"Device {self.name} already opened (or dead).", file=sys.stderr)
            print(f"hint: delete {self.lock} to regain control.", file=sys.stderr)
            raise Exception(f"Device {self.name} already open")

        try:
            self.rx = open(self.fifo_rx, "rb", buffering=0)
            self.tx = open(self.fifo_tx, "wb", buffering=0)
        except OSError as e:
            raise Exception(f"Failed to open {e.filename} (err: {e.errno})")

        with open(self.lock, "w") as file:
            file.write(f"locked by PID {os.getpid()}\n")

        print(f"Opened device {self.name}")

    def close(self):
        self.rx.close()
        self.tx.close()

        if self.lock.is_file():
            self.lock.unlink()

    def read(self, size: int):
        chars = b""

        try:
            chars = self.rx.read(size)

            if len(chars) != size:
                # Oh no! The computer said we got fewer bytes than we expected!
                # This usually means our friend on the other side stopped talking to us.
                # Since they can't talk again, we should stop talking too.
                #
                # When we stop talking, it will make the computer get upset next time,
                # and then it will tell the grown-ups (the upper layer) to stop the talking machine.
                print(
                    f"Pipe to {self.name} disconnected, terminating transport thread."
                )
                self.close()
        except BlockingIOError:
            # Wait a little bit, but don't wait forever!
            #
            # We're using a special kind of talking called "non-blocking".
            # This is because sometimes someone might want to stop talking quickly.
            # There's a magic button called "Close" that people can press.
            # When they press it, we set up a flag that says "I'm done!"
            # But we don't stop talking right away. Instead, we keep talking
            # until we've finished what we were saying.
            # After we finish talking, we stop completely.
            time.sleep(0.001)
        except ValueError as v:
            if "I/O operation on closed file" in str(v):
                return b""
            else:
                raise v

        return chars

    def write(self, payload: bytes) -> int:
        try:
            print(f"{payload}")
            written = self.tx.write(payload)
        except BlockingIOError:
            raise Exception("OS failed to write")

        if written != len(payload):
            raise Exception(f"OS only wrote {written} out of {len(payload)} bytes")

        return written


class SerialThread(threading.Thread):
    def __init__(self, com_port, bsim_device=None, rx_handler=None):
        super().__init__(daemon=True)

        self.com_port = com_port
        self.bsim_device = bsim_device

        self.rx_handler = rx_handler

        self._____stop_rx_flag = threading.Event()

    def run(self):
        while not self._____stop_rx_flag.is_set():
            recv = self.uart.read(1)

            if recv == b"":
                time.sleep(0.0001)
                continue

            self.rx_handler(recv)

    def open(self):
        if self.bsim_device:
            self.uart = PipeSerial(self.com_port)
        else:
            self.uart = None

        self.uart.open()

        self.start()

    def close(self):
        self._____stop_rx_flag.set()
        self.join()
        self.uart.close()

    def send(self, data: bytearray):
        self.uart.write(data)


class ShellDevice:
    def __init__(self, com_port: str, baud_rate: int = 1000000, bsim_device=False):
        self.thread = SerialThread(com_port, bsim_device=bsim_device, rx_handler=self.rx_handler)

        self.rx_buf = b""
        self.rx_queue = queue.Queue()

    def open(self):
        self.thread.open()

    def close(self):
        self.thread.close()

    def rx_handler(self, data: bytes):
        self.rx_buf += data

        if self.rx_buf.endswith(b"\n"):
            self.rx_queue.put(self.rx_buf)
            self.rx_buf = b""

    def send_cmd(self, cmd: str):
        cmd += "\n"
        data = bytearray(cmd, encoding="utf-8")

        self.thread.send(data)

    def _bytes_to_str(self, data: bytes):
        return "".join(f"{byte:02x}" for byte in data)

    def wait_for(self, regex: str, timeout: float = 5) -> str:
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if self.rx_queue.empty():
                time.sleep(.0001)
                continue

            queued_data = self.rx_queue.get()
            data_str = queued_data.decode("utf-8")

            if re.match(regex, data_str) is not None:
                return data_str

        return None

    def pipe_to_stdout(self):
        while True:
            if self.rx_queue.empty():
                time.sleep(.0001)

            queued_data = self.rx_queue.get()
            data_str = queued_data.decode("utf-8")

            print(data_str, end="", file=sys.stdout)


def get_bsim_out_path():
    try:
        bsim_out_path = Path(os.environ["BSIM_OUT_PATH"]).expanduser()
    except KeyError:
        raise Exception("BSIM_OUT_PATH is not set")
    except RuntimeError:
        raise Exception("Could not resolve HOME directory")

    return bsim_out_path


def stop_bsim():
    bsim_out_path = get_bsim_out_path()

    stop_bsim_path = bsim_out_path / "components" / "common" / "stop_bsim.sh"
    stop_bsim_cmd = [ stop_bsim_path ]

    try:
        subprocess.run(
            stop_bsim_cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise Exception(f"Error: failed to run '{stop_bsim_path}'")


def instantiate_hci_uart_devices(num_devices: int, image_path: Path, simulation_id: str):
    bsim_id = "shell_tests"
    simulation_id = bsim_id
    # TODO: use real temp folder
    fifo_path = Path("/tmp/shell_tests")

    bsim_out_path = get_bsim_out_path()

    # hci_uart_exe = "app/hci_sim/build/hci_sim/zephyr/zephyr.exe"
    hci_uart_exe = image_path
    handbrake_exe = bsim_out_path / "bin" / "bs_device_handbrake"
    phy_exe = bsim_out_path / "bin" / "bs_2G4_phy_v1"

    stop_bsim()

    devices = []

    for i in range(num_devices):
        uart_h2c = fifo_path / f"d{i}-h2c.fifo"
        uart_c2h = fifo_path / f"d{i}-c2h.fifo"
        lock_file = fifo_path / f"d{i}.lock"

        os.makedirs(uart_c2h.parent, exist_ok=True)

        if not uart_h2c.exists():
            os.mkfifo(uart_h2c)
        if not uart_c2h.exists():
            os.mkfifo(uart_c2h)

        pipes = {"h2c": uart_h2c, "c2h": uart_c2h}

        rand_seed = i * 100

        hci_uart_cmd = [
            hci_uart_exe,
            f"-s={simulation_id}",
            f"-d={i}",
            "-RealEncryption=1",
            f"-rs={rand_seed}",
            f"-fifo_0_rx={uart_h2c}",
            f"-fifo_0_tx={uart_c2h}",
        ]
        process = subprocess.Popen(hci_uart_cmd)

        devices.append({
            "process": process,
            "pipes": pipes,
            "lock": lock_file,
        })

    handbrake_cmd = [
        handbrake_exe,
        f"-s={simulation_id}",
        f"-d={num_devices}",
        "-r=10"
    ]
    subprocess.Popen(handbrake_cmd, cwd=handbrake_exe.parent)

    phy_cmd = [
        phy_exe,
        f"-s={simulation_id}",
        f"-D={num_devices + 1}"
    ]
    subprocess.Popen(phy_cmd, cwd=phy_exe.parent)

    return devices


@pytest.fixture
def two_device_fixture(request):
    test_name = request.node.name

    hci_uart_exe = Path("app/hci_sim/build/hci_sim/zephyr/zephyr.exe")

    devices = instantiate_hci_uart_devices(2, hci_uart_exe, test_name)

    d1 = ShellDevice(devices[0]["pipes"]["h2c"], bsim_device=True)
    d2 = ShellDevice(devices[1]["pipes"]["h2c"], bsim_device=True)

    d1.open()
    d2.open()

    yield (d1, d2)

    stop_bsim()
    d1.close()
    d2.close()
    stop_bsim()


def _main():
    hci_uart_exe = Path("app/hci_sim/build/hci_sim/zephyr/zephyr.exe")

    devices = instantiate_hci_uart_devices(1, hci_uart_exe, "myid")

    d1 = ShellDevice(devices[0]["pipes"]["h2c"], bsim_device=True)
    # d2 = ShellDevice(devices[1]["pipes"]["h2c"], bsim_device=True)

    d1.open()
    # d2.open()

    # while True:
        # time.sleep(.0001)

    time.sleep(5)

    d1.send_cmd("bt init")

    d1.pipe_to_stdout()

    d1.wait_for(r"Bluetooth initialized.*")

    print("bt initialized")

    # devices[0]["process"].kill()
    stop_bsim()
    d1.close()
