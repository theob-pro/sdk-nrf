##########################################################################################
# Copyright (c) Nordic Semiconductor ASA. All Rights Reserved.
#
# The information contained herein is confidential property of Nordic Semiconductor ASA.
# The use, copying, transfer or disclosure of such information is prohibited except by
# express written agreement with Nordic Semiconductor ASA.
##########################################################################################

from conftest import Command


def test_adv_scan_2(two_device_fixture_2):
    d1, d2 = two_device_fixture_2

    bt_init = Command("bt init", "Bluetooth initialized")
    bt_advertiser_on = Command("bt advertise on", "Advertising started")
    bt_scan_on = Command("bt scan on", "Bluetooth active scan enabled")

    d1.send_cmd_sync(bt_init)
    d2.send_cmd_sync(bt_init)

    d1.send_cmd_sync(bt_advertiser_on)

    d2.send_cmd_sync(bt_scan_on)

    result = d2.wait_for(r".*AD evt type 4.*", timeout=2)

    print(f"{result}")

    assert "Nordic-host" in result
