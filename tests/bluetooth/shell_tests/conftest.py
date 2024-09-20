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
from abc import ABC, abstractmethod
import struct
import stat


class Command():
    def __init__(self, cmd: str, cmd_complete: str):
        self.PB_MSG_WAIT = 0x01

        self.cmd = cmd
        self.cmd_complete = f"{cmd_complete}"


class PipeSerial:
    def __init__(self, h2c_fifo: Path):
        self.fifo_tx = Path(h2c_fifo)
        self.fifo_rx = Path(str(h2c_fifo).replace("h2c", "c2h"))
        self.name = str(str(h2c_fifo).replace("-h2c.fifo", ""))
        self.lock = Path(str(h2c_fifo).replace("-h2c.fifo", ".lock"))

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

        with open(self.lock, "w") as f:
            f.write(f"locked by PID {os.getpid()}\n")

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
            # import pdb;pdb.set_trace()
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
        # import pdb;pdb.set_trace()
        self.uart.write(data)


class Device(ABC):
    @abstractmethod
    def __init__(self, com_port: str, baud_rate: int = 1000000, bsim_device: bool = False):
        pass

    @abstractmethod
    def open(self):
        pass

    @abstractmethod
    def close(self):
        pass

    @abstractmethod
    def rx_handler(self):
        pass

    @abstractmethod
    def send_cmd(self):
        pass


class BsimTimeManager():
    # The device wants to wait
    PB_MSG_WAIT = 0x01
    # The device is disconnecting from the phy or viceversa:
    PB_MSG_DISCONNECT = 0xFFFF
    # The device is disconnecting from the phy, and is requesting the phy to end the simulation ASAP
    PB_MSG_TERMINATE = 0xFFFE
    # The requested time tick has just finished
    PB_MSG_WAIT_END = 0x81

    def __init__(self, device_id: int, simulation_id: str):
        self.device_id = device_id
        self.simulation_id = simulation_id

        self.time = 0

    def __write_to_phy(self, data: bytes):
        self.phy_fifo_tx.write(data)

    def __read_from_phy(self, nbytes: int) -> bytes:
        data = self.phy_fifo_rx.read(nbytes)

        if len(data) != nbytes:
            raise Exception(f"Did not receive the expected number of bytes when reading from PHY (got {len(data)} when {nbytes} was expected)")

        return data

    def connect(self):
        user = get_user()

        com_folder = Path(f"/tmp/bs_{user}/{self.simulation_id}")
        os.makedirs(com_folder, stat.S_IRWXG | stat.S_IRWXU, exist_ok=True)

        lock = com_folder / f"2G4.d{self.device_id}.lock"

        if lock.exists():
            raise Exception(f"Lock file exist (you may want to delete '{lock}')")

        with open(lock, "w") as f:
            f.write(f"{os.getpid()}\n")

        self.phy_fifo_rx_path = com_folder / f"2G4.d{self.device_id}.ptd"
        self.phy_fifo_tx_path = com_folder / f"2G4.d{self.device_id}.dtp"

        if not self.phy_fifo_rx_path.exists():
            os.mkfifo(self.phy_fifo_rx_path, stat.S_IRWXG | stat.S_IRWXU)
        if not self.phy_fifo_tx_path.exists():
            os.mkfifo(self.phy_fifo_tx_path, stat.S_IRWXG | stat.S_IRWXU)

        # import pdb;pdb.set_trace()
        # the order is important see:
        # https://github.com/EDTTool/EDTT/blob/b9ca3c7030518f07b7937dacf970d37a47865a76/src/components/edttt_bsim.py#L91
        self.phy_fifo_rx = open(self.phy_fifo_rx_path, "rb", buffering=0)
        self.phy_fifo_tx = open(self.phy_fifo_tx_path, "wb", buffering=0)

    def disconnect(self):
        self.phy_fifo_rx.close()
        self.phy_fifo_tx.close()

        self.phy_fifo_rx_path.unlink(missing_ok=True)
        self.phy_fifo_rx_path.unlink(missing_ok=True)

    def _wait_until_t(self, wait_end: int):
        # wait_end is in ms
        msg = struct.pack("=IQ", self.PB_MSG_WAIT, wait_end)
        self.__write_to_phy(msg)

        self.time = wait_end

        raw_header = self.__read_from_phy(4)
        header, = struct.unpack("=I", raw_header)

        if header == self.PB_MSG_DISCONNECT:
            raise Exception("Simulation terminated by the PHY")
        elif header != self.PB_MSG_WAIT_END:
            raise Exception(f"Low level communication with PHY failed. Received invalid response {header}")

        # wait a bit to avoid being locked if wait is called too quickly after
        time.sleep(.001)

    def wait(self, wait_time: int):
        # import pdb; pdb.set_trace()
        # print(f'Wait {wait_time} ms, self.time {self.time}')
        wait_end = self.time + (wait_time * 1000)

        self._wait_until_t(wait_end)


