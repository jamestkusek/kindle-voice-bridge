# Bluetooth recovery

The Bluetooth handoff stops the stock Bluetooth daemon (`btd`) and takes
`/dev/ttymxc2` from `bsa_server`. The supervisor restores Bluetooth on a normal
exit. The supervisor cannot restore Bluetooth after SIGKILL, SIGSEGV, a kernel
hang, or a power loss. Stock Bluetooth remains off in those cases.

Recovery is not guaranteed. A reboot is not guaranteed to restore Bluetooth.

## First step

Reboot the Kindle. Wait for the device to start. Check whether Bluetooth is
enabled in the Kindle UI.

## Manual commands

Run these commands in a device shell when a reboot does not restore Bluetooth:

```sh
printf 0 > /proc/bluetooth/sleep/proto
printf 0 > /proc/bluetooth/sleep/btwake
printf 0 > /proc/bluetooth/btenable
initctl restart btd
lipc-set-prop com.lab126.btfd BTenable 1:1
```

These commands are not a guarantee.

## After a hard crash

A SIGKILL or a hard crash leaves the control socket and the lock file stale. The
next session start removes the stale socket. Restore the stock Bluetooth daemon
first.

The control socket is `/tmp/voicebridge-bt-session.sock`. The state file is
`/tmp/voicebridge-bt-session.state`. The daemon removes both files on a normal
stop.
