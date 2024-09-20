##########################################################################################
# Copyright (c) Nordic Semiconductor ASA. All Rights Reserved.
#
# The information contained herein is confidential property of Nordic Semiconductor ASA.
# The use, copying, transfer or disclosure of such information is prohibited except by
# express written agreement with Nordic Semiconductor ASA.
##########################################################################################

import time


def test_adv_scan(two_device_fixture):
    d1, d2 = two_device_fixture

    d1.send_cmd("bt init")
    d2.send_cmd("bt init")

    time.sleep(1)

    d1.send_cmd("bt advertise on")

    d2.send_cmd("bt scan on")

    result = d2.wait_for(r".*AD evt type 4.*", timeout=2)

    print(f"{result}")

    assert "Nordic-host" in result