class ShellDevice(Device):
    def __init__(self, com_port: str, baud_rate: int = 1000000, bsim_device=False, time_manager: BsimTimeManager = None):
        self.thread = SerialThread(com_port, bsim_device=bsim_device, rx_handler=self.rx_handler)

        self.rx_buf = b""
        self._rx_log = b""
        self.rx_queue = queue.Queue()

        self.time_manager = time_manager

    def open(self):
        self.thread.open()

    def close(self):
        self.thread.close()

    def rx_handler(self, data: bytes):
        self.rx_buf += data
        self._rx_log += data

        if self.rx_buf.endswith(b"\n"):
            self.rx_queue.put(self.rx_buf)
            self.rx_buf = b""

    def send_cmd(self, cmd: str):
        cmd += "\n"
        data = bytearray(cmd, encoding="utf-8")

        self.thread.send(data)

    def send_cmd_sync(self, cmd: Command, timeout: int = 5):
        self.send_cmd(cmd.cmd)

        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if self.rx_queue.empty():
                self.time_manager.wait(100)
                continue

            line_out = self.rx_queue.get().decode("utf-8").strip()

            if line_out == cmd.cmd_complete:
                return

        raise Exception(f"Command timeout: {cmd.cmd}. The line {cmd.cmd_complete} was not found after {timeout}s.")

    def wait_for(self, regex: str, timeout: int = 5) -> str:
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if self.rx_queue.empty():
                # time.sleep(.0001)
                self.time_manager.wait(10)
                continue

            queued_data = self.rx_queue.get()
            data_str = queued_data.decode("utf-8")

            if re.match(regex, data_str) is not None:
                return data_str

        return None

    @property
    def rx_log(self):
        return self._rx_log.decode("utf-8")

    @rx_log.setter
    def rx_log(self, value):
        self._rx_log = value

    @rx_log.deleter
    def rx_log(self):
        del self._rx_log

    def pipe_to_stdout(self):
        while True:
            if self.rx_queue.empty():
                time.sleep(.0001)

            queued_data = self.rx_queue.get()
            data_str = queued_data.decode("utf-8")

            print(data_str, end="", file=sys.stdout)


def get_bsim_out_path() -> Path:
    try:
        bsim_out_path = Path(os.environ["BSIM_OUT_PATH"]).expanduser()
    except KeyError:
        raise Exception("BSIM_OUT_PATH is not set")
    except RuntimeError:
        raise Exception("Could not resolve HOME directory")

    return bsim_out_path


def get_user() -> str:
    try:
        user = os.environ["USER"]
    except KeyError:
        raise Exception("Environment variable '$USER' is not set")

    return user


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
    # TODO: use real temp folder
    fifo_path = Path(f"/tmp/shell_tests/{simulation_id}")

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

    # handbrake_cmd = [
    #     handbrake_exe,
    #     f"-s={simulation_id}",
    #     f"-d={num_devices}",
    #     "-r=10"
    # ]
    # subprocess.Popen(handbrake_cmd, cwd=handbrake_exe.parent)

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

    bold = "\x1b[1m"
    reset = "\x1b[0m"

    print(f"\n\n{bold}--- Device 1:{reset}\n{d1.rx_log}", end="")
    print(f"\n\n{bold}--- Device 2:{reset}\n{d2.rx_log}", end="")

    stop_bsim()
    d1.close()
    d2.close()
    stop_bsim()


@pytest.fixture
def two_device_fixture_2(request):
    test_name = request.node.name

    hci_uart_exe = Path("app/hci_sim/build/hci_sim/zephyr/zephyr.exe")

    devices = instantiate_hci_uart_devices(2, hci_uart_exe, test_name)

    time_manager = BsimTimeManager(2, test_name)

    d1 = ShellDevice(devices[0]["pipes"]["h2c"], bsim_device=True, time_manager=time_manager)
    d2 = ShellDevice(devices[1]["pipes"]["h2c"], bsim_device=True, time_manager=time_manager)

    d1.open()
    d2.open()

    time_manager.connect()

    yield (d1, d2)

    bold = "\x1b[1m"
    reset = "\x1b[0m"

    print(f"\n\n{bold}--- Device 1:{reset}\n{d1.rx_log}", end="")
    print(f"\n\n{bold}--- Device 2:{reset}\n{d2.rx_log}", end="")

    stop_bsim()
    time_manager.disconnect()
    d1.close()
    d2.close()
    stop_bsim()
