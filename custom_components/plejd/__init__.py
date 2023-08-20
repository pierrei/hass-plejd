import logging

from home_assistant_bluetooth.models import BluetoothServiceInfoBleak

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

import pyplejd

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT, Platform.SWITCH, Platform.BUTTON, Platform.EVENT]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    plejdManager = pyplejd.PlejdManager(config_entry.data)
    await plejdManager.init()

    devices = plejdManager.devices
    scenes = plejdManager.scenes

    # Add a service entry if there are no devices - just so the user can get diagnostics data
    if sum(d.outputType in [pyplejd.LIGHT, pyplejd.SWITCH] for d in devices) == 0:
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, config_entry.data["siteId"])},
            manufacturer="Plejd",
            name=plejdManager.site_data.get("site", {}).get("title", "Unknown site"),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    data = hass.data[DOMAIN].setdefault(config_entry.entry_id, {})
    data.update(
        {
            "stopping": False,
            "devices": devices,
            "scenes": scenes,
            "manager": plejdManager,
        }
    )

    # Close any stale connections that may be open
    for dev in devices:
        ble_device = bluetooth.async_ble_device_from_address(hass, dev.BLEaddress, True)
        if ble_device:
            await plejdManager.close_stale(ble_device)

    # Search for devices in the mesh
    def _discovered_plejd(service_info: BluetoothServiceInfoBleak, *_):
        plejdManager.add_mesh_device(service_info.device, service_info.rssi)
        hass.async_create_task(plejdManager.ping())

    config_entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _discovered_plejd,
            BluetoothCallbackMatcher(
                connectable=True, service_uuid=pyplejd.const.PLEJD_SERVICE.lower()
            ),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    # Run through already discovered devices and add plejds to the mesh
    for service_info in bluetooth.async_discovered_service_info(hass, True):
        if pyplejd.PLEJD_SERVICE.lower() in service_info.advertisement.service_uuids:
            plejdManager.add_mesh_device(service_info.device, service_info.rssi)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Ping mesh intermittently to keep the connection alive
    async def _ping(now=None):
        if data["stopping"]:
            return
        if not await plejdManager.ping():
            _LOGGER.debug("Ping failed")

    # hass.async_create_task(_ping())
    config_entry.async_on_unload(
        async_track_time_interval(hass, _ping, plejdManager.ping_interval)
    )

    # Cleanup when Home Assistant stops
    async def _stop(ev):
        data["stopping"] = True
        await plejdManager.disconnect()

    config_entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        if entry.entry_id in hass.data[DOMAIN]:
            del hass.data[DOMAIN][entry.entry_id]
    return unload_ok
