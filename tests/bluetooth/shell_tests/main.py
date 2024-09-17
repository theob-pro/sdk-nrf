#!/usr/bin/env python
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


class PipeSerial:
    def __init__(self, h2c_fifo: str):
        self.fifo_tx = Path(h2c_fifo)
        self.fifo_rx = Path(h2c_fifo.replace("h2c", "c2h"))
        self.name = h2c_fifo.replace("-h2c.fifo", "")
        self.lock = Path(h2c_fifo.replace("-h2c.fifi", ".lock"))

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
            chars = self.rx.read(size=size)

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

        return chars

    def write(self, payload: bytes) -> int:
        try:
            written = self.tx.write(payload)
        except BlockingIOError:
            raise Exception("OS failed to write")

        if written != len(payload):
            raise Exception(f"OS only wrote {written} out of {len(payload)} bytes")

        return written


class SerialThread(threading.Thread):
    def __init__(self, com_port, bsim_device=None, rx_handler=None):
        super.__init__(daemon=True)

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
        self.thread = SerialThread(None, bsim_device=bsim_device, rx_handler=self.rx_handler)

        self.rx_buf = b""
        self.rx_queue = queue.Queue()

    def open(self):
        self.thread.open()

    def rx_handler(self, data: bytes):
        self.rx_buf += data

        if self.rx_buf.endswith(b"\n"):
            self.rx_queue.put(self.rx_buf)
            self.rx_buf = b""

    def send_cmd(self, cmd: str):
        cmd += "\n"
        data = bytearray(cmd)

        self.thread.send(data)

    def _bytes_to_str(self, data: bytes):
        return "".join(f"{byte:02x}" for byte in data)

    def wait_for(self, regex: str) -> str:
        match_found = False
        compiled_regex = re.compile(regex)

        while not match_found:
            if self.rx_queue.empty():
                time.sleep(.0001)
                continue

            queued_data = self.rx_queue.get()
            data_str = self._bytes_to_str(queued_data)

            if compiled_regex.match(data_str):
                match_found = True

        return data_str


def main():
    pass


if __name__ == "__main__":
    main()
