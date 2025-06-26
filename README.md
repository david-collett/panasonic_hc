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

## Installation

The simplest method is using 'HACS':

- Go to HACS / Integrations
- Click the 3 dots in the top right
- Select "Custom repositories"
- Add the repository URL
- Select category Integration
- Click ADD
- Now from HACS / Integrations you can find Panasonic H&C and click Download
- Restart Home Assistant

Home assistant should now detect compatible Panasonic controllers.

## Notes

The controller requires pairing/bonding. Instructions depend upon your bluetooth setup.

### Local Bluetooth Adapter

For setups using a local bluetooth adapter, this can be done from the command line:

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

### Esphome Bluetooth Proxies

If your home assistant uses esphome Bluetooth proxies, some configuration of your esphome device is required. This has been tested with esphome 2025.6.1. Note that 2025.6.0 had a critical bug that breaks pairing. Earlier versions may also work.

1. Ensure bluetooth proxy is enabled and configured for active connections:


```
bluetooth_proxy:
  active: true
```

2. Add a `ble_client` section for your Panasonic Remote. You will need to know the mac address.
The below configuration will send an event to homeassistant when a pairing request is received.
For this to work, your esphome device must be configured to [allow actions](https://esphome.io/components/api.html#api-actions).

```
esp32_ble:
  io_capability: display_yes_no

ble_client:
  - mac_address: "AA:BB:CC:DD:EE:FF"
    id: panasonic_hc
    auto_connect: False
    on_numeric_comparison_request:
      then:
        - homeassistant.event:
            event: esphome.numeric_comparison_request
            data_template:
              pin: !lambda 'return passkey;'
```

3. Add an action (service) to your api section to perform pairing:

```
api:
  ...
  actions:
    - action: numeric_comparison_reply
      then:
        - ble_client.numeric_comparison_reply:
            id: panasonic_hc
            accept: True
```

4. Create automations to simplify paring using the Home Assistant companion mobile app:

This step is optional, as it is possible to manually listen for the pairing event, and manually invoke the pairing action using the Developer Tools in home assistant.
The following currently only works with the Android Companion app, as `TAG` is used to pass the device_id between automations. There may be a better way, but I couldn't find it!

Create an automation to receive the pairing request and send a notification to your phone (replace `<your_phone>` as appropriate):
```
alias: Panasonic HC Pairing Event
description: ""
triggers:
  - trigger: event
    event_type: esphome.numeric_comparison_request
    event_data: {}
conditions: []
actions:
  - action: notify.mobile_app_<your_phone>
    metadata: {}
    data:
      title: Pairing Request
      message: >-
        Panasonic HC wants to pair with {{
        device_attr(trigger.event.data['device_id'], 'name') }}, using PIN: {{
        trigger.event.data['pin'] }}. Please confirm below and on the Panasonic
        Remote
      data:
        tag: "{{trigger.event.data['device_id']}}"
        actions:
          - action: NUMERIC_COMPARISON_CONFIRM
            title: Confirm PIN
mode: single
```

Create a second automation to receive the notification action when Confirm PIN is selected, and invoke the pairing action:

```
alias: Panasonic HC Pairing Action
description: ""
triggers:
  - trigger: event
    event_type: mobile_app_notification_action
    event_data:
      action: NUMERIC_COMPARISON_CONFIRM
conditions: []
actions:
  - action: >-
      {{'esphome.'~slugify(device_attr(trigger.event.data['tag'],
      'name'))~'_numeric_comparison_reply' }}
    data: {}
mode: single
```

Now when Home Assistant uses your active bluetooth proxy to connect to the Panasonic remote, you should receive a notification on your phone showing the pairing PIN and requesting confirmation. Compare this PIN with what is displayed on your Panasonic Remote. You must confirm on both the Panasonic remote and your phone (order doesnt matter).
