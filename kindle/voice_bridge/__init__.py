"""Device-side capture and upload for Kindle Voice Bridge.

The entry points are:

- ``bt_session.py``: the resident Bluetooth session the KOReader plugin drives.
- ``capture_local.py``: the one-shot capture, run from a device shell.

The remaining modules are support code: ``pw4_supervisor.py`` for the Bluetooth
handoff, ``uploader.py`` for the receiver client, and ``hci_relay.py`` /
``launch_hci.py`` / ``link_probe.py`` for the HCI probe path.
"""
