# Panasonic H&C Home Assistant Integration

This integration supports Bluetooth enabled wired remote controllers
for Panasonic reverse cycle air conditioning units supported by the
"Panasonic H&C Control" Andriod and iOS apps.

It is currently being developed and tested with the CZ-RTC6BLW controller.

It uses local BLE control and does not require any cloud connection or
registration.

The integration provides a Climate entity for basic control
(mode, target temp, fan speed, powersave mode).

It also exposes an energy sensor that can be added to energy dashboard.
Note that the energy sensor only receives new data hourly for the 
previous hour so it is not entirely accurate in energy dashboard.

## Notes

The controller requires pairing/bonding. For hass installations with
a local bluetooth controller, this can be done from the command line:

```
bluetoothctl
scan on
<Wait for the thermostat to show up and copy the MAC address>
scan off
pair <MAC>
<The thermostat will display a code, confirm it is correct and hit enter on the thermostat.>
trust <MAC>
disconnect <MAC>
exit
```

If your home assistant uses Bluetooth proxies, it will not currently
work, and may continually try (and fail) to connect. This may be
resolved in the future, let me know if you are interested in a solution.
